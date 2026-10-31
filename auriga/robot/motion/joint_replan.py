"""Conservative selection of one native plan to an equivalent revolute endpoint.

This module never executes motion, changes planner seeds, or normalizes a returned
trajectory. The native planner remains responsible for path feasibility.
"""
import math
import time

import numpy as np

MIN_REPLAN_REMAINING_S = 15.0


def nearest_equivalent(current, goal, limits, joint_types):
    current, goal, limits = (np.asarray(value, dtype=float) for value in (current, goal, limits))
    if (current.shape != (6,) or goal.shape != (6,) or limits.shape != (6, 2)
            or not np.isfinite(np.r_[current, goal, limits.ravel()]).all()
            or np.any(limits[:, 0] >= limits[:, 1]) or list(joint_types) != ["revolute"] * 6
            or np.any(goal < limits[:, 0]) or np.any(goal > limits[:, 1])):
        raise ValueError("invalid_equivalent_endpoint_inputs")
    equivalent, turns = goal.copy(), []
    for index in range(6):
        lower = math.ceil((limits[index, 0] - goal[index]) / math.tau)
        upper = math.floor((limits[index, 1] - goal[index]) / math.tau)
        # The nearest integer, clipped to the legal interval, avoids enumeration
        # proportional to the width of a robot's limits. Prefer zero on ties.
        center = (current[index] - goal[index]) / math.tau
        principal = round(-goal[index] / math.tau)
        choices = {max(lower, min(upper, k)) for k in (0, principal, math.floor(center), math.ceil(center))}
        def _turn_score(k):
            cand = goal[index] + math.tau * k
            dist = abs(cand - current[index])
            # 避免跨象限（> pi/2）的大幅旋转或基座关节从主分支越过 ±pi 落入第二圈（如 ±6.28 rad 累积缠绕）
            outside_principal = abs(cand) > math.pi + 1e-6 and (
                dist > math.pi / 2 or (index == 0 and abs(current[0]) <= math.pi + 1e-6)
            )
            return (outside_principal, dist, abs(k))
        turn = min(choices, key=_turn_score)
        equivalent[index] += math.tau * turn
        turns.append(turn)
    return equivalent, turns


def _trajectory(value, current, limits, goal=None):
    value = np.asarray(value, dtype=float)
    if value.ndim != 2 or value.shape[1] != 6 or len(value) < 2 or not np.isfinite(value).all():
        raise ValueError("invalid_trajectory")
    if np.any(value < limits[:, 0] - 1e-5) or np.any(value > limits[:, 1] + 1e-5):
        raise ValueError("trajectory_outside_limits")
    if np.max(np.abs(value[0] - current)) > 1e-4:
        raise ValueError("trajectory_start_mismatch")
    if goal is not None and np.max(np.abs(value[-1] - goal)) > 1e-4:
        raise ValueError("trajectory_goal_mismatch")
    return value


def _metrics(value, current, limits, resample):
    rows = np.asarray(resample(value), dtype=float)
    if rows.ndim != 2 or rows.shape[1] != 6 or not len(rows) or not np.isfinite(rows).all():
        raise ValueError("invalid_resampled_trajectory")
    if (np.any(rows < limits[:, 0] - 1e-5) or np.any(rows > limits[:, 1] + 1e-5)
            or np.max(np.abs(rows[-1] - value[-1])) > 1e-4):
        raise ValueError("invalid_resampled_limits_or_endpoint")
    deltas = np.abs(np.diff(np.vstack([current, rows]), axis=0))
    return {"resampled_rows": len(rows), "total_joint_travel_rad": float(np.abs(np.diff(value, axis=0)).sum()),
            "first_dispatch_delta_rad": float(deltas[0].max()),
            "max_dispatch_delta_rad": float(deltas.max())}


def _pose_valid(pose_error, q, diagnostic, name, position_tolerance_m):
    error = np.asarray(pose_error(q), dtype=float)
    if error.shape != (2,) or not np.isfinite(error).all() or np.any(error < 0):
        raise ValueError("invalid_pose_error")
    diagnostic[name] = {"position_error_m": float(error[0]), "orientation_error_rad": float(error[1])}
    if error[0] > position_tolerance_m or error[1] > 0.05:
        raise ValueError("original_pose_target_mismatch")


def select_joint_replan(*, original, current, limits, joint_types, transforms, pose_error,
                       resample, plan_joint, deadline, diagnostic, clock=time.monotonic,
                       position_tolerance_m=0.005):
    """Return original or one validated native candidate; native exceptions propagate."""
    started = clock()
    diagnostic.update(stage="joint_replan", selected=False, attempted=False,
                      minimum_remaining_s=MIN_REPLAN_REMAINING_S)
    try:
        current, limits = np.asarray(current, dtype=float), np.asarray(limits, dtype=float)
        baseline = _trajectory(original, current, limits)
        equivalent, turns = nearest_equivalent(current, baseline[-1], limits, joint_types)
        before, after = float(np.linalg.norm(baseline[-1] - current)), float(np.linalg.norm(equivalent - current))
        unwinds_principal = any(
            abs(baseline[-1][i]) > math.pi + 1e-6 and abs(equivalent[i]) <= math.pi + 1e-6
            for i in range(6)
        )
        diagnostic.update(turn_shifts=turns, original_endpoint_distance_rad=before,
                          equivalent_endpoint_distance_rad=after, equivalent_goal_q_rad=equivalent.tolist(),
                          unwinds_principal=unwinds_principal)
        if not any(turns) or (after >= before - 1e-6 and not unwinds_principal):
            diagnostic["reason"] = "no_strictly_closer_legal_endpoint"
            return original
        old, new = transforms(baseline[-1]), transforms(equivalent)
        if not old or old.keys() != new.keys():
            raise ValueError("invalid_fk_links")
        differences = []
        for name in old:
            first, second = np.asarray(old[name]), np.asarray(new[name])
            if first.shape != (4, 4) or second.shape != (4, 4) or not np.isfinite([first, second]).all():
                raise ValueError("invalid_fk_transform")
            differences.append(float(np.max(np.abs(first - second))))
        diagnostic["all_link_fk_max_abs_error"] = max(differences)
        if max(differences) > 1e-9:
            raise ValueError("endpoint_not_fk_equivalent")
        _pose_valid(pose_error, baseline[-1], diagnostic, "baseline_pose", position_tolerance_m)
        _pose_valid(pose_error, equivalent, diagnostic, "equivalent_pose", position_tolerance_m)
        diagnostic["baseline"] = _metrics(baseline, current, limits, resample)
        remaining = float(deadline - clock())
        diagnostic["remaining_before_replan_s"] = remaining
        if not math.isfinite(remaining) or remaining < MIN_REPLAN_REMAINING_S:
            diagnostic["reason"] = "insufficient_action_deadline"
            return original
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        diagnostic.update(reason="baseline_validation_failed", validation_error=str(error))
        return original
    # Deliberately outside the validation exception handlers. An exception from
    # native planning can indicate a damaged CUDA context; never dispatch fallback.
    diagnostic["attempted"] = True
    try:
        result = plan_joint(current.copy(), equivalent.copy())
    except BaseException as error:
        diagnostic.update(reason="native_exception", exception_type=type(error).__name__, elapsed_wall_s=clock() - started)
        raise
    diagnostic["elapsed_wall_s"] = clock() - started
    if clock() > deadline:
        diagnostic["reason"] = "action_deadline_exceeded"
        return original
    try:
        if not isinstance(result, dict) or result.get("status") != "Success":
            diagnostic["reason"] = "native_replan_failed"
            return original
        candidate = _trajectory(result.get("position"), current, limits, equivalent)
        _pose_valid(pose_error, candidate[-1], diagnostic, "candidate_pose", position_tolerance_m)
        diagnostic["candidate"] = _metrics(candidate, current, limits, resample)
        old, new = diagnostic["baseline"], diagnostic["candidate"]
        if after < before - 1e-6:
            improved = (new["resampled_rows"] < old["resampled_rows"]
                        and new["total_joint_travel_rad"] < old["total_joint_travel_rad"] - 1e-6)
        else:
            improved = (new["total_joint_travel_rad"] <= old["total_joint_travel_rad"] + 0.65
                        and new["total_joint_travel_rad"] <= after + 0.65
                        and new["resampled_rows"] <= old["resampled_rows"] + 15)
        if not improved:
            diagnostic["reason"] = "no_strict_path_improvement"
            return original
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        diagnostic.update(reason="candidate_validation_failed", validation_error=str(error))
        return original
    diagnostic.update(selected=True, reason="validated_shorter_native_plan")
    return candidate


def replan_from_ik_candidate(*, ik_q, current, limits, joint_types, transforms, pose_error,
                             resample, plan_joint, deadline, diagnostic, clock=time.monotonic,
                             position_tolerance_m=0.005, allow_zero_turns=False):
    """当 native plan_path 因 ±2π 缠绕的 IK 种子在 trajopt 阶段自碰撞失败，或选入反肩翻转分支时，规划到优选关节终点。"""
    started = clock()
    diagnostic.update(stage="joint_replan_from_ik", selected=False, attempted=False,
                      minimum_remaining_s=MIN_REPLAN_REMAINING_S)
    try:
        current, ik_q, limits = (np.asarray(v, dtype=float) for v in (current, ik_q, limits))
        equivalent, turns = nearest_equivalent(current, ik_q, limits, joint_types)
        before, after = float(np.linalg.norm(ik_q - current)), float(np.linalg.norm(equivalent - current))
        unwinds_principal = any(
            abs(ik_q[i]) > math.pi + 1e-6 and abs(equivalent[i]) <= math.pi + 1e-6
            for i in range(6)
        )
        diagnostic.update(turn_shifts=turns, original_endpoint_distance_rad=before,
                          equivalent_endpoint_distance_rad=after, equivalent_goal_q_rad=equivalent.tolist(),
                          unwinds_principal=unwinds_principal)
        if not allow_zero_turns and (not any(turns) or (after >= before - 1e-6 and not unwinds_principal)):
            diagnostic["reason"] = "no_strictly_closer_legal_endpoint"
            return None
        old, new = transforms(ik_q), transforms(equivalent)
        if not old or old.keys() != new.keys():
            raise ValueError("invalid_fk_links")
        differences = [float(np.max(np.abs(np.asarray(old[n]) - np.asarray(new[n])))) for n in old]
        diagnostic["all_link_fk_max_abs_error"] = max(differences)
        if max(differences) > 1e-9:
            raise ValueError("endpoint_not_fk_equivalent")
        _pose_valid(pose_error, ik_q, diagnostic, "ik_pose", position_tolerance_m)
        _pose_valid(pose_error, equivalent, diagnostic, "equivalent_pose", position_tolerance_m)
        remaining = float(deadline - clock())
        diagnostic["remaining_before_replan_s"] = remaining
        if not math.isfinite(remaining) or remaining < MIN_REPLAN_REMAINING_S:
            diagnostic["reason"] = "insufficient_action_deadline"
            return None
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        diagnostic.update(reason="ik_validation_failed", validation_error=str(error))
        return None
    diagnostic["attempted"] = True
    try:
        result = plan_joint(current.copy(), equivalent.copy())
    except BaseException as error:
        diagnostic.update(reason="native_exception", exception_type=type(error).__name__, elapsed_wall_s=clock() - started)
        raise
    diagnostic["elapsed_wall_s"] = clock() - started
    if clock() > deadline:
        diagnostic["reason"] = "action_deadline_exceeded"
        return None
    try:
        if not isinstance(result, dict) or result.get("status") != "Success":
            diagnostic["reason"] = "native_replan_failed"
            return None
        candidate = _trajectory(result.get("position"), current, limits, equivalent)
        _pose_valid(pose_error, candidate[-1], diagnostic, "candidate_pose", position_tolerance_m)
        diagnostic["candidate"] = _metrics(candidate, current, limits, resample)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        diagnostic.update(reason="candidate_validation_failed", validation_error=str(error))
        return None
    diagnostic.update(selected=True, reason="rescued_winding_ik_via_joint_plan")
    return candidate
