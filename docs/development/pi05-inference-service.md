# Integrating the pi0.5 Inference Service with Auriga

Auriga supports two independent integration paths for pi0.5 policy inference. In both designs, policy inference runs in its own isolated Python environment outside the Harness: the Harness runs in the `Auriga` environment, the robot simulator worker runs in the `RoboDojo` environment, and policy inference runs in a dedicated policy environment so that each component manages its own dependencies cleanly. See [RoboDojo Runtime Setup](robodojo-setup.md) for environment preparation instructions.

## ARX X5 Local Policy (`robot_policy_step`)

When `features.pi05=true` is enabled (default `false`), the `robot_policy_step` tool runs the official RoboDojo ARX X5 checkpoint (training config `pi05_base_aloha_full_sim_arx-x5_seed_0`; see [`pi05_arx_manifest.json`](../../auriga/runtime/policy/policy_workers/pi05_arx_manifest.json) for provenance and per-file SHA-256 digests). The integration is implemented in [`action_policy.py`](../../auriga/runtime/policy/action_policy.py):

- The policy worker reads only archived formal actor observations: synchronized raw RGB frames from all three cameras (`cam_head`, `cam_left_wrist`, and `cam_right_wrist`), measured 6-DoF joint angles for both arms plus the most recent normalized gripper commands (14 dimensions total), and the episode's public task instruction. The observation, camera frames, and robot state must share the same simulation step and clock domain.
- Inference must return a post-processed `50 × 14` array of finite joint actions. The Harness executes only the caller-requested prefix (`prefix` between 1 and 50 steps, defaulting to 5 steps = 0.20 s of simulation time at 25 Hz, with each row consuming one native step), routing physical execution through the single `Harness.execute` entrypoint.
- When `policy.server_url` is unset, the Harness launches [`pi05_arx_inference.py`](../../auriga/runtime/policy/policy_workers/pi05_arx_inference.py) inside the OpenPI Python interpreter specified by `policy.python` (`AURIGA_PI05_PYTHON`) and loads the checkpoint at `policy.checkpoint` (`AURIGA_PI05_CHECKPOINT`). When `policy.server_url` (`ws://` or `wss://`) is configured, the Harness instead uses [`pi05_arx_client.py`](../../auriga/runtime/policy/policy_workers/pi05_arx_client.py) to connect to an already running OpenPI WebSocket server; that lightweight client does not load model weights, verify checkpoints, execute robot actions, or retry failed inference calls.

This OpenPI ARX X5 path does not use the 32-dimensional gRPC output of the generic LeRobot base service described below.

## Generic LeRobot pi0.5 Base Service

An alternative setup runs LeRobot's native gRPC async-inference server as a standalone local process that can be called by Auriga or other local Python clients. The inference environment (for example, a mamba environment named `lerobot-inference`) installs official PyPI `lerobot==0.6.1` with the `pi` and `async` extras, using the Hugging Face `lerobot/pi05_base` weights and the PaliGemma tokenizer (`paligemma-3b-pt-224`). Deployments should pin the checkpoint revision, verify SHA-256 checksums, and keep service caches and logs outside Git.

### Auriga Integration Boundaries

- Calling this gRPC service requires `grpcio`, LeRobot protobuf definitions, and LeRobot Python dataclasses. The client should run inside the dedicated inference environment rather than installing LeRobot and PyTorch into the `Auriga` environment. To connect with the Harness, a dedicated bridge process in the inference environment can query the gRPC server and exchange defined observation and action payloads with the Harness (this generic 32-DoF base gRPC bridge is not yet wired into the runtime; the active ARX X5 tool uses the standalone OpenPI path above).
- Sandboxed actor programs are prohibited from opening network connections. Any gRPC calls must reside in trusted Harness adapter code or a dedicated worker process that respects existing isolation and control boundaries.
- Because `lerobot/pi05_base` outputs 32-dimensional policy actions, its output cannot be dispatched directly as an Auriga `EEAction`. Any adapter must first verify joint ordering, gripper conventions, action units, absolute versus delta semantics, normalization statistics, action-chunk execution horizon, and control frequency, and must route all physical execution through `Harness.execute`.
- Camera images and proprioceptive states must originate from a single permitted formal observation preserving observation, frame, and environment identity, and the task string must come from the episode's public instruction without injecting hidden evaluator scores or scene ground truth.

System-wide architectural boundaries are defined in the [Shared Interface and Event Protocol](../design/shared-protocol.md).

### gRPC Wire Protocol

The repository does not bundle a sample client for the generic LeRobot gRPC server. A client using `grpc` and `lerobot.transport.services_pb2_grpc.AsyncInferenceStub` follows the native four-step protocol:

1. `Ready(Empty)`: initializes a client session and clears internal queues.
2. `SendPolicyInstructions(PolicySetup)`: sends a pickled `lerobot.async_inference.helpers.RemotePolicyConfig` specifying the checkpoint path, target device, feature schema, and action chunk size; this step loads the model weights into GPU memory.
3. `SendObservations(stream Observation)`: streams pickled `TimedObservation` payloads chunked via `lerobot.transport.utils.send_bytes_in_chunks`.
4. `GetActions(Empty)`: decodes `response.data` into a `list[TimedAction]`.

The input contract for `lerobot/pi05_base` is:

| Policy Feature | Raw Observation Key | Format |
| --- | --- | --- |
| `observation.images.base_0_rgb` | `base_0_rgb` | `224 × 224 × 3`, HWC `uint8` RGB |
| `observation.images.left_wrist_0_rgb` | `left_wrist_0_rgb` | `224 × 224 × 3`, HWC `uint8` RGB |
| `observation.images.right_wrist_0_rgb` | `right_wrist_0_rgb` | `224 × 224 × 3`, HWC `uint8` RGB |
| `observation.state` | 32 scalar fields named in the feature schema's `names` list | 32-dimensional `float32` |
| Task instruction | `task` | Public task instruction string for the current episode |

State scalars may be named `state_0` through `state_31`. The native LeRobot server assembles the state vector from the raw observation dictionary according to the schema's `names` list rather than accepting pre-batched training tensors. Camera keys in the raw observation must match the policy feature names directly; do not rely on `rename_map` to remap arbitrary camera names, because the server looks up policy image configurations before running the rename processor.

Each inference call returns a chunk of 50 32-dimensional actions (using 10 denoising steps in `float32`). Because the base checkpoint's normalizer and unnormalizer ship with empty `features={}`, they do not contain state or action normalization statistics for the RoboDojo ARX X5 robot; the 32-dimensional shape reflects the pre-training interface rather than a calibrated mapping to the ARX X5 state and action spaces.

Each server instance supports a single active client session (the upstream implementation does not isolate state across concurrent clients). Because every `SendPolicyInstructions` call reloads the model weights, a client should run setup once per configuration and then loop over `SendObservations` and `GetActions` (re-running setup only if the server process restarts). Because the RPC payloads use Python `pickle`, the server must bind strictly to a local loopback address (such as `127.0.0.1:50051`) and exposes no HTTP/REST interface. If an upstream inference exception occurs, `GetActions` may return an empty message; clients must verify that `response.data` is non-empty and that all returned action values have the expected dimensions and finite numbers. The first observation of a new episode should explicitly set `must_go=True` so that it is not dropped by the server's duplicate-state filter against the end of the previous episode.

Smoke tests using zeroed states, black images, and synthetic prompts verify only weight loading and RPC execution; they do not measure RoboDojo task performance, and single-call latency does not represent sustained throughput. Any policy performance comparison must align the RoboDojo tasks, robot interfaces, initial layouts, step budgets, and official evaluation protocol.
