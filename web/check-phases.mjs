// Read-only acceptance of phase review. Synthetic cases are browser routes only.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const engines = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.AURIGA_URL || 'http://127.0.0.1:8780';
const output = process.env.AURIGA_SCREENSHOT_DIR || 'runs/web-phase-check';
const source = path.dirname(fileURLToPath(import.meta.url));
const preview = process.env.AURIGA_PREVIEW_ASSETS;
const syntheticOnly = process.env.AURIGA_SYNTHETIC_ONLY === '1';
const realOnly = process.env.AURIGA_REAL_ONLY === '1';
assert.ok(!syntheticOnly || preview, 'synthetic-only checks require preview assets');
assert.ok(!(syntheticOnly && realOnly), 'select only one restricted check mode');
const checks = [], evidence = [], errors = [], writes = [], socketWrites = [];
const browser = await engines[process.env.AURIGA_BROWSER || 'chromium'].launch({ headless: true,
  ...(process.env.AURIGA_BROWSER_EXECUTABLE ? { executablePath: process.env.AURIGA_BROWSER_EXECUTABLE } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
context.setDefaultTimeout(Number(process.env.AURIGA_TIMEOUT_MS || 30000));
await fs.mkdir(output, { recursive: true });
await context.route('**/*', async (route) => {
  const request = route.request(), pathname = new URL(request.url()).pathname;
  if (!['GET', 'HEAD'].includes(request.method())) {
    writes.push({ method: request.method(), url: request.url() });
    return route.abort();
  }
  if (preview) {
    const name = path.basename(pathname);
    let file;
    if (/^\/runs\/[^/]+\/$/.test(pathname)) file = path.join(source, 'index.html');
    else if (['app.js', 'app.css'].includes(name)) file = path.join(preview, name);
    else if (['styles.css', 'task-labels.js'].includes(name)) file = path.join(source, name);
    if (file) return route.fulfill({ path: file, contentType: file.endsWith('.html') ? 'text/html' : file.endsWith('.css') ? 'text/css' : 'application/javascript' });
  }
  if (syntheticOnly) return route.fulfill({ status: 404, body: 'synthetic fixture has no such resource' });
  return route.continue();
});
const watch = (page) => {
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('websocket', (socket) => socket.on('framesent', ({ payload }) => {
    let value;
    try { value = JSON.parse(String(payload)); } catch { value = { type: 'unrecognized' }; }
    if (value.type !== 'resize') socketWrites.push(value);
  }));
};
const read = async (endpoint) => {
  const response = await fetch(new URL(endpoint, origin), { signal: AbortSignal.timeout(Number(process.env.AURIGA_TIMEOUT_MS || 30000)) });
  assert.equal(response.status, 200, endpoint);
  return response.json();
};
const selected = (page) => page.locator('#phase-nav button[aria-current="step"]');
const assertSelection = async (page) => {
  assert.equal(await selected(page).count(), 1, 'one current phase in navigation');
  const id = await selected(page).getAttribute('data-phase-id');
  assert.equal(await page.locator('#phase-activity button[aria-current="step"]').count(), 1, 'one current phase in activity');
  assert.equal(await page.locator('#phase-activity button[aria-current="step"]').getAttribute('data-phase-id'), id);
  assert.ok((await page.locator('#phase-title').textContent()).trim());
  return id;
};
const screenshot = async (page, name) => {
  for (const width of [320, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, `${name} at ${width}px`);
    await page.screenshot({ path: path.join(output, `${name}-${width}.png`), fullPage: true });
  }
};
let lastPage;

try {
  let runURL;
  if (!syntheticOnly) {
  const record = process.env.AURIGA_RUN_ID
    ? await read(`/v1/experiments/${encodeURIComponent(process.env.AURIGA_RUN_ID)}`)
    : (await read('/v1/experiments?limit=100&sort=active')).experiments.find((item) => item.replay_available && !item.live && item.terminal_available);
  assert.ok(record, 'an existing recorded run is required');
  runURL = new URL(record.view_url || `/runs/${record.id}/`, origin).href;
  const page = await context.newPage(); lastPage = page; watch(page);
  const run = await read(`/runs/${record.id}/v1/run`);
  await page.goto(runURL);
  await page.waitForSelector('#phase-nav button[data-phase-id]');
  await page.waitForFunction((minimum) => document.querySelectorAll('#phase-nav button.has-video').length >= minimum, Number(process.env.AURIGA_MIN_PLAYABLE_PHASES || 1));
  await page.locator('#phase-nav button.has-video').first().click();
  await page.waitForFunction(() => { const video = document.getElementById('phase-video'); return !video.hidden && video.readyState >= 2 && !video.seeking; });
  await page.locator('#codex-tab-terminal').click();
  await page.waitForSelector('#terminal .xterm-screen');
  await page.locator('#codex-tab-records').click();
  await page.waitForFunction(() => document.getElementById('input-status').textContent.includes('只读'));
  assert.equal(await page.locator('#task-prompt').isDisabled(), true);
  assert.equal(await page.locator('#stop').isDisabled(), true);
  assert.equal(await page.locator('#phase-continuous').isChecked(), true);
  assert.equal(await page.locator('#activity').evaluate((node) => Boolean(node.closest('details') && !node.closest('details').open)), true, 'raw events are folded');
  await page.evaluate(() => { window.checkedTerminalScreen = document.querySelector('#terminal .xterm-screen'); });
  const beforeId = await assertSelection(page);
  if (await page.locator('#phase-next').isEnabled()) {
    await page.locator('#phase-next').click();
    assert.notEqual(await assertSelection(page), beforeId);
    await page.locator('#phase-previous').click();
    assert.equal(await assertSelection(page), beforeId);
  }
  const last = page.locator('#phase-activity button[data-phase-id]').last();
  await last.click();
  assert.equal(await assertSelection(page), await last.getAttribute('data-phase-id'));
  assert.equal(await page.evaluate(() => window.checkedTerminalScreen === document.querySelector('#terminal .xterm-screen')), true, 'phase navigation retains terminal');
  assert.ok((await page.locator('#phase-context').textContent()).trim(), 'recorded context is visible');
  await page.locator('#codex-tab-terminal').click();
  if (await page.locator('#phase-terminal-jump').isEnabled()) await page.locator('#phase-terminal-jump').click();
  const terminalStatus = await page.locator('#phase-terminal-status').textContent();
  assert.match(terminalStatus, /定位|匹配|未|没有|无法|不可|找到|原文/, 'terminal match or its limitation must be explicit');
  await page.locator('#codex-tab-records').click();
  await page.locator('#phase-nav button.has-video').first().click();
  const cameraEvidence = [];
  for (const camera of ['cam_head', 'cam_left_wrist', 'cam_right_wrist']) {
    await page.locator('#phase-camera').selectOption(camera);
    await page.waitForFunction(() => {
      const video = document.getElementById('phase-video');
      return !video.hidden && video.readyState >= 2 && !video.seeking && Math.abs(video.currentTime - Number(video.dataset.startPts)) < 0.03;
    });
    cameraEvidence.push({ camera, ...await page.locator('#phase-video').evaluate((video) => ({ width: video.videoWidth, height: video.videoHeight,
      current_time: video.currentTime, start_pts: Number(video.dataset.startPts), end_pts: Number(video.dataset.endPts), source: video.currentSrc })) });
  }
  assert.equal(new Set(cameraEvidence.map((entry) => entry.source)).size, 3, 'three camera selections decode distinct saved videos');
  await page.locator('#phase-camera').selectOption('cam_head');
  await page.locator('#phase-nav button.has-video').nth(1).click();
  await page.waitForFunction(() => {
    const video = document.getElementById('phase-video');
    return video.readyState >= 2 && !video.seeking && Number(video.dataset.startPts) > 0 && Math.abs(video.currentTime - Number(video.dataset.startPts)) < 0.03;
  });
  const laterSeek = await page.locator('#phase-video').evaluate((video) => ({ phase_id: video.dataset.phaseId, action_id: video.dataset.actionId,
    current_time: video.currentTime, start_pts: Number(video.dataset.startPts), end_pts: Number(video.dataset.endPts) }));
  await page.locator('#phase-nav button.has-video').first().click();
  await page.waitForFunction(() => { const video = document.getElementById('phase-video'); return video.readyState >= 2 && !video.seeking; });
  await screenshot(page, 'recorded-phases');
  const phaseCount = await page.locator('#phase-nav button[data-phase-id]').count();
  const cameras = await page.locator('#phase-camera option').evaluateAll((options) => options.map((option) => ({ value: option.value, text: option.textContent })));
  assert.ok(cameras.some((camera) => camera.value === 'cam_head'));
  evidence.push({ data_origin: 'existing_recorded_run', catalog_id: record.id, run_id: run.run_id, thread_id: run.thread_id,
    terminal_session_id: run.terminal?.session_id, phase_count: phaseCount, playable_phase_count: await page.locator('#phase-nav button.has-video').count(),
    cameras, camera_evidence: cameraEvidence, later_phase_seek: laterSeek, terminal_status: terminalStatus });
  const after = await read(`/runs/${record.id}/v1/run`);
  assert.equal(after.thread_id, run.thread_id);
  assert.equal(after.terminal?.session_id, run.terminal?.session_id);
  checks.push('existing record: three saved camera videos decode and seek to recorded PTS, synchronized navigation/activity, phase context, preserved terminal, explicit terminal location status, read-only controls, 320/1440px');
  await page.close();
  }

  if (!realOnly) {
  const videoFile = process.env.AURIGA_FIXTURE_VIDEO || path.join(output, 'synthetic-phase.mp4');
  if (!process.env.AURIGA_FIXTURE_VIDEO) {
    const result = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=10:duration=6',
      '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', videoFile], { encoding: 'utf8' });
    assert.equal(result.status, 0, `generate CPU-only synthetic test clip: ${result.stderr || result.error || ''}`);
  }
  const clip = await fs.readFile(videoFile);
  const identity = { run_id: 'synthetic-phase-run', episode_id: 'synthetic-episode', env_instance_id: 'synthetic-env', env_generation: 1 };
  const thread = { thread_id: 'synthetic-thread', turn_id: 'synthetic-turn' };
  const items = [
    { ...thread, id: 'prompt', type: 'userMessage', content: [{ type: 'text', text: '合成任务指令：移动并核对方块。' }] },
    { ...thread, id: 'before', type: 'agentMessage', text: '合成前文：先移动方块。' },
    { ...thread, id: 'call-1', type: 'dynamicToolCall', tool: 'robot_move_ee', arguments: { synthetic_fixture: true, label: 'first movement' }, status: 'completed' },
    { ...thread, id: 'between', type: 'agentMessage', text: '合成后文：移动完毕，检查画面。' },
    { ...thread, id: 'call-2', type: 'dynamicToolCall', tool: 'robot_observe', arguments: { synthetic_fixture: true }, status: 'completed' },
    { ...thread, id: 'call-3', type: 'dynamicToolCall', tool: 'robot_move_grasp', arguments: { synthetic_fixture: true }, status: 'completed' },
    { ...thread, id: 'call-4', type: 'dynamicToolCall', tool: 'robot_reset', arguments: { synthetic_fixture: true }, status: 'completed' },
    { ...thread, id: 'call-5', type: 'dynamicToolCall', tool: 'robot_gripper', arguments: { synthetic_fixture: true }, status: 'completed' },
    { ...thread, id: 'closing-1', type: 'agentMessage', text: '合成收尾第一条：操作结束。' },
    { ...thread, id: 'closing-2', type: 'agentMessage', text: '合成收尾第二条：保留未确认的结果。' },
  ];
  const action = (id, start, end, generation = 1) => ({ request: { ...identity, ...thread, env_generation: generation, action_id: id },
    receipt: { status: 'completed', result_observation_id: `obs-${id}`, execution_interval: { start_sim_step: start, end_sim_step: end, step_domain: 'synthetic_control' } } });
  const actions = { 'action-1': action('action-1', 0, 10), 'action-3': action('action-3', 20, 30),
    'action-4': action('action-4', 0, 10, 2), 'action-5': action('action-5', 30, 40) };
  const observations = Object.values(actions).map((value) => ({ ...value.request, observation_id: value.receipt.result_observation_id,
    sim_step: value.receipt.execution_interval.end_sim_step, step_domain: 'synthetic_control', clock_domain: 'synthetic-clock', cameras: [], data_origin: 'synthetic_fixture' }));
  const events = [];
  const event = (type, correlation = {}, payload = {}) => ({ ...identity, event_id: `synthetic-event-${events.length + 1}`, server_seq: events.length + 1,
    type, correlation: { ...thread, ...correlation }, payload, step_domain: 'synthetic_control', clock_domain: 'synthetic-clock', world_kind: 'observed', data_origin: 'synthetic_fixture' });
  for (const item of items) {
    events.push(event('codex.item/completed', { item_id: item.id }, { item }));
    const id = item.id.replace('call-', 'action-');
    if (id === 'action-3') events.push(event('action.requested', { item_id: item.id, action_id: id }, { request: actions[id].request }));
    if (actions[id]) events.push(event('action.completed', { item_id: item.id, action_id: id }, actions[id].receipt));
  }
  events.push(event('observer.video_exported', {}, { ...identity, data_origin: 'synthetic_fixture', videos: ['cam_head', 'cam_left_wrist', 'cam_right_wrist'].map((camera_id) => ({
    camera_id, codec: 'h264', video_ref: `artifact:synthetic-${camera_id}`, frames: 60, fps: 10, step_domain: 'synthetic_control',
    frame_mapping: [0, 10, 20, 30].map((sim_step, index) => ({ sim_step, pts_s: index + 0.2, video_frame: index * 10 + 2 })) })) }));
  const synthetic = await context.newPage(); lastPage = synthetic; watch(synthetic);
  let emptyFixture = false;
  let firstActionAttempts = 0;
  const actionRequests = [];
  await synthetic.route('**/runs/synthetic-phase-fixture/v1/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname.split('/v1/')[1];
    if (!['GET', 'HEAD'].includes(route.request().method())) { writes.push({ method: route.request().method(), url: route.request().url() }); return route.abort(); }
    if (pathname.startsWith('artifacts/synthetic-')) {
      const range = /^bytes=(\d+)-(\d*)$/.exec(route.request().headers().range || '');
      if (range) {
        const start = Number(range[1]), end = Math.min(Number(range[2] || clip.length - 1), clip.length - 1);
        return route.fulfill({ status: 206, contentType: 'video/mp4', headers: { 'Accept-Ranges': 'bytes', 'Content-Range': `bytes ${start}-${end}/${clip.length}` }, body: clip.subarray(start, end + 1) });
      }
      return route.fulfill({ contentType: 'video/mp4', headers: { 'Accept-Ranges': 'bytes' }, body: clip });
    }
    if (pathname === 'events') return route.fulfill({ contentType: 'text/event-stream', body: emptyFixture ? ': no recorded events\n\n' : events.map((entry) => `id: ${entry.server_seq}\ndata: ${JSON.stringify(entry)}\n\n`).join('') });
    let body;
    if (pathname === 'run') body = { ...identity, ...thread, status: 'replay', codex_status: 'replay', read_only: true, writable: false,
      terminal_available: !emptyFixture, terminal: { session_id: 'synthetic-terminal', pid: null }, library: { live: false, terminal_available: !emptyFixture } };
    else if (pathname === 'items') body = { items: emptyFixture ? [] : items, cursor: emptyFixture ? 0 : events.length };
    else if (pathname.startsWith('items/')) body = items.find((item) => item.id === pathname.slice(6));
    else if (pathname === 'requests') body = { requests: [] };
    else if (pathname === 'observations') body = { observations: emptyFixture ? [] : observations };
    else if (pathname === 'observations/latest') body = emptyFixture ? null : observations[0];
    else if (pathname.startsWith('observations/')) body = observations.find((observation) => observation.observation_id === pathname.slice(13));
    else if (pathname.startsWith('actions/')) {
      actionRequests.push(pathname.slice(8));
      if (pathname === 'actions/action-1' && ++firstActionAttempts === 1) return route.fulfill({ status: 503, json: { error: 'synthetic_temporary_failure' } });
      body = actions[pathname.slice(8)];
    }
    else if (pathname === 'scene/head') body = { scene_version: null };
    else if (pathname === 'scene/versions') body = { versions: [] };
    return route.fulfill({ status: body ? 200 : 404, json: body || { error: 'synthetic_record_unavailable' } });
  });
  await synthetic.routeWebSocket('**/runs/synthetic-phase-fixture/v1/terminal**', (socket) => {
    const text = 'Synthetic terminal only\r\nrobot_move_ee first movement\r\nrobot_observe\r\nrobot_move_grasp\r\n';
    const bytes = Buffer.from(text);
    socket.send(JSON.stringify({ type: 'ready', status: { session_id: 'synthetic-terminal', cursor: bytes.length, exit_code: 0 } }));
    socket.send(JSON.stringify({ type: 'output', after: 0, cursor: bytes.length, data: bytes.toString('base64') }));
  });
  if (!preview) await synthetic.route('**/runs/synthetic-phase-fixture/', async (route) => route.fulfill({ body: await (await fetch(runURL)).text(), contentType: 'text/html' }));
  await synthetic.goto(new URL('/runs/synthetic-phase-fixture/', origin).href);
  await synthetic.waitForSelector('#phase-nav button[data-phase-id]');
  const choose = async (itemId) => {
    const button = synthetic.locator('#phase-nav button[data-phase-id]').filter({ hasText: items.find((item) => item.id === itemId).tool });
    if (await button.count() === 1) await button.click();
    else {
      const id = await synthetic.locator('#phase-nav button[data-phase-id]').evaluateAll((buttons, itemId) => buttons.find((button) => button.dataset.phaseId.includes(itemId))?.dataset.phaseId, itemId);
      assert.ok(id, `phase for ${itemId}`);
      await synthetic.locator(`#phase-nav button[data-phase-id=${JSON.stringify(id)}]`).click();
    }
    return assertSelection(synthetic);
  };
  await choose('call-1');
  await synthetic.waitForFunction(() => { const video = document.getElementById('phase-video'); return !video.hidden && video.readyState >= 2 && !video.seeking; });
  const firstPhase = await assertSelection(synthetic);
  const contextText = await synthetic.locator('#phase-context').textContent();
  assert.ok(contextText.includes('合成任务指令') && contextText.includes('合成前文') && contextText.includes('合成后文') && contextText.includes('robot_move_ee'), 'prompt, before, tool and after context are retained');
  assert.ok(Math.abs(await synthetic.locator('#phase-video').evaluate((video) => video.currentTime) - 0.2) < 0.03, 'seek uses recorded start PTS');
  await synthetic.locator('#phase-camera').selectOption('cam_left_wrist');
  await synthetic.waitForFunction(() => document.getElementById('phase-video').currentSrc.includes('cam_left_wrist'));
  assert.equal(await assertSelection(synthetic), firstPhase, 'camera switch retains phase');
  await synthetic.locator('#phase-camera').selectOption('cam_head');
  await synthetic.locator('#codex-tab-terminal').click();
  if (await synthetic.locator('#phase-terminal-jump').isEnabled()) await synthetic.locator('#phase-terminal-jump').click();
  assert.match(await synthetic.locator('#phase-terminal-status').textContent(), /定位|匹配|原文/);
  await synthetic.locator('#codex-tab-records').click();
  await screenshot(synthetic, 'synthetic-valid-phase');
  await choose('call-2');
  assert.equal(await synthetic.locator('#phase-play').isDisabled(), true);
  assert.equal(await synthetic.locator('#phase-video').isVisible(), false, 'missing video clears previous player');
  assert.match(await synthetic.locator('#phase-empty').textContent(), /未|无|没有|缺/);
  await screenshot(synthetic, 'synthetic-missing-video');
  const thirdPhase = await choose('call-3');
  await synthetic.waitForFunction(() => document.getElementById('phase-video').readyState >= 2 && !document.getElementById('phase-video').seeking);
  assert.ok(Math.abs(await synthetic.locator('#phase-video').evaluate((video) => video.currentTime) - 2.2) < 0.03);
  for (const id of ['call-4', 'call-5']) {
    await choose(id);
    assert.equal(await synthetic.locator('#phase-play').isDisabled(), true, `${id} cannot play an unverified clip`);
    assert.equal(await synthetic.locator('#phase-video').isVisible(), false);
    const reason = (await synthetic.locator('#phase-position').textContent()) + (await synthetic.locator('#phase-empty').textContent());
    assert.match(reason, id === 'call-4' ? /不同|匹配|身份|generation|代际/ : /两端|边界|映射|区间/);
    evidence.push({ data_origin: 'synthetic_browser_route', case: id === 'call-4' ? 'generation_mismatch' : 'missing_boundary', reason });
  }
  await choose('call-1');
  await synthetic.locator('#phase-continuous').uncheck();
  await synthetic.locator('#phase-play').click();
  await synthetic.waitForFunction(() => { const video = document.getElementById('phase-video'); return video.paused && video.currentTime >= 1.15; });
  assert.equal(await assertSelection(synthetic), firstPhase, 'single-phase playback stops at clip boundary');
  assert.ok(await synthetic.locator('#phase-video').evaluate((video) => video.currentTime <= 1.3), 'clip does not continue into following stages');
  await synthetic.locator('#phase-continuous').check();
  await synthetic.locator('#phase-play').click();
  await synthetic.waitForFunction((id) => document.querySelector('#phase-nav button[aria-current="step"]')?.dataset.phaseId === id, thirdPhase);
  await assertSelection(synthetic);
  await synthetic.locator('#phase-video').evaluate((video) => video.pause());
  checks.push('synthetic route only: native MP4 decode and exact PTS, camera switch, prompt/before/tool/after, missing-video clearing, generation and boundary rejection, clip end stop, continuous playback skips unmapped phase');
  await choose('call-1');
  await synthetic.locator('#phase-video').evaluate(async (video) => { await video.play(); video.currentTime = 2.6; });
  await synthetic.waitForFunction((id) => document.querySelector('#phase-nav button[aria-current="step"]')?.dataset.phaseId === id, thirdPhase);
  await synthetic.waitForTimeout(200);
  assert.equal(await assertSelection(synthetic), thirdPhase, 'forward scrub while playing keeps requested phase');
  await synthetic.locator('#phase-video').evaluate((video) => { video.currentTime = 0.5; });
  await synthetic.waitForFunction((id) => document.querySelector('#phase-nav button[aria-current="step"]')?.dataset.phaseId === id, firstPhase);
  await synthetic.waitForTimeout(200);
  assert.equal(await assertSelection(synthetic), firstPhase, 'backward scrub while playing keeps requested phase');
  await synthetic.locator('#phase-video').evaluate((video) => video.pause());
  await synthetic.locator('#phase-nav button[data-phase-id="codex:closing-1"]').click();
  const closingContext = await synthetic.locator('#phase-context').textContent();
  assert.ok(closingContext.includes('合成收尾第一条') && closingContext.includes('合成收尾第二条'), 'all closing Codex messages remain visible');
  assert.ok(firstActionAttempts >= 2, 'a temporary action-read failure recovers');
  assert.equal(actionRequests.includes('action-3'), false, 'complete request/receipt events plus observation index avoid redundant action GET');
  evidence.push({ data_origin: 'synthetic_browser_route', case: 'temporary_action_read_failure', first_action_attempts: firstActionAttempts });
  checks.push('playing scrub in both directions retains the target phase; multiple closing messages remain visible; first action GET 503 recovers; complete event evidence avoids redundant action GET');
  emptyFixture = true;
  await synthetic.reload();
  await synthetic.waitForFunction(() => document.getElementById('run-mode').textContent.includes('只读'));
  await synthetic.waitForFunction(() => /未|无|没有|缺/.test(document.getElementById('phase-empty').textContent));
  assert.equal(await synthetic.locator('#phase-nav button[data-phase-id]').count(), 0);
  assert.equal(await synthetic.locator('#phase-play').isDisabled(), true);
  assert.equal(await synthetic.locator('#phase-video').isVisible(), false);
  assert.match(await synthetic.locator('#phase-empty').textContent(), /未|无|没有|缺/);
  await screenshot(synthetic, 'synthetic-empty-record');
  checks.push('empty recorded run has no fabricated phases or playable media and gives an explicit empty state');
  await synthetic.close();
  const continuityOutput = path.join(output, 'continuity');
  const continuity = spawnSync(process.execPath, [path.join(source, 'check-phase-continuity.mjs')], {
    env: { ...process.env, AURIGA_SCREENSHOT_DIR: continuityOutput }, encoding: 'utf8',
  });
  assert.equal(continuity.status, 0, `isolated playback continuity: ${continuity.stderr || continuity.stdout || continuity.error || ''}`);
  const continuityReport = JSON.parse(await fs.readFile(path.join(continuityOutput, 'continuity-report.json'), 'utf8'));
  evidence.push({ data_origin: continuityReport.data_origin, case: 'playback_continuity', report: path.join(continuityOutput, 'continuity-report.json'),
    scenarios: continuityReport.evidence.map((entry) => ({ case: entry.case, seeks: entry.seeks, pauses: entry.pauses, plays: entry.plays })) });
  checks.push('isolated generated video: contiguous clips advance context without pause, seek or time reversal; camera/source retained; recorded gaps and separate export clocks seek once with explicit notices');
  }
  assert.deepEqual(writes, [], 'no non-GET/HEAD HTTP requests');
  assert.deepEqual(socketWrites, [], 'no terminal input; resize is the only outgoing terminal message');
  assert.deepEqual(errors, []);
  const report = { status: 'passed', observed_at: new Date().toISOString(), origin, preview_assets: preview || null,
    browser: browser.version(), synthetic_only: syntheticOnly, real_only: realOnly, widths: [320, 1440], checks, evidence, writes, socket_writes: socketWrites, errors,
    synthetic_limits: realOnly ? null : 'Browser-only records and CPU-generated clip; no model, robot, server mutation, or claim of physical execution.' };
  await fs.writeFile(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  const pageState = lastPage && !lastPage.isClosed() ? await lastPage.evaluate(() => ({
    phase_count: document.getElementById('phase-count')?.textContent, events_status: document.getElementById('events-status')?.textContent,
    position: document.getElementById('phase-position')?.textContent, empty: document.getElementById('phase-empty')?.textContent,
    video: document.getElementById('phase-video') ? { ready_state: document.getElementById('phase-video').readyState,
      current_time: document.getElementById('phase-video').currentTime, source: document.getElementById('phase-video').currentSrc,
      error: document.getElementById('phase-video').error?.message } : null,
  })).catch(() => null) : null;
  if (lastPage && !lastPage.isClosed()) await lastPage.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
  const report = { status: 'failed', observed_at: new Date().toISOString(), error: error.message, checks, evidence, errors, writes, socket_writes: socketWrites, page_state: pageState };
  await fs.writeFile(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2));
  console.error(JSON.stringify(report, null, 2));
  throw error;
} finally { await browser.close(); }
