"""Synchronized native rows from independent per-arm planner trajectories.

Each arm retains its own sampling cadence. A shorter arm holds its endpoint;
merging these paths does not establish other-arm or task-object collision safety.
"""


ARMS = ("left", "right")


def is_dual_action(action):
    """Recognize dual paths before dispatch, including unsupported mode checks."""
    path = action.get("path")
    points = path.get("waypoints") if isinstance(path, dict) else None
    return isinstance(points, list) and any(
        isinstance(waypoint, dict) and all(arm in waypoint for arm in ARMS)
        for waypoint in points)


def merge_rows(rows, fallback=None):
    """Join each arm's seven fields at common indices, holding final targets."""
    count = max(len(rows[arm]) for arm in ARMS)
    merged = []
    for index in range(count):
        row = []
        for arm, offset in (("left", 0), ("right", 7)):
            values = rows[arm]
            source = values[min(index, len(values) - 1)] if values else fallback[arm]
            row.extend(source[offset:offset + 7])
        merged.append(row)
    return merged


def execute_dual(adapter, action, receipt, segments, planning_diagnostics,
                 progress, interrupted, dispatch, deadline):
    """Execute paired waypoints inside the worker's common receipt lifecycle."""
    if action.get("dual_arm_progression", "paired") == "independent":
        from auriga.robot.motion.independent_dual_motion import execute_independent_dual
        return execute_independent_dual(adapter, action, receipt, segments,
                                        planning_diagnostics, progress, interrupted, dispatch, deadline)
    from auriga.robot.motion.motion_speed import arm_action

    actions = {arm: arm_action(action, arm) for arm in ARMS}
    robots = {arm: adapter.robot.get_robot_by_arm_name(arm + "_arm") for arm in ARMS}
    path = action["path"]
    receipt["dual_arm_progression"] = "paired"
    receipt["arm_options"] = {arm: {
        "speed_scale": actions[arm].get("speed_scale", 1.),
        "gripper_speed_scale": actions[arm].get("gripper_speed_scale", 1.),
        "waypoint_speed_scales": [adapter._waypoint_speed_scale(actions[arm], index)
                                  for index in range(len(path["waypoints"]))]}
        for arm in ARMS}
    receipt["coordination_semantics"] = (
        "one merged 14-value native row per step; independent plans from the same measured state; "
        "shorter arms hold their final targets; after_motion jaws start after both endpoint checks")

    def endpoint_result(waypoint_index, targets):
        state = adapter.state()
        arms = {}
        for arm in ARMS:
            actual, target = state["arms"][arm]["ee_pose"], targets[arm]
            if not all(adapter.np.isfinite(actual[field]).all() for field in ("position_m", "quaternion_xyzw")):
                receipt.update(failed_arm=arm, failed_waypoint_index=waypoint_index)
                raise ValueError("nonfinite_endpoint_measurement")
            position_error = float(adapter.np.linalg.norm(
                adapter.np.asarray(actual["position_m"]) - target["position_m"]))
            orientation_error = 2 * float(adapter.np.arccos(adapter.np.clip(
                abs(adapter.np.dot(actual["quaternion_xyzw"], target["quaternion_xyzw"])), 0, 1)))
            if not adapter.np.isfinite([position_error, orientation_error]).all():
                receipt.update(failed_arm=arm, failed_waypoint_index=waypoint_index)
                raise ValueError("nonfinite_endpoint_residual")
            arms[arm] = {"position_error_m": position_error, "orientation_error_rad": orientation_error,
                         "waypoint_index": waypoint_index, "sim_step": adapter.control_step,
                         "speed_scale": adapter._waypoint_speed_scale(actions[arm], waypoint_index)}
        receipt.update(arm_results=arms,
                       endpoint_result_waypoint_index=waypoint_index, endpoint_result_sim_step=adapter.control_step,
                       endpoint_result_semantics="last checked paired waypoint; may precede terminal state after a later failure",
                       position_error_m=max(value["position_error_m"] for value in arms.values()),
                       orientation_error_rad=max(value["orientation_error_rad"] for value in arms.values()))
        return {"waypoint_index": waypoint_index, "arms": arms, "sim_step": adapter.control_step}

    def endpoints_valid(result):
        failed = [arm for arm in ARMS if (
            result["arms"][arm]["position_error_m"] > path.get("position_tolerance_m", .005)
            or result["arms"][arm]["orientation_error_rad"] > path.get("orientation_tolerance_rad", .1))]
        if failed:
            receipt.update(reason="tracking_error", failed_waypoint_index=result["waypoint_index"],
                           failed_arm=failed[0], failed_arms=failed)
            receipt["stop_confirmation"] = adapter._hold()
        return not failed

    for waypoint_index, waypoint in enumerate(path["waypoints"]):
        if interrupted("planning"):
            return
        progress({"phase": "planning", "arm": "both", "waypoint_index": waypoint_index,
                  "sim_step": adapter.control_step})
        # Neither planning nor compilation advances the simulator. Both plans
        # therefore start from this pair's unchanged measured joint state.
        before = adapter.state()
        plans, diagnostics_by_arm = {}, {}
        for arm in ARMS:
            if interrupted("planning"):
                return
            options = {}
            speed = adapter._waypoint_speed_scale(actions[arm], waypoint_index)
            if (getattr(adapter, "joint_replan_enabled", False) and path.get("kind") == "backend_free"
                    and path.get("blend_radius_m", 0) == 0
                    and path.get("orientation", "backend_free") == "backend_free"
                    and not any(path.get(key) for key in ("constraints", "path_constraints", "constraint_pose"))):
                options["joint_replan_context"] = {"deadline": deadline, "speed_scale": speed,
                    "position_tolerance_m": path.get("position_tolerance_m", .005),
                    "interrupted": lambda: interrupted("planning")}
            diagnostics = diagnostics_by_arm[arm] = []
            try:
                plans[arm] = adapter._plan_segment(robots[arm], waypoint[arm], diagnostics,
                                                   waypoint_index, **options)
            except Exception:
                receipt.update(failed_arm=arm, failed_waypoint_index=waypoint_index)
                raise
            finally:
                planning_diagnostics.extend(dict(item, arm=arm) for item in diagnostics)
            if interrupted("planning"):
                return
        failed = [arm for arm in ARMS if plans[arm] is None]
        if failed:
            arm = failed[0]
            receipt.update(reason="path_infeasible", failed_waypoint_index=waypoint_index,
                           failed_arm=arm, failed_arms=failed)
            receipt["failure_feedback"] = adapter._failure_feedback(
                actions[arm], arm, waypoint_index, diagnostics_by_arm[arm])
            receipt["planner_failure_stage"] = receipt["failure_feedback"]["failure_stage"]
            return

        remaining = int(adapter.np.ceil(10 * adapter.control_hz)) - receipt["executed_steps"]
        compiled = {}
        for arm in ARMS:
            try:
                positions = adapter._resample_action_plan(plans[arm][0], arm, actions[arm], waypoint_index)
                compiled[arm] = adapter._motion_segment(actions[arm], before, positions, arm, remaining,
                    last_waypoint=waypoint_index == len(path["waypoints"]) - 1)
            except Exception:
                receipt.update(failed_arm=arm, failed_waypoint_index=waypoint_index)
                raise
            if compiled[arm] is None:
                receipt.update(reason="motion_duration_limit", failed_arm=arm,
                               failed_waypoint_index=waypoint_index)
                return
        motion_rows = merge_rows({arm: compiled[arm][1] for arm in ARMS})
        extra_rows = merge_rows({arm: compiled[arm][2] for arm in ARMS},
                                fallback={arm: compiled[arm][1][-1] for arm in ARMS})
        if len(motion_rows) + len(extra_rows) > remaining:
            receipt.update(reason="motion_duration_limit", failed_waypoint_index=waypoint_index)
            return
        from auriga.robot.motion.motion_safety import admit_motion
        if not admit_motion(adapter, before, {arm: compiled[arm][0] for arm in ARMS}, receipt,
                native_steps=len(motion_rows)+len(extra_rows), request_remaining=remaining,
                waypoint_index=waypoint_index):
            return

        arm_traces = {}
        for arm in ARMS:
            positions, own_motion, own_extra, schedule = compiled[arm]
            speed = adapter._waypoint_speed_scale(actions[arm], waypoint_index)
            arm_traces[arm] = {
                "joint_names": list(robots[arm].arm_joints_name), "q_rad": positions.tolist(),
                "speed_scale": speed, "gripper_speed_scale": actions[arm].get("gripper_speed_scale", 1.),
                "command_step_limits_rad": adapter._speed_step_limits(arm, speed).tolist(),
                "resampling": "legacy total variation; 10 s cap" if speed == 1 else
                              "bounded significant-turn polyline; 0.001 rad simplification tolerance; 10 s cap",
                "planned_motion_steps": len(own_motion), "motion_hold_steps": len(motion_rows) - len(own_motion),
                "planned_gripper_steps": len(own_extra), "gripper_hold_steps": len(extra_rows) - len(own_extra)}
            if schedule is not None:
                arm_traces[arm]["gripper_motion"] = schedule
        segment = {"arm": "both", "waypoint_index": waypoint_index, "action_space": "native_joint",
                   "actions": motion_rows + extra_rows, "arms": arm_traces, "dt_s": 1 / adapter.control_hz,
                   "planned_motion_steps": len(motion_rows), "planned_gripper_steps": len(extra_rows),
                   "step_semantics": "max per-arm motion length plus max per-arm after_motion jaw length; held rows count once"}
        segments.append(segment)
        progress({"phase": "started", "arm": "both", "waypoint_index": waypoint_index,
                  "planned_steps": len(motion_rows) + len(extra_rows), "sim_step": adapter.control_step})
        for row in motion_rows:
            if not dispatch(row, waypoint_index):
                return
        # Release is conditional on both actual endpoints, including the arm
        # that reached its last command earlier and was held while its peer moved.
        waypoint_result = endpoint_result(waypoint_index, waypoint)
        receipt["waypoint_results"].append(waypoint_result)
        if not endpoints_valid(waypoint_result):
            return
        # Both endpoints must pass before either jaw is admitted. Rebuild both
        # ramps from the same fresh state, then check their merged shared cost.
        current = adapter.state()
        arrived_jaws = {}
        for arm in ARMS:
            old_rows = compiled[arm][2]
            rows = old_rows
            if old_rows:
                rows, compilation = adapter._arrival_gripper_rows(actions[arm], compiled[arm][1][-1], state=current)
                offset = 0 if arm == "left" else 7
                arm_traces[arm].update(initial_gripper_steps_estimate=len(old_rows),
                    gripper_actions=[row[offset:offset + 7] for row in rows], gripper_compilation=compilation)
            arrived_jaws[arm] = rows
            arm_traces[arm]["planned_gripper_steps"] = len(rows)
        extra_rows = merge_rows(arrived_jaws, fallback={arm: compiled[arm][1][-1] for arm in ARMS})
        for arm in ARMS:
            arm_traces[arm]["gripper_hold_steps"] = len(extra_rows) - len(arrived_jaws[arm])
        segment.update(actions=motion_rows + extra_rows, planned_gripper_steps=len(extra_rows))
        if receipt["executed_steps"] + len(extra_rows) > int(adapter.np.ceil(10 * adapter.control_hz)):
            receipt.update(reason="motion_duration_limit", failed_waypoint_index=waypoint_index,
                           failed_phase="before_gripper")
            receipt["stop_confirmation"] = adapter._hold()
            return
        from auriga.robot.motion.motion_safety import admit_jaw_budget
        if not admit_jaw_budget(adapter,current,ARMS,receipt,native_steps=len(extra_rows),
                request_remaining=int(adapter.np.ceil(10*adapter.control_hz))-receipt['executed_steps'],
                waypoint_index=waypoint_index):
            return
        for row in extra_rows:
            if not dispatch(row, waypoint_index, command_phase="gripper"):
                return
        if extra_rows:
            waypoint_result.update(endpoint_result(waypoint_index, waypoint))
            if not endpoints_valid(waypoint_result):
                return
    receipt.update(status="completed", reason=None)
