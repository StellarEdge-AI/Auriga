# Motion cost and contact-reference overlays

> Status: `auriga.motion_cost.preview_motion_cost` and the `robot_preview_cost` tool described in the first section were removed in the 2026-10 refactor (trusted_legacy only; unreachable by the actor or programs) and are kept here as historical reference; `tests/test_motion_cost.py` went with them. The contact-reference overlay (`auriga/perception/overlays/contact_overlay.py`) remains current. Current action interface: [unified actions](unified-actions.md).

These helpers expose two bounded forms of assistance using the actor's own
candidate targets and permitted robot observations. Cost preview plans and
compiles commands without moving the robot. Contact overlay projects disclosed
finger reference geometry onto an existing observed image. Neither helper
infers task-object truth or certifies manipulation success.

## Candidate motion cost

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

The caller supplies **one to three** uniquely labelled, actor-authored
`EEAction` candidates. Harness must validate the current observation/environment
and perform ordinary grasp/contact conversion and speed resolution first. Each
action must contain exactly one `backend_free` waypoint. A waypoint may contain
one arm or both arms, with the same explicit progression, per-arm speeds and jaw
events supported by execution. Policy/queue actions, holds, cached plan IDs,
unresolved speed presets, path constraints and future waypoints are rejected.

The worker must hold its existing operation lock. The helper snapshots measured
state and the worker's preview binding, then runs `_plan_segment`,
`_resample_action_plan` and `_motion_segment` for every candidate from that same
state. It never sets robot joints to a guessed endpoint, dispatches a native
row, renders an observation, requests task scores, or caches an executable plan.
It deliberately supplies no `joint_replan_context`, so the optional winding
replan assistance remains unused even if enabled elsewhere. Normal native
planning retains its disclosed robot/static-table collision scope.

Every candidate result retains its label and action digest, feasibility/status
and any error. Successful compilation returns:

- `nominal_native_steps`, corresponding physics ticks and nominal duration;
- per-arm motion, jaw, endpoint-hold and final-hold counts;
- each joint's total absolute command travel, maximum joint travel and sum
  across joints/arms, starting from the actual measured joint state;
- resolved arm/jaw speeds and any compiled during-motion jaw schedule.

For one segment, let `m_left`, `m_right` be motion row counts after resampling
and any during-motion jaw subdivision, and let `j_left`, `j_right` be additional
after-motion jaw row counts. A single arm costs `m + j`. Paired dual motion
costs `max(m_left, m_right) + max(j_left, j_right)`. Independent dual motion
costs `max(m_left + j_left, m_right + j_right)`. These formulas preserve the
respective execution jaw policy. Holding slots occupy an arm while the peer
works; they are not additional shared native rows.

For example, 3 left motion rows followed by 6 left jaw rows and 9 right motion
rows compile to 15 paired rows or 9 independent rows. Both results still depend
on the required measured endpoint checks and jaw-start conditions. The shared
10-second request cap is checked after merging costs, as well as during each
arm's compilation.

The count is **exact for the newly compiled nominal commands**, not a prediction
of completed physical work. Early tracking failure, changed jaw opening,
cancellation, episode termination or contact can prevent those rows from
executing. A later execution invokes planning again and may return a different
path or cost; the cost result is not a consumable plan ID. Independently planned
candidates can also differ because of planner variability. No candidate is
automatically selected or retried.

The result includes the caller observation ID and measured worker
`state_binding` before/after, `same_measured_state_verified`,
`zero_step_check_passed`, and actual native/physics counter deltas. A state or
counter change invalidates the affected comparison and stops further candidate
planning. Counters are reported honestly if such an invariant fails; the helper
does not attempt a state rollback. The observation ID is supplied by Harness,
which remains responsible for rejecting stale observations.

Multi-waypoint cost is deliberately unsupported. Execution plans a later
segment from its future measured state. Pretending that planned endpoints are
measurements would produce false precision, especially during contact or dual
motion. If an actor wants cost feedback for a later decision, it should obtain
the intervening observation and submit a new bounded candidate comparison.

## Contact-reference overlay

Harness entry point:

```python
from auriga.perception.overlays.contact_overlay import render_contact_overlay

camera = geometry.camera(identity, observation_id, camera_id)
# Resolve this original RGB through actor-scoped artifact access.
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

`camera` is the validated result of `GeometryTools.camera`, so projection uses
the existing optical right/down/forward convention, intrinsics, distortion,
camera mount and synchronized public proprioception. The image must be its
original, uncropped BGR array. The helper returns a separate image of identical
dimensions, leaves the original image and records unchanged, and retains the
original RGB reference in metadata. It has no simulator or scene-query API.
The caller archives the overlay separately and returns it together with its
source observation/image metadata.

Each selected arm requires a measured `arx-x5-link6-v1` pose in the calibration
reference frame. Current reference positions are derived as
`p_link6 + R_link6 @ [offset_m, 0, 0]`:

| Reference | Offset from link6 along local +x |
|---|---:|
| Distal pad center | 0.1501 m |
| Nominal finger middle | 0.1226 m |
| Fingertip reference | 0.1576 m |
| Selected custom depth `d` | `0.1501 - d` m |

The overlay draws these three current references, the explicitly selected
reference, and the current approach axis. Labels beginning with **actual** use
measured robot pose only. Optional **REQUESTED** cross markers and approach
arrows use caller inputs only. `targets[arm].position_m` denotes the selected
contact reference between the fingers, **not** a link6 target or a detected
object point. Requested approach directions are normalized and drawn with a
60 mm arrow; actual arrows also use 60 mm.

Metadata records world coordinates, projected original-image pixels, camera
depth, source kind and whether each point lies in front of the camera and
within image bounds. Offscreen/behind-camera points are retained in metadata
but not drawn as visible points. `in_image` describes the camera frustum; visual
occlusion remains unassessed. Current-to-requested distance is a robot-reference
comparison, never evidence of object contact, grasp retention or task success.

## Focused validation

```bash
PYTHONPATH=tests mamba run -n Auriga python \
  -m unittest test_contact_overlay -v
```

CPU tests compare cost compilation with fresh deterministic dispatch fixtures,
including paired/independent jaw timing, slow speeds, during-motion subdivision,
shared caps, failure isolation and invariant violations. They assert no native
dispatch, no observation rendering, no new executable cache and no replan
assistance during cost preview. Overlay tests verify calibrated coordinates,
rotation/camera conventions, current/requested provenance, original-image
preservation, visibility handling and rejection of untrusted or unsynchronized
measurements. These tests do not validate physical collision safety or grasp
success.
