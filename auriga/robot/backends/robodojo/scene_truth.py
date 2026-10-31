"""analysis.scene_truth_log (off by default): simulator ground truth for OFFLINE geometry scoring only.

ANALYSIS-ONLY, PRIVILEGED. With the switch on, the RoboDojo worker process calls
:meth:`SceneTruthRecorder.record` once per actor-visible observation capture (``RoboDojoAdapter.observe``
without ``video_only``: robot_observe, action start/progress/terminal observations, program
observations). Each call appends one JSON line to ``<run>/private/scene_truth.jsonl`` and returns
nothing; the worker discards the call's value and never puts any of it into the observation, the
receipt, the IPC reply or any other message. No harness, tool, program, library or payload code reads
the file; only ``scripts/score_geometry_truth.py`` (offline, after the run) and the validation probe
``scripts/probes/scene_truth_probe.py`` do. ``tests/test_scene_truth.py`` enforces that no other module in
``auriga/`` refers to it, and ``observation_policy.PRIVILEGED_FIELDS`` rejects a ``scene_truth`` key
in any actor-facing payload.

The switch does not change the observation profile (``rgb_proprio_calibrated_v1`` stays as configured),
the actor tool catalog, instructions, observations or any model-facing byte.

One line per capture::

    {"schema": "auriga-scene-truth-v1", "audience": "analysis_only", "raw_observation_id": "raw-...",
     "sim_step": n, "step_domain": "adapter_control_step", "sim_time_s": t, "wall_time": unix,
     "frame_id": "env_origin",
     "objects": [{"name", "inst", "type", "category", "model_id", "labelled",
                  "position_m", "quaternion_xyzw",            # object root pose (LayoutManager.get_instance_pose)
                  "centroid_m", "top_z_m", "bottom_z_m",       # from the root-frame bounding box, when known
                  "bbox_local_min_m", "bbox_local_max_m", "bbox_size_m", "bbox_source"}],
     "table": {"inst", "top_z_m", "pos", "size"},              # LayoutManager.table_info when present
     "robot": {arm: {"link6_position_m", "link6_quaternion_xyzw", "grasp_center_m", "gripper_position_m"}},
     "errors": [...]}

Frames: env-origin frame, the frame of camera calibration and of every geometry_query result
(``get_instance_pose(relative=True)``; camera poses subtract ``env_origins[0]`` the same way). Robot
values are the measured link6 pose already in the observation and the grasp centre
``p_link6 + R(q) @ [GRASP_POINT_OFFSET_M, 0, 0]``.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import time

FILENAME = "scene_truth.jsonl"
SCHEMA = "auriga-scene-truth-v1"
OBJECT_TYPES = ("Rigid", "Articulation", "Dynamic", "Geometry", "Garment")


def truth_path(run_dir):
    """``<run>/private/scene_truth.jsonl`` for a worker run directory (normally ``<run>/private/robot``)."""
    run_dir = Path(run_dir)
    if run_dir.name == "robot" and run_dir.parent.name == "private":
        return run_dir.parent / FILENAME
    if run_dir.name == "private":
        return run_dir / FILENAME
    return run_dir / "private" / FILENAME


def _host(value):
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    if hasattr(value, "tolist"):
        value = value.tolist()
    return value


def _flat(value):
    value = _host(value)
    out = []
    stack = [value]
    while stack:
        item = stack.pop(0)
        if isinstance(item, (list, tuple)):
            stack[0:0] = list(item)
        else:
            out.append(item)
    return out


def _vec(value, n):
    try:
        items = [float(x) for x in _flat(value)]
    except (TypeError, ValueError):
        return None
    if len(items) < n or not all(math.isfinite(x) for x in items[:n]):
        return None
    return [round(x, 6) for x in items[:n]]


def quat_matrix_wxyz(q):
    w, x, y, z = (float(v) for v in q)
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]]


def _apply(r, p, v):
    return [p[i] + sum(r[i][j] * v[j] for j in range(3)) for i in range(3)]


def _usd_bbox_root_frame(obj):  # pragma: no cover - simulator only
    """Axis-aligned box of all mesh points under the object's root prim, in the root's unscaled frame
    (scale included). Same method as RoboDojo GPT_FluxVLA scene_state.py."""
    try:
        import numpy as np
        import omni.usd  # type: ignore
        from pxr import Usd, UsdGeom  # type: ignore
    except Exception:
        return None
    stage = getattr(obj, "stage", None)
    root_path = getattr(obj, "_prim_path", None) or getattr(obj, "prim_path", None)
    if stage is None or not root_path:
        return None
    root = stage.GetPrimAtPath(str(root_path))
    if not root or not root.IsValid():
        return None
    m_root = np.array(omni.usd.get_world_transform_matrix(root), dtype=np.float64)
    basis = m_root[:3, :3]
    norms = np.linalg.norm(basis, axis=1)
    if np.any(norms < 1e-9):
        return None
    r_rows, t, parts = basis / norms[:, None], m_root[3, :3], []
    for prim in Usd.PrimRange(root):
        if not prim.IsA(UsdGeom.Mesh):
            continue
        pts = UsdGeom.Mesh(prim).GetPointsAttr().Get()
        if pts is None or len(pts) == 0:
            continue
        p = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        m = np.array(omni.usd.get_world_transform_matrix(prim), dtype=np.float64)
        pw = np.c_[p, np.ones(len(p))] @ m
        parts.append((pw[:, :3] - t) @ r_rows.T)
    if not parts:
        return None
    allp = np.concatenate(parts, axis=0)
    return allp.min(axis=0).tolist(), allp.max(axis=0).tolist()


class SceneTruthRecorder:
    """Reads object poses from a RoboDojo env and appends one line per observation capture.

    Every read is guarded; a failure becomes an ``errors`` entry and never raises into the worker.
    ``record`` returns None so nothing can flow back into an observation or a reply."""

    def __init__(self, env, path, *, grasp_offset_m=None, usd_bbox=_usd_bbox_root_frame, env_idx=0):
        self.env, self.path, self.env_idx = env, Path(path), int(env_idx)
        if grasp_offset_m is None:
            from ...model.roboprobe_pose import GRASP_POINT_OFFSET_M
            grasp_offset_m = GRASP_POINT_OFFSET_M
        self.grasp_offset_m = float(grasp_offset_m)
        self.usd_bbox = usd_bbox
        self._bbox = {}
        self.n_written = 0
        self.last_error = None

    @property
    def layout(self):
        manager = getattr(self.env, "scene_manager", None)
        return getattr(manager, "layout_manager", None) if manager is not None else getattr(self.env, "layout_manager", None)

    def _records(self, layout):
        by_type = getattr(layout, "object_records_by_type", None) or {}
        for kind in OBJECT_TYPES:
            group = by_type.get(kind)
            if group is None:
                continue
            records = group.layout_records_by_env[self.env_idx]
            metadata = group.metadata_by_env[self.env_idx] if self.env_idx < len(getattr(group, "metadata_by_env", [])) else {}
            for record in records:
                yield kind, dict(record), dict((metadata or {}).get(record.get("inst_name"), {}) or {})

    def _box(self, layout, inst, record, meta):
        if inst in self._bbox:
            return self._bbox[inst]
        box = None
        try:
            obj = layout.get_scene_object(self.env_idx, inst) if hasattr(layout, "get_scene_object") else None
            got = self.usd_bbox(obj) if (obj is not None and self.usd_bbox is not None) else None
            if got is not None:
                box = dict(lo=[float(v) for v in got[0]], hi=[float(v) for v in got[1]], source="usd_mesh")
        except Exception:
            box = None
        if box is None:
            vertices = ((meta.get("geometry") or {}).get("oriented_bbox") or {}).get("vertices")
            if vertices is not None:
                try:
                    flat = [float(v) for v in _flat(vertices)]
                    points = [flat[i:i + 3] for i in range(0, len(flat) - 2, 3)]
                    scale = record.get("scale")
                    scale = [float(v) for v in _flat(scale)] if scale is not None else [1.0]
                    scale = scale * 3 if len(scale) == 1 else scale[:3]
                    points = [[p[i] * scale[i] for i in range(3)] for p in points]
                    box = dict(lo=[min(p[i] for p in points) for i in range(3)],
                               hi=[max(p[i] for p in points) for i in range(3)], source="metadata_oriented_bbox")
                except Exception:
                    box = None
        self._bbox[inst] = box
        return box

    def objects(self, errors):
        layout = self.layout
        if layout is None:
            errors.append("layout_manager_unavailable")
            return []
        out, names = [], set()
        for kind, record, meta in self._records(layout):
            inst = record.get("inst_name")
            if not inst:
                continue
            label = record.get("label")
            name = str(label) if label else str(inst)
            if name in names:
                name = f"{name}#{inst}"
            names.add(name)
            item = dict(name=name, inst=str(inst), type=kind.lower(), labelled=bool(label),
                        category=meta.get("model_name") or record.get("category"), model_id=meta.get("model_id"))
            p = q = None
            try:
                position, quaternion = layout.get_instance_pose(env_idx=self.env_idx, inst_name=inst)
                p, q = _vec(position, 3), _vec(quaternion, 4)   # quaternion is (w, x, y, z)
            except Exception as error:
                errors.append(f"pose:{inst}:{type(error).__name__}:{str(error)[:120]}")
            item["position_m"] = p
            item["quaternion_xyzw"] = [q[1], q[2], q[3], q[0]] if q else None
            box = self._box(layout, inst, record, meta)
            if box is not None:
                item.update(bbox_local_min_m=[round(v, 6) for v in box["lo"]],
                            bbox_local_max_m=[round(v, 6) for v in box["hi"]],
                            bbox_size_m=[round(box["hi"][i] - box["lo"][i], 6) for i in range(3)],
                            bbox_source=box["source"])
                if p is not None and q is not None:
                    r = quat_matrix_wxyz(q)
                    lo, hi = box["lo"], box["hi"]
                    centre = _apply(r, p, [(lo[i] + hi[i]) / 2 for i in range(3)])
                    zs = [_apply(r, p, [x, y, z])[2] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
                    item.update(centroid_m=[round(v, 6) for v in centre],
                                top_z_m=round(max(zs), 6), bottom_z_m=round(min(zs), 6))
            out.append(item)
        return out

    def table(self, errors):
        layout = self.layout
        try:
            info = (getattr(layout, "table_info", None) or [{}])[self.env_idx] or {}
            tables = (getattr(layout, "tables", None) or [[]])[self.env_idx] or []
        except Exception as error:
            errors.append(f"table:{type(error).__name__}")
            return None
        if not info and not tables:
            return None
        height = info.get("height")
        return dict(inst=str(tables[0]) if tables else None,
                    top_z_m=round(float(height), 6) if isinstance(height, (int, float)) else None,
                    pos=_vec(info.get("pos"), 3), size=_vec(info.get("size"), 4),
                    source="LayoutManager.table_info (configured pose and scale)")

    def robot(self, robot_state):
        out = {}
        for arm, state in ((robot_state or {}).get("arms") or {}).items():
            pose = (state or {}).get("ee_pose") or {}
            p, qxyzw = _vec(pose.get("position_m"), 3), _vec(pose.get("quaternion_xyzw"), 4)
            entry = dict(link6_position_m=p, link6_quaternion_xyzw=qxyzw, tcp_config_id=pose.get("tcp_config_id"))
            if p is not None and qxyzw is not None:
                r = quat_matrix_wxyz([qxyzw[3], qxyzw[0], qxyzw[1], qxyzw[2]])
                entry["grasp_center_m"] = [round(v, 6) for v in _apply(r, p, [self.grasp_offset_m, 0.0, 0.0])]
            gripper = (state or {}).get("gripper") or {}
            if gripper.get("position_m") is not None:
                try:
                    entry["gripper_position_m"] = [round(float(v), 6) for v in _flat(gripper["position_m"])]
                except (TypeError, ValueError):
                    pass
            out[arm] = entry
        return out

    def record(self, raw_observation_id, sim_step, robot_state=None, sim_time_s=None):
        """Append one line. Never raises and returns None (write-only)."""
        try:
            errors = []
            line = dict(schema=SCHEMA, audience="analysis_only", raw_observation_id=raw_observation_id,
                        sim_step=sim_step, step_domain="adapter_control_step", sim_time_s=sim_time_s,
                        wall_time=round(time.time(), 3), frame_id="env_origin", env_idx=self.env_idx)
            try:
                line["objects"] = self.objects(errors)
            except Exception as error:
                line["objects"] = []
                errors.append(f"objects:{type(error).__name__}:{str(error)[:200]}")
            line["table"] = self.table(errors)
            try:
                line["robot"] = self.robot(robot_state)
            except Exception as error:
                line["robot"] = {}
                errors.append(f"robot:{type(error).__name__}")
            line["errors"] = errors
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(line, default=str, allow_nan=False) + "\n")
            self.n_written += 1
        except Exception as error:  # never break the simulator worker
            self.last_error = f"{type(error).__name__}: {error}"[:300]
        return None


def make_recorder(path, env, **kwargs):
    """Recorder for the worker when the parent passed ``--scene-truth-log PATH``; else None."""
    if not path:
        return None
    return SceneTruthRecorder(env, path, **kwargs)
