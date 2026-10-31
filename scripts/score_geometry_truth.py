"""Offline scoring of geometry estimates against the analysis-only simulator truth log.

Reads, never writes, run directories produced with ``analysis.scene_truth_log`` on:
``events.sqlite`` (copied to a temporary directory first, so SQLite creates no sidecar in the run),
``blobs/`` and ``private/scene_truth.jsonl``. Run it after the episode, inside the container (``private/``
is root-owned there)::

    python scripts/score_geometry_truth.py <run dir or a directory containing runs> [...] \
        [--out-json scores.json] [--out-md scores.md] [--max-depth 4]

Per geometry_query call (pixel_plane ``value.point_m``; triangulate ``value.points_m``, one row per point;
object_table ``value.rows``, one row per object id, columns looked up by name so the v5 ``label`` column and the
``labels`` field change nothing) the truth record of the same capture is used (``frame_ids`` ->
``raw_observation_id``; fallback observation -> sim_step). Tables attached to robot_observe / robot_action /
robot_wait replies (features.object_table_observe) are scored as kind ``object_table_obs``, one table per
observation (a table reused for a later reply of the same observation is counted as a delivery, not scored twice):
from the internal events ``analysis.obs3d_object_table`` (written with ``analysis.scene_truth_log`` on), or, for
runs without them, from the JSON text part of those replies (``wire_response`` of the tool result). Each estimate is matched to the task object whose centre is nearest in xy;
``unambiguous`` means the second-nearest centre is at least 2x as far and at least 2 cm farther.
Fixtures (default: camera_stand) are never matched. ``inside_xy_footprint`` tests the two local box
axes closest to horizontal; ``top_z_m`` is the highest corner of the rotated local box (for a tilted
object it can lie a few mm above the true top surface).
Reported: xy error, z error vs the object centre and vs its top surface, and for pixel_plane the plane
height applied, the caller's plane, the VGGT centre/top estimate and the truth heights. Requested grasp
contact targets (``requested_contact_position_m`` in receipts) are scored against the truth at the
action's start capture. Aggregates (p50/p90 of absolute values) per episode, per tool kind, per
plane_source, per role. The script also counts whether any recorded tool call or program source names
the truth file (leak audit).
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile

TRUTH_FILE = "scene_truth.jsonl"
RAW_ID = re.compile(r"^(raw-[0-9a-fA-F-]{36})(?:-|$)")
AMBIGUITY_RATIO, AMBIGUITY_GAP_M = 2.0, 0.02
CLOSE_GOAL = 0.5
LEAK_MARKERS = ("scene_truth", "private/robot/evaluations")
OBS3D_EVENT = "analysis.obs3d_object_table"
OBS3D_TOOLS = ("robot_observe", "robot_action", "robot_wait")
# scene fixtures that are layout objects but never manipulation targets
EXCLUDE_CATEGORIES = {"camera_stand"}


# ------------------------------------------------------------------ small math helpers
def quat_matrix_xyzw(q):
    x, y, z, w = (float(v) for v in q)
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]]


def percentile(values, q):
    values = sorted(v for v in values if v is not None and math.isfinite(v))
    if not values:
        return None
    k = (len(values) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def _vec(value, n=3):
    if not isinstance(value, (list, tuple)) or len(value) < n:
        return None
    try:
        out = [float(v) for v in value[:n]]
    except (TypeError, ValueError):
        return None
    return out if all(math.isfinite(v) for v in out) else None


def _r(value, digits=4):
    return None if value is None else round(float(value), digits)


# ------------------------------------------------------------------ run access (read-only)
class RunReader:
    def __init__(self, root):
        self.root = Path(root)
        self.tmp = tempfile.mkdtemp(prefix="score-geometry-truth-")
        for name in ("events.sqlite", "events.sqlite-wal"):
            source = self.root / name
            if source.exists():
                shutil.copyfile(source, Path(self.tmp) / name)
        self.db = sqlite3.connect(Path(self.tmp) / "events.sqlite")

    def close(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def records(self, kind):
        return [(identifier, json.loads(body)) for identifier, body in
                self.db.execute("SELECT id, body FROM records WHERE kind=? ORDER BY rowid", (kind,))]

    def events(self, event_type):
        """Event bodies of one type, in order (substring prefilter, exact type check)."""
        out = []
        for (body,) in self.db.execute("SELECT body FROM events WHERE body LIKE ? ORDER BY seq", ("%" + event_type + "%",)):
            try:
                event = json.loads(body)
            except ValueError:
                continue
            if event.get("type") == event_type:
                out.append(event)
        return out

    def artifact_json(self, reference):
        identifier = str(reference).removeprefix("artifact:")
        row = self.db.execute("SELECT body FROM artifacts WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return None
        info = json.loads(row[0])
        try:
            return json.loads((self.root / "blobs" / info["storage_key"]).read_bytes())
        except (OSError, ValueError):
            return None


def load_truth(root):
    path = Path(root) / "private" / TRUTH_FILE
    by_raw, by_step, errors = {}, {}, 0
    if not path.exists():
        return None, {}, {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                errors += 1
                continue
            by_raw[item.get("raw_observation_id")] = item
            by_step.setdefault(item.get("sim_step"), item)
    return dict(path=str(path), lines=len(by_raw), parse_errors=errors), by_raw, by_step


def raw_id(frame_id):
    match = RAW_ID.match(str(frame_id or ""))
    return match.group(1) if match else None


class Episode:
    def __init__(self, root):
        self.root = Path(root)
        self.reader = RunReader(root)
        self.truth_info, self.truth_raw, self.truth_step = load_truth(root)
        self.observations = {}
        for identifier, body in self.reader.records("observation"):
            cameras = body.get("cameras") or []
            self.observations[identifier] = dict(
                raw=next((raw_id(c.get("frame_id")) for c in cameras if raw_id(c.get("frame_id"))), None),
                sim_step=body.get("sim_step"))

    def truth_for(self, frame_ids=(), observation_ids=()):
        for frame in frame_ids or ():
            item = self.truth_raw.get(raw_id(frame))
            if item is not None:
                return item, "frame_id"
        for obs in observation_ids or ():
            info = self.observations.get(obs) or {}
            if info.get("raw") in self.truth_raw:
                return self.truth_raw[info["raw"]], "observation_raw_id"
            if info.get("sim_step") in self.truth_step:
                return self.truth_step[info["sim_step"]], "sim_step"
        return None, None


# ------------------------------------------------------------------ matching and errors
def object_centre(item):
    return _vec(item.get("centroid_m")) or _vec(item.get("position_m"))


def inside_footprint(item, point):
    lo, hi = _vec(item.get("bbox_local_min_m")), _vec(item.get("bbox_local_max_m"))
    p, q = _vec(item.get("position_m")), _vec(item.get("quaternion_xyzw"), 4)
    if None in (lo, hi, p, q):
        return None
    r = quat_matrix_xyzw(q)
    d = [point[i] - p[i] for i in range(3)]
    local = [sum(r[j][i] * d[j] for j in range(3)) for i in range(3)]   # R^T d
    vertical = max(range(3), key=lambda i: abs(r[2][i]))                  # local axis closest to world z
    return all(lo[i] - 1e-6 <= local[i] <= hi[i] + 1e-6 for i in range(3) if i != vertical)


def match(point, truth, exclude=None):
    exclude = EXCLUDE_CATEGORIES if exclude is None else exclude
    candidates = []
    for item in truth.get("objects") or []:
        if item.get("category") in exclude or item.get("name") in exclude:
            continue
        centre = object_centre(item)
        if centre is None:
            continue
        candidates.append((math.hypot(point[0] - centre[0], point[1] - centre[1]), item, centre))
    if not candidates:
        return None
    candidates.sort(key=lambda entry: entry[0])
    d1, item, centre = candidates[0]
    d2 = candidates[1][0] if len(candidates) > 1 else None
    unambiguous = d2 is None or (d2 >= AMBIGUITY_RATIO * d1 and d2 - d1 >= AMBIGUITY_GAP_M)
    top = item.get("top_z_m")
    out = dict(object=item.get("name"), category=item.get("category"), unambiguous=bool(unambiguous),
               second_object=candidates[1][1].get("name") if d2 is not None else None,
               second_xy_distance_m=_r(d2), inside_xy_footprint=inside_footprint(item, point),
               truth_centre_m=[_r(v) for v in centre], truth_top_z_m=_r(top),
               xy_error_m=_r(d1), z_error_centre_m=_r(point[2] - centre[2]),
               z_error_top_m=_r(point[2] - top) if isinstance(top, (int, float)) else None,
               error_3d_centre_m=_r(math.dist(point, centre)))
    return out


def plane_info(value):
    """Plane heights of a pixel_plane result: applied, caller's, VGGT estimate."""
    origin, normal = _vec(value.get("plane_origin_m")), _vec(value.get("plane_normal"))
    horizontal = normal is not None and abs(normal[2]) >= 0.98 * math.sqrt(sum(v * v for v in normal))
    estimate = value.get("plane_estimate") if isinstance(value.get("plane_estimate"), dict) else None
    applied = origin[2] if (origin and horizontal) else None
    alt = _vec(value.get("plane_origin_alt_m"))
    if estimate and estimate.get("status") == "ok":
        source = "vggt"
        caller = alt[2] if (alt and str(estimate.get("alternate_plane", "")).startswith("caller")) else None
    else:
        source = "caller" if estimate is None else "caller(vggt_" + str(estimate.get("status")) + ")"
        caller = applied
    return dict(plane_source=source, plane_horizontal=horizontal, plane_z_applied_m=_r(applied),
                caller_plane_z_m=_r(caller),
                vggt_centre_z_m=_r(estimate.get("center_z_m")) if estimate and estimate.get("status") == "ok" else None,
                vggt_top_z_m=_r(estimate.get("top_z_m")) if estimate and estimate.get("status") == "ok" else None,
                vggt_quality=estimate.get("quality") if estimate else None)


def geometry_rows(episode):
    rows = []
    for identifier, record in episode.reader.records("tool_result"):
        if record.get("tool") != "geometry_query":
            continue
        payload = episode.reader.artifact_json(record.get("raw_ref")) or {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        kind, value = data.get("query_type"), data.get("value")
        if kind not in ("pixel_plane", "triangulate", "object_table") or not isinstance(value, dict):
            continue
        truth, joined = episode.truth_for(data.get("frame_ids"), data.get("observation_ids"))
        base = dict(tool_result_id=identifier, kind=kind, role=record.get("role"), status=data.get("status"),
                    observation_id=(data.get("observation_ids") or [None])[0],
                    camera_ids=[str(f).split("-")[-1] if raw_id(f) else None for f in data.get("frame_ids") or []],
                    truth_join=joined, truth_sim_step=truth.get("sim_step") if truth else None,
                    program_id=(record.get("correlation") or {}).get("program_id"))
        points = []
        if kind == "pixel_plane" and _vec(value.get("point_m")):
            extra = plane_info(value)
            alt = _vec(value.get("alternate_point_m"))
            points.append((_vec(value["point_m"]), extra, alt))
        elif kind == "object_table":   # features.object_table: one row per reported object id
            points.extend(table_points(value.get("columns"), value.get("rows"), "object_table"))
        elif kind == "triangulate":
            reproj = value.get("reprojection_error_px") or []
            accepted = value.get("accepted_indices") or list(range(len(value.get("points_m") or [])))
            for k, point in enumerate(value.get("points_m") or []):
                index = accepted[k] if k < len(accepted) else k
                points.append((_vec(point), dict(plane_source="triangulate", point_index=index,
                    reprojection_error_px=_r(reproj[index]) if index < len(reproj) else None), None))
        for point, extra, alt in points:
            if point is None:
                continue
            row = dict(base, estimate_m=[_r(v) for v in point], **extra)
            if truth is not None:
                found = match(point, truth)
                if found:
                    row.update(found)
                    if kind == "pixel_plane":
                        centre_z, top_z = found["truth_centre_m"][2], found["truth_top_z_m"]
                        for key, label in (("plane_z_applied_m", "plane_applied"), ("caller_plane_z_m", "caller_plane"),
                                           ("vggt_centre_z_m", "vggt_centre")):
                            if row.get(key) is not None:
                                row[label + "_minus_truth_centre_z_m"] = _r(row[key] - centre_z)
                        if row.get("vggt_top_z_m") is not None and top_z is not None:
                            row["vggt_top_minus_truth_top_z_m"] = _r(row["vggt_top_z_m"] - top_z)
                        if alt is not None:
                            row["alternate_xy_error_m"] = _r(math.hypot(alt[0] - found["truth_centre_m"][0],
                                                                        alt[1] - found["truth_centre_m"][1]))
                    if kind == "object_table":
                        add_table_errors(row, found)
            rows.append(row)
    return rows


def table_points(columns, rows, source):
    """(centre, extra, None) per object-table row; columns by name (v1..v5: optional trailing 'label')."""
    columns = list(columns or [])
    out = []
    for item in rows or []:
        if not isinstance(item, (list, tuple)):
            continue
        entry = dict(zip(columns, item))
        extra = dict(plane_source=source, object_id=entry.get("id"), ot_top_z_m=entry.get("top_z"),
                     ot_bottom_z_m=entry.get("bottom_z"), ot_quality=entry.get("quality"), ot_support=entry.get("support"))
        if "label" in entry:
            extra["ot_label"] = entry.get("label")
        out.append((_vec(entry.get("centre_m")), extra, None))
    return out


def add_table_errors(row, found):
    if isinstance(row.get("ot_top_z_m"), (int, float)) and found["truth_top_z_m"] is not None:
        row["ot_top_minus_truth_top_z_m"] = _r(row["ot_top_z_m"] - found["truth_top_z_m"])


def _reply_tables(payload):
    """Attached obs3d parts in a reply's wire_response: [(observation_id, table or 'unavailable: ...')]."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            text = node.get("text")
            if node.get("type") == "inputText" and isinstance(text, str) and '"object_table"' in text:
                try:
                    part = json.loads(text)
                except ValueError:
                    part = None
                if isinstance(part, dict) and "object_table" in part and part.get("observation_id"):
                    found.append((part["observation_id"], part["object_table"]))
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk((payload or {}).get("wire_response"))
    return found


def obs3d_tables(episode):
    """Attached tables in delivery order: (source, attachments); each attachment is a dict
    {observation_id, frame_ids, reply, reused, table, tool_result_id, role}."""
    roles, results = {}, []
    for identifier, record in episode.reader.records("tool_result"):
        call = (record.get("correlation") or {}).get("tool_call_id")
        if call:
            roles[call] = record.get("role")
        if record.get("tool") in OBS3D_TOOLS:
            results.append((identifier, record))
    events = episode.reader.events(OBS3D_EVENT)
    if events:
        out = []
        for event in events:
            body = event.get("payload") or {}
            table = (dict(columns=body.get("columns"), rows=body.get("rows"), table_z=body.get("table_z"))
                     if body.get("available") else body.get("unavailable") or "unavailable")
            correlation = event.get("correlation") or {}
            out.append(dict(observation_id=body.get("observation_id"), frame_ids=body.get("frame_ids") or [],
                            reply=body.get("reply"), reused=bool(body.get("reused")), table=table,
                            tool_result_id=None, role=roles.get(correlation.get("tool_call_id"))))
        return "event", out
    out = []
    for identifier, record in results:
        payload = episode.reader.artifact_json(record.get("raw_ref"))
        for observation_id, table in _reply_tables(payload):
            out.append(dict(observation_id=observation_id, frame_ids=[],
                            reply="observe" if record.get("tool") == "robot_observe" else "action", reused=None,
                            table=table, tool_result_id=identifier, role=record.get("role")))
    return ("reply_text" if out else None), out


def obs3d_rows(episode):
    """kind object_table_obs: one scored table per observation; returns (rows, delivery summary)."""
    source, attachments = obs3d_tables(episode)
    first, deliveries = {}, {}
    for item in attachments:
        key = item["observation_id"]
        deliveries[key] = deliveries.get(key, 0) + 1
        if key not in first or (not isinstance(first[key]["table"], dict) and isinstance(item["table"], dict)):
            first[key] = item
    rows = []
    for key, item in first.items():
        table = item["table"]
        if not isinstance(table, dict):
            continue
        truth, joined = episode.truth_for(item["frame_ids"], [key])
        base = dict(tool_result_id=item["tool_result_id"], kind="object_table_obs", role=item["role"], status="conditional",
                    observation_id=key, camera_ids=["cam_head"], truth_join=joined,
                    truth_sim_step=truth.get("sim_step") if truth else None, program_id=None, obs3d_reply=item["reply"],
                    obs3d_source=source, obs3d_deliveries=deliveries[key])
        for point, extra, _ in table_points(table.get("columns"), table.get("rows"), "object_table_obs"):
            if point is None:
                continue
            row = dict(base, estimate_m=[_r(v) for v in point], **extra)
            if truth is not None:
                found = match(point, truth)
                if found:
                    row.update(found)
                    add_table_errors(row, found)
            rows.append(row)
    summary = dict(source=source, attachments=len(attachments), observations=len(first),
                   unavailable_observations=sum(1 for item in first.values() if not isinstance(item["table"], dict)),
                   reused_deliveries=sum(1 for item in attachments if item["reused"]) if source == "event" else None,
                   scored_rows=len(rows))
    return rows, summary


# ------------------------------------------------------------------ grasp targets from receipts
def _goal(stage):
    if isinstance(stage.get("goal_normalized"), (int, float)):
        return float(stage["goal_normalized"])
    for event in ((stage.get("action") or {}).get("gripper_events") or []):
        if isinstance(event.get("goal_normalized"), (int, float)):
            return float(event["goal_normalized"])
    return None


def grasp_stage_ids(request):
    """Move stages followed on the same arm by a closing gripper stage, or closing after their own motion."""
    sequences = {}
    if isinstance(request.get("stages"), list):
        for stage in request["stages"]:
            if isinstance(stage, dict):
                sequences.setdefault(stage.get("arm"), []).append(stage)
    for arm, stages in (request.get("arm_queues") or {}).items():
        sequences.setdefault(arm, []).extend(s for s in stages if isinstance(s, dict))
    grasps = set()
    for arm, stages in sequences.items():
        for index, stage in enumerate(stages):
            if stage.get("kind") not in ("move", None) or stage.get("kind") == "gripper":
                continue
            own = [e for e in ((stage.get("action") or {}).get("gripper_events") or [])
                   if e.get("arm") in (arm, None) and isinstance(e.get("goal_normalized"), (int, float))]
            if any(e["goal_normalized"] <= CLOSE_GOAL for e in own):
                grasps.add(stage.get("id"))
                continue
            following = next((s for s in stages[index + 1:] if s.get("kind") in ("gripper", "move")), None)
            if following is not None and following.get("kind") == "gripper":
                goal = _goal(following)
                if goal is not None and goal <= CLOSE_GOAL:
                    grasps.add(stage.get("id"))
    return grasps


def _contact_targets(value, path=()):
    if isinstance(value, dict):
        if "requested_contact_position_m" in value:
            yield path, value
        for key, item in value.items():
            yield from _contact_targets(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _contact_targets(item, path + (index,))


def grasp_rows(episode):
    requests = dict(episode.reader.records("action_request"))
    rows = []
    for action_id, receipt in episode.reader.records("action_receipt"):
        request = requests.get(action_id) or {}
        grasps = grasp_stage_ids(request)
        single_close = any(isinstance(e, dict) and isinstance(e.get("goal_normalized"), (int, float))
                           and e["goal_normalized"] <= CLOSE_GOAL for e in request.get("gripper_events") or [])
        start = receipt.get("execution_start_observation_id") or request.get("based_on_observation_id")
        truth, joined = episode.truth_for((), [start] if start else [])
        seen = set()
        for path, entry in _contact_targets(receipt):
            stage = path[path.index("stage_results") + 1] if "stage_results" in path and path.index("stage_results") + 1 < len(path) else None
            arm = path[path.index("arms") + 1] if "arms" in path and path.index("arms") + 1 < len(path) else None
            target = _vec(entry.get("requested_contact_position_m"))
            if target is None or (stage, arm) in seen:
                continue
            seen.add((stage, arm))
            row = dict(action_id=action_id, stage_id=stage, arm=arm, receipt_status=receipt.get("status"),
                       is_grasp=(stage in grasps) if stage is not None else single_close,
                       target_m=[_r(v) for v in target], truth_join=joined,
                       truth_sim_step=truth.get("sim_step") if truth else None,
                       arrival_error_m=_r(entry.get("position_error_m")) if entry.get("available") else None)
            if truth is not None:
                found = match(target, truth)
                if found:
                    row.update(found)
            rows.append(row)
    return rows


def leak_audit(episode):
    hits = dict(tool_call_arguments=0, program_sources=0, markers=list(LEAK_MARKERS))
    for _, call in episode.reader.records("tool_call"):
        text = json.dumps(call.get("params", {}))
        if any(marker in text for marker in LEAK_MARKERS):
            hits["tool_call_arguments"] += 1
    workspaces = episode.root / "private" / "program-workspaces"
    try:
        for source in workspaces.glob("*/*.py"):
            try:
                text = source.read_text(errors="replace")
            except OSError:
                continue
            if any(marker in text for marker in LEAK_MARKERS):
                hits["program_sources"] += 1
    except OSError:
        hits["program_sources"] = None
    return hits


# ------------------------------------------------------------------ aggregation and output
def stats(rows, key, absolute=True):
    values = [abs(r[key]) if absolute else r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return dict(n=len(values), p50=_r(percentile(values, .5)), p90=_r(percentile(values, .9)))


METRICS = ("xy_error_m", "z_error_centre_m", "z_error_top_m", "plane_applied_minus_truth_centre_z_m",
           "caller_plane_minus_truth_centre_z_m", "vggt_centre_minus_truth_centre_z_m", "vggt_top_minus_truth_top_z_m",
           "ot_top_minus_truth_top_z_m")


def summarize(rows, metrics=METRICS):
    out = dict(n=len(rows), matched=sum(1 for r in rows if r.get("object")),
               unambiguous=sum(1 for r in rows if r.get("unambiguous")),
               inside_footprint=sum(1 for r in rows if r.get("inside_xy_footprint")))
    for metric in metrics:
        out[metric] = stats(rows, metric)
    signed = [r["z_error_centre_m"] for r in rows if isinstance(r.get("z_error_centre_m"), (int, float))]
    out["z_error_centre_signed_p50_m"] = _r(percentile(signed, .5))
    return out


def group(rows, key):
    groups = {}
    for row in rows:
        groups.setdefault(str(row.get(key)), []).append(row)
    return {name: summarize(items) for name, items in sorted(groups.items())}


def score_run(root):
    episode = Episode(root)
    try:
        queried, (observed, obs3d) = geometry_rows(episode), obs3d_rows(episode)
        geometry, grasp = queried + observed, grasp_rows(episode)
        grasp_only = [r for r in grasp if r.get("is_grasp")]
        # all / by_role / unambiguous_only: geometry_query calls only (same meaning as before obs3d);
        # by_kind / by_plane_source also list the attached tables (object_table_obs)
        result = dict(run_dir=str(Path(root)), truth=episode.truth_info, n_observations=len(episode.observations),
                      geometry=dict(all=summarize(queried), by_kind=group(geometry, "kind"),
                                    by_plane_source=group(geometry, "plane_source"), by_role=group(queried, "role"),
                                    unambiguous_only=summarize([r for r in queried if r.get("unambiguous")])),
                      grasp_targets=dict(grasp=summarize(grasp_only, ("xy_error_m", "z_error_centre_m", "z_error_top_m")),
                                         all_contact_targets=summarize(grasp, ("xy_error_m", "z_error_centre_m", "z_error_top_m"))),
                      obs3d=obs3d, leak_audit=leak_audit(episode), geometry_rows=geometry, grasp_rows=grasp)
        if episode.truth_info is None:
            result["warning"] = "no private/scene_truth.jsonl (run without analysis.scene_truth_log or private/ unreadable)"
        return result
    finally:
        episode.reader.close()


def find_runs(paths, max_depth=4):
    runs = []
    for path in paths:
        path = Path(path)
        if (path / "events.sqlite").exists():
            runs.append(path)
            continue
        base = len(path.parts)
        for directory, directories, files in os.walk(path):
            depth = len(Path(directory).parts) - base
            directories[:] = sorted(d for d in directories if d not in ("private", "blobs", "scene", "actor")
                                    and depth < max_depth)
            if "events.sqlite" in files:
                runs.append(Path(directory))
                directories[:] = []
    return runs


def _fmt(stat):
    return "-" if not stat or not stat.get("n") else f"{stat['p50']:.3f} / {stat['p90']:.3f} ({stat['n']})"


def markdown(results):
    lines = ["# Geometry estimates vs simulator truth (analysis only)", "",
             "Values: p50 / p90 of |error| in metres (n). xy = nearest task-object centre in xy. "
             "Groups all / role=: geometry_query calls only; kind=object_table_obs / plane=object_table_obs: tables "
             "attached to observation replies (one per observation).", ""]
    head = ("| episode | group | n | unamb. | in footprint | xy | z vs centre | z vs top | applied plane - centre z "
            "| caller plane - centre z | VGGT centre - centre z | VGGT top - top z | table top - top z |")
    lines += [head, "|" + "---|" * (5 + len(METRICS))]
    for result in results:
        name = Path(result["run_dir"]).name
        geometry = result["geometry"]
        groups = [("all", geometry["all"])] + [("kind=" + k, v) for k, v in geometry["by_kind"].items()] + \
                 [("plane=" + k, v) for k, v in geometry["by_plane_source"].items()] + \
                 [("role=" + k, v) for k, v in geometry["by_role"].items()]
        for label, s in groups:
            lines.append(f"| {name} | {label} | {s['n']} | {s['unambiguous']} | {s['inside_footprint']} | "
                         + " | ".join(_fmt(s.get(m)) for m in METRICS) + " |")
    lines += ["", "## Executed contact targets (requested_contact_position_m) vs truth at action start", "",
              "| episode | set | n | unamb. | in footprint | xy | z vs centre | z vs top |", "|" + "---|" * 8]
    for result in results:
        name = Path(result["run_dir"]).name
        for label, s in result["grasp_targets"].items():
            lines.append(f"| {name} | {label} | {s['n']} | {s['unambiguous']} | {s['inside_footprint']} | "
                         + " | ".join(_fmt(s.get(m)) for m in ("xy_error_m", "z_error_centre_m", "z_error_top_m")) + " |")
    lines += ["", "## Attached object tables (features.object_table_observe, kind object_table_obs)", "",
              "| episode | source | attachments | observations | unavailable | reused deliveries | scored rows |",
              "|---|---|---|---|---|---|---|"]
    for result in results:
        obs = result.get("obs3d") or {}
        if obs.get("attachments"):
            lines.append(f"| {Path(result['run_dir']).name} | {obs['source']} | {obs['attachments']} | {obs['observations']} | "
                         f"{obs['unavailable_observations']} | {'-' if obs['reused_deliveries'] is None else obs['reused_deliveries']} | "
                         f"{obs['scored_rows']} |")
    lines += ["", "## Truth log and leak audit", "", "| episode | truth lines | observations | tool calls naming truth | program sources naming truth |",
              "|---|---|---|---|---|"]
    for result in results:
        truth = result.get("truth") or {}
        audit = result["leak_audit"]
        lines.append(f"| {Path(result['run_dir']).name} | {truth.get('lines', 'missing')} | {result['n_observations']} | "
                     f"{audit['tool_call_arguments']} | {audit['program_sources']} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--out-json")
    parser.add_argument("--out-md")
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--exclude", action="append", default=None,
                        help="object category/name never matched (default: camera_stand); repeatable")
    args = parser.parse_args(argv)
    if args.exclude is not None:
        EXCLUDE_CATEGORIES.clear()
        EXCLUDE_CATEGORIES.update(args.exclude)
    runs = find_runs(args.paths, args.max_depth)
    if not runs:
        print("no run directories (events.sqlite) found", file=sys.stderr)
        return 2
    results = [score_run(run) for run in runs]
    pooled_geometry = [row for r in results for row in r["geometry_rows"]]
    pooled_queried = [row for row in pooled_geometry if row.get("kind") != "object_table_obs"]
    pooled_grasp = [row for r in results for row in r["grasp_rows"] if row.get("is_grasp")]
    document = dict(schema="auriga-geometry-truth-score-v2", runs=results,
                    pooled=dict(geometry=summarize(pooled_queried), by_kind=group(pooled_geometry, "kind"),
                                by_plane_source=group(pooled_geometry, "plane_source"),
                                grasp=summarize(pooled_grasp, ("xy_error_m", "z_error_centre_m", "z_error_top_m"))))
    text = markdown(results)
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(document, indent=1))
    if args.out_md:
        Path(args.out_md).write_text(text)
        print(f"scored {len(results)} run(s): {args.out_md}" + (f", {args.out_json}" if args.out_json else ""))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
