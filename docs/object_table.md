# Object Table (`features.object_table`, Enabled by Default)

Calling `geometry_query` with `kind="object_table"` returns an estimated 3D geometric table of objects on the support surface so the model does not have to eyeball object heights and footprints from 2D images alone. The tool reports 3D geometry only; it never names objects on its own or selects manipulation targets.

## Request Format

```json
{
  "kind": "object_table",
  "observation_id": "obs-...",
  "camera_id": "cam_head",
  "pixels": [[u, v], ...],
  "labels": ["..."],
  "target_camera_ids": ["cam_left_wrist", "cam_right_wrist"]
}
```

- **`camera_id`**: Camera view to segment (defaults to `"cam_head"`).
- **`pixels`** (optional, up to 16 `[u, v]` pairs): Specific image coordinates on objects the caller wants guaranteed in the table even if automatic segmentation missed them. Each pixel triggers a SAM 2.1 point-prompt mask, and `value.requested` maps each requested pixel to its resulting object ID.
- **`labels`** (optional, up to 16 noun phrases, truncated to 80 characters each): Free-text noun phrases chosen by the model and forwarded verbatim to the VGGT service (`object-table-v5`), which segments each phrase using SAM 3.1 text prompts. Automatic unlabelled rows come from the fixed class-agnostic prompt `"object"`; labelled instances are inserted first, and any generic `"object"` instance that duplicates or lies inside a labelled instance is suppressed. Returned rows include an extra `label` column (the highest-scoring matching phrase for that object, or `null` for pixel-only objects), and `value.labels` reports `{label, status: "ok" | "no_match" | "filtered" | "backend_unavailable" | "unsupported_backend", ids}` for each phrase. One phrase may match multiple object IDs, and one object ID may match multiple phrases (displaying the highest-scoring phrase in the row). The Harness never invents object labels itself. If the SAM 3.1 sidecar is unreachable, the service automatically falls back to the SAM 2.1 point grid (`status: "backend_unavailable"`).
- **`target_camera_ids`** (optional, up to 3 cameras): Additional synchronized cameras from the same observation (typically wrist cameras). Each object's estimated 3D center is projected into those views using their calibrated intrinsics and extrinsics (this is a geometric 3D-to-2D center projection without occlusion checking), and `value.projected_ids[camera_id]` lists the object IDs whose projected centers fall within that camera's image bounds.

## Model-Facing Result

`value.columns` and `value.rows` contain one row per detected object with IDs `o1..oN` ($N \le$ `object_table_max_objects`, default `20`; `value.omitted` reports the count of additional objects dropped by the cap):

| Column | Description |
|---|---|
| `centre_m` | `[x, y, z]` in `env_origin` meters: horizontal footprint center of the visible mask points, with $z = (\text{top\_z} + \text{bottom\_z}) / 2$ |
| `top_z`, `bottom_z` | `top_z` is the 98th-percentile height of visible mask points; `bottom_z` is the table height, the top of a supporting object underneath, or the lowest visible point when no support is found |
| `length`, `width`, `yaw_deg` | Horizontal extents along and across the 2D principal axis (PCA) of visible points, and the yaw angle of that principal axis in $[-90^\circ, 90^\circ)$ |
| `tilt_deg` | Angle of the 3D principal axis from vertical (populated only for elongated point sets with aspect ratio $\ge 1.8$, otherwise `null`) |
| `support` | `"table"`, the object ID (`oK`) of the supporting object directly underneath, `"held_<arm>"`, or `"unknown"` |
| `quality` | `"ok"`, `"held_fk_depth"`, or `"low:<flags>"` (see quality flags below) |

Each call also returns one overlay image per camera: object contours and IDs drawn on `camera_id`, plus projected object IDs drawn at their 3D centers on each requested `target_camera_id`. Internal service diagnostics (mask pixel counts, 3D point counts, depth-confidence percentiles, and stage latencies) are saved in the Store under `diagnostics` and omitted from model-facing responses.

### Quality Flags

Computed purely from visual and geometric consistency without simulator ground truth:
- `height_above_max`: `top_z` exceeds the table height by more than `object_table_max_height_m` (default `0.30 m`).
- `low_depth_confidence`: Median VGGT depth confidence of mask pixels falls in the bottom 25% of the image.
- `contact_mismatch`: The lowest mask pixels, when back-projected onto the table plane, lie more than `3 cm` farther from the camera than the estimated 3D center (indicating that monocular depth placed the object too high or too close to the camera).
- `no_support_below`: The lowest visible point lies more than `2 cm` above the table with no supporting object detected underneath.
- `few_points` / `fragmented_points`: Sparse or spatially disconnected 3D point clusters.

## Service Pipeline (`POST /object_table`)

1. **VGGT-Omega Reconstruction**: Runs a single VGGT-Omega forward pass on the image, back-projects depth using the calibrated camera intrinsics `K` and rotation `T_world_camera`, and scales the point cloud so the RANSAC-fitted tabletop plane sits at `vggt_support_z_m` (sharing the per-image reconstruction cache with `/height` and `/points`).
2. **Candidate Segmentation**:
   - **v5 default (`seg_backend = "sam31"`)**: Queries the SAM 3.1 text-prompt sidecar (`sam31_service.py` on `127.0.0.1:8333`) with any caller-supplied `labels` plus the generic prompt `"object"` (score threshold `0.4`).
   - **v2–v4 fallback (`seg_backend = "sam21"`)**: Evaluates SAM 2.1 point prompts on an `8 px` grid restricted to pixels whose VGGT height lies between `4 mm` and `45 cm` above the table (3 candidate masks per point; keeping masks with predicted IoU $\ge 0.5$, stability $\ge 0.8$, area between `40 px` and 8% of the image, $\ge 60\%$ of pixels above the table, and not touching image borders or the head camera mount exclusion box).
3. **Filtering and Deduplication**: Selects masks greedily from largest to smallest, skipping masks with $> 30\%$ overlap against already selected masks. Discards masks overlapping the robot arm (via the rendered FK pixel mask in `v5-hp1`, or within `6 cm` of FK link capsules in fallback mode), background masks whose top exceeds `45 cm` above the table, fragmented clusters, and flat table-texture masks with less than `4 mm` height relief over their surrounding border ring.
4. **Per-Object 3D Geometry**: Erodes each mask, keeps pixels with depth confidence above the image 10th percentile, extracts the largest `1 cm` DBSCAN 3D cluster, and computes the table columns above.
5. **Floating-Object Re-Anchoring (`object-table-v2`)**: Monocular VGGT depth often places thin, reflective, or transparent tabletop objects (such as test tubes) too close to the camera so their 3D points appear to float in mid-air. When an unheld object's lowest visible point lies more than `3 cm` above the table with no supporting object underneath **and** that vertical gap exceeds the object's visible thickness ($\text{top\_z} - \text{lowest\_z}$), the point cluster is rescaled along camera rays until its lowest point rests on the table (annotated with `floating_rescaled_to_table`). Objects whose vertical gap is smaller than their visible thickness (such as spheres whose bottom hemisphere is self-occluded) keep their unscaled geometry and receive the `floating` flag.

Typical latency on an RTX 4090 is **~0.5 s** with SAM 3.1 (**~1–2 s** with the SAM 2.1 grid), and faster when the VGGT reconstruction is already cached.

### Evolution from v4 to v5

- **v4 (Connected-component point matching)**: A caller-requested `[u, v]` pixel reuses an existing segmented object only when the pixel lies inside that mask's largest connected component and the SAM 2.1 point-prompt mask at `[u, v]` has IoU $\ge 0.5$ with it; otherwise, the point mask is added as a separate row (fixing cases in v1–v3 where clicking a test tube inside a rack resolved to the rack's mask). Two requested pixels resolving to the same object receive status `ok_same_object`.
- **v5 (SAM 3.1 text backend)**: Switching default segmentation from the SAM 2.1 point grid to SAM 3.1 text prompting improved offline recall on 28 benchmark frames from `138/191` to `146/191` task objects, reduced false-positive background IDs from `24` to `0`, improved center XY p90 error from `3.7 cm` to `2.0 cm` and top-Z p90 error from `3.0 cm` to `2.5 cm`, and reduced median latency from `2.07 s` to `0.46 s`. Passing `seg_backend: "sam21"` reproduces v4 behavior exactly.

## Configuration

| Configuration Key | Default | Description |
|---|---|---|
| `features.object_table` | `true` | Publishes and serves `geometry_query` `kind="object_table"` |
| `features.object_table_max_objects` | `20` | Maximum number of object rows returned per call |
| `features.object_table_max_height_m` | `0.30` | Height threshold above the table for the `height_above_max` quality flag |
| `features.object_table_head_exclude_px` | `"200,405,440,480"` | Pixel bounding box `[u_min, v_min, u_max, v_max]` excluded in `cam_head` to mask out the fixed camera mount (empty string disables) |
| `features.vggt_endpoint`, `vggt_support_z_m`, `vggt_timeout_s` | Shared with `vggt_height` | VGGT HTTP service URL, nominal tabletop height (`0.765 m`), and request timeout (`60 s`) |

When `features.object_table` is `false`, the published tool catalog is byte-identical to a build without this module (`tests/test_object_table.py`). If the VGGT service is unreachable at runtime, the query degrades gracefully by returning `status: "unknown"` with `reason: "object_table_unavailable:<error>"`. When `analysis.scene_truth_log` is enabled, `scripts/score_geometry_truth.py` evaluates every reported object center (`plane_source = "object_table"`) and top-height error (`ot_top_minus_truth_top_z_m`) offline.
