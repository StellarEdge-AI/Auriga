# obs3d: 3D scene information as part of the default observation

Two switches, both on by default (part of `features.external_perception`), make the information of the on-demand
`geometry_query` kinds `object_table` and `scene3d` part of every model observation reply, so that an ablation
measures the information itself rather than whether the model decides to call a tool.
Code: `auriga/perception/external/obs3d.py`; tests: `tests/test_obs3d.py`.

## Switches

| key | default | effect |
|---|---|---|
| `features.object_table_observe` | `true` | attach the object table of the head frame (+ one id overlay image on `robot_observe`) |
| `features.scene3d_observe` | `true` | save the head-frame point cloud as a `scene3d_points` artifact, attach its handle |
| `features.obs3d_timeout_s` | `60` | per-request service timeout for the attachment |
| `features.obs3d_max_objects` | `10` | rows of the attached table (the on-demand kind keeps `object_table_max_objects`) |
| `features.obs3d_action_overlay` | `false` | also attach the id overlay image to `robot_action` / `robot_wait` replies (experiments) |

The on-demand switches `features.object_table` / `features.scene3d` are independent: with them
on, `geometry_query object_table` (labels, pixels, wrist targets) and `geometry_query scene3d` (other voxel sizes,
wrist cameras) stay available. `scene3d_helpers.py` is copied into `program_run` workspaces when either
`scene3d` or `scene3d_observe` is on.

## Which replies get it

Only replies to the model (not to program jobs) and only full observations that have `cam_head`:

| reply | text part | overlay image |
|---|---|---|
| `robot_observe` without `camera_id` | yes | yes |
| `robot_observe` with `camera_id` / `roi` / `scale` (single-camera / ROI view) | no | no |
| `robot_action` with a final observation (mode `execute`, `direct` that executed) | yes | only with `obs3d_action_overlay` |
| `robot_wait` (final observation; the temporal head-camera samples get nothing) | yes | only with `obs3d_action_overlay` |
| `robot_action` preview / inspect, `robot_policy_step`, legacy execute tools, program callers | no | no |

The split follows the call pattern: in recorded insert_tubes episodes the model called `robot_observe` 1.2 times
per episode, while 8.6 `robot_action` / `robot_wait` replies per episode carried a final observation. The table text
goes on every such reply; the overlay image (640x480 in RoboDojo) only on explicit `robot_observe` calls.

One service call per kind and observation (/object_table, then /points, which reuses the reconstruction on the
service). The last successful result is kept for the most recent observation id, so a `robot_observe` of an
observation that an action reply already covered reuses the rows and the artifact without a new call (the overlay is
then added from the kept result).

## Reply format

After the observation metadata and the camera images the builder appends one JSON text part and at most one image:

- `object_table_observe`: `{"observation_id", "object_table": {"columns", "rows", "table_z", "note", "grasp_hints"?,
  "omitted"?, "held"?}}`. Columns and rows are those of the `object_table` kind (ids o1..oN, centre, top/bottom z,
  length, width, yaw, tilt, support, quality; no object names, no simulator truth). `grasp_hints` maps ids to the
  precomputed grasp geometry of that kind (`pad_z_m` = the object's centre z, `fingertip_support_clearance_m`,
  `opening_check` against the 88.8 mm opening, `elongated`; for horizontally elongated objects `across_yaw_deg` =
  `yaw_deg +/- 90`, for lying ones `stand_upright_angles_deg`); `held` comes from `features.held_perception`. On
  `robot_observe` one overlay of the head frame with the ids drawn, encoded like the camera frames. Wrist cameras:
  nothing.
- `scene3d_observe`: `{"observation_id", "scene3d": {"artifact", "points", "table_z", "use"}}`; `use` is one line
  (`import scene3d_helpers as s3; s3.load(artifact); print(s3.API)`). The bounding boxes and the helper API list of
  the on-demand kind are not repeated; `scene3d_helpers.API` holds the list. No table, no image.
- both: one text part with both fields, then the overlay (observe only).
- service down / error / timeout: the field is the string `"unavailable: <reason>"` (`service_down`, `timeout`,
  or the service error code); the observation and the action receipt are returned unchanged. In action replies any
  other exception inside the attachment is swallowed (the action already executed).

The ObservationBundle (typed `data`) and the action result are not changed. Each attachment writes the event
`geometry.obs3d_attached` (`reply`: `observe` / `action`, latencies, row count, artifact, service timing, `reused`);
an overlay is stored like other geometry overlays (`geometry.overlay_delivered`, `source_kind`
`obs3d_object_table_overlay`).

## Context and cache behaviour

The parts live inside the tool result. With `lean_projection` the block projection treats them like any other part of
that result: inside the open block and at anchors they are sent unchanged; in closed blocks the text part becomes a
fixed summary and the overlay an image placeholder. `lean_projection.summarize_text` recognises the obs3d part
(keys `observation_id` + `object_table` / `scene3d` only) and writes, for any tool,
`[summary <tool>] {"obs3d_of": "<observation_id>", "object_rows": N | "unavailable", "scene3d": "stored" | "unavailable"}`
(20 / 21 / 26 o200k tokens with the table only / the cloud only / both); without it a `robot_action` part would have been summarised as an
empty action summary. `lean_compact` applies as to other observation text. The developer instructions gain a summary
paragraph for the active 3D perception switches (`obs3d.developer_instructions`: the attached table fields and
`grasp_hints`, and how to request custom segmentation or wrist projections on demand); the only catalogue change is
one sentence appended to the `robot_observe` description, which names the action replies. With
`object_table_observe` on:

> Head-camera observations here and in robot_action / robot_wait replies also carry the frame's VGGT-estimated
> object table (ids o1..oN: centre, top/bottom z, footprint, tilt, support, quality); robot_observe adds an id overlay
> image.

The sentence adds 233 characters to the actor catalogue with the table, 198 with the cloud and 288 with both; with
both switches off the catalogue is unchanged.

## Cost (model-free e2e, 10 recorded insert_tubes episodes, 98 attachments)

Every `robot_observe` / `robot_action` / `robot_wait` reply of the recorded episodes that carried a final observation,
recomputed against the live service and presented as for the actor in the measurement setup (compact reply encodings,
including `lean_compact`); o200k tokens of the added text:

| variant | action replies (n=86): added tokens min / p50 / mean / p90 / max | images | observe replies (n=12): p50 / max, images | latency s p50 (max), action |
|---|---|---|---|---|
| table | 91 / 182 / 172 / 275 / 291 | 0 | 258 / 287, 12 overlays | 0.40 (0.56) |
| cloud | 62 / 62 / 62 / 62 / 62 | 0 | 62 / 62, 0 | 0.36 (0.48) |
| both | 144 / 235 / 225 / 328 / 344 | 0 | 311 / 340, 12 overlays | 0.58 (0.71) |

For comparison the recorded action replies with an observation were 5,847 tokens of text (p50) plus their images, so
the text part adds about 3% (table), 1% (cloud) and 4% (both) to such a reply. Table size grows about 45 tokens per row
(0 rows = 91 tokens, 4 rows = about 290; rows per action reply: 0 to 4, 23 of 98 with 0 rows).

Context growth. A part that stays in the context is re-read by every later request; with N model steps and t tokens
added per step, the cumulative extra input is t x sum_{i=1..N}(N - i) = t x N(N-1)/2, the final context grows by t x N.

| variant | 60 steps, every step attaches: final context / cumulative input | 60 steps at the measured rate (19.8% of tool calls carry an observation): final / cumulative | measured 10 episodes (32-73 calls, mean 49.5): cumulative mean |
|---|---|---|---|
| table | +10.3k / +305k | +2.0k / +60k | +46.5k (actions alone +33.9k) |
| cloud | +3.7k / +110k | +0.7k / +22k | +14.8k |
| both | +13.5k / +399k | +2.7k / +79k | +59.1k |

These are upper bounds for `lean_projection`: once a block closes, each older part costs 20-26 tokens instead of t,
so the cumulative figure shrinks roughly by the closed-block share; most of the re-read input is a prompt cache hit.
Service time adds 0.4-0.6 s to each action reply. Every action reply with `scene3d_observe` stores one
`scene3d_points` artifact (npz 0.26-0.42 MB, p50 0.28 MB; about 2.4 MB per episode at 8.6 action replies).

## Tests

`tests/test_obs3d.py` covers: switches off (reply and catalogue unchanged, also with only `obs3d_action_overlay`);
catalogue sentence only on robot_observe and naming robot_action / robot_wait; table + overlay size; row cap; artifact
stored; both; service down / error code / timeout; program callers and observations without cam_head get nothing;
helper API = tool API; request projection of a robot_observe reply (summary + placeholder); robot_action and
robot_wait replies carry the table and cloud text with no image; robot_observe after an action reply on the same
observation adds the overlay and reuses the result (one call per kind); `obs3d_action_overlay` adds the overlay to
action replies; action reply with switches off / service down / program caller / non-wait execute tool; ROI view gets
nothing; request projection of a robot_action reply (summary `obs3d_of`, full text kept for the open block). Related
suites: `tests/test_object_table.py`, `tests/test_scene3d.py`, `tests/test_vggt_height.py`.

## Service dependency

VGGT service on `features.vggt_endpoint` (default `http://127.0.0.1:8331`; object-table-v5-hp1 with the SAM 3.1
sidecar on 127.0.0.1:8333, scene3d-points-v1; see `services/vggt_height/README.md`). Same request bodies as the
on-demand kinds (saved RGB, calibrated K / T, support height, robot link segments from public FK); `/height` is
untouched. With both switches on the client sends /object_table first and /points a few milliseconds later from a
second thread; the service serves requests FIFO on one GPU worker, so the second one hits the reconstruction cache.

## Truth scoring of attached tables

`scripts/score_geometry_truth.py` scores the attached tables as kind `object_table_obs` (plane source
`object_table_obs`): one table per observation (a reused table on a later reply of the same observation counts as a
delivery, not a second sample), each row matched to the nearest truth object exactly like the on-demand kind
`object_table` (centre xy, centre z, top z, table top - truth top). Source: the internal event
`analysis.obs3d_object_table`, written by `auriga/perception/external/obs3d.py` only with `analysis.scene_truth_log` on (observation id,
head frame id, reply kind, reused flag, the attached columns / rows / table_z, or the unavailable reason); it holds the
model-visible estimate only, never enters a reply, and the reply bytes are identical with the switch on or off
(`tests/test_obs3d.py`). Runs recorded before the event existed are scored from the JSON text part of the
robot_observe / robot_action / robot_wait replies (`wire_response`, joined by observation id). The groups `all` and
`role=` keep their meaning (geometry_query calls only); the markdown adds an attached-tables summary and the missing
`table top - top z` header cell.

The on-demand v5 tables (`label` column, `labels` field) are parsed by column name. Tests: `tests/test_scene_truth.py`
(v5 on-demand table with labels, v1 columns, attachments from events and from reply text), `tests/test_obs3d.py`
(event only with the switch, reply unchanged, unavailable table recorded).
