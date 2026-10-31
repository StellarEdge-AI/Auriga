// Browser-only synthetic acceptance: every request is intercepted; no live service is contacted.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const engines = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const preview = process.env.AURIGA_PREVIEW_ASSETS;
assert.ok(preview, 'build an isolated preview and set AURIGA_PREVIEW_ASSETS');
const output = process.env.AURIGA_SCREENSHOT_DIR || '/tmp/auriga-comparison-check';
await fs.mkdir(output, { recursive: true });
const videoFile = process.env.AURIGA_FIXTURE_VIDEO || path.join(output, 'synthetic-comparison.mp4');
if (!process.env.AURIGA_FIXTURE_VIDEO) {
  const result = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=10:duration=6',
    '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', videoFile], { encoding: 'utf8' });
  assert.equal(result.status, 0, `generate CPU-only synthetic clip: ${result.stderr || result.error || ''}`);
}
const clip = await fs.readFile(videoFile);
const origin = 'http://comparison-fixture.invalid';
const checks = [], errors = [], writes = [], apiRequests = [], mediaRequests = [], screenshots = [];
let revision = 1, failGroups = false, lateMediaAvailable = false, delayedGroup, releaseGroup, delayedMedia, releaseMedia;
let holdMedia = true, allowRunDetails = false;
const pendingMedia = [], failMedia = new Set(['unknown-b']);
const failGroupDetails = new Set();
const hostile = '<img src=x onerror="window.comparisonInjected=true">';
const keys = { common: 'stack_bowls:1', regression: 'stack_bowls:2', unscored: 'stack_bowls:3', baselineOnly: 'stack_bowls:4', candidateOnly: 'stack_bowls:9' };
const records = new Map();
function run(id, score, valid = true) {
  const record = { id, run_id: `synthetic-${id}`, title: `合成记录 ${id}`, task: 'stack_bowls', status: 'completed',
    created_at: '2031-01-01T00:00:00Z', updated_at: '2032-01-01T00:00:00Z', data_origin: 'synthetic_fixture',
    started_at: '2026-10-01T00:01:02Z', finished_at: '2026-10-01T00:06:07Z',
    started_at_source: 'events.session_started.ts', finished_at_source: 'events.session_finished.ts',
    registered_at: '2026-10-02T01:02:03+08:00', registered_at_source: 'experiment.json.created_at',
    native_finished_at: '2033-01-01T00:00:00Z', report_generated_at: '2034-01-01T00:00:00Z', mtime: '2035-01-01T00:00:00Z',
    category: 'synthetic_fixture', kind: 'native', tags: ['synthetic_fixture'], live: false, read_only: true, writable: false,
    replay_available: true, terminal_available: false, view_url: `/runs/${id}/`,
    summary: { normalized_score: score, task_success: valid ? score === 1 : null, valid_episode: valid, scored_episode: valid,
      ...(valid ? {} : { primary_outcome: 'unscored_harness_timeout', termination_reason: 'official_no_action_progress_timeout' }) } };
  records.set(id, record);
  return record;
}
function fixtureCase(key, record, extra = {}) {
  return { case_key: key, case_id: key, task: 'stack_bowls', variant: null, eval_seed: Number(key.split(':').at(-1)), layout_id: 'synthetic-layout',
    run: record, attempts: record ? [record] : [], selection: record ? 'single' : 'missing', warnings: [], ...extra };
}
const cases = {
  base: [fixtureCase(keys.common, run('base-a', 0)), fixtureCase(keys.regression, run('base-b', 0.8)),
    fixtureCase(keys.unscored, run('base-c', 0, false)), fixtureCase(keys.baselineOnly, run('base-d', 1))],
  iteration1: [fixtureCase(keys.common, run('iter1-a', 0.5)), fixtureCase(keys.regression, run('iter1-b', 0.5)),
    fixtureCase(keys.unscored, run('iter1-c', 0, false)), fixtureCase(keys.candidateOnly, run('iter1-e', 0))],
  iteration2: [fixtureCase(keys.common, run('iter2-a', 0.9)), fixtureCase(keys.regression, null),
    fixtureCase(keys.unscored, run('iter2-c', 0.2))],
  unknown: [fixtureCase(keys.common, run('unknown-a', 0.2)), fixtureCase(keys.regression, run('unknown-b', null, false))],
};
function group(id, label, groupCases, extra = {}) {
  return { id, label, benchmark_group: 'synthetic-benchmark', method: 'synthetic-method', series: 'synthetic-series', iteration: null,
    series_name_zh: '合成观察实验', started_at: '2026-10-01T00:01:02Z', finished_at: '2026-10-01T00:06:07Z',
    time_coverage: { run_count: groupCases.filter((item) => item.run).length, started_count: groupCases.filter((item) => item.run).length, finished_count: groupCases.filter((item) => item.run).length },
    created_at: '2026-10-01T00:00:00Z', baseline_group_id: null, parent_group_id: null,
    source: { manifest: `synthetic/${id}/manifest.json`, worktree: `synthetic/${id}`, branch: 'synthetic-fixture', revision: 'synthetic-revision' },
    conditions: { model: 'synthetic-model', reasoning_effort: 'high', observation_profile: 'rgb_only' },
    case_count: groupCases.length, cases: groupCases, warnings: [], revision: `fixture-${id}-1`, details_loaded: true, ...extra };
}
const groups = [
  group('base', '合成基线', cases.base, { iteration: 0, name_zh: '合成对照基线', role: 'baseline', series_name_zh: '独立对照方向',
    relation_status: { parent: 'not_applicable', baseline: 'not_applicable' } }),
  group('iteration1', `旧名称 ${hostile}`, cases.iteration1, { iteration: 1, baseline_group_id: 'base', parent_group_id: 'base',
    name_zh: `合成中文方案名称 ${hostile}`, purpose: `合成用途：复查指定场景 ${hostile}`,
    experiment_nature: ['合成同场景对照', `恶意性质文本 ${hostile}`], baseline_reason: `同一合成协议的显式对照 ${hostile}`,
    relation_status: { parent: 'confirmed', baseline: 'confirmed' } }),
  group('iteration2', '合成迭代 2', cases.iteration2, { iteration: 2, baseline_group_id: 'base', parent_group_id: 'iteration1' }),
  group('unknown', '合成独立组', cases.unknown, { series: 'unknown-method', series_name_zh: '其他独立实验', source: {}, conditions: {}, protocol_denominator_known: false,
    relation_status: { parent: 'unresolved', baseline: 'unresolved' },
    warnings: ['合成夹具：缺少可验证的迭代关系'] }),
  group('fifth', '第五个合成组', [fixtureCase(keys.common, run('broken-a', 0.3))], { series: 'fifth-method', series_name_zh: '其他独立实验' }),
];
for (const number of [3, 4, 5]) groups.push(group(`iteration${number}`, `合成方案 ${number}`,
  [fixtureCase(keys.common, run(`iter${number}-a`, 0.3)), fixtureCase(keys.regression, run(`iter${number}-b`, 0.8))],
  { iteration: number, baseline_group_id: 'base', parent_group_id: `iteration${number - 1}` }));
const directionIds = ['base', 'iteration1', 'iteration2', 'iteration3', 'iteration4', 'iteration5'];
Object.assign(records.get('iter1-c'), { started_at: null, finished_at: null,
  imported_at: '2026-10-03T04:05:06Z', imported_at_source: 'import.json.imported_at' });
cases.base[0].attempts.push(run('base-later-success', 1));
cases.base[0].selection = 'first_scored';
cases.iteration1[0].attempts.unshift(run('iter1-interrupted', null, false));
cases.iteration1[0].selection = 'first_scored';
Object.assign(records.get('iter1-interrupted'), { started_at: '2026-09-30T23:59:58Z', finished_at: null });
Object.assign(records.get('base-later-success'), { started_at: '2026-10-04T02:03:04+08:00', finished_at: '2026-10-04T02:08:09+08:00' });
groups[1].time_coverage = { run_count: 5, started_count: 4, finished_count: 3 };
for (const [item, alias] of [[groups[0], 'old-baseline'], [groups[1], 'old-recovery']]) {
  item.id_aliases = [alias];
  item.execution_groups = [
    { id: item.id, name_zh: item.name_zh, manifest: item.manifest, source: item.source, conditions: item.conditions },
    { id: alias, name_zh: `旧执行记录 ${hostile}`, manifest: `synthetic/${alias}.json`, source: { branch: `旧分支 ${hostile}` }, conditions: {} },
  ];
}
const browser = await engines[process.env.AURIGA_BROWSER || 'firefox'].launch({ headless: true,
  ...(process.env.AURIGA_BROWSER_EXECUTABLE ? { executablePath: process.env.AURIGA_BROWSER_EXECUTABLE } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, timezoneId: 'America/Los_Angeles' });
context.setDefaultTimeout(Number(process.env.AURIGA_TIMEOUT_MS || 30000));
await context.addInitScript(() => {
  window.EventSource = class extends EventTarget {
    static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
    readyState = 1;
    constructor() { super(); window.fixtureEvents = this; queueMicrotask(() => this.dispatchEvent(new Event('open'))); }
    emit(type, data) { this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify(data) })); }
    close() { this.readyState = 2; }
  };
});
await context.route('**/*', async (route) => {
  const request = route.request(), endpoint = new URL(request.url());
  if (!['GET', 'HEAD'].includes(request.method())) { writes.push({ method: request.method(), path: endpoint.pathname }); return route.abort(); }
  if (endpoint.pathname.startsWith('/v1/')) apiRequests.push(endpoint.pathname + endpoint.search);
  if (endpoint.pathname === '/v1/experiment-groups') {
    assert.equal(endpoint.searchParams.get('view'), 'summary', 'catalog requests use the lightweight group projection');
    if (failGroups) return route.fulfill({ status: 503, json: { error: 'synthetic catalog unavailable' } });
    return route.fulfill({ json: { groups: groups.map((item) => ({ ...item, execution_groups: undefined, details_loaded: false,
      cases: item.cases.map((entry) => ({ case_key: entry.case_key, run: entry.run ? { summary: entry.run.summary } : null })) })),
    errors: [], observed_at: new Date().toISOString(), revision } });
  }
  if (endpoint.pathname.startsWith('/v1/experiment-groups/')) {
    const id = decodeURIComponent(endpoint.pathname.split('/').at(-1)), item = groups.find((entry) => entry.id === id);
    if (!item) return route.fulfill({ status: 404, json: { error: 'synthetic group absent' } });
    if (failGroupDetails.has(id)) return route.fulfill({ status: 503, json: { error: 'synthetic group detail unavailable' } });
    const body = JSON.stringify(item);
    if (id === delayedGroup) await new Promise((resolve) => { releaseGroup = resolve; });
    return route.fulfill({ contentType: 'application/json', body });
  }
  if (endpoint.pathname === '/v1/experiments') return route.fulfill({ json: { experiments: [...records.values()], total: records.size, offset: 0, limit: 100,
    facets: { categories: ['synthetic_fixture'], statuses: ['completed'], tasks: ['stack_bowls'], tags: ['synthetic_fixture'] }, errors: [] } });
  if (endpoint.pathname.startsWith('/v1/experiments/')) {
    if (!endpoint.pathname.endsWith('/media')) {
      assert.ok(allowRunDetails, 'only an explicit run-details click may fetch a full experiment');
      const record = records.get(decodeURIComponent(endpoint.pathname.split('/').at(-1)));
      return route.fulfill({ status: record ? 200 : 404, json: record ? { ...record, media: [], reports: [] } : { error: 'synthetic record absent' } });
    }
    const id = decodeURIComponent(endpoint.pathname.split('/').at(-2)), record = records.get(id);
    if (!record) return route.fulfill({ status: 404, json: { error: 'synthetic record absent' } });
    if (holdMedia) await new Promise((resolve) => pendingMedia.push(resolve));
    if (failMedia.has(id)) return route.fulfill({ status: 503, json: { error: 'synthetic detail unavailable' } });
    const cameras = id.startsWith('iter2') || id.startsWith('unknown') ? ['cam_head'] : ['cam_head', 'cam_left_wrist', 'cam_right_wrist'];
    const media = id === 'iter1-b' && !lateMediaAvailable ? [] : cameras.map((camera_id) => ({ name: `${camera_id}.mp4`, camera_id, mime_type: 'video/mp4',
      url: `/synthetic-media/${id}/${camera_id}.mp4` }));
    const body = JSON.stringify({ id, view_url: record.view_url, replay_available: record.replay_available, media, reports: [] });
    if (id === delayedMedia) await new Promise((resolve) => { releaseMedia = resolve; });
    return route.fulfill({ contentType: 'application/json', body });
  }
  if (endpoint.pathname.startsWith('/synthetic-media/')) {
    mediaRequests.push({ path: endpoint.pathname, range: request.headers().range || null });
    if (endpoint.pathname.includes('/broken-a/')) return route.fulfill({ status: 404, body: 'synthetic recording absent' });
    const match = /^bytes=(\d+)-(\d*)$/.exec(request.headers().range || '');
    if (match) {
      const start = Number(match[1]), end = Math.min(Number(match[2] || clip.length - 1), clip.length - 1);
      return route.fulfill({ status: 206, contentType: 'video/mp4', body: clip.subarray(start, end + 1),
        headers: { 'accept-ranges': 'bytes', 'content-range': `bytes ${start}-${end}/${clip.length}` } });
    }
    return route.fulfill({ contentType: 'video/mp4', body: clip, headers: { 'accept-ranges': 'bytes' } });
  }
  const name = endpoint.pathname === '/' ? 'library.html' : path.basename(endpoint.pathname);
  if (['library.html', 'library.js', 'library.css', 'styles.css'].includes(name)) return route.fulfill({ path: path.join(preview, name),
    contentType: name.endsWith('.html') ? 'text/html' : name.endsWith('.css') ? 'text/css' : 'application/javascript' });
  return route.fulfill({ status: 404, body: 'resource absent from synthetic fixture' });
});
const page = await context.newPage();
page.on('pageerror', (error) => errors.push(error.message));
page.on('websocket', (socket) => socket.on('framesent', ({ payload }) => writes.push({ websocket: socket.url(), payload: String(payload) })));
const rowSelector = (key) => `#comparison-videos tr[data-case-key="${key}"]`;
const card = (id, key = keys.common) => page.locator(`${rowSelector(key)} article[data-group-id="${id}"]`);
const videoState = (key) => page.locator(key ? `${rowSelector(key)} video` : '#comparison-videos video').evaluateAll((videos) => videos.map((video) => ({
  src: video.currentSrc, time: video.currentTime, duration: video.duration, paused: video.paused, speed: video.playbackRate,
  ready: video.readyState, width: video.videoWidth, error: video.error?.code ?? null,
})));
const waitDecoded = async (count, key = keys.common) => page.waitForFunction(({ expected, key }) => {
  const videos = [...document.querySelectorAll('#comparison-videos tr')].find((row) => row.dataset.caseKey === key)?.querySelectorAll('video') || [];
  return videos.length === expected && [...videos].every((video) => video.readyState >= 2 && video.videoWidth > 0 && !video.seeking);
}, { expected: count, key });
const assertCase = async (key) => {
  await page.waitForFunction((expected) => new URL(location.href).searchParams.get('case') === expected, key);
  assert.equal(await page.locator('#case-table .case-select[aria-pressed="true"]').getAttribute('data-case-key'), key);
  assert.match(await page.locator('#comparison-case-meta').textContent(), new RegExp(`seed ${key.split(':').at(-1)}(?:\\D|$)`));
};
const chooseGroup = async (id) => {
  const family = page.locator(`#group-list details:has([data-group-id="${id}"])`);
  if (!await family.evaluate((element) => element.open)) await family.locator(':scope > summary').click();
  await page.locator(`#group-list [data-group-id="${id}"]`).click();
};
const gridIds = () => page.locator(`${rowSelector(keys.common)} article`).evaluateAll((items) => items.map((item) => item.dataset.groupId));
const selectCase = async (key) => { await page.locator(`${rowSelector(key)} .case-select`).click(); await assertCase(key); };
let decoded;
try {
  const entry = new URL(origin);
  for (const [key, value] of Object.entries({ view: 'compare', scope: 'direction', group: 'old-recovery', compare: 'old-baseline,old-recovery', reference: 'old-baseline', case: keys.common, camera: 'head' })) entry.searchParams.set(key, value);
  await page.goto(entry.href);
  await page.waitForSelector('#comparison-videos article[data-group-id="iteration5"]');
  assert.equal(await page.locator('#case-table thead th').count(), 7);
  assert.equal(await page.locator('#comparison-videos tr').count(), 5);
  assert.equal(await page.locator('#comparison-videos article').count(), 30, 'all scene × scheme cells exist before any media response');
  assert.equal(await page.locator('#comparison-videos video').count(), 0);
  assert.deepEqual(await gridIds(), directionIds, 'a whole direction includes five candidates and its separately registered baseline');
  assert.equal(mediaRequests.length, 0);
  assert.equal(apiRequests.some((endpoint) => /\/(?:base-d|iter1-e)\/media$/.test(endpoint)), false, 'offscreen final scene manifests stay unloaded');
  assert.deepEqual([...new Set(apiRequests.filter((endpoint) => endpoint.startsWith('/v1/experiment-groups/')))].sort(),
    directionIds.map((id) => `/v1/experiment-groups/${id}`).sort());
  assert.equal(apiRequests.some((endpoint) => endpoint === '/v1/experiments' || endpoint.startsWith('/v1/experiments?')), false);
  assert.ok(apiRequests.filter((endpoint) => endpoint.startsWith('/v1/experiments/')).every((endpoint) => endpoint.endsWith('/media')));
  const slowPlay = page.waitForRequest((request) => new URL(request.url()).pathname === '/v1/experiments/iter5-a/media');
  await page.locator('#comparison-play').click(); await slowPlay;
  await page.locator('#comparison-pause').click();
  holdMedia = false; pendingMedia.splice(0).forEach((release) => release());
  await waitDecoded(6);
  assert.ok((await videoState()).every((video) => video.paused), 'pause while manifests are pending cancels the delayed row play');
  checks.push('six whole-direction columns and all five scene rows render before media; offscreen rows stay lazy; only summary, selected group details and visible media manifests are requested');

  assert.equal(await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone), 'America/Los_Angeles');
  assert.equal(await card('base').locator(':scope > .experiment-time').textContent(), '开始：2026-10-01 08:01:02\n结束：2026-10-01 08:06:07');
  assert.match(await card('iteration1', keys.unscored).locator(':scope > .experiment-time').textContent(), /开始：时间未记录\n结束：时间未记录\n登记：2026-10-02 01:02:03\n导入：2026-10-03 12:05:06/);
  assert.equal(await card('iteration2', keys.regression).locator(':scope > .experiment-time').textContent(), '尚无运行记录');
  assert.match(await card('base').locator(':scope > .experiment-time').getAttribute('title'), /events.session_started.ts/);
  assert.match(await page.locator('#group-relations .experiment-time').textContent(), /2026-10-01 08:01:02.*开始 4\/5，结束 3\/5.*UTC\+8/);
  assert.doesNotMatch(await page.locator('#comparison-workspace').textContent(), /203[1-5]-01-01/, 'registration, native finish, report time and mtime never stand in for execution time');
  assert.equal(await page.locator('#reference-group').inputValue(), 'base');
  assert.equal(await page.locator('#group-title').textContent(), groups[1].name_zh);
  const relations = await page.locator('#group-relations').textContent();
  assert.ok(relations.includes(groups[1].purpose));
  assert.ok(relations.includes(`实验性质：${groups[1].experiment_nature.join(' · ')}`));
  assert.ok((await page.locator('#group-provenance').textContent()).includes(`对照依据：${groups[1].baseline_reason}`));
  assert.equal(await page.locator('#comparison-workspace img').count(), 0);
  assert.equal(await page.evaluate(() => Boolean(window.comparisonInjected)), false);
  assert.equal(new URL(page.url()).searchParams.get('group'), 'iteration1');
  assert.equal(new URL(page.url()).searchParams.get('reference'), 'base');
  assert.equal(new URL(page.url()).searchParams.get('scope'), 'direction');
  assert.equal(await page.locator('#group-list [data-group-id="old-recovery"]').count(), 0);
  assert.match(relations, /共 2 条执行记录/);
  const direction = page.locator('#group-list .group-family').filter({ has: page.locator('[data-group-id="iteration1"]') });
  await direction.locator(':scope > summary').click();
  assert.equal(await page.locator('#group-list [data-group-id="iteration1"]').isVisible(), false);
  assert.equal(await page.locator('#group-title').textContent(), groups[1].name_zh);
  await direction.locator(':scope > summary').click();
  await page.locator('#group-provenance > summary').click();
  assert.equal(await page.locator('#group-provenance .execution-record').count(), 2);
  assert.equal(await page.locator('#group-provenance img').count(), 0);
  await page.locator('#group-provenance > summary').click();
  for (const id of ['base', 'iteration1']) {
    const attempts = card(id).locator('.attempt-history');
    await attempts.locator('summary').click();
    assert.equal(await attempts.locator('[data-attempt-id]').count(), 2);
    assert.equal(await attempts.locator('.experiment-time').count(), 2);
  }
  assert.match(await page.locator('[data-attempt-id="base-a"]').textContent(), /0\.0 分 · 当前采用/);
  assert.doesNotMatch(await page.locator('[data-attempt-id="base-later-success"]').textContent(), /当前采用/);
  assert.match(await page.locator('[data-attempt-id="base-later-success"]').locator('..').textContent(), /开始：2026-10-04 02:03:04.*结束：2026-10-04 02:08:09/s);
  assert.match(await page.locator('[data-attempt-id="iter1-a"]').textContent(), /50\.0 分 · 当前采用/);
  assert.match(await page.locator('[data-attempt-id="iter1-interrupted"]').locator('..').textContent(), /未评分.*开始：2026-10-01 07:59:58.*结束：时间未记录/s);
  for (const id of ['base', 'iteration1']) await card(id).locator('.attempt-history summary').click();
  checks.push('UTC+8 second-precision start/end, missing times, separate registration/import labels and attempt times are correct in a Los Angeles browser; aliases, Chinese names, hostile text and first formal zero remain intact');

  const baselineRow = page.locator('#score-comparison tbody tr[data-group-id="base"]');
  const candidateRow = page.locator('#score-comparison tbody tr[data-group-id="iteration1"]');
  assert.equal(await baselineRow.locator('[data-metric="mean"]').textContent(), '60.0');
  assert.equal(await candidateRow.locator('[data-metric="mean"]').textContent(), '33.3');
  assert.match(await candidateRow.locator('[data-metric="scored"]').textContent(), /3\s*\/\s*4/);
  assert.match(await candidateRow.locator('[data-metric="paired-count"]').textContent(), /2\s*\/\s*3/);
  assert.match(await candidateRow.locator('[data-metric="paired-delta"]').textContent(), /\+10\.0/);
  await page.locator('#case-filter').selectOption('regressions');
  assert.equal(await page.locator('#case-table .case-select').count(), 1);
  await assertCase(keys.regression);
  assert.equal(await page.locator('#comparison-prev-case').isDisabled(), true);
  assert.equal(await page.locator('#comparison-next-case').isDisabled(), true);
  await page.locator('#case-filter').selectOption('improvements');
  assert.equal(await page.locator('#case-table .case-select').count(), 1);
  await assertCase(keys.common);
  await page.locator('#case-search').fill('no-such-synthetic-task');
  assert.equal(await page.locator('#case-table .case-select').count(), 0);
  assert.equal(await page.locator('#comparison-prev-case').isDisabled(), true);
  assert.equal(await page.locator('#comparison-next-case').isDisabled(), true);
  await page.locator('#case-search').fill('');
  await page.locator('#case-filter').selectOption('all');
  await assertCase(keys.common);
  for (const key of [keys.regression, keys.unscored, keys.baselineOnly, keys.candidateOnly]) {
    await page.locator('#comparison-next-case').click(); await assertCase(key);
    assert.equal(await page.locator('#comparison-next-case').isDisabled(), key === keys.candidateOnly);
  }
  await page.locator('#comparison-prev-case').click(); await assertCase(keys.baselineOnly);
  await selectCase(keys.common);
  checks.push('fixed denominators, formal zero and null scoring survive the full grid; paired delta remains +10.0; filters and scene navigation follow exact seeds and disable at boundaries');

  await page.locator('#comparison-play').click();
  await waitDecoded(6);
  await page.waitForFunction(() => [...document.querySelectorAll('#comparison-videos tr.selected-case video')].every((video) => !video.paused && video.currentTime > 0.1));
  await page.locator('#comparison-pause').click();
  decoded = await videoState(keys.common);
  assert.ok(decoded.every((video) => video.width === 320 && video.error === null && video.paused));
  await page.locator('#comparison-speed').selectOption('2');
  await page.locator('#comparison-seek').evaluate((input) => { input.value = '1.5'; input.dispatchEvent(new Event('input', { bubbles: true })); });
  await page.waitForFunction(() => [...document.querySelectorAll('#comparison-videos tr.selected-case video')].every((video) => !video.seeking && Math.abs(video.currentTime - 1.5) < 0.1));
  assert.ok((await videoState(keys.common)).every((video) => video.speed === 2));
  assert.ok((await videoState(keys.regression)).every((video) => video.time === 0 && video.paused), 'seeking the current row does not seek another scene');
  await page.locator('#comparison-play').click();
  await page.waitForFunction(() => [...document.querySelectorAll('#comparison-videos tr.selected-case video')].every((video) => !video.paused && video.currentTime > 1.6));
  await page.locator('#comparison-pause').click();
  assert.ok((await videoState()).every((video) => video.paused));
  await page.locator('#comparison-reset').click();
  await page.waitForFunction(() => [...document.querySelectorAll('#comparison-videos tr.selected-case video')].every((video) => !video.seeking && video.currentTime < 0.1));
  await page.locator(`${rowSelector(keys.regression)} .play-row`).click();
  await assertCase(keys.regression);
  await waitDecoded(4, keys.regression);
  assert.ok((await videoState(keys.common)).every((video) => video.paused));
  await page.locator('#comparison-seek').evaluate((input) => { input.value = '1.5'; input.dispatchEvent(new Event('input', { bubbles: true })); });
  await page.locator('#case-filter').selectOption('improvements');
  await assertCase(keys.common);
  assert.ok((await videoState()).every((video) => video.paused), 'filtering away the playing row pauses all remaining players');
  assert.equal(await page.locator('#comparison-position').textContent(), '0.0 s', 'a newly selected row starts from zero');
  await page.locator('#case-filter').selectOption('all');
  await page.locator(`${rowSelector(keys.regression)} .play-row`).click();
  await waitDecoded(4, keys.regression);
  await page.locator('#comparison-seek').evaluate((input) => { input.value = '1.5'; input.dispatchEvent(new Event('input', { bubbles: true })); });
  await chooseGroup('iteration2');
  await assertCase(keys.regression);
  assert.ok((await videoState(keys.regression)).every((video) => video.time >= 1.4), 'choosing another scheme within this direction keeps the same scene and position');
  await chooseGroup('iteration1');
  await page.locator('#comparison-pause').click();
  await selectCase(keys.common);
  assert.match(await page.locator('#comparison-playback-status').textContent(), /动作阶段.*同步/);
  checks.push('six real synthetic MP4 decoders support row play, seek, pause, reset and speed; switching rows pauses the prior row; other scene positions are independent');

  await page.locator('#comparison-seek').evaluate((input) => { input.value = '1.5'; input.dispatchEvent(new Event('input', { bubbles: true })); });
  for (const [camera, name, count] of [['left', 'cam_left_wrist', 5], ['right', 'cam_right_wrist', 5], ['head', 'cam_head', 6]]) {
    const position = Number.parseFloat(await page.locator('#comparison-position').textContent());
    await page.locator('#comparison-camera').selectOption(camera);
    await page.locator('#comparison-play').click();
    await waitDecoded(count);
    await page.locator('#comparison-pause').click();
    assert.equal(await page.locator('#comparison-videos article').count(), 30);
    assert.ok((await videoState(keys.common)).every((video) => video.src.includes(name) && video.speed === 2));
    assert.ok((await videoState(keys.common)).every((video) => video.time >= position - 0.15 && video.time < position + 1), 'camera switching preserves the row recording position');
    if (camera !== 'head') assert.match(await card('iteration2').textContent(), /这个视角尚无录像/);
  }
  await page.evaluate(() => { window.comparisonVideoNodes = [...document.querySelectorAll('#comparison-videos video')]; });
  const catalogRequests = () => apiRequests.filter((endpoint) => !endpoint.endsWith('/media')).length;
  const idleRequests = catalogRequests();
  await page.waitForTimeout(5300);
  assert.equal(catalogRequests(), idleRequests);
  revision += 1; groups[1].name_zh += ` · 更新 ${revision}`; groups[1].revision = `fixture-iteration1-${revision}`;
  await page.evaluate((next) => window.fixtureEvents.emit('changed', { revision: next }), revision);
  await page.waitForFunction((name) => document.querySelector('#group-title').textContent === name, groups[1].name_zh);
  assert.equal(await page.evaluate(() => window.comparisonVideoNodes.every((video) => video.isConnected)), true, 'refresh keeps existing video DOM nodes');
  assert.deepEqual(await gridIds(), directionIds);
  assert.equal(await page.locator('#reference-group').inputValue(), 'base');
  assert.equal(await page.locator('#comparison-camera').inputValue(), 'head');
  checks.push('global camera switches preserve all cells and identify missing cameras; healthy event stream avoids redundant catalogs; refreshed metadata preserves player nodes and selection');

  for (const width of [320, 736, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.evaluate(() => window.scrollTo(0, 0));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, `comparison layout ${width}px`);
    assert.equal(await page.locator('.video-table-scroll').evaluate((table) => table.scrollWidth > table.clientWidth && ['auto', 'scroll'].includes(getComputedStyle(table).overflowX)), true);
    const name = `comparison-${width}.png`; screenshots.push(name);
    await page.screenshot({ path: path.join(output, name), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator(`${rowSelector(keys.regression)} .case-select`).focus(); await page.keyboard.press('Enter'); await assertCase(keys.regression);
  await card('iteration1', keys.regression).scrollIntoViewIfNeeded();
  await page.waitForFunction(() => document.querySelector('article[data-group-id="iteration1"][data-case-key="stack_bowls:2"]')?.textContent.includes('尚无录像'));
  lateMediaAvailable = true;
  const lateMediaRequest = page.waitForResponse((response) => new URL(response.url()).pathname === '/v1/experiments/iter1-b/media');
  await page.locator('#comparison-camera').selectOption('left'); await page.locator('#comparison-play').click(); await lateMediaRequest;
  await waitDecoded(5, keys.regression);
  assert.ok((await videoState(keys.regression)).some((video) => video.src.includes('/iter1-b/')));
  await page.locator('#comparison-pause').click();
  checks.push('keyboard scene selection, late recording publication and 320/736/1440px layouts work; the table owns horizontal scrolling without document overflow');

  await page.goto(`${origin}/?view=compare&scope=custom&group=old-recovery&compare=old-baseline,old-recovery,iteration2,iteration3,iteration4,iteration5&reference=old-baseline&case=${keys.common}`);
  await page.waitForSelector('#comparison-videos article[data-group-id="iteration5"]');
  assert.deepEqual(await gridIds(), directionIds);
  assert.match(await page.locator('#video-grid-title').textContent(), /自选方案/);
  await page.locator('#group-list [data-compare-id="iteration5"]').uncheck();
  assert.equal((await gridIds()).length, 5);
  await page.locator('#group-list [data-compare-id="iteration5"]').check();
  assert.equal((await gridIds()).length, 6, 'manual selection also has no four-column cap');
  const stateURL = page.url(); await page.reload();
  await page.waitForSelector('#comparison-videos article[data-group-id="iteration5"]');
  assert.equal(page.url(), stateURL); assert.deepEqual(await gridIds(), directionIds);
  allowRunDetails = true;
  await card('base').scrollIntoViewIfNeeded();
  await card('base').getByRole('button', { name: '运行详情', exact: true }).click();
  await page.waitForSelector('#detail-content:not([hidden])');
  assert.equal(new URL(page.url()).searchParams.get('view'), 'runs');
  assert.equal(new URL(page.url()).searchParams.get('scope'), 'custom');
  assert.equal(new URL(page.url()).searchParams.get('compare'), directionIds.join(','));
  assert.equal(await page.locator('#detail-title').textContent(), records.get('base-a').title);
  await page.locator('[data-library-view="compare"]').click();
  await page.waitForSelector('#comparison-videos article[data-group-id="iteration5"]');
  assert.equal(new URL(page.url()).searchParams.get('scope'), 'custom');
  assert.deepEqual(await gridIds(), directionIds);
  checks.push('explicit custom scope restores manual selections and aliases, accepts more than four schemes and retains the complete URL after reload');

  await chooseGroup('unknown');
  await page.waitForSelector('#comparison-videos article[data-group-id="unknown"]');
  assert.deepEqual((await gridIds()).sort(), ['fifth', 'unknown']);
  assert.equal(await page.locator('#group-title').textContent(), groups[3].label);
  assert.match(await page.locator('#group-relations').textContent(), /关系待确认/);
  assert.doesNotMatch(await page.locator('#group-relations').textContent(), /本组是基线或无需评分对照|起始方案，无前序迭代/);
  assert.match(await page.locator('#group-metrics').textContent(), /未记录协议预定局数/);
  assert.match(await page.locator('#score-comparison tr[data-group-id="unknown"] [data-metric="success"]').textContent(), /\?/);
  assert.equal(await page.locator('#iteration-list [data-group-id]').count(), 1);
  assert.equal(await page.locator('#iteration-list [data-group-id]').getAttribute('data-group-id'), 'unknown');
  await card('fifth').scrollIntoViewIfNeeded();
  await page.waitForFunction(() => document.querySelector('article[data-group-id="fifth"]')?.textContent.includes('录像无法播放'));
  assert.equal(await card('fifth').locator('a[download]').count(), 1);
  await selectCase(keys.regression); await card('unknown', keys.regression).scrollIntoViewIfNeeded();
  await page.waitForFunction(() => document.querySelector('article[data-group-id="unknown"][data-case-key="stack_bowls:2"]')?.textContent.includes('录像读取失败'));
  failMedia.delete('unknown-b'); await card('unknown', keys.regression).getByRole('button', { name: '重试', exact: true }).click();
  await waitDecoded(1, keys.regression);
  checks.push('same Chinese direction groups independent series together without inventing lineage; unknown denominators remain unknown; media 404 retains download; failed manifests retry');

  delayedMedia = 'unknown-a';
  const pendingManifest = page.waitForRequest((request) => new URL(request.url()).pathname === '/v1/experiments/unknown-a/media');
  await selectCase(keys.common); await page.locator('#comparison-camera').selectOption('left'); await card('unknown').scrollIntoViewIfNeeded(); await pendingManifest;
  await chooseGroup('iteration1');
  await page.waitForSelector('#comparison-videos article[data-group-id="iteration5"]');
  assert.ok(releaseMedia);
  const staleManifest = page.waitForResponse((response) => new URL(response.url()).pathname === '/v1/experiments/unknown-a/media');
  delayedMedia = undefined; releaseMedia(); await staleManifest;
  assert.equal(await page.locator('#comparison-videos article[data-group-id="unknown"]').count(), 0);
  assert.deepEqual(await gridIds(), directionIds);
  checks.push('a media manifest that returns after changing directions cannot replace the current cells or players');

  groups[4].revision += '-retry'; failGroupDetails.add('fifth');
  await page.locator('#refresh-library').click(); await chooseGroup('fifth');
  await page.waitForFunction(() => document.querySelector('#case-empty').textContent.includes('场景读取失败'));
  failGroupDetails.delete('fifth'); delayedGroup = 'fifth';
  const pendingGroup = page.waitForRequest((request) => new URL(request.url()).pathname === '/v1/experiment-groups/fifth');
  await page.locator('#case-empty button').click(); await pendingGroup;
  await chooseGroup('iteration1');
  await page.waitForSelector('#comparison-videos article[data-group-id="iteration5"]');
  assert.ok(releaseGroup);
  const lateGroup = page.waitForResponse((response) => new URL(response.url()).pathname === '/v1/experiment-groups/fifth');
  delayedGroup = undefined; releaseGroup(); await lateGroup;
  assert.equal(new URL(page.url()).searchParams.get('group'), 'iteration1');
  assert.equal(await page.locator('#group-title').textContent(), groups[1].name_zh);
  assert.deepEqual(await gridIds(), directionIds);
  assert.equal(await page.locator('#comparison-videos article').count(), 30);
  await page.evaluate(() => { window.comparisonVideoNodes = [...document.querySelectorAll('#comparison-videos video')]; });
  failGroups = true; await page.locator('#refresh-library').click();
  await page.waitForFunction(() => document.querySelector('#comparison-status').textContent.includes('暂不可用'));
  assert.equal(await page.evaluate(() => window.comparisonVideoNodes.every((video) => video.isConnected)), true);
  failGroups = false;
  checks.push('failed group details retry; stale replies after switching directions cannot change title, URL or grid; temporary catalog failures preserve players');

  await chooseGroup('base');
  assert.equal(await page.locator('#group-iteration').textContent(), '对照方案');
  assert.equal(await page.locator('#group-provenance dt').filter({ hasText: '原始轮次编号' }).locator('+ dd').textContent(), '0');
  assert.match(await page.locator('#group-relations').textContent(), /本组是基线或无需评分对照/);
  assert.match(await page.locator('#group-relations').textContent(), /起始方案，无前序迭代/);
  await page.goto(`${origin}/?view=compare&group=old-recovery&compare=base&reference=base`);
  await page.waitForFunction(() => document.querySelectorAll('#group-provenance .execution-record').length === 2);
  assert.equal(new URL(page.url()).searchParams.get('group'), 'iteration1');
  assert.equal(await page.locator('#score-comparison tbody tr').count(), 1);
  assert.equal(new URL(page.url()).searchParams.get('scope'), 'custom', 'legacy explicit comparisons retain manual selection');
  assert.equal(await page.locator('#group-title').textContent(), groups[1].name_zh);
  groups[3].cases[0].attempts.push(run('unknown-later', 1));
  groups[3].cases[0].run = null; groups[3].cases[0].selection = 'ambiguous'; groups[3].revision += '-ambiguous';
  await page.locator('#refresh-library').click(); await chooseGroup('unknown');
  await page.waitForFunction(() => document.querySelector('article[data-group-id="unknown"] .attempt-history')?.textContent.includes('尚未确认合并与取分规则'));
  assert.doesNotMatch(await card('unknown').locator('.attempt-history').textContent(), /首次正式评分|当前采用/);
  assert.match(await card('unknown').locator('.comparison-video-score').textContent(), /得分 —/);
  assert.equal(await card('unknown').locator('.attempt-history .experiment-time').count(), 2);
  checks.push('baseline iteration zero stays provenance; custom overview loads an unselected active group; ambiguous attempts retain null score and all attempt timestamps');
  await page.goto(`${origin}/?view=compare&scope=custom&group=iteration2&compare=iteration2&reference=iteration2&case=${keys.regression}`);
  await page.waitForSelector('article[data-group-id="iteration2"][data-case-key="stack_bowls:2"]');
  await page.locator('#comparison-play').click();
  await page.waitForFunction(() => document.querySelector('#comparison-playback-status').textContent.includes('这一行暂无可播放录像'));
  assert.equal(await page.locator(`${rowSelector(keys.regression)} video`).count(), 0);
  checks.push('pause cancels row playback while manifests are pending; entering run details preserves custom comparison scope; an empty row reports unavailable playback');
  assert.deepEqual(writes, []); assert.deepEqual(errors, []);
  const report = { status: 'passed', observed_at: new Date().toISOString(), data_origin: 'synthetic_browser_routes_only',
    preview_assets: preview, checks, decoded_media: decoded, api_requests: apiRequests, media_requests: mediaRequests, screenshots, writes, errors,
    limitations: ['Synthetic playback checks browser behavior only; no real benchmark scores, evaluator, native motion, live service or public deployment was exercised.'] };
  await fs.writeFile(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true });
  const report = { status: 'failed', observed_at: new Date().toISOString(), error: error.message, checks, media: await videoState(), writes, errors };
  await fs.writeFile(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2));
  console.error(JSON.stringify(report, null, 2));
  throw error;
} finally {
  holdMedia = false; pendingMedia.splice(0).forEach((release) => release()); releaseGroup?.(); releaseMedia?.();
  await browser.close();
}
