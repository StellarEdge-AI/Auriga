import assert from 'node:assert/strict';
import test from 'node:test';
import { createRecordCache } from './record-cache.js';

test('incremental changes retain original order and bound the current view', () => {
  const cache = createRecordCache(2);
  const entry = (id, position, text = id) => ({ id, position, value: { id, text } });
  let values = cache.apply({ entries: [entry('a', 1), entry('b', 2)], cursor: 2, incremental: true });
  assert.deepEqual(values.map((value) => value.id), ['a', 'b']);
  values = cache.apply({ entries: [entry('a', 1, 'changed')], cursor: 3, incremental: true });
  assert.deepEqual(values.map((value) => value.text), ['changed', 'b']);
  values = cache.apply({ entries: [entry('c', 3)], cursor: 4, incremental: true });
  assert.deepEqual(values.map((value) => value.id), ['b', 'c']);
  assert.equal(cache.dropped, true);
  assert.equal(cache.firstPosition, 2);
  values = cache.apply({ entries: [entry('new', 1)], cursor: 1, reset: true, incremental: true });
  assert.deepEqual(values.map((value) => value.id), ['new']);
  assert.equal(cache.dropped, false);
});

test('legacy snapshots page without pretending they support future deltas', () => {
  const cache = createRecordCache();
  cache.apply({ entries: [{ id: 'a', position: 1, value: {} }], cursor: 1, incremental: false, has_more: true });
  assert.equal(cache.cursor, 1);
  cache.apply({ entries: [], cursor: 1, incremental: false, has_more: false });
  assert.equal(cache.cursor, 0);
});
