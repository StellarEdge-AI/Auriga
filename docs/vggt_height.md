# features.vggt_height: estimated object height for `pixel_plane`

On by default in mode `optional` (part of `features.external_perception`). With the switch off the tool
catalog, instructions and every geometry_query result are unchanged
(`tests/test_vggt_height.py::test_off_*`).

## What the model sees (switch on)

- `geometry_query` description gains one sentence: pixel_plane also accepts `plane_source:'vggt'`, which
  replaces the caller's plane height with an image-based estimate of the object's centre height at that pixel
  (alternate plane: its top); check `value.plane_estimate.quality`.
- `pixel_plane` request branch: new optional field `plane_source` (enum `["caller", "vggt"]`, default `caller` = unchanged behaviour; two values so the input field guide does not treat it as a branch selector); `plane_origin_m` and
  `plane_normal` become optional when `plane_source` is given (input field guide rule; `caller` without a plane is rejected by pixel_plane itself;
  "at least one of (requires plane_origin_m, plane_normal | requires plane_source)").
- Result: the usual pixel_plane value (`point_m`, `alternate_point_m`, `assumption_delta_m`,
  `cross_camera_projections`, optional overlay) with the plane set to the horizontal plane at the estimated
  centre height, plus `value.plane_estimate`:
  `{source: "vggt_estimate", measured: false, center_z_m, top_z_m, support_z_m, object_height_m, method
  (sam_point_mask | window), quality (ok | low), quality_reasons, n_points, mask_px, depth_scale, support_fit,
  latency_s}`. The first assumption line states that the height is an estimate, not measured depth.
- Alternate plane: the caller's `plane_origin_alt_m`, else the caller's `plane_origin_m`, else the estimated top plane.
- Service failure: with a caller plane the normal pixel_plane result is returned with
  `plane_estimate.status = "unavailable"`; without one the query returns `unknown`,
  `reason: vggt_estimate_unavailable:<error>`.

## features.vggt_height_mode

- `optional` (default): as above, only `plane_source:'vggt'` uses the estimate.
- `default`: every `pixel_plane` uses the estimated centre height unless the caller sets `plane_source:'caller'`;
  a caller `plane_origin_m` becomes the alternate plane (so `assumption_delta_m` is the disagreement between the
  model's plane and the estimate; a caller `plane_origin_alt_m` is then not used). A caller plane whose normal is
  not near-vertical (|n_z| < 0.95, e.g. a wall) is left unchanged (`plane_estimate.status = "not_applied"`).
  Service unavailable -> caller plane with `plane_estimate.status = "unavailable"`. The description sentence is
  replaced by: the plane height is estimated from the image unless plane_source is 'caller'; a plane_origin_m you
  pass is kept as the alternate hypothesis.

## Why an option on pixel_plane and not depth_query

The RGB-D `depth_query` contract is tied to `rgbd_proprio_calibrated_v1` (measured depth artifacts, target
handles, grasp/place candidates) and is unavailable under `rgb_proprio_calibrated_v1`. Carrying a monocular
estimate there would need a profile change, a "measured vs estimated" flag through all depth kinds, and a much
larger instruction change. The model already calls `pixel_plane` on demand (about five calls per episode in recorded
evaluations); an optional field on that call changes the least.

## Privileged-information boundary

Inputs: the saved RGB image of the selected camera, its calibrated K and `T_world_camera` (both part of
`rgb_proprio_calibrated_v1`), and `features.vggt_support_z_m` (0.765 m, the RoboDojo table top = the disclosed
arm-mount height in `auriga/robot/model/robot_information_data/arx_x5/physical.json`). No simulator state or object truth.

## Service

`features.vggt_endpoint` (default `http://127.0.0.1:8331`) is a separate process (VGGT-Omega 1B-512 +
SAM 2.1 hiera-large); see `services/vggt_height/README.md` for installation and start-up.
Method: one VGGT-Omega forward pass on the native image; depth back-projected with the calibrated K and the
known camera rotation; RANSAC near-horizontal plane, per-image scale so that plane lies at the support height;
SAM 2.1 point prompt at the pixel -> mask -> VGGT points (eroded, confidence > p10, above support + 4 mm,
DBSCAN 1 cm) -> top = p98 z; centre = (top + support) / 2; 21 x 21 px window when no mask.
Results are cached per image, so repeated queries on the same
observation cost only the SAM decoder.

Config (these are the shipped defaults):

```json
"features": {"vggt_height": true, "vggt_endpoint": "http://127.0.0.1:8331",
             "vggt_support_z_m": 0.765, "vggt_timeout_s": 60}
```

Weights licence: VGGT-Omega is FAIR Noncommercial Research License v1 (research use only).
