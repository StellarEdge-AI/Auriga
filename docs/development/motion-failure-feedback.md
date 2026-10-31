# Motion Planning Failure Feedback

When a `move` stage in `robot_action` (or the legacy `robot_move_grasp` tool, removed in 2026-10) fails with `path_infeasible`, the end-effector execution backend returns structured diagnostics in `receipt.failure_feedback` (and provides the same fields on failed previews). This feedback is persisted with the original receipt and delivered directly across `full`, `lean`, `compact`, and program replies so the model never needs access to private planner logs.

Existing fields—including `planner_failure_stage`, `failed_waypoint_index`, executed step counts, `execution_interval`, completed `waypoint_results`, trajectories, and terminal observations—remain unchanged. Because a planning failure on a later waypoint can occur after earlier segments have already moved the robot, never infer zero physical motion from `path_infeasible` alone.

## Real-Hardware Information Boundary

Failure feedback exposes only caller request parameters, numerical solver outputs that a real-robot motion planner can likewise report, and the configured public robot kinematic/collision model. When no real-hardware sensor, calibration, or caller-supplied environment geometry is present, the feedback never leaks simulated obstacle positions, ground-truth tabletop coordinates, physical contact forces, simulator base poses, or world-frame diagnostics derived from them. `information_sources` explicitly lists which sources were used and which were unavailable.

## Fields Delivered to the Model

| Field | Meaning |
| --- | --- |
| `waypoint_index`, `waypoint_role` | Zero-based index of the failed waypoint and whether it is an `"intermediate"` waypoint or the `"final"` target |
| `requested_grasp_pose` | Caller's requested grasp contact center and `angles_deg` at that waypoint (present for grasp-pose requests) |
| `requested_ee_pose` | Converted `link6` target pose after applying the tool offset, expressed in `frame_id` |
| `solver_summary` | Per-stage solver call counts, native solver status codes, feasible candidate counts, and minimum position/orientation residuals across returned seeds (numerical solver outputs, not measured physical contact) |
| `candidates` | One representative candidate per IK / trajectory-optimization stage, recording `stage_call_index`, seed index, that candidate's exact position/orientation residuals, and terminal joint values |
| `constraint_witness` | Checks against configured URDF joint limits, dynamic limits, and the robot self-collision sphere model (explicitly marked when unavailable) |
| `cause_status` | Currently `"undetermined"`, since solver stage summaries and discrete candidate witnesses do not prove a single unique root cause |

For example, if an intermediate waypoint at `grasp_position_m=[-0.22, -0.08, 1.08]`, `angles_deg=[0, 0, 0]` fails planning, `failure_feedback` explicitly labels it as `"intermediate"` and shows the converted `link6` target height of `1.2301 m`—making clear that the intermediate waypoint failed rather than the final grasp target.

Candidate buffers are copied immediately upon each native solver return before subsequent CUDA graph replays can overwrite them. Within each stage, the selected candidate is the seed minimizing the maximum normalized end-effector residual; while independent minimum position and orientation errors in `solver_summary` may come from different seeds, the position error, orientation error, joint configuration, and constraint witness inside a `candidates` entry always belong to the **same single seed**. `stage_call_index` is zero-based within that solver stage rather than a global retry counter across stages, and `selection_normalization` distinguishes native solver tolerances from diagnostic ranking scales.

Because native `js_solution` tensors can include locked gripper joints and permute column order, feedback strictly maps values by native `active_joints` names and excludes metric finger joints; if joint names are missing or tensor shapes mismatch, only verified Cartesian residuals are reported rather than fabricating joint diagnostics. Velocities, accelerations, and jerks are read directly from the seed's native derivative tensors rather than estimated via finite differences.

## Joint Limit and Self-Collision Diagnostics

Joint position violation records report the joint name, trajectory sample index, joint value, lower/upper bounds, nearest bound, and violation margin. Joints within `0.05 rad` of a bound without violating it are reported separately under `joints_near_limits` (being near a limit is advisory and not itself a failure). Dynamic constraint violations report the offending joint, sample index, native velocity/acceleration/jerk value, and configured limit.

Self-collision diagnostics evaluate only the configured robot collision spheres, link pairs, and padding, reporting the two colliding links, sphere indices, sphere centers, and penetration depth (retaining up to the three worst violations per category alongside the omitted count). Computing these witnesses performs only candidate forward kinematics and self-collision sphere checks—never replanning, stepping physics, or querying the environment scene model.

All candidate forward kinematics and self-collision sphere centers are expressed in `planner_base`. Because this diagnostic path does not assume a live world-frame base calibration source, it never reads `robot.entity_origin_pose` and omits `T_world_planner_base`, world-frame candidate poses, and world-frame residual vectors. The caller's `requested_ee_pose` remains in the requested `frame_id` and must not be subtracted directly from `planner_base` candidate coordinates.

Similarly, the diagnostic path ignores static tabletop obstacles injected into `planner.scene_model` by the simulation config, omitting static obstacle IDs, surface points, contact locations, and penetration depths. `environment_geometry_unavailable_reason` and `world_frame_transform_unavailable_reason` document why those fields are absent; any future environment-collision diagnostics must derive from permitted perception, calibration, or caller-supplied geometry rather than hidden simulator configs.

Collision-sphere overlap is an approximate model check rather than physical contact, and the absence of discrete sample overlap does not prove continuous-time feasibility. Likewise, `solver_feasible=false` reflects the native planner's internal constraint evaluation (which may include its internal static table model) and cannot by itself distinguish self-collision, environment collision, or joint-limit failure. Raw `planning_diagnostics`, ground-truth object states, and evaluator flags remain excluded from model replies.

## Matching Information Boundary in Motion Preview

Native motion preview likewise withholds `planner_scene_model` and never reads simulator base configs to construct public geometry. When permitted proprioception is complete, preview derives `T_world_base = T_world_ee @ inverse(T_base_ee)` from public named joint measurements, the measured `link6` pose, and the public URDF kinematic model, tagging the result with `derived_from_public_proprioception_and_robot_model`. This reuses already disclosed robot state without adding hidden environment geometry. If public proprioception or the kinematic model is incomplete, `preview_geometry.available=false` is returned without falling back to simulator ground truth (the compiled trajectory can still be cached while geometric clearance review reports unavailable). Environmental clearance estimates rely exclusively on caller-supplied proxy bounding boxes or permitted 3D scene reconstructions.

## Validation

CPU regressions cover solver hooks, tensor buffer copying, seed and joint-name alignment, real-hardware information boundaries, partial execution preservation, joint and sphere-model witness extraction, graceful degradation on missing data, and feedback preservation across payload profiles.

Historically, an end-to-end native GPU probe (`scripts/motion_failure_probe.py`, removed in 2026-10 alongside `robot_move_grasp`) verified live solver diagnostics on an idle GPU:

```bash
python scripts/motion_failure_probe.py \
  --config /path/to/working-config.json \
  --run-dir /path/to/fresh-validation-directory \
  --gpu 1
```

That probe tested a recorded intermediate waypoint, a direct target, and an unreachable far target—verifying that the unreachable target failed with zero executed steps, returned native candidate diagnostics in the model-facing receipt, and persisted cleanly to `Store` without invoking a model or claiming task-level success.
