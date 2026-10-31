"""Public completion reminders; the actor still chooses and executes every move."""

import json

COMPLETION_GUIDANCE_MODES = ("none", "initial", "hook", "both")
COMPLETION_GUIDANCE_SOURCE = "public_bimanual_initial_link6_pose_contract_v4"
COMPLETION_GUIDANCE_TRIGGER = "turn.completed"

LINK6_ACTION_GUIDANCE = (
    "These are link6 targets for robot_action: each move stage has kind='move', a unique id, arm, "
    "position_m and quaternion_xyzw. Use mode='preview' to review the chosen route and cached evidence, "
    "then mode='execute' with its exact plan_id; do not replace link6 targets with grasp-center coordinates. "
)

INITIAL_COMPLETION_GUIDANCE = (
    "At the beginning, save the measured position and orientation of both arms from robot_observe: "
    "robot_state.arms.left.ee_pose and robot_state.arms.right.ee_pose (position_m and quaternion_xyzw). "
    + LINK6_ACTION_GUIDANCE +
    "If the public task rules require return-home, after the visible object-placement requirements "
    "are complete, safely release any held object, "
    "clear the placed objects, and return BOTH arms to their saved initial positions AND orientations "
    "before declaring completion. For tasks without a public return-home rule, do not add this "
    "requirement; assess the remaining task using permitted observations and native feedback. "
    "If the environment still has not ended, inspect the actual remaining "
    "issue using permitted observations instead of assuming more object placement is needed."
)

HOOK_COMPLETION_GUIDANCE = (
    "The native environment has not ended. A check of the measured link6 poses found that at least "
    "one arm is outside the return-home tolerance relative to this episode's saved initial pose. "
    "This is a pose measurement, not an additional task completion condition. "
    "If the public task rules require return-home and the visible object-placement requirements are complete, "
    "safely release any held object, clear the placed objects, and return BOTH arms to their initial "
    "positions AND orientations for this episode. For tasks without a public return-home rule, "
    "inspect the remaining task using permitted observations and native feedback. "
    "Use the measured robot_state.arms.left.ee_pose and "
    "robot_state.arms.right.ee_pose (position_m and quaternion_xyzw) from this episode's initial "
    "robot_observe observation, retrieving that permitted observation from this thread's history if "
    "needed. " + LINK6_ACTION_GUIDANCE +
    "Do not treat a current pose as an initial pose or invent missing initial measurements; report "
    "missing evidence if the initial observation cannot be recovered. Choose a safe route using "
    "permitted RGB and measured state, then inspect the actual feedback. If the environment still "
    "has not ended, inspect the remaining issue using permitted observations. Do not read simulator "
    "files, hidden evaluator thresholds or object coordinates; do not reset the simulation."
)


def hook_completion_guidance(initial_observation_id=None):
    """Link only a saved permitted observation; never insert evaluator poses."""
    if initial_observation_id is None:
        return HOOK_COMPLETION_GUIDANCE
    return (HOOK_COMPLETION_GUIDANCE + "\nA permitted observation was saved before the first actor turn "
            "of this episode. Recover its measured initial poses using history_view_observation with "
            + json.dumps({"observation_id": initial_observation_id}) + ".")
