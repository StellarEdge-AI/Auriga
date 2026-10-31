"""Pre-dispatch command diagnostics; no simulator, planning or scene access.

Reject conspicuous winding/backtracking, never normalize commands or assert
that a shorter joint endpoint has a collision-free route. Budgets are disclosed
controller accounting, not task success or evaluator observations.
"""
import math
import numpy as np

from ...core.config import motion_thresholds

_BASE_THRESHOLDS = motion_thresholds()

POLICY = 'joint-travel-admission-v1'
MIN_TRAVEL_RAD = _BASE_THRESHOLDS['joint_travel_standard_max_rad']
MIN_EXCESS_RAD = _BASE_THRESHOLDS['joint_travel_standard_backtracking_rad']

JOINT_TRAVEL_PRESETS = {
    'standard': {'min_total_travel_rad': MIN_TRAVEL_RAD, 'min_excess_rad': MIN_EXCESS_RAD},
    'extended': {
        'min_total_travel_rad': _BASE_THRESHOLDS['joint_travel_extended_max_rad'],
        'min_excess_rad': _BASE_THRESHOLDS['joint_travel_extended_backtracking_rad'],
    },
    'unconstrained': {'min_total_travel_rad': None, 'min_excess_rad': None},
}
JOINT_TRAVEL_FIELDS = ('joint_travel_preset', 'max_joint_travel_rad', 'max_backtracking_rad')


def resolve_joint_travel_thresholds(joint_travel_preset=None, max_joint_travel_rad=None, max_backtracking_rad=None):
    """解析关节行程准入预设与显式数值覆盖，返回 (min_total_travel_rad, min_excess_rad)。"""
    preset = 'standard' if joint_travel_preset is None else joint_travel_preset
    if not isinstance(preset, str) or preset not in JOINT_TRAVEL_PRESETS:
        raise ValueError('invalid_joint_travel_preset')
    base = JOINT_TRAVEL_PRESETS[preset]
    min_travel = base['min_total_travel_rad']
    min_excess = base['min_excess_rad']
    if max_joint_travel_rad is not None:
        if type(max_joint_travel_rad) not in (int, float) or not math.isfinite(max_joint_travel_rad) or max_joint_travel_rad <= 0:
            raise ValueError('invalid_max_joint_travel_rad')
        min_travel = float(max_joint_travel_rad)
    if max_backtracking_rad is not None:
        if type(max_backtracking_rad) not in (int, float) or not math.isfinite(max_backtracking_rad) or max_backtracking_rad <= 0:
            raise ValueError('invalid_max_backtracking_rad')
        min_excess = float(max_backtracking_rad)
    return min_travel, min_excess


def extract_joint_travel_options(mapping, batch_defaults=None):
    """从阶段或批次字典中提取关节行程准入参数，阶段显式配置优先于批次默认。"""
    mapping = mapping if isinstance(mapping, dict) else {}
    batch_defaults = batch_defaults if isinstance(batch_defaults, dict) else {}
    has_local = any(mapping.get(k) is not None for k in JOINT_TRAVEL_FIELDS)
    source = mapping if has_local else batch_defaults
    options = {k: source[k] for k in JOINT_TRAVEL_FIELDS if source.get(k) is not None}
    resolve_joint_travel_thresholds(**options)
    return options


def assess_motion(start, positions, *, joint_names, joint_types=None, joint_limits=None,
                  native_steps=None, request_remaining=None, episode_remaining=None,
                  joint_travel_preset=None, max_joint_travel_rad=None, max_backtracking_rad=None):
    min_travel_rad, min_excess_rad = resolve_joint_travel_thresholds(
        joint_travel_preset=joint_travel_preset,
        max_joint_travel_rad=max_joint_travel_rad,
        max_backtracking_rad=max_backtracking_rad,
    )
    start, positions = np.asarray(start, dtype=float), np.asarray(positions, dtype=float)
    if (start.shape != (6,) or positions.ndim != 2 or positions.shape[1] != 6 or not len(positions)
            or not np.isfinite(start).all() or not np.isfinite(positions).all() or len(joint_names) != 6):
        raise ValueError('motion_safety_requires_finite_six_joint_trajectory')
    values = np.vstack((start, positions))
    travel = np.abs(np.diff(values, axis=0)).sum(axis=0)
    net = positions[-1] - start
    reversals = travel - np.abs(net)
    types = list(joint_types) if joint_types is not None else [None]*6
    if len(types) != 6:
        raise ValueError('motion_safety_joint_type_count_mismatch')
    limits = list(joint_limits) if joint_limits is not None else [None]*6
    if len(limits) != 6:
        raise ValueError('motion_safety_joint_limit_count_mismatch')
    witnesses, periodic_checks = [], []
    for i, name in enumerate(joint_names):
        check = dict(joint_name=name, status='unknown_joint_type', eligible=False)
        periodic_checks.append(check)
        if types[i] not in ('revolute', 'continuous'):
            continue
        wrapped = (net[i] + math.pi) % math.tau - math.pi
        winding_excess = abs(net[i])-abs(wrapped)
        equivalent = float(start[i] + wrapped)
        check['shortest_equivalent_goal_rad'] = equivalent
        if types[i] == 'continuous':
            check.update(status='continuous_joint', eligible=True)
        else:
            bound = limits[i]
            if (not isinstance(bound, (list, tuple)) or len(bound) != 2
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in bound)
                    or bound[0] > bound[1]):
                check['status'] = 'unknown_limits'
            else:
                lower, upper = bound
                check['joint_limits_rad'] = [lower, upper]
                if not lower-1e-9 <= start[i] <= upper+1e-9:
                    check['status'] = 'measured_start_outside_limits'
                elif not lower-1e-9 <= equivalent <= upper+1e-9:
                    check['status'] = 'shortest_equivalent_outside_limits'
                else:
                    check.update(status='bounded_equivalent_within_limits', eligible=True)
        if min_travel_rad is not None and min_excess_rad is not None:
            winding = check['eligible'] and winding_excess >= min_excess_rad - 1e-9
            if travel[i] > min_travel_rad + 1e-9 and (
                    winding or reversals[i] >= min_excess_rad - 1e-9):
                witnesses.append(dict(joint_name=name, joint_index=i, start_q_rad=float(start[i]),
                    goal_q_rad=float(positions[-1,i]), total_travel_rad=float(travel[i]),
                    signed_net_rad=float(net[i]), shortest_periodic_delta_rad=float(wrapped),
                    periodic_excess_rad=float(winding_excess), backtracking_rad=float(reversals[i]),
                    periodic_eligibility=check,
                    reason='periodic_winding' if winding else 'large_backtracking'))
    count = len(positions) if native_steps is None else int(native_steps)
    exceeded = {kind:int(left) for kind,left in [('request',request_remaining),('episode',episode_remaining)]
                if left is not None and count>left}
    if exceeded and not witnesses:
        semantics = (
            'admission policy only; nominal_native_steps exceeds remaining request or episode native action budget; '
            'reduce stages, shorten travel distance, or increase speed_scale/speed_preset within remaining budget; '
            'joint_travel_preset does not change step budgets; no commands changed or replanning'
        )
    else:
        semantics = (
            'admission policy only; periodic delta requires a continuous joint or a shortest equivalent goal within declared limits; '
            'set joint_travel_preset ("extended" or "unconstrained") or max_joint_travel_rad/max_backtracking_rad on the stage or batch '
            'to override travel thresholds; this is not a substitute path; no commands changed or replanning; no collision or arrival guarantee'
        )
    return dict(policy=POLICY, status='reject' if witnesses or exceeded else 'pass',
        reasons=(['abnormal_joint_travel'] if witnesses else [])+(['insufficient_remaining_native_budget'] if exceeded else []),
        joint_names=list(joint_names), joint_types=types, joint_travel_rad=travel.tolist(),
        joint_net_delta_rad=net.tolist(), witnesses=witnesses,
        thresholds=dict(min_total_travel_rad=min_travel_rad, min_periodic_or_backtracking_excess_rad=min_excess_rad),
        nominal_native_steps=count, request_remaining_native_steps=request_remaining,
        episode_remaining_native_steps=episode_remaining, exceeded_budgets=exceeded,
        periodic_check=('available' if all(c['eligible'] for c in periodic_checks) else
                        'unknown_joint_types_skipped' if all(c['status']=='unknown_joint_type' for c in periodic_checks) else
                        'ineligible_or_unknown_limits_skipped'), periodic_checks=periodic_checks,
        semantics=semantics)


def adapter_assessment(adapter, before, compiled, *, native_steps, request_remaining,
                       joint_travel_preset=None, max_joint_travel_rad=None, max_backtracking_rad=None):
    """Use only public robot metadata, named encoder state and command budget."""
    model = getattr(adapter, 'robot_model', None) or {}
    joints = {j['name']:j for j in model.get('joints', [])}
    # These counters already drive public action-budget accounting. Never query
    # end_flag, success, score, object poses or a simulator collision world.
    limit = getattr(adapter, 'native_action_budget_limit', None)
    count = getattr(adapter, 'native_actions_dispatched', None)
    remaining = max(0, limit-count) if limit is not None and count is not None else None
    results = {}
    for arm, positions in compiled.items():
        state=before['arms'][arm]
        names=state.get('joint_names') or getattr(adapter,'arm_joint_names',{}).get(arm)
        if not names:
            names=list(adapter.robot.get_robot_by_arm_name(arm+'_arm').arm_joints_name)
        results[arm]=assess_motion(state['q_rad'],positions,joint_names=names,
            joint_types=[joints.get(n,{}).get('type') for n in names],
            joint_limits=[[joints[n]['limits']['lower'],joints[n]['limits']['upper']]
                          if all(k in joints.get(n,{}).get('limits',{}) for k in ('lower','upper')) else None for n in names],
            native_steps=native_steps,
            request_remaining=request_remaining,episode_remaining=remaining,
            joint_travel_preset=joint_travel_preset,
            max_joint_travel_rad=max_joint_travel_rad,
            max_backtracking_rad=max_backtracking_rad)
    return dict(enabled=bool(getattr(adapter,'motion_safety_guard_enabled',False)),
        status='reject' if any(r['status']=='reject' for r in results.values()) else 'pass',
        arms=results, information_sources=['public measured named joint positions','public robot URDF joint types and declared limits',
            'compiled candidate joint commands','controller request and episode action budgets'])


def admit_motion(adapter, before, compiled, receipt, *, native_steps, request_remaining,
                 waypoint_index=None, stage_id=None,
                 joint_travel_preset=None, max_joint_travel_rad=None, max_backtracking_rad=None):
    if not getattr(adapter,'motion_safety_guard_enabled',False):
        return True
    result=adapter_assessment(adapter,before,compiled,native_steps=native_steps,request_remaining=request_remaining,
        joint_travel_preset=joint_travel_preset,max_joint_travel_rad=max_joint_travel_rad,max_backtracking_rad=max_backtracking_rad)
    result.update(compiled_sim_step=adapter.control_step,waypoint_index=waypoint_index,stage_id=stage_id,
                  prior_executed_native_steps=receipt['executed_steps'],rejected_segment_executed_native_steps=0)
    receipt.setdefault('motion_safety_checks',[]).append(result)
    if result['status']!='reject':
        return True
    failed=[arm for arm,v in result['arms'].items() if v['status']=='reject']
    receipt.update(reason='motion_safety_rejected',failed_arm=failed[0],failed_arms=failed,
        failed_waypoint_index=waypoint_index,failed_phase='before_segment_dispatch',motion_safety=result)
    if stage_id is not None:receipt['failed_stage_id']=stage_id
    receipt['stop_confirmation']=adapter._hold()
    return False


def admit_jaw_budget(adapter, before, arms, receipt, *, native_steps, request_remaining,
                     waypoint_index=None, stage_id=None):
    """Fresh jaw length may exceed its initial forecast; check admission again."""
    if not native_steps:
        return True
    return admit_motion(adapter,before,{a:[before['arms'][a]['q_rad']] for a in arms},receipt,
        native_steps=native_steps,request_remaining=request_remaining,waypoint_index=waypoint_index,stage_id=stage_id)
