# RGB-D Numeric Tools and Bounded Manipulation Skills

Depth perception is disabled by default. The default observation profile remains `rgb_proprio_calibrated_v1`: native capture actively disables depth sensors after merging task configurations, depth tools are hidden from the actor catalog, and neither programs, historical observation readers, nor geometry aliases can bypass the profile gate. The RGB-D configurations below must be explicitly selected by the operator before launching a run; the actor cannot change the active observation profile mid-episode.

## Staged Enablement

| Configuration File | Numeric Tools & Candidates | Automatic Post-Action Target Verification | Grasp / Place Skills | Event-Driven Compact Payloads |
|---|---|---|---|---|
| Default `configs/auriga.json` | Disabled | Disabled | Disabled | Disabled |
| `configs/rgbd-numeric-v1.json` | Enabled | Disabled | Disabled | Disabled |
| `configs/rgbd-feedback-v2.json` | Enabled | Enabled | Disabled | Disabled |
| `configs/rgbd-skills-v3.json` | Enabled | Enabled | Enabled | Enabled |

These three files are opt-in configuration presets and do not replace `configs/auriga.json`. Pass them via `--config` at startup, or set the corresponding keys in a custom configuration file:

```json
{
  "features": {
    "observation_profile": "rgbd_proprio_calibrated_v1",
    "depth_auto_feedback": true,
    "depth_skills": true,
    "depth_event_payload": true
  }
}
```

The corresponding CLI flags are `--observation-profile rgbd_proprio_calibrated_v1`, `--depth-auto-feedback`, `--depth-skills`, and `--depth-event-payload` (each boolean flag also supports `--no-...`). The boolean flags cannot unlock depth on their own: enabling any of them under a non-RGB-D observation profile raises a configuration error, and `depth_event_payload` additionally requires `depth_auto_feedback`. The benchmark queue supports the same fields; the effective configuration is frozen in the run manifest, and resumed sessions honor the archived configuration (defaulting to disabled for older archives lacking these fields).

## Sensor and Numeric Contract

- Native `distance_to_image_plane` provides optical-axis **z-depth in meters** (not Euclidean distance along normalized camera rays). Using the calibrated intrinsics `K`, ROS optical axes (`+x` right, `+y` down, `+z` forward), and `T_world_camera`, points are back-projected directly into the `env_origin` frame.
- `float32` `depth[H, W]` and `bool` `valid[H, W]` arrays are stored in an NPZ artifact. A depth pixel is marked valid if and only if it is finite, strictly positive, and within the camera's near/far clipping range. RGB and depth share the same camera frame ID, native resolution, and observation simulation step.
- Published camera metadata in observation JSON includes only `depth_ref`, `depth_kind: "z"`, `depth_unit: "m"`, `depth_clip_m`, and `depth_shape`. Raw depth matrices are never serialized into observation JSON.
- Depth tools validate the active profile, episode/generation identity, original image dimensions, camera calibration, camera-pose timestamp, artifact integrity, and NPZ array shapes and dtypes. Hidden object poses, contact states, scoring thresholds, and evaluator state remain strictly off-limits to the actor.
- Numeric outputs represent visible-surface measurements or explicit candidate hypotheses. Backside geometry, full occluded dimensions, mass distribution, contact forces, grasp security, and long-term stability remain unknown. Valid-pixel ratios and plane-fit residuals are geometric quality metrics, not probabilities of grasp success.

## Tools

`depth_query` is the primary numeric entrypoint and takes `{"request": {...}}`. The same query kinds can also be invoked through `geometry_query` subject to the identical RGB-D profile permission check; disabling `depth_query` simultaneously disables those geometry aliases. Responses never dump raw point clouds into context: queries return at most 16 pixel measurements, at most 3 grasp/place candidates, or fixed-size numeric summaries.

| `kind` | Input Fields | Numeric Output |
|---|---|---|
| `depth_points` | `observation_id`, `camera_id`, `pixels` (1–16 pairs), optional `radius_px` | Visible surface `[x, y, z]`, z-depth, local valid-pixel ratio, and depth ambiguity for each pixel |
| `region_geometry` | `observation_id`, `camera_id`, `roi`, optional `seed_pixel` | Visible center, bounding range, extents, local cross-section width, principal axes, plane normal, and fit residual |
| `grasp_candidates` | Region fields, optional `count`, `grasp_depth_m`, `clearance_m` | 1–3 top-down grasp hypotheses with grasp center, orientation, visible width, jaw aperture margin, and approach pose |
| `place_candidates` | Region fields, `held_dimensions_m`, `grasp_offset_m`, optional `yaw_deg`, `count`, `margin_m` | Object placement center and gripper center hypotheses within the visible support region, orientation, and support clearance |
| `compare_regions` | `before_observation_id`, `after_observation_id`, `camera_id`, prior `roi`, optional `arm`, `seed_pixel` | World translation/rotation/lift, displacement/rotation relative to the gripper, measured jaw aperture, separation change, and updated ROI/seed |

In `place_candidates`, `held_dimensions_m` is the caller's conservative bounding-box estimate of the held object, and `grasp_offset_m` is the world-frame offset from the object center to the grasp center at the requested placement orientation. The placement query checks footprint clearance on the visible support surface; it does not plan a transport path, guarantee obstacle avoidance along the route, or certify physical stability after release.

Example query:

```json
{"request": {"kind": "depth_points", "observation_id": "obs-...", "camera_id": "cam_head", "pixels": [[320, 240], [330, 240]]}}
```

All numeric results share a uniform response envelope: `status`, `frame_id`, `unit`, `value`, `quality`, `unknowns`, `measurement_id`, and source observation provenance. A status of `supported` indicates only that the measurement has sufficient point correspondence or surface support; grasp and place candidates always return `partial` and enumerate their geometric assumptions. Occlusion, depth discontinuities, textureless surfaces, non-rigid deformation, or degenerate fits return `uncertain` with unknown numeric fields left `null`.

Model-facing projections strip redundant run-identity metadata and round floating-point values to at most 6 decimal places, while full floating-point precision is preserved in the Store and program interfaces. Targets, measurements, and candidates are assigned concise episode-local IDs such as `d1t1`, `d1m2`, and `d1m2c1`. Identical requests reuse the existing `measurement_id` after re-verifying source integrity and serve cached in-memory geometric fits without recomputation.

### Target Handles and Candidate IDs

`register_target` accepts the current `observation_id`, `camera_id`, and `roi` (plus optional `seed_pixel`, `arm`, and `label`) and returns a `target_id`. Up to 4 targets can be tracked concurrently per episode, and `forget_target` removes targets that are no longer needed. Once registered, `region_geometry`, `grasp_candidates`, and `place_candidates` can reference `target_id` directly.

`refresh_target` updates a tracked target with a new observation and re-selected ROI, while `target_status` reports its current registration state. Target IDs never carry over across episodes or environment generations. After any physical action executes, targets without a verified post-action update expire automatically so that stale coordinates cannot be silently reused.

Generated candidates receive a `candidate_id`. Historically, a candidate could be dispatched via `depth_move` (which was removed in the 2026-10 refactor; callers now pass the candidate's pose into a `robot_action` `move` stage):

```json
{"candidate_id": "d1m2c1", "arm": "left", "stage": "approach", "gripper_goal_normalized": 1}
```

In that legacy helper, `stage` selected `"approach"` or `"target"`, routing execution through `robot_move_grasp` and the single action writer without automatic jaw closure or retries. Every candidate is strictly bound to the observation from which it was computed; after any new observation, candidates must be recomputed from fresh measurements.

### Automatic Post-Action Target Verification

When `depth_auto_feedback` is enabled, the Harness compares registered targets across the existing pre-action and post-action observations without triggering extra camera captures or robot motions. The comparator matches RGB feature correspondences, fits a 3D rigid-body transform from the corresponding valid depth points, and compensates for camera motion using measured camera and gripper poses. Reliable matches update the target's ROI and attach a numeric `depth_feedback` summary to the action response (alongside a standalone evidence record); insufficient correspondences mark the target `uncertain`.

Local texture correspondence does not establish full object identity. Callers should place the target ROI on the object body while avoiding the robot fingers and background clutter; short-horizon motions of visible, rigid surfaces work best. Transparent, specular, thin-edged, heavily occluded, or deformable objects may yield `uncertain` correspondences and require re-selecting the ROI or camera viewpoint.

## Bounded Manipulation Skills

Calling `depth_query {"request": {"kind": "skill_help"}}` returns the parameter schemas and current enablement status for the bounded RGB-D skills. Historically, these two skills were executed via `skill_run` (which was removed in the 2026-10 refactor, so the skills are currently inspectable via `skill_help` only):

- `depth-grasp-lift-v1`: takes explicit `approach_pose`, `grasp_pose`, and `lift_pose`; opens the gripper during approach, moves to the grasp pose, closes the jaws, performs a short test lift of at most `5 cm`, and measures object elevation and displacement relative to the gripper.
- `depth-place-release-retreat-v1`: takes explicit `place_pose` and `retreat_pose` (plus up to two optional `via_poses`); places the object, opens the jaws, retreats by at most `15 cm`, and measures object retention at the destination and separation from the gripper.

Both skills take `observation_id`, `camera_id`, `roi`, `arm`, and `max_path_m`, plus an optional `seed_pixel` identifying the target pixel inside the ROI. The skill updates its tracked ROI and seed across phases (retaining the pre-release anchor during release). Each pose is specified as `grasp_position_m: [x, y, z]` and `angles_deg: [pitch, roll, yaw]` using the top-down relative convention of `robot_action` `move` stages rather than absolute world RPY. Skills never select targets or pick candidates autonomously; the caller supplies the poses based on current evidence.

Every phase requires a `completed` action receipt, a fresh observation, a measured grasp-center pose, and sufficient surface-correspondence support before advancing to the next phase. Any execution error, stale observation, occlusion, out-of-bounds displacement, failed threshold check, or cancellation halts the skill immediately without automatic retries. Phase checks match the physical stage: during approach, the object must remain stationary in the scene; during lift and transport, it must move rigidly with the gripper; during release and retreat, it must remain at the placement site.

Default timeouts are 40 seconds per single action, 215 seconds reserved for a complete grasp-and-lift sequence, and 167–263 seconds reserved for a place-release-retreat sequence (within a 300-second program ceiling and 8–12 tool calls). `max_path_m` bounds the Euclidean chord length from the newly measured start pose (which does not bound the joint-space path length produced by the native planner). Executed native step counts and action IDs are recorded in the result, while overall task success remains governed by the native evaluator terminal state.

`depth_skills` is independent of `vision_compare`; enabling RGB-D skills does not require enabling legacy RGB skills. Any depth read inside a program remains gated by the active observation profile and cannot bypass the default RGB-only boundary.

## Event-Driven Compact Payloads

`depth_event_payload` alters only the payload projection delivered to the actor model; full RGB images, depth arrays, proprioceptive states, action traces, and numeric measurements are always archived in full. For routine intermediate actions where all registered targets have reliable post-action depth measurements, the response can omit inline RGB images and return compact numeric deltas plus the `observation_id` (allowing the actor to fetch any camera view on demand via `history_view_observation`).

Full RGB camera views are always included on: gripper open/close stages, `robot_wait`, policy joint steps (`robot_policy_step`), non-completed or anomalous action receipts, uncertain target correspondence, target displacement exceeding the change threshold, and every third consecutive action that would otherwise qualify for compact delivery. Programs always read full observations internally, and both initial episode observations and explicit `robot_observe` calls always return full RGB views. Compact payload delivery does not alter action permissions, scoring, or continuation rules.

## Validation and Experimental Boundaries

CPU unit tests cover z-depth back-projection, multi-camera pose transforms, invalid depth and boundary masks, default profile rejection across historical and program readers, local grasp/place candidate generation, perforated support surfaces, camera-motion compensation, observation and candidate expiration, isolated program stop conditions, and full-RGB fallback triggers.

In native simulator verification, the default RGB probe confirms that depth capture is disabled, while the explicit RGB-D probe completes three-camera `640 × 480` capture, 9-pixel before/after numeric depth queries on each camera, a 1 cm left-arm upward move, and clean worker shutdown. Single numeric depth queries take roughly `16–33 ms`, returning ~`1.7 KB` of JSON while keeping the ~`1.5 MB` raw NPZ artifact out of model context (this reflects local query overhead excluding sensor capture, simulator startup, and model inference, and is not a measure of full-episode token savings).

Historical model-free native probe commands (`scripts/depth_probe.py` drove the removed `depth_move` tool and was removed in the 2026-10 refactor; shown below for historical reference only):

```bash
python scripts/depth_probe.py --run-dir /absolute/new/probe-rgb --gpu 0
python scripts/depth_probe.py --run-dir /absolute/new/probe-rgbd --gpu 0 \
  --observation-profile rgbd_proprio_calibrated_v1 --move
```

Controlled ablations evaluating RGB-D impact should compare conditions step by step—calibrated RGB baseline vs. RGB-D numeric queries, then adding automatic post-action verification, bounded skills, and compact event payloads—holding task layouts, native backend versions, and base prompts fixed, and reporting task success/failure/unscored counts, tool adoption rates, model turns, cached/uncached/output tokens, native execution steps, and wall-clock time. Because historical RGB-only archives lack ground-truth depth captures from those runs, synthetic depth must never be backfilled onto RGB runs and reported as an empirical RGB-D comparison.
