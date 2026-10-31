"""运动容差预设、受阻早停配置与末端残差诊断计算。"""

import math

import numpy as np

from ...core.config import motion_thresholds
from .feedback_motion import pose_error

_BASE_THRESHOLDS = motion_thresholds()

TOLERANCE_PRESETS = {
    'tight': {
        'position_tolerance_m': _BASE_THRESHOLDS['tight_position_tolerance_m'],
        'orientation_tolerance_rad': _BASE_THRESHOLDS['tight_orientation_tolerance_rad'],
    },
    'fine': {
        'position_tolerance_m': _BASE_THRESHOLDS['tight_position_tolerance_m'],
        'orientation_tolerance_rad': _BASE_THRESHOLDS['tight_orientation_tolerance_rad'],
    },
    'strict': {
        'position_tolerance_m': _BASE_THRESHOLDS['default_position_tolerance_m'],
        'orientation_tolerance_rad': _BASE_THRESHOLDS['orientation_tolerance_rad'],
    },
    'normal': {
        'position_tolerance_m': _BASE_THRESHOLDS['default_position_tolerance_m'],
        'orientation_tolerance_rad': _BASE_THRESHOLDS['orientation_tolerance_rad'],
    },
    'grasp': {
        'position_tolerance_m': _BASE_THRESHOLDS['default_position_tolerance_m'],
        'orientation_tolerance_rad': _BASE_THRESHOLDS['orientation_tolerance_rad'],
    },
    'relaxed': {
        'position_tolerance_m': _BASE_THRESHOLDS['relaxed_position_tolerance_m'],
        'orientation_tolerance_rad': _BASE_THRESHOLDS['relaxed_orientation_tolerance_rad'],
    },
    'transit': {
        'position_tolerance_m': _BASE_THRESHOLDS['coarse_position_tolerance_m'],
        'orientation_tolerance_rad': _BASE_THRESHOLDS['coarse_orientation_tolerance_rad'],
    },
    'compliant': {
        'position_tolerance_m': _BASE_THRESHOLDS['compliant_position_tolerance_m'],
        'orientation_tolerance_rad': _BASE_THRESHOLDS['relaxed_orientation_tolerance_rad'],
    },
    'seating': {
        'position_tolerance_m': _BASE_THRESHOLDS['relaxed_position_tolerance_m'],
        'orientation_tolerance_rad': _BASE_THRESHOLDS['relaxed_orientation_tolerance_rad'],
    },
    'coarse': {
        'position_tolerance_m': _BASE_THRESHOLDS['coarse_position_tolerance_m'],
        'orientation_tolerance_rad': _BASE_THRESHOLDS['coarse_orientation_tolerance_rad'],
    },
    'unconstrained_orientation': {
        'position_tolerance_m': _BASE_THRESHOLDS['relaxed_position_tolerance_m'],
        'orientation_tolerance_rad': round(math.pi, 5),
    },
}

DEFAULT_TOLERANCE_PRESET = 'normal'
MAX_POSITION_TOLERANCE_M = _BASE_THRESHOLDS['max_position_tolerance_m']
MAX_ORIENTATION_TOLERANCE_RAD = _BASE_THRESHOLDS['max_orientation_tolerance_rad']

STALL_GUARD_PRESETS = {
    'off': None,
    'contact_stop': _BASE_THRESHOLDS['stall_guard_contact_stop_divergence_m'],
    'firm': _BASE_THRESHOLDS['stall_guard_contact_stop_divergence_m'],
    'normal': _BASE_THRESHOLDS['stall_guard_contact_stop_divergence_m'],
    'sensitive': _BASE_THRESHOLDS['stall_guard_strict_divergence_m'],
    'compliant': _BASE_THRESHOLDS['stall_guard_compliant_divergence_m'],
    'strict': _BASE_THRESHOLDS['stall_guard_strict_divergence_m'],
}

DEFAULT_STALL_GUARD_PRESET = 'off'
MIN_TRACKING_DIVERGENCE_M = _BASE_THRESHOLDS['stall_guard_min_divergence_m']
MAX_TRACKING_DIVERGENCE_M = _BASE_THRESHOLDS['stall_guard_max_divergence_m']
MAX_UPWARD_DZ_M = 0.15

TOLERANCE_FIELDS = ('tolerance_preset', 'position_tolerance_m', 'orientation_tolerance_rad', 'max_upward_dz_m')
STALL_GUARD_FIELDS = ('stall_guard_preset', 'max_tracking_divergence_m')


def validate_tolerance_fields(mapping):
    """校验 mapping 中的容差参数合法性。"""
    preset = mapping.get('tolerance_preset')
    if preset is not None and (not isinstance(preset, str) or preset not in TOLERANCE_PRESETS):
        raise ValueError('invalid_tolerance_preset')
    if 'position_tolerance_m' in mapping and mapping['position_tolerance_m'] is not None:
        pos_tol = mapping['position_tolerance_m']
        if isinstance(pos_tol, bool) or not isinstance(pos_tol, (int, float)) or not math.isfinite(pos_tol) or not (0 < float(pos_tol) <= MAX_POSITION_TOLERANCE_M):
            raise ValueError('invalid_position_tolerance_m')
    if 'orientation_tolerance_rad' in mapping and mapping['orientation_tolerance_rad'] is not None:
        ori_tol = mapping['orientation_tolerance_rad']
        if isinstance(ori_tol, bool) or not isinstance(ori_tol, (int, float)) or not math.isfinite(ori_tol) or not (0 < float(ori_tol) <= MAX_ORIENTATION_TOLERANCE_RAD):
            raise ValueError('invalid_orientation_tolerance_rad')
    if 'max_upward_dz_m' in mapping and mapping['max_upward_dz_m'] is not None:
        dz = mapping['max_upward_dz_m']
        if isinstance(dz, bool) or not isinstance(dz, (int, float)) or not math.isfinite(dz) or not (0.0 <= float(dz) <= MAX_UPWARD_DZ_M):
            raise ValueError('invalid_max_upward_dz_m')


def validate_stall_guard_fields(mapping):
    """校验 mapping 中的受阻早停参数合法性。"""
    preset = mapping.get('stall_guard_preset')
    if preset is not None and (not isinstance(preset, str) or preset not in STALL_GUARD_PRESETS):
        raise ValueError('invalid_stall_guard_preset')
    if 'max_tracking_divergence_m' in mapping and mapping['max_tracking_divergence_m'] is not None:
        div = mapping['max_tracking_divergence_m']
        if isinstance(div, bool) or not isinstance(div, (int, float)) or not math.isfinite(div) or not (MIN_TRACKING_DIVERGENCE_M <= float(div) <= MAX_TRACKING_DIVERGENCE_M):
            raise ValueError('invalid_max_tracking_divergence_m')


def resolve_stage_tolerance(stage, batch_defaults=None):
    """解析单个 move 阶段的生效位置与姿态容差。

    优先级：stage 显式数值 > stage tolerance_preset > batch 显式数值 > batch tolerance_preset > 默认 normal (0.01m, 0.10rad)。
    """
    defaults = dict(batch_defaults or {})
    current = dict(stage or {})
    validate_tolerance_fields(defaults)
    validate_tolerance_fields(current)

    batch_preset = defaults.get('tolerance_preset') or DEFAULT_TOLERANCE_PRESET
    resolved = dict(TOLERANCE_PRESETS[batch_preset])
    resolved_preset = batch_preset

    if defaults.get('position_tolerance_m') is not None:
        resolved['position_tolerance_m'] = float(defaults['position_tolerance_m'])
    if defaults.get('orientation_tolerance_rad') is not None:
        resolved['orientation_tolerance_rad'] = float(defaults['orientation_tolerance_rad'])

    stage_preset = current.get('tolerance_preset')
    if stage_preset is not None:
        resolved = dict(TOLERANCE_PRESETS[stage_preset])
        resolved_preset = stage_preset

    if current.get('position_tolerance_m') is not None:
        resolved['position_tolerance_m'] = float(current['position_tolerance_m'])
    if current.get('orientation_tolerance_rad') is not None:
        resolved['orientation_tolerance_rad'] = float(current['orientation_tolerance_rad'])

    out = {
        'tolerance_preset': resolved_preset,
        'position_tolerance_m': round(float(resolved['position_tolerance_m']), 5),
        'orientation_tolerance_rad': round(float(resolved['orientation_tolerance_rad']), 5),
    }
    max_dz = current.get('max_upward_dz_m')
    if max_dz is None:
        max_dz = defaults.get('max_upward_dz_m')
    if max_dz is not None:
        out['max_upward_dz_m'] = round(float(max_dz), 5)
    return out


def resolve_stall_guard(stage, batch_defaults=None):
    """解析单个 move 阶段的受阻早停配置。

    优先级：stage max_tracking_divergence_m > stage stall_guard_preset > batch max_tracking_divergence_m > batch stall_guard_preset > 默认 off (None)。
    """
    defaults = dict(batch_defaults or {})
    current = dict(stage or {})
    validate_stall_guard_fields(defaults)
    validate_stall_guard_fields(current)

    batch_preset = defaults.get('stall_guard_preset') or DEFAULT_STALL_GUARD_PRESET
    resolved_div = STALL_GUARD_PRESETS[batch_preset]
    resolved_preset = batch_preset

    if defaults.get('max_tracking_divergence_m') is not None:
        resolved_div = float(defaults['max_tracking_divergence_m'])

    stage_preset = current.get('stall_guard_preset')
    if stage_preset is not None:
        resolved_div = STALL_GUARD_PRESETS[stage_preset]
        resolved_preset = stage_preset

    if current.get('max_tracking_divergence_m') is not None:
        resolved_div = float(current['max_tracking_divergence_m'])

    return {
        'stall_guard_preset': resolved_preset,
        'max_tracking_divergence_m': None if resolved_div is None else round(float(resolved_div), 5),
    }


def compute_endpoint_residual(target_pose, measured_pose):
    """计算带符号三轴位置残差（实测减目标）及标量位置/姿态误差。"""
    target_pos = np.asarray(target_pose['position_m'], dtype=float)
    measured_pos = np.asarray(measured_pose['position_m'], dtype=float)
    delta = measured_pos - target_pos
    pos_err, ori_err = pose_error(measured_pose, target_pose)
    return {
        'dx_m': round(float(delta[0]), 5),
        'dy_m': round(float(delta[1]), 5),
        'dz_m': round(float(delta[2]), 5),
        'position_error_m': round(pos_err, 5),
        'orientation_error_rad': round(ori_err, 5),
    }
