# Motion Cost and Contact-Reference Overlays

> **Status**: `auriga.motion_cost.preview_motion_cost` and the `robot_preview_cost` tool described in the first section were removed in the 2026-10 refactor (`trusted_legacy` only; unreachable by actors or programs) and are retained here as historical reference (`tests/test_motion_cost.py` was removed with them). The contact-reference overlay (`auriga/perception/overlays/contact_overlay.py` / `robot_contact_overlay`) remains active. For the current motion interface, see [Unified Actions](unified-actions.md).

These helpers provide two bounded forms of geometric assistance using the actor's own candidate targets and permitted robot observations: motion cost preview plans and compiles commands without moving the robot, while contact-reference overlay projects disclosed finger reference points onto an existing observed RGB image. Neither helper accesses hidden task-object ground truth or certifies manipulation success.

## Candidate Motion Cost (Historical)

Worker entry point:

```python
from auriga.motion_cost import preview_motion_cost

result = preview_motion_cost(
    adapter,
    [
        {"label": "direct", "action": resolved_single_segment_action},
        {"label": "alternate", "action": another_resolved_single_segment_action},
    ],
    observation_id=current_observation_id,
)
```

The caller supplied **1 to 3** uniquely labeled, actor-authored `EEAction` candidates after `Harness` validated the current observation/environment and resolved grasp-contact offsets and speed presets. Each candidate had to contain a single `backend_free` waypoint for one or both arms (with the same progression modes, per-arm speeds, and gripper events supported by execution); policy/queue actions, holds, cached plan IDs, unresolved speed presets, path constraints, and multi-waypoint lookahead were rejected.

While holding the worker operation lock, the helper snapshotted the measured state and preview binding, then ran `_plan_segment`, `_resample_action_plan`, and `_motion_segment` for each candidate from that identical starting state—never mutating robot joints to a guessed endpoint, dispatching native rows, rendering observations, querying task scores, caching an executable plan, or invoking `joint_replan_context`.

Each candidate result reported its label, action digest, feasibility/status, any error, and (when compilation succeeded):

- `nominal_native_steps`, corresponding physics ticks, and nominal duration;
- Per-arm motion, gripper, endpoint-hold, and final-hold step counts;
- Per-joint total absolute command travel, maximum joint travel, and sum across joints/arms starting from the measured joint state;
- Resolved arm/gripper speed scales and any compiled `during_motion` gripper schedule.

For a single segment with resampled arm motion row counts $m_{\text{left}}, m_{\text{right}}$ (including any `during_motion` subdivision) and post-motion gripper row counts $j_{\text{left}}, j_{\text{right}}$:

- **Single arm**: $m + j$
- **Paired dual-arm**: $\max(m_{\text{left}}, m_{\text{right}}) + \max(j_{\text{left}}, j_{\text{right}})$
- **Independent dual-arm**: $\max(m_{\text{left}} + j_{\text{left}}, m_{\text{right}} + j_{\text{right}})$

For example, 3 left motion rows + 6 left gripper rows paired with 9 right motion rows compiled to 15 steps in `paired` mode vs. 9 steps in `independent` mode, subject to the shared 10-second (250-step) per-request cap.

Compiled counts were **exact for the newly compiled nominal commands** rather than a prediction of physical execution, which could still halt early due to tracking error, changed jaw opening, cancellation, episode termination, or contact. Because cost preview did not cache an executable `plan_id` and native planning is stochastic, a subsequent execution call replanned from scratch. The response also recorded `state_binding` before and after, `same_measured_state_verified`, `zero_step_check_passed`, and native/physics counter deltas, aborting further candidate evaluation if any invariant was violated. Multi-waypoint cost lookahead was intentionally unsupported to avoid pretending that unexecuted planned endpoints are future measurements.

## Contact-Reference Overlay

Harness entry point:

```python
from auriga.perception.overlays.contact_overlay import render_contact_overlay

camera = geometry.camera(identity, observation_id, camera_id)
# Resolve the original RGB through actor-scoped artifact access.
overlay_bgr, metadata = render_contact_overlay(
    camera,
    original_image_bgr,
    arms=["left", "right"],
    selections={"left": {"contact_region": "finger_middle"}},
    targets={
        "left": {
            "position_m": caller_selected_contact_target,
            "approach_axis_world": caller_requested_approach_direction,
        }
    },
)
```

`camera` is the validated output of `GeometryTools.camera`, ensuring 2D projection uses the standard optical right/down/forward convention, intrinsics, distortion coefficients, camera mount, and synchronized public proprioception. Given the original uncropped BGR image array, `render_contact_overlay` returns a new image of identical dimensions while leaving the source image and stored records untouched and preserving the source RGB reference in `metadata`. It has no simulator or scene-query API; the caller archives the rendered overlay as a separate artifact and returns it alongside its source observation metadata.

Each selected arm requires a measured `arx-x5-link6-v1` pose in the calibrated reference frame. Current finger reference points are computed as `p_link6 + R_link6 @ [offset_m, 0, 0]`:

| Contact Reference | Axial Offset from `link6` Along Local $+x$ |
| --- | ---: |
| Distal pad center (`"pad_center"`) | `0.1501 m` |
| Nominal finger middle (`"finger_middle"`) | `0.1226 m` |
| Fingertip reference (`"fingertip"`) | `0.1576 m` |
| Custom contact depth `d` (`contact_depth_m`) | `0.1501 - d` m |

The overlay renders all three standard reference points, highlights the caller's selected reference, and draws a `60 mm` arrow along the current tool approach axis. Labels prefixed with **actual** derive solely from measured robot proprioception, whereas optional **REQUESTED** crosshairs and `60 mm` approach arrows derive solely from caller inputs. Note that `targets[arm].position_m` represents the requested contact reference point **between the fingers**—neither a `link6` flange target nor a detected object surface point.

The returned `metadata` records 3D world coordinates, projected pixel coordinates in the original image, camera-frame depth, provenance kind, and whether each point lies in front of the camera (`in_front`) and inside the image bounds (`in_image`). Points behind the camera or outside the frame are retained in `metadata` without drawing visible markers. Because `in_image` checks only the camera frustum, visual occlusion by scene objects is not evaluated, and the Euclidean distance between current and requested references is purely a kinematic comparison—never proof of object contact, grasp retention, or task success.

## Focused Validation

```bash
PYTHONPATH=tests mamba run -n Auriga python \
  -m unittest test_contact_overlay -v
```

`test_contact_overlay` verifies calibrated 3D-to-2D projection, rotation and camera axis conventions, `actual` vs. `REQUESTED` provenance labeling, source-image immutability, frustum/bounds visibility flags, and rejection of untrusted or unsynchronized proprioception. These unit tests do not certify physical collision safety or grasp success.
