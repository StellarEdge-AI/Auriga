# Held-object perception (`features.held_perception`)

On by default together with the other external-perception switches (`features.external_perception`; set
`held_perception: false` to turn only this part off). Changes only what object tables return (`geometry_query` kind
`object_table` and the table attached by `features.object_table_observe`); nothing is moved or corrected. Inputs
are RGB, the camera calibration, the measured joints and gripper state, the robot meshes of the RoboDojo asset
directory and the harness's own earlier object tables. No simulator truth, no asset metadata, no object names.

Code: `auriga/perception/external/held_perception.py` (robot mask, holding tracker, held row), hooks in `auriga/perception/external/object_table.py`
(request fields, row merge, diagnostics), `auriga/perception/external/obs3d.py` (`held` passed through to the attached table) and
`auriga/apps/gateway/workbench.py` (one instruction sentence). Service side: `services/vggt_height/vggt_height_service.py`
(object-table-v5-hp1, see "Service fields"). Tests: `tests/test_held_perception.py`.

## What it fixes

Three defects of the object table for an object that is in a closed gripper:

1. **The capsule filter deletes held objects.** Robot pixels were excluded with 6 cm capsules around the robot link
   segments; a mask overlapping a capsule is dropped as robot. A held object always lies within 6 cm of the gripper,
   so the table usually had no row for it: in the offline set the held object had a row in 53 of 373 holding
   observations (14 %).
2. **Floating -> table rescale.** A mask whose points float above the table plane is re-anchored to the table (the
   rescale meant for objects whose VGGT depth is off), which moves and resizes a lifted object.
3. **VGGT depth of a held object.** The VGGT points are scaled to the table plane; for an object in the air next to the
   gripper the fit says nothing about its depth, while the robot's own kinematics give the grasp point and its camera
   depth exactly in this simulator. The held row therefore takes depth from FK and uses VGGT only for the shape.

## What it does (flag on)

**Robot pixel mask.** The harness renders the robot meshes (links base_link..link8 and the camera mount of both arms,
decimated to 2 mm) with the requested camera's K and T_world_camera and the forward kinematics of the measured joints
(fingers at the measured opening), dilates the silhouette by 3 px and sends it as `robot_mask_b64`. The service drops
a mask with >= 50 % robot pixels and cuts the robot pixels out of every other mask. A wrist camera does not render its
own arm's camera housing. If meshes or FK are unavailable the request falls back to the capsules and
`diagnostics.held_perception.error` says why.

**Held row.** An arm is holding when its jaws are commanded closed and stopped on something (the `held_end` holding
test: goal < 0.5 and 0.002 m < opening < full opening - 0.004 m). Its grasp centre is G = FK link6 + R [0.1501, 0, 0].

* v2 (default, `EXTENT_MODE = "pregrasp"`): the harness follows the gripper state of every saved observation; the
  first holding observation of a streak is the closure. At the first table that needs the held row it looks up the
  object's row in the newest 4 tables computed before the closure (row within 6 cm xy of G at the closure, rows whose
  mask contains the projected G first) and stores its extents, centre, yaw, tilt, the centre offset in the link6 frame
  and its footprint box. Groups from that row: tube-like (length >= 0.09 m and width <= 0.025 m), elongated (length >=
  2.5 x width), compact. Centre: compact = G; elongated and tube-like = G moved along the long axis by the carried
  along-axis offset (`carried_axis`). Long axis: static prior (link6 z, or link6 x when the pre-grasp row was upright).
  Extents: the pre-grasp box carried rigidly with link6 and re-measured in the world frame. A release drops the carried
  state; a regrasp captures again.
* v1 (fallback when there is no closure observation, no pre-grasp table or no row within 6 cm; or
  `EXTENT_MODE = "gk"`): mask = SAM 3.1 instance of "object" (plus the grasp row label when there is one)
  containing or within 40 px of the projected G, robot pixels removed, SAM 3.1 point prompt as fallback; centre = G;
  extents from the mask's VGGT points rescaled along the camera rays so that the points next to G sit at the FK depth
  (holding wrist camera when its mask has >= 100 px, else the requested camera). The note says which fallback applied.

**v2.1 guards** (module constants in `auriga/perception/external/held_perception.py`; with the shipped defaults v2.1 behaves the same as
v2 on the offline truth set, the 45 deg gate never triggered there):

* Axis-consistency gate (`AXIS_CONFLICT_DEG = 45.0`, `AXIS_CONFLICT_CENTRE = "grasp"`): when the long axis comes from
  the static prior and the carried pre-grasp row axis is more than 45 deg from it, the object was not grasped across
  the long axis the prior assumes. The centre is then the FK grasp centre (`"carried_axis"`: moved along the carried
  axis by the carried offset), the reported long axis is the carried axis, and the row note and `value.held[id]` get
  `axis_conflict_deg`. `None` turns the gate off.
* Partial pre-grasp row guard (off by default: `PARTIAL_ROW_RATIO = None`, `PARTIAL_ROW_CONTAINS = False`,
  `PARTIAL_ROW_REF = ("mask",)`, `GROUP_FROM_MAX = False`): a pre-grasp row shorter than `PARTIAL_ROW_RATIO` x the
  reference length (max of the sources in `PARTIAL_ROW_REF`: `mask` = the row's mask outline on its mid-height plane,
  `gk` = the held mask's VGGT extent at the first held request), or whose outline does not contain the projected grasp
  centre (`PARTIAL_ROW_CONTAINS`), keeps the v1 extents and group (`extent_source = gk_partial_row`).
  `GROUP_FROM_MAX` takes the group from max(row length, reference length). The setting 0.8 / True / ("mask", "gk") /
  True stays selectable and tested; on the truth set it made test tubes worse (2.9 / 10.6 -> 5.0 / 24.8 mm) because
  neither reference predicts which rows are short.
* Row source metadata (always on with the flag): `value.held[id]` gains `pregrasp_row_centre_m` and
  `pregrasp_table_source` (per-run object-table number, request kind, observation, camera of the pre-grasp table);
  the note names the table number and centre.

The held row replaces the object row with the same mask (else it is appended), with support `held_<arm>` and quality
`held_fk_depth`; it is never re-anchored to the table. `value.held[o<i>]` gives `source` (`held_pregrasp_row` or
`held_fk_depth`), `arm`, `group`, `centre` (estimator), `camera_id`, `note` (method and measured p50/p90 error),
`long_axis` (direction, source, carried, prior) or `orientation_prior`, and for v2 `pregrasp_row_id`,
`pregrasp_observation_id`, `closure_observation_id`, `extent_source`. Table objects keep the unchanged scale path.

Developer instructions gain one short paragraph (`held_perception.INSTRUCTION`): rows with quality held_fk_depth are
candidate held-object geometry estimates conditioned on rigid attachment to the gripper; a closed jaw or a carried
pre-grasp row does not confirm retention; the held entry distinguishes a current mask from prediction_only, and even a
current mask is not a co-motion check, so actual retention, slip and support are judged from RGB and action evidence.

## Measurements (offline truth check)

Replays of saved observations through the code and the service variant, compared with simulator truth that is used
for the analysis only.

| quantity | before | v1 | v2 |
|---|---|---|---|
| held object has a row (373 holding observations) | 53 / 373 = 14 % | 373 / 373 = 100 % | same as v1 |
| held centre xy error p50 / p90, all held rows (633) | - | 11.4 / 31.6 mm | 4.3 / 24.8 mm |
| held centre xy error p50 / p90, v1 sample (370) | - | 6.9 / 33.0 mm | 5.1 / 28.7 mm |
| held centre xy error p50 / p90, test tubes with a pre-grasp row (285) | - | 22.0 / 31.4 mm | 2.9 / 10.6 mm |
| held centre xy error p50 / p90, elongated (167) / compact (173) | - | 14.9 / 29.7, 5.8 / 28.2 mm | 2.5 / 10.1, 5.8 / 28.2 mm |
| length error p50 / p90, tube-like | - | 43 / 74 mm | 13 / 30 mm |
| length error p50 / p90, all | - | 43 / 97 mm | 17 / 61 mm |
| long-axis error p50 / p90 (static prior), tube-like / elongated | - | - | 2.2 / 6.8, 2.8 / 19.6 deg |
| table objects: centre change of paired objects p50 / p90 (1985) | - | 0.00 / 3.7 mm | same |
| robot mask vs truth render IoU p50 (p10), 81 head images | - | 0.959 (0.950) | same |
| robot mask time per head observation (max) | - | 0.14 s (0.19 s); wrist 0.01 s | same |

v2.1 with the shipped defaults gives the same rows as v2 on all 633 held rows (the 45 deg gate never fires there;
no non-compact row is above 45 deg).

v2 coverage: 511 of 633 held rows had a pre-grasp row (81 %); the fallbacks were no row within 6 cm (96), no closure
observation (25) and a request before the closure (1). After dilation the robot mask covers 100 % of the robot pixels
and removes about 4 % of object pixels. The estimator choice per group (`CENTRE_MODE`, `CENTRE_MODE_V2`, `AXIS_MODE`)
comes from these replays; the alternatives are kept selectable in the module.

Limits: the grasp point is the FK grasp centre at the closure (a pose_plan grasp point is not used); slip after the
closure is not modelled (0.4 / 5.2 mm for tubes from closure to pre-place in the replay); VGGT overestimates tube width
by about 13 mm, which the carried pre-grasp width inherits; truth boxes include caps and handles.

## How to run

The shipped defaults already enable it:

```json
{"features": {"object_table": true, "object_table_observe": true, "held_perception": true,
              "vggt_endpoint": "http://127.0.0.1:8331"}}
```

`vggt_endpoint` serves `object-table-v5-hp1` (`GET /health` reports `object_table: object-table-v5-hp1` and
`held: held-v1`); start it from `services/vggt_height` as described in its README. If `vggt_endpoint` is an older
`object-table-v5` service (`answer.version == "object-table-v5"` and no `robot_mask` field), the client automatically
falls back to re-querying with `robot_segments_m` capsule segments, skips `POST /held`, and still produces client-side
v2 pre-grasp carried held rows (or FK grasp-centre fallback rows) while reporting `service_fallback: "object-table-v5_no_hp1"`
in `diagnostics.held_perception`. `backend.root` must point at the RoboDojo checkout (robot meshes under
`Assets/Robots/x5/meshes`).

With the flag off the tool catalogue, the developer instructions and every object table are unchanged; only
`held_perception.enabled()` is consulted.

## Service fields

Request additions on `POST /object_table`: `robot_mask_b64` (np.packbits of the bool H x W mask, row-major),
`robot_mask_shape` `[H, W]`, `held: [{arm, grasp_centre_m, phrases?}]` (at most 2). Answer additions:
`robot_mask: "pixels"`, `held` (per item: `status`, `mask_source`, `phrase`, `mask_px`, `dist_px`, `centre_m`,
`grasp_px`, `grasp_depth_m`, `contour_px`, `label_px`, `bbox_px`, `gk` with length, width, top/low z, yaw, elongation
and scale `k`, `object_index`, `object_iou`) and `held_s`. `POST /held` returns the same per-item results for one
image without an object table (used for the holding wrist camera).


## Explicit motion and held-evidence contract

Grasp-angle planning preserves the requested orientation. Infeasible poses return planning failure; the actor may explicitly choose new angles and preview again. Joint-space replanning remains available without changing the Cartesian target. Vertical compliance never relaxes the declared orientation tolerance; downward error remains bounded by the declared position tolerance. Contact-stop only changes stopping behavior and the documented upward window, not declared horizontal or angular tolerances.

Held metadata reports `retention_status="unverified"` (no retention assertion), `attachment_assumption="rigid_since_grasp"` (conditional kinematic model), `geometry_evidence="current_mask"|"prediction_only"` (current usable segmentation availability), and `current_mask_views` (head/wrist views with usable masks). These categorical fields have no physical units. They are present for both carried and fallback rows; a current mask does not establish co-motion. Prediction-only rows remain available during occlusion but cannot establish a successful grasp or continued retention. No additional observation or model call is required.
