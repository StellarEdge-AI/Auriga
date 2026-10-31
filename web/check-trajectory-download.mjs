import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';

const engines = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.AURIGA_URL || 'http://127.0.0.1:8780';
const output = process.env.AURIGA_SCREENSHOT_DIR || 'runs/trajectory-download-check';
const ids = (process.env.AURIGA_RUN_IDS || '').split(',').filter(Boolean);
assert.ok(ids.length >= 2, 'choose at least two completed real runs to check download scoping');
await fs.mkdir(output, { recursive: true });
const browser = await engines[process.env.AURIGA_BROWSER || 'firefox'].launch({ headless: true });
const report = { url: base, observed_at: new Date().toISOString(), downloads: [], writes: [], page_errors: [] };
try {
  const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1440, height: 1000 } });
  await context.route('**/*', async (route) => {
    if (!['GET', 'HEAD'].includes(route.request().method())) {
      report.writes.push(route.request().url()); return route.abort();
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.on('pageerror', (error) => report.page_errors.push(error.message));
  for (const id of ids) {
    await page.goto(`${base}/?selected=${encodeURIComponent(id)}`);
    const button = page.getByRole('link', { name: '下载原始轨迹', exact: true });
    await button.waitFor({ state: 'visible' });
    const endpoint = `${base}/v1/experiments/${id}/trajectory-bundle`;
    assert.equal(await button.getAttribute('href'), endpoint);
    const downloadEvent = page.waitForEvent('download');
    await button.click();
    const download = await downloadEvent;
    const target = path.resolve(output, `${id}.zip`);
    await download.saveAs(target);
    assert.equal(await download.failure(), null);
    const bytes = await fs.readFile(target), sha256 = createHash('sha256').update(bytes).digest('hex');
    const head = await fetch(endpoint, { method: 'HEAD' });
    assert.equal(head.status, 200);
    assert.equal(Number(head.headers.get('content-length')), bytes.length);
    assert.equal(head.headers.get('x-sha256'), sha256);
    assert.match(head.headers.get('content-disposition'), /attachment/);
    const range = await fetch(endpoint, { headers: { Range: 'bytes=0-31' } });
    assert.equal(range.status, 206);
    assert.deepEqual(Buffer.from(await range.arrayBuffer()), bytes.subarray(0, 32));
    const inspected = JSON.parse(execFileSync(process.env.AURIGA_PYTHON || 'python',
      ['-m', 'auriga.apps.replay.hd_replay', '--source-run', target, '--inspect'], { encoding: 'utf8' }));
    assert.equal(inspected.source_file_sha256, sha256);
    assert.ok(inspected.control_ticks > 0);
    assert.equal(inspected.model_calls, 0);
    report.downloads.push({ catalog_id: id, path: target, filename: download.suggestedFilename(), byte_size: bytes.length,
      sha256, source_run_id: inspected.source_run_id, control_ticks: inspected.control_ticks, native_actions: inspected.native_actions });
    await page.screenshot({ path: path.join(output, `${id}.png`), fullPage: true });
  }
  assert.equal(new Set(report.downloads.map((item) => item.source_run_id)).size, ids.length);
  assert.deepEqual(report.writes, []);
  assert.deepEqual(report.page_errors, []);
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.error = error.stack; throw error;
} finally {
  await fs.writeFile(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2));
  await browser.close();
}
console.log(JSON.stringify(report, null, 2));
