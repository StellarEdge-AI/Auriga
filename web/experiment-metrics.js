const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);

const unscoredReasons = {
  unscored_harness_timeout: '动作进展超时',
  unscored_model_budget: '模型调用额度耗尽',
  unscored_infrastructure: '运行基础设施错误',
  unscored_interrupted: '运行中断',
  unscored_evaluation: '缺少有效正式评测',
};
const failureReasons = {
  official_startup_no_action_timeout: '启动后无动作超时',
  official_no_action_progress_timeout: '动作进展超时',
  model_request_limit: '模型调用额度耗尽',
  actor_repeated_inability: '模型连续 5 次声明无法继续完成',
  development_wall_budget: '实验时间预算耗尽',
  native_step_limit: '原生任务步数耗尽',
  native_task_failure: '原生任务判定失败',
  native_failure: '原生任务判定失败',
};

export function isUnscored(summary = {}, review) {
  summary ??= {};
  const result = review?.result;
  return result?.status === 'unscored' || result?.scored_episode === false
    || ['unavailable', 'unscored', 'invalid'].includes(summary.score_status)
    || summary.scored_episode === false || summary.valid_episode === false || result?.valid_episode === false
    || result?.primary_outcome?.startsWith?.('unscored_') || summary.primary_outcome?.startsWith?.('unscored_')
    || (Boolean(summary.infrastructure_error) && summary.scored_episode !== true);
}

export function formalScore(summary = {}) {
  if (isUnscored(summary)) return null;
  const score = summary?.normalized_score ?? summary?.score;
  return isNumber(score) && score >= 0 && score <= 1 ? score : null;
}

// The list has only summary.json; detail also has an observer task review.
// Explicitly unscored runs never acquire a formal result from a snapshot.
export function metrics(summary = {}, review) {
  summary ??= {};
  const result = review?.result;
  const outcome = result?.primary_outcome ?? summary.primary_outcome;
  const unscored = isUnscored(summary, review);
  if (unscored) {
    const reason = summary.termination_reason === 'official_startup_no_action_timeout'
      ? '启动后无动作超时' : unscoredReasons[outcome] || (summary.infrastructure_error ? '运行基础设施错误' : undefined);
    const parts = [`本次未完成正式评测${reason ? `（${reason}）` : ''}`];
    const confirmedFailure = (result?.task_success ?? summary.task_success) === false && (result?.failure_reason ?? summary.failure_reason)?.label;
    if (confirmedFailure) {
      parts[0] = `任务得分：不可用 · 任务成功：否 · 失败原因：${(result?.failure_reason ?? summary.failure_reason).label} · 缺少有效得分证据`;
    }
    const partial = result?.partial_native_result ?? summary.partial_native_result;
    const native = result?.native_process_score ?? summary.native_process_score ?? partial?.native_process_score;
    if (isNumber(native)) parts.push(`局部原生过程分：${native} / 100`);
    if (isNumber(partial?.normalized_score)) parts.push(`局部归一化分：${partial.normalized_score}`);
    if (parts.length === 1 && !confirmedFailure) parts.push('局部原生评测见详情证据');
    return parts.join(' · ');
  }
  const score = result ? result.normalized_score : (summary.score ?? summary.normalized_score);
  const success = result ? result.task_success : summary.task_success;
  const text = `任务得分：${isNumber(score) ? String(score) : '未知'} · 任务成功：${success === true ? '是' : success === false ? '否' : '未知'}`;
  if (success === false) {
    const failure = result?.failure_reason ?? summary.failure_reason;
    const code = failure?.code ?? (summary.native_episode?.ended ? summary.native_episode.reason : summary.termination_reason);
    const reason = failure?.label ?? failureReasons[code];
    if (reason) return `${text} · 失败原因：${reason}`;
  }
  return text;
}
