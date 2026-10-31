"""Bounded actor-authored arm queues with explicit ordering, never a task solver."""

import math
from ...core.action_validation import finite_vector


def validate_arm_queue_action(action, capabilities=None):
    if action.get('type') != 'ArmQueueAction':
        raise ValueError('invalid_arm_queue_type')
    if set(action).intersection({'path', 'gripper_events', 'arm_options', 'hold_native_steps',
            'actions', 'preview_plan_id', 'dual_arm_progression', 'speed_scale', 'speed_preset',
            'waypoint_speed_scales', 'waypoint_speed_presets', 'gripper_speed_scale', 'gripper_speed_preset'}):
        raise ValueError('arm_queue_requires_stage_local_commands')
    if capabilities is not None:
        support = capabilities.get('arm_queue', {})
        if not isinstance(support, dict) or support.get('available') is not True:
            raise ValueError('unsupported_capability:arm_queue')
    queues = action.get('arm_queues')
    if not isinstance(queues, dict) or set(queues) != {'left', 'right'}:
        raise ValueError('arm_queue_requires_left_and_right_lists')
    stages, predecessors = {}, {}
    for arm, queue in queues.items():
        if not isinstance(queue, list) or len(queue) > 8:
            raise ValueError('arm_queue_requires_zero_to_eight_stages_per_arm')
        previous = None
        for stage in queue:
            if not isinstance(stage, dict):
                raise ValueError('invalid_arm_queue_stage')
            identifier, kind = stage.get('id'), stage.get('kind')
            if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 80 or identifier in stages:
                raise ValueError('arm_queue_stage_ids_must_be_unique_nonempty_strings')
            common = {'id', 'kind', 'depends_on'}
            allowed = {'move': {'target', 'speed_scale', 'position_tolerance_m', 'input_grasp_pose'},
                       'gripper': {'goal_normalized', 'gripper_speed_scale'}, 'observe': {'reason'}}
            if not isinstance(kind, str) or kind not in allowed or set(stage) - common - allowed[kind]:
                raise ValueError('invalid_arm_queue_stage_fields')
            deps = stage.get('depends_on', [])
            if (not isinstance(deps, list) or len(deps) > 16 or any(not isinstance(x, str) for x in deps)
                    or len(set(deps)) != len(deps)):
                raise ValueError('invalid_arm_queue_dependencies')
            predecessors[identifier] = set(deps) | ({previous} if previous else set())
            if kind == 'move':
                pose = stage.get('target')
                if not isinstance(pose, dict) or set(pose) != {'position_m', 'quaternion_xyzw'}:
                    raise ValueError('invalid_arm_queue_target')
                finite_vector(pose.get('position_m'), 3, 'position_m')
                finite_vector(pose.get('quaternion_xyzw'), 4, 'quaternion_xyzw')
                if max(map(abs, pose['position_m'])) > 2 or abs(sum(x*x for x in pose['quaternion_xyzw'])-1) > 1e-4:
                    raise ValueError('invalid_arm_queue_target_bounds_or_orientation')
                _number(stage.get('speed_scale', 1.), .25, 2., 'speed_scale')
                _number(stage.get('position_tolerance_m', .005), .0001, .05, 'position_tolerance_m')
                if 'input_grasp_pose' in stage and not isinstance(stage['input_grasp_pose'], dict):
                    raise ValueError('invalid_arm_queue_grasp_metadata')
            elif kind == 'gripper':
                _number(stage.get('goal_normalized'), 0, 1., 'goal_normalized')
                _number(stage.get('gripper_speed_scale', 1.), .25, 1., 'gripper_speed_scale')
            else:
                reason = stage.get('reason')
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                    raise ValueError('arm_queue_observe_requires_reason')
            stages[identifier] = stage
            previous = identifier
    if not stages:
        raise ValueError('arm_queue_requires_at_least_one_stage')
    if any(deps - stages.keys() for deps in predecessors.values()):
        raise ValueError('arm_queue_unknown_dependency')
    completed = set()
    while len(completed) < len(stages):
        ready = {identifier for identifier, deps in predecessors.items() if identifier not in completed and deps <= completed}
        if not ready:
            raise ValueError('arm_queue_dependency_cycle')
        completed.update(ready)
    return action


def _number(value, lower, upper, field):
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError('invalid_arm_queue_' + field)
