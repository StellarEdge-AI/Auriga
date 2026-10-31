// Bounded current views. Full evidence remains queryable through history windows.
export const RECORD_LIMIT = 2000;
export function createRecordCache(limit = RECORD_LIMIT) {
  const entries = new Map();
  let cursor = 0, dropped = false;
  return {
    get cursor() { return cursor; },
    get dropped() { return dropped; },
    get firstPosition() { return Math.min(...[...entries.values()].map((entry) => entry.position)); },
    clear() { entries.clear(); cursor = 0; dropped = false; },
    apply(page) {
      if (page.reset) { entries.clear(); dropped = false; }
      for (const entry of page.entries) entries.set(entry.id, entry);
      const ordered = [...entries.values()].sort((a, b) => a.position - b.position);
      for (const entry of ordered.slice(0, Math.max(0, ordered.length - limit))) { entries.delete(entry.id); dropped = true; }
      cursor = page.incremental ? page.cursor : page.has_more ? page.cursor : 0;
      return [...entries.values()].sort((a, b) => a.position - b.position).map((entry) => entry.value);
    },
  };
}
