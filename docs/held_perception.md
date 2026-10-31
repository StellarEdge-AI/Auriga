# Held-Object Perception (`features.held_perception`)

Enabled by default alongside the other external-perception switches (`features.external_perception`; set `held_perception: false` to disable this component individually). `held_perception` modifies only what object tables return (`geometry_query` with `kind="object_table"` and the table attached automatically by `features.object_table_observe`); it never moves the robot or alters executed actions. Its inputs are strictly permitted observations: RGB images, camera calibration, measured joint angles and gripper aperture, public robot meshes from the RoboDojo asset directory, and the Harness's own earlier object tables. No simulator ground truth, asset metadata, or hidden object names are used.

- **Client implementation**: `auriga/perception/external/held_perception.py` (robot silhouette mask, holding-state tracker, held-object row synthesis), with hooks in `auriga/perception/external/object_table.py` (request fields, row merging, diagnostics), `auriga/perception/external/obs3d.py` (`held` metadata passed through to attached tables), and `auriga/apps/gateway/workbench.py` (one developer-instruction paragraph).
- **Service implementation**: `services/vggt_height/vggt_height_service.py` (`object-table-v5-hp1`; see [Service Fields](#service-fields)).
- **Tests**: `tests/test_held_perception.py`.

## What It Fixes

Standard tabletop object tables suffer from three systematic failure modes when an object is held inside a closed gripper:

1. **Link-capsule filtering deletes held objects.** Previously, robot pixels were excluded by placing `6 cm` cylinders/capsules around robot link segments and dropping any segmentation mask that overlapped a capsule. Because a grasped object always lies within `6 cm` of the gripper fingers, the table omitted the held object in almost every frame—in offline replays, only **53 of 373** holding observations (14%) contained a row for the held object.
2. **Floating-to-table re-anchoring distorts lifted objects.** Masks whose back-projected points float above the table plane are normally rescaled along camera rays down to the table surface (a heuristic designed for tabletop objects whose monocular VGGT depth reads too shallow). Applying that heuristic to an object lifted into the air shifts its 3D position and shrinks its estimated size.
3. **Monocular VGGT depth is unanchored in mid-air.** VGGT point clouds are globally scaled to match the RANSAC tabletop plane; for an object suspended in mid-air beside the gripper, the tabletop plane fit does not constrain its depth along the camera ray, whereas forward kinematics (FK) of the robot arm gives the exact 3D grasp center and camera depth. The held-object row therefore anchors depth to robot FK and uses VGGT/pre-grasp geometry solely for object extents and orientation.

## How It Works (When Enabled)

### 1. Exact Robot Pixel Mask

The Harness renders the public robot link meshes (`base_link` through `link8` plus the camera mounts of both arms, decimated to `2 mm`) using the requested camera's intrinsics `K`, extrinsics `T_world_camera`, and measured joint forward kinematics (with fingers placed at the measured jaw aperture), dilates the rendered silhouette by `3 px`, and sends it to the VGGT service as `robot_mask_b64`. The service discards any candidate mask with $\ge 50\%$ robot pixels and subtracts robot pixels from every remaining mask before computing 3D geometry. A wrist camera omits its own arm's camera housing mesh. If robot meshes or FK are unavailable, the request transparently falls back to `6 cm` link capsules and records the reason in `diagnostics.held_perception.error`.

### 2. Held-Object Row Synthesis

An arm is classified as holding an object whenever its jaws are commanded closed and have stalled before full closure (the `held_end` test: `goal < 0.5` and `0.002 m < opening < full_opening - 0.004 m`). Its kinematic grasp center is $G = p_{\text{link6}} + R_{\text{link6}} [0.1501, 0, 0]^T$.

- **v2 (default, `EXTENT_MODE = "pregrasp"`)**: The Harness tracks the gripper state across every saved observation; the first holding observation of a continuous holding streak marks the grasp closure. The first time an object table is requested during that streak, the tracker searches the 4 most recent object tables computed prior to closure for a pre-grasp row within `6 cm` in XY of $G$ at closure (prioritizing rows whose segmentation mask contains the projected pixel of $G$), recording its 3D extents, center, yaw, tilt, center offset in the `link6` frame, and oriented bounding footprint. Objects are categorized by aspect ratio from that pre-grasp row:
  - `tube-like`: $\text{length} \ge 0.09\text{ m}$ and $\text{width} \le 0.025\text{ m}$
  - `elongated`: $\text{length} \ge 2.5 \times \text{width}$
  - `compact`: all other shapes
  For `compact` objects, the held center is $G$; for `elongated` and `tube-like` objects, the center is $G$ shifted along the principal axis by the pre-grasp along-axis offset (`carried_axis`). The long axis uses the static kinematic prior (`link6` $z$-axis, or `link6` $x$-axis when the pre-grasp row was upright), and the 3D bounding box is carried rigidly with `link6` and re-measured in the world frame. Releasing the jaws clears the carried state, and a subsequent re-grasp captures a fresh pre-grasp row.
- **v1 fallback (used when no closure observation, pre-grasp table, or matching row within `6 cm` exists, or when `EXTENT_MODE = "gk"`)**: Queries SAM 3.1 for an `"object"` instance (plus the grasp row label when available) containing or within `40 px` of the projected $G$ with robot pixels subtracted (falling back to a SAM 3.1 point prompt), sets the center to $G$, and computes extents by rescaling the mask's VGGT points along camera rays so that points adjacent to $G$ match the FK depth of $G$ (preferring the holding arm's wrist camera when its mask has $\ge 100\text{ px}$, else the requested camera). The row note records which fallback path applied.

### 3. v2.1 Consistency Guards

Defined as module constants in `auriga/perception/external/held_perception.py` (with shipped defaults, v2.1 produces identical rows to v2 across all 633 held rows in the offline truth benchmark):

- **Axis-consistency gate (`AXIS_CONFLICT_DEG = 45.0`, `AXIS_CONFLICT_CENTRE = "grasp"`)**: When the carried pre-grasp principal axis deviates by more than `45°` from the static kinematic prior, the object was not grasped across its principal axis as assumed by the prior. The tracker then reports the carried axis, sets the center according to `AXIS_CONFLICT_CENTRE`, and records `axis_conflict_deg` in the row note and `value.held[id]` (`None` disables the gate).
- **Partial pre-grasp row guard (disabled by default: `PARTIAL_ROW_RATIO = None`, `PARTIAL_ROW_CONTAINS = False`, `PARTIAL_ROW_REF = ("mask",)`, `GROUP_FROM_MAX = False`)**: Optional guard that falls back to v1 `gk` extents (`extent_source = "gk_partial_row"`) when a pre-grasp row is shorter than `PARTIAL_ROW_RATIO` times a reference length (`mask` outline or held `gk` extent) or does not contain the projected grasp center. Kept off by default because offline replays showed higher test-tube center errors (`5.0 / 24.8 mm` vs. `2.9 / 10.6 mm`) when enabled.
- **Row provenance metadata (always enabled with `held_perception`)**: `value.held[id]` includes `pregrasp_row_centre_m` and `pregrasp_table_source` (recording the per-run object-table sequence number, request kind, observation ID, and camera ID of the source pre-grasp table).

The synthesized held row replaces any segmented object row sharing the same mask (or is appended if none matched), tagged with `support="held_<arm>"` and `quality="held_fk_depth"`, and is never re-anchored to the table plane. `value.held[o<i>]` reports `source` (`"held_pregrasp_row"` or `"held_fk_depth"`), `arm`, `group`, `centre` (estimator mode), `camera_id`, `note` (method and offline p50/p90 accuracy reference), `long_axis` (direction, source, carried, prior) or `orientation_prior`, and—for v2—`pregrasp_row_id`, `pregrasp_observation_id`, `closure_observation_id`, and `extent_source`. Unheld tabletop objects continue to use the standard table-plane scaling path.

Developer instructions gain one concise paragraph (`held_perception.INSTRUCTION`) explaining that rows with `quality="held_fk_depth"` are conditional geometry estimates assuming rigid attachment to the gripper since closure: neither a partially closed jaw nor a carried pre-grasp row proves physical retention, and even a current segmentation mask is not a co-motion check, so actual retention, slip, and placement support must be verified from RGB views and action feedback.

## Offline Ground-Truth Evaluation

Saved episode observations were replayed through the pipeline and compared against simulator ground truth (used exclusively for offline evaluation):

| Metric | Without `held_perception` | v1 (`gk` Rescale) | v2 (Pre-Grasp Carried) |
|---|---|---|---|
| Held object present in table (373 holding observations) | 53 / 373 (14%) | 373 / 373 (100%) | 373 / 373 (100%) |
| Held center XY error p50 / p90, all held rows ($n = 633$) | — | 11.4 / 31.6 mm | **4.3 / 24.8 mm** |
| Held center XY error p50 / p90, v1 subset ($n = 370$) | — | 6.9 / 33.0 mm | **5.1 / 28.7 mm** |
| Held center XY error p50 / p90, test tubes with pre-grasp row ($n = 285$) | — | 22.0 / 31.4 mm | **2.9 / 10.6 mm** |
| Held center XY error p50 / p90, elongated ($n = 167$) / compact ($n = 173$) | — | 14.9 / 29.7, 5.8 / 28.2 mm | **2.5 / 10.1**, 5.8 / 28.2 mm |
| Length error p50 / p90, `tube-like` | — | 43 / 74 mm | **13 / 30 mm** |
| Length error p50 / p90, all held objects | — | 43 / 97 mm | **17 / 61 mm** |
| Principal-axis angular error p50 / p90 (static prior), `tube-like` / `elongated` | — | — | **2.2° / 6.8°**, **2.8° / 19.6°** |
| Tabletop objects: center shift on paired rows p50 / p90 ($n = 1985$) | — | 0.00 / 3.7 mm | 0.00 / 3.7 mm |
| Robot mask vs. simulator ground-truth render IoU p50 (p10), 81 head frames | — | 0.959 (0.950) | 0.959 (0.950) |
| Robot mask render latency per head observation (max) | — | 0.14 s (0.19 s); wrist 0.01 s | Same |

In the 633-row evaluation set, 511 rows (81%) matched a pre-grasp row via v2; the remaining cases fell back to v1 due to no pre-grasp row within `6 cm` (96), no recorded closure observation (25), or a query prior to closure (1). After `3 px` dilation, the rendered robot mask covers 100% of true robot pixels while trimming ~4% of adjacent object pixels.

Known limitations: the grasp center is anchored at the FK grasp point at closure, in-hand slip after closure is not modeled (`0.4 / 5.2 mm` median/p90 drift on test tubes between closure and pre-placement), VGGT overestimates thin tube widths by ~`13 mm` (which carried pre-grasp widths inherit), and ground-truth bounding boxes include caps and handles.

## Configuration and Service Compatibility

`held_perception` is enabled by default in shipped configurations:

```json
{
  "features": {
    "object_table": true,
    "object_table_observe": true,
    "held_perception": true,
    "vggt_endpoint": "http://127.0.0.1:8331"
  }
}
```

`vggt_endpoint` should run `object-table-v5-hp1` (`GET /health` reports `object_table: "object-table-v5-hp1"` and `held: "held-v1"`; see [`services/vggt_height/README.md`](../services/vggt_height/README.md)). If `vggt_endpoint` points to an older `object-table-v5` instance (`answer.version == "object-table-v5"` without `robot_mask`), the client automatically falls back to re-querying with `robot_segments_m` link capsules, skips `POST /held`, and still synthesizes client-side v2 pre-grasp carried rows (or FK grasp-center fallback rows), recording `service_fallback: "object-table-v5_no_hp1"` under `diagnostics.held_perception`. `backend.root` must point to the RoboDojo checkout containing `Assets/Robots/x5/meshes`.

When `features.held_perception` is `false`, the tool catalog, developer instructions, and object tables are completely unchanged.

## Service Fields

- **`POST /object_table` request additions**: `robot_mask_b64` (base64-encoded `np.packbits` of the row-major `bool[H, W]` silhouette mask), `robot_mask_shape: [H, W]`, and `held: [{arm, grasp_centre_m, phrases?}]` (up to 2 arms).
- **`POST /object_table` response additions**: `robot_mask: "pixels"`, `held` (per item: `status`, `mask_source`, `phrase`, `mask_px`, `dist_px`, `centre_m`, `grasp_px`, `grasp_depth_m`, `contour_px`, `label_px`, `bbox_px`, `gk` containing `length`, `width`, `top_z`, `low_z`, `yaw`, `elongation`, and scale factor `k`, plus `object_index` and `object_iou`), and `held_s`.
- **`POST /held`**: Computes the same per-item `held` results for a single image without building a full tabletop object table (used for the holding arm's wrist camera).

## Explicit Motion and Held-Evidence Contract

Grasp-angle planning strictly preserves the caller's requested orientation: infeasible poses return a planning failure so the actor can explicitly choose new angles and preview again, while joint-space replanning remains restricted to equivalent configurations that keep the Cartesian target unchanged. Vertical compliance never relaxes the declared orientation tolerance, and downward error remains bounded by the declared position tolerance. Similarly, `contact_stop` modifies only stopping behavior and the documented upward compliance window, never horizontal or angular tolerances.

Held-object metadata explicitly reports:
- `retention_status="unverified"` (no physical retention assertion)
- `attachment_assumption="rigid_since_grasp"` (conditional kinematic carry model)
- `geometry_evidence="current_mask" | "prediction_only"` (whether a usable segmentation mask is visible in the current observation)
- `current_mask_views` (list of head/wrist cameras with usable masks)

These categorical fields carry no physical units and are populated for both v2 carried rows and v1 fallback rows. A `current_mask` indicates current segmentation visibility rather than verified co-motion, while `prediction_only` rows remain available through occlusions without asserting a successful grasp or continued retention.
