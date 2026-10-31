# Ground-truth log for geometry scoring (analysis only)

`analysis.scene_truth_log` (default `false`) makes the RoboDojo worker append the simulator's object
poses to `<run>/private/scene_truth.jsonl` at every actor-visible observation capture (robot_observe,
action start / progress / terminal observations, program observations; not the 5 Hz video-only frames).
It exists so that geometry estimates (pixel_plane with or without VGGT, triangulate) and executed grasp
targets can be scored offline. It never reaches the model.

## What is written

One JSON line per capture: `raw_observation_id` (the `raw-...` prefix of every camera `frame_id` of
that observation), `sim_step`, `sim_time_s`, `wall_time`, `frame_id: env_origin`, and

- `objects[]`: every layout object (Rigid / Articulation / Dynamic / Geometry / Garment): `name`
  (task label, else instance name), `inst`, `type`, `category`, `position_m` and `quaternion_xyzw`
  (root pose, `LayoutManager.get_instance_pose(relative=True)`), `bbox_local_min_m/max_m`,
  `bbox_size_m` (USD mesh box in the root frame, else the asset metadata box), `centroid_m`,
  `top_z_m`, `bottom_z_m` (corners of the rotated local box);
- `table`: `LayoutManager.table_info` (`top_z_m` = 0.765 in the RoboDojo layouts);
- `robot`: the measured link6 pose already in the observation, the grasp centre
  (`p_link6 + R @ [0.1501, 0, 0]`) and the gripper joint positions;
- `errors`: accessor failures (the writer never raises into the worker).

## Isolation

- The worker calls `SceneTruthRecorder.record(...)` as a bare statement; it returns `None`, and
  nothing is merged into the observation, receipt or IPC reply.
- Only `auriga/robot/backends/robodojo/scene_truth.py`, `robot_worker.py` (call), `robodojo.py` (passes
  `--scene-truth-log PATH`), `config.py` (switch), `observation_policy.py` and
  `auriga/perception/external/obs3d.py` (reads the switch to log the attached, model-visible object table as an
  internal event; no truth) mention it in `auriga/`; only `scripts/score_geometry_truth.py` and
  `scripts/probes/scene_truth_probe.py` read the file. `tests/test_scene_truth.py` enforces both lists.
- `scene_truth` is in `observation_policy.PRIVILEGED_FIELDS`, so any actor payload carrying such a key
  is rejected.
- The switch changes no feature, tool, instruction or observation; the observation profile stays as
  configured.
- With `features.sandbox_enabled: false` (the default; the opt-in sandbox needs a recent Landlock ABI) a model-authored program
  runs without file-system restrictions and could in principle open any file of the run, as it already
  could for `private/robot/evaluations/`. The scorer's leak audit counts tool calls and program
  sources that name the file.

## Scoring

    python scripts/score_geometry_truth.py RUN_OR_PARENT [...] --out-json s.json --out-md s.md

Joins each geometry_query result to the truth of the same capture, matches the estimate to the nearest
task-object centre in xy (fixtures such as `camera_stand` excluded; `unambiguous` when the second
centre is at least 2x and 2 cm farther), and reports xy error, z error vs centre and vs top, the
applied / caller / VGGT plane heights vs truth, and the executed `requested_contact_position_m` grasp
targets vs the truth at the action's start capture. Aggregates: p50 / p90 per episode, tool kind,
plane source and role, plus pooled over all runs in the JSON.

`scripts/probes/scene_truth_probe.py` is a model-free end-to-end check (one environment, no motion).
