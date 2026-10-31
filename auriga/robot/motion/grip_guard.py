"""夹爪本体事实计算与基于指尖开度区间 (米) 的条件门禁。"""

import math
import numpy as np

from ...core.config import motion_thresholds

_BASE_THRESHOLDS = motion_thresholds()

# ARX-X5 双指完全张开时的指尖间距为 88mm（单指行程 44mm），全闭为 0.0m
DEFAULT_MAX_APERTURE_M = _BASE_THRESHOLDS["max_gripper_aperture_m"]

GRIP_GUARD_PRESETS = {
    "none": {"min_aperture_m": None, "max_aperture_m": None},
    "non_empty": {
        "min_aperture_m": _BASE_THRESHOLDS["grip_non_empty_min_aperture_m"],
        "max_aperture_m": None,
    },
    "normal": {
        "min_aperture_m": _BASE_THRESHOLDS["grip_non_empty_min_aperture_m"],
        "max_aperture_m": None,
    },
    "thin_object": {
        "min_aperture_m": _BASE_THRESHOLDS["grip_thin_min_aperture_m"],
        "max_aperture_m": None,
    },
    "thin": {
        "min_aperture_m": _BASE_THRESHOLDS["grip_thin_threshold_mm"] / 1000.0,
        "max_aperture_m": None,
    },
    "rigid_object": {
        "min_aperture_m": _BASE_THRESHOLDS["grip_rigid_min_aperture_m"],
        "max_aperture_m": None,
    },
    "solid": {
        "min_aperture_m": _BASE_THRESHOLDS["grip_solid_threshold_mm"] / 1000.0,
        "max_aperture_m": None,
    },
    "wide_object": {
        "min_aperture_m": _BASE_THRESHOLDS["grip_wide_min_aperture_m"],
        "max_aperture_m": None,
    },
    "empty_only": {
        "min_aperture_m": None,
        "max_aperture_m": _BASE_THRESHOLDS["grip_empty_threshold_mm"] / 1000.0,
    },
}

GRIP_GUARD_FIELDS = ("grip_guard_preset", "min_aperture_m", "max_aperture_m")


def _max_aperture_m(adapter=None, arm=None):
    """获取当前夹爪归一化 1.0 对应的指尖最大间距（米）。"""
    geom = getattr(adapter, "grasp_geometry", None) if adapter is not None else None
    if isinstance(geom, dict) and type(geom.get("max_aperture_m")) in (int, float):
        return float(geom["max_aperture_m"])
    if hasattr(geom, "max_aperture_m") and type(geom.max_aperture_m) in (int, float):
        return float(geom.max_aperture_m)
    return DEFAULT_MAX_APERTURE_M


def _aperture_from_jaw(jaw, adapter=None, arm=None):
    """由归一化开度计算指尖间距（米）。优先使用 adapter.grasp_geometry 几何映射。"""
    if jaw is None:
        return None
    geom = getattr(adapter, "grasp_geometry", None) if adapter is not None else None
    if hasattr(geom, "aperture_m") and callable(geom.aperture_m):
        return float(geom.aperture_m(jaw))
    if callable(geom):
        value = geom(jaw) if arm is None else geom(jaw, arm=arm)
        if type(value) in (int, float):
            return float(value)
    return float(jaw) * _max_aperture_m(adapter, arm)


def _measured_physical_jaw(adapter, state, arm):
    """提取真实物理夹爪开度归一化值 [0, 1]（0.0 对应双指物理闭合接触 0mm，忽略 PD 控制器负向过压区间）。"""
    arm_state = state["arms"].get(arm) if isinstance(state, dict) and isinstance(state.get("arms"), dict) else state
    if not isinstance(arm_state, dict):
        return None
    gripper = arm_state.get("gripper")
    if not isinstance(gripper, dict) or gripper.get("position_source_kind") != "measurement":
        return None
    pos = gripper.get("position_m")
    if not isinstance(pos, (list, tuple, np.ndarray)) or len(pos) == 0:
        return None
    if adapter is not None and hasattr(adapter, "robot") and hasattr(adapter.robot, "get_robot_by_arm_name"):
        try:
            robot = adapter.robot.get_robot_by_arm_name(f"{arm}_arm")
            names = list(robot.gripper_joints_name)
            base = names.index(robot.gripper_move["base"])
            value = float(pos[base])
            lower, upper = robot.gripper_scale
            if robot.gripper_move["sign"] == 1:
                phys_lower = max(0.0, float(lower))
                span = float(upper) - phys_lower
                normalized = (value - phys_lower) / span if span > 0 else 0.0
            else:
                phys_upper = min(0.0, float(upper))
                span = phys_upper - float(lower)
                normalized = (phys_upper - value) / span if span > 0 else 0.0
            if math.isfinite(normalized):
                return float(np.clip(normalized, 0.0, 1.0))
        except (ValueError, KeyError, AttributeError, TypeError, IndexError):
            pass
    val = float(pos[0])
    if math.isfinite(val):
        half_max = _max_aperture_m(adapter, arm) / 2.0
        return float(np.clip(val / half_max if half_max > 0 else 0.0, 0.0, 1.0))
    return None


def _extract_jaw(state, adapter=None, arm=None):
    """从 7 维向量、标量或机器人状态字典中提取真实物理归一化夹爪开度 [0, 1]。"""
    if state is None:
        return None
    if type(state) in (int, float) and math.isfinite(state):
        return float(np.clip(state, 0.0, 1.0))
    if isinstance(state, (list, tuple, np.ndarray)):
        arr = np.asarray(state, dtype=float)
        if arr.ndim == 1 and arr.size >= 7 and math.isfinite(arr[6]):
            return float(np.clip(arr[6], 0.0, 1.0))
        return None
    if isinstance(state, dict):
        if "arms" in state and isinstance(state["arms"], dict):
            if arm in state["arms"]:
                phys = _measured_physical_jaw(adapter, state, arm)
                if phys is not None:
                    return phys
                return _extract_jaw(state["arms"][arm], adapter=adapter, arm=arm)
            return None
        if arm is not None:
            phys = _measured_physical_jaw(adapter, {"arms": {arm: state}}, arm)
            if phys is not None:
                return phys
        if type(state.get("measured_jaw")) in (int, float) and math.isfinite(state["measured_jaw"]):
            return float(np.clip(state["measured_jaw"], 0.0, 1.0))
        gripper = state.get("gripper")
        if isinstance(gripper, dict):
            pos = gripper.get("position_m")
            if isinstance(pos, (list, tuple, np.ndarray)) and len(pos) > 0:
                val = float(pos[0])
                if math.isfinite(val):
                    half_max = _max_aperture_m(adapter, arm) / 2.0
                    return float(np.clip(val / half_max if half_max > 0 else 0.0, 0.0, 1.0))
            for key in ("goal_normalized", "command_normalized"):
                val = gripper.get(key)
                if type(val) in (int, float) and math.isfinite(val):
                    return float(np.clip(val, 0.0, 1.0))
    return None


def compute_grip_facts(arm_state_before, arm_state_after, adapter=None, *, arm=None):
    """计算夹爪动作前后的客观本体测量事实，不作空抓或接触猜测。"""
    jaw_before = _extract_jaw(arm_state_before, adapter=adapter, arm=arm)
    jaw_after = _extract_jaw(arm_state_after, adapter=adapter, arm=arm)
    ap_before = _aperture_from_jaw(jaw_before, adapter=adapter, arm=arm)
    ap_after = _aperture_from_jaw(jaw_after, adapter=adapter, arm=arm)
    jaw_delta = (jaw_after - jaw_before) if (jaw_before is not None and jaw_after is not None) else None
    ap_delta = (ap_after - ap_before) if (ap_before is not None and ap_after is not None) else None
    return {
        "measured_jaw_before": round(float(jaw_before), 6) if jaw_before is not None else None,
        "measured_jaw_after": round(float(jaw_after), 6) if jaw_after is not None else None,
        "jaw_delta": round(float(jaw_delta), 6) if jaw_delta is not None else None,
        "measured_aperture_m": round(float(ap_after), 6) if ap_after is not None else None,
        "aperture_delta_m": round(float(ap_delta), 6) if ap_delta is not None else None,
    }


def validate_grip_guard_fields(mapping):
    """校验阶段或批次上的 grip_guard 参数合法性。"""
    if not isinstance(mapping, dict):
        raise ValueError("invalid_grip_guard_mapping")
    preset = mapping.get("grip_guard_preset")
    if preset is not None:
        if not isinstance(preset, str) or preset not in GRIP_GUARD_PRESETS:
            raise ValueError("invalid_grip_guard_preset")
    for field in ("min_aperture_m", "max_aperture_m"):
        if field in mapping and mapping[field] is not None:
            value = mapping[field]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0.0 <= float(value) <= DEFAULT_MAX_APERTURE_M:
                raise ValueError(f"invalid_{field}")
    base = dict(GRIP_GUARD_PRESETS[preset]) if preset is not None else {"min_aperture_m": None, "max_aperture_m": None}
    min_ap = float(mapping["min_aperture_m"]) if mapping.get("min_aperture_m") is not None else base["min_aperture_m"]
    max_ap = float(mapping["max_aperture_m"]) if mapping.get("max_aperture_m") is not None else base["max_aperture_m"]
    if min_ap is not None and max_ap is not None and min_ap > max_ap:
        raise ValueError("invalid_grip_guard_aperture_bounds")
    return {"grip_guard_preset": preset, "min_aperture_m": min_ap, "max_aperture_m": max_ap}


def resolve_grip_guard(stage, batch_defaults=None):
    """合并批次默认与阶段显式 grip_guard 配置，返回实际生效的上下界。"""
    stage = stage if isinstance(stage, dict) else {}
    batch_defaults = batch_defaults if isinstance(batch_defaults, dict) else {}
    has_stage = any(stage.get(k) is not None for k in GRIP_GUARD_FIELDS)
    has_batch = any(batch_defaults.get(k) is not None for k in GRIP_GUARD_FIELDS)
    if not has_stage and not has_batch:
        return None
    source = stage if has_stage else batch_defaults
    validated = validate_grip_guard_fields(source)
    if validated["min_aperture_m"] is None and validated["max_aperture_m"] is None and validated["grip_guard_preset"] in (None, "none"):
        return None
    return validated


def evaluate_grip_guard(grip_facts, guard_config):
    """根据计算出的 grip_facts 与门禁上下界判断是否触发暂停门禁。"""
    if not guard_config:
        return {"triggered": False, "reason": None, "message": None}
    if any(k in guard_config for k in GRIP_GUARD_FIELDS):
        resolved = validate_grip_guard_fields(guard_config)
    else:
        resolved = guard_config
    min_ap = resolved.get("min_aperture_m")
    max_ap = resolved.get("max_aperture_m")
    measured = grip_facts.get("measured_aperture_m") if isinstance(grip_facts, dict) else None
    if measured is None or (min_ap is None and max_ap is None):
        return {
            "triggered": False,
            "reason": None,
            "message": None,
            "min_aperture_m": min_ap,
            "max_aperture_m": max_ap,
            "measured_aperture_m": measured,
        }
    if min_ap is not None and measured < min_ap - 1e-9:
        reason = "aperture_below_min_m"
        message = (
            f"Grip guard triggered ({reason}): measured_aperture_m={measured:.4f}m "
            f"< min_aperture_m={min_ap:.4f}m."
        )
        return {
            "triggered": True,
            "reason": reason,
            "message": message,
            "min_aperture_m": min_ap,
            "max_aperture_m": max_ap,
            "measured_aperture_m": measured,
        }
    if max_ap is not None and measured > max_ap + 1e-9:
        reason = "aperture_above_max_m"
        message = (
            f"Grip guard triggered ({reason}): measured_aperture_m={measured:.4f}m "
            f"> max_aperture_m={max_ap:.4f}m."
        )
        return {
            "triggered": True,
            "reason": reason,
            "message": message,
            "min_aperture_m": min_ap,
            "max_aperture_m": max_ap,
            "measured_aperture_m": measured,
        }
    return {
        "triggered": False,
        "reason": None,
        "message": None,
        "min_aperture_m": min_ap,
        "max_aperture_m": max_ap,
        "measured_aperture_m": measured,
    }
