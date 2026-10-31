import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const engines = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const url = process.env.AURIGA_URL || 'http://127.0.0.1:8780';
const output = process.env.AURIGA_SCREENSHOT_DIR || 'runs/web-library-check';
const preview = process.env.AURIGA_PREVIEW_ASSETS;
const syntheticOnly = process.env.AURIGA_SYNTHETIC_ONLY === '1';
assert.ok(!syntheticOnly || preview, 'synthetic-only checks require preview assets');
await fs.mkdir(output, { recursive: true });
let browser;
try {
  browser = await engines[process.env.AURIGA_BROWSER || 'chromium'].launch({ headless: true, ...(process.env.AURIGA_BROWSER_EXECUTABLE ? { executablePath: process.env.AURIGA_BROWSER_EXECUTABLE } : {}) });
} catch (error) {
  await fs.writeFile(path.join(output, 'browser-report.json'), JSON.stringify({ status: 'blocked', stage: 'browser_launch', observed_at: new Date().toISOString(),
    url, preview_assets: preview, synthetic_only: syntheticOnly, error: error.message, checks: [], writes: [], limitations: ['The browser could not start; no page or live service checks ran.'] }, null, 2));
  throw error;
}
const errors = [], writes = [], checks = [], limitations = [], runEvidence = [], layoutEvidence = [], fixtureRequests = [];
const context = await browser.newContext();
const source = path.dirname(fileURLToPath(import.meta.url));
await context.route('**/*', async (route) => {
  const request = route.request(), pathname = new URL(request.url()).pathname;
  if (!['GET', 'HEAD'].includes(request.method())) { writes.push({ method: request.method(), url: request.url() }); return route.abort(); }
  if (preview) {
    const name = path.basename(pathname);
    let file;
    if (pathname === '/') file = path.join(source, 'library.html');
    else if (/^\/runs\/[^/]+\/$/.test(pathname)) file = path.join(source, 'index.html');
    else if (['app.js', 'app.css', 'library.js'].includes(name)) file = path.join(preview, name);
    else if (['styles.css', 'library.js', 'library.css', 'task-labels.js'].includes(name)) file = path.join(source, name);
    if (file) return route.fulfill({ path: file, contentType: file.endsWith('.html') ? 'text/html' : file.endsWith('.css') ? 'text/css' : 'application/javascript' });
  }
  if (syntheticOnly) return route.fulfill({ status: 404, body: 'synthetic fixture has no such resource' });
  return route.continue();
});
const page = await context.newPage(); page.setDefaultTimeout(30000);
let activePage = page;
const watch = (target) => {
  target.on('pageerror', (error) => errors.push(error.message));
};
watch(page); await fs.mkdir(output, { recursive: true });
const read = async (endpoint) => { const response = await fetch(new URL(endpoint, url)); assert.equal(response.status, 200, endpoint); return response.json(); };
const scrollState = (target) => target.evaluate(() => {
  const panel = (selector) => { const node = document.querySelector(selector); return { top: node.scrollTop, maximum: node.scrollHeight - node.clientHeight, height: node.clientHeight, overflow: getComputedStyle(node).overflowY }; };
  const filters = document.querySelector('#library-filters'), restore = document.querySelector('#library-show-filters');
  return { page: scrollY, pageMaximum: document.documentElement.scrollHeight - innerHeight,
    layoutTop: document.querySelector('.library-layout').getBoundingClientRect().top,
    filtersBottom: filters.getBoundingClientRect().bottom,
    restoreVisible: Boolean(restore && !restore.hidden && getComputedStyle(restore).display !== 'none'),
    left: panel('.experiment-list-panel'), right: panel('#experiment-detail') };
});
async function settleScroll(target) {
  // Resize, browser scroll anchoring, and smooth scroll can complete after the
  // wheel promise. Observe stable positions rather than assuming a fixed delay.
  await target.evaluate(() => new Promise((resolve, reject) => {
    let previous, stableSince = performance.now(); const started = stableSince;
    const tick = () => {
      const nodes = [document.querySelector('.library-layout'), document.querySelector('.experiment-list-panel'), document.querySelector('#experiment-detail')];
      const value = [scrollY, ...nodes.flatMap((node) => [node.scrollTop, node.scrollHeight, node.clientHeight, node.getBoundingClientRect().top])].join(':');
      const now = performance.now();
      if (value !== previous) { previous = value; stableSince = now; }
      if (now - stableSince >= 180) { resolve(); return; }
      if (now - started > 5000) { reject(new Error('page and column geometry did not settle')); return; }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(() => requestAnimationFrame(tick));
  }));
}
async function replayAboveVideo(target) {
  const position = await target.evaluate(() => {
    const links = document.querySelector('#detail-links'), media = document.querySelector('#detail-media'), scoring = document.querySelector('#detail-task-review');
    return { beforeMedia: Boolean(links.compareDocumentPosition(media) & Node.DOCUMENT_POSITION_FOLLOWING), beforeScoring: Boolean(links.compareDocumentPosition(scoring) & Node.DOCUMENT_POSITION_FOLLOWING), linksBottom: links.getBoundingClientRect().bottom, mediaTop: media.getBoundingClientRect().top };
  });
  assert.equal(position.beforeMedia, true, 'replay link precedes video in document order');
  assert.equal(position.beforeScoring, true, 'replay link precedes the scoring rules');
  assert.ok(position.linksBottom <= position.mediaTop, 'replay link is visually above the videos');
  return position;
}
async function wheelPanel(target, selector, delta) {
  const box = await target.locator(selector).boundingBox();
  // Aim at the panel edge so nested video controls or evidence pre blocks do not
  // consume a wheel intended for the column itself.
  await target.mouse.move(box.x + box.width - 24, Math.max(30, Math.min(box.y + 85, target.viewportSize().height - 35)));
  await target.mouse.wheel(0, delta);
  await settleScroll(target);
  return scrollState(target);
}
async function wheelToBoundary(target, selector, side, end) {
  let state = await scrollState(target);
  const other = side === 'left' ? 'right' : 'left', direction = end ? 1 : -1;
  // Firefox can clamp one wheel gesture to a viewport page even for a huge delta.
  for (let gesture = 0; gesture < 30; gesture++) {
    const remaining = end ? state[side].maximum - state[side].top : state[side].top;
    if (remaining <= 1) return state;
    const next = await wheelPanel(target, selector, direction * Math.max(120, remaining));
    assert.ok(direction * (next[side].top - state[side].top) > 0, `${side} wheel must progress toward its boundary`);
    assert.equal(next[other].top, state[other].top, 'boundary scrolling keeps the neighboring column still');
    assert.equal(next.page, state.page, 'boundary scrolling keeps the page still');
    state = next;
  }
  assert.fail(`${side} column did not reach its boundary within 30 wheel gestures`);
}
async function wheelVideo(target, delta) {
  const box = await target.locator('#detail-media video').first().boundingBox();
  const top = Math.max(0, box.y), bottom = Math.min(target.viewportSize().height, box.y + box.height);
  assert.ok(bottom - top > 10, 'a camera video is visible for a real mouse wheel gesture');
  await target.mouse.move(box.x + box.width / 2, (top + bottom) / 2);
  await target.mouse.wheel(0, delta); await settleScroll(target);
  return scrollState(target);
}
async function independentColumns(target, viewport) {
  await target.setViewportSize(viewport);
  await settleScroll(target);
  await target.evaluate(() => { document.querySelector('.experiment-list-panel').scrollTop = 0; document.querySelector('#experiment-detail').scrollTop = 0; window.scrollTo(0, 0); });
  await settleScroll(target);
  const before = await scrollState(target), replay = await replayAboveVideo(target);
  assert.ok(before.left.maximum > 400 && before.right.maximum > 400, 'both fixtures have enough content to exercise independent scrolling');
  assert.ok(before.pageMaximum > 200, 'desktop document can scroll the heading and filters out of view');
  assert.ok(before.left.height >= viewport.height - 80 && before.right.height >= viewport.height - 80, 'desktop columns use almost the full viewport instead of its remainder below the filters');
  assert.ok(before.filtersBottom > 0); assert.equal(before.restoreVisible, false);
  await target.screenshot({ path: path.join(output, `filters-visible-${viewport.width}x${viewport.height}.png`), fullPage: false });
  const leftFirst = await wheelPanel(target, '.experiment-list-panel', 120);
  assert.ok(leftFirst.page > 0 && leftFirst.page < before.layoutTop, 'wheel over the left column first scrolls the document');
  assert.equal(leftFirst.left.top, 0); assert.equal(leftFirst.right.top, 0);
  await target.evaluate(() => window.scrollTo(0, 0)); await settleScroll(target);
  const rightFirst = await wheelPanel(target, '#experiment-detail', 120);
  assert.ok(rightFirst.page > 0 && rightFirst.page < before.layoutTop, 'wheel over the right column first scrolls the document');
  assert.equal(rightFirst.left.top, 0); assert.equal(rightFirst.right.top, 0);
  const videoFirst = await wheelVideo(target, Math.min(80, rightFirst.layoutTop - 12));
  assert.ok(videoFirst.page > rightFirst.page, 'wheel over the camera video also hides the filters first');
  assert.equal(videoFirst.left.top, 0); assert.equal(videoFirst.right.top, 0);
  const docked = await wheelPanel(target, '#experiment-detail', videoFirst.layoutTop - 12);
  assert.ok(docked.layoutTop >= 8 && docked.layoutTop <= 16, 'columns settle near the top of the viewport');
  assert.ok(docked.filtersBottom <= 0, 'search and filters no longer consume visible space');
  assert.equal(docked.restoreVisible, true); assert.equal(docked.left.top, 0); assert.equal(docked.right.top, 0);
  const restorePosition = await target.locator('#library-show-filters').evaluate((node) => ({ barPosition: getComputedStyle(node.closest('.panel-bar')).position, top: node.getBoundingClientRect().top, bottom: node.getBoundingClientRect().bottom }));
  assert.equal(restorePosition.barPosition, 'sticky'); assert.ok(restorePosition.top >= 0 && restorePosition.bottom <= viewport.height);
  const left = await wheelPanel(target, '.experiment-list-panel', 330);
  assert.ok(left.left.top > 100, 'mouse wheel now scrolls the left column'); assert.equal(left.right.top, 0); assert.equal(left.page, docked.page);
  const right = await wheelPanel(target, '#experiment-detail', 260);
  assert.ok(right.right.top > 100, 'mouse wheel scrolls the right column'); assert.equal(right.left.top, left.left.top); assert.equal(right.page, docked.page);
  await target.screenshot({ path: path.join(output, `independent-columns-${viewport.width}x${viewport.height}.png`), fullPage: false });
  const leftEnd = await wheelToBoundary(target, '.experiment-list-panel', 'left', true);
  assert.ok(Math.abs(leftEnd.left.top - leftEnd.left.maximum) <= 1); assert.equal(leftEnd.right.top, right.right.top); assert.equal(leftEnd.page, docked.page);
  const leftPastEnd = await wheelPanel(target, '.experiment-list-panel', 1000);
  assert.deepEqual(leftPastEnd, leftEnd, 'left bottom wheel does not chain to the other column or page');
  const rightEnd = await wheelToBoundary(target, '#experiment-detail', 'right', true);
  assert.ok(Math.abs(rightEnd.right.top - rightEnd.right.maximum) <= 1); assert.equal(rightEnd.left.top, leftEnd.left.top); assert.equal(rightEnd.page, docked.page);
  const rightPastEnd = await wheelPanel(target, '#experiment-detail', 1000);
  assert.deepEqual(rightPastEnd, rightEnd, 'right bottom wheel does not chain to the other column or page');
  assert.ok(await target.locator('#library-show-filters').evaluate((node) => node.getBoundingClientRect().top >= 0 && node.getBoundingClientRect().bottom <= innerHeight), 'sticky column header keeps the filter shortcut accessible at the bottom of both columns');
  await target.locator('#library-show-filters').click(); await settleScroll(target);
  const restored = await scrollState(target);
  assert.equal(restored.page, 0, 'return to filters restores the top of the document');
  assert.equal(restored.left.top, rightEnd.left.top); assert.equal(restored.right.top, rightEnd.right.top);
  assert.ok(restored.filtersBottom > 0); assert.equal(restored.restoreVisible, false);
  assert.equal(await target.locator('#q').inputValue(), '正在编辑但未应用的筛选', 'returning to filters keeps the unsubmitted search draft');
  assert.equal(await target.locator('#q').evaluate((node) => node === document.activeElement), true, 'filter shortcut focuses search without resetting column positions');
  const redocked = await wheelPanel(target, '.experiment-list-panel', restored.layoutTop - 12);
  assert.equal(redocked.page, docked.page); assert.equal(redocked.left.top, restored.left.top); assert.equal(redocked.right.top, restored.right.top);
  const leftTop = await wheelToBoundary(target, '.experiment-list-panel', 'left', false);
  assert.equal(leftTop.left.top, 0); assert.equal(leftTop.right.top, redocked.right.top); assert.equal(leftTop.page, docked.page);
  const upward = await wheelPanel(target, '.experiment-list-panel', -120);
  assert.ok(upward.page < leftTop.page && upward.filtersBottom > 0, 'upward wheel at a column top reveals the filters');
  assert.equal(upward.left.top, 0); assert.equal(upward.right.top, leftTop.right.top);
  assert.equal(await target.evaluate(() => window.checkedFixtureVideo === document.querySelector('#detail-media video') && window.checkedWristVideo === document.querySelector('#detail-media [data-camera-role="wrist"] video')), true, 'scroll and filter recovery preserve both video nodes');
  layoutEvidence.push({ viewport, replay, before, leftFirst, rightFirst, videoFirst, docked, restorePosition, left, right, leftEnd, leftPastEnd, rightEnd, rightPastEnd, restored, redocked, leftTop, upward });
}
async function naturalMobileScroll(target, viewport) {
  await target.setViewportSize(viewport);
  await settleScroll(target);
  await target.evaluate(() => window.scrollTo(0, 0));
  await settleScroll(target);
  const before = await scrollState(target), replay = await replayAboveVideo(target);
  assert.equal(before.left.maximum, 0); assert.equal(before.right.maximum, 0);
  assert.equal(before.restoreVisible, false, 'desktop filter shortcut is absent on mobile');
  assert.ok(before.pageMaximum > 400, 'single-column mobile layout can scroll naturally');
  const left = await wheelPanel(target, '.experiment-list-panel', 400);
  assert.ok(left.page > before.page, 'wheel over the list scrolls the mobile page'); assert.equal(left.left.top, 0); assert.equal(left.right.top, 0);
  await target.locator('#experiment-detail').evaluate((node) => node.scrollIntoView({ block: 'start' }));
  await settleScroll(target);
  const beforeRight = await scrollState(target), right = await wheelPanel(target, '#experiment-detail', 400);
  assert.ok(right.page > beforeRight.page, 'wheel over details also scrolls the mobile page'); assert.equal(right.left.top, 0); assert.equal(right.right.top, 0);
  assert.equal(right.restoreVisible, false);
  await target.screenshot({ path: path.join(output, `natural-mobile-scroll-${viewport.width}.png`), fullPage: true });
  layoutEvidence.push({ viewport, replay, before, left, beforeRight, right });
}
try {
  let catalog = { total: null };
  if (!syntheticOnly) {
  catalog = await read('/v1/experiments?limit=100&sort=active'); assert.ok(catalog.total > 0);
  await page.goto(new URL('?view=runs', url).href); await page.waitForSelector('.experiment-row'); const first = catalog.experiments[0];
  await page.locator(`.experiment-row[data-experiment-id="${first.id}"] .experiment-title`).click(); await page.waitForSelector('#detail-content:not([hidden])');
  for (const width of [320, 736, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 }); assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, `library ${width}px`);
    await page.screenshot({ path: path.join(output, `library-${width}.png`), fullPage: true });
  }
  const nonNative = catalog.experiments.find((record) => !record.replay_available && !record.live);
  if (nonNative) {
    await page.locator('#q').fill(nonNative.run_id || nonNative.id); await page.locator('#library-filters button[type="submit"]').click();
    await page.waitForSelector(`.experiment-row[data-experiment-id="${nonNative.id}"]`);
    await page.locator(`.experiment-row[data-experiment-id="${nonNative.id}"] .experiment-title`).click();
    await page.waitForFunction(() => document.getElementById('detail-capabilities').textContent.includes('没有可回放'));
    assert.equal(await page.locator('#detail-links a[href*="/runs/"]').count(), 0); assert.equal(await page.locator('#terminal').count(), 0);
    checks.push('report-only archive has no fabricated terminal');
  } else limitations.push('No report-only archive in catalog.');
  const recorded = catalog.experiments.filter((record) => record.replay_available && !record.live && record.terminal_available).slice(0, 2); assert.ok(recorded.length);
  for (const record of recorded) {
    const target = await context.newPage(); watch(target); const requests = [], sockets = [];
    target.on('request', (request) => { if (new URL(request.url()).pathname.includes('/v1/')) requests.push(new URL(request.url()).pathname + new URL(request.url()).search); });
    target.on('websocket', (socket) => sockets.push(socket.url())); const prefix = `/runs/${record.id}`;
    await target.goto(new URL(record.view_url, url).href);
    await target.waitForFunction(() => document.getElementById('run-mode').textContent.includes('只读回放')); await target.waitForSelector('#terminal .xterm-screen');
    await target.waitForFunction(() => document.getElementById('input-status').textContent.includes('只读回放'));
    assert.equal(await target.locator('#claim-input').isVisible(), false); assert.equal(await target.locator('#task-prompt').isDisabled(), true); assert.equal(await target.locator('#stop').isDisabled(), true);
    assert.equal(await target.locator('.scene-panel form button:not([disabled])').count(), 0);
    assert.doesNotMatch(await target.locator('#terminal-status').textContent(), /实时/); assert.doesNotMatch(await target.locator('#events-status').textContent(), /实时/);
    assert.equal(await target.locator('#library-link').isVisible(), true); await target.waitForTimeout(500);
    assert.ok(requests.every((endpoint) => endpoint.startsWith(`${prefix}/v1/`)));
    assert.ok(requests.some((endpoint) => endpoint === `${prefix}/v1/events?after=0`));
    assert.ok(sockets.some((endpoint) => new URL(endpoint).pathname === `${prefix}/v1/terminal` && !new URL(endpoint).searchParams.has('session_id')));
    const snapshot = await read(`${prefix}/v1/run`); assert.equal(snapshot.library.id, record.id);
    runEvidence.push({ id: record.id, run_id: snapshot.run_id, thread_id: snapshot.thread_id, terminal_session_id: snapshot.terminal?.session_id, requests, sockets });
    if (runEvidence.length === 1) for (const width of [320, 736, 1024, 1440]) {
      await target.setViewportSize({ width, height: 1000 }); assert.equal(await target.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, `workbench ${width}px`);
      await target.screenshot({ path: path.join(output, `replay-${width}.png`), fullPage: true });
    }
    await target.close();
  }
  checks.push('run-scoped HTTP/SSE/WebSocket, fresh cursors, disabled replay controls, four viewports');
  }
  const fixture = await context.newPage(); watch(fixture); let revision = 0;
  activePage = fixture;
  await fixture.addInitScript(() => {
    window.fixtureHidden = false;
    Object.defineProperty(document, 'hidden', { get: () => window.fixtureHidden });
    window.EventSource = class extends EventTarget {
      static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
      readyState = 0;
      constructor() { super(); window.fixtureEvents = this; queueMicrotask(() => this.setState(1)); }
      emit(type, data) { this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify(data) })); }
      setState(state) {
        this.readyState = state;
        const type = state === 1 ? 'open' : 'error', event = new Event(type);
        this.dispatchEvent(event); this[`on${type}`]?.(event);
      }
      close() { this.readyState = 2; }
    };
  });
  fixture.on('request', (request) => {
    const endpoint = new URL(request.url());
    if (endpoint.pathname.startsWith('/v1/')) fixtureRequests.push(endpoint.pathname + endpoint.search);
  });
  const fixtureRecord = (index) => ({ id: `fixture-${index}`, run_id: `synthetic-${index}`, title: `合成实验 ${index}`, task: 'stack_bowls', data_origin: 'synthetic_fixture', category: index === 0 ? 'main' : index === 1 ? 'acceptance' : 'imported', kind: 'native', tags: ['synthetic_fixture'], created_at: '2026-09-30T12:00:00Z', updated_at: '2026-09-30T12:00:00Z', status: index === 0 && !revision ? 'running' : 'completed', live: index === 0 && !revision, replay_available: true, terminal_available: true, read_only: index !== 0 || Boolean(revision), writable: index === 0 && !revision, view_url: `/runs/fixture-${index}/`, summary: index === 0 ? { score: 0, task_success: false } : { score: null, task_success: null } });
  const unscoredSummary = { scored_episode: false, primary_outcome: 'unscored_harness_timeout', termination_reason: 'official_no_action_progress_timeout', score: null, normalized_score: null, task_success: null, native_process_score: 40, partial_native_result: { native_process_score: 40, normalized_score: 0.4, task_success: false } };
  const recordWithMetrics = (index) => index === 2 ? { ...fixtureRecord(index), status: 'stopped', summary: unscoredSummary } : fixtureRecord(index);
  await fixture.route('**/v1/experiments**', async (route) => {
    if (!['GET', 'HEAD'].includes(route.request().method())) { writes.push({ method: route.request().method(), url: route.request().url() }); return route.abort(); }
    const request = new URL(route.request().url());
    if (request.pathname.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: 'event: snapshot\ndata: {"revision":1}\n\n' });
    if (request.pathname !== '/v1/experiments') {
      const index = Number(request.pathname.split('/').at(-1).split('-').at(-1));
      const media = ['cam_head', 'cam_left_wrist'].map((camera_id) => ({ name: `${camera_id}.mp4`, url: `/synthetic-${camera_id}.mp4`, camera_id, mime_type: 'video/mp4' }));
      if (revision) media.push({ name: 'cam_right_wrist.mp4', url: '/synthetic-cam_right_wrist.mp4', camera_id: 'cam_right_wrist', mime_type: 'video/mp4' });
      media.push({ name: 'unrelated-preview.mp4', url: '/synthetic-unrelated.mp4', mime_type: 'video/mp4' }, { name: 'arbitrary-image.png', url: '/synthetic-image.png', mime_type: 'image/png' });
      const taskReview = { task_id: 'stack_bowls', description: { text: 'Synthetic task description for a tall scoring panel.' }, scoring: { criteria: Array.from({ length: 8 }, (_, point) => ({ id: `synthetic-point-${point}`, label: `合成评分项 ${point + 1}`, description: '用于浏览器滚动验证的合成规则；不是实际实验评分。', score: 10 })) }, result: { status: 'scored', normalized_score: 0, task_success: false } };
      if (index === 2) taskReview.result = { ...unscoredSummary, status: 'unscored' };
      return route.fulfill({ json: { ...recordWithMetrics(index), task_review: taskReview, reports: [{ name: 'synthetic-report.json', content: { data_origin: 'synthetic_fixture', revision } }], media, metadata: { data_origin: 'synthetic_fixture' } } });
    }
    let records = Array.from({ length: revision ? 26 : 25 }, (_, index) => recordWithMetrics(index)); const query = request.searchParams.get('q');
    if (query) records = records.filter((record) => JSON.stringify(record).includes(query));
    for (const name of ['category', 'status', 'task']) if (request.searchParams.get(name)) records = records.filter((record) => record[name] === request.searchParams.get(name));
    if (request.searchParams.get('tag')) records = records.filter((record) => record.tags.includes(request.searchParams.get('tag')));
    const start = Number(request.searchParams.get('offset') || 0), count = Number(request.searchParams.get('limit') || 20);
    return route.fulfill({ json: { experiments: records.slice(start, start + count), total: records.length, offset: start, limit: count, revision, facets: { categories: ['main', 'acceptance', 'imported'], statuses: ['running', 'completed'], tasks: ['stack_bowls'], tags: ['synthetic_fixture'] }, errors: [] } });
  });
  await fixture.goto(new URL('?view=runs', url).href); await fixture.waitForSelector('.experiment-row[data-experiment-id="fixture-0"]');
  assert.equal(fixtureRequests.some((endpoint) => /^\/v1\/experiments\/fixture-/.test(endpoint)), false,
    'the runs catalog does not fetch experiment details before a run is selected');
  assert.match(await fixture.locator('.experiment-row[data-experiment-id="fixture-0"] .experiment-metrics').textContent(), /任务得分：0 · 任务成功：否/);
  assert.match(await fixture.locator('.experiment-row[data-experiment-id="fixture-1"] .experiment-metrics').textContent(), /任务得分：未知 · 任务成功：未知/);
  const unscoredText = '本次未完成正式评测（动作进展超时） · 局部原生过程分：40 / 100 · 局部归一化分：0.4';
  assert.equal(await fixture.locator('.experiment-row[data-experiment-id="fixture-2"] .experiment-metrics').textContent(), unscoredText);
  await fixture.locator('.experiment-row[data-experiment-id="fixture-2"] .experiment-title').click();
  await fixture.waitForFunction((text) => document.getElementById('detail-metrics').textContent === text, unscoredText);
  checks.push('unscored timeout retains partial process score and uses identical list/detail labels');
  assert.equal(await fixture.locator('.experiment-row[data-experiment-id="fixture-0"] .task-label').textContent(), '堆叠碗');
  assert.equal(await fixture.locator('.experiment-row[data-experiment-id="fixture-0"] .environment-label').textContent(), '合成测试环境');
  const categoryLabels = await fixture.locator('#category option').allTextContents(); assert.ok(['主实验', '验收验证', '导入记录'].every((value) => categoryLabels.includes(value)));
  await fixture.locator('.experiment-row[data-experiment-id="fixture-0"] .experiment-title').click(); await fixture.waitForSelector('#detail-reports details'); await fixture.locator('#detail-reports summary').click();
  await fixture.waitForSelector('#detail-media .camera-card[data-camera-role="head"] video');
  assert.equal(await fixture.locator('#detail-media video').count(), 2);
  assert.equal(await fixture.locator('#detail-media img').count(), 0);
  assert.equal(await fixture.locator('#detail-media a[href*="unrelated"]').count(), 0);
  assert.equal(await fixture.locator('#detail-context .task-label').textContent(), '堆叠碗');
  assert.equal(await fixture.locator('select[aria-label="辅助摄像机"] option[value="right"]').isDisabled(), true);
  await fixture.evaluate(() => { window.checkedFixtureVideo = document.querySelector('#detail-media video'); window.checkedWristVideo = document.querySelector('#detail-media [data-camera-role="wrist"] video'); });
  await fixture.locator('#q').fill('正在编辑但未应用的筛选'); await fixture.locator('#q').focus();
  const healthyRequests = fixtureRequests.length;
  await fixture.waitForTimeout(5300);
  assert.equal(fixtureRequests.length, healthyRequests, 'healthy event stream avoids repeated list and selected-detail downloads');
  revision = 1;
  await fixture.evaluate((next) => window.fixtureEvents.emit('changed', { revision: next }), revision);
  await fixture.waitForFunction(() => document.getElementById('result-count').textContent.includes('26 个实验') && document.getElementById('detail-state').textContent.includes('已完成') && !document.querySelector('select[aria-label="辅助摄像机"] option[value="right"]').disabled);
  assert.equal(await fixture.locator('#q').inputValue(), '正在编辑但未应用的筛选'); assert.equal(await fixture.locator('#q').evaluate((node) => node === document.activeElement), true);
  assert.equal(await fixture.locator('#detail-reports details').evaluate((node) => node.open), true); assert.equal(await fixture.evaluate(() => window.checkedFixtureVideo === document.querySelector('#detail-media video')), true);
  assert.equal(await fixture.evaluate(() => window.checkedWristVideo === document.querySelector('#detail-media [data-camera-role="wrist"] video')), true);
  await fixture.locator('select[aria-label="辅助摄像机"]').selectOption('right');
  await fixture.evaluate(() => { window.checkedWristVideo = document.querySelector('#detail-media [data-camera-role="wrist"] video'); });
  assert.match(await fixture.locator('#detail-media [data-camera-role="wrist"] video').getAttribute('src'), /cam_right_wrist/);
  assert.match(await fixture.locator('#detail-media [data-camera-role="head"] video').getAttribute('src'), /cam_head/);
  revision = 2;
  await fixture.locator('#refresh-library').click();
  await fixture.waitForFunction(() => document.getElementById('detail-reports').textContent.includes('"revision": 2'));
  assert.equal(await fixture.locator('select[aria-label="辅助摄像机"]').inputValue(), 'right');
  assert.equal(await fixture.evaluate(() => window.checkedFixtureVideo === document.querySelector('#detail-media video') && window.checkedWristVideo === document.querySelector('#detail-media [data-camera-role="wrist"] video')), true);
  assert.equal(await fixture.locator('#detail-media video').count(), 2);
  assert.equal(await fixture.locator('.experiment-row.selected').getAttribute('data-experiment-id'), 'fixture-0');
  assert.equal(await fixture.locator('#detail-links a').first().textContent(), '打开只读回放');
  const duplicateRequests = fixtureRequests.length;
  await fixture.evaluate(() => window.fixtureEvents.emit('changed', { revision: 2 }));
  await fixture.waitForTimeout(400);
  assert.equal(fixtureRequests.length, duplicateRequests, 'duplicate event revisions do not redownload the catalog');
  revision = 3;
  await fixture.evaluate(() => {
    window.fixtureEvents.emit('changed', { revision: 3 });
    window.fixtureHidden = true;
    document.dispatchEvent(new Event('visibilitychange'));
    window.fixtureEvents.setState(0);
    window.fixtureEvents.emit('changed', { revision: 3 });
  });
  const hiddenRequests = fixtureRequests.length;
  await fixture.waitForTimeout(5300);
  assert.equal(fixtureRequests.length, hiddenRequests, 'hidden tabs ignore notifications and fallback polling');
  await fixture.evaluate(() => { window.fixtureHidden = false; document.dispatchEvent(new Event('visibilitychange')); });
  await fixture.waitForFunction(() => document.getElementById('detail-reports').textContent.includes('"revision": 3'));
  await fixture.waitForTimeout(400);
  const fallbackRequests = fixtureRequests.filter((endpoint) => endpoint.startsWith('/v1/experiments?')).length;
  await fixture.waitForTimeout(5300);
  assert.ok(fixtureRequests.filter((endpoint) => endpoint.startsWith('/v1/experiments?')).length > fallbackRequests,
    'visible tabs poll again while the event stream is disconnected');
  await fixture.evaluate(() => window.fixtureEvents.setState(1));
  await fixture.waitForTimeout(400);
  const reconnectedRequests = fixtureRequests.length;
  await fixture.waitForTimeout(5300);
  assert.equal(fixtureRequests.length, reconnectedRequests, 'reconnected event stream suppresses fallback polling again');
  checks.push('run details load only after selection; healthy and reconnected event streams suppress polling, changed revisions refresh, duplicate revisions and hidden tabs do not download, disconnection restores visible-tab polling');
  for (const viewport of [{ width: 1024, height: 800 }, { width: 1440, height: 1000 }, { width: 1440, height: 720 }]) await independentColumns(fixture, viewport);
  for (const viewport of [{ width: 320, height: 800 }, { width: 736, height: 900 }]) await naturalMobileScroll(fixture, viewport);
  checks.push('real mouse wheel: filters scroll away first, desktop columns then scroll independently across almost the full viewport, bottom overscroll stays local, upward wheel and sticky column-header shortcut restore filters without losing column positions; mobile scrolling stays natural; replay link remains above videos');
  await fixture.setViewportSize({ width: 1440, height: 1000 });
  await fixture.locator('#next-page').click(); await fixture.waitForFunction(() => document.getElementById('page-count').textContent.includes('第 2')); assert.equal(await fixture.locator('.experiment-row').count(), 6);
  await fixture.locator('#q').fill('no-such-fixture'); await fixture.locator('#library-filters button[type="submit"]').click(); await fixture.waitForSelector('#empty-library:not([hidden])');
  assert.equal(await fixture.locator('#detail-content').isVisible(), true);
  checks.push('browser-only synthetic fixture: zero/false, Chinese categories, auto-refresh and completion, preserved focus/draft/report/media, Chinese task/environment, head plus selectable wrist cameras, unrelated media hidden, pagination and empty filter');
  for (const width of [320, 1440]) {
    await fixture.setViewportSize({ width, height: 1000 });
    assert.equal(await fixture.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await fixture.screenshot({ path: path.join(output, `synthetic-library-${width}.png`), fullPage: true });
  }
  await fixture.close(); assert.deepEqual(writes, []); assert.deepEqual(errors, []);
  const report = { status: 'passed', observed_at: new Date().toISOString(), url, data_origin: syntheticOnly ? 'synthetic_browser_routes_only' : 'existing_catalog_and_recorded_native_runs', catalog_total: catalog.total, widths: [320,736,1024,1440], checks, limitations, run_evidence: runEvidence, layout_evidence: layoutEvidence, fixture_requests: fixtureRequests, synthetic_checks: 'browser_route_only_no_server_mutation_or_media_decode_claim', writes, errors };
  await fs.writeFile(path.join(output,'browser-report.json'), JSON.stringify(report,null,2)); console.log(JSON.stringify(report,null,2));
} catch(error) {
  if (!activePage.isClosed()) await activePage.screenshot({path:path.join(output,'failure.png'),fullPage:true}); console.error(JSON.stringify({status:'failed',error:error.message,checks,run_evidence:runEvidence,errors,writes},null,2)); throw error;
} finally { await browser.close(); }
