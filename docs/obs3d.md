# `obs3d`: Zero-Turn 3D Scene Information Attached to Default Observations

Two configuration switches—both enabled by default under `features.external_perception`—automatically attach the 3D scene representations of `geometry_query` (`object_table` and `scene3d`) to every full model-facing observation reply. Delivering this 3D context inline eliminates extra tool-call turns and ensures that benchmark ablations measure the value of the 3D information itself rather than whether the model chooses to invoke an on-demand query tool.

- **Implementation**: `auriga/perception/external/obs3d.py`
- **Tests**: `tests/test_obs3d.py`

## Configuration Switches

| Configuration Key | Default | Effect |
|---|---|---|
| `features.object_table_observe` | `true` | Attaches the head-camera `object_table` JSON to every full observation reply (plus an ID overlay image on explicit `robot_observe` calls) |
| `features.scene3d_observe` | `true` | Saves the head-camera 3D point cloud as a `scene3d_points` NPZ artifact and attaches its short artifact handle |
| `features.obs3d_timeout_s` | `60` | Per-request HTTP timeout (seconds) for the VGGT service attachment calls |
| `features.obs3d_max_objects` | `10` | Maximum object rows included in the inline attached table (on-demand `geometry_query` calls retain `object_table_max_objects = 20`) |
| `features.obs3d_action_overlay` | `false` | Experimental: also attaches the visual ID overlay image to `robot_action` and `robot_wait` replies |

The on-demand tool switches (`features.object_table` and `features.scene3d`) remain independent: when enabled, the model can still call `geometry_query(kind="object_table")` (for custom text labels, point prompts, or wrist-camera projections) and `geometry_query(kind="scene3d")` (for custom voxel resolutions or wrist-camera point clouds). Whenever either `scene3d` or `scene3d_observe` is enabled, `scene3d_helpers.py` is automatically provisioned into every `program_run` workspace.

## Which Tool Replies Receive `obs3d` Attachments

Attachments are added **only to primary actor model replies** (never to internal program tool calls) and **only when the reply carries a full observation that includes `cam_head`**:

| Tool Reply Type | Inline JSON Text Part | Visual ID Overlay Image |
|---|---|---|
| `robot_observe` without `camera_id` (full observation) | Yes | Yes |
| `robot_observe` with `camera_id`, `roi`, or `scale` (single-camera or cropped view) | No | No |
| `robot_action` with a terminal observation (`mode="execute"` or executed `direct`) | Yes | Only when `obs3d_action_overlay=true` |
| `robot_wait` (attached to the terminal observation; intermediate temporal frames receive nothing) | Yes | Only when `obs3d_action_overlay=true` |
| `robot_action` `preview` / `inspect`, `robot_policy_step`, legacy execution tools, or program callers | No | No |

This split reflects empirical usage patterns: in recorded `insert_tubes` episodes, the actor called `robot_observe` 1.2 times per episode on average, whereas 8.6 `robot_action` / `robot_wait` replies per episode carried a terminal observation. Attaching the compact JSON table to every terminal action observation gives the model continuous 3D state updates, while restricting the `640 × 480` ID overlay image to explicit `robot_observe` calls avoids bloating image counts on every motion step.

For each new observation, the Harness makes at most one service call per enabled kind (`/object_table` followed by `/points`, which reuses the cached VGGT reconstruction on the server). The most recent result is cached in memory by `observation_id`: if the model subsequently calls `robot_observe` at the same paused state covered by the preceding action reply, the cached rows and point-cloud artifact handle are reused immediately (appending the cached ID overlay image without re-querying the GPU service).

## Attached Reply Format

Immediately after the standard observation JSON and camera images, the response builder appends one JSON text item and (on `robot_observe`) one overlay image:

- **With `object_table_observe=true`**:
  ```json
  {
    "observation_id": "obs-...",
    "object_table": {
      "columns": ["id", "centre_m", "top_z", "bottom_z", "length", "width", "yaw_deg", "tilt_deg", "support", "quality"],
      "rows": [...],
      "table_z": 0.765,
      "note": "...",
      "grasp_hints": {...},
      "omitted": 0,
      "held": {...}
    }
  }
  ```
  `columns` and `rows` follow the standard `object_table` schema (IDs `o1..oN`, 3D center, top/bottom $z$, horizontal length/width/yaw, 3D tilt, support, and quality flags, with no hidden object names or simulator truth). `grasp_hints` maps each object ID to precomputed grasp reference geometry (`pad_z_m` set to the object's center $z$ and adjusted for thin tabletop or stacked objects, `fingertip_support_clearance_m`, `opening_check` against the `88.8 mm` jaw opening, `elongated`, principal-branch `across_yaw_deg = yaw_deg ± 90°` for horizontally elongated objects, and `stand_upright_angles_deg` for lying elongated objects). `held` is populated by `features.held_perception` when an arm is holding an object. On `robot_observe`, an overlay image of `cam_head` with object contours and `o1..oN` labels is appended using the active image transport encoding.
- **With `scene3d_observe=true`**:
  ```json
  {
    "observation_id": "obs-...",
    "scene3d": {
      "artifact": "artifact:ref_7",
      "points": 14820,
      "table_z": 0.765,
      "use": "import scene3d_helpers as s3; s3.load(artifact); print(s3.API)"
    }
  }
  ```
  To keep inline token overhead minimal, the full bounding boxes and helper docstrings returned by on-demand `geometry_query(kind="scene3d")` are omitted here and remain accessible via `scene3d_helpers.API`.
- **With both switches enabled**: A single JSON text item containing both `"object_table"` and `"scene3d"` keys, followed by the overlay image (on `robot_observe` only).
- **Graceful degradation on service error or timeout**: If the VGGT service is unreachable, times out, or returns an error, the corresponding key is set to `"unavailable: <reason>"` (`service_down`, `timeout`, or the service error code) and the observation and action receipt are delivered normally. Any unexpected exception during action-reply attachment is caught and logged so a completed physical action never fails due to an auxiliary perception error.

Neither the internal `ObservationBundle` nor the typed action receipt is mutated. Each attachment logs an internal `geometry.obs3d_attached` event (recording reply type, latencies, row count, artifact ID, service timing, and `reused`), and any delivered overlay image is logged as `geometry.overlay_delivered` with `source_kind="obs3d_object_table_overlay"`.

## Context Management and Prompt Cache Behavior

Because the `obs3d` items are regular parts inside the tool output, `lean_projection` handles them seamlessly: inside the open block and on anchor steps they are sent in full, while in closed blocks the JSON text is condensed to a single-line summary and the overlay image becomes a standard placeholder:

```text
[summary <tool>] {"obs3d_of": "<observation_id>", "object_rows": N | "unavailable", "scene3d": "stored" | "unavailable"}
```

This summary consumes just `20 / 21 / 26` `o200k_base` tokens (for table-only / cloud-only / both).

When enabled, `obs3d.developer_instructions` adds a concise paragraph to the startup developer instructions describing the attached `object_table`, `grasp_hints`, and `scene3d` fields and explaining how to invoke on-demand `geometry_query` for custom text labels or wrist-camera projections. In the tool catalog, only a single sentence is appended to `robot_observe`:

> Head-camera observations here and in robot_action / robot_wait replies also carry the frame's VGGT-estimated object table (ids o1..oN: centre, top/bottom z, footprint, tilt, support, quality); robot_observe adds an id overlay image.

This adds 233 characters to the tool catalog for `object_table_observe`, 198 for `scene3d_observe`, and 288 for both (and 0 characters when both switches are `false`).

## Token, Latency, and Storage Overhead

Measured across 10 recorded `insert_tubes` episodes (98 full observations: 86 action/wait replies and 12 `robot_observe` replies) against the live VGGT service using the default `lean_v1` payload profile:

| Active Switches | Action Replies ($n=86$): Added Tokens (min / p50 / mean / p90 / max) | Extra Images (Actions) | `robot_observe` Replies ($n=12$): Added Tokens (p50 / max) & Overlays | Service Latency p50 (max) per Action |
|---|---|---|---|---|
| `object_table_observe` only | 91 / 182 / 172 / 275 / 291 | 0 | 258 / 287 tokens, 12 overlays | 0.40 s (0.56 s) |
| `scene3d_observe` only | 62 / 62 / 62 / 62 / 62 | 0 | 62 / 62 tokens, 0 overlays | 0.36 s (0.48 s) |
| Both enabled (default) | 144 / 235 / 225 / 328 / 344 | 0 | 311 / 340 tokens, 12 overlays | 0.58 s (0.71 s) |

Because a standard `robot_action` reply with an observation already carries ~5,847 text tokens (median) plus camera images, the inline JSON adds only **~3%** (table), **~1%** (cloud), or **~4%** (both) to the text of an action reply (scaling by ~45 tokens per detected object row).

### Cumulative Context Growth Across an Episode

An inline text item of $t$ tokens appended at step $i$ of an $N$-step episode is re-sent in every subsequent request (mostly served from the upstream prompt cache), adding $t \times N$ tokens to the final context and $t \times N(N-1)/2$ cumulative input tokens across the episode:

| Variant | Worst Case (60 Steps, Every Call Carries an Observation): Final / Cumulative | Empirical Rate (60 Steps, 19.8% of Calls Carry an Observation): Final / Cumulative | Measured 10 Episodes (32–73 Calls, Mean 49.5): Mean Cumulative Input |
|---|---|---|---|
| `object_table_observe` | +10.3k / +305k | +2.0k / +60k | +46.5k (actions alone: +33.9k) |
| `scene3d_observe` | +3.7k / +110k | +0.7k / +22k | +14.8k |
| Both enabled | +13.5k / +399k | +2.7k / +79k | +59.1k |

Each `scene3d_observe` attachment archives one compressed `scene3d_points` NPZ artifact (`0.26–0.42 MB`, median `0.28 MB`, totaling ~`2.4 MB` per episode at 8.6 action observations).

## Service Architecture and Concurrency

Both switches query the local VGGT service at `features.vggt_endpoint` (default `http://127.0.0.1:8331`, running `object-table-v5-hp1` with the SAM 3.1 sidecar on `127.0.0.1:8333` and `scene3d-points-v1`; see [`services/vggt_height/README.md`](../services/vggt_height/README.md)). When both switches are enabled, the client dispatches `/object_table` first and `/points` a few milliseconds later from a background thread; because the service processes requests FIFO on a single GPU worker, `/points` hits the in-memory VGGT reconstruction cache populated by `/object_table`.

## Offline Ground-Truth Scoring

When `analysis.scene_truth_log` is enabled, `auriga/perception/external/obs3d.py` logs an internal `analysis.obs3d_object_table` event for each attached table (containing only model-visible estimates, never entering model replies). `scripts/score_geometry_truth.py` scores these attached tables under `kind="object_table_obs"` (`plane_source="object_table_obs"`), counting one table per unique observation and matching each row to the nearest ground-truth task object in XY to report center XY error, center Z error, top-Z error, and `table_top - truth_top_z` alongside on-demand `geometry_query` calls.
