"""Bounded native scheduler for explicit per-arm move/jaw/observation queues.

Dependencies order command completion, never infer object contact or retention.
All physics is dispatched on the worker's single thread; planning does not tick
the simulator, and checkpoints end the request rather than silently resuming.
"""

from auriga.robot.motion.feedback_motion import pose_error


ARMS = ("left", "right")


def execute_arm_queue(adapter, action, receipt, segments, planning_diagnostics,
                      progress, interrupted, dispatch, deadline):
    queues = action["arm_queues"]
    robots = {arm: adapter.robot.get_robot_by_arm_name(arm + "_arm") for arm in ARMS}
    before = adapter.state()
    held = {arm: adapter._held_row(before) for arm in ARMS}
    targets = {arm: before["arms"][arm]["ee_pose"] for arm in ARMS}
    tolerances = {arm: .005 for arm in ARMS}
    indices = {arm: 0 for arm in ARMS}
    active = {}
    completed = set()
    all_ids = [stage["id"] for arm in ARMS for stage in queues[arm]]
    trace = {"arm": "both", "action_space": "native_joint", "execution_mode": "arm_queue",
        "actions": [], "row_contexts": [], "arms": {arm: {"segments": []} for arm in ARMS},
        "dt_s": 1 / adapter.control_hz,
        "trace_semantics": "incrementally scheduled shared rows; complete compiled stage rows retained separately per arm"}
    segments.append(trace)
    receipt.update(queue_status="running", completed_stage_ids=[], pending_stage_ids=list(all_ids), stage_results={},
        coordination_semantics="per-arm ordered queues plus explicit dependencies; all ready starts gated together; one merged row per step; observe stops the whole request; command completion does not prove support or retention")
    results = receipt["stage_results"]
    for arm in ARMS:
        for index, stage in enumerate(queues[arm]):
            results[stage["id"]] = dict(arm=arm, stage_index=index, kind=stage["kind"], status="pending", completed_steps=0)
    cap = int(adapter.np.ceil(10 * adapter.control_hz))

    def fail(reason, arm, stage, **extra):
        receipt.update(reason=reason, failed_arm=arm, failed_stage_id=stage["id"],
                       failed_waypoint_index=indices[arm], **extra)
        results[stage["id"]]["status"] = "failed"
        receipt["stop_confirmation"] = adapter._hold()

    def check(arm, target, tolerance, phase, stage=None):
        measured = adapter.state()
        position, orientation = pose_error(measured["arms"][arm]["ee_pose"], target)
        entry = dict(arm=arm, stage_id=stage["id"] if stage else None,
            stage_index=indices[arm], sim_step=adapter.control_step, phase=phase,
            position_error_m=position, orientation_error_rad=orientation)
        receipt["waypoint_results"].append(entry)
        receipt.setdefault("arm_results", {})[arm] = entry
        receipt.update(position_error_m=max(item["position_error_m"] for item in receipt["arm_results"].values()),
            orientation_error_rad=max(item["orientation_error_rad"] for item in receipt["arm_results"].values()),
            endpoint_result_semantics="per-arm phase-tagged measured checks; only final_held checks verify terminal holds")
        if stage:
            if stage["kind"] == "move" and stage.get("input_grasp_pose"):
                entry["grasp_contact_feedback"] = contact_feedback(arm, stage, measured)
            results[stage["id"]].setdefault("endpoint_checks", []).append(entry)
        ori_tol = stage.get("orientation_tolerance_rad", .1) if stage and stage.get("kind") == "move" else .1
        return position <= tolerance and orientation <= ori_tol

    def projected(arm, stage):
        result = {key: action[key] for key in ("timeout_s", "frame_id", "tcp_config_id") if key in action}
        if stage["kind"] == "move":
            result.update(type="EEAction", path={"kind": "backend_free", "waypoints": [{arm: stage["target"]}],
                "position_tolerance_m": stage.get("position_tolerance_m", .005)},
                speed_scale=stage.get("speed_scale", 1.), gripper_events=[])
            if "input_grasp_pose" in stage:
                result["input_grasp_pose"] = stage["input_grasp_pose"]
        else:
            result.update(type="EEAction", path={"kind": "hold", "waypoints": []},
                gripper_events=[dict(arm=arm, phase="after_motion", goal_normalized=stage["goal_normalized"])],
                gripper_speed_scale=stage.get("gripper_speed_scale", 1.))
        return result

    def contact_feedback(arm, stage, measured):
        from auriga.robot.model.grasp_contact import contact_target_feedback
        try:
            return contact_target_feedback(projected(arm, stage), measured)
        except Exception as error:
            return dict(available=False, reason="contact_feedback_unavailable", error_type=type(error).__name__)

    def start_group(ready):
        if interrupted("planning"):
            return False
        current = adapter.state()
        plans, diagnostics = {}, {}
        for arm, stage in ready.items():
            if stage["kind"] != "move":
                continue
            own = projected(arm, stage)
            options = {}
            if getattr(adapter, "joint_replan_enabled", False):
                options["joint_replan_context"] = {"deadline": deadline, "speed_scale": own["speed_scale"],
                    "position_tolerance_m": own["path"]["position_tolerance_m"],
                    "interrupted": lambda: interrupted("planning")}
            diagnostics[arm] = []
            progress(dict(phase="planning", arm=arm, stage_id=stage["id"], stage_index=indices[arm], sim_step=adapter.control_step))
            try:
                plans[arm] = adapter._plan_segment(robots[arm], stage["target"], diagnostics[arm], indices[arm], **options)
            except Exception:
                receipt.update(failed_arm=arm, failed_stage_id=stage["id"], failed_waypoint_index=indices[arm])
                raise
            finally:
                planning_diagnostics.extend(dict(item, arm=arm, stage_id=stage["id"]) for item in diagnostics[arm])
            if interrupted("planning"):
                return False
        failed = [arm for arm, plan in plans.items() if plan is None]
        if failed:
            arm, stage = failed[0], ready[failed[0]]
            fail("path_infeasible", arm, stage, failed_arms=failed)
            # Each stage projects to a one-waypoint EE action. Keep queue-stage
            # provenance while translating diagnostic indices into that scope.
            local_diagnostics = [dict(item, waypoint_index=0, queue_stage_index=indices[arm]) for item in diagnostics[arm]]
            own = projected(arm, stage)
            try:
                receipt["failure_feedback"] = adapter._failure_feedback(own, arm, 0, local_diagnostics)
            except (KeyError, TypeError, IndexError, ValueError):
                # Metadata is optional descriptive input, never permission to
                # erase the native planning failure if its shape is malformed.
                own.pop("input_grasp_pose", None)
                receipt["failure_feedback"] = adapter._failure_feedback(own, arm, 0, local_diagnostics)
                receipt["failure_feedback"]["grasp_metadata_unavailable"] = True
            receipt["failure_feedback"].update(stage_id=stage["id"], stage_index=indices[arm])
            receipt["planner_failure_stage"] = receipt["failure_feedback"]["failure_stage"]
            return False
        prepared = {}
        for arm, stage in ready.items():
            own, index = projected(arm, stage), indices[arm]
            offset = 0 if arm == "left" else 7
            if stage["kind"] == "move":
                positions = adapter._resample_action_plan(plans[arm][0], arm, own, 0)
                compiled = adapter._motion_segment(own, current, positions, arm, cap-receipt["executed_steps"])
                if compiled is None:
                    fail("motion_duration_limit", arm, stage)
                    return False
                positions, rows, _, _ = compiled
                from auriga.robot.motion.motion_safety import admit_motion
                if not admit_motion(adapter, current, {arm: positions}, receipt,
                        native_steps=len(rows), request_remaining=cap-receipt["executed_steps"],
                        waypoint_index=index, stage_id=stage['id']):
                    results[stage['id']]['status'] = 'failed'
                    return False
                phase = "motion"
            else:
                # A dependency wait can outlast the preceding arrival check.
                # Recheck the held target at the moment the jaw is admitted.
                if not check(arm, targets[arm], tolerances[arm], "before_gripper", stage):
                    fail("tracking_error", arm, stage, failed_phase="before_gripper")
                    return False
                rows, compilation = adapter._arrival_gripper_rows(own, held[arm], state=current)
                from auriga.robot.motion.motion_safety import admit_jaw_budget
                if not admit_jaw_budget(adapter,current,[arm],receipt,native_steps=len(rows),
                        request_remaining=cap-receipt['executed_steps'],waypoint_index=index,stage_id=stage['id']):
                    results[stage['id']]['status']='failed'
                    return False
                phase = "gripper"
            if not rows or len(rows) > cap-receipt["executed_steps"]:
                fail("motion_duration_limit", arm, stage)
                return False
            own_trace = dict(stage_id=stage["id"], stage_index=index, kind=stage["kind"],
                compiled_sim_step=adapter.control_step, planned_steps=len(rows), completed_steps=0,
                actions=[row[offset:offset+7] for row in rows], phase=phase)
            if phase == "motion":
                own_trace.update(q_rad=positions.tolist(), speed_scale=own["speed_scale"], target_ee_pose=stage["target"],
                    command_step_limits_rad=adapter._speed_step_limits(arm, own["speed_scale"]).tolist())
                if "input_grasp_pose" in stage:
                    own_trace["input_grasp_pose"] = stage["input_grasp_pose"]
            else:
                own_trace.update(gripper_speed_scale=own["gripper_speed_scale"], goal_normalized=stage["goal_normalized"],
                                 gripper_compilation=compilation)
            trace_index = len(trace["arms"][arm]["segments"])
            trace["arms"][arm]["segments"].append(own_trace)
            prepared[arm] = dict(stage=stage, rows=rows, cursor=0, phase=phase, trace=own_trace, trace_index=trace_index)
        if interrupted("planning"):
            return False
        active.update(prepared)
        for arm, state in prepared.items():
            results[state["stage"]["id"]].update(status="running", started_sim_step=adapter.control_step,
                planned_steps=len(state["rows"]))
        return True

    try:
        while True:
            if interrupted("execution"):
                return
            ready = {arm: queues[arm][indices[arm]] for arm in ARMS if arm not in active and indices[arm] < len(queues[arm])
                     and set(queues[arm][indices[arm]].get("depends_on", [])).issubset(completed)}
            checkpoints = {arm: stage for arm, stage in ready.items() if stage["kind"] == "observe"}
            if checkpoints:
                for arm, stage in checkpoints.items():
                    completed.add(stage["id"])
                    results[stage["id"]].update(status="completed", completed_sim_step=adapter.control_step)
                    indices[arm] += 1
                receipt.update(status="stopped", reason="observation_checkpoint", queue_status="awaiting_observation",
                    checkpoint_stage_ids=[stage["id"] for stage in checkpoints.values()],
                    checkpoint_reasons={stage["id"]: stage.get("reason", "") for stage in checkpoints.values()})
                receipt["stop_confirmation"] = adapter._hold()
                return
            if ready and not start_group(ready):
                return
            if not active:
                if len(completed) != len(all_ids):
                    # Cycles are rejected by validation. Never burn idle rows
                    # if malformed/unresolvable dependencies reach the runtime.
                    receipt.update(reason="queue_dependency_deadlock", queue_status="failed")
                    receipt["stop_confirmation"] = adapter._hold()
                    return
                failed = [arm for arm in ARMS if not check(arm, targets[arm], tolerances[arm], "final_held")]
                if failed:
                    receipt.update(reason="tracking_error", failed_arm=failed[0], failed_arms=failed, failed_phase="final_held")
                    receipt["stop_confirmation"] = adapter._hold()
                else:
                    receipt.update(status="completed", reason=None, queue_status="completed")
                return
            if receipt["executed_steps"] >= cap:
                arm = next(iter(active))
                fail("motion_duration_limit", arm, active[arm]["stage"])
                return
            row, context = [], {}
            for arm in ARMS:
                offset, state = (0 if arm == "left" else 7), active.get(arm)
                source = state["rows"][state["cursor"]] if state else held[arm]
                row.extend(source[offset:offset+7])
                context[arm] = dict(phase=state["phase"] if state else "hold", waypoint_index=indices[arm],
                    stage_id=state["stage"]["id"] if state else None,
                    segment_index=state["trace_index"] if state else None,
                    phase_step_index=state["cursor"] if state else None)
            trace["actions"].append(row)
            trace["row_contexts"].append(context)
            count_before = receipt["executed_steps"]
            keep_going = dispatch(row, command_phase="shared", arm_context=context)
            if receipt["executed_steps"] != count_before:
                for arm, state in active.items():
                    state["cursor"] += 1
                    state["trace"]["completed_steps"] += 1
                    results[state["stage"]["id"]]["completed_steps"] += 1
                    held[arm] = state["rows"][state["cursor"]-1]
            if not keep_going:
                return
            finished = [arm for arm, state in active.items() if state["cursor"] == len(state["rows"])]
            failed = []
            for arm in finished:
                stage = active[arm]["stage"]
                target = stage["target"] if stage["kind"] == "move" else targets[arm]
                tolerance = stage.get("position_tolerance_m", .005) if stage["kind"] == "move" else tolerances[arm]
                if not check(arm, target, tolerance, "motion_complete" if stage["kind"] == "move" else "after_gripper", stage):
                    failed.append(arm)
            for arm in (arm for arm in finished if arm not in failed):
                stage = active.pop(arm)["stage"]
                if stage["kind"] == "move":
                    targets[arm], tolerances[arm] = stage["target"], stage.get("position_tolerance_m", .005)
                completed.add(stage["id"])
                results[stage["id"]].update(status="completed", completed_sim_step=adapter.control_step)
                indices[arm] += 1
            if failed:
                arm = failed[0]
                fail("tracking_error", arm, active[arm]["stage"], failed_arms=failed)
                return
    finally:
        for arm in ARMS:
            for own in trace["arms"][arm]["segments"]:
                result = results[own["stage_id"]]
                # Dispatch may finish a native row, then return false or raise
                # during sampling. Its phase counters remain authoritative.
                result["completed_steps"] = own.get("completed_motion_steps", 0) + own.get("completed_gripper_steps", 0)
                own["completed_steps"] = result["completed_steps"]
                result["dispatch_attempted_steps"] = own.get("attempted_steps", 0)
                result["execution_uncertain"] = own.get("execution_uncertain", False)
                result["command_complete"] = result["completed_steps"] == own["planned_steps"]
                if result["status"] == "running":
                    result["status"] = "interrupted"
        receipt["completed_stage_ids"] = [item for item in all_ids if item in completed]
        receipt["pending_stage_ids"] = [item for item in all_ids if item not in completed]
        if receipt["queue_status"] == "running":
            receipt["queue_status"] = "stopped" if receipt["status"] == "stopped" else "failed"
        # A checkpoint can precede the final requested move. Comparisons are
        # explicitly target-scoped, not assertions that this stage was attempted.
        selected = {arm: next((stage for stage in reversed(queues[arm])
                    if stage["kind"] == "move" and stage.get("input_grasp_pose")), None) for arm in ARMS}
        if any(selected.values()):
            try:
                measured = adapter.state()
                receipt["queue_contact_feedback"] = dict(
                    target_scope="last_requested_move_stage_per_arm_not_completion_claim",
                    arms={arm: dict(stage_id=stage["id"], stage_status=results[stage["id"]]["status"],
                                    feedback=contact_feedback(arm, stage, measured))
                          for arm, stage in selected.items() if stage})
            except Exception as error:
                receipt["queue_contact_feedback"] = dict(available=False,
                    reason="terminal_contact_feedback_unavailable", error_type=type(error).__name__)
