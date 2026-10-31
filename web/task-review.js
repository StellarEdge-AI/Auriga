const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
};
const number = (value) => typeof value === 'number' && Number.isFinite(value) ? String(Number(value.toFixed(6))) : '未记录';
const labels = { scored: '已评测', unscored: '未完成正式评测', unknown: '评测状态未确认', partial: '仅保存部分评测结果', pending: '等待正式评测', unavailable: '未保存可用评测结果' };
function disclosure(title, value) {
  const details = node('details');
  details.append(node('summary', title), node('pre', typeof value === 'string' ? value : JSON.stringify(value, null, 2)));
  return details;
}
function section(title) {
  const result = node('section', undefined, 'task-review-section'); result.append(node('h3', title)); return result;
}
function sourceLink(source) {
  if (typeof source.url !== 'string') return null;
  try {
    const url = new URL(source.url, location.href);
    if (url.origin !== location.origin || !['http:', 'https:'].includes(url.protocol)) return null;
    const link = node('a', source.label || source.path || '查看原始证据'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener'; return link;
  } catch { return null; }
}
export function renderTaskReview(container, review, message) {
  const signature = JSON.stringify([review, message]);
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;
  container.replaceChildren(node('h2', '任务要求与评分'));
  if (!review) {
    container.append(node('p', message || '此运行尚未提供可核对的任务与评分记录。', 'meta'));
    return;
  }
  container.dataset.taskId = review.task_id || '';
  const description = section('官方任务描述');
  description.append(node('p', review.description?.text || '未保存官方任务说明。', 'task-description'));
  const kind = { recorded_instruction: '本次运行保存的官方任务指令', official_template: '官方任务指令模板', recorded_observation: '本次运行保存的官方任务指令', installed_task_source: '当前安装版本的官方任务指令模板' }[review.description?.source_kind];
  if (kind) description.append(node('p', kind, 'meta'));
  container.append(description);

  const scoring = review.scoring || {}, criteria = section('每一个得分点');
  if (scoring.rule_source?.status !== 'matched' && scoring.rule_source?.message) criteria.append(node('p', scoring.rule_source.message, 'score-evidence-note'));
  if (scoring.aggregation) criteria.append(node('p', scoring.aggregation, 'task-aggregation'));
  const list = node('ol', undefined, 'score-criteria');
  for (const point of scoring.criteria || []) {
    const row = node('li'); row.dataset.criterionId = point.id || '';
    const heading = node('div', undefined, 'score-criterion-heading');
    heading.append(node('strong', point.label || point.id || '评分项'));
    if (point.score_label || typeof point.score === 'number') heading.append(node('span', point.score_label || `${number(point.score)} 分`, 'score-value'));
    row.append(heading);
    if (point.description) row.append(node('p', point.description));
    if (point.variant) row.append(node('p', `适用：${point.variant}`, 'meta'));
    row.append(node('p', point.recorded_result == null ? '本次命中情况：未保存此得分点的单独判定。' : `本次记录：${typeof point.recorded_result === 'object' ? JSON.stringify(point.recorded_result) : point.recorded_result}`, 'criterion-result'));
    list.append(row);
  }
  if (list.children.length) criteria.append(list);
  else criteria.append(node('p', '没有可核对的分项评分规则。', 'meta'));
  if (scoring.completion_conditions?.length) {
    criteria.append(node('h4', '任务成功条件'));
    const conditions = node('ul'); for (const condition of scoring.completion_conditions) conditions.append(node('li', condition)); criteria.append(conditions);
  }
  container.append(criteria);

  const result = review.result || {}, actual = section('我们的分数 · 如何得到');
  actual.dataset.resultStatus = result.status || 'unknown';
  const summary = node('div', undefined, 'task-score-summary');
  summary.append(node('strong', labels[result.status] || result.status || '评测状态未确认'));
  const snapshotLabel = ['unscored', 'partial'].includes(result.status) ? ' · 局部记录' : '';
  const values = [
    ['归一化得分（0–1）', result.status === 'unscored' ? (result.task_success === false ? '不可用' : '不计分') : number(result.normalized_score)],
    [`原生过程分（0–${scoring.max_score ?? 100}）${snapshotLabel}`, scoring.mode === 'binary_success' && result.native_process_score == null ? '无独立过程分' : number(result.native_process_score)],
    [`原生奖励（native_reward）${snapshotLabel}`, number(result.native_reward)],
    ['任务成功', result.task_success === true ? '是' : result.task_success === false ? '否' : '未确认'],
  ];
  if (result.failure_reason?.label) values.push(['失败原因', result.failure_reason.label]);
  const dl = node('dl'); for (const [key, value] of values) dl.append(node('dt', key), node('dd', value)); summary.append(dl); actual.append(summary);
  for (const explanation of result.explanation || []) actual.append(node('p', explanation, 'score-explanation'));
  if (result.breakdown_status === 'not_recorded') actual.append(node('p', '归档只保存了汇总值，没有保存各评分项的命中明细；不能用总分反推哪些条件达成。', 'score-evidence-note'));
  if (result.partial_native_result != null) actual.append(disclosure('部分评测记录', result.partial_native_result));
  container.append(actual);

  const source = scoring.rule_source || {}, provenance = section('规则与证据来源');
  if (source.message) provenance.append(node('p', source.message, source.status === 'matched' ? 'meta' : 'score-evidence-note'));
  const links = node('div', undefined, 'task-review-sources');
  for (const record of review.sources || []) { const link = sourceLink(record); if (link) links.append(link); }
  if (links.children.length) provenance.append(links);
  const code = scoring.source_code || [];
  if (code.length) {
    const details = node('details'); details.append(node('summary', '核对官方评分函数与规则版本'));
    for (const entry of code) { details.append(node('p', `${entry.title || entry.path}${entry.line ? ` · 第 ${entry.line} 行` : ''}`, 'meta'), node('pre', entry.text || '')); }
    details.append(disclosure('规则版本指纹', source)); provenance.append(details);
  }
  container.append(provenance);
}
