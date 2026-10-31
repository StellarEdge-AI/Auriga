"""移动到达容差解析（预设档位与连续数值双模控制）、下探停滞早停与接触受阻高度直出诊断。"""
from copy import deepcopy
import math

from ...core.config import motion_thresholds



def build_contact_block_summary(record, state, tracker, contact_feedback=None, maybe_contact_feedback=None, *,
                                completed_steps=None, planned_steps=None, config=None):
    """当移动阶段因 tracking_error 失败时，构建顶层直观的接触受阻高度与偏离摘要。"""
    div_thresh = motion_thresholds(config)['contact_divergence_threshold_m']
    if isinstance(record, str):
        stage_id, arm = record, state
        target_pose, measured_pose = tracker, contact_feedback
        contact_feedback = maybe_contact_feedback
        tracker = None
        mx, my, mz = [float(x) for x in measured_pose['position_m']]
        tx, ty, tz = [float(x) for x in target_pose['position_m']]
    else:
        stage_id = record.get('id')
        arm = record['arm']
        mx, my, mz = [float(x) for x in state['arms'][arm]['ee_pose']['position_m']]
        tx, ty, tz = [float(x) for x in record['target']['position_m']]
    summary = dict(
        arm=arm,
        blocked_at_link6_z_m=round(mz, 4),
        target_link6_z_m=round(tz, 4),
        vertical_shortfall_m=round(mz - tz, 4),
        horizontal_offset_m=round(math.hypot(mx - tx, my - ty), 4),
        blocked_above_target=bool(mz - tz > div_thresh),
    )
    if stage_id is not None:
        summary['stage_id'] = stage_id
    if completed_steps is not None:
        summary['completed_steps'] = int(completed_steps)
    if planned_steps is not None:
        summary['planned_steps'] = int(planned_steps)
    if tracker and 'first_divergence' in tracker:
        summary['first_divergence'] = deepcopy(tracker['first_divergence'])
    arm_contact = ((contact_feedback or {}).get('arms') or {}).get(arm) or {}
    if arm_contact.get('available'):
        for key in ('actual_contact_position_m', 'requested_contact_position_m', 'approach_shortfall_m'):
            if key in arm_contact:
                val = arm_contact[key]
                summary[key] = [round(float(x), 4) for x in val] if isinstance(val, list) else round(float(val), 4)
    return summary
