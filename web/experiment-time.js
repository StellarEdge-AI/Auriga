export function formatTime(value) {
  if (typeof value !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return '时间未记录';
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return '时间未记录';
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(parsed).map(({ type, value: part }) => [type, part]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

export function runTimeText(run) {
  if (!run) return '尚无运行记录';
  return `开始：${formatTime(run.started_at)}\n结束：${formatTime(run.finished_at)}`
    + (!run.started_at && run.registered_at ? `\n登记：${formatTime(run.registered_at)}` : '')
    + (run.imported_at ? `\n导入：${formatTime(run.imported_at)}` : '');
}

export function groupTimeText(group) {
  const coverage = group.time_coverage;
  if (!coverage?.run_count) return '尚无运行时间记录';
  return `最早开始：${formatTime(group.started_at)} · 最晚结束：${formatTime(group.finished_at)}`
    + `（开始 ${coverage.started_count}/${coverage.run_count}，结束 ${coverage.finished_count}/${coverage.run_count} 条已记录）`;
}
