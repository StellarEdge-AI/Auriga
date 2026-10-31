# Ground-Truth Scene Logger for Offline Geometry Scoring (`analysis.scene_truth_log`)

Setting `analysis.scene_truth_log=true` (default `false`) instructs the RoboDojo robot worker to append ground-truth simulator object poses to `<run>/private/scene_truth.jsonl` at every actor-visible observation capture (`robot_observe`, action start/progress/terminal observations, and program observations, excluding 5 Hz video-only intermediate frames). This log exists strictly for **offline post-hoc accuracy evaluation** of geometric estimates (`pixel_plane`, `triangulate`, `object_table`) and executed grasp targets. **It never enters model-visible observations, tool responses, or prompts.**

## Logged Record Format

Each capture appends one JSON line containing `raw_observation_id` (matching the `raw-...` prefix of every camera `frame_id` in that observation), `sim_step`, `sim_time_s`, `wall_time`, `frame_id: "env_origin"`, and:

- **`objects[]`**: Every scene object in the layout (`Rigid`, `Articulation`, `Dynamic`, `Geometry`, and `Garment`), recording:
  - `name` (task label or instance name), `inst`, `type`, and `category`
  - `position_m` and `quaternion_xyzw` (root pose from `LayoutManager.get_instance_pose(relative=True)`)
  - `bbox_local_min_m`, `bbox_local_max_m`, and `bbox_size_m` (USD mesh bounding box in the object root frame, or asset metadata bounding box)
  - `centroid_m`, `top_z_m`, and `bottom_z_m` (computed from the rotated local bounding box corners in `env_origin`)
- **`table`**: `LayoutManager.table_info` (`top_z_m = 0.765` in standard RoboDojo layouts).
- **`robot`**: The measured `link6` pose already present in the public observation, the kinematic grasp center ($p_{\text{link6}} + R_{\text{link6}} [0.1501, 0, 0]^T$), and measured gripper joint positions.
- **`errors`**: Any simulator accessor exceptions (the logger catches all exceptions so it never disrupts the worker).

## Strict Information Isolation

- The robot worker invokes `SceneTruthRecorder.record(...)` as a standalone side-effect statement that returns `None`; nothing from the recorder is ever merged into the observation dictionary, action receipt, or worker IPC reply.
- Within `auriga/`, only `auriga/robot/backends/robodojo/scene_truth.py`, `robot_worker.py` (invocation), `robodojo.py` (passing `--scene-truth-log PATH`), `config.py` (configuration flag), `observation_policy.py` (denylist), and `auriga/perception/external/obs3d.py` (which checks the flag only to log the model-visible attached `object_table` as an internal audit event, without reading ground truth) reference `scene_truth`. Only `scripts/score_geometry_truth.py` and `scripts/probes/scene_truth_probe.py` read `scene_truth.jsonl`. `tests/test_scene_truth.py` enforces both allowlists statically.
- `"scene_truth"` is listed in `observation_policy.PRIVILEGED_FIELDS`, so any actor payload containing that key is rejected immediately.
- Enabling `analysis.scene_truth_log` alters no features, tools, prompts, or observation profiles.
- Note that when `features.sandbox_enabled` is `false` (the default when OS Landlock sandboxing is not enabled), model-authored programs run with the host user's filesystem permissions; `scripts/score_geometry_truth.py` includes a leak audit that scans all tool calls and program source code for any reference to `scene_truth`.

## Offline Geometry Scoring

```sh
mamba run -n Auriga python scripts/score_geometry_truth.py RUN_OR_PARENT [...] --out-json s.json --out-md s.md
```

The scorer joins each `geometry_query` result (and each `obs3d` attached object table) to the ground-truth record sharing the same `raw_observation_id`, matches each estimated 3D point or object row to the nearest ground-truth task object center in XY (excluding static fixtures such as `camera_stand`, and marking a match `unambiguous` when the second-closest object is at least $2\times$ and `2 cm` farther away), and reports:
- Horizontal XY error, vertical Z error vs. true object center and vs. true top surface.
- Applied, caller-supplied, and VGGT-estimated plane heights vs. ground truth.
- Executed `requested_contact_position_m` grasp targets vs. ground-truth object positions at the action's start observation.

Summary statistics report p50 and p90 errors broken down by episode, tool kind, plane source, and caller role, as well as pooled across all evaluated runs. `scripts/probes/scene_truth_probe.py` provides a model-free end-to-end verification probe on a single environment without motion.
