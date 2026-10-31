"""Post-action feedback from stored actor traces, public FK and start-frame RGB.

This module has no robot/backend access. Missing commands stay missing; local
independent-arm candidates never acquire an invented shared future timeline.
"""
from copy import deepcopy
import json

import numpy as np

from ...core.contracts import IDENTITY, canonical
from ...robot.model.geometry import array, fk
from ...core.observation_policy import reject_privileged
from ...robot.motion.trajectory_evidence import ARMS, _bases


def _read(harness, reference, kind=None):
    metadata, raw = harness.store.resolve_artifact(reference, allowed_scopes=('actor',))
    if metadata['mime_type'] != 'application/json' or kind and metadata['kind'] != kind:
        raise ValueError('trajectory_feedback_requires_matching_actor_json')
    value = json.loads(raw)
    reject_privileged(value, harness.observation_profile)
    return value


def _measured_arms(state, names):
    output = {}
    for arm in ARMS:
        measured = state['arms'][arm]
        if measured.get('source_kind') != 'measurement' or measured['joint_names'] != names[arm]:
            raise ValueError('trajectory_feedback_requires_consistent_named_measurements')
        output[arm] = dict(q_rad=array(measured['q_rad'], (6,)).tolist(), phase='measured')
        if 'qdot_rad_s' in measured:
            output[arm]['qdot_rad_s'] = array(measured['qdot_rad_s'], (6,)).tolist()
        pose = measured.get('ee_pose', {})
        if pose.get('source_kind') == 'measurement':
            array(pose['position_m'], (3,))
            array(pose['quaternion_xyzw'], (4,))
            output[arm]['measured_ee_pose'] = deepcopy(pose)
        jaw = measured.get('gripper', {})
        for key, source, target in (('goal_normalized', 'goal_source_kind', 'jaw_goal_normalized'),
                                    ('command_normalized', 'command_source_kind', 'jaw_command_normalized')):
            if jaw.get(source) == 'command' and key in jaw:
                output[arm][target] = float(array(jaw[key], ()))
        if jaw.get('position_source_kind') == 'measurement':
            output[arm]['measured_jaw'] = dict(joint_names=list(jaw['joint_names']),
                position_m=array(jaw['position_m'], (len(jaw['joint_names']),)).tolist())
    return output


def _measured_samples(before, trace, names):
    records = [dict(sim_step=before['sim_step'], state=before)] + list(trace or [])
    samples = []
    for record in records:
        state, step = record['state'], record['sim_step']
        if (type(step) is not int or state.get('sim_step') != step
                or state.get('step_domain') != before.get('step_domain')):
            raise ValueError('trajectory_feedback_unsynchronized_measurement')
        arms = _measured_arms(state, names)
        if samples and step == samples[-1]['sim_step']:
            if arms != samples[-1]['arms']:
                raise ValueError('trajectory_feedback_conflicting_same_step_measurement')
            continue
        if samples and step < samples[-1]['sim_step']:
            raise ValueError('trajectory_feedback_nonmonotonic_measurement')
        samples.append(dict(native_step=len(samples), sim_step=step, arms=arms))
    return samples


def _angular_displacement(previous, current):
    """Shortest rotation vector in the declared world frame, not Euler rates."""
    first, second = array(previous, (4,)), array(current, (4,))
    norms = np.linalg.norm(first), np.linalg.norm(second)
    if min(norms) < 1e-12:
        raise ValueError('trajectory_feedback_invalid_measured_orientation')
    first, second = first / norms[0], second / norms[1]
    # q_current * inverse(q_previous), for xyzw quaternions.
    vector = first[3] * second[:3] - second[3] * first[:3] - np.cross(second[:3], first[:3])
    scalar = second[3] * first[3] + np.dot(second[:3], first[:3])
    if scalar < 0:
        vector, scalar = -vector, -scalar
    length = np.linalg.norm(vector)
    return np.zeros(3) if length < 1e-12 else vector * (2 * np.arctan2(length, scalar) / length)


def _add_measured_kinematics(data, observation):
    """Derive bounded-interval motion from original physics-step timestamps.

    The direct qdot and EE measurements remain separate. No native-row index is
    interpreted as a physics tick, and missing time calibration stays unknown.
    """
    data['measurement_timing'] = dict(status='unavailable', reason='physics_step_duration_unavailable')
    if not isinstance(observation, dict):
        return
    durations = [camera.get('control_dt_s') for camera in observation.get('cameras', [])]
    if observation.get('control_dt_s') is not None:
        durations.append(observation['control_dt_s'])
    durations = [value for value in durations if value is not None]
    if (not durations or any(type(value) not in (int, float) or not np.isfinite(value) or value <= 0
                             for value in durations) or any(value != durations[0] for value in durations)):
        return
    samples, tick_s = data['samples'], float(durations[0])
    if samples[0]['sim_step'] != observation.get('sim_step'):
        data['measurement_timing']['reason'] = 'start_observation_time_mismatch'
        return
    data['measurement_timing'] = dict(status='available', physics_dt_s=tick_s,
        step_domain=observation.get('step_domain'), source_kind='recorded_sim_step_and_capture_control_dt',
        derivatives='Finite differences over recorded boundary intervals; acceleration of interval velocities uses interval midpoints. Not high-frequency extrema or contact evidence.')
    initial_step = samples[0]['sim_step']
    for sample in samples:
        sample['elapsed_time_s'] = (sample['sim_step'] - initial_step) * tick_s
    previous_intervals = {}
    for first, second in zip(samples, samples[1:]):
        elapsed = (second['sim_step'] - first['sim_step']) * tick_s
        for arm in ARMS:
            before, after = first['arms'][arm], second['arms'][arm]
            derived = dict(source_kind='finite_difference_at_measurement_boundaries', interval_s=elapsed,
                           interval_sim_steps=[first['sim_step'], second['sim_step']])
            velocities = {}
            # qdot is a direct boundary measurement where present. Avoid
            # replacing it with a less informative position difference.
            if 'qdot_rad_s' in before and 'qdot_rad_s' in after:
                derived['joint_acceleration_rad_s2'] = ((array(after['qdot_rad_s']) - array(before['qdot_rad_s'])) / elapsed).tolist()
                derived['joint_acceleration_source'] = 'measured_joint_velocity_difference'
            else:
                velocities['joint_velocity_rad_s'] = (array(after['q_rad']) - array(before['q_rad'])) / elapsed
            if 'measured_ee_pose' in before and 'measured_ee_pose' in after:
                a, b = before['measured_ee_pose'], after['measured_ee_pose']
                if (a.get('frame_id'), a.get('tcp_config_id')) == (b.get('frame_id'), b.get('tcp_config_id')):
                    velocities['ee_linear_velocity_m_s'] = (array(b['position_m']) - array(a['position_m'])) / elapsed
                    velocities['ee_angular_velocity_rad_s'] = _angular_displacement(a['quaternion_xyzw'], b['quaternion_xyzw']) / elapsed
                    derived['ee_frame_id'] = b.get('frame_id')
            if ('measured_jaw' in before and 'measured_jaw' in after
                    and before['measured_jaw']['joint_names'] == after['measured_jaw']['joint_names']):
                velocities['jaw_velocity_m_s'] = (array(after['measured_jaw']['position_m']) - array(before['measured_jaw']['position_m'])) / elapsed
            previous = previous_intervals.get(arm)
            for key, value in velocities.items():
                derived[key] = value.tolist()
                if previous and key in previous[1]:
                    acceleration_key = key.replace('velocity', 'acceleration') + '2'
                    derived[acceleration_key] = ((value - previous[1][key]) / ((elapsed + previous[0]) / 2)).tolist()
            previous_intervals[arm] = elapsed, velocities
            after['derived_motion'] = derived


def _row_arms(row, phase='planned_command'):
    row = array(row, (14,)).tolist()
    return {arm: dict(q_rad=row[offset:offset+6], jaw_goal_normalized=row[offset+6], phase=phase)
            for arm, offset in (('left', 0), ('right', 7))}


def _planned_samples(trace, measured, names, completed):
    """Normalize retained rows; offsets advance by recorded completed counts."""
    segments = trace.get('segments', [trace]) if isinstance(trace, dict) else []
    samples = [dict(native_step=0, arms=deepcopy(measured[0]['arms']), execution_state='initial_measurement')]
    offset, local, limitations = 0, [], []
    for segment_index, segment in enumerate(segments):
        rows = []
        own_segments = any('segments' in value for value in segment.get('arms', {}).values())
        if 'actions' in segment:
            for index, row in enumerate(segment['actions']):
                arms = _row_arms(row)
                contexts = segment.get('row_contexts', [])
                if index < len(contexts):
                    for arm in ARMS:
                        arms[arm]['phase'] = contexts[index].get(arm, {}).get('phase', 'planned_command')
                rows.append(arms)
        elif segment.get('arm') in ARMS and segment.get('q_rad'):
            arm = segment['arm']
            if segment.get('joint_names') != names[arm]:
                raise ValueError('trajectory_feedback_planned_joint_order_mismatch')
            if offset >= len(measured):
                raise ValueError('trajectory_feedback_segment_start_measurement_missing')
            start = measured[offset]['arms']
            positions = segment['q_rad']
            if segment.get('execution_mode') == 'feedback':
                # This controller repeats an endpoint until measured arrival;
                # its single q row is not a one-step predicted trajectory.
                positions = positions * segment.get('completed_native_steps', 0)
                limitations.append('Feedback endpoint repetition includes only recorded completed rows; future duration is unknown.')
            for q in positions:
                arms = {side: dict(q_rad=deepcopy(start[side]['q_rad']), phase='held_start_command') for side in ARMS}
                arms[arm] = dict(q_rad=array(q, (6,)).tolist(), phase='planned_motion')
                rows.append(arms)
            for row in segment.get('gripper_actions', []):
                rows.append(_row_arms(row, 'planned_gripper'))
            if segment.get('planned_gripper_steps', 0) > len(segment.get('gripper_actions', [])):
                limitations.append('Some initial jaw rows were not retained; no jaw command or missing row was synthesized.')
        done = segment.get('completed_native_steps', 0)
        if type(done) is not int or done < 0:
            raise ValueError('trajectory_feedback_invalid_completed_count')
        for index, arms in enumerate(rows, 1):
            samples.append(dict(native_step=offset+index, segment_index=segment_index, arms=arms,
                execution_state='completed_command' if index <= done else 'not_recorded_completed'))
        if segment_index+1 < len(segments) and len(rows) > done:
            raise ValueError('trajectory_feedback_overlapping_segment_timing')
        if own_segments:
            for arm in ARMS:
                for local_index, own in enumerate(segment.get('arms', {}).get(arm, {}).get('segments', [])):
                    commands = own.get('actions')
                    if commands is None:
                        commands = own.get('motion_actions', []) + own.get('gripper_actions', [])
                    own_done = own.get('completed_motion_steps', 0) + own.get('completed_gripper_steps', 0)
                    if len(commands) <= own_done:
                        continue
                    local_samples = [dict(native_step=index+1, arms={arm: dict(
                        q_rad=array(row, (7,))[:6].tolist(), jaw_goal_normalized=float(row[6]),
                        phase='local_compiled_command')},
                        execution_state='completed_command' if index < own_done else 'not_recorded_completed')
                        for index, row in enumerate(commands)]
                    local.append(dict(label=f'{arm}_unexecuted_segment_{local_index}', samples=local_samples,
                        arm=arm, compiled_sim_step=own.get('compiled_sim_step'), segment_index=local_index,
                        waypoint_index=own.get('waypoint_index'), stage_id=own.get('stage_id'),
                        completed_rows=own_done, remaining_rows=len(commands)-own_done,
                        execution_uncertain=bool(own.get('execution_uncertain')),
                        source_kind='unexecuted_arm_candidate_not_synchronized',
                        timing='Local row indices only; no shared future arm timing. Remainder is not recorded completed; a faulting row may have partially executed.'))
        offset += done
    # The final native call can fault after some control ticks. Its issued row
    # remains a candidate/uncertain row, never a completed command.
    return samples if len(samples) > 1 else [], local, list(dict.fromkeys(limitations))


def _candidate_endpoints(receipt, names):
    feedback = receipt.get('failure_feedback') or {}
    arm = feedback.get('arm')
    if arm not in ARMS:
        return []
    result = []
    for index, candidate in enumerate(feedback.get('candidates', [])):
        q = candidate.get('endpoint_q_rad')
        if q is None or candidate.get('joint_names') != names[arm]:
            continue
        result.append(dict(label=f'{arm}_rejected_{candidate.get("stage", index)}_endpoint', arm=arm,
            source_kind='unexecuted_arm_candidate_not_synchronized',
            samples=[dict(native_step=0, arms={arm: dict(q_rad=array(q, (6,)).tolist(), phase='rejected_solver_endpoint')},
                          execution_state='not_executed')], remaining_rows=None,
            timing='Rejected solver endpoint only; no path, arrival or execution timing is implied.',
            solver_stage=candidate.get('stage'), solver_success=candidate.get('solver_success')))
    return result


def _add_geometry(data, model, bases, names):
    # Commanded normalized jaws are not measured positions. Arm-only FK avoids
    # inventing a jaw linkage conversion that is absent from these artifacts.
    ancestors = {'link6'}
    for joint in reversed(model['joints']):
        if joint['child'] in ancestors:
            ancestors.add(joint['parent'])
    arm_model = {**model, 'joints': [joint for joint in model['joints'] if joint['child'] in ancestors]}
    data.update(robot_model=model, T_world_bases={arm: matrix.tolist() for arm, matrix in bases.items()})
    for sample in data['samples']:
        for arm, state in sample['arms'].items():
            jaw = state.get('measured_jaw')
            active_model = model if jaw else arm_model
            joint_names = names[arm] + (jaw['joint_names'] if jaw else [])
            q = state['q_rad'] + (jaw['position_m'] if jaw else [])
            poses = fk({**active_model, 'T_world_base': bases[arm]},
                       dict(joint_names=joint_names, q_rad=q, source_kind='estimate'))
            state['link_positions_m'] = {link: matrix[:3, 3].tolist() for link, matrix in poses.items()}
            state['link6_pose'] = dict(position_m=poses['link6'][:3, 3].tolist(), rotation_matrix=poses['link6'][:3, :3].tolist())
    data['geometry'] = dict(status='available', source='public_URDF_FK_and_measured_start_link6_base_fit',
        jaw_scope='Measured jaws included only where retained; command geometry stops at link6.',
        calibration_scope='Declared common measured frame; inferred base fit is not independent calibration validation.')


def _summary(data, preferred=()):
    samples = data['samples']
    selected = {0, len(samples)//2, len(samples)-1}
    for step in preferred:
        for index, sample in enumerate(samples):
            if sample['native_step'] == step:
                selected.add(index)
                break
    for index in (len(samples)//4, 3*len(samples)//4):
        if len(selected) < 6:
            selected.add(index)
    result = {key: deepcopy(data[key]) for key in ('source_kind', 'label', 'joint_names', 'geometry', 'timing', 'measurement_timing',
        'arm', 'compiled_sim_step', 'segment_index', 'waypoint_index', 'stage_id', 'completed_rows',
        'remaining_rows', 'execution_uncertain', 'solver_stage', 'solver_success') if key in data}
    result.update(available=True, total_samples=len(samples), q_unit='rad', q_precision='exact_stored_values', samples=[])
    for index in sorted(selected):
        sample = samples[index]
        value = {key: deepcopy(sample[key]) for key in ('native_step', 'sim_step', 'elapsed_time_s', 'execution_state') if key in sample}
        value['arms'] = {}
        for arm, state in sample['arms'].items():
            selected_arm = {key: deepcopy(state[key]) for key in ('q_rad', 'qdot_rad_s', 'measured_ee_pose', 'derived_motion',
                'jaw_goal_normalized', 'jaw_command_normalized', 'measured_jaw', 'phase') if key in state}
            if 'link6_pose' in state:
                selected_arm['link6_position_m'] = np.round(state['link6_pose']['position_m'], 4).tolist()
            value['arms'][arm] = selected_arm
        result['samples'].append(value)
    return result


def _compact_feedback(summary):
    """One shared schema/calibration description, exact q in bounded rows."""
    result = deepcopy(summary)
    groups = [result[key] for key in ('measured', 'planned') if result.get(key, {}).get('available')]
    groups += result.get('unexecuted_candidates', [])
    if groups:
        for key in ('joint_names', 'q_unit', 'q_precision'):
            values = [group.get(key) for group in groups]
            if all(value == values[0] for value in values):
                result[key] = values[0]
                for group in groups:
                    group.pop(key, None)
        geometry = [group.get('geometry') for group in groups]
        if all(value == geometry[0] for value in geometry):
            result['geometry'] = geometry[0]
            for group in groups:
                group.pop('geometry', None)
        measured = result.get('measured', {})
        if measured.get('samples'):
            last = measured['samples'][-1]
            jaws = {arm: deepcopy(value['measured_jaw']) for arm, value in last['arms'].items() if 'measured_jaw' in value}
            if jaws:
                result['final_measured_jaws'] = dict(sim_step=last.get('sim_step'), arms=jaws)
        for group in groups:
            for sample in group['samples']:
                for value in sample['arms'].values():
                    value.pop('measured_jaw', None)
                    value.pop('link6_position_m', None)
    compact_overlays = {}
    bindings = []
    for name, overlay in result.get('overlays', {}).items():
        compact = {key: deepcopy(overlay[key]) for key in
            ('status', 'reason', 'overlay_ref', 'original_rgb_ref', 'source_kind') if key in overlay}
        if overlay.get('status') == 'available':
            compact['sample_indices'] = [panel['native_step'] for panel in overlay['panels']]
            binding = {key: deepcopy(overlay[key]) for key in
                ('observation_id', 'camera_id', 'camera_pose_source', 'frame_id', 'panel_width', 'panel_height')}
            if binding not in bindings:
                bindings.append(binding)
        compact_overlays[name] = compact
    result['overlays'] = compact_overlays
    if bindings:
        result['overlay_camera'] = bindings[0] if len(bindings) == 1 else bindings
    result['detail_access'] = 'trajectory_feedback_ref contains every exact row, public FK, full overlay metadata and source traces; read with AurigaClient.artifact.'
    return result


def _routine_completed(receipt, summary, paths, *, diagnostic, path_limitations):
    """Only a known, fully recorded command outcome qualifies for omission."""
    from ...core.presentation.feedback_delivery import execution_exception
    return (not diagnostic and receipt.get('status') == 'completed'
        and not execution_exception(receipt) and not receipt.get('failure_feedback')
        and bool(receipt.get('result_observation_id')) and not receipt.get('observation_unavailable_reason')
        and summary['native_boundary_alignment'] and summary['planned']['available']
        and not summary['total_unexecuted_candidates']
        and not path_limitations
        and summary['measured']['total_samples'] == receipt.get(
            'completed_native_steps', receipt.get('executed_steps', 0)) + 1
        and all(data['geometry']['status'] == 'available' for data in paths.values())
        and not any(sample.get('execution_state') == 'not_recorded_completed'
                    for sample in paths['planned']['samples']))


def _routine_feedback(summary, action, receipt):
    """Compact execution evidence; completed commands are not physical success."""
    result = {key: deepcopy(summary[key]) for key in (
        'available', 'execution_start_observation_id',
        'native_boundary_alignment', 'measured_command_error', 'trajectory_feedback_ref')
        if key in summary}
    result.update(delivery='routine_execution_summary', action_id=action['action_id'],
        execution_status=receipt['status'],
        semantics='Completed commands only; grasp retention, placement and task success are not assessed.',
        detail_access='Use robot_action(mode="inspect", action_id=...) for measured/planned paths, or AurigaClient.artifact(trajectory_feedback_ref) for full evidence.')
    # Execution counts/endpoints already accompany this feedback in the action
    # receipt or template action_results. The artifact above retains every
    # source handle and the complete standalone diagnostic summary.
    return result


def build_action_trajectory_feedback(harness, action, receipt, correlation, *, diagnostic=False):
    """Return bounded readable feedback and image content; never change action state."""
    references = {key: receipt.get(key) for key in ('planned_trace_ref', 'actual_trace_ref', 'dispatched_trace_ref', 'before_state_ref')}
    summary = dict(available=False, source_refs=references, physics_advanced=False,
        execution_start_observation_id=receipt.get('execution_start_observation_id'))
    images = []
    try:
        if any(action.get(key) != harness.identity[key] for key in IDENTITY):
            raise ValueError('stale_environment_identity')
        before = _read(harness, references['before_state_ref'], 'before_state')
        trace = _read(harness, references['actual_trace_ref'], 'actual_trace')
        planned = _read(harness, references['planned_trace_ref'], 'planned_trace')
        names = {arm: list(before['arms'][arm]['joint_names']) for arm in ARMS}
        actual_samples = _measured_samples(before, trace, names)
        completed = receipt.get('completed_native_steps', receipt.get('executed_steps', 0))
        completed_boundary_sampling = (receipt.get('trace_sampling', {}).get('actual_every_native_actions') == 1
            and receipt.get('trace_sampling', {}).get('actual_boundary_samples') is True
            and len(actual_samples) >= completed+1)
        common = dict(identity=dict(harness.identity), observation_id=receipt.get('execution_start_observation_id'),
            action_id=action['action_id'], frame_id=action['frame_id'], tcp_config_id=action['tcp_config_id'], joint_names=names,
            camera_start_q_rad={arm: deepcopy(before['arms'][arm]['q_rad']) for arm in ARMS},
            executed_native_steps=receipt.get('executed_steps', 0))
        actual = dict(common, label='measured_action', source_kind='measured_action_trajectory', samples=actual_samples,
            timing='Recorded measurement boundary index, with original sim_step; final boundary may represent a partial faulting call.')
        try:
            if not completed_boundary_sampling:
                raise ValueError('Complete native-boundary sampling is unavailable; plan/measurement timing was not inferred.')
            planned_samples, local, limitations = _planned_samples(planned, actual_samples, names, completed)
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as error:
            planned_samples, local, limitations = [], [], [str(error)]
        paths = {'measured': actual}
        if planned_samples:
            paths['planned'] = dict(common, label='recorded_plan', source_kind='recorded_planned_commands_not_measurement',
                samples=planned_samples, timing='Stored native command rows. Independent arms include scheduled merged rows only; unexecuted local candidates are separate.')
        local += _candidate_endpoints(receipt, names)
        for index, candidate in enumerate(local):
            paths['local_'+str(index)] = dict(common, **candidate)
        observation_id = common['observation_id']
        geometry_error = None
        time_observation = None
        try:
            observation = harness.store.read('observation', observation_id)
            if any(observation.get(key) != harness.identity[key] for key in IDENTITY):
                raise ValueError('stale_environment_identity')
            if before.get('sim_step') != observation.get('sim_step') or before.get('step_domain') != observation.get('step_domain'):
                raise ValueError('trajectory_feedback_start_observation_mismatch')
            time_observation = observation
            model = _read(harness, observation['robot_model_ref'])
            bases = _bases(model, before, common['frame_id'], common['tcp_config_id'])
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError, OSError) as error:
            geometry_error = str(error)
        for data in paths.values():
            try:
                if geometry_error:
                    raise ValueError(geometry_error)
                _add_geometry(data, model, bases, names)
            except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as error:
                data['geometry'] = dict(status='unavailable', reason=str(error))
                for sample in data['samples']:
                    for state in sample['arms'].values():
                        state.pop('link_positions_m', None)
                    state.pop('link6_pose', None)
        try:
            _add_measured_kinematics(actual, time_observation)
        except (ValueError, KeyError, TypeError, ArithmeticError) as error:
            actual['measurement_timing'] = dict(status='unavailable', reason='motion_derivation_unavailable', detail=str(error))
            for sample in actual['samples']:
                for state in sample['arms'].values():
                    state.pop('derived_motion', None)
        summary.update(available=True, measured=_summary(actual),
            planned=_summary(paths['planned'], (completed, completed+1)) if 'planned' in paths else dict(
                available=False, reason='No synchronized retained command path; missing or rejected plans are not reconstructed.'),
            native_boundary_alignment=completed_boundary_sampling,
            limitations=limitations + ['No object/contact truth or replanning. RGB/camera pose belongs to action start; scene changes, held objects and swept-volume collision are unknown.'])
        # Keep every local path in the artifact. At most one remaining segment
        # per arm plus solver endpoints is needed in the immediate reply.
        visible_local = []
        seen_arms = set()
        # A remaining compiled path carries more evidence than an isolated
        # solver endpoint for the same arm, so show the path first.
        local_keys = sorted((key for key in paths if key.startswith('local_')),
            key=lambda key: paths[key].get('remaining_rows') is not None)
        for key in reversed(local_keys):
            if not key.startswith('local_'):
                continue
            data = paths[key]
            if data['arm'] not in seen_arms:
                visible_local.append(key)
                seen_arms.add(data['arm'])
        summary['unexecuted_candidates'] = [_summary(paths[key]) for key in reversed(visible_local)]
        summary['total_unexecuted_candidates'] = len(local)
        summary['unexecuted_scope'] = 'Not recorded completed; partial faulting execution is possible. Solver endpoints are explicitly not executed.'
        if 'planned' in paths and completed_boundary_sampling:
            by_step = {sample['native_step']: sample for sample in planned_samples}
            errors = []
            for measured in actual_samples[1:completed+1]:
                command = by_step.get(measured['native_step'])
                if command is None:
                    continue
                for arm in ARMS:
                    delta = np.abs(np.asarray(measured['arms'][arm]['q_rad']) - command['arms'][arm]['q_rad'])
                    errors.append(dict(native_step=measured['native_step'], arm=arm, max_abs_joint_error_rad=float(delta.max())))
            if errors:
                worst = max(errors, key=lambda value: value['max_abs_joint_error_rad'])
                summary['measured_command_error'] = dict(worst,
                    semantics='Encoder minus stored target at completed native boundaries; no contact or task-success inference.')
                summary['measured'] = _summary(actual, (worst['native_step'],))
                summary['planned'] = _summary(paths['planned'], (completed, completed+1, worst['native_step']))
        routine = _routine_completed(receipt, summary, paths, diagnostic=diagnostic,
            path_limitations=limitations)
        # Preserve the optional diagnostics-only policy, while allowing runs to
        # keep automatic q + images even after a routine successful command.
        delivery = getattr(getattr(getattr(harness, 'config', None), 'features', None),
                           'action_trajectory_delivery', 'diagnostic')
        automatic = delivery == 'automatic'
        overlays = {}
        if automatic or not routine:
            from .inspection import _trajectory_image
            # Routine feedback shows the measured route once; exact planned q,
            # measured q and tracking differences still accompany every result.
            # The stored plan can be rendered through robot_action inspection.
            visible_paths = (['measured'] if automatic and routine else
                [key for key in ('planned', 'measured') if key in paths] + list(reversed(visible_local)))
            for key in visible_paths:
                metadata, content = _trajectory_image(harness, paths[key], 'cam_head', require_current=False)
                overlays[key] = metadata
                images.extend(content)
        summary['overlays'] = overlays
        if automatic:
            summary['delivery'] = 'automatic_sampled_joints_and_image'
            if routine:
                summary['image_scope'] = 'Measured route; planned joint samples and tracking comparison included. Planned image is available on demand.'
        full = dict(schema_version='action-trajectory-feedback-v1', **common, source_refs=references,
            paths=paths, summary=deepcopy(summary))
        reject_privileged(full, harness.observation_profile)
        summary['trajectory_feedback_ref'] = harness.store.put_artifact(canonical(full).encode(), 'action_trajectory_feedback', 'application/json')
        summary = (_routine_feedback(summary, action, receipt) if routine and not automatic else _compact_feedback(summary))
        harness.store.event('motion.action_trajectory_feedback', summary, correlation, world_kind='internal')
        return summary, images
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError, OSError) as error:
        summary.update(available=False, reason='stored_trajectory_feedback_unavailable', detail=str(error))
        return summary, []
