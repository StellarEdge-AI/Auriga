# Astra Embodied Harness: Shared Interfaces and Event Protocol

> **2026-10-07 Current Interface Note:** New runs default to `rgb_proprio_calibrated_v1`, while retaining explicit support for the legacy RGB/proprioception-only profile and allowing explicit selection of RGB-D conditions; these three profiles must never be conflated as a single evaluation condition. Earlier statements restricting the formal actor exclusively to uncalibrated RGB/proprioception apply only to historical runs from that period. For current observation boundaries, refer to [Actor Information Boundary](../development/actor-information-boundary.md), and for current tool interfaces, refer to [Unified Actions and Program Approval](../development/unified-actions.md). This document preserves the initial v0.1 architectural design specification and does not override current public code schemas.

> This document preserves the initial design conventions established on 2026-09-30 and outlines long-term extension goals. References to "unimplemented" features reflect the state at that time. For the current supported scope, refer to the code registries, capability declarations, tool contracts, and test suites.

Version: v0.1, September 30, 2026. Status: Proposed interface specification enabling parallel development across the primary Harness, local scene service, and web visualization workbench. The domain interfaces and event schemas defined below are specific to this project and are not native APIs of Codex, RoboDojo, or Isaac Sim.

**All three modules share unified observation bundles, action schemas, and correlation IDs; however, only the primary Harness execution gateway holds write permissions to control the evaluated world.** The local scene service outputs geometric measurements and rollout predictions, while the web workbench renders live event streams and submits user control requests through the gateway—neither can write directly to RoboDojo's joint controllers.

Companion specifications: [Embodied Harness Design Plan](./embodied-harness-design.md), [Local Scene and Short-Horizon Simulation Plan](./local-scene-and-rollout.md), and [Web Workbench and Codex Integration Plan](./web-workbench-and-codex.md).

## 1 Ownership and Transport Boundaries

| Data or Behavior | Single Authoritative Writer | Consumers |
|---|---|---|
| Evaluated World Observations & Measured Motion Feedback | RoboDojo Adapter | Primary Agent, Permitted Geometry & Scene Services, Web Workbench |
| Evaluated World Physical Actions | Primary Harness Execution Gateway | Robot Motion Backend |
| Native Codex Runtime Events | Codex `app-server` (archived verbatim by Gateway) | Web Workbench, Researchers |
| Event Ingestion Sequence & Correlation Index | Primary Harness Event Store | Web Workbench, Replay Client, Offline Analysis Tools |
| Reconstructed Scene Versions & Snapshots | Local Scene Service | Astra, Internal Simulation Worker, Web Workbench |
| Internal Simulation Rollout Results | Isolated Simulation Worker | Local Scene Service, Astra, Web Workbench |
| Authoritative Task Scores & Ground-Truth Diagnostics | Isolated Evaluator | Researcher Evaluation Views (strictly excluded from actor tools) |

Domain requests communicate over versioned HTTP JSON endpoints; long-running operations return asynchronous job handles, and real-time events stream to the web workbench over Server-Sent Events (SSE). Binary media—including raw images, videos, meshes, and dense trajectory arrays—are exchanged via content-addressed file or object store references (`ArtifactRef`) rather than inlined into event JSON payloads. A single run can be backed entirely by a local SQLite database in WAL mode paired with an artifact directory, requiring no external message broker.

Access permissions are configured separately for actor-facing tool services, observer-only web views, and privileged evaluator logs. Global diagnostic views inspected by human researchers never leak into the model's context. Whether camera calibration is exposed to the actor is declared immutably in the run's observation profile manifest rather than injected ad hoc by individual tools.

## 2 Identifiers and Time Domains

| Identifier Field | Semantic Definition |
|---|---|
| `run_id` | Identifies an entire experimental run or interactive session, encompassing its configuration manifest and all child records. |
| `episode_id` | Identifies a single task attempt following an environment initialization or reset. |
| `env_instance_id` | Identifies the specific environment process or container hosting the episode; parallel environments carry distinct IDs. |
| `env_generation` | Monotonically increments whenever an environment instance resets or restores state, preventing stale in-flight requests from executing against a newly reset scene. |
| `observation_id` | Identifies an immutable `ObservationBundle`. |
| `action_id` | Identifies an immutable candidate or requested `EEAction` across planning, internal previewing, and physical execution; whether it was actually submitted to the `observed` world is established by explicit execution receipts and lifecycle events. |
| `scene_version` | Identifies an immutable revision of the reconstructed local scene within a specific environment namespace (never a mutable `latest` pointer). |
| `snapshot_id` | Identifies a frozen combination of a `scene_version`, anchor robot state, and physical parameter priors used to initialize internal simulation rollouts. |
| `branch_id` | Identifies an isolated internal simulation branch with its own independent internal simulation timeline. |
| `job_id` | Identifies an asynchronous background job (scene reconstruction, trajectory preview, or physics rollout). |
| `thread_id / turn_id / item_id` | Native Codex session identifiers mapped from upstream camelCase fields at the adapter boundary. |
| `tool_call_id` | Runtime tool invocation correlation ID; generated by the bridge layer with explicit provenance if absent from the upstream protocol. |

The `world_kind` field takes strictly one of two values: `observed` or `internal`. `observed` denotes the evaluated benchmark environment in the current experiment (i.e., RoboDojo, without implying physical hardware), whereas `internal` denotes the approximate scene reconstructed from observations. Synthetic test fixtures additionally carry `data_origin=synthetic_fixture` so they are never mistaken for live benchmark runs.

A single candidate `EEAction` may be evaluated across multiple internal simulation branches, with each trial uniquely identified by the tuple `(branch_id, job_id, action_id)`. Previewing an action in an `internal` branch never emits an `observed`-world `action.requested` event. When the primary agent decides to commit an action to the evaluated world, it must submit the request through the primary execution gateway against the latest robot state; if the agent modifies the conditioning observation or target coordinates, a fresh `action_id` must be minted (recording the prior candidate ID as its provenance) rather than mutating the payload of an existing `action_id`.

The protocol tracks four distinct categories of timestamps: event occurrence wall-clock time (`occurred_at`), gateway ingestion time (`received_at`), environment simulation step and time (`sim_step` / `sim_time_s`), and media presentation timestamps (`PTS`). Every timestamp is paired with an explicit `clock_domain`; wall-clock timestamps from unsynchronized hosts must never be subtracted to estimate motion latency. Similarly, an internal branch's `sim_time_s` measures elapsed time from its snapshot initialization and must never be conflated with the evaluated world's `sim_time_s`.

Cross-stream alignment relies on explicit correlation IDs, `env_generation`, action step intervals, and frame-to-step mapping tables. Wall-clock proximity may be used only for approximate log browsing—never for assigning causal action ownership. A single agent turn may issue multiple robot actions, and a single robot action may span multiple Codex items across planning, execution, and post-action inspection.

Every `sim_step` value must be accompanied by a `step_domain` field declaring whether it counts adapter control steps or underlying physics substeps, alongside the corresponding timestep $\Delta t$ or time mapping; policy turns, adapter control steps, rendered camera frames, and PhysX substeps are never interchangeable. RoboDojo's `take_action_cnt` is recorded strictly according to its verified environment counter semantics without claiming it represents raw physics substeps. Uncollected counters or timestamps remain `null` and are never back-calculated from video frame rates. The example below illustrates `step_domain="adapter_control_step"`.

## 3 EventEnvelope

Events form an append-only log: once emitted, historical events are never mutated in place. State updates or post-outage reconciliations are expressed by appending new status or reconciliation events. Apart from streaming text deltas, consumers must be able to replay events idempotently without triggering duplicate side effects.

```json
{
  "schema_version": "0.1",
  "event_id": "evt-0083",
  "run_id": "run-demo",
  "episode_id": "ep-001",
  "env_instance_id": "env-01",
  "env_generation": 3,
  "world_kind": "observed",
  "source": "execution_gateway",
  "source_instance_id": "gateway-boot-02",
  "source_seq": 19,
  "server_seq": 83,
  "type": "action.started",
  "occurred_at": "2026-09-30T08:00:00.000Z",
  "received_at": "2026-09-30T08:00:00.015Z",
  "clock_domain": "env-01/generation-3",
  "sim_step": 420,
  "step_domain": "adapter_control_step",
  "sim_time_s": 16.8,
  "correlation": {
    "thread_id": "thr-demo",
    "turn_id": "turn-04",
    "item_id": "item-12",
    "tool_call_id": "tool-07",
    "observation_id": "obs-031",
    "action_id": "action-009"
  },
  "payload": {
    "request_ref": "artifact:action-009-request",
    "planned_trace_ref": "artifact:action-009-plan"
  }
}
```

`server_seq` is a monotonically increasing ingestion sequence number assigned by the primary event store within a single `run_id`; it reflects arrival order at the store rather than physical occurrence order across distributed processes. `source_seq` is strictly ordered only within the lifetime of a single `source_instance_id`; if a producer restarts, it must mint a fresh `source_instance_id` rather than reusing its prior sequence space.

When an event is not associated with a specific action, observation, or Codex item, the corresponding `correlation` keys are omitted rather than populated with placeholder values. When `occurred_at` or `sim_step` is unknown, it is set to `null` accompanied by an explicit reason—never zero-filled. Large event payloads are stored externally via `payload_ref` according to the schema of the specific event `type`.

Core domain event types include:
- **Observations:** `observation.available`
- **Actions:** `action.requested`, `action.accepted`, `action.rejected`, `action.started`, `action.progress`, `action.completed`, `action.failed`, `action.stop_requested`, `action.stopped`, `action.status_unknown`
- **Scene Management:** `scene.updated`, `scene.invalidated`
- **Asynchronous Jobs:** `job.started`, `job.progress`, `job.completed`, `job.failed`, `job.cancelled`, `job.expired`

Granular lifecycle events such as `job.queued` may be added without altering terminal state semantics. Notably, `action.status_unknown` represents a pending reconciliation state rather than a confirmed terminal state; once communication is restored, a subsequent event records the reconciled terminal state and its supporting evidence.

Native Codex JSON-RPC notifications and server requests are archived verbatim in a dedicated raw stream. Projected domain events add indexing and correlation metadata without stripping native `method` names, `params`, JSON-RPC request IDs, or unrecognized upstream fields. An upstream Codex `item/completed` notification never automatically implies a domain-level `action.completed` event.

## 4 ObservationBundle

The `ObservationBundle` is the immutable unit of input consumed both online by Astra and offline by the local scene service. Its manifest, indices, and media artifacts are write-once; any subsequent recalibration or post-processing produces a new `observation_id` or `calibration_id`.

```json
{
  "schema_version": "0.1",
  "run_id": "run-demo",
  "episode_id": "ep-001",
  "env_instance_id": "env-01",
  "env_generation": 3,
  "world_kind": "observed",
  "observation_id": "obs-031",
  "sim_step": 419,
  "step_domain": "adapter_control_step",
  "sim_time_s": 16.76,
  "clock_domain": "env-01/generation-3",
  "observation_profile": "rgb_proprio_calibrated_v1",
  "robot_model_id": "arx-x5-pinned",
  "calibration_id": "cal-002",
  "cameras": [
    {
      "camera_id": "left_wrist",
      "frame_id": "left-wrist-419",
      "rgb_ref": "artifact:rgb-left-wrist-419",
      "width": 640,
      "height": 480,
      "pixel_transform": "identity",
      "calibration_ref": "artifact:cal-left-wrist-002",
      "pose_sample_ref": "artifact:camera-pose-419",
      "capture_time_ref": "artifact:capture-left-wrist-419"
    }
  ],
  "robot_state_ref": "artifact:robot-state-419",
  "last_action_id": "action-008"
}
```

The JSON above illustrates the structural schema without claiming that RoboDojo natively outputs this exact resolution or field layout out of the box. A single bundle may contain multiple camera feeds, recording per-camera sampling associations and synchronization status so asynchronously captured frames are never misrepresented as hardware-synchronized stereo pairs.

What the model actually "saw" is audited via the exact image artifact reference/digest and pixel preprocessing transform dispatched in the model request. Lossy JPEG previews generated for the web UI are stored as separate derived artifacts referencing the raw source frame, never claimed to be byte-identical to model inputs. Intermediate control frames captured during motion carry `delivered_to_actor=false` so logged background frames are clearly distinguished from observations injected into the agent's context.

The `robot_state_ref` artifact records joint names, measured positions `q`, available joint velocities $\dot{q}$, measured or FK-derived EE poses, and gripper commands alongside available feedback. Every state field is tagged with `source_kind` (`measurement`, `fk_from_measurement`, `command`, or `estimate`). Because certain gripper state fields in RoboDojo echo clamped prior commands rather than physical finger encoders, they must be tagged as `command` rather than misrepresented as tactile or position measurements.

Calibration artifacts specify the intrinsic matrix `K`, distortion model and coefficients, native image dimensions, optical frame axis convention, extrinsic transform direction, and provenance. Throughout this protocol, $T_{A\_B}$ denotes the rigid transformation mapping points from frame $B$ into frame $A$:
$$p_A = T_{A\_B} \, p_B$$
Internal units are meters and radians, with rotations expressed as normalized `xyzw` quaternions. Image cropping or resizing operations record their exact affine `pixel_transform`.

Fields prohibited by the active `observation_profile`—such as camera calibration in uncalibrated tracks, depth maps in RGB-only tracks, or privileged object poses—must be physically omitted from the bundle rather than included with a prompt instruction asking the model to "ignore them." Schema validators reject bundles whose fields violate the declared `observation_profile`, while synthetic and oracle tests use distinct profile manifests.

## 5 EEAction and Execution Receipts

While model-facing tools may accept ergonomic orientation angles or relative deltas, the tool service converts every request into the canonical `EEAction` schema below before archiving and dispatching it, preserving both the raw model arguments and the canonical representation for coordinate-transform auditing.

```json
{
  "schema_version": "0.1",
  "action_id": "action-009",
  "run_id": "run-demo",
  "episode_id": "ep-001",
  "env_instance_id": "env-01",
  "env_generation": 3,
  "based_on_observation_id": "obs-031",
  "frame_id": "robot_base",
  "tcp_config_id": "arx-grasp-center-v1",
  "mode": "absolute",
  "path": {
    "kind": "linear",
    "waypoints": [
      {
        "left": {
          "position_m": [0.35, 0.12, 0.22],
          "quaternion_xyzw": [0, 1, 0, 0]
        }
      }
    ],
    "orientation": "slerp",
    "blend_radius_m": 0.0,
    "position_tolerance_m": 0.005
  },
  "gripper_events": [],
  "limits_profile": "simulation-slow-v1",
  "timeout_s": 10.0
}
```

Numerical coordinates, tolerances, and timeouts above are illustrative. Every physical motion must start from the robot's measured feedback state at execution time rather than an invented starting pose in the request payload. Relative delta actions are resolved against the explicitly referenced frame and observation at ingestion time, archiving both pre- and post-conversion parameters.

Supported `path.kind` values include `backend_free`, `linear`, `polyline`, `arc`, and `spline`. The adapter's capability manifest declares which path kinds are implemented; unsupported requests fail explicitly with `unsupported_capability` rather than silently falling back to unconstrained free-space planning. Similarly, `gripper_events` may specify stage-wise or path-parameterized triggers only when supported by the underlying adapter, accurately reflecting the baseline's sequential "arm-then-gripper" execution semantics until continuous synchronization is implemented.

Protocol v0.1 focuses on bounded Cartesian EE trajectories. Invoking an external action model's native joint chunks or executing a synthesized closed-loop control script uses a versioned, dedicated action type rather than stuffing raw joint arrays into `EEAction.waypoints`. When the internal simulator evaluates a synthesized program, it executes an immutable program bundle against the same controlled EE client interface without granting credentials for the evaluated world.

Execution receipts archive four distinct trajectory records—requested waypoints, planned trajectory, dispatched commands, and measured feedback—alongside their respective timelines, partial execution intervals, terminal status, and resulting observation reference (explicitly marking missing sensor streams as `unavailable`). Completing a motion trajectory never implies a successful grasp or completed task.

Every terminal action receipt must provide either a post-action `result_observation_id` or an explicit capture failure diagnostic—including the final action of an episode, which cannot rely on a subsequent agent turn to trigger observation capture. Step-by-step feedback is archived at its actual sampling rate; when only boundary states are recorded, two-point linear interpolation must never be labeled as a dense measured trajectory.

Action submission idempotency is keyed on `(run_id, episode_id, env_generation, action_id)`. Submitting the same key with an identical payload digest returns the existing execution state, whereas submitting the same key with a modified payload returns an HTTP 409 conflict, ensuring network retries never trigger duplicate physical motions. If a backend crash leaves execution status ambiguous, the gateway emits `action.status_unknown` and reconciles state before permitting new motions rather than automatically resending the command.

## 6 SceneSnapshot and Internal Rollout References

The local scene service owns the mutable `scene_head` pointer, while all downstream consumers reference immutable `scene_version` manifests and frozen `SceneSnapshot` records containing at minimum:

| Snapshot Component | Required Semantic Guarantees |
|---|---|
| Scene Identity | `run_id`, `episode_id`, `env_instance_id`, `env_generation`, `scene_version`, and `snapshot_id`. |
| Evidence Boundary | Conditioning `observation_id` list, `calibration_id`, `robot_model_id`, and active `observation_profile`. |
| Scene Assets | Reconstructed geometric primitives, collision proxies, object poses, explicitly tracked unknown/occluded regions, and physical parameter ranges with provenance. |
| Initial State | Measured joint positions `q` from permitted observations, $\dot{q}$ availability, gripper state semantics, and explicit object velocity assumptions. |
| Execution Configuration | Adapter version, `ControllerProfile`, IK/planner settings, physics engine version, and solver parameters. |
| Content Digests | Cryptographic SHA-256 digests of the snapshot manifest and all referenced binary blobs, prohibiting mutable `latest` URLs. |
| Quality & Limitations | Geometric degeneracy flags, unmodeled occlusions, initialization settling drift, and contact state assumptions. |

A `SceneSnapshot` represents an observation-derived initial condition rather than a ground-truth clone of the evaluated simulator's internal state. Because hidden contact forces, solver warm-start caches, and instantaneous object velocities cannot be uniquely recovered from sparse RGB frames, their omission and the chosen initialization strategy (e.g., `cold_quasistatic` settling) must be explicitly recorded in the manifest.

A `RolloutRequest` specifies at minimum the target `snapshot_id`, candidate `EEAction` (or immutable program bundle reference), parameter hypothesis set, RNG seed manifest, simulation horizon and wall-clock budgets, and requested output media. Rollout execution operates exclusively on an isolated `internal` branch and never accepts an option to "auto-commit to the observed world upon success."

A `RolloutResult` records the `branch_id`, `job_id`, input snapshot and action digests, completed step interval, internal robot and object trajectories, rendered media references, queried metric values, and any numerical or initialization errors. Passing rates across a parameter ensemble describe only the tested parameter set and must never be reported as calibrated real-world success probabilities. If the primary agent decides to execute a previewed candidate in the evaluated world, it must submit the `EEAction` through the primary execution gateway, which independently re-verifies the current `observation_id` and `env_generation`.

## 7 ArtifactRef and Media Synchronization

Every `ArtifactRef` entry records at minimum an `artifact_id`, `kind`, `mime_type`, `byte_size`, content SHA-256 digest, logical storage key, and access visibility scope. Concrete download URLs are resolved dynamically by the gateway according to caller permissions; `artifact:...` strings in protocol JSON represent logical identifiers rather than direct file paths.

Image metadata binds each frame to its `camera_id`, `frame_id`, `observation_id`, `env_generation`, `sim_step` (with `step_domain`), and capture clock timestamp. Encoded video files are accompanied by a frame-index-to-`PTS`-to-`sim_step` mapping table; timeline seeking uses this explicit mapping table rather than assuming video frame 0 corresponds to simulation step 0, preserving exact alignment across variable frame rates, dropped frames, or paused recording intervals.

When a user selects an action node in the web workbench, the UI highlights the exact conditioning `observation_id` fed to the model alongside media frames captured during the action's executed step interval. If only an adjacent frame is available, the UI displays the exact step or time offset; if media is missing, it explicitly displays "media unavailable" rather than silently substituting a stale frame from a previous action.

Artifacts from internal simulation branches, the evaluated world, and privileged evaluator diagnostics carry distinct visibility tags (`internal`, `observed`, `evaluator_privileged`). Exported Rerun recordings (`.rrd`), MP4 videos, and log archives inherit these access tags so exporting media for human visualization never creates a backdoor that leaks privileged data to the actor.

## 8 Web Event Recovery and Runtime Control Semantics

The gateway exposes `GET /v1/runs/{run_id}/events?after={server_seq}` for both live SSE streaming and persistent historical replay, using `server_seq` as the SSE `id` field. Reconnecting clients resume from their last seen `server_seq` cursor and deduplicate incoming events by `event_id`. If a client's cursor falls behind the retention window, the server returns an explicit gap notification directing the client to fetch the latest run snapshot rather than retrying an expired cursor indefinitely.

Upstream disconnections between the gateway and Codex `app-server` are tracked separately from downstream SSE disconnections between the browser and the gateway. While browser SSE drops are recovered losslessly from the gateway's local SQLite event store, an upstream `app-server` disconnect requires querying Codex's persisted `thread/item` state to reconcile missed events and inserting an explicit gap marker—never fabricating synthetic token deltas to pretend streaming history was uninterrupted.

User interface controls enforce strict semantic separation across four distinct operations:
1. **Pause UI Playback:** Freezes the local browser timeline cursor without affecting the agent or robot.
2. **Interrupt Agent Turn:** Requests `turn/interrupt` on the active Codex turn while continuing to monitor any in-flight physical robot motion independently.
3. **Stop Run:** Revokes new action submission permissions at the gateway, requests both agent turn interruption and backend robot motion termination, and waits for explicit backend stop confirmation before reporting `stopped` (displaying `status_unknown` if unconfirmed).
4. **Pause Simulation Clock:** Pauses the underlying physics stepping loop, exposed only when supported by the environment adapter.

Closing a browser tab, losing network connectivity, or canceling an LLM inference request never automatically proves that physical robot motion has ceased.

Interactive tool approvals and user input prompts are routed by their native upstream request IDs. Duplicate button clicks are idempotent, and expired prompts cannot be re-triggered as new requests. Write ownership for a run is enforced server-side via coordinator leases rather than relying on client-side disabled UI buttons to prevent multi-tab race conditions.

The persistent native Codex terminal streams raw PTY bytes over a dedicated WebSocket channel without replacing structured `EventEnvelope` logging. Each run manifest binds a single `app-server` instance, `thread_id`, and `terminal_session_id`; recreating a terminal wrapper never spawns a duplicate Codex thread. Browser page refreshes simply reattach to the active PTY session and restore its scrollback buffer. Read-only observer mode is enforced at the gateway by dropping inbound keystroke frames rather than relying on CLI flags for security. Because the automated run coordinator and an interactive human TUI cannot simultaneously issue turn starts, approvals, or cancellations, manual takeover requires an explicit server-side ownership handoff. Selecting historical action nodes in the web timeline updates only the side inspector and media replay pane—never overwriting the live native terminal view.

## 9 Synthetic Fixtures and Compatibility Acceptance for Parallel Development

To decouple module development from live LLM calls and GPU simulation, the project provides an offline synthetic fixture bundle containing a run manifest, multiple `ObservationBundle` records, camera calibration files, robot states, two `EEAction` payloads, a `SceneSnapshot`, raw Codex JSON-RPC event logs, projected domain events, sample images, and trajectories—all tagged with `data_origin=synthetic_fixture` and cryptographic digests.

- The **local scene team** develops and validates scene reconstruction and the Isaac rollout adapter using only the observation and action fixtures.
- The **web workbench team** builds node projection, synchronized media seeking, and SSE reconnection recovery using only the event and media fixtures.
- The **primary Harness team** verifies end-to-end correlation chains against mock tool backends before swapping in live RoboDojo and Codex processes without altering public schema definitions.

Integration acceptance suites must explicitly test:
1. Rejection of stale `EEAction` requests carrying an outdated `env_generation` after an environment reset;
2. Idempotent handling of duplicate `action_id` submissions without double-executing robot motion;
3. Honest `unavailable` reporting when sensor or provenance fields are missing;
4. Explicit offset reporting when camera frames lag behind control steps;
5. Explicit `unsupported_capability` rejection for unimplemented path geometries;
6. Immutability of in-flight `SceneSnapshot` records when `scene_head` advances;
7. Guarantee that internal branch rollouts never trigger `observed`-world motion events;
8. Asynchronous decoupling when a Codex turn finishes before physical robot motion completes;
9. Lossless SSE cursor resumption and client-side event deduplication; and
10. Explicit gap marking when upstream Codex delta events are dropped.

Backwards-compatible schema additions use optional fields, whereas removing fields or altering physical units, coordinate conventions, or state machine semantics requires incrementing `schema_version`. All modules lock a single coordinated protocol version rather than chasing unversioned `latest` branches.
