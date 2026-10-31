# Wrist Camera Viewpoint Solver (`robotic_view`)

`robotic_view` converts a caller-specified camera optical center, look-at target point, and image-up direction into the corresponding robotic arm `link6` target pose, eliminating the need to manually chain camera mount transforms. It is a purely read-only geometric solver: it does not detect occlusion, pick an idle arm or viewpoint automatically, or invoke motion preview, physical execution, or camera capture.

## Enabling the Tool

Set `features.robotic_view=true` in configuration or pass `--robotic-view` to the Gateway CLI (`--no-robotic-view` explicitly disables it). It defaults to `false` and is independent of `stereo_guide`. Benchmark queue manifest cases accept a boolean `robotic_view` field and inherit from the unified configuration when omitted. Session resume and replay honor the archived flag (treating older archives without the field as disabled); see the [Configuration Guide](configuration.md) for full precedence rules.

When disabled, the plugin is neither imported nor wired, no `robotic_view` tool is registered for actor or program callers, and no tool instructions are added to the prompt. Even when enabled, the tool can still be suppressed via `features.disabled_tools` or `--disabled-tools robotic_view`. Usage instructions are delivered exclusively through the tool's own `ToolSpec` without adding task-specific prompts or modifying Task Notes.

## Inputs and Outputs

All five input parameters are required:

| Field | Description |
| --- | --- |
| `observation_id` | Formal observation ID captured while the robot is idle; historical observations or observations taken mid-action are rejected. |
| `camera_id` | Calibrated wrist camera from that observation connected to `link6` through a public fixed-joint chain; fixed head cameras are not supported. |
| `eye_position_m` | Desired `[x, y, z]` camera optical center in meters in the observation's calibrated reference frame (`env_origin` natively)—this is the camera optical center, not the `link6` origin. |
| `target_position_m` | `[x, y, z]` point in meters that the camera optical axis should point toward, in the same reference frame. This is a hypothesis or estimate supplied by the caller from permitted evidence, not an object position measured by the tool. |
| `up_vector` | 3D direction vector in the same reference frame that should align with the top of the image; only its direction is used (magnitude is ignored, and there is no default). |

All three 3D vectors must contain finite numbers. `eye_position_m` and `target_position_m` must be separated by at least `1e-6 m`, `up_vector` must be non-zero, and the sine of the angle between `up_vector` and the viewing direction must exceed `1e-6`. The camera optical frame follows the standard `+x` right, `+y` down, `+z` forward convention.

On success, the tool returns a typed response with `kind="robotic_view"` and `data.status="calculated"`. `data.robot_action_preview` provides a ready-to-call template with `tool="robot_action"` and `arguments` pre-populated for `mode="preview"` containing a single `move` stage with the target arm's `link6` `position_m` and `quaternion_xyzw`, preserving `frame_id` and `tcp_config_id` conventions.

In the returned `checks` block, `checks.ik` and `checks.collision` are marked `not_checked`, while `checks.visibility` and `checks.retention` are marked `unknown`. A status of `calculated` indicates only that the rigid-body coordinate transformation succeeded; it does not prove that the pose is kinematically reachable, that the path is collision-free, that the target will be visible unoccluded, or that a held object will remain stable.

## Calling the Tool and Chaining with `robot_action`

Inside `program_run`, callers can compute a wrist camera pose from the current formal observation and immediately request a motion preview without moving the robot:

```python
from auriga_client import AurigaClient

client = AurigaClient()

def preview_view(observation_id, camera_id, eye, target, up):
    result = client.call("robotic_view", {
        "observation_id": observation_id,
        "camera_id": camera_id,
        "eye_position_m": eye,
        "target_position_m": target,
        "up_vector": up,
    })
    assert result["kind"] == "robotic_view"
    assert result["data"]["status"] == "calculated"
    return client.call("robot_action", result["data"]["robot_action_preview"]["arguments"])
```

The primary actor model can also invoke `robotic_view` directly with the exact same arguments. After inspecting the previewed trajectory, step cost, and advisories, the caller can execute the motion by calling `robot_action(mode="execute", plan_id=...)` with the `plan_id` returned by the preview (or via `AurigaClient.call("robot_action", {"mode": "execute", "plan_id": ...})` inside a program, subject to standard action approval and step budgets; see [Unified Actions and Program Approval](unified-actions.md)). Execution outcomes must always be read from the typed `kind` and `data.receipt` fields rather than inferred from `stdout`.

Callers should prefer an idle wrist camera that offers a complementary viewpoint and verify its swept path. Moving an arm that is currently holding an object simultaneously changes the held object's pose—the tool never treats a wrist-mounted camera as a free-flying sensor. Moving to a new viewpoint consumes normal native step budget. After any subsequent action executes, callers must recompute viewpoint poses from the latest observation and inspect the newly captured image to verify whether the desired visual evidence was actually obtained.

## Implementation and Validation Boundaries

The plugin is implemented in [`auriga/runtime/tools/plugins/robotic_view.py`](../../auriga/runtime/tools/plugins/robotic_view.py). Because a wrist camera's calibrated parent `link_name` need not be `link6` directly, the solver chains fixed-joint transforms from the public robot model to compute the rigid transform between the camera optical frame and `link6`, then inverts that mount transform to map the desired camera pose to the `link6` target pose. The Gateway injects a narrow read callback via `make_robotic_view_tool(read)` without exposing raw Store paths or worker control to the plugin.

Both before and after computing the transform, the tool verifies formal observation identity, freshness, and current idle status, reading only permitted camera calibration, the public robot model, and proprioceptive state. Calling `robotic_view` never clears an `unknown` or `stopped` execution gate, and any subsequent physical motion still routes through `robot_action` and the single `Harness.execute` entrypoint.

Regression tests covering coordinate transforms, degenerate inputs, information boundaries, and actor/program contracts live in [`tests/test_robotic_view.py`](../../tests/test_robotic_view.py). Verified geometric math does not guarantee that a chosen viewpoint will be informative in practice or improve task success rates, which must be evaluated under frozen benchmark conditions with the official native evaluator.
