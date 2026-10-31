# pose_skill: orientation targets and grasp geometry for the actor

Switch `features.pose_skill` (default `false`). Code: `auriga/perception/external/pose_skill.py`, wired through
`auriga/runtime/tools/definitions.py` (catalogue), `auriga/runtime/tools/builtins.py` / `handlers.py` (registration) and
`auriga/apps/gateway/workbench.py` (one developer-instruction paragraph); tests: `tests/test_pose_skill.py`. Built for the
attached object table (`object_table`, `object_table_observe`, both on by default, see `docs/obs3d.md`; developed with
`vggt_height_mode: default`): the tool reads the object table attached to the latest observation.

The switch adds one read-only tool, `pose_plan`, and one paragraph of developer instructions. The tool computes
geometry and returns it as an ordinary tool output. It never moves the robot, never edits or rewrites an action and
never decides the grasp point; what the actor sends to `robot_action` stays its own choice.

## Motivation

Two offline analyses over stored episodes (simulator truth used for analysis only, never model-visible).

**Grasp: the closing direction decides the lift, not the object's angle to the table axes** (7 batches, 1295 grasp
attempts, 637 on elongated objects with length/width >= 1.5).

| jaw closing line relative to the object's long axis | lifted | n |
|---|---|---|
| across (60-90 deg) | 82 % | 451 |
| oblique (30-60 deg) | 53 % | 96 |
| along (< 30 deg) | 20 % | 90 |

The object's own yaw relative to the table / robot axes does not matter (0-15 deg 61 %, 15-30 deg 69 %, 30-45 deg
76 % lifted; class-controlled difference -6 points, inconsistent in sign). The share of along-axis grasps was
10-24 % per batch and not lower in the batches with the object table attached: the table already carries
`yaw_deg` and `length` / `width`, but the actor did not use them to choose the jaw yaw.

**Release: tilt and height at release decide whether the object stays** (9 batches, 907 releases of a held
object; stable = moved < 2 cm and rotated < 10 deg over the next 2-3 observations).

| long axis to table at release | elongated objects | compact objects |
|---|---|---|
| flat 0-15 deg | 76 % stable (n=215) | 91 % (n=369) |
| tilted 15-75 deg | 70 % (n=44) | 67 % (n=24) |
| vertical 75-90 deg | 92 % (n=25, almost all test tubes inserted into a rack) | 87 % (n=15) |

Finer bins for elongated objects: 0-5 deg 82 % stable, 5-10 deg 69 %, 10-15 deg 50 %, 60-75 deg 14 % (n=7, median
drift 9.8 cm). Tilted releases with the object centre 3-6 cm above the support were 87 % stable, > 10 cm 17 %.
Test tubes released lying were 0/8 stable (they roll). The actor did not use the table's `tilt_deg` when placing.

The skill therefore (1) asks the actor up front whether the end orientation of moved objects matters and to state
it, (2) gives the closing direction across the long axis and the geometry facts of the target row at every grasp,
in a form the actor reads right before choosing the grasp, and (3) converts a required end yaw into a gripper yaw
at release.

## What the model sees

### Tool description (as published in the catalogue, including the generated input field guide)

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

### Developer-instruction paragraph (appended once to the developer instructions)

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

### Replies

The reply is the JSON of the result object (tool output projection `data`). Examples rendered from a fixture
(attached row `o3`: a pen lying at yaw 37 deg; no robot state, so `measured_arm_yaw_deg` is null):

`pose_plan(request={kind:'targets', needed:true, reason:'pens go into the holder upright', targets:[{object:'o3 pen',
destination:'pen holder', yaw_deg:null, posture:'upright', basis:'success criterion'}]})`

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

`pose_plan(request={kind:'targets', needed:false, reason:'pure transport into the basket'})`

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

`pose_plan(request={kind:'grasp', object:'o3', arm:'right', grasp_point_m:[0.112,-0.064], reason:'mid-shaft'})`

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

`pose_plan(request={kind:'place', object:'o3', arm:'right', target_yaw_deg:0})` (after the grasp above, so the yaws
at grasp come from the remembered grasp)

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

## Kinds

| kind | request fields | reply fields | event |
|---|---|---|---|
| `targets` | `needed` (bool, required), `reason`, `targets[]` (`object`, `posture` lying/upright/any required; `destination`, `yaw_deg` or null, `pitch_deg`, `roll_deg`, `tolerance_deg` default 10, `basis`) | `needed`, `reason`, normalised `targets` (yaw wrapped to [-90, 90)), `attention_text` (null when not needed, at most 1600 characters), `note` | `pose_skill.targets` |
| `grasp` | `object` (oN of the attached table) or `row` (copied values), `observation_id` (default latest), `arm`, optional `grasp_point_m` (xy or xyz) + `reason` | `facts` (row values); `jaws` (`angles_deg` = [0, 0, yaw] closing across the long axis, the candidate nearer the arm's measured yaw, `equivalent_yaw_deg`, `closing_to_long_axis_deg`); `opening_check` ok / tight (< 10 mm spare) / too_wide against 88.8 mm; `pad_z_reference_m` (centre z, raised only when the fingertips would reach the support); `length_width_ratio`, `elongated`; `chosen_point` (the caller's point along / across the long axis, `within_footprint`); `notes`, `self_prompt`, `row_source`, `measured_arm_yaw_deg` | `pose_skill.grasp` |
| `place` | `target_yaw_deg` (required unless `target_posture` is `upright`), `object`, `arm`, `object_yaw_now_deg`, or `object_yaw_at_grasp_deg` + `gripper_yaw_at_grasp_deg` (default: the last `pose_plan` grasp of that object); optional `source_posture` / `target_posture` (lying / upright) | `release_yaw_deg`, `alternative_yaw_deg` (+180), `rotation_from_current_deg`, `method`, `note`; with a posture field also `posture_transition`, `release_angles_deg` and `alternative_angles_deg` ([pitch, roll, yaw]; lying <-> upright = a 90 deg rotation about the jaw closing line) and, for a transition, `tool_approach_axis_world` | `pose_skill.place` |

Angle convention (from `auriga/robot/model/roboprobe_pose.py`): `angles_deg = [pitch, roll, yaw]`; `[0, 0, 0]` is top-down
with the jaws closing along world x, and yaw rotates that closing line about world +z, so a top-down closing line
lies at `yaw (mod 180)`. The object table's `yaw_deg` is the long-axis yaw in [-90, 90) from world +x, hence
closing across the long axis is `yaw_deg +/- 90`.

`place` uses the current mode (`gripper yaw now + (target - current object yaw)`) only when the request gives
`object_yaw_now_deg` and the arm's measured yaw is available; otherwise it falls back to the yaws at grasp.

Invalid requests raise `ValueError` codes like the other read tools (`pose_plan_targets_requires_needed_boolean`,
`pose_plan_no_attached_table_for_observation`, `pose_plan_object_not_in_attached_table:<id>`,
`pose_plan_place_requires_target_yaw_deg`, ...). A missing robot state never raises; it shows up as
`measured_arm_yaw_deg: null`, and a low-quality row adds a note.

## Switch, events and the off path

| key | default | effect |
|---|---|---|
| `features.pose_skill` | `false` | publish `pose_plan` (actor and program callers) and append the paragraph above to the developer instructions |

Events (internal world kind, never shown to the model): `pose_skill.targets` (needed, reason, targets, observation
id), `pose_skill.grasp` (object, observation, row source and values, arm, measured yaw, jaws, pad reference,
opening check, chosen point), `pose_skill.place` (request, result, observation id).

Off path: `public_definitions(pose_skill=False)` equals `public_definitions()`, the gateway appends the paragraph
only when the switch is on, and with the switch off the actor catalogue is unchanged; with it on the catalogue gains
exactly one tool.

## Cache and cost

The tool definition is part of the static tool catalogue (about 9 k characters of definition JSON, roughly 2 k
tokens) and the instruction paragraph (1.8 k characters, about 0.5 k tokens) is part of the developer
instructions. Both are fixed for the whole run, so they sit in the cached prefix: written once per cache and read
on every request. Replies are ordinary tool outputs appended to the conversation; nothing in the prefix changes
during an episode. Each `grasp` call costs one extra model request (the instructions make it on demand, since the
attached table already carries `grasp_hints`), plus one `targets` call per episode and one `place` call per oriented
release when the actor uses them.

## Validation

### Offline (one stored batch with the attached table; the suggestion recomputed from the rows the actor saw, truth used only for scoring)

| measure | `pose_plan` grasp jaws | actor's actual grasps |
|---|---|---|
| closing line across the true long axis (>= 60 deg), elongated lying objects, same 70 grasps | 94 % (along 3 %) | 68 % (along 19 %) |
| pairs where exactly one of the two is along the axis | suggestion across, actor along: 11 | suggestion along, actor across: 0 |
| pad above the object top by > 5 mm | 2 % | 14 % |
| abs(pad z - true centre z) p50 / p90 | 0.3 / 1.1 cm | 0.6 / 2.7 cm |

Over all rows the suggested closing line is >= 60 deg off the true long axis in 89 % of cases (table yaw error
p50 4.8 deg, p90 36 deg).

### Episodes

In a 20-episode run on four RoboDojo tasks with the switch on, `tracking_error` events per episode and the number of
episodes with many tracking errors were lower than in an earlier run without it, with about 72 requests per episode.
The two runs used different API providers and ran at different times, so this comparison is confounded and does not
isolate the tool's effect.

## Known limitations and next steps

- `place` returns full 3D `release_angles_deg` only for the lying <-> upright transition (`source_posture` /
  `target_posture`); other pitch / roll changes get a note, not numbers.
- In the evaluated episodes every `place` call used the yaws remembered from the grasp; none passed the held object's
  re-estimated yaw (`object_yaw_now_deg`), so rotation of the object in the hand is not corrected. Re-estimating
  the held object's yaw before release is the next step.
- The suggestion inherits the table's yaw error (p90 36 deg); for compact footprints the reply says the closing
  direction matters little.
- The tool description adds about 2 k tokens to the cached prefix and each `pose_plan` call adds one request.
- A script that pairs each `pose_plan` grasp with the grasp the actor finally sent is not part of the repository yet.
