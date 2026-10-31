"""Tool handlers. Registration binds these functions; dispatch has no name switch."""
from ...core.tool_output import StructuredText, ToolResponse, output_contract_error
from ...core.presentation.actor_payload import LEAN_PROFILES, lean_action_result


def execute_action(runtime, name, args, correlation):
    if name == "robot_wait" and not runtime.enable_wait:
        raise ValueError("bounded_wait_not_enabled_for_run")
    args = dict(args)
    args.setdefault("action_id", "action-" + correlation["tool_call_id"])
    if name == "robot_policy_step":
        result = runtime.policy_step(args, correlation)
    else:
        action = runtime._action_from_args(name, args)
    if name != "robot_policy_step":
        result = runtime.execute(action, correlation)
    try:
        return _action_response(runtime, result, correlation, obs3d=name == "robot_wait")
    except Exception:
        # Rendering happens after execution; retain its receipt even without images.
        return output_contract_error(ToolResponse([StructuredText(result, "action_result")],
            data=result, kind="action", data_projection="action_result"), name, "action_feedback_unavailable", "/content")


def _action_response(runtime, result, correlation, obs3d=False):
    depth_feedback = []
    if runtime.depth_auto_feedback:
        try:
            depth_feedback = runtime.depth.feedback(result["request"], result["receipt"], correlation)
        except Exception as error:
            # The robot action already completed. Optional perception
            # failure must not hide its receipt or suggest replaying it.
            depth_feedback = [dict(depth_derived=True, status="uncertain",
                reason="automatic_feedback_failed:" + type(error).__name__)]
    presented = result
    projection = "action_result"
    if runtime.actor_payload_profile == "compact_v1" and not correlation.get("program_id"):
        from ...core.presentation.actor_payload import compact_action_result
        presented = compact_action_result(result)
    elif runtime.actor_payload_profile in LEAN_PROFILES and not correlation.get("program_id"):
        presented = lean_action_result(result)
    if "hold_native_steps" in result["request"]:
        current_id = runtime.latest["observation_id"] if runtime.latest else None
        result = {**result, "current_observation_id": current_id,
            "result_observation_is_current": result["receipt"].get("result_observation_id") == current_id
                and current_id is not None}
        presented = {**presented, "current_observation_id": current_id,
            "result_observation_is_current": result["receipt"].get("result_observation_id") == current_id
                and current_id is not None}
    if depth_feedback:
        from ...perception.depth.depth_payload import numeric_summary
        presented = {**presented, "depth_feedback": (depth_feedback if correlation.get("program_id") else
            [numeric_summary(item) for item in depth_feedback])}
    from ...perception.depth.depth_payload import compact_action_feedback
    try:
        concise = compact_action_feedback(runtime, result, depth_feedback, correlation)
    except Exception:
        concise = None
    if concise is not None:
        concise["depth_feedback"] = [numeric_summary(item) for item in depth_feedback]
        presented = concise
        projection = "action_display"
    from ...core.presentation.feedback_delivery import action_trajectory_feedback
    trajectory, trajectory_images = action_trajectory_feedback(runtime, result["request"], result["receipt"], correlation)
    if trajectory is not None:
        presented = {**presented, "trajectory_feedback": trajectory}
    scheduling = runtime._scheduling_advisory(result["receipt"], correlation)
    if scheduling:
        presented = {**presented, "scheduling_advisory": scheduling}
    content = [StructuredText(presented, projection=projection)] + trajectory_images
    for observation_id in result["receipt"].get("wait_observation_ids", []):
        bundle = runtime.store.read("observation", observation_id)
        cameras = [camera["camera_id"] for camera in bundle["cameras"]]
        camera_id = "cam_head" if "cam_head" in cameras else cameras[0]
        sample = runtime.observation_content(bundle, correlation, camera_id=camera_id,
            metadata_delivery=False, structured=True)
        metadata = dict(sample[0].value)
        if runtime.actor_payload_profile == "lean_v2" and not correlation.get("program_id"):
            # Each sample image needs its time and measured state; calibration,
            # budgets and conventions are in the current observation that follows.
            metadata = {key: metadata[key] for key in ("robot_state", "image_view") if key in metadata}
        metadata.update(temporal_sample=True, current=False,
            observation_id=observation_id, camera_id=camera_id,
            sim_step=bundle["sim_step"], sim_time_s=bundle["sim_time_s"])
        sample[0] = StructuredText(metadata, projection="observation")
        content += sample
    if result["receipt"].get("result_observation_id"):
        bundle = runtime.store.read("observation", result["receipt"]["result_observation_id"])
        content += runtime.observation_content(bundle,
            correlation, numeric_only=concise is not None, feedback_action=result["request"], feedback_receipt=result["receipt"], structured=True)
        if obs3d:
            from ...perception.external.obs3d import action_attachments
            content += action_attachments(runtime, bundle, correlation)
    return ToolResponse(content, data=result, kind="action", data_projection="action_result")
