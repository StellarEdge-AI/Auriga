// Existing Playwright installation may be supplied with PLAYWRIGHT_MODULE=/path/to/index.mjs.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';

const engines = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const url = process.env.AURIGA_URL || 'http://127.0.0.1:8765';
const browser = await engines[process.env.AURIGA_BROWSER || 'chromium'].launch({ headless: true, ...(process.env.AURIGA_BROWSER_EXECUTABLE ? { executablePath: process.env.AURIGA_BROWSER_EXECUTABLE } : {}) });
const page = await browser.newPage();
page.setDefaultTimeout(Number(process.env.AURIGA_TIMEOUT_MS || 30000));
const errors = [];
const checks = [];
const limitations = [];
const writes = [];
let actionEvidence = null;
let sceneEvidence = null;
page.on('pageerror', (error) => errors.push(error.message));
page.on('request', (request) => { if (request.url().endsWith('/v1/control') && request.method() === 'POST') writes.push(request.postDataJSON()); });
const run = () => page.evaluate(async () => (await fetch('/v1/run')).json());

try {
  await page.goto(url);
  await page.waitForSelector('#terminal .xterm-screen');
  await page.waitForFunction(() => document.getElementById('terminal-status').textContent.includes('实时'));
  await page.waitForFunction(() => {
    const image = document.getElementById('camera-image');
    return !image.hidden && image.complete && image.naturalWidth > 0;
  });
  const before = await run();
  const identity = (value) => value.thread_id || value.codex?.thread_id || value.terminal?.thread_id;
  assert.ok(identity(before), 'the gateway must expose its real Codex thread identity');
  await page.evaluate(() => { window.checkedTerminalScreen = document.querySelector('#terminal .xterm-screen'); });
  await page.locator('.raw-activity > summary').click();
  const activitySelector = process.env.AURIGA_REQUIRE_ACTION
    ? '#activity button[data-event-type="action.completed"]' : '#activity button[data-action-id]:not([data-action-id=""]), #activity button[data-event-type="observation.available"]';
  await page.waitForSelector(activitySelector);
  const selectedAction = await page.locator(activitySelector).first().getAttribute('data-action-id');
  await page.locator(activitySelector).first().click();
  await page.waitForSelector('#inspector:not([hidden])');
  assert.equal(await page.evaluate(() => window.checkedTerminalScreen === document.querySelector('#terminal .xterm-screen')), true, 'history inspection must retain the live terminal');
  assert.match(await page.locator('#terminal-status').textContent(), /实时/);
  await page.waitForSelector('#return-live:not([hidden])');
  if (process.env.AURIGA_REQUIRE_ACTION) {
    actionEvidence = await page.evaluate(async (identifier) => {
      const read = async (path) => {
        const response = await fetch(path);
        if (!response.ok) throw new Error(`${path}: ${response.status}`);
        return response.json();
      };
      const action = await read(`/v1/actions/${encodeURIComponent(identifier)}`);
      const observations = [];
      for (const [kind, id] of [['input', action.request.based_on_observation_id], ['execution_start', action.receipt.execution_start_observation_id], ['result', action.receipt.result_observation_id]]) {
        if (!id) throw new Error(`completed action lacks ${kind} observation`);
        const observation = await read(`/v1/observations/${encodeURIComponent(id)}`);
        observations.push({ kind, observation_id: id, run_id: observation.run_id, env_generation: observation.env_generation,
          sim_step: observation.sim_step, step_domain: observation.step_domain, data_origin: observation.data_origin,
          last_action_id: observation.last_action_id, camera_count: observation.cameras?.length || 0 });
      }
      const traces = [];
      for (const kind of ['planned', 'dispatched', 'actual']) {
        const reference = action.receipt[`${kind}_trace_ref`];
        if (!reference?.startsWith('artifact:')) throw new Error(`completed action lacks ${kind} trace`);
        const value = await read(`/v1/artifacts/${encodeURIComponent(reference.slice(9))}`);
        traces.push({ kind, reference, json_bytes: new TextEncoder().encode(JSON.stringify(value)).length });
      }
      return { action_id: identifier, status: action.receipt.status, observations, traces };
    }, selectedAction);
    assert.equal(actionEvidence.status, 'completed');
    for (const observation of actionEvidence.observations) {
      assert.equal(observation.run_id, before.run_id, 'action observations must belong to the current run');
      assert.ok(observation.camera_count > 0, 'action observations must retain robot media');
      if (!process.env.AURIGA_SYNTHETIC) assert.equal(observation.data_origin, 'robodojo_runtime', 'runtime action evidence must come from RoboDojo');
    }
    assert.equal(actionEvidence.observations.at(-1).last_action_id, selectedAction);
    checks.push('completed action retains input/start/result observations and readable planned/dispatched/actual traces');
  }
  await page.locator('#return-live').click();
  assert.equal(await page.locator('#return-live').isVisible(), false);
  checks.push('history retains live terminal and exact observation IDs');
  await page.locator('.native-panel > summary').click();
  await page.waitForSelector('#native-items details');
  await page.locator('.scene-panel > summary').click();
  const availableVersions = await page.evaluate(async () => (await (await fetch('/v1/scene/versions')).json()).versions || []);
  if (availableVersions.length) {
    await page.waitForSelector('#scene-version option', { state: 'attached' });
    if (!process.env.AURIGA_SYNTHETIC) {
      const version = availableVersions.at(-1).scene_version ?? availableVersions.at(-1);
      await page.locator('#scene-version').selectOption(String(version));
      await page.waitForFunction((expected) => {
        try { return JSON.parse(document.getElementById('scene-data').textContent).scene_version === expected; }
        catch { return false; }
      }, version);
      const scene = JSON.parse(await page.locator('#scene-data').textContent());
      const tracked = Object.entries(scene.entities || {}).find(([, entity]) => entity.state === 'tracked');
      sceneEvidence = { scene_version: version, entity_id: tracked?.[0], observation_ids: scene.observation_ids };
      if (tracked) await page.locator('#scene-query').fill(JSON.stringify({ kind: 'entity_pose', entity_id: tracked[0] }));
      else limitations.push('Immutable scene exists but has no tracked proxy; geometry query cannot establish a known pose.');
    }
    await page.locator('#scene-query-form button').click();
    await page.waitForFunction((kind) => {
      try {
        const value = JSON.parse(document.getElementById('scene-data').textContent);
        return Object.hasOwn(value, 'status') && (!kind || value.query_type === kind);
      } catch { return false; }
    }, sceneEvidence?.entity_id ? 'entity_pose' : null);
    if (sceneEvidence?.entity_id) {
      const query = JSON.parse(await page.locator('#scene-data').textContent());
      assert.ok(['supported', 'conditional'].includes(query.status), 'tracked geometry query must return evidence');
      assert.equal(query.world_kind, 'internal');
      sceneEvidence.query_status = query.status;
      await page.locator('#scene-snapshot').click();
      await page.waitForFunction(() => {
        try { return !!JSON.parse(document.getElementById('scene-data').textContent).snapshot_id; }
        catch { return false; }
      });
      const snapshot = JSON.parse(await page.locator('#scene-data').textContent());
      assert.equal(snapshot.scene_version, sceneEvidence.scene_version);
      assert.equal(snapshot.world_kind, 'internal');
      assert.equal(snapshot.quality?.artifact_resolution, 'verified_digest_closed');
      sceneEvidence.snapshot_id = snapshot.snapshot_id;
      sceneEvidence.anchor_observation_id = snapshot.anchor_observation_id;
      sceneEvidence.artifact_resolution = snapshot.quality.artifact_resolution;
      checks.push('runtime tracked proxy query and immutable geometry snapshot');
    }
    checks.push('native item records and internal geometry query');
  } else {
    assert.equal(await page.locator('#scene-snapshot').isDisabled(), true);
    limitations.push('Runtime has no immutable scene version; snapshot/query/alignment acceptance remains separate.');
    checks.push('native item records; unavailable scene snapshot stays disabled');
  }
  if (process.env.AURIGA_SYNTHETIC) {
    const request = async (path, body, headers = {}) => fetch(new URL(path, url), { method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) });
    const stats = async () => (await fetch(new URL('/fixture/stats', url))).json();
    const deliveries = page.locator('#activity button[data-event-type="observation.delivered"]');
    assert.equal(await deliveries.filter({ hasText: 'Astra 模型已接收观察' }).count(), 1);
    assert.equal(await deliveries.filter({ hasText: '程序已接收观察' }).count(), 1);
    checks.push('program observations remain distinct from actual Astra model image delivery');
    await page.locator('#activity button[data-action-id="action-completed"]').click();
    await page.waitForSelector('#associated-native details');
    const actual = page.locator('#trace-records details').filter({ hasText: '实际实测' });
    await actual.locator('summary').click();
    await page.waitForFunction(() => document.getElementById('trace-records').textContent.includes('"kind": "actual"'));
    await page.locator('#activity button[data-action-id="action-failed"]').click();
    await page.waitForFunction(() => document.getElementById('trace-records').textContent.includes('实际实测 · 未记录'));
    await page.locator('#frame-timeline').focus();
    await page.keyboard.press('End');
    await page.waitForFunction(() => document.getElementById('camera-empty').textContent.includes('媒体读取失败'));
    assert.equal(await page.locator('#camera-image').isVisible(), false, 'missing media must clear stale image');
    await page.locator('#return-live').click();
    checks.push('planned/dispatched/actual records; failed trace and missing media remain explicit; keyboard timeline');
    await page.locator('#claim-input').click();
    await page.waitForFunction(() => document.getElementById('input-status').textContent.includes('本窗口'));
    const observer = await browser.newPage();
    observer.on('pageerror', (error) => errors.push(error.message));
    await observer.goto(url);
    await observer.waitForFunction(() => document.getElementById('input-status').textContent.includes('另一窗口'));
    assert.equal(await observer.locator('#submit-task').isDisabled(), true);
    const denied = await observer.evaluate(async () => {
      const response = await fetch('/v1/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request_id: crypto.randomUUID(), command: 'prompt', client_id: 'other-window', writer_token: 'invalid', prompt: 'must not execute' }) });
      return response.status;
    });
    assert.equal(denied, 409, 'server must enforce input ownership');
    const stolenClaim = await observer.evaluate(async () => {
      const state = await (await fetch('/v1/run')).json();
      return (await fetch('/v1/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request_id: crypto.randomUUID(), command: 'claim_input', client_id: state.controller.owner_client_id }) })).status;
    });
    assert.equal(stolenClaim, 409, 'public client ID must not reveal the writer lease token');
    await page.locator('#task-prompt').fill('synthetic subsequent turn');
    await page.locator('#submit-task').click();
    await page.waitForFunction(() => document.getElementById('task-status').textContent.includes('已提交'));
    const approval = page.locator('.pending-request[data-request-id="21"]');
    await approval.locator('select').selectOption('"decline"');
    await approval.locator('form button').click();
    await page.waitForFunction(() => !document.querySelector('.pending-request[data-request-id="21"]'));
    const question = page.locator('.pending-request[data-request-id="22"]');
    await question.locator('input').fill('left');
    await question.locator('form button').click();
    await page.waitForFunction(() => !document.querySelector('.pending-request[data-request-id="22"]'));
    assert.equal((await stats()).prompts, 1);
    assert.equal((await stats()).replies, 2);
    const promptWrite = writes.find((value) => value.command === 'prompt');
    const repeat = await page.evaluate(async (value) => {
      const send = (body) => fetch('/v1/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      return [(await send(value)).status, (await send({ ...value, prompt: 'conflicting content' })).status];
    }, promptWrite);
    assert.deepEqual(repeat, [200, 409], 'identical task retry is idempotent; conflicting request ID is rejected');
    assert.equal((await stats()).prompts, 1);
    await page.locator('#terminal').click();
    await page.keyboard.type('must never reach PTY');
    assert.equal((await stats()).terminal_inputs, 0, 'native TUI remains read-only');
    await observer.close();
    checks.push('one input owner across windows; same-thread task; native approval and question; read-only PTY');
    await request('/fixture/fault', { codex_status: 'completed', active_action: 'motion-still-running' });
    await page.waitForFunction(() => document.getElementById('motion-state').textContent.includes('motion-still-running'));
    assert.match(await page.locator('#turn-state').textContent(), /completed/);
    assert.equal(await page.locator('#submit-task').isDisabled(), true);
    // Inject while the browser is offline so an immediate reconnect cannot consume the one-shot gap.
    await page.context().setOffline(true);
    await request('/fixture/fault', { active_action: null, ws_gap: true, sse_gap: true });
    await page.waitForTimeout(400);
    await page.context().setOffline(false);
    await page.waitForFunction(() => !document.getElementById('connection-gap').hidden);
    await page.waitForFunction(() => document.getElementById('connection-gap').textContent.includes('synthetic_upstream_gap'));
    await page.waitForFunction(() => document.getElementById('terminal-status').textContent.includes('实时'));
    assert.equal(await page.locator('#activity button[data-action-id="action-completed"]').count(), 1, 'SSE replay must deduplicate actions');
    await page.locator('#scene-snapshot').click();
    await page.waitForFunction(() => document.getElementById('scene-records').textContent.includes('snapshot-fixture'));
    await page.locator('#scene-align').fill('{"observation_ids":["obs-4"],"camera_id":"head","points_m":[[0,0,1]],"pixels":[[0,0]],"correspondence_source":"synthetic_fixture"}');
    await page.locator('#scene-align-form button').click();
    await page.waitForFunction(() => document.getElementById('scene-records').textContent.includes('job-alignment · succeeded'));
    checks.push('independent turn/motion states; WS/SSE reconnect and gap; scene snapshot/alignment');
  }
  const origin = await fetch(new URL('/v1/control', url), { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: 'https://untrusted.example' }, body: '{}' });
  assert.equal(origin.status, 403, 'cross-origin controls must be rejected');
  const foreign = await browser.newPage();
  await foreign.goto('data:text/html,<title>Origin test</title>');
  const wsDenied = await foreign.evaluate((endpoint) => new Promise((resolve) => {
    const socket = new WebSocket(endpoint.replace(/^http/, 'ws') + '/v1/terminal');
    socket.onopen = () => { socket.close(); resolve(false); };
    socket.onerror = () => resolve(true);
    setTimeout(() => { socket.close(); resolve(false); }, 5000);
  }), url.replace(/\/$/, ''));
  assert.equal(wsDenied, true, 'cross-origin terminal must be rejected');
  await foreign.close();
  checks.push('HTTP and WebSocket origin restrictions');
  for (const width of [320, 736, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.locator('#terminal-bottom').click();
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, `${width}px viewport overflows`);
    assert.equal(await page.locator('#terminal .xterm-screen').isVisible(), true);
    if (process.env.AURIGA_SCREENSHOT_DIR) {
      await fs.mkdir(process.env.AURIGA_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.AURIGA_SCREENSHOT_DIR, `workbench-${width}.png`), fullPage: true });
    }
  }
  await page.reload();
  await page.waitForFunction(() => document.getElementById('terminal-status').textContent.includes('实时'));
  const after = await run();
  assert.equal(identity(after), identity(before), 'refresh must attach to the same thread');
  if (before.terminal?.pid) assert.equal(after.terminal?.pid, before.terminal.pid, 'refresh must retain the PTY');
  await page.close();
  const reopened = await browser.newPage();
  reopened.on('pageerror', (error) => errors.push(error.message));
  await reopened.goto(url);
  await reopened.waitForFunction(() => document.getElementById('terminal-status').textContent.includes('实时'));
  const reopenedRun = await reopened.evaluate(async () => (await fetch('/v1/run')).json());
  assert.equal(identity(reopenedRun), identity(before), 'close and reopen must retain the thread');
  assert.equal(reopenedRun.terminal?.pid, before.terminal?.pid, 'close and reopen must retain PTY PID');
  checks.push('refresh/resize/close/reopen preserve thread and PTY');
  assert.deepEqual(errors, [], 'browser JavaScript errors');
  const report = { status: 'passed', observed_at: new Date().toISOString(), url,
    browser: process.env.AURIGA_BROWSER || 'chromium', thread_id: identity(after),
    terminal_before: before.terminal, terminal_after: after.terminal, selected_action_id: selectedAction, action_evidence: actionEvidence, scene_evidence: sceneEvidence,
    data_origin: process.env.AURIGA_SYNTHETIC ? 'synthetic_fixture' : 'runtime_service',
    widths: [320, 736, 1024, 1440], checks, limitations, errors };
  if (process.env.AURIGA_SCREENSHOT_DIR) {
    await fs.writeFile(path.join(process.env.AURIGA_SCREENSHOT_DIR, 'browser-report.json'), JSON.stringify(report, null, 2));
  }
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  const state = page.isClosed() ? {} : await page.evaluate(() => Object.fromEntries(['terminal-status','camera-empty','frame-meta','input-status','task-status','scene-data'].map((id) => [id, document.getElementById(id)?.textContent]))).catch(() => ({}));
  if (process.env.AURIGA_SCREENSHOT_DIR && !page.isClosed()) {
    await fs.mkdir(process.env.AURIGA_SCREENSHOT_DIR, { recursive: true });
    await page.screenshot({ path: path.join(process.env.AURIGA_SCREENSHOT_DIR, 'failure.png'), fullPage: true });
  }
  console.error(JSON.stringify({ status: 'failed', error: error.message, checks, errors, state }, null, 2));
  throw error;
} finally {
  await browser.close();
}
