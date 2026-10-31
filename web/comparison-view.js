import { comparisonCases, deltaText, directionGroups, familyKey, formalScore, groupStats, iterationGroups, pairedStats, scoreText } from './comparison-model.js';
import { cameraVideos, localURL } from './experiment-media.js';
import { taskLabel } from './task-labels.js';
import { formatTime, groupTimeText, runTimeText } from './experiment-time.js';

const $ = (id) => document.getElementById(id);
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
};
const button = (text, action, className) => {
  const element = node('button', text, className); element.type = 'button'; element.onclick = action; return element;
};
const signClass = (value) => value === null || value === 0 ? '' : value > 0 ? 'score-up' : 'score-down';
const title = (group) => group?.name_zh || group?.label || group?.method || group?.id || '未记录';
const denominator = (stats) => stats.denominatorKnown ? stats.count : '?';
const option = (value, text) => { const element = node('option', text); element.value = value; return element; };
const roleNames = { baseline: '对照方案', candidate: '实验方案', recovery: '再次验证', benchmark: '完整评测', validation: '功能验证', preparation: '尚未执行' };

export function setupComparisons({ read, openRun, onViewChange }) {
  const initial = new URLSearchParams(location.search);
  let view = initial.get('view') || (initial.has('selected') || initial.has('q') || initial.has('tag') ? 'runs' : 'compare');
  let groups = [], activeId = initial.get('group'), selectedIds = (initial.get('compare') || '').split(',').filter(Boolean);
  let selectionMode = initial.get('scope') === 'custom' || !initial.has('scope') && initial.has('compare') ? 'custom' : 'direction';
  let referenceId = initial.get('reference'), caseKey = initial.get('case'), camera = initial.get('camera') || 'head';
  if (!['head', 'left', 'right'].includes(camera)) camera = 'head';
  let refreshing = false, renderVersion = 0, playbackVersion = 0, caseRows = [], visibleCaseKeys = [], dataErrors = [], sharedTime = 0;
  const cards = new Map(), gridRows = new Map(), detailCache = new Map(), groupDetails = new Map();
  const expandedFamilies = new Map();
  const getGroup = (id) => groups.find((group) => group.id === id);
  const selectedGroups = () => selectedIds.map(getGroup).filter(Boolean);
  const videos = (key) => [...cards.values()].filter((entry) => key === undefined || entry.caseKey === key).map((entry) => entry.player).filter(Boolean);
  const visibility = new IntersectionObserver((entries) => {
    for (const observed of entries) {
      const entry = cards.get(observed.target.dataset.cardKey);
      if (!entry) continue;
      entry.visible = observed.isIntersecting;
      if (entry.visible && view === 'compare' && !document.hidden) loadVideo(entry);
    }
  }, { rootMargin: '120px' });

  function save() {
    const query = new URLSearchParams(location.search);
    for (const [key, value] of Object.entries({ view, scope: selectionMode, group: activeId, compare: selectedIds.join(','), reference: referenceId, case: caseKey, camera })) {
      if (value) query.set(key, value); else query.delete(key);
    }
    history.replaceState(null, '', `${location.pathname}?${query}`);
    sessionStorage.setItem('auriga-library-url', location.pathname + location.search);
  }
  function setView(next, persist = true) {
    renderVersion++;
    view = next === 'runs' ? 'runs' : 'compare';
    $('comparison-workspace').hidden = view !== 'compare'; $('runs-workspace').hidden = view !== 'runs';
    for (const tab of document.querySelectorAll('[data-library-view]')) tab.setAttribute('aria-pressed', String(tab.dataset.libraryView === view));
    if (view !== 'compare') pause();
    else document.querySelectorAll('#detail-media video').forEach((video) => video.pause());
    if (persist) { save(); onViewChange?.(); }
    window.dispatchEvent(new Event('resize'));
  }
  for (const tab of document.querySelectorAll('[data-library-view]')) tab.onclick = () => setView(tab.dataset.libraryView);
  function chooseGroup(id) {
    const group = getGroup(id); if (!group) return;
    activeId = id;
    expandedFamilies.set(familyKey(group), true);
    const baseline = getGroup(group.baseline_group_id);
    selectedIds = directionGroups(group, groups).map((item) => item.id); selectionMode = 'direction';
    referenceId = baseline?.id || id;
    render(); save();
  }
  function toggleGroup(id, checked) {
    selectionMode = 'custom';
    if (checked) selectedIds.push(id); else selectedIds = selectedIds.filter((value) => value !== id);
    if (!selectedIds.length) selectedIds = [activeId];
    if (!selectedIds.includes(referenceId)) referenceId = selectedIds[0];
    render(); save();
  }
  function renderSidebar() {
    const search = $('group-search').value.trim().toLowerCase(), benchmark = $('benchmark-filter').value;
    const visible = groups.filter((group) => (!benchmark || group.benchmark_group === benchmark)
      && [group.name_zh, group.label, group.purpose, ...(group.experiment_nature || []), group.method, group.series, group.series_name_zh, group.source?.branch, group.id].join(' ').toLowerCase().includes(search));
    $('group-count').textContent = `${visible.length} / ${groups.length}`;
    const fragment = document.createDocumentFragment(), families = new Map();
    for (const group of visible) {
      const family = familyKey(group);
      if (!families.has(family)) families.set(family, []);
      families.get(family).push(group);
    }
    const ordered = [...families].sort(([a], [b]) => (a === familyKey(getGroup(activeId)) ? -1 : b === familyKey(getGroup(activeId)) ? 1 : a === '其他独立实验' ? 1 : b === '其他独立实验' ? -1 : a.localeCompare(b, 'zh-CN')));
    for (const [family, members] of ordered) {
      const section = node('details', undefined, 'group-family');
      section.open = Boolean(search) || (expandedFamilies.get(family) ?? members.some((item) => selectedIds.includes(item.id)));
      section.ontoggle = () => { if (section.isConnected && !search) expandedFamilies.set(family, section.open); };
      const summary = node('summary');
      summary.append(node('span', family), node('span', `${members.length} 组`, 'group-family-count'));
      section.append(summary, button('对比整个方向', () => chooseGroup(members.find((item) => item.role !== 'baseline')?.id || members[0].id), 'direction-select'));
      const orderedMembers = [...members].sort((a, b) => Number(a.role === 'baseline') - Number(b.role === 'baseline')
        || (a.iteration ?? 999) - (b.iteration ?? 999) || title(a).localeCompare(title(b), 'zh-CN'));
      let category;
      for (const group of orderedMembers) {
      const nextCategory = group.role === 'baseline' ? '对照方案' : '实验方案';
      if (nextCategory !== category) { section.append(node('p', nextCategory, 'group-category')); category = nextCategory; }
      const item = node('div', undefined, `group-choice${group.id === activeId ? ' active' : ''}`);
      const select = button('', () => chooseGroup(group.id), 'group-select'); select.dataset.groupId = group.id;
      select.setAttribute('aria-pressed', String(group.id === activeId));
      const stats = groupStats(group);
      select.append(node('span', title(group), 'group-name'), node('span', roleNames[group.role] || '独立实验', 'group-caption'),
        node('span', `均分 ${scoreText(stats.mean)} · 已评分 ${stats.scored}/${stats.count}`, 'group-caption'),
        node('span', group.started_at ? `开始 ${formatTime(group.started_at)}` : '开始时间未记录', 'group-caption'));
      const check = node('input'); check.type = 'checkbox'; check.checked = selectedIds.includes(group.id); check.dataset.compareId = group.id;
      check.setAttribute('aria-label', `加入对比：${title(group)}`); check.onchange = () => toggleGroup(group.id, check.checked);
      item.append(select, check); section.append(item);
      }
      fragment.append(section);
    }
    if (!visible.length) fragment.append(node('p', '没有匹配的实验组。', 'muted small'));
    $('group-list').replaceChildren(fragment);
  }
  function relation(label, id, missing) {
    const block = node('div'); block.append(node('span', `${label} `, 'muted'));
    const group = getGroup(id);
    block.append(group ? button(title(group), () => chooseGroup(id), 'text-button') : node('span', missing));
    return block;
  }
  function renderOverview(group) {
    const missingRelation = (kind) => group.relation_status?.[kind] === 'not_applicable' ? (kind === 'baseline' ? '本组是基线或无需评分对照' : '起始方案，无前序迭代') : '关系待确认';
    $('group-title').textContent = title(group); $('group-protocol').textContent = group.series_name_zh || '独立实验';
    $('group-iteration').textContent = roleNames[group.role] || '独立实验';
    $('group-relations').replaceChildren(relation('对照方案（Baseline）', group.baseline_group_id, missingRelation('baseline')), relation('上一版方案', group.parent_group_id, missingRelation('parent')));
    if (group.recovery_of_group_id) $('group-relations').append(relation('补跑来源', group.recovery_of_group_id, '原始运行组未入库'));
    if (group.execution_groups?.length > 1) $('group-relations').append(node('p', `准备和重跑已收进本方案，共 ${group.execution_groups.length} 条执行记录。每个场景保留首次正式得分，所有尝试都可查看。`, 'merged-execution-note'));
    else if (group.scope === 'recovery_subset') $('group-relations').append(node('p', `本次验证 ${group.case_count} 个场景，原计划 ${group.protocol_case_count} 个；实验条件与来源见下方详情。`, 'muted small'));
    if (group.purpose) $('group-relations').prepend(node('p', group.purpose));
    $('group-relations').append(node('p', `${groupTimeText(group)} · 北京时间（UTC+8）`, 'experiment-time'));
    if (group.experiment_nature?.length) $('group-relations').append(node('p', `实验性质：${group.experiment_nature.join(' · ')}`, 'muted small'));
    const stats = groupStats(group), baseline = getGroup(group.baseline_group_id), paired = baseline ? pairedStats(group, baseline) : null;
    $('group-metrics').replaceChildren(...[
      ['整组均分 / 100', scoreText(stats.mean), `${stats.scored} 局有正式评分`],
      ['相对 baseline', paired ? deltaText(paired.delta) : '—', paired ? `${paired.scored} 个场景有双方评分` : '尚无已确认的 baseline'],
      ['任务成功', `${stats.successes} / ${denominator(stats)}`, stats.denominatorKnown && stats.count ? `${(stats.successes / stats.count * 100).toFixed(1)}% · 固定分母` : '未记录协议预定局数'],
      ['待评 / 未评分', String(stats.unknown), stats.denominatorKnown ? '保留在预定局数中' : `已发现 ${stats.count} 个场景`],
    ].map(([label, value, hint], index) => {
      const card = node('div', undefined, 'metric-card'); card.append(node('span', label), node('strong', value, index === 1 ? signClass(paired?.delta ?? null) : ''), node('small', hint)); return card;
    }));
    const details = $('group-provenance').querySelector('div'), list = node('dl', undefined, 'provenance-grid');
    const fields = [['评测协议', group.benchmark_group], ['方案', group.method], ['原始轮次编号', group.iteration], ['首次执行分支', group.source?.branch], ['首次执行 Worktree', group.source?.worktree || group.source?.worktrees?.join('；')], ['首次执行 Revision', group.source?.revision], ['来源记录', group.source?.manifest]];
    const conditionLabels = { model: '模型', reasoning_effort: '推理档位', observation_profile: '观察条件', provider: '供应商', max_control_steps: '控制步数上限', control_dt: '控制周期' };
    for (const [key, value] of Object.entries(group.conditions || {})) fields.push([conditionLabels[key] || key, typeof value === 'object' ? JSON.stringify(value) : value]);
    for (const [label, value] of fields) list.append(node('dt', label), node('dd', value === null || value === undefined || value === '' ? '未记录' : String(value)));
    details.replaceChildren(list);
    if (group.baseline_reason) details.prepend(node('p', `对照依据：${group.baseline_reason}`, 'muted small'));
    if (group.note) details.append(node('p', group.note, 'muted small'));
    if (group.execution_groups?.length) {
      details.append(node('h3', '执行记录'));
      group.execution_groups.forEach((execution, index) => {
        const record = node('details', undefined, 'execution-record');
        record.append(node('summary', `第 ${index + 1} 条执行记录 · ${execution.started_at ? formatTime(execution.started_at) : '开始时间未记录'}`));
        const values = node('dl', undefined, 'provenance-grid');
        for (const [label, value] of [['原始名称', title(execution)], ['清单', execution.manifest], ['分支', execution.source?.branch], ['Worktree', execution.source?.worktree || execution.source?.worktrees?.join('；')], ['Revision', execution.source?.revision]]) {
          values.append(node('dt', label), node('dd', value || '未记录'));
        }
        for (const [key, value] of Object.entries(execution.conditions || {})) values.append(node('dt', conditionLabels[key] || key), node('dd', value === null ? '未记录' : String(value)));
        record.append(node('p', groupTimeText(execution), 'experiment-time'), values); details.append(record);
      });
    }
    for (const evidence of group.relationship_evidence || []) details.append(node('p', `关系依据：${evidence}`, 'muted small'));
    for (const warning of group.warnings || []) details.append(node('p', warning, 'comparison-warning'));
  }
  function renderIterations(group) {
    const iterations = iterationGroups(group, groups);
    $('iteration-list').replaceChildren(...iterations.map((item) => {
      const stats = groupStats(item), parent = getGroup(item.parent_group_id), paired = parent ? pairedStats(item, parent) : null;
      const card = button('', () => chooseGroup(item.id), `iteration-card${item.id === activeId ? ' current' : ''}`);
      card.dataset.groupId = item.id; card.setAttribute('aria-pressed', String(item.id === activeId));
      card.append(node('span', roleNames[item.role] || '实验方案', 'iteration-label'), node('strong', title(item)), node('span', `均分 ${scoreText(stats.mean)} · ${stats.scored}/${stats.count} 已评分`),
        node('span', item.recovery_of_group_id ? '同轮补跑，不计为新迭代' : paired ? `同场景较上一版 ${deltaText(paired.delta)} · ${paired.scored} 对` : item.relation_status?.parent === 'not_applicable' ? '起始方案' : '前序关系未记录', paired && !item.recovery_of_group_id ? signClass(paired.delta) : 'muted'));
      return card;
    }));
  }
  function renderScores(selected) {
    const reference = getGroup(referenceId);
    $('reference-group').replaceChildren(...selected.map((group) => option(group.id, title(group)))); $('reference-group').value = referenceId;
    const fragment = document.createDocumentFragment();
    for (const group of selected) {
      const stats = groupStats(group), paired = pairedStats(group, reference), row = node('tr'); row.dataset.groupId = group.id;
      const name = node('th'); name.scope = 'row'; name.append(node('span', title(group)));
      if (group.id === referenceId) name.append(node('small', '比较基准', 'reference-tag'));
      else if (group.id === getGroup(activeId)?.baseline_group_id) name.append(node('small', '已记录 baseline', 'reference-tag'));
      row.append(name);
      for (const [metric, value, css] of [
        ['mean', scoreText(stats.mean)], ['success', `${stats.successes} / ${denominator(stats)}`], ['scored', `${stats.scored} / ${denominator(stats)}`],
        ['paired-count', group.id === referenceId ? '—' : `${paired.scored} / ${paired.matched}`],
        ['paired-delta', group.id === referenceId ? '基准' : deltaText(paired.delta), signClass(paired.delta)],
      ]) { const cell = node('td', value, css); cell.dataset.metric = metric; row.append(cell); }
      fragment.append(row);
    }
    $('score-comparison').querySelector('tbody').replaceChildren(fragment);
    const mismatch = selected.some((group) => group.id !== referenceId && !pairedStats(group, reference).same_cases);
    $('comparison-notes').textContent = `${mismatch ? '场景集合不完全相同；配对列只使用交集。' : ''}“配对场景”为有双方评分数 / 同场景数。各组完整均分的评分覆盖可能不同；分差不单独证明某项改动的效果。`;
  }
  function selectCase(key, scroll = false) {
    const changed = caseKey !== key;
    if (changed) { pause(); sharedTime = 0; }
    caseKey = key;
    if (changed) seek(0);
    for (const [id, row] of gridRows) {
      row.classList.toggle('selected-case', id === caseKey);
      row.querySelector('.case-select').setAttribute('aria-pressed', String(id === caseKey));
    }
    const row = caseRows.find((item) => item.key === caseKey), current = visibleCaseKeys.indexOf(caseKey);
    $('comparison-prev-case').disabled = current <= 0;
    $('comparison-next-case').disabled = current < 0 || current >= visibleCaseKeys.length - 1;
    $('comparison-case-title').textContent = row ? `当前同步行：${taskLabel(row.task)}` : '选择一行同步播放';
    $('comparison-case-meta').textContent = row ? `${row.variant ?? '?'} · seed ${row.eval_seed ?? '?'} · layout ${row.layout_id ?? '?'} · ${selectedIds.length} 个方案` : '';
    $('comparison-position').textContent = `${sharedTime.toFixed(1)} s`;
    if (scroll) gridRows.get(key)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    updatePlayback(); save();
  }
  function renderCases() {
    const selected = selectedGroups();
    if (selected.some((group) => group.details_loaded === false)) return;
    caseRows = comparisonCases(selected);
    const search = $('case-search').value.trim().toLowerCase(), filter = $('case-filter').value, referenceIndex = selectedIds.indexOf(referenceId);
    const visible = caseRows.filter((row) => {
      if (!`${row.task} ${taskLabel(row.task)}`.toLowerCase().includes(search)) return false;
      const scores = row.members.map((item) => formalScore(item?.run?.summary)), base = scores[referenceIndex];
      if (filter === 'unscored') return scores.some((value) => value === null);
      if (filter === 'regressions' || filter === 'improvements') return base !== null && scores.some((value, i) => i !== referenceIndex && value !== null && (filter === 'regressions' ? value < base : value > base));
      return true;
    });
    visibleCaseKeys = visible.map((row) => row.key);
    const nextCaseKey = visibleCaseKeys.includes(caseKey) ? caseKey : visibleCaseKeys[0];
    const heading = node('tr'); heading.append(node('th', '场景 / 同步播放'));
    for (const group of selected) {
      const cell = node('th', title(group));
      if (group.role === 'baseline') cell.append(node('small', '对照方案', 'reference-tag'));
      cell.append(node('small', group.started_at ? `最早开始 ${formatTime(group.started_at)}` : '开始时间未记录', 'experiment-time'));
      heading.append(cell);
    }
    for (const cell of heading.children) cell.scope = 'col';
    $('case-table').querySelector('thead').replaceChildren(heading);
    const body = $('comparison-videos'), keep = new Set();
    visible.forEach((row, rowIndex) => {
      let tr = gridRows.get(row.key);
      if (!tr) {
        tr = node('tr'); tr.dataset.caseKey = row.key; gridRows.set(row.key, tr);
        const heading = node('th'); heading.scope = 'row';
        const select = button('', () => selectCase(row.key), 'case-select'); select.dataset.caseKey = row.key;
        select.append(node('span', taskLabel(row.task)), node('small', `${row.variant ?? '?'} · seed ${row.eval_seed ?? '?'} · layout ${row.layout_id ?? '?'}`));
        const play = button('播放这一行', () => playRow(row.key), 'play-row'); play.setAttribute('aria-label', `一起播放 ${taskLabel(row.task)} seed ${row.eval_seed} layout ${row.layout_id}`);
        heading.append(select, play); tr.append(heading);
      }
      if (body.children[rowIndex] !== tr) body.insertBefore(tr, body.children[rowIndex] || null);
      const base = formalScore(row.members[referenceIndex]?.run?.summary);
      selected.forEach((group, column) => {
        const key = JSON.stringify([row.key, group.id]); keep.add(key);
        let entry = cards.get(key);
        if (!entry) {
          entry = makeCard(group); entry.caseKey = row.key; entry.cell = node('td'); entry.cell.append(entry.node);
          entry.node.dataset.cardKey = key; entry.node.dataset.caseKey = row.key; cards.set(key, entry); visibility.observe(entry.node);
        }
        if (tr.children[column + 1] !== entry.cell) tr.insertBefore(entry.cell, tr.children[column + 1] || null);
        entry.group = group; entry.item = row.members[column];
        const run = entry.item?.run, score = formalScore(run?.summary);
        entry.heading.textContent = title(group);
        entry.score.textContent = `得分 ${scoreText(score)} / 100${score !== null && base !== null && column !== referenceIndex ? ` · 较基准 ${deltaText(score - base)}` : ''}`;
        entry.time.textContent = runTimeText(run);
        entry.time.title = [run?.started_at_source, run?.finished_at_source, ...(run?.time_warnings || [])].filter(Boolean).join('；');
        renderAttempts(entry, entry.item);
        const identity = `${run?.id || ''}:${camera}`;
        if (entry.identity !== identity) {
          entry.request = (entry.request || 0) + 1; entry.loading = null;
          removePlayer(entry); entry.source = undefined; entry.identity = identity;
          entry.content.replaceChildren(node('p', run ? '滚动到此处加载录像' : entry.item?.selection === 'ambiguous' ? '多次尝试尚未确定采用哪一局' : '该方案没有对应运行', 'video-empty'));
          entry.status.textContent = ''; entry.links.replaceChildren();
        }
      });
    });
    for (const [key, entry] of cards) if (!keep.has(key)) {
      visibility.unobserve(entry.node); removePlayer(entry); entry.cell.remove(); cards.delete(key);
    }
    for (const [key, row] of gridRows) if (!visibleCaseKeys.includes(key)) { row.remove(); gridRows.delete(key); }
    $('case-empty').hidden = visible.length > 0; $('case-empty').textContent = '没有符合条件的场景。';
    $('case-count').textContent = `${visible.length} / ${caseRows.length} 个场景 × ${selected.length} 个方案 · 全部列出`;
    $('video-grid-title').textContent = selectionMode === 'direction' ? `${familyKey(getGroup(activeId))} · 整组视频表` : '自选方案 · 视频对比表';
    selectCase(nextCaseKey); updateVideos();
  }
  async function loadGroup(group) {
    let cached = groupDetails.get(group.id);
    if (!cached || cached.revision !== group.revision) {
      cached = { revision: group.revision }; groupDetails.set(group.id, cached);
      cached.pending = read(`/v1/experiment-groups/${encodeURIComponent(group.id)}`).then((value) => {
        cached.value = value; cached.revision = value.revision; return value;
      }).catch((error) => { if (groupDetails.get(group.id) === cached) groupDetails.delete(group.id); throw error; });
    }
    return cached.value || cached.pending;
  }
  async function render() {
    const version = ++renderVersion;
    if (view !== 'compare') return;
    const group = getGroup(activeId); if (!group) return;
    renderSidebar(); renderOverview(group); renderIterations(group); renderScores(selectedGroups());
    $('comparison-content').hidden = false;
    const pending = [...new Set([group, ...selectedGroups()])].filter((item) => item.details_loaded === false);
    if (pending.length) {
      pause(); caseRows = []; visibleCaseKeys = [];
      $('case-table').hidden = true; $('comparison-videos').hidden = true;
      $('case-empty').hidden = false; $('case-empty').textContent = '正在读取所选实验组的场景…';
      $('case-count').textContent = '正在读取场景';
      try {
        const loaded = await Promise.all(pending.map(loadGroup));
        if (version !== renderVersion || view !== 'compare') return;
        const byId = new Map(loaded.map((item) => [item.id, item]));
        groups = groups.map((item) => byId.get(item.id) || item);
        renderOverview(getGroup(activeId)); renderScores(selectedGroups());
      } catch (error) {
        if (version !== renderVersion || view !== 'compare') return;
        $('case-empty').replaceChildren(node('span', `场景读取失败：${error.message} `), button('重试', render, 'text-button'));
        return;
      }
    }
    $('case-table').hidden = false; $('comparison-videos').hidden = false;
    renderCases();
  }
  function pause() { playbackVersion++; for (const player of videos()) player.pause(); }
  function seek(value) {
    sharedTime = value;
    for (const player of videos(caseKey)) if (Number.isFinite(player.duration)) player.currentTime = Math.min(value, player.duration);
    $('comparison-seek').value = value; $('comparison-position').textContent = `${Number(value).toFixed(1)} s`;
  }
  function updatePlayback() {
    const durations = videos(caseKey).map((video) => video.duration).filter(Number.isFinite);
    $('comparison-seek').max = durations.length ? Math.max(...durations) : sharedTime; $('comparison-seek').disabled = !durations.length;
    $('comparison-play').disabled = !caseKey;
  }
  function removePlayer(entry) {
    if (entry.player) { entry.player.pause(); entry.player.removeAttribute('src'); entry.player.load(); entry.player = null; }
  }
  function makeCard(group) {
    const article = node('article', undefined, 'comparison-video-card'); article.dataset.groupId = group.id;
    const heading = node('h3', title(group)), score = node('p', '', 'comparison-video-score'), content = node('div', undefined, 'comparison-video-stage');
    const time = node('p', '', 'experiment-time');
    const status = node('p', '', 'muted small'); status.setAttribute('role', 'status');
    const links = node('div', undefined, 'comparison-video-links'), attempts = node('details', undefined, 'attempt-history');
    attempts.hidden = true; article.append(heading, score, time, content, status, links, attempts);
    return { node: article, heading, score, time, content, status, links, attempts, player: null, source: undefined };
  }
  function renderAttempts(entry, item) {
    const key = JSON.stringify([item?.run?.id, item?.selection, (item?.attempts || []).map((attempt) => [attempt.id, formalScore(attempt.summary), attempt.started_at, attempt.finished_at])]);
    if (entry.attemptKey === key) return;
    entry.attemptKey = key;
    entry.attempts.hidden = (item?.attempts?.length || 0) < 2;
    entry.attempts.replaceChildren(node('summary', `查看全部 ${item?.attempts?.length || 0} 次尝试`));
    const explanation = item?.selection === 'ambiguous' ? '这些尝试尚未确认合并与取分规则，因此暂不计入分数。可逐次查看原始记录。'
      : item?.selection_reason || '按登记顺序保留首次正式评分（包括 0 分）；只有此前未评分，才使用补跑结果。全部未评分时展示最后一次尝试。';
    entry.attempts.append(node('p', explanation, 'muted small'));
    for (const [index, attempt] of (item?.attempts || []).entries()) {
      const score = formalScore(attempt.summary), current = attempt.id === item?.run?.id;
      const line = node('p');
      line.append(button(`第 ${index + 1} 次：${score === null ? '未评分' : `${scoreText(score)} 分`}${current ? ' · 当前采用' : ''}`,
        () => { setView('runs'); openRun(attempt.id); }, 'text-button'));
      line.querySelector('button').dataset.attemptId = attempt.id;
      line.append(node('span', runTimeText(attempt), 'experiment-time'));
      entry.attempts.append(line);
    }
  }
  async function detail(run) {
    let cached = detailCache.get(run.id);
    if (!cached) {
      cached = { value: read(`/v1/experiments/${encodeURIComponent(run.id)}/media`) }; detailCache.set(run.id, cached);
      const clear = () => { if (detailCache.get(run.id) === cached) detailCache.delete(run.id); };
      cached.value.then(clear, clear);
    }
    return cached.value;
  }
  async function loadVideo(entry) {
    const run = entry.item?.run;
    if (!run || !entry.node.isConnected || view !== 'compare') return;
    if (entry.loading) return entry.loading;
    const request = ++entry.request, identity = entry.identity, group = entry.group;
    const current = () => entry.request === request && entry.identity === identity && entry.node.isConnected && view === 'compare';
    entry.loading = (async () => {
      try {
        const record = await detail(run); if (!current()) return;
        const cameras = cameraVideos(record.media || [], record.reports || []), recording = cameras[camera], source = localURL(recording?.url);
        if (entry.source !== source) {
          removePlayer(entry); entry.source = source; entry.content.replaceChildren();
          if (source) {
            const player = node('video'); player.controls = true; player.playsInline = true; player.preload = 'metadata'; player.muted = true;
            player.setAttribute('aria-label', `${title(group)} · ${$('comparison-camera').selectedOptions[0].textContent}录像`);
            player.defaultPlaybackRate = Number($('comparison-speed').value); player.playbackRate = player.defaultPlaybackRate;
            player.onloadedmetadata = () => {
              player.playbackRate = Number($('comparison-speed').value);
              if (entry.caseKey === caseKey) player.currentTime = Math.min(sharedTime, player.duration);
              entry.status.textContent = `${player.videoWidth}×${player.videoHeight} · ${player.duration.toFixed(1)} 秒`; updatePlayback();
            };
            player.onerror = () => { entry.status.textContent = '录像无法播放；可下载原文件，或打开完整回放。'; updatePlayback(); };
            player.onplay = () => { if (entry.caseKey !== caseKey) { selectCase(entry.caseKey); player.play().catch(() => {}); } };
            player.ontimeupdate = () => {
              const lead = videos(caseKey).find((video) => !video.paused);
              if (entry.caseKey === caseKey && lead === player && document.activeElement !== $('comparison-seek')) {
                sharedTime = player.currentTime; $('comparison-seek').value = sharedTime; $('comparison-position').textContent = `${sharedTime.toFixed(1)} s`;
              }
            };
            entry.player = player; entry.content.append(player); player.src = source;
            entry.status.textContent = '正在读取录像信息…';
          } else {
            entry.content.append(node('p', '这个视角尚无录像', 'video-empty')); entry.status.textContent = '可切换视角，或打开运行详情查看证据。';
          }
        }
        const links = [button('运行详情', () => { setView('runs'); openRun(run.id); }, 'text-button')];
        const replay = localURL(record.view_url || run.view_url);
        if (replay && record.replay_available !== false) { const a = node('a', '完整回放 ↗'); a.href = replay; a.target = '_blank'; a.rel = 'noopener'; links.push(a); }
        if (source) { const a = node('a', '下载录像'); a.href = source; a.download = recording.name?.split('/').at(-1) || 'recording.mp4'; links.push(a); }
        entry.links.replaceChildren(...links);
      } catch (error) {
        if (!current()) return;
        entry.status.textContent = `录像读取失败：${error.message}`;
        entry.links.replaceChildren(button('重试', () => { detailCache.delete(run.id); loadVideo(entry); }, 'text-button'));
      } finally { if (entry.request === request) entry.loading = null; }
    })();
    return entry.loading;
  }
  function updateVideos() {
    if (view !== 'compare' || document.hidden) return;
    for (const entry of cards.values()) if (entry.visible || entry.player && !entry.player.paused) loadVideo(entry);
  }
  async function playRow(key) {
    pause();
    selectCase(key);
    const version = renderVersion, playback = playbackVersion;
    await Promise.all([...cards.values()].filter((entry) => entry.caseKey === key).map(loadVideo));
    if (version !== renderVersion || playback !== playbackVersion || caseKey !== key || view !== 'compare' || document.hidden) return;
    if (!videos(key).length) { $('comparison-playback-status').textContent = '这一行暂无可播放录像，可切换视角或查看运行详情。'; return; }
    seek(sharedTime);
    const result = await Promise.allSettled(videos(key).map((video) => video.play()));
    $('comparison-playback-status').textContent = result.some((item) => item.status === 'rejected')
      ? '部分录像未能播放，可用各播放器的播放按钮重试。' : '同步播放这一行；按录像起点对齐，不同方案的动作阶段不一定同步。';
  }
  async function refresh() {
    if (refreshing) return; refreshing = true;
    try {
      const data = await read('/v1/experiment-groups?view=summary'); dataErrors = data.errors || [];
      groups = (data.groups || []).map((group) => {
        const cached = groupDetails.get(group.id);
        return cached?.value && cached.revision === group.revision ? cached.value : group;
      });
      const available = new Set(groups.map((group) => group.id));
      const aliases = new Map(groups.flatMap((group) => (group.id_aliases || []).map((id) => [id, group.id])));
      const canonicalId = (id) => aliases.get(id) || id;
      activeId = canonicalId(activeId); referenceId = canonicalId(referenceId);
      selectedIds = [...new Set(selectedIds.map(canonicalId))];
      for (const id of groupDetails.keys()) if (!available.has(id)) groupDetails.delete(id);
      selectedIds = selectedIds.filter((id) => available.has(id));
      if (!available.has(activeId)) activeId = groups.find((group) => group.series && group.iteration !== null && group.baseline_group_id)?.id
        || groups.find((group) => group.baseline_group_id)?.id || groups[0]?.id;
      if ((selectionMode === 'direction' || !selectedIds.length) && activeId) {
        selectedIds = directionGroups(getGroup(activeId), groups).map((item) => item.id);
        if (!selectedIds.includes(referenceId)) referenceId = getGroup(getGroup(activeId)?.baseline_group_id)?.id || selectedIds[0];
      }
      if (!selectedIds.includes(referenceId)) referenceId = selectedIds[0];
      const benchmarks = [...new Set(groups.map((group) => group.benchmark_group).filter(Boolean))].sort();
      const previous = $('benchmark-filter').value;
      if (JSON.stringify(benchmarks) !== $('benchmark-filter').dataset.values) {
        $('benchmark-filter').replaceChildren(option('', '全部协议'), ...benchmarks.map((value) => option(value, value)));
        $('benchmark-filter').value = previous; $('benchmark-filter').dataset.values = JSON.stringify(benchmarks);
      }
      $('comparison-status').textContent = dataErrors.length ? `${dataErrors.length} 条分组信息读取异常；已读取的组仍可查看。`
        : groups.length ? `${groups.length} 个实验组 · ${data.ungrouped_count ?? 0} 条运行未纳入分数对比，可在“全部运行”查看` : '尚无可识别的实验组；请在“全部运行”查看原始记录。';
      await render(); save();
      if (!groups.length) { $('comparison-content').hidden = true; renderSidebar(); }
      return data;
    } catch (error) {
      $('comparison-status').textContent = `实验组暂不可用：${error.message}。仍可切换“全部运行”查看记录。`;
    } finally { refreshing = false; }
  }
  $('group-search').oninput = renderSidebar; $('benchmark-filter').onchange = renderSidebar;
  $('reference-group').onchange = () => { referenceId = $('reference-group').value; renderScores(selectedGroups()); renderCases(); save(); };
  $('case-search').oninput = renderCases; $('case-filter').onchange = renderCases;
  $('comparison-camera').value = camera;
  for (const [id, step] of [['comparison-prev-case', -1], ['comparison-next-case', 1]]) $(id).onclick = () => {
    const next = visibleCaseKeys[visibleCaseKeys.indexOf(caseKey) + step];
    if (next) selectCase(next, true);
  };
  $('comparison-camera').onchange = () => { camera = $('comparison-camera').value; detailCache.clear(); pause(); renderCases(); save(); };
  $('comparison-play').onclick = () => playRow(caseKey);
  $('comparison-pause').onclick = pause; $('comparison-reset').onclick = () => { pause(); seek(0); };
  $('comparison-seek').oninput = () => seek(Number($('comparison-seek').value));
  $('comparison-speed').onchange = () => videos().forEach((video) => { video.defaultPlaybackRate = Number($('comparison-speed').value); video.playbackRate = video.defaultPlaybackRate; });
  window.addEventListener('pagehide', pause);
  document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); else updateVideos(); });
  // Video exports can arrive after the catalog's final event; revalidate only visible media manifests.
  setInterval(() => { if (!document.hidden && view === 'compare') { detailCache.clear(); updateVideos(); } }, 5000);
  setView(view, false);
  return { refresh, isActive: () => view === 'compare' };
}
