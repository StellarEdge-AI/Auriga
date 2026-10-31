"""Bounded observations of a failed candidate in the native planner model.

These geometric witnesses describe sampled candidate configurations, not the
cause of a failed search, an executed trajectory, or contacts in the simulator.
The adapter performs FK only; it never solves, changes collision settings, reads
task objects, scene geometry or world transforms, or advances physics. Geometry
uses only the configured robot sphere/pair model, including self-collision
padding and ignored pairs. Robot model and solver data are reproducible in a
real deployment; simulator environment geometry has no permitted source here.
"""

from __future__ import annotations

import numpy as np


SCOPE = "configured robot model joint limits and self-collision only; no environment geometry, simulator transforms, task objects or other arm"


def _array(value):
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    return np.asarray(value)


def _finite(value, shape=None):
    array = np.asarray(value, dtype=float)
    if (shape is not None and array.shape != shape) or not np.isfinite(array).all():
        raise ValueError("invalid_shape_or_nonfinite")
    return array


def _dynamic_findings(q, names, joint_derivatives, derivative_limits):
    """Compare native derivative snapshots independently; never derive them from q."""
    result = dict(dynamic_joint_limit_violations=[], dynamic_diagnostic_unavailable={},
                  omitted_dynamic_witness_count=0)
    derivatives = joint_derivatives if isinstance(joint_derivatives, dict) else {}
    limits_by_type = derivative_limits if isinstance(derivative_limits, dict) else {}
    for kind, units in (("velocity", "rad/s"), ("acceleration", "rad/s^2"), ("jerk", "rad/s^3")):
        if kind not in derivatives or derivatives[kind] is None:
            result["dynamic_diagnostic_unavailable"][kind] = "native_derivative_not_returned"
            continue
        if kind not in limits_by_type or limits_by_type[kind] is None:
            result["dynamic_diagnostic_unavailable"][kind] = "native_derivative_limits_unavailable"
            continue
        try:
            values = _finite(derivatives[kind], q.shape)
            limits = _finite(limits_by_type[kind], (len(names), 2))
            if (limits[:, 0] >= limits[:, 1]).any():
                raise ValueError("invalid_derivative_limits")
            violations = []
            for joint, name in enumerate(names):
                margin = np.minimum(values[:, joint] - limits[joint, 0], limits[joint, 1] - values[:, joint])
                sample = int(np.argmin(margin))
                if margin[sample] < -1e-6:
                    value = float(values[sample, joint])
                    violations.append(dict(derivative=kind, joint_name=name,
                        trajectory_sample_index=sample, value=value,
                        lower=float(limits[joint, 0]), upper=float(limits[joint, 1]),
                        units=units, exceed=float(-margin[sample]),
                        violated_bound="lower" if value < limits[joint, 0] else "upper"))
            result["dynamic_joint_limit_violations"].extend(
                sorted(violations, key=lambda item: -item["exceed"]))
        except (ValueError, TypeError, OverflowError):
            result["dynamic_diagnostic_unavailable"][kind] = "invalid_native_derivative_or_limits"
    result["dynamic_limit_source"] = "native planner derivative limits; native candidate derivatives with native timing; no finite differences"
    return result


def diagnose_candidate_geometry(q_rad, joint_names, *, joint_limits=None,
                                spheres=None, sphere_link_names=None,
                                self_collision_pairs=None, self_collision_padding=None,
                                joint_derivatives=None, derivative_limits=None,
                                max_witnesses=3, near_limit_rad=0.05):
    """Diagnose CPU snapshots; q is [dof] or [samples,dof], limits [dof,2].

    Sphere centers use the planner base frame; no simulator/world transform is read.
    Positive penetration is sphere-model overlap in meters, not optimizer cost.
    Keep every joint's worst constraint violation. The worker includes a short
    collision preview; the host validates and delivers the complete witness list.
    Empty lists prove only that these sampled geometric checks found no overlap.
    """
    if not isinstance(max_witnesses, int) or not 1 <= max_witnesses <= 10:
        raise ValueError("invalid_witness_bound")
    if not np.isfinite(near_limit_rad) or near_limit_rad < 0:
        raise ValueError("invalid_near_limit_threshold")
    q = _finite(q_rad)
    if q.ndim == 1:
        q = q[None, :]
    names = list(joint_names)
    if q.ndim != 2 or not len(q) or q.shape[1] != len(names) or len(set(names)) != len(names):
        raise ValueError("invalid_candidate_joint_mapping")
    result = dict(available=True, candidate_only=True, sampled_only=True,
                  frame_id="planner_base", scope=SCOPE, samples_checked=len(q),
                  interpretation="candidate observations; unique failure cause unknown; absence of sampled overlap does not prove path feasibility",
                  collision_model_witnesses=[], joint_limit_violations=[], joints_near_limits=[],
                  environment_geometry_unavailable_reason="no_real_sensor_or_calibrated_environment_source",
                  world_frame_transform_unavailable_reason="no_real_calibration_source")
    result.update(_dynamic_findings(q, names, joint_derivatives, derivative_limits))
    violations, near = [], []
    if joint_limits is not None:
        limits = _finite(joint_limits, (len(names), 2))
        if (limits[:, 0] >= limits[:, 1]).any():
            raise ValueError("invalid_joint_limits")
        for joint, name in enumerate(names):
            margin = np.minimum(q[:, joint] - limits[joint, 0], limits[joint, 1] - q[:, joint])
            sample = int(np.argmin(margin))
            item = dict(joint_name=name, trajectory_sample_index=sample,
                        position_rad=float(q[sample, joint]), lower_rad=float(limits[joint, 0]),
                        upper_rad=float(limits[joint, 1]), margin_rad=float(margin[sample]),
                        nearest_bound="lower" if q[sample, joint] - limits[joint, 0] <= limits[joint, 1] - q[sample, joint] else "upper")
            if margin[sample] < -1e-6:
                violations.append(item)
            elif margin[sample] <= near_limit_rad:
                near.append(item)
        result["joint_limit_source"] = "native planner position limits"
        result["joint_limit_violations"] = sorted(violations, key=lambda i: i["margin_rad"])
        result["joints_near_limits"] = sorted(near, key=lambda i: i["margin_rad"])
    else:
        result["joint_limits_unavailable"] = True
    self_hits = []
    self_hit_count = 0
    if spheres is None:
        result["collision_geometry_unavailable"] = True
        return result
    spheres = _finite(spheres)
    if spheres.ndim != 3 or spheres.shape[0] != len(q) or spheres.shape[2] != 4:
        raise ValueError("invalid_sphere_shape")
    links = list(sphere_link_names or [])
    sphere_count = spheres.shape[1]
    if len(links) != sphere_count or any(not isinstance(name, str) for name in links):
        raise ValueError("invalid_sphere_link_mapping")
    if self_collision_pairs is not None:
        raw_pairs = np.asarray(self_collision_pairs)
        if raw_pairs.ndim != 2 or raw_pairs.shape[1] != 2 or not np.isfinite(raw_pairs).all():
            raise ValueError("invalid_self_collision_pairs")
        pairs = raw_pairs.astype(int)
        if not np.array_equal(raw_pairs, pairs) or (pairs < 0).any() or (pairs >= sphere_count).any():
            raise ValueError("invalid_self_collision_pairs")
        padding = _finite(self_collision_padding, (sphere_count,))
        # Evaluate one sample at a time to bound temporary memory by pair count.
        for sample, state in enumerate(spheres):
            radii = state[pairs, 3] + padding[pairs]
            depths = radii.sum(axis=1) - np.linalg.norm(state[pairs[:, 0], :3] - state[pairs[:, 1], :3], axis=1)
            hits = np.flatnonzero((radii >= 0).all(axis=1) & (depths > 1e-6))
            self_hit_count += len(hits)
            for pair_index in hits:
                i, j = pairs[pair_index]
                self_hits.append(dict(kind="self_collision", trajectory_sample_index=sample,
                    link_names=[links[i], links[j]], sphere_indices=[int(i), int(j)],
                    sphere_centers_m=state[[i, j], :3].tolist(),
                    effective_sphere_radii_m=radii[pair_index].tolist(), penetration_m=float(depths[pair_index])))
    else:
        result["self_collision_pairs_unavailable"] = True
    self_hits.sort(key=lambda item: -item["penetration_m"])
    result["collision_model_witnesses"] = self_hits[:max_witnesses]
    if len(self_hits) > max_witnesses:
        result["_full_collision_model_witnesses"] = self_hits
    result["omitted_witness_count"] = max(0, self_hit_count-max_witnesses)
    return result


def capture_candidate_witness(planner, q_rad, joint_names, *, joint_derivatives=None, max_witnesses=3):
    """Best-effort v2 cuRobo adapter; diagnostics never replace the planner result."""
    fallback = dict(available=False, candidate_only=True, sampled_only=True,
                    frame_id="planner_base", scope=SCOPE,
                    interpretation="diagnostics unavailable; unique failure cause unknown",
                    environment_geometry_unavailable_reason="no_real_sensor_or_calibrated_environment_source",
                    world_frame_transform_unavailable_reason="no_real_calibration_source")
    try:
        motion = planner.motion_planner
        names = list(joint_names)
        if names != list(motion.joint_names):
            raise ValueError("native_joint_names_mismatch")
        q = _finite(q_rad)
        if q.ndim == 1:
            q = q[None, :]
        if q.ndim != 2 or not len(q) or q.shape[1] != len(names):
            raise ValueError("invalid_candidate_shape")
        kinematics = motion.kinematics
        cfg = kinematics.config
        limits = kinematics.get_joint_limits()
        if list(limits.joint_names) != names:
            raise ValueError("native_limit_joint_names_mismatch")
        derivative_limits = {}
        for field in ("velocity", "acceleration", "jerk"):
            try:
                derivative_limits[field] = _array(getattr(limits, field)).T
            except Exception:
                derivative_limits[field] = None
        result = diagnose_candidate_geometry(q, names, joint_limits=_array(limits.position).T,
                                             joint_derivatives=joint_derivatives, derivative_limits=derivative_limits,
                                             max_witnesses=max_witnesses)
        # Keep trustworthy joint observations even if a version's FK/map differs.
        try:
            import torch
            from curobo.types import JointState
            device_cfg = planner.device_cfg
            position = torch.as_tensor(q, device=device_cfg.device, dtype=device_cfg.dtype)
            with torch.no_grad():
                state = motion.compute_kinematics(JointState.from_position(position, joint_names=names))
            sphere_data = _array(state.robot_spheres)
            if sphere_data.ndim == 4 and sphere_data.shape[1] == 1:
                sphere_data = sphere_data[:, 0]
            tensor_cfg = cfg.kinematics_config
            indices = _array(tensor_cfg.link_sphere_idx_map).reshape(-1)
            inverse = {index: name for name, index in tensor_cfg.link_name_to_idx_map.items()}
            links = [inverse[int(index)] for index in indices]
            self_cfg = getattr(cfg, "self_collision_config", None)
            result = diagnose_candidate_geometry(q, names, joint_limits=_array(limits.position).T,
                spheres=sphere_data, sphere_link_names=links,
                self_collision_pairs=None if self_cfg is None else _array(self_cfg.collision_pairs),
                self_collision_padding=None if self_cfg is None else _array(self_cfg.sphere_padding),
                joint_derivatives=joint_derivatives,
                derivative_limits=derivative_limits, max_witnesses=max_witnesses)
            try:
                tools = state.tool_poses
                positions, quaternions = _array(tools.position), _array(tools.quaternion)
                positions = positions.reshape(len(q), len(tools.tool_frames), 3)
                quaternions = quaternions.reshape(len(q), len(tools.tool_frames), 4)
                _finite(positions)
                _finite(quaternions)
                result["last_sample_tool_poses"] = [dict(tool_frame=name,
                    position_m=positions[-1, i].tolist(), quaternion_wxyz=quaternions[-1, i].tolist())
                    for i, name in enumerate(tools.tool_frames)]
            except (AttributeError, ValueError, TypeError):
                result["tool_poses_unavailable"] = True
        except Exception as error:
            result["collision_diagnostic_error"] = type(error).__name__
        return result
    except Exception as error:
        fallback["diagnostic_error"] = type(error).__name__
        return fallback
