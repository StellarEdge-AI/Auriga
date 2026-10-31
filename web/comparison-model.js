import { formalScore } from './experiment-metrics.js';

export { formalScore };
export const scoreText = (score) => score === null || score === undefined ? '—' : (score * 100).toFixed(1);
export const deltaText = (delta) => delta === null || delta === undefined ? '—' : `${delta > 0 ? '+' : ''}${(delta * 100).toFixed(1)}`;
export const familyKey = (group) => group?.series_name_zh || group?.series || '其他独立实验';

export function directionGroups(group, groups) {
  if (!group) return [];
  const members = groups.filter((item) => familyKey(item) === familyKey(group));
  const ids = new Set(members.map((item) => item.id));
  for (const item of members) if (item.baseline_group_id) ids.add(item.baseline_group_id);
  return groups.filter((item) => ids.has(item.id)).sort((a, b) => Number(b.role === 'baseline') - Number(a.role === 'baseline')
    || (a.iteration ?? 999) - (b.iteration ?? 999) || String(a.name_zh || a.label).localeCompare(String(b.name_zh || b.label), 'zh-CN'));
}

export function groupStats(group) {
  const cases = group?.cases || [];
  const scores = cases.map((item) => formalScore(item.run?.summary)).filter((score) => score !== null);
  const count = Math.max(group?.case_count || 0, cases.length);
  return { count, denominatorKnown: group?.protocol_denominator_known !== false, scored: scores.length, unknown: count - scores.length,
    successes: cases.filter((item) => formalScore(item.run?.summary) !== null && item.run?.summary?.task_success === true).length,
    mean: scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null };
}

export function caseIndex(group) {
  const index = new Map(), duplicate = new Set();
  for (const item of group?.cases || []) {
    if (!item.case_key) continue;
    if (index.has(item.case_key)) duplicate.add(item.case_key);
    index.set(item.case_key, item);
  }
  for (const key of duplicate) index.delete(key);
  return index;
}

export function pairedStats(group, reference) {
  const left = caseIndex(group), right = caseIndex(reference);
  const common = [...left.keys()].filter((key) => right.has(key));
  const pairs = common.map((key) => [formalScore(left.get(key).run?.summary), formalScore(right.get(key).run?.summary)])
    .filter(([a, b]) => a !== null && b !== null);
  return { matched: common.length, scored: pairs.length,
    same_cases: left.size === (group?.cases || []).length && right.size === (reference?.cases || []).length
      && left.size === right.size && common.length === left.size,
    delta: pairs.length ? pairs.reduce((sum, [a, b]) => sum + a - b, 0) / pairs.length : null };
}

export function comparisonCases(groups) {
  const indices = groups.map(caseIndex), rows = new Map();
  groups.forEach((group, groupIndex) => {
    (group.cases || []).forEach((item, caseNumber) => {
      // Incomplete or duplicate identities stay separate instead of being paired by task name.
      const key = item.case_key && indices[groupIndex].has(item.case_key) ? item.case_key : `unmatched:${group.id}:${caseNumber}`;
      if (!rows.has(key)) rows.set(key, { key, ...item, members: groups.map(() => null) });
      rows.get(key).members[groupIndex] = item;
    });
  });
  return [...rows.values()].sort((a, b) => String(a.task).localeCompare(String(b.task))
    || String(a.variant).localeCompare(String(b.variant)) || Number(a.eval_seed) - Number(b.eval_seed)
    || Number(a.layout_id) - Number(b.layout_id) || a.key.localeCompare(b.key));
}

export function iterationGroups(group, groups) {
  if (!group) return [];
  const ids = new Set([group.id]);
  for (let changed = true; changed;) {
    changed = false;
    for (const candidate of groups) {
      if (candidate.parent_group_id && (ids.has(candidate.id) || ids.has(candidate.parent_group_id))) {
        for (const id of [candidate.id, candidate.parent_group_id]) if (!ids.has(id)) { ids.add(id); changed = true; }
      }
    }
  }
  const depth = (item) => {
    const visited = new Set(); let current = item;
    while ((current?.recovery_of_group_id || current?.parent_group_id) && !visited.has(current.id)) {
      visited.add(current.id); current = groups.find((value) => value.id === (current.recovery_of_group_id || current.parent_group_id));
    }
    return visited.size;
  };
  return groups.filter((item) => (ids.has(item.id) || group.series && item.series === group.series)
      && (item.id === group.id || group.iteration === null || group.iteration === undefined || item.iteration !== null && item.iteration !== undefined))
    .sort((a, b) => (typeof a.iteration === 'number' && typeof b.iteration === 'number' ? a.iteration - b.iteration : depth(a) - depth(b))
      || depth(a) - depth(b) || String(a.created_at || '').localeCompare(String(b.created_at || '')) || a.id.localeCompare(b.id));
}
