// Isolated browser verification. Only generated media and local source modules
// are served; this check never reads or changes a real experiment.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const engines = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const source = path.dirname(fileURLToPath(import.meta.url));
const output = process.env.AURIGA_SCREENSHOT_DIR || await fs.mkdtemp(path.join(os.tmpdir(), 'auriga-continuity-'));
await fs.mkdir(output, { recursive: true });
const movie = path.join(output, 'synthetic-continuity.mp4');
const encoded = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=5:duration=5',
  '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', movie], { encoding: 'utf8' });
assert.equal(encoded.status, 0, encoded.stderr);
const media = await fs.readFile(movie);
const scripts = new Map(await Promise.all(['phase-review.js', 'phase-model.js'].map(async (name) => [name, await fs.readFile(path.join(source, name))])));
const fields = ['phase-title', 'phase-empty', 'phase-count', 'phase-position', 'phase-playing', 'phase-context', 'terminal-phase-label', 'phase-terminal-status'];
const buttons = ['phase-previous', 'phase-next', 'phase-play', 'phase-terminal-jump', 'phase-retry'];
const html = `<!doctype html><html><body><video id="phase-video" muted controls></video><select id="phase-camera"><option value="cam_head">头部</option></select>
<input type="checkbox" id="phase-continuous" checked>${fields.map((id) => `<div id="${id}"></div>`).join('')}${buttons.map((id) => `<button id="${id}">${id}</button>`).join('')}
<div id="phase-nav"></div><ol id="phase-activity"></ol><div id="phase-segments"></div><script type="module">
import { createPhaseReview } from '/phase-review.js';
const presentedFrame = HTMLVideoElement.prototype.requestVideoFrameCallback;
if (presentedFrame) HTMLVideoElement.prototype.requestVideoFrameCallback = function (callback) {
  return presentedFrame.call(this, (now, metadata) => { window.presentedTime = metadata.mediaTime; callback(now, metadata); });
};
window.setup = (fixture) => {
  window.phaseChanges = [];
  const review = createPhaseReview({ read: async (url) => fixture.actions[url.split('/').at(-1)], artifactURL: (ref) => '/' + ref.slice(9) + '.mp4',
    terminal: { buffer: { active: { length: 0 } } }, terminalReady: async () => false, inspectEvent() {},
    onPhaseChange: (phase, position) => window.phaseChanges.push({ id: phase?.id, time: document.getElementById('phase-video').currentTime, presented: window.presentedTime, ...position }) });
  review.setItems(fixture.items); review.setVideos(fixture.records);
  for (const event of fixture.events) review.receive(event);
  window.addExport = (record) => { fixture.records.push(record); review.setVideos(fixture.records); };
};
window.watchPlayback = () => {
  const video = document.getElementById('phase-video'), descriptor = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'currentTime');
  window.playback = { seeks: [], pauses: 0, plays: 0, samples: [] };
  Object.defineProperty(video, 'currentTime', { get() { return descriptor.get.call(this); }, set(value) { window.playback.seeks.push(value); descriptor.set.call(this, value); } });
  const pause = video.pause.bind(video), play = video.play.bind(video);
  video.pause = () => { window.playback.pauses++; return pause(); }; video.play = () => { window.playback.plays++; return play(); };
  video.addEventListener('timeupdate', () => window.playback.samples.push({ time: video.currentTime, action: video.dataset.actionId, phase: video.dataset.phaseId }));
};
</script></body></html>`;
const identity = { run_id: 'synthetic', episode_id: 'episode', env_instance_id: 'env', env_generation: 1 };
const thread = { thread_id: 'thread', turn_id: 'turn' };
const item = (id) => ({ id, ...thread, type: 'dynamicToolCall', tool: id, arguments: { synthetic_fixture: true } });
const action = (id, start, end) => ({ request: { ...identity, action_id: id }, receipt: { status: 'completed',
  execution_interval: { start_sim_step: start, end_sim_step: end, step_domain: 'recorded-control' } } });
const record = (camera, name, frames) => ({ identity, event: {}, entry: { camera_id: camera, video_ref: 'artifact:' + name, step_domain: 'recorded-control',
  frame_mapping: frames.map(([sim_step, pts_s]) => ({ sim_step, pts_s })) } });
const eventsFor = (entries) => entries.map(([id, owner], index) => ({ event_id: 'event-' + index, server_seq: index + 1, type: 'action.completed', correlation: { ...thread, action_id: id, item_id: owner } }));
const cameraFrames = [[0, 0], [20, 0.8], [40, 1.6], [60, 2.4], [80, 3.2]];
const baseFixture = () => ({ items: ['tool-0', 'observe', 'zero-step', 'tool-3', 'tool-4'].map(item),
  actions: { a0: action('a0', 0, 20), a1: action('a1', 20, 40), zero: action('zero', 40, 40), a3: action('a3', 40, 60), a4: action('a4', 60, 80) },
  events: eventsFor([['a0', 'tool-0'], ['a1', 'tool-0'], ['zero', 'zero-step'], ['a3', 'tool-3'], ['a4', 'tool-4']]),
  records: [record('cam_head', 'head', cameraFrames), record('cam_right_wrist', 'right', cameraFrames)] });
const browser = await engines[process.env.AURIGA_BROWSER || 'chromium'].launch({ headless: true,
  ...(process.env.AURIGA_BROWSER_EXECUTABLE ? { executablePath: process.env.AURIGA_BROWSER_EXECUTABLE } : {}) });
const errors = [], evidence = [];

async function openFixture(fixture, fallback = false) {
  const page = await browser.newPage(); page.setDefaultTimeout(10000);
  page.on('pageerror', (error) => errors.push(error.message));
  if (fallback) await page.addInitScript(() => { HTMLVideoElement.prototype.requestVideoFrameCallback = undefined; });
  await page.route('http://continuity.test/**', async (route) => {
    assert.ok(['GET', 'HEAD'].includes(route.request().method()));
    const name = path.basename(new URL(route.request().url()).pathname);
    if (scripts.has(name)) return route.fulfill({ contentType: 'text/javascript', body: scripts.get(name) });
    if (name.endsWith('.mp4')) {
      const range = /^bytes=(\d+)-(\d*)$/.exec(route.request().headers().range || '');
      if (range) {
        const start = Number(range[1]), end = Math.min(Number(range[2] || media.length - 1), media.length - 1);
        return route.fulfill({ status: 206, contentType: 'video/mp4', headers: { 'Accept-Ranges': 'bytes', 'Content-Range': `bytes ${start}-${end}/${media.length}` }, body: media.subarray(start, end + 1) });
      }
      return route.fulfill({ contentType: 'video/mp4', headers: { 'Accept-Ranges': 'bytes' }, body: media });
    }
    return route.fulfill({ contentType: 'text/html', body: html });
  });
  await page.goto('http://continuity.test/'); await page.waitForFunction(() => typeof window.setup === 'function');
  await page.evaluate((value) => window.setup(value), fixture);
  await page.waitForFunction(() => { const video = document.getElementById('phase-video'); return video.readyState >= 2 && !video.seeking && !video.hidden; });
  return page;
}
async function startWatching(page) {
  await page.locator('#phase-play').click();
  await page.waitForFunction(() => { const video = document.getElementById('phase-video'); return !video.paused && !video.seeking && video.currentTime > 0.08; });
  await page.evaluate(() => window.watchPlayback());
}

try {
  for (const fallback of [false, true]) {
    const fixture = baseFixture(), page = await openFixture(fixture, fallback);
    await page.locator('#phase-camera').selectOption('cam_right_wrist');
    await page.waitForFunction(() => document.getElementById('phase-video').readyState >= 2 && !document.getElementById('phase-video').seeking);
    await startWatching(page);
    // A newer export with identical coverage must not replace the active source.
    await page.evaluate((value) => window.addExport(value), record('cam_right_wrist', 'newer-right', cameraFrames));
    await page.waitForFunction(() => document.getElementById('phase-video').dataset.actionId === 'a4' && document.getElementById('phase-video').currentTime > 2.65);
    const result = await page.evaluate(() => ({ ...window.playback, source: document.getElementById('phase-video').currentSrc,
      camera: document.getElementById('phase-camera').value, selected: document.querySelector('#phase-nav [aria-current="step"]').dataset.phaseId,
      context: document.getElementById('phase-context').textContent, changes: window.phaseChanges }));
    assert.deepEqual(result.seeks, [], 'contiguous boundaries must not assign currentTime');
    assert.equal(result.pauses, 0); assert.equal(result.plays, 0);
    assert.equal(result.camera, 'cam_right_wrist'); assert.match(result.source, /\/right\.mp4$/);
    assert.equal(result.selected, 'item:tool-4'); assert.match(result.context, /tool-4/);
    assert.ok(result.samples.every((sample, index) => !index || sample.time >= result.samples[index - 1].time), 'playback time never goes backwards');
    const boundaryTime = (change) => fallback ? change.time : change.presented;
    assert.ok(result.changes.some((change) => change.id === 'item:tool-3' && boundaryTime(change) >= 1.6 && boundaryTime(change) < 2.4), JSON.stringify(result.changes));
    assert.ok(result.changes.some((change) => change.id === 'item:tool-4' && boundaryTime(change) >= 2.4 && boundaryTime(change) < 3.2), JSON.stringify(result.changes));
    evidence.push({ case: fallback ? 'contiguous-timeupdate-fallback' : 'contiguous-presented-frames', ...result });
    await page.close();
  }
  const gap = baseFixture(); gap.items = ['tool-0', 'tool-3'].map(item); gap.actions = { a0: gap.actions.a0, a3: gap.actions.a3 };
  gap.events = eventsFor([['a0', 'tool-0'], ['a3', 'tool-3']]);
  const gapPage = await openFixture(gap); await startWatching(gapPage);
  await gapPage.waitForFunction(() => document.getElementById('phase-video').dataset.actionId === 'a3' && !document.getElementById('phase-video').seeking && document.getElementById('phase-video').currentTime >= 1.6);
  const gapResult = await gapPage.evaluate(() => ({ ...window.playback, notice: document.getElementById('phase-position').textContent }));
  assert.deepEqual(gapResult.seeks, [1.6]); assert.equal(gapResult.pauses, 1); assert.equal(gapResult.plays, 1);
  assert.match(gapResult.notice, /已跳过未关联片段.*0\.800–1\.600/);
  evidence.push({ case: 'explicit-recorded-gap', ...gapResult }); await gapPage.close();

  const separate = { items: ['first', 'second'].map(item), actions: { a0: action('a0', 0, 20), a1: action('a1', 20, 40) },
    events: eventsFor([['a0', 'first'], ['a1', 'second']]),
    records: [record('cam_head', 'first-export', [[0, 0], [20, 0.8]]), record('cam_head', 'second-export', [[20, 0], [40, 0.8]])] };
  const separatePage = await openFixture(separate); await startWatching(separatePage);
  await separatePage.waitForFunction(() => document.getElementById('phase-video').dataset.actionId === 'a1' && document.getElementById('phase-video').readyState >= 2 && !document.getElementById('phase-video').seeking);
  const separateResult = await separatePage.evaluate(() => ({ ...window.playback, source: document.getElementById('phase-video').currentSrc, notice: document.getElementById('phase-position').textContent }));
  assert.deepEqual(separateResult.seeks, [0]); assert.match(separateResult.source, /\/second-export\.mp4$/); assert.match(separateResult.notice, /已切换录像文件/);
  evidence.push({ case: 'separate-export-clock', ...separateResult }); await separatePage.close();
  assert.deepEqual(errors, []);
  const report = { status: 'passed', data_origin: 'generated_browser_fixture', observed_at: new Date().toISOString(), evidence, errors };
  await fs.writeFile(path.join(output, 'continuity-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ status: report.status, cases: evidence.map((entry) => entry.case), report: path.join(output, 'continuity-report.json') }, null, 2));
} finally { await browser.close(); }
