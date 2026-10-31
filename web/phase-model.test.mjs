import assert from 'node:assert/strict';
import test from 'node:test';
import { buildPhases, mapActionVideo, clipTransition } from './phase-model.js';

const native = (id, type, extra = {}) => ({ id, type, thread_id: 'thread', turn_id: 'turn', ...extra });
const tool = (id, extra = {}) => native(id, 'dynamicToolCall', { tool: 'robot_move_ee', ...extra });
const event = (seq, item, action, extra = {}) => ({ server_seq: seq, type: 'action.completed',
  correlation: { item_id: item, ...(action ? { action_id: action } : {}) }, ...extra });
const identity = { run_id: 'run', episode_id: 'episode', env_instance_id: 'env', env_generation: 1 };
const action = (start = 0, end = 10) => ({ request: { ...identity, action_id: 'action' },
  receipt: { execution_interval: { start_sim_step: start, end_sim_step: end, step_domain: 'control' } } });
const record = () => ({ identity: { ...identity }, event: { clock_domain: 'clock' }, entry: { camera_id: 'head', step_domain: 'control',
  frame_mapping: [{ sim_step: 0, pts_s: 0 }, { sim_step: 4, pts_s: 0.8 }, { sim_step: 10, pts_s: 2 }] } });
const result = { ...identity, clock_domain: 'clock', step_domain: 'control', sim_step: 10 };

test('context is bounded by neighboring tools and never crosses thread or turn', () => {
  const items = [native('prompt', 'userMessage', { content: [{ text: 'Recorded task' }] }), native('before', 'agentMessage', { text: 'Recorded plan' }),
    tool('one'), native('other', 'agentMessage', { turn_id: 'elsewhere', text: 'Unrelated' }),
    native('reason', 'reasoning', { content: [], summary: [] }), tool('two'), native('tail', 'agentMessage', { text: 'Recorded finish' }),
    tool('different', { thread_id: 'different' })];
  const phases = buildPhases([event(1, 'one', 'a'), event(4, 'two', 'b')], items);
  const one = phases.find((phase) => phase.item?.id === 'one'), two = phases.find((phase) => phase.item?.id === 'two');
  assert.deepEqual(one.before.map((item) => item.id), ['before']);
  assert.deepEqual(one.after.map((item) => item.id), ['reason']);
  assert.deepEqual(two.before.map((item) => item.id), ['reason']);
  assert.equal(one.prompt.id, 'prompt');
  assert.equal(phases.find((phase) => phase.item?.id === 'different').prompt, null);
  assert.deepEqual(two.before[0].summary, []);
  assert.ok(phases.some((phase) => phase.kind === 'codex' && phase.items[0].id === 'tail'));
});

test('one native tool retains multiple actions and explicit hydration data', () => {
  const original = action();
  const phases = buildPhases([event(3, 'one', 'a'), event(4, 'one', 'b')], [tool('one')], new Map([['a', original], ['b', { action: original, result }]]));
  assert.equal(phases.length, 1);
  assert.deepEqual(phases[0].actions.map((entry) => entry.action_id), ['a', 'b']);
  assert.equal(phases[0].actions[0].action, original);
  assert.equal(phases[0].actions[1].result, result);
});

test('concurrent hydration cannot reverse recorded action order within a phase', () => {
  const phases = buildPhases([event(3, 'one', 'a'), event(4, 'one', 'b')], [tool('one')], new Map([['b', action(10, 20)], ['a', action(0, 10)]]));
  assert.deepEqual(phases[0].actions.map((entry) => entry.action_id), ['a', 'b']);
});

test('missing, conflicting, or wrong-turn association remains a Harness phase', () => {
  const events = [event(10, 'one', null), event(12, 'missing', 'unlinked'), event(13, 'one', 'conflict'), event(14, 'two', 'conflict'),
    event(15, 'one', 'wrong-turn', { correlation: { item_id: 'one', action_id: 'wrong-turn', turn_id: 'other' } }), event(20, 'two', null)];
  const phases = buildPhases(events, [tool('one'), tool('two')]);
  assert.deepEqual(phases.map((phase) => phase.id), ['item:one', 'action:unlinked', 'action:conflict', 'item:two', 'action:wrong-turn']);
  assert.ok(phases.filter((phase) => phase.kind === 'tool').every((phase) => phase.actions.length === 0));
  assert.deepEqual(buildPhases([], [tool('without-events')])[0].actions, []);
  assert.equal(buildPhases([], [tool('without-events')])[0].seq, null);
  const unknownScope = buildPhases([], [{ id: 'prompt', type: 'userMessage', content: [{ text: 'Unknown turn' }] },
    { id: 'tool', type: 'dynamicToolCall', tool: 'robot_observe' }]);
  assert.equal(unknownScope.find((phase) => phase.kind === 'tool').prompt, null);
});

test('native callId is accepted as an explicit item reference, not guessed from action name', () => {
  const phases = buildPhases([{ server_seq: 1, correlation: { tool_call_id: 'one', action_id: 'custom-id' } },
    { server_seq: 2, correlation: { action_id: 'action-one' } }], [tool('one')]);
  assert.equal(phases[0].actions[0].action_id, 'custom-id');
  assert.equal(phases[1].kind, 'harness');
});

test('video uses recorded nonuniform PTS and exact endpoints', () => {
  const mapped = mapActionVideo(record(), action(), result);
  assert.equal(mapped.start.pts_s, 0); assert.equal(mapped.end.pts_s, 2);
  assert.equal(mapped.basis, 'declared_step_domain'); assert.equal(mapped.pause, false);
  assert.match(mapActionVideo(record(), action(1, 10), result).reason, /精确映射/);
  assert.match(mapActionVideo(record(), action(0, 11), result).reason, /精确映射/);
});

test('video rejects generation, result identity, domain, and clock mismatch', () => {
  const changed = record(); changed.identity.env_generation = 2;
  assert.match(mapActionVideo(changed, action(), result).reason, /generation/);
  assert.match(mapActionVideo(record(), action(), { ...result, env_generation: 2 }).reason, /结果观察/);
  const wrongDomain = record(); wrongDomain.entry.step_domain = 'other';
  assert.match(mapActionVideo(wrongDomain, action(), result).reason, /步域/);
  assert.match(mapActionVideo(record(), action(), { ...result, clock_domain: 'other' }).reason, /时钟/);
});

test('undeclared video domain requires exact result frame anchor', () => {
  const video = record(); delete video.entry.step_domain; video.entry.frame_mapping.at(-1).frame_id = 'frame-head';
  assert.match(mapActionVideo(video, action(), result).reason, /步域/);
  const anchored = mapActionVideo(video, action(), { ...result, cameras: [{ camera_id: 'head', frame_id: 'frame-head' }] });
  assert.equal(anchored.basis, 'result_frame_anchor');
  assert.match(mapActionVideo(video, action(), { ...result, cameras: [{ camera_id: 'head', frame_id: 'different' }] }).reason, /步域/);
});

test('zero-length actions are explicit pauses and invalid mapping is rejected', () => {
  assert.equal(mapActionVideo(record(), action(10, 10), result).pause, true);
  const malformed = record(); malformed.entry.frame_mapping[1].pts_s = -1;
  assert.match(mapActionVideo(malformed, action(), result).reason, /无效/);
  const missing = record(); missing.entry.frame_mapping[1] = null;
  assert.match(mapActionVideo(missing, action(), result).reason, /无效/);
  const reversed = record(); reversed.entry.frame_mapping.reverse();
  assert.match(mapActionVideo(reversed, action(), result).reason, /顺序/);
  const conflicting = record(); conflicting.entry.frame_mapping[0].step_domain = 'wrong';
  assert.match(mapActionVideo(conflicting, action(), result).reason, /步域/);
});

const clip = (start, end, startStep, endStep) => ({ url: '/recorded-head.mp4', record: record(), domain: 'control',
  start: { pts_s: start, sim_step: startStep }, end: { pts_s: end, sim_step: endStep } });

test('shared recorded boundaries continue without seeking; repeated boundary frames play only once', () => {
  const before = clip(0, 2, 0, 10);
  assert.deepEqual(clipTransition(before, clip(2, 3, 10, 20)), { kind: 'continuous', continuous: true });
  assert.deepEqual(clipTransition(before, clip(1.8, 3, 10, 20)), { kind: 'overlap', continuous: true });
  assert.equal(clipTransition(before, clip(1, 1.8, 10, 20)).continuous, false, 'never continue into an already finished clip');
});

test('real PTS gaps and incompatible boundaries require an explicit transition', () => {
  const before = clip(0, 2, 0, 10);
  assert.deepEqual(clipTransition(before, clip(2.4, 3, 20, 30)), { kind: 'gap', continuous: false, from: 2, to: 2.4 });
  assert.equal(clipTransition(before, clip(2.00001, 3, 10, 20)).kind, 'gap', 'do not infer a frame interval to hide missing PTS');
  assert.deepEqual(clipTransition(before, clip(2, 3, 20, 30)), { kind: 'discontinuous', continuous: false });
  assert.equal(clipTransition(before, clip(1, 3, 5, 20)).continuous, false, 'overlapping distinct simulation intervals are not a shared boundary');
});

test('changing export, camera, generation or domain cannot reuse the old playback clock', () => {
  const before = clip(0, 2, 0, 10);
  for (const mutate of [
    (value) => { value.url = '/new-export.mp4'; },
    (value) => { value.record.entry.camera_id = 'other-camera'; },
    (value) => { value.record.identity.env_generation = 2; },
    (value) => { value.domain = 'other-domain'; },
    (value) => { delete value.record.identity.episode_id; },
  ]) {
    const after = clip(2, 3, 10, 20); mutate(after);
    assert.deepEqual(clipTransition(before, after), { kind: 'source', continuous: false });
  }
});
