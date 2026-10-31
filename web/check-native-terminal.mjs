// Read-only acceptance against a real archived Codex TUI. Preview routing only
// substitutes static build assets; all protocol data and terminal bytes are real.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';

const engines = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.AURIGA_URL || 'http://127.0.0.1:8780';
const runId = process.env.AURIGA_RUN_ID || 'exp-6f68726126bcb800bf1ec75baf610106';
const prefix = `/runs/${encodeURIComponent(runId)}`;
const preview = process.env.AURIGA_PREVIEW_ASSETS;
const output = process.env.AURIGA_SCREENSHOT_DIR || 'runs/web-native-terminal-check';
const timeout = Number(process.env.AURIGA_TIMEOUT_MS || 90000);
const bootstrapError = 'Error: account/read failed during TUI bootstrap:';
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
const stripAnsi = (text) => text.replace(/\x1b\][\s\S]*?(?:\x07|\x1b\\)/g, '').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').replace(/\x1b[()][A-Z0-9]/g, '').replace(/\x1b[@-_]/g, '');
const compact = (text) => stripAnsi(text).replace(/\s+/g, '');
const read = async (endpoint, binary = false) => {
  const response = await fetch(new URL(endpoint, origin), { signal: AbortSignal.timeout(timeout) });
  assert.equal(response.status, 200, endpoint);
  return binary ? { bytes: Buffer.from(await response.arrayBuffer()), disposition: response.headers.get('content-disposition') } : response.json();
};
const until = async (condition, description) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { const value = condition(); if (value) return value; await new Promise((resolve) => setTimeout(resolve, 100)); }
  throw new Error(`Timed out: ${description}`);
};
const checks = [], errors = [], writes = [], socketWrites = [], streams = [];
let report = { status: 'running', run_id: runId, origin, preview_assets: preview || null };
await fs.mkdir(output, { recursive: true });
const browser = await engines[process.env.AURIGA_BROWSER || 'chromium'].launch({ headless: true,
  ...(process.env.AURIGA_BROWSER_EXECUTABLE ? { executablePath: process.env.AURIGA_BROWSER_EXECUTABLE } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
context.setDefaultTimeout(timeout);
await context.route('**/*', async (route) => {
  const request = route.request(), pathname = new URL(request.url()).pathname;
  if (!['GET', 'HEAD'].includes(request.method())) { writes.push({ method: request.method(), url: request.url() }); return route.abort(); }
  if (preview) {
    const name = /^\/runs\/[^/]+\/$/.test(pathname) ? 'index.html' : path.basename(pathname);
    if (['index.html', 'app.js', 'app.css', 'styles.css'].includes(name)) {
      return route.fulfill({ path: path.join(preview, name), contentType: name.endsWith('.html') ? 'text/html' : name.endsWith('.css') ? 'text/css' : 'application/javascript' });
    }
  }
  return route.continue();
});
// Prevent unexpected input from leaving the browser, including future UI changes.
await context.addInitScript(() => {
  const NativeWebSocket = window.WebSocket;
  window.__nativeTerminalBlocked = [];
  window.WebSocket = class extends NativeWebSocket {
    send(data) {
      if (new URL(this.url).pathname.endsWith('/v1/terminal')) {
        let message; try { message = JSON.parse(data); } catch { message = { type: 'invalid' }; }
        if (message.type !== 'resize') { window.__nativeTerminalBlocked.push(message.type); throw new Error('Acceptance probe blocks terminal input'); }
      }
      return super.send(data);
    }
  };
});
const page = await context.newPage();
page.on('pageerror', (error) => errors.push(error.message));
page.on('websocket', (socket) => {
  if (new URL(socket.url()).pathname !== `${prefix}/v1/terminal`) return;
  const stream = { url: socket.url(), chunks: [], types: [], errors: [], resize_count: 0, closed: false };
  streams.push(stream);
  socket.on('framesent', ({ payload }) => {
    let message; try { message = JSON.parse(String(payload)); } catch { message = { type: 'invalid' }; }
    if (message.type === 'resize') stream.resize_count++; else socketWrites.push(message);
  });
  socket.on('framereceived', ({ payload }) => {
    let message; try { message = JSON.parse(String(payload)); } catch { stream.errors.push('invalid JSON'); return; }
    stream.types.push(message.type);
    if (message.type === 'ready') stream.ready = message.status;
    if (message.type === 'status') stream.status = message.status;
    if (message.type === 'output') stream.chunks.push({ after: message.after, cursor: message.cursor, bytes: Buffer.from(message.data, 'base64') });
    if (['error', 'gap'].includes(message.type)) stream.errors.push(message);
  });
  socket.on('close', () => { stream.closed = true; });
});
const completeStream = async (index) => until(() => {
  if (streams[index]?.errors.length) throw new Error(`Terminal stream ${index}: ${JSON.stringify(streams[index].errors)}`);
  return streams[index]?.status && streams[index];
}, `terminal stream ${index} completion`);
function inspectStream(stream, start = 0) {
  assert.deepEqual(stream.errors, []);
  assert.equal(stream.ready?.replay?.kind, 'recovered_native_tui');
  assert.equal(stream.ready.bootstrap_failed, false);
  assert.equal(stream.ready.replay.history_unchanged, true);
  assert.equal(stream.status.session_id, stream.ready.session_id);
  let cursor = start;
  for (const chunk of stream.chunks) {
    assert.equal(chunk.after, cursor, 'terminal chunks are contiguous, with no overlap or repeated bytes');
    assert.equal(chunk.cursor, cursor + chunk.bytes.length, 'terminal cursor matches actual byte length');
    cursor = chunk.cursor;
  }
  assert.equal(cursor, stream.status.cursor, 'all promised archive bytes were received');
  const bytes = Buffer.concat(stream.chunks.map((chunk) => chunk.bytes));
  return { bytes, summary: { start, cursor, bytes: bytes.length, sha256: sha(bytes), chunk_count: stream.chunks.length, resize_count: stream.resize_count, status: stream.status } };
}
// Reconnect to the real endpoint using its cursor contract. No fabricated frames,
// terminal input, or app state changes are used by this additional reader.
async function resumeStream(after, session) {
  const index = streams.length;
  await page.evaluate(async ({ prefix, after, session, timeout }) => {
    const endpoint = new URL(`${prefix}/v1/terminal`, location.href);
    endpoint.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    endpoint.searchParams.set('after', String(after)); endpoint.searchParams.set('session_id', session);
    await new Promise((resolve, reject) => {
      const socket = new WebSocket(endpoint), timer = setTimeout(() => { socket.close(); reject(new Error('terminal resume timeout')); }, timeout);
      socket.onmessage = ({ data }) => { const message = JSON.parse(data); if (message.type === 'status') { clearTimeout(timer); socket.close(); resolve(); } else if (['error', 'gap'].includes(message.type)) { clearTimeout(timer); socket.close(); reject(new Error(JSON.stringify(message))); } };
      socket.onerror = () => { clearTimeout(timer); reject(new Error('terminal resume WebSocket failed')); };
    });
  }, { prefix, after, session, timeout });
  return inspectStream(await completeStream(index), after);
}
try {
  const beforeRun = await read(`${prefix}/v1/run`);
  assert.ok(beforeRun.read_only || beforeRun.library?.read_only, 'only an archived read-only run is permitted');
  await page.goto(new URL(`${prefix}/`, origin).href);
  await page.waitForSelector('#terminal .xterm-screen');
  const original = inspectStream(await completeStream(0));
  await fs.writeFile(path.join(output, 'native-terminal.bin'), original.bytes);
  report = { ...report, original_stream: original.summary };
  const run = await read(`${prefix}/v1/run`);
  assert.equal(run.terminal?.replay?.kind, 'recovered_native_tui');
  assert.equal(run.terminal.session_id, original.summary.status.session_id);
  assert.equal(run.thread_id, original.summary.status.thread_id);
  const plain = stripAnsi(original.bytes.toString('utf8'));
  assert.ok(original.bytes.includes(Buffer.from('\x1b[')), 'actual native terminal stream contains ANSI control sequences');
  assert.match(plain, /OpenAI Codex/);
  assert.match(plain, /Ask Codex to do anything/);
  assert.equal(compact(plain).includes(compact(bootstrapError)), false, 'recovered native stream excludes the original bootstrap failure even across wrapped lines');
  const items = (await read(`${prefix}/v1/items`)).items;
  const tools = items.filter((item) => item.type === 'dynamicToolCall');
  const failed = tools.filter((item) => item.status === 'failed' || item.success === false).length;
  const called = (plain.match(/\bCalled\b/g) || []).length, nativeFailed = (plain.match(/\bFailed\b/g) || []).length;
  report.native_content = { recorded_tools: tools.length, recorded_failed: failed, called, failed: nativeFailed };
  assert.equal(called, tools.length - failed, 'native Called entries match the recorded successful tools');
  assert.equal(nativeFailed, failed, 'native Failed entries match the recorded failed tools');
  const prompt = items.find((item) => item.type === 'userMessage');
  const promptText = prompt?.text || prompt?.content?.filter((part) => part.type === 'text').map((part) => part.text).join('\n');
  assert.ok(promptText, 'archive contains a recorded user instruction');
  assert.ok(compact(plain).includes(compact(promptText.slice(0, 100))), 'full native stream contains the initial recorded instruction, beyond the visible viewport');
  if (runId === 'exp-6f68726126bcb800bf1ec75baf610106') assert.match(plain, /9532/);
  if (process.env.AURIGA_NATIVE_TEXT) assert.ok(compact(plain).includes(compact(process.env.AURIGA_NATIVE_TEXT)));
  checks.push('real recovered native ANSI stream, archive identity, initial prompt and exact recorded tool counts');

  assert.equal(await page.locator('#codex-tab-terminal').getAttribute('aria-selected'), 'true');
  assert.equal(await page.locator('#raw-terminal').isVisible(), true);
  assert.equal(await page.locator('#codex-replay').isVisible(), false);
  assert.equal(await page.locator('#codex-terminal-notice').isVisible(), false);
  await page.waitForFunction(() => /归档|恢复/.test(document.querySelector('.terminal-note')?.textContent || ''));
  const terminalLabel = { title: await page.locator('.terminal-panel>.panel-bar h2').textContent(), note: await page.locator('.terminal-note').textContent(), status: await page.locator('#terminal-status').textContent() };
  assert.match(terminalLabel.title, /原生/); assert.match(terminalLabel.note, /恢复/);
  await page.waitForFunction(() => document.querySelector('#terminal .xterm-rows')?.textContent?.trim());
  const screen = await page.locator('#terminal .xterm-screen').evaluate((node) => ({ width: node.clientWidth, height: node.clientHeight, text: node.textContent?.trim(), canvases: node.querySelectorAll('canvas').length }));
  assert.ok(screen.width > 0 && screen.height > 0 && screen.text, 'native xterm has visible rendered text, not only an allocated screen');
  assert.equal(await page.locator('#task-prompt').isDisabled(), true);
  assert.equal(await page.locator('#stop').isDisabled(), true);
  const downloaded = await read(`${prefix}/v1/terminal-log`, true);
  assert.match(downloaded.disposition || '', /attachment/);
  if (original.summary.status.replay.original_bootstrap_failed) {
    assert.equal(await page.locator('#terminal-original-log').isVisible(), true);
    assert.equal(new URL(await page.locator('#terminal-original-log').getAttribute('href'), origin).pathname, `${prefix}/v1/terminal-log`);
    assert.ok(downloaded.bytes.includes(Buffer.from(bootstrapError)), 'original download still preserves its bootstrap failure');
  }
  assert.notEqual(sha(downloaded.bytes), original.summary.sha256, 'recovered view and original evidence are distinct');
  checks.push('native TUI is the visible default, recovery is disclosed, original failed log remains downloadable, controls are read-only');

  await page.waitForFunction(() => document.querySelectorAll('#phase-nav button[data-phase-id]').length > 1);
  await page.evaluate(() => { window.__checkedNativeTerminal = document.querySelector('#terminal .xterm-screen'); });
  const phaseButtons = page.locator('#phase-nav button[data-phase-id]');
  assert.ok(await phaseButtons.count() > 1, 'real archive has multiple navigable phases');
  await phaseButtons.last().click();
  const phaseId = await phaseButtons.last().getAttribute('data-phase-id');
  await page.waitForFunction((id) => document.querySelector('#phase-nav button[aria-current]')?.dataset.phaseId === id, phaseId);
  assert.equal(await page.locator('#codex-tab-terminal').getAttribute('aria-selected'), 'true');
  await page.locator('#codex-tab-records').click();
  await page.waitForFunction((id) => document.querySelector('#codex-replay-content')?.dataset.phaseId === id, phaseId);
  assert.equal(await page.locator('#codex-replay').isVisible(), true);
  assert.ok(await page.locator('#codex-replay-content .codex-record').count() > 0);
  await page.locator('#codex-tab-terminal').click();
  assert.equal(await page.evaluate(() => window.__checkedNativeTerminal === document.querySelector('#terminal .xterm-screen')), true);
  await page.locator('#terminal-bottom').click();
  await page.screenshot({ path: path.join(output, 'native-terminal-1440.png'), fullPage: true });
  await page.locator('.terminal-panel').screenshot({ path: path.join(output, 'native-terminal-panel-1440.png') });
  checks.push('phase navigation and supporting records tab preserve the native xterm node');

  const rereadIndex = streams.length;
  await page.locator('#terminal-retry').click();
  const reread = inspectStream(await completeStream(rereadIndex));
  assert.equal(reread.summary.sha256, original.summary.sha256, 'native reread receives one identical archive');
  assert.equal(await page.evaluate(() => window.__checkedNativeTerminal === document.querySelector('#terminal .xterm-screen')), true, 'native reread retains its xterm node');
  const resumed = await resumeStream(original.summary.cursor, original.summary.status.session_id);
  assert.equal(resumed.bytes.length, 0, 'reconnect at the final cursor does not repeat any archive bytes');
  const midpoint = Math.floor(original.summary.cursor / 2);
  const suffix = await resumeStream(midpoint, original.summary.status.session_id);
  assert.deepEqual(suffix.bytes, original.bytes.subarray(midpoint), 'partial reconnect receives exactly the remaining native byte suffix');
  const reloadIndex = streams.length;
  await page.reload();
  await page.waitForSelector('#terminal .xterm-screen');
  const refreshed = inspectStream(await completeStream(reloadIndex));
  assert.equal(refreshed.summary.sha256, original.summary.sha256, 'refresh receives one identical complete archive');
  assert.equal(refreshed.summary.status.session_id, original.summary.status.session_id, 'refresh reuses the recovered archive cache');
  assert.equal(new URL(streams[reloadIndex].url).searchParams.get('after'), '0');
  assert.equal(await page.locator('#codex-tab-terminal').getAttribute('aria-selected'), 'true');
  checks.push('native reread preserves xterm, final-cursor reconnect yields zero bytes, partial reconnect yields an exact suffix, refresh reuses one identical complete archive');
  await page.setViewportSize({ width: 320, height: 900 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  await page.locator('#terminal').scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, 'native-terminal-320.png'), fullPage: true });
  assert.deepEqual(await page.evaluate(() => window.__nativeTerminalBlocked), []);
  assert.deepEqual(writes, []); assert.deepEqual(socketWrites, []); assert.deepEqual(errors, []);
  report = { ...report, status: 'passed', thread_id: run.thread_id, checks, terminal_label: terminalLabel,
    native_content: { recorded_tools: tools.length, called, failed: nativeFailed, initial_prompt_matched: true },
    original_stream: original.summary, original_download: { bytes: downloaded.bytes.length, sha256: sha(downloaded.bytes), bootstrap_error_preserved: downloaded.bytes.includes(Buffer.from(bootstrapError)) },
    reread_stream: reread.summary, end_cursor_reconnect: resumed.summary, partial_reconnect: suffix.summary, refreshed_stream: refreshed.summary };
} catch (error) {
  report = { ...report, status: 'failed', error: error.stack || error.message, checks };
  await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  report = { ...report, observed_at: new Date().toISOString(), writes, socket_writes: socketWrites, errors,
    stream_observations: streams.map((stream) => ({ url: stream.url, types: stream.types, chunk_count: stream.chunks.length, ready: stream.ready, status: stream.status, errors: stream.errors, resize_count: stream.resize_count })) };
  await fs.writeFile(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ status: report.status, run_id: runId, origin, report: path.resolve(output, 'browser-report.json'), checks,
    native_content: report.native_content, bytes: report.original_stream?.bytes, error: report.error, writes, socket_writes: socketWrites, errors }, null, 2));
  await browser.close();
}
