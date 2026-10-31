"""Bounded actor feedback from requested poses and actual native solver results.

Never promote a rejected numerical candidate to a measured robot state, or a
constraint witness to proof that no alternative configuration/path exists.
"""

from copy import deepcopy
import math

import numpy as np


CANDIDATE_PUBLIC_FIELDS = frozenset({
    "seed_index", "selection", "selection_score", "selection_normalization",
    "solver_success", "solver_feasible", "position_error_m", "orientation_error_rad",
    "position_tolerance_m", "orientation_tolerance_rad", "joint_names",
    "joint_mapping_unavailable", "derivatives_unavailable",
})


def archive_collision_witnesses(store, result, observation_profile):
    """Validate and deliver complete witnesses, retaining the existing artifact lookup."""
    from ...core.contracts import canonical
    from ...core.observation_policy import reject_privileged

    feedback = result.get("failure_feedback") or {}
    for candidate in feedback.get("candidates", []):
        witness = candidate.get("constraint_witness") or {}
        complete = witness.pop("_full_collision_model_witnesses", None)
        if complete is None:
            continue
        witness["collision_model_witness_count"] = len(complete)
        details = dict(candidate_only=True, sampled_only=True,
            frame_id=witness.get("frame_id"), scope=witness.get("scope"),
            samples_checked=witness.get("samples_checked"), arm=feedback.get("arm"),
            failed_stage_id=result.get("failed_stage_id"), solver_stage=candidate.get("stage"),
            seed_index=candidate.get("seed_index"), stage_call_index=candidate.get("stage_call_index"),
            collision_model_witnesses=complete)
        try:
            reject_privileged(details, observation_profile)
            encoded = canonical(details).encode()
            witness["omitted_witness_count"] = max(0, witness.get("omitted_witness_count", 0)
                - (len(complete) - len(witness.get("collision_model_witnesses", []))))
            witness["collision_model_witnesses"] = complete
            witness["collision_model_witnesses_ref"] = store.put_artifact(
                encoded, "planner_collision_witnesses", "application/json", access_scope="actor")
        except Exception as error:
            # Optional evidence storage cannot replace the known planning/execution result.
            try:
                # The worker's short list can share tainted entries with the full list.
                reject_privileged(witness.get("collision_model_witnesses"), observation_profile)
                canonical(witness.get("collision_model_witnesses"))
            except (TypeError, ValueError, OverflowError):
                witness.pop("collision_model_witnesses", None)
            witness["collision_model_witnesses_unavailable"] = dict(
                reason="full_witness_archive_failed", error_type=type(error).__name__)


def host_array(value):
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return np.asarray(value)


def _numbers(value):
    values = host_array(value).astype(float)
    if not values.size or not np.isfinite(values).all():
        raise ValueError("invalid_solver_numbers")
    return values


def solver_snapshot(result, stage, active_joint_names=None):
    """Copy one seed while CUDA-graph result buffers still belong to this call.

    Seed selection minimizes the worst normalized endpoint residual when both
    tolerances are available. Only single-batch, aligned native shapes qualify;
    absent/mismatched data stays unavailable, never guessed from another seed.
    """
    snapshot = {}
    for source, field in (("position_error", "position_error_m"), ("rotation_error", "orientation_error_rad")):
        value = getattr(result, source, None)
        if value is not None:
            array = _numbers(value)
            snapshot["min_" + field] = float(array.min())
    feasible = getattr(result, "feasible", None)
    if feasible is not None:
        array = host_array(feasible)
        if array.size and array.dtype.kind == "b":
            snapshot["feasible_seed_count"] = int(array.sum())
            snapshot["feasibility_checked_seed_count"] = int(array.size)
    if stage not in ("ik", "trajopt"):
        return snapshot
    success = host_array(getattr(result, "success", None))
    if success.ndim != 2 or success.shape[0] != 1 or not success.size or success.dtype.kind != "b":
        return snapshot
    errors = []
    candidate_fields = {}
    normalization = {}
    for source, field, tolerance_source in (("position_error", "position_error_m", "position_tolerance"),
                                            ("rotation_error", "orientation_error_rad", "orientation_tolerance")):
        value = getattr(result, source, None)
        if value is None:
            continue
        array = _numbers(value)
        if array.shape != success.shape or np.any(array < 0):
            return snapshot
        tolerance = getattr(result, tolerance_source, None)
        if tolerance is not None and math.isfinite(float(tolerance)) and float(tolerance) > 0:
            tolerance = float(tolerance)
            candidate_fields[field.replace("error", "tolerance")] = tolerance
            errors.append(array[0] / tolerance)
            normalization[field] = dict(value=tolerance, source="native_solver_tolerance")
        else:
            # Explicit heuristic only when native convergence thresholds are absent.
            reference = .005 if source == "position_error" else .1
            errors.append(array[0] / reference)
            normalization[field] = dict(value=reference, source="diagnostic_selection_reference_not_solver_tolerance")
        candidate_fields[field] = array[0]
    if not errors:
        return snapshot
    score = np.max(errors, axis=0)
    seed = int(np.argmin(score))
    candidate = dict(seed_index=seed, selection="lowest_max_normalized_endpoint_residual_among_returned_seeds",
                     selection_score=float(score[seed]), selection_normalization=normalization,
                     solver_success=bool(success[0, seed]))
    for field, value in candidate_fields.items():
        candidate[field] = float(value[seed]) if isinstance(value, np.ndarray) else value
    if feasible is not None:
        array = host_array(feasible)
        if array.shape == success.shape and array.dtype.kind == "b":
            candidate["solver_feasible"] = bool(array[0, seed])
    js = getattr(result, "js_solution", None)
    names = getattr(js, "joint_names", None)
    positions = getattr(js, "position", None)
    if positions is not None and names is not None:
        positions = _numbers(positions)
        expected_ndim = 3 if stage == "ik" else 4
        if (positions.ndim == expected_ndim and positions.shape[:2] == success.shape
                and positions.shape[-1] == len(names) and len(set(names)) == len(names)):
            selected = positions[0, seed]
            # Native full-DOF JointState can include locked prismatic jaws and
            # reorder its columns. Only named active arm joints are radians.
            if active_joint_names is not None:
                active = list(active_joint_names)
                if len(set(active)) != len(active) or not active or any(name not in names for name in active):
                    candidate["joint_mapping_unavailable"] = "active_joint_names_not_unique_or_missing_from_native_joint_state"
                    snapshot["candidate"] = candidate
                    return snapshot
                columns = [list(names).index(name) for name in active]
                selected = selected[..., columns]
                names = active
            else:
                columns = list(range(len(names)))
            if selected.ndim == 1:
                selected = selected[None, :]
            if len(selected) <= 512:
                candidate.update(joint_names=list(names), q_rad=selected.tolist())
                if stage == "ik" and positions.ndim == 4 and positions.shape[2] == 1:
                    valid_mask = success[0].copy()
                    if feasible is not None:
                        f_arr = host_array(feasible)
                        if f_arr.shape == success.shape and f_arr.dtype.kind == "b":
                            valid_mask &= f_arr[0]
                    valid_indices = [int(i) for i in np.flatnonzero(valid_mask) if float(score[i]) <= 1.0][:12]
                    if valid_indices:
                        candidate["feasible_q_rads"] = [
                            positions[0, i, 0, columns].astype(float).tolist() for i in valid_indices
                        ]
                derivatives = {}
                for field in ("velocity", "acceleration", "jerk"):
                    value = getattr(js, field, None)
                    if value is None:
                        continue
                    try:
                        array = _numbers(value)
                        if array.shape != positions.shape:
                            raise ValueError("derivative_shape_mismatch")
                        derivative = array[0, seed][..., columns]
                        if derivative.ndim == 1:
                            derivative = derivative[None, :]
                        derivatives[field] = derivative.tolist()
                    except (ValueError, TypeError, OverflowError):
                        candidate.setdefault("derivatives_unavailable", []).append(field)
                if derivatives:
                    candidate["joint_derivatives"] = derivatives
    snapshot["candidate"] = candidate
    return snapshot


def failure_feedback(action, arm, waypoint_index, diagnostics, collision_scope, witness=None):
    """Whitelist public fields; raw planner debug and scene truth stay private."""
    points = action["path"]["waypoints"]
    target = points[waypoint_index][arm]
    relevant = [item for item in diagnostics if item.get("waypoint_index") == waypoint_index]
    attempts = [item for item in relevant if item.get("stage") == "ik"]
    failed_ik = any(item.get("stage") == "plan_pose" and "IK_FAIL" in str(item.get("status"))
                    for item in relevant) or bool(attempts) and all(
        item.get("success") is not None and np.asarray(item["success"]).size
        and not np.asarray(item["success"]).any() for item in attempts)
    stage = "inverse_kinematics" if failed_ik else "path_planning"
    feedback = dict(schema_version="1", arm=arm, waypoint_index=waypoint_index,
                    waypoint_role="final_target" if waypoint_index == len(points) - 1 else "intermediate",
                    waypoint_count=len(points), frame_id=action.get("frame_id", "env_origin"),
                    tcp_config_id=action.get("tcp_config_id"),
                    requested_ee_pose=deepcopy(target), failure_stage=stage,
                    cause_status="undetermined", collision_scope=collision_scope,
                    interpretation="No executable plan was found for this segment. Candidate findings do not prove a unique cause or global unreachability.",
                    solver_summary=[], candidates=[],
                    information_sources=dict(requested_targets="caller_request",
                        solver_candidates="motion_planner_internal_result_not_measurement",
                        joint_limits="configured_robot_model",
                        self_collision="configured_robot_model_fk",
                        environment_collision="unavailable_without_real_sensor_or_calibrated_source",
                        world_transform="unavailable_without_real_calibration"))
    grasp = action.get("input_grasp_pose")
    if grasp is not None:
        if arm in grasp:
            grasp = grasp[arm]
        if waypoint_index == len(points) - 1:
            pose = dict(grasp_position_m=grasp["position_m"], angles_deg=grasp["angles_deg"])
            pose.update(grasp.get("contact_selection", {}))
        else:
            pose = grasp["via_grasp_poses"][waypoint_index]
        feedback["requested_grasp_pose"] = deepcopy(pose)
        contact_frames = grasp.get("waypoint_contact_frames")
        contact_frame = (contact_frames[waypoint_index] if isinstance(contact_frames, list)
                         and waypoint_index < len(contact_frames) else grasp)
        feedback["grasp_to_ee_offset_m"] = contact_frame["offset_m"]
        if "contact_region" in contact_frame or "contact_depth_m" in contact_frame:
            feedback["contact_frame"] = {key: deepcopy(contact_frame[key])
                for key in ("contact_region", "contact_depth_m", "offset_m") if key in contact_frame}
    for name in ("ik", "trajopt", "graph_seed", "plan_pose"):
        items = [item for item in relevant if item.get("stage") == name]
        if not items:
            continue
        summary = dict(stage=name, calls=len(items))
        successes = [np.asarray(item["success"]) for item in items if item.get("success") is not None]
        summary["successful_seed_count"] = sum(int(value.sum()) for value in successes)
        summary["checked_seed_count"] = sum(int(value.size) for value in successes)
        statuses = list(dict.fromkeys(item["status"] for item in items if item.get("status") is not None))
        if statuses:
            summary["native_statuses"] = statuses
        for field in ("min_position_error_m", "min_orientation_error_rad"):
            values = [item[field] for item in items if field in item]
            if values:
                summary[field] = min(values)
        for field in ("feasible_seed_count", "feasibility_checked_seed_count"):
            values = [item[field] for item in items if field in item]
            if values:
                summary[field] = sum(values)
        feedback["solver_summary"].append(summary)
        candidates = [(i, item["candidate"]) for i, item in enumerate(items) if "candidate" in item]
        if name not in ("ik", "trajopt") or not candidates:
            continue
        index, candidate = min(candidates, key=lambda pair: pair[1]["selection_score"])
        public = {key: deepcopy(value) for key, value in candidate.items()
                  if key in CANDIDATE_PUBLIC_FIELDS}
        public.update(stage=name, stage_call_index=index, source_kind="solver_candidate_not_executed",
                      candidate_only=True)
        if "q_rad" in candidate:
            public["endpoint_q_rad"] = deepcopy(candidate["q_rad"][-1])
            public["trajectory_sample_count"] = len(candidate["q_rad"])
            if witness is not None:
                try:
                    if "joint_derivatives" in candidate:
                        public["constraint_witness"] = witness(candidate["q_rad"], candidate["joint_names"],
                                                               candidate["joint_derivatives"])
                    else:
                        public["constraint_witness"] = witness(candidate["q_rad"], candidate["joint_names"])
                except Exception as error:
                    public["constraint_witness"] = dict(available=False, reason="diagnostic_unavailable",
                                                         error_type=type(error).__name__)
        feedback["candidates"].append(public)
    feedback["suggested_adjustments"] = [
        "Revise the failed intermediate pose first; the final target was not tested by this segment."
        if feedback["waypoint_role"] == "intermediate" else
        "Consider a different target orientation/position or explicit intermediate poses based on current observations.",
        "Use candidate limit/collision findings as evidence for changing posture or clearance; no exact Cartesian correction is certified."]
    if not feedback["candidates"]:
        feedback["detail_unavailable_reason"] = "native_solver_did_not_return_aligned_candidate_data"
    return feedback
