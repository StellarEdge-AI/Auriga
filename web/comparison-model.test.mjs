import assert from 'node:assert/strict';
import test from 'node:test';
import { comparisonCases, directionGroups, formalScore, groupStats, iterationGroups, pairedStats } from './comparison-model.js';
import { formatTime, runTimeText } from './experiment-time.js';

const row = (key, score, summary = {}) => ({ case_key: key, task: 'task', variant: 'standard', eval_seed: 0, layout_id: key,
  run: { summary: { normalized_score: score, scored_episode: true, task_success: score === 1, ...summary } } });
const group = (id, cases, extra = {}) => ({ id, cases, case_count: cases.length, ...extra });

test('a direction includes all versions and each distinct baseline without a four-group cap', () => {
  const versions = Array.from({ length: 6 }, (_, i) => group(`v${i}`, [], { series_name_zh: '同方向', series: `different-${i}`, iteration: i, baseline_group_id: i % 2 ? 'b1' : 'b2' }));
  const groups = [...versions, group('b1', [], { series: 'external', role: 'baseline' }), group('b2', [], { series: 'external', role: 'baseline' }), group('other', [], { series: 'other' })];
  assert.deepEqual(directionGroups(versions[0], groups).map((g) => g.id), ['b1', 'b2', ...versions.map((g) => g.id)]);
  assert.deepEqual(iterationGroups(versions[0], groups).map((g) => g.id), ['v0']);
});

test('timestamps use Beijing seconds regardless of browser timezone and never invent a start from registration', () => {
  assert.equal(formatTime('2026-10-13T16:00:09Z'), '2026-10-14 00:00:09');
  assert.equal(formatTime('2026-10-13T09:00:09-07:00'), '2026-10-14 00:00:09');
  assert.equal(formatTime('2026-10-13T16:00:09'), '时间未记录');
  assert.equal(formatTime(null), '时间未记录');
  assert.equal(formatTime('invalidZ'), '时间未记录');
  const text = runTimeText({ registered_at: '2026-10-13T16:00:09Z', imported_at: '2026-10-14T01:00:00Z' });
  assert.match(text, /开始：时间未记录/); assert.match(text, /登记：2026-10-14 00:00:09/); assert.match(text, /导入：2026-10-14 09:00:00/);
});

test('paired delta uses only common formally scored scenes while missing cases keep the denominator', () => {
  const baseline = group('base', [row('a', 0), row('b', 0.8), row('x', 1), row('c', 0, { scored_episode: false })]);
  const candidate = group('candidate', [row('a', 0.5), row('b', 0.5), row('y', 0), { case_key: 'c', run: null }]);
  assert.equal(groupStats(baseline).mean, 0.6);
  assert.equal(groupStats(candidate).scored, 3);
  assert.equal(groupStats(candidate).count, 4);
  assert.equal(groupStats(candidate).unknown, 1);
  const pairs = pairedStats(candidate, baseline);
  assert.equal(pairs.scored, 2); assert.equal(pairs.matched, 3); assert.equal(pairs.same_cases, false);
  assert.ok(Math.abs(pairs.delta - 0.1) < 1e-9);
  assert.equal(comparisonCases([baseline, candidate]).length, 5);
});

test('unknown identities and duplicate case keys never form invented pairs', () => {
  const a = group('a', [row(null, 1), row('duplicate', 0), row('duplicate', 1)]);
  const b = group('b', [row(null, 1), row('duplicate', 1)]);
  assert.equal(pairedStats(a, b).scored, 0);
  assert.equal(comparisonCases([a, b]).length, 5);
});

test('invalid, partial and infrastructure-only scores are unavailable, including numeric zero', () => {
  for (const summary of [{ normalized_score: 0, valid_episode: false }, { normalized_score: 1, primary_outcome: 'unscored_infrastructure' },
    { normalized_score: 1, infrastructure_error: true }, { normalized_score: 0.5, scored_episode: false }, { normalized_score: true }, { normalized_score: Infinity }]) {
    assert.equal(formalScore(summary), null);
  }
  assert.equal(formalScore({ normalized_score: 0, scored_episode: true }), 0);
  assert.equal(groupStats(group('empty', [])).mean, null);
});

test('recorded iteration order takes precedence over tied or late archive dates', () => {
  const a = group('r0', [], { series: 'method', iteration: 0, created_at: '2026-10-15' });
  const b = group('r1', [], { series: 'method', iteration: 1, parent_group_id: a.id, created_at: '2026-10-14' });
  const c = group('r2', [], { series: 'method', iteration: 2, parent_group_id: b.id, created_at: '2026-10-13' });
  assert.deepEqual(iterationGroups(c, [c, b, a]).map((item) => item.id), ['r0', 'r1', 'r2']);
  const other = group('unrelated', [], { created_at: '2026-10-16' });
  assert.deepEqual(iterationGroups(other, [c, b, a, other]).map((item) => item.id), ['unrelated']);
  a.parent_group_id = c.id;
  assert.equal(iterationGroups(c, [c, b, a]).length, 3, 'malformed cycles terminate');
});
