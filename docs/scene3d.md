# `scene3d` Point Cloud (`features.scene3d`, Enabled by Default)

Calling `geometry_query` with `kind="scene3d"` saves the VGGT-estimated 3D point cloud of a camera view as a compressed NPZ artifact and returns a compact summary handle. The model can inspect the point cloud either inline via the `queries` field or inside `program_run` using the pre-installed `scene3d_helpers` module (automatically copied into every program workspace while `scene3d` or `scene3d_observe` is enabled). Raw point-cloud arrays never enter the model context unless explicitly printed by a program.

`scene3d` complements `object_table`: whereas `object_table` provides per-object bounding geometry from instance segmentation, `scene3d` answers segmentation-free regional and surface queries—such as local tabletop height, container floor and rim elevations, highest obstacle surface in a region, 3D principal axis of a held object in the gripper, or stereo ray triangulation.

## Request Format

```json
{
  "kind": "scene3d",
  "observation_id": "obs-...",
  "camera_id": "cam_head",
  "voxel_m": 0.01,
  "artifact": "artifact:ref_7",
  "queries": [
    {"op": "top_z", "region": {"center": [0.25, -0.10], "radius": 0.05}}
  ]
}
```

- **`artifact`** (optional): Handle of an existing `scene3d_points` artifact (`artifact:ref_N`), allowing the caller to run helper queries against an already stored point cloud without a new VGGT service call.
- **`queries`** (optional, 1 to 32 items): In-process helper operations executed directly on the point cloud, each formatted as `{"op": "<helper_name>", ...arguments}`. Supported operations are `info`, `table_z`, `points_in_box`, `points_in_cylinder`, `top_z`, `nearest`, `cluster_at_pixel`, `in_hand_axis`, `surface_extrema`, and `triangulate_rays`. Results are returned in `value.query_results` (with any failed query reporting `status: "error"`). When querying the default `cam_head` without a custom `voxel_m`, any point cloud already attached to that observation by `features.scene3d_observe` is reused automatically.

## Model-Facing Result

`value` contains:
- `artifact`: Short artifact reference (e.g., `"artifact:ref_7"`).
- `points`, `points_above_table`, `near_robot`: Point counts.
- `voxel_m`, `table_z`: Voxel resolution and fitted tabletop elevation in meters.
- `bbox` and `bbox_above_table`: `[min_xyz, max_xyz]` bounding boxes in `env_origin` meters.
- `usage` and `api`: Concise usage example and function signatures for `scene3d_helpers`.

Internal service diagnostics (latency, NPZ byte size, and RANSAC support-plane fit metrics) remain in the stored record and are omitted from model-facing output.

## NPZ Artifact Schema (`kind="scene3d_points"`, `application/x-npz`)

| Array / Scalar | Dtype & Shape | Description |
|---|---|---|
| `xyz` | `float32 (N, 3)` | 3D coordinates in `env_origin` meters; one representative point per voxel (the pixel with highest depth confidence in that voxel) |
| `conf` | `uint8 (N,)` | Per-image depth-confidence percentile rank (`0..255` mapped to `0.0..1.0`) |
| `uv` | `uint16 (N, 2)` | Source image pixel coordinates `[u, v]` (column, row) |
| `count` | `uint16 (N,)` | Number of raw image pixels falling into that voxel |
| `robot` | `bool (N,)` | `True` if the 3D point lies within `6 cm` of a robot link segment (computed from public FK of the measured joint angles) |
| `support_z`, `voxel_m`, `image_size` | Scalars | Nominal tabletop height the cloud was scaled to, voxel size in meters, and `[width, height]` |

Points are retained within `[table_z - scene3d_z_below_m, table_z + scene3d_z_above_m]` vertically and within `3.0 m` of the camera optical center.

## Helper Module (`import scene3d_helpers as s3` in `program_run`)

- **`load(artifact)`**, **`info()`**, **`table_z(xy=None, radius=0.08)`**: Returns the global tabletop plane height, or the local median $z$ of points within `1.5 cm` of the tabletop plane inside `radius` around `xy`.
- **`points_in_box(lo, hi)`**, **`points_in_cylinder(center_xy, radius, z_min, z_max)`**: Returns `n`, `centroid`, `min`, `max`, `top_z` (98th percentile), and `conf_p50`.
- **`top_z(region, q=98)`**: Accepts `region` as `(lo, hi)`, `{"center": [x, y], "radius": r}`, or `{"polygon": [[x, y], ...]}` (plus optional `z_min`, `z_max`), returning the $q$-th percentile height of points lying more than `4 mm` above the table.
- **`nearest(p, k=5)`** (2D XY or 3D XYZ) and **`cluster_at_pixel(u, v, radius=0.015)`**: Finds above-table points connected within `radius` to the cloud point whose source pixel is closest to `(u, v)` (bounded to a `0.4 m` growth radius).
- **`in_hand_axis(grasp_center_m, radius=0.065)`**: Computes the 3D PCA principal axis of held-object points near the gripper (`yaw_deg`, `tilt_deg`, `posture`, `elongation`, `bottom_point_m`, `tip_offset_from_grasp_m`; automatically includes `robot=True` points near the fingers).
- **`surface_extrema(region, radius=0.04)`**: Returns unfiltered local surface heights (`floor_z`, `rim_z`, and lowest/highest 3D points) without the `4 mm` tabletop cutoff, useful for recessed slots or shallow trays.
- **`triangulate_rays(u1, v1, K1, T1, u2, v2, K2, T2)`**: Computes the 3D midpoint `point_m` and ray-to-ray `skew_gap_m` from pixel coordinates in two calibrated camera views (accepting flexible parameter orderings).
- **Common keyword arguments**: `min_conf=0.1`, `robot=False` (set `robot=True` to include points within `6 cm` of the robot arms, such as a held object), and `xyz=False` (set `xyz=True` to include the matched `(M, 3)` point array in the result).

> [!NOTE]
> Like `object_table`, raw VGGT depth can place thin, white, or transparent objects too close to the camera so their points appear elevated above the table (e.g., `14–21 cm` for thin test tubes). Because `scene3d` does not apply per-object connected-component ray rescaling, `scene3d_helpers` exposes `conf` and `table_z` directly so callers can inspect point confidence and support height.

## Service Pipeline (`POST /points`)

Shares the exact same VGGT-Omega depth reconstruction and in-memory per-image cache as `/height` and `/object_table` (back-projecting depth with calibrated `K` and `T_world_camera` and scaling the RANSAC tabletop plane to `vggt_support_z_m`), then performs 3D voxel downsampling, computes confidence ranks and the `6 cm` robot-link proximity mask, and returns the compressed NPZ payload encoded in base64 JSON.

## Configuration

| Configuration Key | Default | Description |
|---|---|---|
| `features.scene3d` | `true` | Publishes `geometry_query` `kind="scene3d"` and provisions `scene3d_helpers.py` into program workspaces |
| `features.scene3d_voxel_m` | `0.01` | Default voxel grid resolution in meters (`1 cm`) |
| `features.scene3d_z_above_m`, `scene3d_z_below_m` | `0.60`, `0.05` | Vertical clipping window (`[table_z - 0.05, table_z + 0.60]` m) retained in the NPZ artifact |
| `features.vggt_endpoint`, `vggt_support_z_m`, `vggt_timeout_s` | Shared | VGGT HTTP service URL, nominal tabletop height (`0.765 m`), and request timeout (`60 s`) |

When `features.scene3d` is `false` (and `scene3d_observe` is `false`), the tool catalog and program workspaces remain completely unchanged (`tests/test_scene3d.py`). If the VGGT service is unreachable, queries return `status: "unknown"` with `reason: "scene3d_unavailable:<error>"`.
