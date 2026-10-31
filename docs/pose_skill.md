# `pose_skill`: Orientation Targets and Grasp Geometry for the Actor

Controlled by `features.pose_skill` (default `false`). When enabled, it registers one read-only tool—`pose_plan`—for both actor and program callers and appends one guidance paragraph to the startup developer instructions.

- **Implementation**: `auriga/perception/external/pose_skill.py`, wired via `auriga/runtime/tools/definitions.py` (catalog), `auriga/runtime/tools/builtins.py` & `handlers.py` (registration), and `auriga/apps/gateway/workbench.py` (developer instructions).
- **Tests**: `tests/test_pose_skill.py`.
- **Companion features**: Designed to work alongside the attached object table (`features.object_table` and `features.object_table_observe`, both enabled by default; see [`docs/obs3d.md`](obs3d.md)).

`pose_plan` is a purely read-only geometric helper: it computes grasp and release orientations from the object table and returns them as standard tool output. It never moves the robot, never modifies or rewrites action requests, and never chooses the grasp point for the model; what the actor passes to `robot_action` remains entirely under its own control.

## Empirical Motivation

Two offline analyses across recorded RoboDojo episodes (using simulator ground truth strictly for post-hoc evaluation) motivated this tool:

### 1. Grasp Success Depends on Jaw Closing Direction Relative to the Object's Long Axis

Across 7 benchmark batches (1,295 grasp attempts, including 637 attempts on elongated objects with $\text{length}/\text{width} \ge 1.5$):

| Jaw Closing Line Relative to Object Long Axis | Successful Lift Rate | Sample Count ($n$) |
|---|---|---|
| **Across long axis (`60°–90°`)** | **82%** | 451 |
| **Oblique (`30°–60°`)** | **53%** | 96 |
| **Along long axis (`< 30°`)** | **20%** | 90 |

By contrast, the object's absolute yaw relative to the table or robot axes had no meaningful effect (`0°–15°`: 61%, `15°–30°`: 69%, `30°–45°`: 76% lifted). Even with `object_table` attached—which already reports `yaw_deg`, `length`, and `width`—along-axis grasps still accounted for 10–24% of attempts per batch because the model did not consistently convert `yaw_deg` into a perpendicular jaw yaw.

### 2. Release Tilt and Drop Height Govern Post-Placement Stability

Across 9 benchmark batches (907 object releases, defining a placement as "stable" when the object drifted $< 2\text{ cm}$ and rotated $< 10^\circ$ over the next 2–3 observations):

| Object Long-Axis Angle to Table at Release | Elongated Objects | Compact Objects |
|---|---|---|
| **Flat (`0°–15°`)** | **76% stable** ($n=215$) | **91% stable** ($n=369$) |
| **Tilted (`15°–75°`)** | **70% stable** ($n=44$) | **67% stable** ($n=24$) |
| **Vertical (`75°–90°`)** | **92% stable** ($n=25$, primarily test tubes inserted into racks) | **87% stable** ($n=15$) |

Within the flat regime for elongated objects, stability dropped sharply with residual tilt: `0°–5°` was **82%** stable, `5°–10°` was **69%**, `10°–15°` was **50%**, and `60°–75°` was just **14%** ($n=7$, median drift `9.8 cm`). Tilted releases with the object center `3–6 cm` above the support surface were **87%** stable, versus **17%** when dropped from $> 10\text{ cm}$. Test tubes released horizontally on the table were `0/8` stable because they rolled away.

To address these patterns, `pose_plan`:
1. Prompts the actor to check up front whether final object orientation is constrained by the task rules (`kind="targets"`).
2. Computes the perpendicular jaw closing angle (`yaw_deg ± 90°`, selecting the principal-branch candidate nearer the arm's current measured yaw) and pad height reference for any object-table row (`kind="grasp"`). Note that `object_table_observe` now also embeds these `across_yaw_deg` and `pad_z_m` hints directly in `grasp_hints` with zero extra tool turns.
3. Converts a desired final object yaw or 3D posture transition (`lying` $\leftrightarrow$ `upright`) into the corresponding gripper release angles (`kind="place"`).

## Model-Facing Contract

### Published Tool Description and Schema

```text
Read-only pose planning from the object table; no motion, no action edits. kind='targets': record whether final object orientation matters for this task and, if so, each moved object's absolute end rotation (long-axis yaw in world degrees, upright or lying, optional pitch/roll); returns attention_text to keep in task_plan attention. kind='grasp': for one object-table row (object id from the table attached to the latest observation, or copied row values) returns its geometry facts (centre, top, bottom, length, width, yaw, tilt), angles_deg with the jaws closing across the long axis (object yaw +/- 90 deg, the candidate nearer the arm's measured yaw), the width check against the 88.8 mm maximum opening, the pad height reference (centre z) and a one-line self_prompt; the grasp point itself is your choice (an optional grasp_point_m + reason is placed relative to the footprint). kind='place': converts an object end yaw into the gripper yaw at release. All values derive from VGGT estimates (not measurements); check them against current RGB. Angles use robot_action's angles_deg=[pitch,roll,yaw] convention: [0,0,0] is top-down with the jaws closing along world x; yaw rotates that line about world +z.
Input field guide (use only fields applicable to the selected mode/kind):
input: Type: object. Object rules: requires request; only declared fields.
request: Type: object. Object rules: kind is one of ["targets","grasp","place"]; arm is one of ["left","right"] when supplied; source_posture is one of ["lying","upright"] when supplied; target_posture is one of ["lying","upright"] when supplied; requires kind; only declared fields.
request.kind: Type: string; Allowed: ["targets","grasp","place"].
request.needed: targets: whether the success criteria constrain final orientation. Type: boolean.
request.reason: targets: one-line reason for the needed decision; grasp: why this grasp point (e.g. handle, rim, centre). Type: string.
request.grasp_point_m: grasp: optional grasp point you chose (xy or xyz, env_origin m); the reply places it relative to the object's long axis and footprint. Type: array; Minimum items: 2; Maximum items: 3.
request.grasp_point_m[]: Type: number.
request.targets: targets: one entry per moved object. Type: array; Maximum items: 12.
request.targets[]: Type: object. Object rules: posture is one of ["lying","upright","any"]; requires object, posture; only declared fields.
request.targets[].object: Object-table id at planning time and/or a short visible label. Type: string.
request.targets[].destination: Short description of where it goes (no coordinates needed). Type: string.
request.targets[].yaw_deg: Required end yaw of the object's long axis in world degrees (any value; normalised to [-90, 90) because the long axis is symmetric); null when yaw does not matter. Type: number or null.
request.targets[].posture: lying = long axis horizontal (tilt ~90 deg from vertical), upright = long axis vertical, any = not constrained. Type: string; Allowed: ["lying","upright","any"].
request.targets[].pitch_deg: Type: number.
request.targets[].roll_deg: Type: number.
request.targets[].tolerance_deg: Allowed yaw deviation; default 10. Type: number; Minimum (inclusive): 0; Maximum (inclusive): 90.
request.targets[].basis: Evidence: success criterion, fixture orientation, table row. Type: string.
request.object: grasp: object-table id (oN) of the table attached to observation_id; place: id or label used in targets. Type: string.
request.observation_id: grasp: observation whose attached table holds object; default the latest observation. Type: string.
request.row: Copied object-table values when no attached-table id applies (e.g. an on-demand geometry_query table). Type: object. Object rules: requires centre_m, length, width, yaw_deg; only declared fields.
request.row.centre_m: Type: array; Minimum items: 3; Maximum items: 3.
request.row.centre_m[]: Type: number.
request.row.top_z: Type: number.
request.row.bottom_z: Type: number.
request.row.length: Type: number; Minimum (inclusive): 0.
request.row.width: Type: number; Minimum (inclusive): 0.
request.row.yaw_deg: Type: number.
request.row.tilt_deg: Type: number or null.
request.arm: grasp/place: arm whose measured yaw selects the nearer of the two equivalent jaw yaws. Type: string; Allowed: ["left","right"].
request.target_yaw_deg: place: required object long-axis end yaw (world deg); optional when target_posture='upright'. Type: number.
request.source_posture: place: object posture at grasp ('lying' horizontal or 'upright' vertical); pair with target_posture for 3D release_angles_deg. Type: string; Allowed: ["lying","upright"].
request.target_posture: place: required object posture at release ('lying' or 'upright'); lying <-> upright computes full 3D release_angles_deg=[pitch,roll,yaw] via a 90 deg rotation about the jaw closing line. Type: string; Allowed: ["lying","upright"].
request.object_yaw_now_deg: place: held object's long-axis yaw in the latest table. Type: number.
request.object_yaw_at_grasp_deg: place: object long-axis yaw when it was grasped. Type: number.
request.gripper_yaw_at_grasp_deg: place: angles_deg yaw used for that grasp; default the yaw of the last pose_plan grasp for this object. Type: number.
```

### Developer-Instruction Paragraph (When `features.pose_skill=true`)

```text
## Pose planning (pose_plan)
Read the task's success criteria to check whether the final orientation of moved objects matters (insertion,
hanging, placing onto a fixture or slot, aligning, standing upright or lying flat). When final orientation matters,
estimate each moved object's end rotation (long-axis yaw in world degrees, upright or lying, pitch/roll) from the
attached object table and destination fixture; you may record them with
pose_plan(request={kind:'targets', needed:true, targets:[...]}) and keep the returned attention_text in a task_plan
attention entry so the final waypoint reaches this rotation (for pure transport where orientation is free, either
skip pose_plan or call kind:'targets' with needed:false).
Notice that the attached object_table already provides inline `grasp_hints` (`across_yaw_deg = yaw_deg +/- 90`,
`pad_z_m`, `opening_check`) for every object with zero extra tool turns, and `robot_action(mode='preview')`
automatically audits jaw-to-long-axis alignment, grasp Z height, and grip_guard width in `grasp_3d_review`.
You can use `grasp_hints` directly in `robot_action`, or call pose_plan(request={kind:'grasp', object:'oN', arm,
grasp_point_m, reason}) on demand when you want to verify a specific off-centre grasp point (such as a tool's
handle or rim) or select the candidate nearer the arm's measured yaw. Jaws closing along the long axis of an
elongated object rarely lift it. Before releasing an object that requires a target yaw or a 3D posture transition
(such as standing a lying tube/pen upright via source_posture:'lying', target_posture:'upright'), call
pose_plan(request={kind:'place', ...}) for the release yaw and full 3D release_angles_deg=[pitch,roll,yaw], and
lower close to the support before opening.
```

### Example Calls and Responses

1. **Recording orientation targets (`kind="targets"`, orientation constrained)**:
   ```python
   pose_plan(request={
       "kind": "targets",
       "needed": True,
       "reason": "pens go into the holder upright",
       "targets": [{
           "object": "o3 pen",
           "destination": "pen holder",
           "yaw_deg": None,
           "posture": "upright",
           "basis": "success criterion",
       }],
   })
   ```
   ```json
   {
     "kind": "targets",
     "needed": true,
     "reason": "pens go into the holder upright",
     "targets": [{"object": "o3 pen", "posture": "upright", "yaw_deg": null, "tolerance_deg": 10.0, "destination": "pen holder", "basis": "success criterion"}],
     "attention_text": "End rotations (absolute, world): o3 pen -> pen holder: upright. The final waypoint of each transport must reach its rotation; before release use pose_plan kind='place' for the gripper yaw.",
     "note": "Keep attention_text in task_plan attention."
   }
   ```

2. **Recording unconstrained transport (`kind="targets"`, `needed=false`)**:
   ```python
   pose_plan(request={"kind": "targets", "needed": False, "reason": "pure transport into the basket"})
   ```
   ```json
   {
     "kind": "targets",
     "needed": false,
     "reason": "pure transport into the basket",
     "targets": [],
     "attention_text": null,
     "note": "Orientation not constrained; grasp suggestions remain available."
   }
   ```

3. **Computing grasp geometry for a lying pen (`kind="grasp"`, `o3` at `yaw_deg = 37.0`)**:
   ```python
   pose_plan(request={
       "kind": "grasp",
       "object": "o3",
       "arm": "right",
       "grasp_point_m": [0.112, -0.064],
       "reason": "mid-shaft",
   })
   ```
   ```json
   {
     "object": "o3",
     "facts": {"centre_m": [0.112, -0.064, 0.781], "top_z": 0.789, "bottom_z": 0.765, "length": 0.142, "width": 0.024, "yaw_deg": 37.0, "tilt_deg": 88.0, "quality": "ok"},
     "jaws": {"angles_deg": [0.0, 0.0, -53.0], "equivalent_yaw_deg": 127.0, "closure_yaw_candidates_deg": [127.0, -53.0], "closing_to_long_axis_deg": 90.0},
     "max_opening_m": 0.0888,
     "opening_check": "ok",
     "pad_z_reference_m": 0.781,
     "length_width_ratio": 5.92,
     "elongated": true,
     "chosen_point": {"grasp_point_m": [0.112, -0.064], "reason": "mid-shaft", "along_long_axis_m": 0.0, "across_long_axis_m": 0.0, "within_footprint": true},
     "notes": ["centre z is 8 mm below the visible top (VGGT tops of small curved objects read 1-3.5 cm low; centre z is the better pad reference)"],
     "self_prompt": "Grasp o3 facts: centre [0.112, -0.064, 0.781], top 0.789, bottom 0.765, length 0.142, width 0.024 m, yaw 37 deg, tilt 88 deg. Jaws: angles_deg [0.0, 0.0, -53.0] closes across the long axis (equivalent yaw 127.0); width fits the 88.8 mm opening. Pad height reference: centre z 0.781. The grasp point is yours to choose from the image and these numbers (for example a tool's handle rather than its geometric centre); state it and the reason.",
     "basis": "object table (VGGT estimate), not a measurement; grasp point chosen by the actor",
     "kind": "grasp",
     "observation_id": "obs-12",
     "row_source": "attached_table",
     "arm": "right",
     "measured_arm_yaw_deg": null
   }
   ```

4. **Computing release orientation (`kind="place"`)**:
   ```python
   pose_plan(request={"kind": "place", "object": "o3", "arm": "right", "target_yaw_deg": 0})
   ```
   ```json
   {
     "kind": "place",
     "object": "o3",
     "arm": "right",
     "target_yaw_deg": 0.0,
     "measured_arm_yaw_deg": null,
     "release_yaw_deg": -90.0,
     "alternative_yaw_deg": 90.0,
     "rotation_from_current_deg": null,
     "method": "gripper yaw at grasp + (target - object yaw at grasp)",
     "note": "Keep the grasp's pitch/roll unless the end posture changes; lying -> upright needs a 90 deg rotation about the jaw closing line. Lower the object close to the support before opening."
   }
   ```

## Summary of Query Kinds

| `kind` | Request Fields | Response Fields | Internal Event |
|---|---|---|---|
| `targets` | `needed` (bool, required), `reason`, `targets[]` (`object`, `posture` in `lying`/`upright`/`any` required; optional `destination`, `yaw_deg`, `pitch_deg`, `roll_deg`, `tolerance_deg` default `10`, `basis`) | `needed`, `reason`, normalized `targets` (yaw wrapped to $[-90^\circ, 90^\circ)$), `attention_text` (`null` when not needed, $\le 1600$ chars), `note` | `pose_skill.targets` |
| `grasp` | `object` (`oN` from attached table) or `row` (explicit dict), `observation_id` (default latest), `arm`, optional `grasp_point_m` + `reason` | `facts`, `jaws` (`angles_deg = [0, 0, yaw]` closing across the long axis, nearer the arm's measured yaw, plus `equivalent_yaw_deg` and `closing_to_long_axis_deg`), `opening_check` (`ok` / `tight` / `too_wide` vs. `88.8 mm`), `pad_z_reference_m`, `length_width_ratio`, `elongated`, `chosen_point`, `notes`, `self_prompt`, `row_source`, `measured_arm_yaw_deg` | `pose_skill.grasp` |
| `place` | `target_yaw_deg` (required unless `target_posture="upright"`), `object`, `arm`, `object_yaw_now_deg`, or `object_yaw_at_grasp_deg` + `gripper_yaw_at_grasp_deg` (defaults to last `pose_plan` grasp for that object); optional `source_posture` / `target_posture` (`lying` / `upright`) | `release_yaw_deg`, `alternative_yaw_deg` ($+180^\circ$), `rotation_from_current_deg`, `method`, `note`; when posture transition is specified, also returns `posture_transition`, `release_angles_deg`, `alternative_angles_deg` (`[pitch, roll, yaw]` via a $90^\circ$ rotation about the jaw closing line), and `tool_approach_axis_world` | `pose_skill.place` |

### Angle Convention

Following `auriga/robot/model/roboprobe_pose.py`, `angles_deg = [pitch, roll, yaw]` defines `[0, 0, 0]` as a top-down approach whose jaws close along the world $+x$ axis, and `yaw` rotates that jaw closing line about world $+z$. Because an object's `yaw_deg` in `object_table` gives its principal horizontal axis in $[-90^\circ, 90^\circ)$ relative to world $+x$, closing perpendicular to the principal axis corresponds to $\text{yaw\_deg} \pm 90^\circ$.

## Offline Validation Against Ground Truth

Evaluated offline on 70 grasps of elongated lying objects from a recorded batch with `object_table_observe` enabled (comparing the jaw yaw and pad height suggested by `pose_plan` against the grasps actually executed by the actor):

| Metric | `pose_plan` Suggestion | Actor's Executed Grasps |
|---|---|---|
| Jaw closing line across true long axis ($\ge 60^\circ$), $n=70$ | **94%** (along-axis: 3%) | **68%** (along-axis: 19%) |
| Paired cases where only one method closed along the axis | Suggestion across, actor along: **11** | Suggestion along, actor across: **0** |
| Pad height $> 5\text{ mm}$ above true object top (hovering miss) | **2%** | **14%** |
| $|\text{pad\_z} - \text{true\_centre\_z}|$ p50 / p90 | **0.3 / 1.1 cm** | **0.6 / 2.7 cm** |

Across all object-table rows, the suggested closing line lies within $30^\circ$ of perpendicular to the true principal axis in **89%** of cases (inheriting the underlying VGGT object-table yaw error of p50 `4.8°`, p90 `36°`).

## Known Limitations

- `place` computes full 3D `release_angles_deg` specifically for `lying` $\leftrightarrow$ `upright` transitions (`source_posture` / `target_posture`); arbitrary oblique pitch/roll transitions return an explanatory note rather than a 3D Euler triple.
- When `place` is called without `object_yaw_now_deg` from an updated held-object table or `in_hand_axis` query, it relies on the yaw remembered at grasp time and cannot compensate for in-hand rotational slip.
- Publishing the `pose_plan` tool schema and developer instructions adds ~2.5k tokens to the static cached prefix, and each explicit `pose_plan` call consumes one model turn (which is why inline `grasp_hints` in `object_table_observe` are enabled by default for zero-turn access).
