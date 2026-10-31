# Object table (features.object_table, on by default)

`geometry_query` kind `object_table` returns an estimated table of the objects on the support surface, so the
model does not have to read heights and footprints off the image itself. The tool reports geometry only; it does
not name objects or choose targets.

## Request

    {kind: "object_table", observation_id, camera_id?: "cam_head", pixels?: [[u, v], ...], labels?: ["..."], target_camera_ids?: [...]}

- `camera_id`: the camera that is segmented (default `cam_head`).
- `pixels` (optional, up to 16): points on objects the model wants included even if the automatic segmentation
  missed them; each gets one SAM 2.1 point-prompt mask. `value.requested` maps each pixel to its id.
- `labels` (optional, up to 16 strings, each cut to 80 characters): noun phrases chosen by the model. They are forwarded
  verbatim; the service (object table v5) segments each phrase with SAM 3.1 text prompts. The automatic part of the
  table comes from the fixed class-agnostic prompt "object" (unlabelled rows); labelled instances are added first and a
  generic instance that repeats or lies inside one of them is skipped. Rows then carry an extra column
  `label` (the best-scoring phrase of that object, null for pixel-only objects) and `value.labels` lists per phrase
  `{label, status: ok|no_match|filtered|backend_unavailable|unsupported_backend, ids}`; one phrase can match several ids
  and one id can match several phrases (only the best is shown in the row). The harness never names objects itself.
  If the SAM 3.1 process is down the service falls back to the automatic SAM 2.1 table (status `backend_unavailable`).
- `target_camera_ids` (optional, up to 3): other cameras of the same observation (wrist views). The same ids are
  drawn there at the projection of each object's 3D centre with the calibrated camera (a projection, not a detection;
  occlusion is not checked). `value.projected_ids[camera]` lists the ids that fall inside that image.

## Result (model-facing)

`value.columns` / `value.rows`, one row per object, ids `o1..oN` (N <= `object_table_max_objects`, default 20;
`value.omitted` = objects dropped by the cap):

| column | meaning |
|---|---|
| `centre_m` | [x, y, z] env_origin: footprint centre of the visible points; z = (top + bottom) / 2 |
| `top_z`, `bottom_z` | 98th-percentile height of the visible mask points; bottom = table height, the top of the supporting object, or the lowest visible point when no support was found |
| `length`, `width`, `yaw_deg` | extent of the visible points along / across the principal horizontal axis (PCA), yaw of that axis in [-90, 90) |
| `tilt_deg` | angle of the 3D principal axis from vertical; only for elongated point sets (ratio >= 1.8), else null |
| `support` | `table`, the id of the object below, or `unknown` |
| `quality` | `ok` or `low:<flags>` (see below) |

Plus one overlay image per camera: contours and ids on `camera_id`; ids at projected centres on each target camera.
Service diagnostics (mask size, point counts, depth-confidence rank, timing) stay in the stored result
(`diagnostics`) and are not sent to the model.

Quality flags (computed by the service, no truth): `height_above_max` (top more than
`object_table_max_height_m`, default 0.30 m, above the table), `low_depth_confidence` (median VGGT depth confidence of
the mask pixels in the lowest 25 % of the image), `contact_mismatch` (the lowest mask pixels, back-projected onto the
table plane, lie more than 3 cm farther from the camera than the estimated centre: the depth under the mask places
the object too high or too near), `no_support_below` (lowest visible point more than 2 cm above the table and no
object below), `few_points`, `fragmented_points`.

## Service side (VGGT service v2, POST /object_table)

1. VGGT-Omega depth of the image, back-projected with the calibrated K and camera rotation, scaled so that the
   RANSAC table plane lies at `vggt_support_z_m` (same reconstruction and cache as `/height`).
2. SAM 2.1 point prompts on an 8 px grid, restricted to pixels whose VGGT point is 4 mm to 45 cm above the table;
   3 masks per prompt; kept when predicted IoU >= 0.5, stability >= 0.8, 40 px <= area <= 8 % of the image,
   >= 60 % of the mask above the table, not touching the image border or the configured exclusion box.
3. Greedy selection, larger masks first, skipping masks more than 30 % covered by selected ones; dropped: masks
   whose points are mostly within 6 cm of the robot links (FK of the measured joints, sent by the harness), top more
   than 45 cm above the table (background), fragmented point sets, and masks with less than 4 mm relief over the ring
   of pixels around them (table texture).
4. Per mask: VGGT points (eroded mask, depth confidence above the image p10, DBSCAN 1 cm largest cluster) -> the
   columns above.
5. Floating objects (object-table-v2): when the lowest visible point is more than 3 cm above the table, no
   supporting object is found, and that gap also exceeds the visible thickness of the point set (top - lowest
   point), the point set is rescaled along the camera rays so its lowest point lies on the table (flag
   `floating_rescaled_to_table`; VGGT places thin objects such as test tubes too near the camera). Objects whose
   gap is smaller than their visible thickness (bottom hidden, e.g. spheres) keep the measured geometry and get the
   flag `floating`; v1 rescaled every floating object (service parameter `reanchor_mode = "always"`).

Latency on an RTX 4090: about 1-2 s for a new image, about 1 s when the image is cached.

### Service v4 / v5

- v4: a requested pixel reuses an existing object only when the pixel lies in that mask's largest connected component
  and the SAM 2.1 point-prompt mask at the pixel has IoU >= 0.5 with it; otherwise the point mask becomes a new object.
  (v1-v3 reused any automatic object whose mask contained the pixel; a rack mask with a detached tube component made a
  tube click resolve to the rack.) Two pixels resolving to one object get status `ok_same_object`.
- v5: `labels` (above). Backend switch: service param `seg_backend` = `sam31` (default) or `sam21`; SAM 3.1 runs in a
  second process (`sam31_service.py`, 127.0.0.1:8333, own venv) and the main service calls it per request.
  Default segmentation (with or without labels) is SAM 3.1 text prompting (threshold 0.4) instead of the SAM 2.1 point
  grid; offline on 28 frames: task objects found 146/191 vs 138/191, false-positive ids 0 vs 24, centre xy p90 2.0 vs
  3.7 cm, top p90 2.5 vs 3.0 cm, latency p50 0.46 vs 2.07 s. `seg_backend: "sam21"` reproduces v4 exactly.

## Configuration

| key | default | meaning |
|---|---|---|
| `features.object_table` | `true` | publish and serve kind `object_table` |
| `features.object_table_max_objects` | `20` | cap on returned objects |
| `features.object_table_max_height_m` | `0.30` | threshold of the `height_above_max` flag |
| `features.object_table_head_exclude_px` | `200,405,440,480` | image box of the head view showing the camera mount; empty = none |
| `features.vggt_endpoint`, `vggt_support_z_m`, `vggt_timeout_s` | shared with `vggt_height` | service address, table height, timeout |

With the switch off the tool catalog is byte-identical to the build without this module (tests/test_object_table.py).
Without a reachable service the query returns status `unknown` with `reason: object_table_unavailable:<error>`.
Truth scoring: `scripts/score_geometry_truth.py` scores each reported object centre (`plane_source = object_table`)
and `ot_top_minus_truth_top_z_m` when `analysis.scene_truth_log` is on.
