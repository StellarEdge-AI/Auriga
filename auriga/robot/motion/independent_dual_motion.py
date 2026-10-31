"""Advance two resampled arm routes without a barrier at every waypoint.

The simulator remains single-thread owned. Planning pauses both arms' physics;
each dispatched native row then advances both current queues together. This is
not a coupled collision planner or a constraint controller for shared objects.
"""

from auriga.robot.motion.motion_speed import arm_action


ARMS = ("left", "right")


def execute_independent_dual(adapter, action, receipt, segments, planning_diagnostics,
                             progress, interrupted, dispatch, deadline):
    path = action["path"]
    actions = {arm: arm_action(action, arm) for arm in ARMS}
    points = {arm: [point[arm] for point in actions[arm]["path"]["waypoints"]] for arm in ARMS}
    robots = {arm: adapter.robot.get_robot_by_arm_name(arm + "_arm") for arm in ARMS}
    limit = int(adapter.np.ceil(10 * adapter.control_hz))
    states = {}
    trace = {"arm": "both", "dual_arm_progression": "independent", "action_space": "native_joint",
             "actions": [], "row_contexts": [], "arms": {arm: {"segments": []} for arm in ARMS},
             "dt_s": 1 / adapter.control_hz,
             "trace_semantics": "actions are incrementally scheduled merged rows, not an uncomputed future plan; arms retain complete compiled segments including unexecuted remainders",
             "step_semantics": "one merged native row per shared step; each arm independently advances motion, jaw and final hold"}
    segments.append(trace)
    receipt.update(dual_arm_progression="independent", arm_options={arm: {
        "speed_scale": actions[arm].get("speed_scale", 1.),
        "gripper_speed_scale": actions[arm].get("gripper_speed_scale", 1.),
        "waypoint_speed_scales": [adapter._waypoint_speed_scale(actions[arm], i) for i in range(len(points[arm]))]}
        for arm in ARMS}, coordination_semantics=(
            "both initial plans precede any dispatch; each arm advances after its own measured endpoint check; "
            "after_motion jaw starts at that arm's final arrival; a later peer failure can occur after release; "
            "same-row completions checked before either next phase; final held endpoints rechecked; one shared 10 s cap"))

    def fail(reason, arm, index, **extra):
        receipt.update(reason=reason, failed_arm=arm, failed_waypoint_index=index, **extra)
        receipt["stop_confirmation"] = adapter._hold()

    def compile_next(indices):
        if interrupted("planning"):
            return False
        # This snapshot and every native plan in this group share unchanged
        # simulator state. Later segments use the freshly measured own joints.
        before = adapter.state()
        plans, diagnostics_by_arm = {}, {}
        for arm, index in indices.items():
            if interrupted("planning"):
                return False
            progress({"phase": "planning", "arm": arm, "waypoint_index": index,
                      "sim_step": adapter.control_step, "dual_arm_progression": "independent"})
            speed = adapter._waypoint_speed_scale(actions[arm], index)
            options = {}
            if (getattr(adapter, "joint_replan_enabled", False) and path.get("kind") == "backend_free"
                    and path.get("blend_radius_m", 0) == 0
                    and path.get("orientation", "backend_free") == "backend_free"
                    and not any(path.get(key) for key in ("constraints", "path_constraints", "constraint_pose"))):
                options["joint_replan_context"] = {"deadline": deadline, "speed_scale": speed,
                    "position_tolerance_m": path.get("position_tolerance_m", .005),
                    "interrupted": lambda: interrupted("planning")}
            diagnostics = diagnostics_by_arm[arm] = []
            try:
                plans[arm] = adapter._plan_segment(robots[arm], points[arm][index], diagnostics, index, **options)
            except Exception:
                receipt.update(failed_arm=arm, failed_waypoint_index=index)
                raise
            finally:
                planning_diagnostics.extend(dict(item, arm=arm) for item in diagnostics)
            if interrupted("planning"):
                return False
        failed = [arm for arm in indices if plans[arm] is None]
        if failed:
            arm = failed[0]
            index = indices[arm]
            fail("path_infeasible", arm, index, failed_arms=failed)
            receipt["failure_feedback"] = adapter._failure_feedback(actions[arm], arm, index, diagnostics_by_arm[arm])
            receipt["planner_failure_stage"] = receipt["failure_feedback"]["failure_stage"]
            return False
        remaining = limit - receipt["executed_steps"]
        prepared = {}
        for arm, index in indices.items():
            try:
                positions = adapter._resample_action_plan(plans[arm][0], arm, actions[arm], index)
                compiled = adapter._motion_segment(actions[arm], before, positions, arm, remaining,
                                                     last_waypoint=index == len(points[arm]) - 1)
            except Exception:
                receipt.update(failed_arm=arm, failed_waypoint_index=index)
                raise
            if compiled is None:
                fail("motion_duration_limit", arm, index)
                return False
            positions, motion, jaws, schedule = compiled
            from auriga.robot.motion.motion_safety import admit_motion
            if not admit_motion(adapter, before, {arm: positions}, receipt,
                    native_steps=len(motion)+len(jaws), request_remaining=remaining, waypoint_index=index):
                return False
            if not motion:
                raise ValueError("empty_independent_arm_motion")
            offset = 0 if arm == "left" else 7
            speed = adapter._waypoint_speed_scale(actions[arm], index)
            own_trace = {"waypoint_index": index, "compiled_sim_step": adapter.control_step,
                "target_ee_pose": points[arm][index],
                "joint_names": list(robots[arm].arm_joints_name), "q_rad": positions.tolist(),
                "speed_scale": speed, "gripper_speed_scale": actions[arm].get("gripper_speed_scale", 1.),
                "command_step_limits_rad": adapter._speed_step_limits(arm, speed).tolist(),
                "resampling": "legacy total variation; 10 s cap" if speed == 1 else
                              "bounded significant-turn polyline; 0.001 rad simplification tolerance; 10 s cap",
                "planned_motion_steps": len(motion), "planned_gripper_steps": len(jaws),
                "action_fields": "own six joint targets plus own jaw goal",
                "motion_actions": [row[offset:offset + 7] for row in motion],
                "gripper_actions": [row[offset:offset + 7] for row in jaws]}
            if schedule is not None:
                own_trace["gripper_motion"] = schedule
            contact = actions[arm].get("input_grasp_pose", {}).get("waypoint_contact_frames")
            if isinstance(contact, list) and index < len(contact):
                own_trace["grasp_contact_frame"] = contact[index]
            trace["arms"][arm]["segments"].append(own_trace)
            prepared[arm] = {"index": index, "phase": "motion", "cursor": 0,
                             "motion": motion, "gripper": jaws, "held": motion[-1], "trace": own_trace}
        states.update(prepared)
        return not interrupted("planning")

    def check_endpoint(arm, phase):
        index = states[arm]["index"]
        actual = adapter.state()["arms"][arm]["ee_pose"]
        target = points[arm][index]
        if not all(adapter.np.isfinite(actual[field]).all() for field in ("position_m", "quaternion_xyzw")):
            receipt.update(failed_arm=arm, failed_waypoint_index=index)
            raise ValueError("nonfinite_endpoint_measurement")
        position = float(adapter.np.linalg.norm(adapter.np.asarray(actual["position_m"]) - target["position_m"]))
        angle = 2 * float(adapter.np.arccos(adapter.np.clip(abs(adapter.np.dot(
            actual["quaternion_xyzw"], target["quaternion_xyzw"])), 0, 1)))
        if not adapter.np.isfinite([position, angle]).all():
            receipt.update(failed_arm=arm, failed_waypoint_index=index)
            raise ValueError("nonfinite_endpoint_residual")
        result = {"waypoint_index": index, "sim_step": adapter.control_step, "phase": phase,
                  "command_phase": states[arm]["phase"],
                  "position_error_m": position, "orientation_error_rad": angle,
                  "speed_scale": adapter._waypoint_speed_scale(actions[arm], index)}
        receipt.setdefault("arm_results", {})[arm] = result
        receipt["waypoint_results"].append({"arm": arm, **result, "arms": {arm: dict(result)}})
        receipt.update(position_error_m=max(value["position_error_m"] for value in receipt["arm_results"].values()),
            orientation_error_rad=max(value["orientation_error_rad"] for value in receipt["arm_results"].values()),
            endpoint_result_semantics="latest measured check per arm, with individual waypoint_index/sim_step; only final_held checks verify terminal targets together")
        return position <= path.get("position_tolerance_m", .005) and angle <= path.get("orientation_tolerance_rad", .1)

    def check_group(arms, phase):
        # Evaluate every arm completing on this row before making any next
        # phase eligible. A peer failing on the same row prevents jaw release.
        failed = [arm for arm in arms if not check_endpoint(arm, phase)]
        if failed:
            fail("tracking_error", failed[0], states[failed[0]]["index"], failed_arms=failed)
        return not failed

    if not compile_next({arm: 0 for arm in ARMS}):
        return
    progress({"phase": "started", "arm": "both", "dual_arm_progression": "independent",
              "sim_step": adapter.control_step, "planned_steps": None})
    while True:
        if interrupted("execution"):
            return
        if receipt["executed_steps"] >= limit:
            active = next(arm for arm in ARMS if states[arm]["phase"] != "hold")
            fail("motion_duration_limit", active, states[active]["index"])
            return
        row, context = [], {}
        for arm in ARMS:
            state = states[arm]
            phase = state["phase"]
            source = state["held"] if phase == "hold" else state[phase][state["cursor"]]
            offset = 0 if arm == "left" else 7
            row.extend(source[offset:offset + 7])
            context[arm] = {"phase": phase, "waypoint_index": state["index"],
                            "phase_step_index": None if phase == "hold" else state["cursor"]}
        trace["actions"].append(row)
        trace["row_contexts"].append(context)
        if not dispatch(row, command_phase="shared", arm_context=context):
            return
        finished = []
        for arm in ARMS:
            state = states[arm]
            if state["phase"] == "hold":
                continue
            state["held"] = state[state["phase"]][state["cursor"]]
            state["cursor"] += 1
            if state["cursor"] == len(state[state["phase"]]):
                finished.append(arm)
        if not finished:
            continue
        if not check_group(finished, "phase_complete"):
            return
        # Admit fresh jaw ramps as a group after all same-row endpoint checks.
        # A budget failure prevents every new phase from dispatching. Jaws on
        # an earlier row may already have executed; they cannot be rolled back.
        current = adapter.state()
        for arm in finished:
            state = states[arm]
            if state["phase"] == "motion" and state["gripper"]:
                old_count = len(state["gripper"])
                rows, compilation = adapter._arrival_gripper_rows(actions[arm], state["held"], state=current)
                offset = 0 if arm == "left" else 7
                state["gripper"] = rows
                state["trace"].update(initial_gripper_steps_estimate=old_count,
                    planned_gripper_steps=len(rows), gripper_compilation=compilation,
                    gripper_actions=[row[offset:offset + 7] for row in rows])
                if len(rows) > limit - receipt["executed_steps"]:
                    fail("motion_duration_limit", arm, state["index"], failed_phase="before_gripper")
                    return
                from auriga.robot.motion.motion_safety import admit_jaw_budget
                if not admit_jaw_budget(adapter,current,[arm],receipt,native_steps=len(rows),
                        request_remaining=limit-receipt['executed_steps'],waypoint_index=state['index']):
                    return
        next_indices = {}
        for arm in finished:
            state = states[arm]
            if state["phase"] == "motion" and state["index"] + 1 < len(points[arm]):
                next_indices[arm] = state["index"] + 1
            elif state["phase"] == "motion" and state["gripper"]:
                state.update(phase="gripper", cursor=0)
            else:
                state.update(phase="hold", cursor=0)
        if next_indices and not compile_next(next_indices):
            return
        if all(state["phase"] == "hold" for state in states.values()):
            if check_group(ARMS, "final_held"):
                receipt.update(status="completed", reason=None)
            return
