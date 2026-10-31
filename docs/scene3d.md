# scene3d point cloud (features.scene3d, on by default)

`geometry_query` kind `scene3d` stores the VGGT point cloud of one camera image as an artifact and returns a short
summary. The model queries the cloud inside `program_run` with the helper module `scene3d_helpers` (copied into every
program job workspace while the switch is on). The cloud never enters the model context unless a program prints it.
Complementary to `object_table` (per-object table): scene3d answers region / point questions that do not depend on
the automatic segmentation (top surface height at a place, local table height, what is near a point).

## Request

    {kind: "scene3d", observation_id, camera_id?: "cam_head", voxel_m?: 0.005-0.03, artifact?, queries?: [{op, ...}, ...]}

- `artifact` (optional): an existing `scene3d_points` handle (`artifact:ref_N`); the cloud is then queried without a
  new service call.
- `queries` (optional, 1..32): helper queries run in-process on the cloud, `{op: <helper name>, ...its arguments}`
  (`info`, `table_z`, `points_in_box`, `points_in_cylinder`, `top_z`, `nearest`, `cluster_at_pixel`, `in_hand_axis`,
  `surface_extrema`, `triangulate_rays`); results in `value.query_results`, a failed item gets `status: "error"`.
  On the default head camera without `voxel_m`, a cloud already attached by `features.scene3d_observe` for that
  observation is reused.

## Result (model-facing)

`value`: `artifact` (handle, e.g. `artifact:ref_7`), `points`, `points_above_table`, `near_robot`, `voxel_m`,
`table_z`, `bbox` and `bbox_above_table` ([min, max], env_origin m), `usage` (one paragraph) and `api` (helper
summary). Service diagnostics (timing, npz size, support-plane fit) stay in the stored result.

## Artifact (kind `scene3d_points`, `application/x-npz`)

| array | dtype | meaning |
|---|---|---|
| `xyz` | float32 (N, 3) | env_origin m; one point per voxel (the most confident pixel of that voxel) |
| `conf` | uint8 (N,) | depth-confidence rank of that pixel in the image (0..255 = 0..1) |
| `uv` | uint16 (N, 2) | source pixel (column, row) |
| `count` | uint16 (N,) | pixels in the voxel |
| `robot` | bool (N,) | within 6 cm of a robot link segment (public FK of the measured joints), 3D test only |
| `support_z`, `voxel_m`, `image_size` | scalars | table plane height the cloud was scaled to, voxel size, [W, H] |

Kept: points within [table - `scene3d_z_below_m`, table + `scene3d_z_above_m`] and 3 m of the camera.

## Helpers (`import scene3d_helpers as s3` in program_run)

- `load(artifact)`, `info()`, `table_z(xy=None, radius=0.08)` (global plane, or local median z of points within
  1.5 cm of the plane around xy)
- `points_in_box(lo, hi)`, `points_in_cylinder(center_xy, radius, z_min, z_max)` -> n, centroid, min, max, top_z
  (p98), conf_p50
- `top_z(region, q=98)` with region `(lo, hi)` | `{'center': [x, y], 'radius': r}` | `{'polygon': [[x, y], ...]}`
  (+ optional `z_min`, `z_max`): q-th percentile height of points more than 4 mm above the table
- `nearest(p, k=5)` (2D or 3D), `cluster_at_pixel(u, v, radius=0.015)` (points above the table connected to the
  cloud point whose source pixel is nearest to (u, v); growth limited to 0.4 m)
- `in_hand_axis(grasp_center_m, radius=0.065)` (3D PCA principal axis of the held-object points near the gripper:
  `yaw_deg`, `tilt_deg`, posture, elongation, `bottom_point_m`, `tip_offset_from_grasp_m`; uses `robot=True`),
  `surface_extrema(region, radius=0.04)` (unfiltered local surface `floor_z`, `rim_z`, lowest / highest point),
  `triangulate_rays(u1, v1, K1, T1, u2, v2, K2, T2)` (stereo 3D `point_m` and `skew_gap_m` from two calibrated views)
- keyword args everywhere: `min_conf=0.1`, `robot=False` (True includes points near the arm, e.g. a held object),
  `xyz=False` (True also returns the selected points as an array)

Caveat (same as object_table): VGGT places thin, white or transparent objects too near the camera, so their points
can float above the table (test tubes: 14-21 cm). The helpers do not correct depth per object; they expose `conf` and
`table_z` so a program can check. The object_table rescaling rule is not applied here.

## Service side (VGGT service v3, POST /points)

Same reconstruction and cache as `/height` and `/object_table` (VGGT-Omega depth back-projected with the calibrated K
and T, scaled so the RANSAC table plane lies at `vggt_support_z_m`); voxel downsampling, confidence rank, near-robot
flag; npz returned base64 in JSON. `/height` and `/object_table` are unchanged.

## Configuration

| key | default | meaning |
|---|---|---|
| `features.scene3d` | `true` | publish and serve kind `scene3d`; copy `scene3d_helpers.py` into program workspaces |
| `features.scene3d_voxel_m` | `0.01` | default voxel size |
| `features.scene3d_z_above_m`, `scene3d_z_below_m` | `0.60`, `0.05` | kept height range around the table |
| `features.vggt_endpoint`, `vggt_support_z_m`, `vggt_timeout_s` | shared | service address, table height, timeout |

With the switch off the tool catalog and program workspaces are unchanged (tests/test_scene3d.py). Without a
reachable service the query returns status `unknown` with `reason: scene3d_unavailable:<error>`.
