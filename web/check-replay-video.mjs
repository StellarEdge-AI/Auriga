// Read-only browser acceptance of recorded native MP4; no controls or evaluator calls.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';

const engines = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const url = process.env.AURIGA_URL || 'http://127.0.0.1:8767';
const output = process.env.AURIGA_SCREENSHOT_DIR || 'runs/web-replay-video-20260930';
const browser = await engines[process.env.AURIGA_BROWSER || 'firefox'].launch({ headless: true, ...(process.env.AURIGA_BROWSER_EXECUTABLE ? { executablePath: process.env.AURIGA_BROWSER_EXECUTABLE } : {}) });
const page = await browser.newPage();
page.setDefaultTimeout(30000);
const errors = [], writes = [], evidence = [];
page.on('pageerror', (error) => errors.push(error.message));
page.on('request', (request) => { if (!['GET', 'HEAD'].includes(request.method())) writes.push({ method: request.method(), url: request.url() }); });
await fs.mkdir(output, { recursive: true });
try {
  await page.goto(url);
  const run = await page.evaluate(async () => (await fetch((location.pathname.match(/^\/runs\/[^/]+/)?.[0] || '') + '/v1/run')).json());
  assert.equal(run.status, 'replay');
  assert.equal(run.terminal.pid, null);
  await page.waitForFunction(() => document.querySelectorAll('.video-record').length >= 3);
  const exportId = process.env.AURIGA_VIDEO_EXPORT_EVENT_ID || await page.locator('.video-record').first().getAttribute('data-export-event-id');
  const selector = `.video-record[data-export-event-id="${exportId}"]`;
  const records = page.locator(selector);
  await page.waitForFunction((selector) => document.querySelectorAll(selector).length === 3, selector);
  await page.locator('.video-panel > summary').click();
  await page.locator('.raw-activity > summary').click();
  await page.evaluate((selector) => document.querySelectorAll(selector + ' video').forEach((video) => { video.preload = 'auto'; video.load(); }), selector);
  await page.waitForFunction((selector) => Array.from(document.querySelectorAll(selector + ' video')).every((video) => video.readyState >= 2 && video.videoWidth > 0), selector);
  const metadata = JSON.parse(await records.first().locator('pre').textContent());
  const actions = await page.evaluate(async () => {
    const ids = Array.from(new Set(Array.from(document.querySelectorAll('#activity button[data-action-id]'), (button) => button.dataset.actionId).filter(Boolean)));
    return Promise.all(ids.map(async (id) => (await fetch((location.pathname.match(/^\/runs\/[^/]+/)?.[0] || '') + `/v1/actions/${encodeURIComponent(id)}`)).json()));
  });
  const matching = actions.find((action) => action.receipt?.status === 'completed' && action.request.env_generation === metadata.env_generation && action.request.episode_id === metadata.episode_id);
  assert.ok(matching, 'recorded MP4 must have an action from the same physical generation');
  await page.locator(`#activity button[data-action-id="${matching.request.action_id}"]`).click();
  await page.waitForFunction(({ selector, identifier }) => Array.from(document.querySelectorAll(selector)).every((record) => record.dataset.mappedActionId === identifier), { selector, identifier: matching.request.action_id });
  const count = await records.count();
  assert.equal(count, 3);
  for (let index = 0; index < count; index++) {
    const record = records.nth(index);
    const info = JSON.parse(await record.locator('pre').textContent());
    const frames = info.frame_mapping;
    const interval = matching.receipt.execution_interval;
    const start = frames.find((frame) => frame.sim_step === interval.start_sim_step);
    const end = frames.findLast((frame) => frame.sim_step === interval.end_sim_step);
    assert.ok(start && end);
    const media = [];
    for (const [boundary, mapping] of [['start', start], ['end', end]]) {
      await record.locator('.video-actions button').nth(boundary === 'start' ? 0 : 1).click();
      await page.waitForFunction(({ selector, index, pts }) => {
        const video = document.querySelectorAll(selector + ' video')[index];
        return !video.seeking && video.readyState >= 2 && Math.abs(video.currentTime - pts) < 0.001;
      }, { selector, index, pts: mapping.pts_s });
      const frame = await record.locator('video').evaluate((video) => {
        const canvas = document.createElement('canvas'); canvas.width = video.videoWidth; canvas.height = video.videoHeight;
        canvas.getContext('2d').drawImage(video, 0, 0);
        return { png: canvas.toDataURL('image/png').split(',')[1], currentTime: video.currentTime,
          width: video.videoWidth, height: video.videoHeight, duration: video.duration, error: video.error?.code ?? null };
      });
      const bytes = Buffer.from(frame.png, 'base64');
      const filename = `${info.camera_id}-${boundary}.png`;
      await fs.writeFile(path.join(output, filename), bytes);
      delete frame.png;
      assert.equal(frame.error, null);
      media.push({ boundary, video_frame: mapping.video_frame, sim_step: mapping.sim_step, pts_s: mapping.pts_s,
        ...frame, screenshot: filename, sha256: createHash('sha256').update(bytes).digest('hex') });
    }
    const endpoint = new URL((new URL(url).pathname.match(/^\/runs\/[^/]+/)?.[0] || '') + '/v1/artifacts/' + info.video_ref.slice(9), url);
    const head = await fetch(endpoint, { method: 'HEAD' });
    assert.equal(head.status, 200);
    assert.match(head.headers.get('content-type'), /video\/mp4/);
    const range = await fetch(endpoint, { headers: { Range: 'bytes=0-15' } });
    assert.equal(range.status, 206);
    assert.equal((await range.arrayBuffer()).byteLength, 16);
    evidence.push({ camera_id: info.camera_id, run_id: info.run_id, episode_id: info.episode_id,
      env_generation: info.env_generation, frames: info.frames, fps: info.fps, timing: info.timing,
      video_ref: info.video_ref, derived_from_ref: info.derived_from_ref, derivation: info.derivation, codec: info.codec,
      artifact_sha256: head.headers.get('x-artifact-sha256'), head_status: head.status,
      range_status: range.status, mapping_basis: await record.locator('.video-action-status').textContent(), media });
  }
  for (const width of [320, 736, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await page.screenshot({ path: path.join(output, `workbench-${width}.png`), fullPage: true });
  }
  const other = actions.find((action) => action.request.env_generation !== metadata.env_generation);
  assert.ok(other, 'fixture must retain an earlier generation to check video identity rejection');
  await page.locator(`#activity button[data-action-id="${other.request.action_id}"]`).click();
  await page.waitForFunction(() => Array.from(document.querySelectorAll('.video-action-status')).every((status) => status.textContent.includes('不同')));
  assert.equal(await page.locator('.video-actions button:not([disabled])').count(), 0);
  const after = await page.evaluate(async () => (await fetch((location.pathname.match(/^\/runs\/[^/]+/)?.[0] || '') + '/v1/run')).json());
  assert.equal(after.thread_id, run.thread_id);
  assert.equal(after.terminal.session_id, run.terminal.session_id);
  assert.equal(after.terminal.pid, null);
  assert.deepEqual(writes, []);
  assert.deepEqual(errors, []);
  const report = { status: 'passed', observed_at: new Date().toISOString(), data_origin: 'recorded_robodojo_runtime',
    url, run_id: run.run_id, thread_id: run.thread_id, replay_env_generation: run.env_generation,
    export_event_id: exportId, action_id: matching.request.action_id, execution_interval: matching.receipt.execution_interval,
    widths: [320, 736, 1024, 1440], evidence, cross_generation_mapping: 'rejected', writes, errors };
  await fs.writeFile(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true });
  const media = await page.locator('.video-record video').evaluateAll((videos) => videos.map((video) => ({
    src: video.currentSrc, ready_state: video.readyState, error: video.error ? { code: video.error.code, message: video.error.message } : null,
  })));
  console.error(JSON.stringify({ status: 'failed', error: error.message, media, evidence, writes, errors }, null, 2));
  throw error;
} finally {
  await browser.close();
}
