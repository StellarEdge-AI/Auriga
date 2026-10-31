// Read-only browser checks for scoring evidence and structured Codex replay.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
const engines = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.AURIGA_URL || 'http://127.0.0.1:8780';
const output = process.env.AURIGA_SCREENSHOT_DIR || 'runs/web-task-review-check';
const preview = process.env.AURIGA_PREVIEW_ASSETS;
const browser = await engines[process.env.AURIGA_BROWSER || 'chromium'].launch({ headless: true, ...(process.env.AURIGA_BROWSER_EXECUTABLE ? { executablePath: process.env.AURIGA_BROWSER_EXECUTABLE } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const errors = [], writes = [], checks = [];
await fs.mkdir(output, { recursive: true });
await context.route('**/*', async (route) => {
  const request = route.request(), pathname = new URL(request.url()).pathname;
  if (!['GET', 'HEAD'].includes(request.method())) { writes.push(request.url()); return route.abort(); }
  if (preview) {
    const name = path.basename(pathname);
    const file = pathname === '/' ? 'library.html' : /^\/runs\/[^/]+\/$/.test(pathname) ? 'index.html' : ['app.js', 'app.css', 'styles.css', 'library.js', 'library.css'].includes(name) ? name : null;
    if (file) return route.fulfill({ path: path.join(preview, file), contentType: file.endsWith('.html') ? 'text/html' : file.endsWith('.css') ? 'text/css' : 'application/javascript' });
  }
  return route.continue();
});
const page = await context.newPage();
page.on('pageerror', (error) => errors.push(error.message));
page.setDefaultTimeout(30000);
const review = {
  task_id: 'synthetic_task', description: { text: 'Synthetic official instruction: place the numbered tiles in descending order.', source_kind: 'recorded_instruction' },
  scoring: { max_score: 100, aggregation: '取满足的最高阶段分，不累加。', criteria: [
    { id: 'grasp', label: '至少抓起一块数字牌', score: 10, description: '抓取状态必须为真。', recorded_result: null },
    { id: 'place', label: '全部数字牌按降序摆放', score: 100, description: '每块牌均在规定位置内。', recorded_result: null },
  ], completion_conditions: ['双臂回到初始位置。'], rule_source: { status: 'matched', message: '已核对运行记录与规则版本一致。' } },
  result: { status: 'scored', scored_episode: true, normalized_score: 0.1, native_process_score: 10, task_success: false, breakdown_status: 'not_recorded', explanation: ['记录原生过程分 10，按 10 ÷ 100 = 0.1 归一化。'] }, sources: [],
};
const id = 'synthetic-score-review', thread = { thread_id: 'fixture-thread', turn_id: 'fixture-turn' };
const items = [
  { ...thread, id: 'prompt', type: 'userMessage', content: [{ type: 'text', text: 'Synthetic prompt <script>window.injected=true</script>' }] },
  { ...thread, id: 'before', type: 'agentMessage', text: 'Saved explanation before the first tool.' },
  { ...thread, id: 'empty-reasoning', type: 'reasoning', summary: [], content: [] },
  { ...thread, id: 'tool-a', type: 'dynamicToolCall', tool: 'robot_observe', arguments: { language: 'Saved exact instruction A' }, contentItems: [{ type: 'text', text: 'Saved result A' }], status: 'completed', success: true },
  { ...thread, id: 'after', type: 'agentMessage', text: 'Saved explanation after the first tool.' },
  { ...thread, id: 'tool-b', type: 'dynamicToolCall', tool: 'robot_check', arguments: { language: 'Saved exact instruction B' }, status: 'completed' },
];
let itemsUnavailable = false, knownBootstrap, live = false, reviewReads = 0, recordsFixture = false, legacyLongItems = false;
const recordCursors = [];
const longItems = Array.from({ length: 2002 }, (_, index) => ({ id: `history-${index}`, position: index + 1,
  value: { ...thread, id: `history-${index}`, type: 'dynamicToolCall', tool: `recorded_tool_${index}`, status: 'completed', arguments: { language: `Historical instruction ${index}` } } }));
const record = { id, task: review.task_id, run_id: 'fixture-run', title: 'Synthetic scoring evidence', status: 'completed', live: false, replay_available: true, terminal_available: true, view_url: `/runs/${id}/`, summary: { score: 0.1, task_success: false }, task_review: review };
const json = (route, body, status = 200) => route.fulfill({ status, json: body });
try {
  if (process.env.AURIGA_REAL_ONLY !== '1') {
    assert.ok(preview, 'synthetic checks require a preview build');
    await page.route('**/v1/experiments**', (route) => json(route, new URL(route.request().url()).pathname.endsWith(`/${id}`) ? record : { experiments: [record], total: 1, facets: {} }));
    await page.route(`**/runs/${id}/v1/**`, (route) => {
      const endpoint = new URL(route.request().url()).pathname.split('/v1/')[1];
      const query = new URL(route.request().url()).searchParams;
      if (endpoint === 'records' && recordsFixture) {
        if (query.get('mode') === 'history') {
          const before = Number(query.get('before') || Infinity);
          const values = longItems.filter((entry) => entry.position < before).slice(-200);
          return json(route, { items: values, events: [], videos: [], before: values[0]?.position, has_more: values[0]?.position > 1, events_truncated: true });
        }
        const cursor = Number(query.get('items') || 0); recordCursors.push(cursor);
        const entries = longItems.filter((entry) => entry.position > cursor).slice(0, 500);
        const empty = { entries: [], cursor: 0, incremental: true, has_more: false, reset: false };
        return json(route, { event_cursor: 0, observations: empty, requests: empty,
          items: { ...empty, entries, cursor: entries.at(-1)?.position || cursor, has_more: (entries.at(-1)?.position || cursor) < longItems.length } });
      }
      if (endpoint === 'task-review') reviewReads++;
      if (endpoint === 'items' && itemsUnavailable) return json(route, { error: 'temporary_items_failure' }, 503);
      if (endpoint === 'events') return route.fulfill({ contentType: 'text/event-stream', body: ': no actions in fixture\n\n' });
      const data = { run: { ...thread, run_id: 'fixture-run', status: live ? 'running' : 'replay', view_mode: live ? 'live' : 'replay', read_only: true, terminal_available: true, library: { live }, terminal: { exit_code: 1, bootstrap_failed: knownBootstrap } }, 'task-review': review,
        items: { items: legacyLongItems ? longItems.map((entry) => entry.value) : items, cursor: 0 }, requests: { requests: [] }, observations: { observations: [] }, 'scene/head': { scene_version: null }, 'scene/versions': { versions: [] } };
      return json(route, data[endpoint] || { error: 'fixture_unavailable' }, data[endpoint] ? 200 : 404);
    });
    await page.routeWebSocket(`**/runs/${id}/v1/terminal**`, (socket) => {
      const bytes = Buffer.from('Error: account/read failed during TUI bootstrap: account/read failed: workspace routing discovery failed (code -32603)\r\n');
      socket.send(JSON.stringify({ type: 'ready', status: { session_id: 'fixture-terminal', exit_code: 1, cursor: bytes.length, bootstrap_failed: knownBootstrap } }));
      socket.send(JSON.stringify({ type: 'output', after: 0, cursor: bytes.length, data: bytes.toString('base64') }));
    });
    await page.goto(`${origin}/?selected=${id}`);
    await page.waitForSelector('#detail-task-review [data-criterion-id="place"]');
    const homeText = await page.locator('#detail-task-review').innerText();
    assert.match(homeText, /Synthetic official instruction/); assert.match(homeText, /10 ÷ 100 = 0.1/); assert.match(homeText, /未保存此得分点/);
    await page.goto(`${origin}/runs/${id}/`);
    await page.waitForSelector('#run-task-review [data-criterion-id="place"]');
    assert.equal(await page.locator('#run-task-review').innerText(), homeText, 'both views show the same scoring evidence');
    await page.waitForSelector('#codex-replay-content[data-phase-id="item:tool-a"]', { state: 'attached' });
    await page.waitForSelector('#codex-terminal-notice:not([hidden])');
    assert.equal(await page.locator('#codex-tab-terminal').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#raw-terminal').isVisible(), true);
    await page.locator('#codex-tab-records').click();
    const saved = await page.locator('#codex-replay-content').innerText();
    for (const expected of ['Saved explanation before', 'Saved exact instruction A', 'Saved explanation after', '没有可见文本']) assert.ok(saved.includes(expected), expected);
    assert.equal(await page.evaluate(() => window.injected), undefined, 'saved text is rendered as text');
    await page.locator('#codex-replay-content details').filter({ has: page.locator('summary', { hasText: '工具返回' }) }).first().evaluate((node) => { node.open = true; });
    assert.match(await page.locator('#codex-replay-content').innerText(), /Saved result A/);
    await page.locator('#phase-nav button[data-phase-id="item:tool-b"]').click();
    await page.waitForSelector('#codex-replay-content[data-phase-id="item:tool-b"]');
    assert.match(await page.locator('#codex-replay-content').innerText(), /Saved exact instruction B/);
    await page.locator('#codex-tab-terminal').click(); assert.equal(await page.locator('#terminal').isVisible(), true);
    await page.locator('#codex-tab-records').click();
    checks.push('identical official description, all criteria, recorded derivation and missing breakdown on both pages', 'native terminal is the default; supplementary recorded messages follow selected phase and preserve empty reasoning');
    itemsUnavailable = true;
    await page.locator('#phase-retry').click();
    await page.waitForFunction(() => document.getElementById('codex-replay-status').textContent.includes('temporary_items_failure'));
    itemsUnavailable = false;
    await page.locator('#phase-retry').click();
    await page.waitForFunction(() => !document.getElementById('codex-replay-status').textContent.includes('读取失败'));
    assert.match(await page.locator('#codex-replay-status').textContent(), /阶段/);
    itemsUnavailable = true; await page.reload();
    await page.waitForFunction(() => document.getElementById('codex-replay-status').textContent.includes('temporary_items_failure'));
    itemsUnavailable = false; await page.locator('#phase-retry').click();
    await page.waitForSelector('#codex-replay-content .codex-record', { state: 'attached' });
    await page.waitForFunction(() => !document.getElementById('codex-replay-status').textContent.includes('读取失败'));
    knownBootstrap = false; await page.reload();
    await page.waitForSelector('#codex-replay-content .codex-record', { state: 'attached' });
    await page.waitForFunction(() => document.getElementById('terminal-status').textContent.includes('退出'));
    await page.waitForTimeout(150);
    assert.equal(await page.locator('#codex-terminal-notice').isVisible(), false, 'explicit healthy attachment metadata overrides stale errors in an append-only log');
    checks.push('same-record recovery and manual refresh after first-load items failure', 'current terminal diagnosis takes precedence over an older failed attachment');
    review.result = { status: 'unscored', scored_episode: false, normalized_score: null, native_process_score: 10, task_success: null, explanation: ['初始化错误使本次没有正式评分。'], breakdown_status: 'not_recorded' };
    await page.reload(); await page.waitForSelector('#run-task-review [data-result-status="unscored"]');
    const unscored = await page.locator('#run-task-review').innerText(); assert.match(unscored, /不计分/); assert.match(unscored, /未完成正式评测/); assert.doesNotMatch(unscored, /10 ÷ 100/);
    for (const width of [320, 1440]) { await page.setViewportSize({ width, height: 1000 }); assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true); await page.screenshot({ path: path.join(output, `synthetic-${width}.png`), fullPage: true }); }
    checks.push('unscored episode never displays a formal zero; narrow and desktop layouts fit');
    live = true;
    await page.reload(); await page.waitForSelector('#run-task-review [data-result-status="unscored"]');
    const beforeTerminal = reviewReads;
    review.result = { status: 'scored', scored_episode: true, normalized_score: 0.4, native_process_score: 40, task_success: false, breakdown_status: 'not_recorded', explanation: ['终局评分 40 ÷ 100 = 0.4。'] };
    live = false;
    await page.waitForFunction(() => document.getElementById('run-mode').textContent.includes('只读回放'));
    await page.waitForFunction(() => document.getElementById('run-task-review').textContent.includes('40 ÷ 100 = 0.4'));
    assert.ok(reviewReads > beforeTerminal, 'live-to-replay fetches terminal scoring without reload or click');
    checks.push('live-to-replay refreshes final scoring in the existing page without user input');
    recordsFixture = true;
    await page.reload();
    await page.waitForFunction(() => !document.getElementById('records-older').hidden);
    assert.equal(await page.locator('#native-items > li').count(), 2000, 'current native view is bounded');
    assert.deepEqual(recordCursors.slice(0, 5), [0, 500, 1000, 1500, 2000], 'pages advance without repeated full snapshots');
    await page.locator('#records-older').click();
    await page.waitForFunction(() => document.getElementById('records-window').textContent.includes('历史窗口'));
    assert.equal(await page.locator('#native-items > li').count(), 2);
    assert.match(await page.locator('#native-items').textContent(), /recorded_tool_0/);
    assert.equal(await page.locator('#records-latest').isVisible(), true);
    assert.match(await page.locator('#records-window').textContent(), /其余关联事件请查看完整事件证据/);
    assert.equal(await page.locator('#records-events').isVisible(), true);
    assert.equal(await page.locator('#records-events').getAttribute('href'), `/runs/${id}/v1/events?after=0`);
    await page.setViewportSize({ width: 320, height: 1000 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, 'historical limit explanation fits a narrow viewport');
    await page.screenshot({ path: path.join(output, 'historical-320.png'), fullPage: true });
    checks.push('incremental cursors load a bounded current view and earlier items remain available through an explicit history window');
    checks.push('truncated correlated events explicitly link the complete resumable event stream and fit the narrow viewport');
    recordsFixture = false; legacyLongItems = true;
    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll('#native-items > li').length === 2002);
    assert.match(await page.locator('#native-items').textContent(), /recorded_tool_0/);
    assert.equal(await page.locator('#records-older').isVisible(), false, 'unsupported history endpoint is not offered');
    checks.push('legacy servers without paging preserve every returned native item');
  }
  if (process.env.AURIGA_SYNTHETIC_ONLY !== '1') {
    const real = process.env.AURIGA_RUN_ID || 'exp-6f68726126bcb800bf1ec75baf610106';
    const data = await (await fetch(`${origin}/v1/experiments/${real}`)).json();
    assert.ok(data.task_review?.scoring?.criteria?.length, 'recorded task has scoring rules');
    await page.goto(`${origin}/?selected=${real}`); await page.waitForSelector('#detail-task-review .score-criteria li');
    const home = await page.locator('#detail-task-review').innerText();
    await page.goto(`${origin}/runs/${real}/`); await page.waitForSelector('#run-task-review .score-criteria li');
    assert.equal(await page.locator('#run-task-review').innerText(), home);
    await page.waitForSelector('#codex-replay-content .codex-record', { state: 'attached' });
    assert.equal(await page.locator('#codex-tab-terminal').getAttribute('aria-selected'), 'true');
    const selected = await page.locator('#phase-nav button[aria-current]').getAttribute('data-phase-id');
    assert.equal(await page.locator('#codex-replay-content').getAttribute('data-phase-id'), selected);
    await page.screenshot({ path: path.join(output, 'recorded-detail.png'), fullPage: true });
    checks.push(`recorded ${real}: shared scoring evidence and phase-synchronized saved Codex items`);
  }
  assert.deepEqual(errors, []); assert.deepEqual(writes, []);
  console.log(JSON.stringify({ checks, errors, writes }));
} finally { await fs.writeFile(path.join(output, 'browser-report.json'), JSON.stringify({ checks, errors, writes }, null, 2)); await browser.close(); }
