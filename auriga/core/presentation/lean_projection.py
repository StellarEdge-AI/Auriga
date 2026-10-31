"""Cache-preserving upstream request projection (``features.lean_projection``).

Codex keeps the native thread history; only the Responses request body sent
upstream is rewritten. Each history item is projected by a pure function of the
item itself (plus the immutable function_call that names its call_id):

* every ``input_image`` inside a tool output becomes a fixed placeholder text;
* text results of high-volume robot tools become deterministic short summaries.

One trailing user message, inserted directly after the last tool output, then
carries the latest tool step's full result text and the images of the latest
K tool steps. Request n+1 therefore equals request n without its trailing
message, plus the new items, plus a new trailing message, which keeps the
upstream prompt-cache prefix intact. Compaction items and every other item
type pass through unchanged.

Block mode (``block`` = B > 0, the default through ``features.lean_projection_block``)
has no trailing message. Measured on a Codex Responses relay (2026-10-17), a
request only reuses an earlier request's cache entry when it contains that
entire earlier request; dropping the trailing message of request n therefore
loses request n's entry, and the cached prefix stays at the last request that
had no trailing message. In block mode tool steps (groups of adjacent tool
outputs, numbered from the start of the input) inside the open block, and the
first step of every block (its anchor), are sent unchanged; older steps are
projected as above. Within a block request n+1 is request n plus new items.
When step c+B+1 opens a new block, steps c+2..c+B are rewritten once; the
request whose latest step was the anchor c+1 is still a prefix of the rewritten
request, so only the rewritten block is re-read uncached. Tool outputs whose
call_id is in ``keep`` (replies that hold a ``lean_compact`` context
reference, see ``LeanCompact.pinned_calls``) are always sent unchanged, like
an anchor; they never change once sent, so the prefix property is kept.

Decision reminders (block mode only). Main's decision-context hook
(``CodexSession._task_decision_context``) removes the previous
"Current task decision boundary" assistant message and appends a fresh one at
the end of every actor request. Request n then ends with an item that request
n+1 no longer contains, so no request is ever contained in the next one and
the block-mode extension property above is lost on every call. With block
mode on, :func:`place_block_reminders` keeps the hook's message text but gives
it a fixed slot, and never as the last input item (some Responses relays rarely
reuse the cache entry of a request that ends with an assistant message, measured
2026-10-19: 3 of 36, against 170 of 170 for requests ending with a tool
output). Slot 0 sits directly before the task message of the first request;
each block gets one slot, created on the first request of the block for which
a plan is stored and placed directly before the model turn of the newest step
(after the previous step's output). That position is where the previous
request ended, so the creating request is still an extension of it and still
ends with a tool output. The slot keeps the snapshot it was created with;
reminders of earlier slots stay where they were sent. The "no plan stored"
variant is shown only in slot 0; a block without a stored plan gets no slot,
and the first plan appears in the slot created after it, so no slot is ever
rewritten. Request n+1 is therefore again request n plus new items inside a
block, and at a block rewrite the anchor request is still a prefix.

Decision reminders (block mode only). Main's decision-context hook
(``CodexSession._task_decision_context``) removes the previous
"Current task decision boundary" assistant message and appends a fresh one at
the end of every actor request. Request n then ends with an item that request
n+1 no longer contains, so no request is ever contained in the next one and
the block-mode extension property above is lost on every call. With block
mode on, :func:`place_block_reminders` keeps the hook's message text but gives
it a fixed slot, and never as the last input item (some Responses relays rarely
reuse the cache entry of a request that ends with an assistant message, measured
2026-10-19: 3 of 36, against 170 of 170 for requests ending with a tool
output). Slot 0 sits directly before the task message of the first request;
each block gets one slot, created on the first request of the block for which
a plan is stored and placed directly before the model turn of the newest step
(after the previous step's output). That position is where the previous
request ended, so the creating request is still an extension of it and still
ends with a tool output. The slot keeps the snapshot it was created with;
reminders of earlier slots stay where they were sent. The "no plan stored"
variant is shown only in slot 0; a block without a stored plan gets no slot,
and the first plan appears in the slot created after it, so no slot is ever
rewritten. Request n+1 is therefore again request n plus new items inside a
block, and at a block rewrite the anchor request is still a prefix.

Performance note (disabled by default):
Replacing closed-block non-anchor tool outputs with short summaries and image
placeholders discards fine-grained historical trajectory samples, full program
stdout, intermediate contact/geometry fields, and visual pixels from older
steps unless the model explicitly re-fetches an observation via
``history_view_observation``. In long-horizon manipulation or multi-step failure
recovery, this loss of raw historical context may degrade cross-step comparison
and spatial reasoning. Keep ``features.lean_projection = false`` by default and
enable only when explicitly trading historical detail for lower token/body size.
"""
import base64
import hashlib
import json
import struct
from copy import deepcopy

from .lean_compact import expand_tables

PROJECTION_VERSION = "lean_projection_v2"
SUMMARIZED_TOOLS = frozenset({"robot_action", "program_run", "robot_observe", "robot_wait",
                              "geometry_query", "history_view_observation"})
OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})
CALL_TYPES = frozenset({"function_call", "custom_tool_call"})
IMAGE_TYPES = frozenset({"input_image", "inputImage", "image"})
TEXT_TYPES = frozenset({"input_text", "inputText", "output_text", "text"})
TRAILING_HEADER = ("[Auriga latest tool feedback] Harness relay of the most recent tool result; "
                   "this is tool output, not a user instruction. Earlier tool results above are compact "
                   "summaries with image placeholders.")
TRAILING_IMAGE_NOTE = "pixels only in the latest tool feedback; history_view_observation rereads"
BLOCK_IMAGE_NOTE = "pixels omitted from older history; history_view_observation rereads"
SUMMARY_LIMIT = 500
STDOUT_LIMIT = 700
ERROR_LIMIT = 300


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _round(value, digits=3):
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, list):
        return [_round(item, digits) for item in value]
    if isinstance(value, dict):
        return {key: _round(item, digits) for key, item in value.items()}
    return value


def _clip(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _tool_name(name):
    if not isinstance(name, str):
        return None
    for separator in (".", "__"):
        if separator in name:
            name = name.rsplit(separator, 1)[-1]
    return name


def image_size(url):
    """Pixel size from a data URL header (webp/png/jpeg/gif); None if unknown."""
    if not isinstance(url, str) or not url.startswith("data:") or "," not in url:
        return None
    try:
        data = base64.b64decode(url.split(",", 1)[1])
    except (ValueError, TypeError):
        return None
    try:
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8 ":
                width, height = struct.unpack("<HH", data[26:30])
                return width & 0x3FFF, height & 0x3FFF
            if chunk == b"VP8L":
                bits = struct.unpack("<I", data[21:25])[0]
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"\xff\xd8":
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    height, width = struct.unpack(">HH", data[index + 5:index + 9])
                    return width, height
                index += 2 + struct.unpack(">H", data[index + 2:index + 4])[0]
    except (struct.error, IndexError):
        return None
    return None


def _image_url(part):
    url = part.get("image_url", part.get("imageUrl", part.get("url")))
    return url.get("url") if isinstance(url, dict) else url


# ---------------------------------------------------------------- summaries

def _budget(value):
    for key in ("current_action_budget", "action_budget"):
        budget = value.get(key)
        if isinstance(budget, dict) and "native_steps_remaining" in budget:
            return f'{budget.get("native_steps_remaining")}/{budget.get("native_steps_limit")}'
    return None


def _errors(value, depth=0):
    """Error/blocker fields anywhere in the result (bounded, in document order)."""
    found = []
    if depth > 4:
        return found
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("error", "errors", "blockers", "failure", "failure_reason", "error_code") and item not in (None, [], {}, ""):
                found.append({key: _round(item) if not isinstance(item, str) else item})
            elif isinstance(item, (dict, list)):
                found.extend(_errors(item, depth + 1))
    elif isinstance(value, list):
        for item in value[:8]:
            found.extend(_errors(item, depth + 1))
    return found


def _arm_states(robot_state):
    arms = (robot_state or {}).get("arms") if isinstance(robot_state, dict) else None
    if not isinstance(arms, dict):
        return None
    result = {}
    for arm, state in arms.items():
        if not isinstance(state, dict):
            continue
        entry = {}
        pose = state.get("grasp_pose") or {}
        if isinstance(pose, dict) and pose.get("position_m") is not None:
            entry["grasp_xyz"] = _round(pose.get("position_m"))
            if pose.get("angles_deg") is not None:
                entry["angles_deg"] = _round(pose.get("angles_deg"), 1)
        elif isinstance(state.get("ee_pose"), dict):
            entry["link6_xyz"] = _round(state["ee_pose"].get("position_m"))
        gripper = state.get("gripper") or {}
        if isinstance(gripper, dict) and isinstance(gripper.get("position_m"), list) and gripper["position_m"]:
            entry["jaw_m"] = round(sum(gripper["position_m"]) / len(gripper["position_m"]), 4)
        if entry:
            result[arm] = entry
    return result or None


def _observation(value):
    bundle = value.get("bundle") if isinstance(value.get("bundle"), dict) else value
    result = {"observation_id": bundle.get("observation_id"), "sim_step": bundle.get("sim_step")}
    if value.get("observation_is_current") is not None:
        result["current"] = value.get("observation_is_current")
    view = value.get("image_view")
    if isinstance(view, dict):
        result["view"] = {key: view.get(key) for key in ("camera_id", "source_roi", "display_scale") if key in view}
    arms = _arm_states(value.get("robot_state"))
    if arms:
        result["arms"] = arms
    budget = _budget(value) or _budget(bundle)
    if budget:
        result["budget"] = budget
    return result


def _is_observation(value):
    return isinstance(value, dict) and isinstance(value.get("bundle"), dict) and "robot_state" in value


OBS3D_FIELDS = frozenset({"object_table", "scene3d"})


def _is_obs3d(value):
    """The features.*_observe part of an observation reply (auriga/obs3d.py)."""
    return (isinstance(value, dict) and "observation_id" in value and bool(OBS3D_FIELDS & set(value))
            and set(value) <= OBS3D_FIELDS | {"observation_id", "object_state_delta"})


def _summary_obs3d(value):
    result = {"obs3d_of": value.get("observation_id")}
    table = value.get("object_table")
    if table is not None:
        rows = table.get("rows") if isinstance(table, dict) else None
        result["object_rows"] = len(rows) if isinstance(rows, list) else "unavailable"
    cloud = value.get("scene3d")
    if cloud is not None:
        result["scene3d"] = "stored" if isinstance(cloud, dict) and cloud.get("artifact") else "unavailable"
    return result


def _summary_robot_action(value, arguments):
    receipt = value.get("receipt") if isinstance(value.get("receipt"), dict) else {}
    result = {"status": value.get("status") or receipt.get("status"),
              "mode": arguments.get("mode") or ("preview" if value.get("status") == "planned" else None),
              "plan_id": value.get("plan_id"), "action_id": value.get("action_id")}
    if arguments.get('mode') == 'assess_chain':
        for key in ('executable', 'payload_clearance', 'cost_scope', 'jaw_geometry',
                    'phase_endpoints', 'failed_stage_id', 'failed_arm', 'reason', 'path_status',
                    'structure_clearance', 'risk_witnesses', 'additional_risk_witnesses'):
            if key in value:
                result[key] = deepcopy(value[key])
    if receipt:
        result["receipt"] = {key: receipt.get(key) for key in
            ("status", "reason", "executed_native_steps", "result_observation_id",
             "contact_block_summary", "first_divergence", "endpoint_residual_m",
             "grip_lost", "in_hand_tracking_error") if receipt.get(key) is not None}
        pending = receipt.get("pending_stage_ids")
        if pending:
            result["receipt"]["pending_stages"] = pending[:8]
    if value.get("repair_action") is not None:
        result["repair_action"] = value.get("repair_action")
    if value.get("planned_native_steps") is not None:
        result["planned_steps"] = value.get("planned_native_steps")
    stages = value.get("stages")
    if isinstance(stages, dict):
        issues = [name for name, stage in stages.items()
                  if isinstance(stage, dict) and stage.get("status") not in (None, "completed", "planned")]
        if issues:
            result["stage_issues"] = issues[:6]
    review = value.get("geometry_review")
    if isinstance(review, dict):
        result["geometry"] = {"status": review.get("status"),
                              "min_clear_m": _round(review.get("minimum_estimated_clearance_m"))}
        if review.get('status') == 'unavailable':
            for key in ('reason', 'unknown'):
                if key in review:
                    result['geometry'][key] = deepcopy(review[key])
        if review.get("risks"):
            result["geometry"]["risks"] = len(review["risks"])
            # Preserve actionable pairs in compact output, not just a risk count.
            result['geometry']['risk_pairs'] = [{k: deepcopy(r[k]) for k in (
                'moving_part', 'obstacle', 'native_step', 'refined_separating_axis_gap_m',
                'physical_contact', 'uncertainty_classification', 'separation_review') if k in r}
                for r in review['risks'][:6]]
            result['geometry']['additional_risk_pairs'] = max(0, len(review['risks']) - 6)
    # A compressed rejected preview must still explain how to repair it. It
    # must not turn an unexecuted candidate into an apparently usable plan.
    for key in ("physics_advanced", "risk_review", "issues", "next_step", "object_coverage_state"):
        if key in value:
            result[key] = deepcopy(value[key])
    coverage = value.get("object_coverage")
    if isinstance(coverage, dict):
        result["object_coverage"] = {key: deepcopy(coverage[key]) for key in
            ("status", "issues", "next_step", "evidence_scope", "release_checks") if key in coverage}
    safety = value.get("motion_safety")
    if isinstance(safety, dict) and safety.get("status") is not None:
        result["motion_safety"] = safety.get("status")
    feedback = value.get("trajectory_feedback")
    if isinstance(feedback, dict):
        jaws = (feedback.get("final_measured_jaws") or {}).get("arms")
        if isinstance(jaws, dict):
            result["final_jaw_m"] = {arm: round(sum(state["position_m"]) / len(state["position_m"]), 4)
                for arm, state in jaws.items()
                if isinstance(state, dict) and isinstance(state.get("position_m"), list) and state["position_m"]}
    evidence = value.get("trajectory_evidence")
    if isinstance(evidence, dict) and evidence.get("anomalies"):
        result["anomalies"] = len(evidence["anomalies"])
    budget = _budget(value)
    if budget:
        result["budget"] = budget
    return result


def _summary_program_run(value, arguments):
    result = {key: value.get(key) for key in ("job_id", "status", "returncode", "calls") if value.get(key) is not None}
    actions = value.get("action_summary")
    if isinstance(actions, dict):
        result["actions"] = {key: actions.get(key) for key in ("action_calls", "executed_native_steps_known", "status_counts")
                             if actions.get(key) not in (None, {}, 0)}
    if value.get("final_observation_id"):
        result["final_observation_id"] = value.get("final_observation_id")
    review = value.get("pending_review") or value.get("review")
    if isinstance(review, dict):
        result["review"] = {key: review.get(key) for key in ("review_id", "status", "plan_id") if review.get(key) is not None}
    calls = value.get("call_summary")
    if isinstance(calls, dict) and calls.get("failed_calls"):
        result["failed_calls"] = calls.get("failed_calls")
    stderr = value.get("stderr_text")
    if isinstance(stderr, str) and stderr.strip():
        result["stderr_tail"] = _clip(stderr.strip()[-200:], 200)
    return result


def _summary_geometry_query(value, arguments):
    result = {"query": value.get("query_type"), "status": value.get("status")}
    answer = value.get("value")
    if isinstance(answer, dict):
        for key in ("accepted", "total", "points_m", "point_status", "point_m", "distance_m", "position_m",
                    "pixel", "pixels", "depth_m", "intersection_m", "query_result", "query_results", "batch_results"):
            if key in answer:
                result[key] = _round(answer[key], 4 if key.endswith("_m") else 3)
    elif answer is not None:
        result["value"] = _round(answer, 4)
    return result


def _summary_observation_tool(value, arguments):
    return _observation(value)


def _summary_robot_wait(value, arguments):
    result = {key: value.get(key) for key in ("status", "reason", "executed_native_steps", "native_steps",
                                              "result_observation_id", "observation_id") if value.get(key) is not None}
    receipt = value.get("receipt")
    if isinstance(receipt, dict):
        result["receipt"] = {key: receipt.get(key) for key in ("status", "reason", "executed_native_steps",
                             "result_observation_id") if receipt.get(key) is not None}
    budget = _budget(value)
    if budget:
        result["budget"] = budget
    return result


SUMMARIES = {"robot_action": _summary_robot_action, "program_run": _summary_program_run,
             "geometry_query": _summary_geometry_query, "robot_observe": _summary_observation_tool,
             "history_view_observation": _summary_observation_tool, "robot_wait": _summary_robot_wait}


def summarize_text(tool, text, arguments=None):
    """Deterministic compact replacement for one tool-result text part."""
    try:
        value = expand_tables(json.loads(text))
    except (TypeError, ValueError):
        return "[summary " + str(tool) + "] " + _clip(text, SUMMARY_LIMIT)
    if not isinstance(value, dict):
        return "[summary " + str(tool) + "] " + _clip(_canonical(_round(value)), SUMMARY_LIMIT)
    try:
        summary = (_observation(value) if _is_observation(value) and tool not in ("robot_observe", "history_view_observation")
                   else _summary_obs3d(value) if _is_obs3d(value)
                   else SUMMARIES.get(tool, _summary_robot_wait)(value, arguments if isinstance(arguments, dict) else {}))
    except Exception:
        summary = {key: value.get(key) for key in ("status", "reason", "observation_id") if key in value}
    summary = {key: item for key, item in summary.items() if item not in (None, {}, [])}
    errors = _errors(value)
    text_value = _clip(_canonical(summary), SUMMARY_LIMIT)
    if errors:
        text_value += " errors=" + _clip(_canonical(errors[:3]), ERROR_LIMIT)
    if tool == "program_run" and isinstance(value.get("stdout_text"), str) and value["stdout_text"].strip():
        text_value += " stdout=" + _clip(value["stdout_text"].strip(), STDOUT_LIMIT)
    label = "observation" if _is_observation(value) and tool not in ("robot_observe", "history_view_observation") else tool
    return "[summary " + str(label) + "] " + text_value


# --------------------------------------------------------------- projection

def _parts(output):
    return output if isinstance(output, list) else None


def _image_labels(parts):
    """Camera labels for images that follow an observation text part."""
    labels, queue, observation = [], [], None
    for part in parts:
        if not isinstance(part, dict):
            labels.append(None)
            continue
        if part.get("type") in TEXT_TYPES:
            try:
                value = expand_tables(json.loads(part.get("text", "")))
            except (TypeError, ValueError):
                value = None
            if _is_observation(value):
                bundle = value["bundle"]
                observation = bundle.get("observation_id")
                view = value.get("image_view")
                feedback = value.get("image_feedback")
                if isinstance(view, dict) and view.get("camera_id"):
                    queue = [view["camera_id"]]
                elif isinstance(feedback, dict) and isinstance(feedback.get("included_camera_ids"), list):
                    queue = list(feedback["included_camera_ids"])
                else:
                    queue = [camera.get("camera_id") for camera in bundle.get("cameras", []) if isinstance(camera, dict)]
            else:
                queue, observation = [], None
            labels.append(None)
        elif part.get("type") in IMAGE_TYPES:
            camera = queue.pop(0) if queue else None
            labels.append(f"{observation}/{camera}" if observation and camera else None)
        else:
            labels.append(None)
    return labels


def _project_output(item, tool, arguments, placeholder_note=None):
    """Return (projected item, trailing text parts, trailing image parts)."""
    placeholder_note = placeholder_note or TRAILING_IMAGE_NOTE
    original = item.get("output")
    summarize = tool in SUMMARIZED_TOOLS
    if isinstance(original, str):
        if not summarize:
            return item, [], []
        result = dict(item)
        result["output"] = summarize_text(tool, original, arguments)
        return result, [{"type": "input_text", "text": original}], []
    parts = _parts(original)
    if parts is None or not any(isinstance(part, dict) and (part.get("type") in IMAGE_TYPES
                                or (summarize and part.get("type") in TEXT_TYPES)) for part in parts):
        return item, [], []
    labels = _image_labels(parts)
    images_total = sum(1 for part in parts if isinstance(part, dict) and part.get("type") in IMAGE_TYPES)
    projected, texts, images, index = [], [], [], 0
    for part, label in zip(parts, labels):
        kind = part.get("type") if isinstance(part, dict) else None
        if kind in IMAGE_TYPES:
            index += 1
            size = image_size(_image_url(part))
            description = " ".join(piece for piece in (
                label, f"{size[0]}x{size[1]}" if size else None) if piece)
            projected.append({"type": "input_text", "text":
                f"[image {index}/{images_total}{' ' + description if description else ''}: {placeholder_note}]"})
            images.append((f"image {index}/{images_total}" + (" " + description if description else ""), part))
        elif kind in TEXT_TYPES and summarize:
            projected.append({"type": "input_text", "text": summarize_text(tool, part.get("text", ""), arguments)})
            texts.append(part)
        else:
            projected.append(part)
    result = dict(item)
    result["output"] = projected
    return result, texts, images


def _trailing(steps, k):
    """One user message: latest step's full summarized text, latest K steps' images."""
    content = []
    selected = steps[-k:]
    for position, step in enumerate(selected):
        latest = position == len(selected) - 1
        for item, tool, texts, images in step:
            if not (images or (latest and texts)):
                continue
            content.append({"type": "input_text", "text":
                f"Tool {tool} call_id={item.get('call_id')}" + (" (latest step, full result):" if latest else " (earlier step, images):")})
            # Fixed order: full text parts first, then each labeled image.
            if latest:
                for part in texts:
                    content.append({"type": "input_text", "text": part.get("text", "")})
            for label, image in images:
                content.append({"type": "input_text", "text": label + ":"})
                content.append(image if image.get("type") == "input_image" else dict(image, type="input_image"))
    if not content:
        return None
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": TRAILING_HEADER}] + content}


def tool_steps(items):
    """Positions of the tool steps (groups of directly adjacent tool outputs), in input order."""
    steps, previous_kind = [], None
    for position, item in enumerate(items):
        kind = item.get("type") if isinstance(item, dict) else None
        if kind in OUTPUT_TYPES:
            if previous_kind in OUTPUT_TYPES and steps:
                steps[-1].append(position)
            else:
                steps.append([position])
        previous_kind = kind
    return steps


def _item_key(item):
    """Identity of the item directly before a reminder slot (call_id when it has one)."""
    if isinstance(item, dict) and item.get("type") in OUTPUT_TYPES | CALL_TYPES and item.get("call_id"):
        return str(item["type"]) + ":" + str(item["call_id"])
    return hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _is_model_item(item):
    """Item produced by the model turn (reasoning, assistant text, tool call)."""
    if not isinstance(item, dict):
        return False
    kind = item.get("type")
    return (kind in CALL_TYPES or kind in ("reasoning", "tool_search_call")
            or (kind == "message" and item.get("role") == "assistant"))


def _turn_start(items, output_position):
    """First position of the model turn that directly precedes a tool output."""
    at = output_position
    while at > 0 and _is_model_item(items[at - 1]):
        at -= 1
    return at


def place_block_reminders(items, reminder, block, slots, *, planned=True):
    """Insert decision reminders at fixed block slots (see module docstring).

    ``items``: request input without reminders. ``reminder``: the hook's message
    for this request. ``slots``: mutable per-thread state, slot -> dict(position,
    key, item, planned), positions in ``items`` coordinates. Slot 0 is created on a
    request without tool steps, directly before its last item when that is a user
    message (else at the end). Slot s >= 1 belongs to the block whose anchor is step
    s (s = 1, B+1, 2B+1, ...); it is created on the first request of that block with
    ``planned`` true, directly before the model turn of the newest step, so it is
    never the last item. A slot whose preceding item changed (Codex compaction, a
    new thread) is dropped. ``planned``: False for the hook's "no plan stored"
    variant, which only slot 0 carries. Returns the new input list; ``items`` is
    not modified.
    """
    steps = tool_steps(items)
    current = 0 if not steps else block * ((len(steps) - 1) // block) + 1

    def key(at):
        return _item_key(items[at - 1]) if at else None

    placed = {}
    for slot in sorted(slots):
        entry = slots[slot]
        at = entry["position"]
        if slot > current or at > len(items) or entry["key"] != key(at):
            del slots[slot]
            continue
        placed[slot] = entry
    if current not in placed and (planned or current == 0):
        if steps:
            at = _turn_start(items, steps[-1][0])
        else:
            last = items[-1] if items else None
            at = len(items) - 1 if isinstance(last, dict) and last.get("role") == "user" else len(items)
        slots[current] = placed[current] = dict(position=at, key=key(at), item=deepcopy(reminder),
                                                planned=bool(planned))
    output = list(items)
    for slot in sorted(placed, reverse=True):
        output.insert(placed[slot]["position"], deepcopy(placed[slot]["item"]))
    return output


def _project_blocks(request, items, calls, block, keep=frozenset()):
    """Block mode: open block, block anchors and kept call_ids unchanged, older steps projected."""
    steps = tool_steps(items)
    if not steps:
        return request
    start = block * ((len(steps) - 1) // block)  # steps 1..start are closed
    output = list(items)
    for number, positions in enumerate(steps[:start], 1):
        if (number - 1) % block == 0:
            continue  # anchor: frozen as it was sent while it was the latest step
        for position in positions:
            item = items[position]
            if item.get("call_id") in keep:
                continue  # holds a compact-encoding reference: never summarised
            tool, arguments = calls.get(item.get("call_id"), (None, None))
            output[position] = _project_output(item, tool, arguments, BLOCK_IMAGE_NOTE)[0]
    result = dict(request)
    result["input"] = output
    return result


def project_request(request, k=1, block=0, keep=()):
    """Pure projection of one Responses request body (see module docstring).

    ``keep``: call_ids sent unchanged in block mode (ignored in trailing mode).
    """
    items = request.get("input") if isinstance(request, dict) else None
    if not isinstance(items, list):
        return request
    calls = {}
    for item in items:
        if isinstance(item, dict) and item.get("type") in CALL_TYPES and item.get("call_id"):
            arguments = item.get("arguments", item.get("input"))
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except ValueError:
                arguments = None
            calls[item["call_id"]] = (_tool_name(item.get("name")), arguments)
    if block:
        return _project_blocks(request, items, calls, block, frozenset(keep or ()))
    output, steps, last_output, previous_kind = [], [], None, None
    for item in items:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind in OUTPUT_TYPES:
            tool, arguments = calls.get(item.get("call_id"), (None, None))
            projected, texts, images = _project_output(item, tool, arguments)
            # Directly adjacent outputs answer one model response's calls.
            if previous_kind in OUTPUT_TYPES and steps:
                steps[-1].append((item, tool, texts, images))
            else:
                steps.append([(item, tool, texts, images)])
            output.append(projected)
            last_output = len(output)
        else:
            output.append(item)
        previous_kind = kind
    if last_output is None:
        return request
    trailing = _trailing(steps, k)
    if trailing is not None:
        output.insert(last_output, trailing)
    result = dict(request)
    result["input"] = output
    return result
