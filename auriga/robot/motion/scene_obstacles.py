"""会话级持久化场景障碍物登记表、释放位置自动包络记录与水平跨障安全抬升规划。"""
from copy import deepcopy
import math

import numpy as np

from ...core.config import motion_thresholds
from ...core.contracts import IDENTITY
from .proxy_contract import validate_boxes
from ..model.roboprobe_pose import quat_to_matrix

_BASE_THRESHOLDS = motion_thresholds()
MAX_PERSISTENT_OBSTACLES = 32
GRASP_OFFSET_M = 0.147


def _registry(harness):
    """获取当前 episode 绑定的持久化障碍物字典；环境切换时自动重置。"""
    ident = getattr(harness, '_persistent_obstacles_identity', None)
    current_ident = {k: harness.identity.get(k) for k in IDENTITY}
    if ident != current_ident or not hasattr(harness, '_persistent_obstacles'):
        harness._persistent_obstacles_identity = current_ident
        harness._persistent_obstacles = {}
    return harness._persistent_obstacles


def _extract_stage_grasp_xy(stage):
    """提取阶段声明的目标水平坐标 (x, y)，用于排除目标点自身所在位置的自动放置包络。"""
    if not isinstance(stage, dict) or stage.get('kind') != 'move':
        return None
    for key in ('grasp_position_m', 'position_m'):
        pos = stage.get(key)
        if isinstance(pos, list) and len(pos) == 3:
            return float(pos[0]), float(pos[1])
    gp = stage.get('grasp_pose') or (stage.get('action') or {}).get('input_grasp_pose')
    if isinstance(gp, dict) and isinstance(gp.get('position_m'), list) and len(gp['position_m']) == 3:
        return float(gp['position_m'][0]), float(gp['position_m'][1])
    center = _stage_grasp_center_m(stage)
    if center is not None:
        return float(center[0]), float(center[1])
    arm = stage.get('arm')
    wps = stage.get('waypoints') or ((stage.get('action') or {}).get('path') or {}).get('waypoints') or []
    if wps and arm and isinstance(wps[-1], dict):
        pose = wps[-1].get(arm) if arm in wps[-1] else wps[-1]
        if isinstance(pose, dict) and isinstance(pose.get('position_m'), list) and len(pose['position_m']) == 3:
            return float(pose['position_m'][0]), float(pose['position_m'][1])
    return None


# 目标点自动障碍物豁免半径预设（默认 normal=5.5 cm，支持 GPT 显式选预设或自由给值）
DEFAULT_TARGET_EXEMPTION_RADIUS_M = _BASE_THRESHOLDS['target_exemption_radius_m']
TARGET_EXEMPTION_PRESETS = {
    'none': 0.0,
    'tight': 0.030,
    'normal': DEFAULT_TARGET_EXEMPTION_RADIUS_M,
    'wide': 0.080,
}

# 水平跨障安全抬升高度预设（默认 normal=0.13 m，与方向二 v2 一致；支持 GPT 显式选预设或自由给值）
DEFAULT_CLEARANCE_HEIGHT_M = _BASE_THRESHOLDS['clearance_height_m']
CLEARANCE_PRESETS = {
    'none': 0.0,
    'low': 0.09,
    'normal': DEFAULT_CLEARANCE_HEIGHT_M,
    'high': 0.18,
}

OBSTACLE_CLEARANCE_FIELDS = (
    'obstacle_object_ids',
    'obstacle_policy',
    'ignore_obstacle_ids',
    'target_exemption_preset',
    'target_exemption_radius_m',
    'clearance_preset',
    'clearance_height_m',
)
STAGE_CLEARANCE_FIELDS = (
    'clearance_preset',
    'clearance_height_m',
)


def resolve_target_exemption_radius(args):
    """解析目标交互点自动障碍物豁免半径：支持预设（none/tight/normal/wide）或自由数值 target_exemption_radius_m。"""
    if 'target_exemption_preset' in args and 'target_exemption_radius_m' in args:
        raise ValueError('conflicting_target_exemption_preset_and_target_exemption_radius_m')
    preset = args.get('target_exemption_preset')
    if preset is not None:
        if preset not in TARGET_EXEMPTION_PRESETS:
            raise ValueError('invalid_target_exemption_preset')
        radius = TARGET_EXEMPTION_PRESETS[preset]
    else:
        radius = args.get('target_exemption_radius_m', DEFAULT_TARGET_EXEMPTION_RADIUS_M)
    if type(radius) not in (int, float) or not 0.0 <= float(radius) <= 0.15:
        raise ValueError('invalid_target_exemption_radius_m')
    return float(radius)


def resolve_stage_clearance(fields, batch_default_m=DEFAULT_CLEARANCE_HEIGHT_M):
    """解析跨障抬升安全高度：支持预设（none/low/normal/high）或自由数值 clearance_height_m。"""
    if 'clearance_preset' in fields and 'clearance_height_m' in fields:
        raise ValueError('conflicting_clearance_preset_and_clearance_height_m')
    preset = fields.pop('clearance_preset', None)
    if preset is not None:
        if preset not in CLEARANCE_PRESETS:
            raise ValueError('invalid_clearance_preset')
        clearance_m = CLEARANCE_PRESETS[preset]
    else:
        clearance_m = fields.pop('clearance_height_m', batch_default_m)
    if type(clearance_m) not in (int, float) or not 0.0 <= float(clearance_m) <= 0.25:
        raise ValueError('invalid_clearance_height_m')
    return float(clearance_m)


def obstacles_from_object_table(harness, observation_id, object_ids=None, *, min_height_above_table_m=0.020):
    """将缓存的 3D object_table 行转换为世界坐标系 AABB 障碍物包络（支持指定 object_ids 或自动筛选高置信度物体）。"""
    if not observation_id:
        return []
    from ...perception.external.pose_skill import attached_table
    table = attached_table(harness, observation_id)
    if not isinstance(table, dict) or not table.get('rows'):
        return []
    columns = list(table.get('columns') or [])
    table_z = float(table.get('table_z') or 0.765)
    wanted = set(object_ids) if object_ids is not None else None
    boxes = []
    for raw in table.get('rows') or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < len(columns):
            continue
        row = dict(zip(columns, raw))
        oid = row.get('id')
        if not isinstance(oid, str):
            continue
        if wanted is not None and oid not in wanted:
            continue
        quality = str(row.get('quality') or 'ok')
        if str(row.get('support') or '').startswith('held_') or quality == 'held_fk_depth':
            continue
        if wanted is None and not quality.startswith('ok'):
            continue
        centre = row.get('centre_m')
        if not isinstance(centre, list) or len(centre) != 3:
            continue
        cx, cy, cz = float(centre[0]), float(centre[1]), float(centre[2])
        top_z = float(row['top_z']) if isinstance(row.get('top_z'), (int, float)) else cz + 0.02
        bottom_z = float(row['bottom_z']) if isinstance(row.get('bottom_z'), (int, float)) else table_z
        if wanted is None and (top_z - table_z) < min_height_above_table_m:
            continue
        length = max(float(row.get('length') or 0.03), 0.015)
        width = max(float(row.get('width') or 0.03), 0.015)
        yaw_rad = math.radians(float(row.get('yaw_deg') or 0.0))
        dx = max(0.5 * abs(length * math.cos(yaw_rad)) + 0.5 * abs(width * math.sin(yaw_rad)), 0.008)
        dy = max(0.5 * abs(length * math.sin(yaw_rad)) + 0.5 * abs(width * math.cos(yaw_rad)), 0.008)
        z_lo = min(bottom_z, top_z - 0.010)
        z_hi = max(top_z, z_lo + 0.010)
        box_id = oid if wanted is not None else f'vggt_auto_{oid}'
        boxes.append(
            dict(
                id=box_id,
                min_m=[round(cx - dx, 4), round(cy - dy, 4), round(z_lo, 4)],
                max_m=[round(cx + dx, 4), round(cy + dy, 4), round(z_hi, 4)],
                margin_m=0.005,
                evidence=f'vggt_object_table:{observation_id}:{oid}',
            )
        )
    return boxes


def merge_and_persist_obstacles(harness, args, compiled_stages=None):
    """按 obstacle_policy 管理会话级障碍物并合并历史包络；支持 ignore_obstacle_ids 与逐阶段目标豁免半径。"""
    policy = args.get('obstacle_policy', 'persist')
    if policy not in ('persist', 'stateless', 'clear'):
        raise ValueError('invalid_obstacle_policy')
    reg = _registry(harness)
    if policy == 'clear':
        reg.clear()
    exemption_r = resolve_target_exemption_radius(args)
    if compiled_stages and exemption_r != DEFAULT_TARGET_EXEMPTION_RADIUS_M:
        for stage in compiled_stages:
            if isinstance(stage, dict) and stage.get('kind') == 'move':
                path = ((stage.get('action') or {}).get('path'))
                if isinstance(path, dict):
                    path['target_exemption_radius_m'] = exemption_r
    supplied = list(args.get('obstacles') or [])
    obs_id = args.get('observation_id')
    explicit_ids = args.get('obstacle_object_ids')
    if explicit_ids:
        supplied.extend(obstacles_from_object_table(harness, obs_id, object_ids=explicit_ids))
    validated_supplied = []
    if supplied:
        validated = validate_boxes(supplied)
        for box in validated:
            clean_box = {k: deepcopy(v) for k, v in box.items() if k != 'source_kind'}
            validated_supplied.append(clean_box)
            if policy != 'stateless':
                reg[box['id']] = clean_box
        if policy != 'stateless':
            while len(reg) > MAX_PERSISTENT_OBSTACLES:
                reg.pop(next(iter(reg)))
    active_source = {box['id']: box for box in validated_supplied} if policy == 'stateless' else reg
    if not active_source:
        return args
    ignored = set(args.get('ignore_obstacle_ids') or [])
    raw_stages = args.get('stages') or []
    target_xys = [xy for s in raw_stages if (xy := _extract_stage_grasp_xy(s)) is not None]
    if not target_xys and compiled_stages:
        target_xys = [xy for s in compiled_stages if (xy := _extract_stage_grasp_xy(s)) is not None]
    merged_by_id = {}
    for k, v in active_source.items():
        if k in ignored:
            continue
        is_auto = str(k).startswith('placed_') or str(k).startswith('vggt_auto_')
        if is_auto and target_xys and exemption_r > 0.0:
            low, high = v.get('min_m', [0, 0, 0]), v.get('max_m', [0, 0, 0])
            if all(low[0] - exemption_r <= tx <= high[0] + exemption_r and low[1] - exemption_r <= ty <= high[1] + exemption_r for tx, ty in target_xys):
                continue
        merged_by_id[k] = deepcopy(v)
    if validated_supplied:
        for box in validated_supplied:
            if isinstance(box, dict) and isinstance(box.get('id'), str) and box['id'] not in ignored:
                merged_by_id[box['id']] = deepcopy(box)
    if not merged_by_id:
        if 'obstacles' in args:
            result = dict(args)
            result.pop('obstacles', None)
            return result
        return args
    merged = list(merged_by_id.values())[-MAX_PERSISTENT_OBSTACLES:]
    result = dict(args)
    result['obstacles'] = merged
    return result


def _stage_grasp_center_m(stage):
    """从移动阶段的 input_grasp_pose 或末端 waypoint 推算抓取中心 3D 坐标。"""
    own = (stage or {}).get('action') or {}
    gp = own.get('input_grasp_pose')
    if isinstance(gp, dict) and isinstance(gp.get('position_m'), list) and len(gp['position_m']) == 3:
        return np.asarray(gp['position_m'], dtype=float)
    arm = (stage or {}).get('arm')
    wps = (own.get('path') or {}).get('waypoints') or []
    if wps and arm and isinstance(wps[-1].get(arm), dict):
        pose = wps[-1][arm]
        if 'position_m' in pose and 'quaternion_xyzw' in pose:
            origin = np.asarray(pose['position_m'], dtype=float)
            xyzw = np.asarray(pose['quaternion_xyzw'], dtype=float)
            norm = float(np.linalg.norm(xyzw))
            if norm >= 1e-6:
                xyzw = xyzw / norm
                rot = quat_to_matrix(np.r_[xyzw[3], xyzw[:3]])
                return origin + GRASP_OFFSET_M * rot[:, 0]
    return None


def record_placed_obstacles(harness, action, receipt):
    """不自动凭空生成 placed_* 虚拟障碍物包络；障碍物列表完全由模型通过 obstacles 参数显式声明与管理。"""
    return


def _segment_xy_distance_to_box(p0_xy, p1_xy, low_xy, high_xy):
    """计算 2D 线段 (p0_xy -> p1_xy) 到轴对齐矩形 [low_xy, high_xy] 的最小水平距离。"""
    ts = np.linspace(0.0, 1.0, 17)
    pts = p0_xy[None, :] + ts[:, None] * (p1_xy - p0_xy)[None, :]
    clamped = np.clip(pts, low_xy[None, :], high_xy[None, :])
    return float(np.linalg.norm(pts - clamped, axis=1).min())


def plan_move_with_obstacle_clearance(
    adapter,
    robot,
    arm,
    own,
    target,
    start_pos_m,
    expected_q,
    obstacles,
    contact_entity_id,
    local_diagnostics,
    *,
    before_state=None,
    cap_remaining=None,
    **options,
):
    """当水平跨障移动穿过非目标障碍物上方且安全高度不足时，按声明的 clearance_height_m 尝试双段抬升跨越弧线。"""
    base_plan = adapter._plan_segment(robot, target, local_diagnostics, 0, **options)
    if base_plan is None or not obstacles or start_pos_m is None:
        return base_plan, None
    path_cfg = (own or {}).get('path') or {}
    clearance_h = float(path_cfg.get('clearance_height_m', DEFAULT_CLEARANCE_HEIGHT_M))
    if clearance_h <= 0.0:
        return base_plan, None
    exemption_r = float(path_cfg.get('target_exemption_radius_m', DEFAULT_TARGET_EXEMPTION_RADIUS_M))
    p0 = np.asarray(start_pos_m, dtype=float)
    p1 = np.asarray(target['position_m'], dtype=float)
    horiz_dist = float(math.hypot(p1[0] - p0[0], p1[1] - p0[1]))
    if horiz_dist < 0.06:
        return base_plan, None
    min_link6_z = min(float(p0[2]), float(p1[2]))
    required_z = None
    gp_pos = ((own or {}).get('input_grasp_pose') or {}).get('position_m')
    for box in obstacles:
        if not isinstance(box, dict) or box.get('id') == contact_entity_id:
            continue
        low = np.asarray(box['min_m'], dtype=float)
        high = np.asarray(box['max_m'], dtype=float)
        margin = float(box.get('margin_m', 0.0))
        # 仅当当前阶段自身的起点、终点或抓取中心落在该障碍物 XY 豁免邻域内时豁免该阶段，同 batch 其他过境阶段仍正常跨障
        bid = str(box.get('id', ''))
        pad = exemption_r if (bid.startswith('placed_') or bid.startswith('vggt_auto_') or str(box.get('evidence', '')).startswith('vggt_object_table:')) else 0.015
        if (low[0] - pad <= p0[0] <= high[0] + pad and low[1] - pad <= p0[1] <= high[1] + pad):
            continue
        if (low[0] - pad <= p1[0] <= high[0] + pad and low[1] - pad <= p1[1] <= high[1] + pad):
            continue
        if isinstance(gp_pos, list) and len(gp_pos) == 3 and (
            low[0] - pad <= float(gp_pos[0]) <= high[0] + pad and low[1] - pad <= float(gp_pos[1]) <= high[1] + pad
        ):
            continue
        xy_dist = _segment_xy_distance_to_box(p0[:2], p1[:2], low[:2] - margin, high[:2] + margin)
        if xy_dist <= 0.035:
            clearance_link6_z = float(high[2]) + margin + clearance_h
            if min_link6_z < clearance_link6_z:
                required_z = clearance_link6_z if required_z is None else max(required_z, clearance_link6_z)
    if required_z is None:
        return base_plan, None
    safe_z = min(max(float(p0[2]), float(p1[2]), required_z), max(float(p0[2]), float(p1[2])) + 0.18, 1.18)
    if safe_z <= min_link6_z + 0.015:
        return base_plan, None
    mid_target = deepcopy(target)
    mid_target['position_m'] = [
        round(0.5 * (float(p0[0]) + float(p1[0])), 4),
        round(0.5 * (float(p0[1]) + float(p1[1])), 4),
        round(safe_z, 4),
    ]
    mid_diag, end_diag = [], []
    plan1 = adapter._plan_segment(robot, mid_target, mid_diag, 0, **options)
    if plan1 is None or len(plan1[0]) == 0:
        return base_plan, None
    pos1 = adapter._resample_action_plan(plan1[0], arm, own, start_q=np.asarray(expected_q))
    if len(pos1) == 0:
        return base_plan, None
    mid_q = np.asarray(pos1[-1], dtype=float)
    options2 = dict(options, start_q=mid_q)
    plan2 = adapter._plan_segment(robot, target, end_diag, 1, **options2)
    if plan2 is None or len(plan2[0]) == 0:
        return base_plan, None
    pos2 = adapter._resample_action_plan(plan2[0], arm, own, start_q=mid_q)
    if len(pos2) == 0:
        return base_plan, None
    positions = np.vstack([pos1, pos2])
    if cap_remaining is not None and len(positions) >= cap_remaining:
        return base_plan, None
    if before_state is not None:
        from .motion_safety import adapter_assessment
        state = deepcopy(before_state)
        state['arms'][arm]['q_rad'] = list(expected_q)
        assessment = adapter_assessment(
            adapter,
            state,
            {arm: [row.tolist() for row in positions]},
            native_steps=len(positions),
            request_remaining=cap_remaining or len(positions) + 1,
        )
        if assessment.get('enabled') and assessment.get('status') == 'reject':
            return base_plan, None
    return base_plan, positions


def filter_review_obstacles(obstacles, records):
    """自动记录的 placed_* 与自动桥接的 vggt_auto_* 包络仅用于跨障弧线规划，不注入几何碰撞预览告警，仅保留显式传入或登记的精确障碍物。"""
    if not obstacles:
        return []
    return [
        box
        for box in obstacles
        if not (str(box.get('id', '')).startswith('placed_') or str(box.get('id', '')).startswith('vggt_auto_'))
    ]
