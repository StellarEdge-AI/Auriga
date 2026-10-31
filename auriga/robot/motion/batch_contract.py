"""Strict native contract for a completely compiled, reviewable action batch."""
import math

ARMS = ('left', 'right')


def validate_batch_action(action, capabilities=None):
    from ..model.arx_contract import validate_action
    from .action_compilation import uses_arx_actions
    from .motion_tolerance import (TOLERANCE_FIELDS, STALL_GUARD_FIELDS,
        validate_tolerance_fields, validate_stall_guard_fields)
    from .grip_guard import GRIP_GUARD_FIELDS, validate_grip_guard_fields
    from .motion_safety import JOINT_TRAVEL_FIELDS, extract_joint_travel_options
    custom = capabilities is not None and not uses_arx_actions(capabilities)
    if action.get('type') != 'BatchAction':
        raise ValueError('batch_action_type_required')
    motion_fields = set(TOLERANCE_FIELDS) | set(STALL_GUARD_FIELDS)
    allowed = {'schema_version', 'run_id', 'episode_id', 'env_instance_id', 'env_generation',
        'world_kind', 'type', 'action_id', 'based_on_observation_id', 'mode', 'frame_id',
        'tcp_config_id', 'limits_profile', 'timeout_s', 'stages', 'batch_plan_id',
        'batch_expected_cursor', 'batch_expected_revision', 'review_proxies', 'checkpoint_after_gripper',
        *GRIP_GUARD_FIELDS, *JOINT_TRAVEL_FIELDS} | motion_fields
    if set(action) - allowed:
        raise ValueError('unsupported_batch_action_fields')
    if 'checkpoint_after_gripper' in action and not isinstance(action['checkpoint_after_gripper'], bool):
        raise ValueError('invalid_checkpoint_after_gripper')
    validate_tolerance_fields(action)
    validate_stall_guard_fields(action)
    if any(k in action for k in GRIP_GUARD_FIELDS):
        validate_grip_guard_fields(action)
    if any(k in action for k in JOINT_TRAVEL_FIELDS):
        extract_joint_travel_options(action)
    frame = capabilities.get('frame_id') if custom else 'env_origin'
    tcp = capabilities.get('tcp_config_id') if custom else 'arx-x5-link6-v1'
    if not frame or not tcp or action.get('mode') != 'absolute' or action.get('frame_id') != frame or action.get('tcp_config_id') != tcp:
        raise ValueError('unsupported_batch_frame_or_tcp')
    limits = {capabilities.get('limits_profile')} if custom else {'robodojo-native-v1', 'simulation-slow-v1'}
    if not action.get('limits_profile') or action.get('limits_profile') not in limits:
        raise ValueError('unsupported_batch_limits')
    timeout = action.get('timeout_s')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 300:
        raise ValueError('invalid_batch_timeout')
    if 'batch_plan_id' in action and (not isinstance(action['batch_plan_id'], str) or not action['batch_plan_id']):
        raise ValueError('invalid_batch_plan_id')
    if 'batch_expected_cursor' in action and (type(action['batch_expected_cursor']) is not int or action['batch_expected_cursor'] < 0):
        raise ValueError('invalid_batch_expected_cursor')
    if 'batch_expected_revision' in action and (type(action['batch_expected_revision']) is not int or action['batch_expected_revision'] < 0):
        raise ValueError('invalid_batch_expected_revision')
    if 'review_proxies' in action:
        from .proxy_contract import validate_proxies
        validate_proxies(action['review_proxies'])
    stages = action.get('stages')
    if not isinstance(stages, list) or not 1 <= len(stages) <= 32:
        raise ValueError('batch_requires_one_to_thirty_two_stages')
    ids, dependencies, last, counts = set(), {}, {}, dict.fromkeys(ARMS, 0)
    stage_allowed = {'id', 'arm', 'kind', 'depends_on', 'action', 'reason', 'checkpoint_after_gripper',
        *GRIP_GUARD_FIELDS, *JOINT_TRAVEL_FIELDS} | motion_fields
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) - stage_allowed:
            raise ValueError('invalid_batch_stage_fields')
        identifier = stage.get('id')
        if not isinstance(identifier, str) or not identifier or len(identifier) > 80 or identifier in ids:
            raise ValueError('batch_requires_unique_stage_ids')
        ids.add(identifier)
        kind, arm = stage.get('kind'), stage.get('arm')
        if kind not in ('move', 'gripper', 'observe') or arm not in (*ARMS, None):
            raise ValueError('invalid_batch_stage_kind_or_arm')
        deps = stage.get('depends_on', [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps) or len(set(deps)) != len(deps):
            raise ValueError('invalid_batch_dependencies')
        deps = set(deps)
        owners = ARMS if arm is None else (arm,)
        for side in owners:
            if side in last:
                deps.add(last[side])
            last[side] = identifier
            counts[side] += 1
        dependencies[identifier] = deps
        if kind == 'observe':
            if ('action' in stage or 'checkpoint_after_gripper' in stage or (set(stage) & motion_fields)
                    or any(k in stage for k in (*GRIP_GUARD_FIELDS, *JOINT_TRAVEL_FIELDS))
                    or not isinstance(stage.get('reason'), str) or not stage['reason'].strip()):
                raise ValueError('batch_observe_requires_reason')
        else:
            if arm is None or 'reason' in stage or not isinstance(stage.get('action'), dict):
                raise ValueError('batch_command_requires_arm_action')
            if kind == 'gripper' and (set(stage) & motion_fields):
                raise ValueError('invalid_batch_stage_fields')
            validate_tolerance_fields(stage)
            validate_stall_guard_fields(stage)
            own = stage['action']
            if 'checkpoint_after_gripper' in stage:
                if not isinstance(stage['checkpoint_after_gripper'], bool):
                    raise ValueError('invalid_checkpoint_after_gripper')
                if kind == 'move' and not own.get('gripper_events'):
                    raise ValueError('checkpoint_after_gripper_requires_gripper_event')
            if any(k in stage for k in GRIP_GUARD_FIELDS):
                validate_grip_guard_fields(stage)
            if any(k in stage for k in JOINT_TRAVEL_FIELDS):
                if kind != 'move':
                    raise ValueError('joint_travel_options_require_move_stage')
                extract_joint_travel_options(stage)
            if own.get('type', 'EEAction') != 'EEAction':
                raise ValueError('batch_stage_requires_ee_action')
            if isinstance(own.get('path'), dict):
                validate_tolerance_fields(own['path'])
                validate_stall_guard_fields(own['path'])
            if custom:
                from ...core.contracts import IDENTITY, validate_action as validate_common
                validate_common(own, {key: action[key] for key in IDENTITY},
                    action['based_on_observation_id'], capabilities)
                points = own['path']['waypoints']
                selected = next(iter(points[0])) if len(points) == 1 and len(points[0]) == 1 else None
                tolerance = own['path'].get('position_tolerance_m', .005)
                if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or not 0 < tolerance <= .10:
                    raise ValueError('invalid_position_tolerance_m')
                ori_tolerance = own['path'].get('orientation_tolerance_rad', .1)
                if type(ori_tolerance) not in (int, float) or not math.isfinite(ori_tolerance) or not 0 < ori_tolerance <= math.pi:
                    raise ValueError('invalid_orientation_tolerance_rad')
                if own.get('limits_profile') != action['limits_profile']:
                    raise ValueError('unsupported_batch_limits')
                if own['path']['kind'] == 'hold' and points:
                    raise ValueError('batch_hold_requires_empty_waypoints')
            else:
                selected, _ = validate_action(own)
            if kind == 'move':
                if selected != arm or len(own['path']['waypoints']) != 1:
                    raise ValueError('batch_move_requires_one_owned_target')
                if any(event['phase'] != 'during_motion' for event in own.get('gripper_events', [])):
                    raise ValueError('batch_after_motion_jaw_requires_separate_stage')
            elif own['path']['kind'] != 'hold' or len(own.get('gripper_events', [])) != 1:
                raise ValueError('batch_gripper_requires_one_jaw_event')
            if any(event['arm'] != arm for event in own.get('gripper_events', [])):
                raise ValueError('batch_stage_cannot_control_peer_jaw')
    if any(count > 16 for count in counts.values()):
        raise ValueError('batch_exceeds_sixteen_stages_per_arm')
    if any(deps - ids or identifier in deps for identifier, deps in dependencies.items()):
        raise ValueError('batch_dependency_unknown_or_self_reference')
    remaining, done = dict(dependencies), set()
    while remaining:
        ready = [identifier for identifier, deps in remaining.items() if deps <= done]
        if not ready:
            raise ValueError('batch_dependency_cycle')
        for identifier in ready:
            done.add(identifier)
            del remaining[identifier]
    if capabilities is not None:
        if capabilities.get('motion_execution_mode', 'resampled') != 'resampled':
            raise ValueError('batch_requires_resampled_execution')
        if len({s.get('arm') for s in stages if s['kind'] != 'observe'}) > 1 and not capabilities.get('dual_arm', False):
            raise ValueError('unsupported_capability:dual_arm')
    return dependencies
