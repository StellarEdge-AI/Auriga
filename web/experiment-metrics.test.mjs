import assert from 'node:assert/strict';
import test from 'node:test';
import { metrics } from './experiment-metrics.js';

const timeout = {
  scored_episode: false, primary_outcome: 'unscored_harness_timeout',
  termination_reason: 'official_no_action_progress_timeout',
  score: null, normalized_score: null, task_success: null, native_process_score: 40,
  partial_native_result: { normalized_score: 0.4, native_process_score: 40, task_success: false },
};

test('timeout with a saved process score explains the same result in list and detail', () => {
  const expected = '本次未完成正式评测（动作进展超时） · 局部原生过程分：40 / 100 · 局部归一化分：0.4';
  assert.equal(metrics(timeout), expected);
  assert.equal(metrics(timeout, { result: { ...timeout, status: 'unscored' } }), expected);
});

test('exclusion takes precedence over numeric score and success snapshots', () => {
  for (const exclusion of [
    { scored_episode: false }, { primary_outcome: 'unscored_interrupted' }, { infrastructure_error: 'worker_failed' },
  ]) {
    const text = metrics({ score: 1, task_success: true, ...exclusion });
    assert.match(text, /未完成正式评测/);
    assert.doesNotMatch(text, /任务得分：1|任务成功：是/);
  }
});

test('unscored snapshots preserve zero without deriving missing numbers', () => {
  assert.equal(metrics({ scored_episode: false, partial_native_result: { native_process_score: 0, normalized_score: 0 } }),
    '本次未完成正式评测 · 局部原生过程分：0 / 100 · 局部归一化分：0');
  assert.equal(metrics({ scored_episode: false, native_process_score: 40 }),
    '本次未完成正式评测 · 局部原生过程分：40 / 100');
});

test('startup and progress timeouts have distinct reasons', () => {
  assert.match(metrics({ ...timeout, termination_reason: 'official_startup_no_action_timeout' }), /启动后无动作超时/);
  assert.match(metrics({ infrastructure_error: 'worker_failed' }), /未完成正式评测（运行基础设施错误）/);
});

test('an unscored task review suppresses stale list scores', () => {
  assert.equal(metrics({ score: 1, task_success: true }, { result: { status: 'unscored' } }),
    '本次未完成正式评测 · 局部原生评测见详情证据');
  assert.match(metrics({ primary_outcome: 'unscored_interrupted' },
    { result: { primary_outcome: 'scored_success', normalized_score: 1, task_success: true } }), /未完成正式评测/);
});

test('intentional actor inability stop shows the evaluated score and stop reason', () => {
  assert.equal(metrics({ scored_episode: true, primary_outcome: 'scored_failure',
    termination_reason: 'actor_repeated_inability', score: 0.4, task_success: false }),
  '任务得分：0.4 · 任务成功：否 · 失败原因：模型连续 5 次声明无法继续完成');
});

test('scored failures and missing evaluations keep their distinct values', () => {
  assert.equal(metrics({ score: 0, task_success: false }), '任务得分：0 · 任务成功：否');
  assert.equal(metrics({ score: 0.4, task_success: false }), '任务得分：0.4 · 任务成功：否');
  assert.equal(metrics(null), '任务得分：未知 · 任务成功：未知');
  assert.equal(metrics({ score: 1, task_success: true }, { result: { normalized_score: null, task_success: null } }),
    '任务得分：未知 · 任务成功：未知');
});

test('invalid numeric snapshots are not displayed as scores', () => {
  for (const value of [NaN, Infinity, '40', true]) {
    assert.equal(metrics({ scored_episode: false, native_process_score: value, partial_native_result: { normalized_score: value } }),
      '本次未完成正式评测 · 局部原生评测见详情证据');
  }
});

 test('confirmed task limits retain scores and expose specific failure reasons', () => {
  for (const [code, label] of [['official_no_action_progress_timeout', '动作进展超时'], ['official_startup_no_action_timeout', '启动后无动作超时'], ['model_request_limit', '模型调用额度耗尽'], ['development_wall_budget', '实验时间预算耗尽']]) {
    const summary = { scored_episode: true, primary_outcome: 'scored_failure', score: 0.4, normalized_score: 0.4, task_success: false, failure_reason: { code, label } };
    const expected = `任务得分：0.4 · 任务成功：否 · 失败原因：${label}`;
    assert.equal(metrics(summary), expected);
    assert.equal(metrics(summary, { result: { ...summary, status: 'scored' } }), expected);
  }
});

test('confirmed failures without usable scores still expose outcome and reason', () => {
  const summary = { scored_episode: false, primary_outcome: 'unscored_evaluation', task_outcome: 'failure', task_success: false, normalized_score: null, failure_reason: { code: 'official_no_action_progress_timeout', label: '动作进展超时' } };
  const expected = '任务得分：不可用 · 任务成功：否 · 失败原因：动作进展超时 · 缺少有效得分证据';
  assert.equal(metrics(summary), expected);
  assert.equal(metrics(summary, { result: { ...summary, status: 'unscored' } }), expected);
});
