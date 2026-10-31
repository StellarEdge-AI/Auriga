"""Lossless compact encoding of actor tool replies (``features.lean_compact``).

Model-facing copies only: the harness archive, program interfaces and stored
evidence are unchanged, and no value is dropped. The JSON text parts of
``robot_action``, ``program_run``, ``robot_observe`` and
``history_view_observation`` replies are re-encoded:

* Tables. A list of per-arm samples (trajectory samples, key poses) becomes
  one row per sample x arm; ``stages``/``stage_costs`` become one row per
  stage; any other list of >= 3 records becomes one row per record. Column
  names appear once, absent cells are null, and values that are equal in every
  row move to ``const`` (strings only, except plain record lists). Inside a
  stage row, ``arms.<arm>`` of the row's own arm is written ``arms.@arm``. A
  table is used only if decoding it gives back the original value exactly and
  it is shorter; otherwise the original is kept.
* Context-once fields. In one model context the first observation is the
  reference: later observations give ``camera_calibration`` and
  ``camera_pose_sample`` as "unchanged since <id>" or as the changed fields
  only, and omit bundle/robot-state strings equal to the reference (marked by
  ``unchanged_since``). Fixed explanatory strings (measurement timing, scope
  notes) and joint-name lists are likewise given once per context.
* Numbers: integral floats are written as integers (1.0 -> 1, -0.0 -> 0).
* Each changed reply carries one ``encoding`` line; the developer instructions
  are not changed.

A model context ends when the harness context epoch changes (Codex compaction,
failed or uncertain tool delivery, app-server change) or the thread changes.
With the block projection (``lean_projection_block`` = B) the reply that
holds the reference observation is pinned by its tool ``call_id``:
:meth:`pinned_calls` is read by the request projection, which sends pinned
replies unchanged instead of summarising them, so the reference stays in the
model context however Codex numbers its tool steps (built-in tools such as
exec_command, parallel calls and compaction included). Context-once strings
first given in that reply are omitted for the rest of the context; one first
given in any other reply is omitted only later in the same reply (pinning
every such reply would keep whole action replies in every later request).
Without a call_id the reference falls back to the harness call count (a copy
outside a block anchor expires when its block closes and is renewed). In
trailing mode (B = 0) context-once fields are off and only tables are used.
``detail='full'`` requests and any internal error return the reply unchanged.
"""
import json
import threading

from ..contracts import canonical

TOOLS = frozenset({"robot_action", "program_run", "robot_observe", "history_view_observation"})
TEXT_TYPES = frozenset({"inputText", "input_text", "text"})
ENCODING = ("tables: rows follow columns, null=absent, const=same in every row, @arm=row's arm; "
            "unchanged/omitted=as first shown")
TABLE_KINDS = ("per_sample_arm", "per_key", "per_item")
TABLE_FIELDS = frozenset({"table", "columns", "rows", "const", "nulls"})
KEYED_TABLES = frozenset({"stages", "stage_costs"})
ITEM_MIN_ROWS = 3
# Columns that identify or classify a row; never moved to const.
ROW_COLUMNS = frozenset({"arm", "phase", "kind", "status", "execution_state", "native_step", "sim_step",
                         "elapsed_time_s", "camera_id", "stage_id", "observation_id", "action_id", "_key"})
TAIL_PREFIXES = ("derived_motion.",)
UNCHANGED_PREFIX = "unchanged since "
CAMERA_DOCUMENTS = ("camera_calibration", "camera_pose_sample")
STATIC_SECTIONS = ("bundle", "robot_state")
STATIC_KEEP = frozenset({"camera_id", "observation_id", "arm", "last_action_id", "capture_time"})
# container key -> fields given once per model context (equal values only).
ONCE_FIELDS = {
    "measurement_timing": ("derivatives", "source_kind", "step_domain"),
    "trajectory_feedback": ("limitations",),
    "geometry": ("calibration_scope", "jaw_scope", "source"),
    "measured": ("timing",),
    "planned": ("timing",),
    "geometry_delivery": ("omitted",),
    "trajectory_evidence": ("details",),
    "geometry_review": ("unknown",),
}
ONCE_ANYWHERE = frozenset({"joint_names"})
_ABSENT = object()


class _Skip(Exception):
    """The value cannot be encoded losslessly; keep the original."""


# ------------------------------------------------------------------ tables

def _flatten(record, out, prefix="", arm=None):
    for key, value in record.items():
        if not isinstance(key, str) or not key or "." in key or key.startswith("@"):
            raise _Skip
        name = key
        if arm is not None and key == arm and (prefix == "arms." or prefix.endswith(".arms.")):
            name = "@arm"
        path = prefix + name
        if isinstance(value, dict) and value:
            _flatten(value, out, path + ".", arm)
        else:
            out[path] = value
    return out


def _unflatten(pairs, arm=None):
    result = {}
    for path, value in pairs:
        parts = path.split(".")
        if "@arm" in parts:
            if not isinstance(arm, str):
                raise _Skip
            parts = [arm if part == "@arm" else part for part in parts]
        node = result
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise _Skip
            node = child
        if parts[-1] in node:
            raise _Skip
        node[parts[-1]] = value
    return result


def _ordered(flats):
    seen = {}
    for flat in flats:
        for path in flat:
            seen.setdefault(path, None)
    return sorted(seen, key=lambda path: path.startswith(TAIL_PREFIXES))


def _const_columns(flats, columns, all_scalars):
    """Columns with one value wherever present, present exactly where their parent object is."""
    candidates = []
    for column in columns:
        if column in ROW_COLUMNS or column.rsplit(".", 1)[-1] in ROW_COLUMNS:
            continue
        present = [flat[column] for flat in flats if column in flat]
        if len(present) < 2:
            continue
        first = present[0]
        if not (isinstance(first, str) or (all_scalars and (first is None or isinstance(first, (bool, int, float))))):
            continue
        if any(canonical(item) != canonical(first) for item in present[1:]):
            continue
        candidates.append(column)
    while True:
        kept = [column for column in columns if column not in candidates]
        removed = None
        for column in candidates:
            parent = column.rsplit(".", 1)[0] + "." if "." in column else ""
            for flat in flats:
                expected = parent == "" or any(path.startswith(parent) for path in kept if path in flat)
                if expected != (column in flat):
                    removed = column
                    break
            if removed:
                break
        if removed is None:
            return candidates
        candidates.remove(removed)


def _cells(flats, columns, all_scalars, use_const=True):
    const = _const_columns(flats, columns, all_scalars) if use_const else []
    kept = [column for column in columns if column not in const]
    nulls = []
    for column in kept:
        has_null = any(flat.get(column, _ABSENT) is None for flat in flats)
        if has_null:
            if any(column not in flat for flat in flats):
                raise _Skip
            nulls.append(column)
    rows = [[flat.get(column) for column in kept] for flat in flats]
    constants = {column: next(flat[column] for flat in flats if column in flat) for column in const}
    return kept, rows, constants, nulls


def _table(kind, columns, rows, constants, nulls):
    table = {"table": kind, "columns": columns, "rows": rows}
    if constants:
        table["const"] = constants
    if nulls:
        table["nulls"] = nulls
    return table


def _row_pairs(columns, cells, nulls, start=0, stop=None):
    for index in range(start, len(columns) if stop is None else stop):
        value = cells[index]
        if value is not None or columns[index] in nulls:
            yield columns[index], value


def _with_const(pairs, constants):
    pairs = list(pairs)
    present = [path for path, _ in pairs]
    for column, value in constants.items():
        parent = column.rsplit(".", 1)[0] + "." if "." in column else ""
        if parent == "" or any(path.startswith(parent) for path in present):
            pairs.append((column, value))
    return pairs


def _encode_items(items, use_const):
    flats = [_flatten(item, {}) for item in items]
    columns = _ordered(flats)
    kept, rows, constants, nulls = _cells(flats, columns, True, use_const)
    return _table("per_item", kept, rows, constants, nulls)


def _encode_keyed(records, use_const):
    flats = []
    for key, record in records.items():
        if not isinstance(record, dict) or "_key" in record:
            raise _Skip
        arm = record.get("arm") if isinstance(record.get("arm"), str) else None
        flats.append(_flatten(record, {}, arm=arm))
    columns = _ordered(flats)
    kept, rows, constants, nulls = _cells(flats, columns, False, use_const)
    if "_key" in kept:
        raise _Skip
    rows = [[key] + row for key, row in zip(records, rows)]
    return _table("per_key", ["_key"] + kept, rows, constants, nulls)


def _encode_sample_arm(samples, use_const):
    sample_flats, arm_flats, arms = [], [], []
    for sample in samples:
        flat = _flatten({key: value for key, value in sample.items() if key != "arms"}, {})
        for arm, record in sample["arms"].items():
            sample_flats.append(flat)
            arm_flats.append(_flatten(record, {}))
            arms.append(arm)
    sample_columns = _ordered(sample_flats)
    arm_columns = _ordered(arm_flats)
    if "arm" in sample_columns or "arm" in arm_columns or set(sample_columns) & set(arm_columns):
        raise _Skip
    _, sample_rows, _, sample_nulls = _cells(sample_flats, sample_columns, False, use_const=False)
    kept, arm_rows, constants, arm_nulls = _cells(arm_flats, arm_columns, False, use_const)
    rows = [left + [arm] + right for left, arm, right in zip(sample_rows, arms, arm_rows)]
    return _table("per_sample_arm", sample_columns + ["arm"] + kept, rows, constants, sample_nulls + arm_nulls)


def is_table(value):
    return (isinstance(value, dict) and value.get("table") in TABLE_KINDS and set(value) <= TABLE_FIELDS
            and isinstance(value.get("columns"), list) and isinstance(value.get("rows"), list))


def decode_table(table):
    columns, rows = table["columns"], table["rows"]
    constants, nulls = table.get("const") or {}, set(table.get("nulls") or ())
    kind = table["table"]
    if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
        raise _Skip
    if kind == "per_item":
        return [_unflatten(_with_const(_row_pairs(columns, row, nulls), constants)) for row in rows]
    if kind == "per_key":
        result = {}
        arm_index = columns.index("arm") if "arm" in columns else None
        for row in rows:
            arm = row[arm_index] if arm_index is not None else None
            if row[0] in result:
                raise _Skip
            result[row[0]] = _unflatten(_with_const(_row_pairs(columns, row, nulls, start=1), constants), arm)
        return result
    split = columns.index("arm")
    samples, current_cells, current = [], None, None
    for row in rows:
        sample_cells, arm = row[:split], row[split]
        if current is None or sample_cells != current_cells or arm in current["arms"]:
            current = _unflatten(_row_pairs(columns, row, nulls, stop=split))
            if "arms" in current:
                raise _Skip
            current["arms"] = {}
            current_cells = sample_cells
            samples.append(current)
        current["arms"][arm] = _unflatten(_with_const(_row_pairs(columns, row, nulls, start=split + 1), constants))
    return samples


def expand_tables(value):
    """Inverse of the table encoding (pure); other fields are returned as they are."""
    if is_table(value):
        try:
            return decode_table(value)
        except (_Skip, ValueError, TypeError, IndexError, KeyError):
            return value
    if isinstance(value, dict):
        return {key: expand_tables(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_tables(item) for item in value]
    return value


def _contains_table(value):
    if is_table(value):
        return True
    if isinstance(value, dict):
        return any(_contains_table(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_table(item) for item in value)
    return False


def _verified(encoder, value):
    original = canonical(value)
    for use_const in (True, False):
        try:
            table = encoder(value, use_const)
            if canonical(decode_table(json.loads(canonical(table)))) == original:
                return table if len(canonical(table)) < len(original) else None
        except (_Skip, ValueError, TypeError, IndexError, KeyError, StopIteration):
            continue
    return None


def _sample_list(value):
    return (len(value) >= 2 and all(isinstance(item, dict) and isinstance(item.get("arms"), dict) and item["arms"]
                                    and all(isinstance(record, dict) for record in item["arms"].values())
                                    for item in value))


def _record_list(value):
    return len(value) >= ITEM_MIN_ROWS and all(isinstance(item, dict) and item for item in value)


def encode_tables(value):
    """Replace encodable record collections by verified tables (pure)."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            table = None
            if key in KEYED_TABLES and isinstance(item, dict) and len(item) >= 2:
                table = _verified(_encode_keyed, item)
            elif isinstance(item, list) and _sample_list(item):
                table = _verified(_encode_sample_arm, item)
            elif isinstance(item, list) and _record_list(item):
                table = _verified(_encode_items, item)
            result[key] = table if table is not None else encode_tables(item)
        return result
    if isinstance(value, list):
        return [encode_tables(item) for item in value]
    return value


def integral_numbers(value):
    """Write integral floats as integers (1.0 -> 1, -0.0 -> 0); the numeric value is unchanged."""
    if isinstance(value, float):
        return int(value) if value.is_integer() and abs(value) < 2 ** 53 else value
    if isinstance(value, list):
        return [integral_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: integral_numbers(item) for key, item in value.items()}
    return value


# ------------------------------------------------------- context-once fields

def is_observation(value):
    return isinstance(value, dict) and isinstance(value.get("bundle"), dict) and "robot_state" in value


def _static_leaf(value):
    return isinstance(value, str) or (isinstance(value, list) and value and all(isinstance(item, str) for item in value))


def _static_strings(value, path=(), out=None):
    out = {} if out is None else out
    if isinstance(value, dict):
        for key, item in value.items():
            if key in STATIC_KEEP:
                continue
            if _static_leaf(item):
                out[path + (key,)] = canonical(item)
            elif isinstance(item, (dict, list)):
                _static_strings(item, path + (key,), out)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, (dict, list)):
                _static_strings(item, path + (index,), out)
    return out


def _drop_static(value, reference, path=()):
    """Remove string leaves equal to the reference at the same path; True if any was removed."""
    removed = False
    if isinstance(value, dict):
        for key in list(value):
            item = value[key]
            if key in STATIC_KEEP:
                continue
            if _static_leaf(item):
                if reference.get(path + (key,)) == canonical(item):
                    del value[key]
                    removed = True
            elif isinstance(item, (dict, list)):
                removed = _drop_static(item, reference, path + (key,)) or removed
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, (dict, list)):
                removed = _drop_static(item, reference, path + (index,)) or removed
    return removed


def document_delta(first, current, reference_id):
    """Camera document relative to the reference copy (lossless given that copy)."""
    if canonical(first) == canonical(current):
        return UNCHANGED_PREFIX + reference_id
    if (not isinstance(first, dict) or not isinstance(current, dict) or set(first) - set(current)
            or "unchanged_since" in current):
        return current
    result = {"unchanged_since": reference_id}
    for key, item in current.items():
        if key == "cameras" and isinstance(item, dict) and isinstance(first.get(key), dict):
            reference_cameras = first[key]
            if set(reference_cameras) - set(item):
                return current
            cameras = {}
            for name, camera in item.items():
                base = reference_cameras.get(name)
                if base is None:
                    if camera == "unchanged" or (isinstance(camera, dict) and "unchanged_since" in camera):
                        return current
                    cameras[name] = camera
                elif canonical(base) == canonical(camera):
                    cameras[name] = "unchanged"
                elif isinstance(base, dict) and isinstance(camera, dict) and not set(base) - set(camera):
                    cameras[name] = {field: part for field, part in camera.items()
                                     if field not in base or canonical(base[field]) != canonical(part)}
                else:
                    return current
            result[key] = cameras
        elif key not in first or canonical(first[key]) != canonical(item):
            result[key] = item
    return result if len(canonical(result)) < len(canonical(current)) else current


def restore_document(first, delivered):
    """Inverse of :func:`document_delta`."""
    if isinstance(delivered, str) and delivered.startswith(UNCHANGED_PREFIX):
        return json.loads(canonical(first))
    if not (isinstance(delivered, dict) and "unchanged_since" in delivered):
        return delivered
    result = json.loads(canonical(first))
    for key, item in delivered.items():
        if key == "unchanged_since":
            continue
        if key == "cameras" and isinstance(item, dict):
            cameras = result.setdefault("cameras", {})
            for name, camera in item.items():
                if camera == "unchanged":
                    continue
                if name in cameras and isinstance(camera, dict):
                    cameras[name] = dict(cameras[name], **camera)
                else:
                    cameras[name] = camera
        else:
            result[key] = item
    return result


class LeanCompact:
    """Per-actor-context state for the compact encoding (thread-safe).

    ``anchor_period``: None = no request projection, a full copy stays visible
    for the whole context; 0 = trailing projection, context-once fields off
    (tables only); B > 0 = block projection. Under the block projection the
    reply that holds the reference observation is pinned by its ``call_id``
    (see :meth:`pinned_calls`) and stays visible for the whole context. Without a
    call_id the visibility falls back to the harness call count: a full copy
    given at call c (0-based) stays visible if c % B == 0 and otherwise only
    until call B * (c // B + 1), after which it is given in full again.
    """

    def __init__(self, *, anchor_period=None):
        self.anchor_period = anchor_period
        self._lock = threading.Lock()
        self._epoch = self._thread = object()
        self._calls = 0
        self._key = 0  # reference key of the reply being encoded: call_id (pinned) or call count
        self._pinned = frozenset()  # call_ids the projection must send unchanged (this thread)
        self._reset()

    def _reset(self):
        self._reference = None  # dict(id, call, documents, static)
        self._once = {}  # (container, key) -> (canonical value, call)
        self._replays = {}  # call_id -> encoded response for idempotent replay within epoch

    def pinned_calls(self):
        """Tool call_ids whose replies hold the reference; the request projection keeps them in full.

        Safe to read from another thread. Pins survive a context-epoch change
        (an earlier reply may still cite the old reference) and are cleared
        when the thread changes.
        """
        return self._pinned

    def _visible(self, call):
        period = self.anchor_period
        if period is None or isinstance(call, str):
            return True  # no projection, or pinned by call_id
        return call % period == 0 or self._calls < period * (call // period + 1)

    def apply(self, tool, arguments, response, epoch=None, thread=None, call_id=None):
        try:
            with self._lock:
                if thread != self._thread:
                    self._thread, self._calls, self._pinned = thread, 0, frozenset()
                    self._reset()
                if epoch != self._epoch:
                    self._epoch = epoch
                    self._reset()
                if isinstance(call_id, str) and call_id and call_id in self._replays:
                    return self._replays[call_id]
                context = self.anchor_period != 0
                if context:
                    if self._reference is not None and not self._visible(self._reference["call"]):
                        self._reference = None
                    self._once = {key: item for key, item in self._once.items() if self._visible(item[1])}
                pin = call_id if isinstance(call_id, str) and call_id and self.anchor_period else None
                self._key = pin if pin is not None else self._calls
                try:
                    if tool not in TOOLS or (isinstance(arguments, dict) and arguments.get("detail") == "full"):
                        return response
                    result = self._apply(response, context)
                    if isinstance(call_id, str) and call_id:
                        self._replays[call_id] = result
                    return result
                finally:
                    if pin is not None:
                        self._pin(pin)
                    self._calls += 1
        except Exception:
            # An optional presentation step never hides an executed result.
            return response

    # ------------------------------------------------------------ internals

    def _pin(self, pin):
        """After a reply with a call_id: pin it if it holds the reference observation.

        A pinned reply stays in every later request, so only the reference
        observation is pinned; a context-once string first given in another
        reply is a reference within that reply only (dropped here).
        """
        if (self._reference or {}).get("call") == pin:
            self._pinned = self._pinned | {pin}
        else:
            self._once = {key: item for key, item in self._once.items() if item[1] != pin}

    def _apply(self, response, context):
        if not isinstance(response, dict) or not isinstance(response.get("contentItems"), list):
            return response
        items, texts, noted = response["contentItems"], {}, False
        for index, item in enumerate(items):
            if not isinstance(item, dict) or item.get("type") not in TEXT_TYPES:
                continue
            text = item.get("text")
            try:
                value = json.loads(text)
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict) or _contains_table(value):
                continue
            new = self.encode(value, context)
            if canonical(new) == canonical(value):
                continue
            if not noted and "encoding" not in new:
                new["encoding"] = ENCODING
            encoded = canonical(new)
            if len(encoded) < len(text):
                texts[index] = encoded
                noted = noted or new.get("encoding") == ENCODING
        if not texts:
            return response
        content = [dict(item, text=texts[index]) if index in texts else item for index, item in enumerate(items)]
        return dict(response, contentItems=content)

    def encode(self, value, context=True):
        """Encoded copy of one reply part; updates the context state."""
        value = integral_numbers(json.loads(canonical(value)))
        if context:
            value = self._context(value)
        return encode_tables(value)

    def _context(self, value, container=None):
        if isinstance(value, dict):
            observation = is_observation(value)
            if observation:
                self._observation(value)
            fields = ONCE_FIELDS.get(container, ())
            for key in list(value):
                if (key in fields and _static_leaf(value[key])) or key in ONCE_ANYWHERE:
                    signature = (container, key)
                    delivered = canonical(value[key])
                    known = self._once.get(signature)
                    if known is not None and known[0] == delivered:
                        del value[key]
                        continue
                    if known is None:
                        self._once[signature] = (delivered, self._key)
                if isinstance(value.get(key), (dict, list)) and not (observation and key in CAMERA_DOCUMENTS):
                    self._context(value[key], key)
        elif isinstance(value, list):
            for item in value:
                self._context(item, container)
        return value

    def _observation(self, value):
        bundle = value["bundle"]
        observation_id = bundle.get("observation_id")
        documents = {key: value.get(key) for key in CAMERA_DOCUMENTS if isinstance(value.get(key), dict)}
        reference = self._reference
        if reference is None:
            if isinstance(observation_id, str) and len(documents) == len(CAMERA_DOCUMENTS):
                self._reference = dict(id=observation_id, call=self._key, documents=json.loads(canonical(documents)),
                    static={section: _static_strings(value.get(section)) for section in STATIC_SECTIONS})
            return
        if observation_id == reference["id"]:
            return
        changed = False
        for key, document in documents.items():
            delivered = document_delta(reference["documents"][key], document, reference["id"])
            if delivered is not document:
                value[key] = delivered
                changed = True
        for section in STATIC_SECTIONS:
            if isinstance(value.get(section), (dict, list)):
                changed = _drop_static(value[section], reference["static"][section]) or changed
        if changed and "unchanged_since" not in value:
            value["unchanged_since"] = reference["id"]
