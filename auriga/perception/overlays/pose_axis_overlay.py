"""3D 夹爪抓取中心、前向插入轴 (+X) 与开合夹持平面横杠 (±Y) 投影叠加辅助模块。"""
import cv2
import numpy as np

from ...robot.model.geometry import project

GRASP_CENTER_OFFSET_M = 0.1226
PAD_CENTER_OFFSET_M = 0.1501
FINGERTIP_OFFSET_M = 0.1576
APPROACH_ARROW_EXTRA_M = 0.045
JAW_CROSSBAR_HALF_M = 0.036


def _extract_grasp_center(arm_data):
    pose = (arm_data or {}).get("link6_pose")
    if not isinstance(pose, dict) or "position_m" not in pose or "rotation_matrix" not in pose:
        return None, None
    origin = np.asarray(pose["position_m"], dtype=float)
    rot = np.asarray(pose["rotation_matrix"], dtype=float)
    if origin.shape != (3,) or rot.shape != (3, 3) or not (np.isfinite(origin).all() and np.isfinite(rot).all()):
        return None, None
    return origin, rot


def draw_grasp_center_path(background, camera, samples, width, height):
    """在轨迹预览底图上绘制两侧夹爪真实抓取中心 (finger_middle) 的连续投影路径。"""
    grasp_colors = {"left": (255, 235, 60), "right": (60, 210, 255)}
    for arm, color in grasp_colors.items():
        points = []
        for sample in samples:
            origin, rot = _extract_grasp_center((sample.get("arms") or {}).get(arm))
            if origin is None:
                points.append(None)
            else:
                points.append((origin + GRASP_CENTER_OFFSET_M * rot[:, 0]).tolist())
        previous = None
        for start in range(0, len(points), 2000):
            chunk = points[start : start + 2000]
            valid = [pt for pt in chunk if pt is not None]
            if not valid:
                previous = None
                continue
            pixels, depth, _ = project(camera, valid)
            iterator = iter(zip(pixels, depth))
            for pt in chunk:
                if pt is None:
                    previous = None
                    continue
                pixel, z = next(iterator)
                if not np.isfinite(pixel).all():
                    previous = None
                    continue
                pixel_i = tuple(np.clip(np.rint(pixel), -100000, 100000).astype(int))
                if previous is not None and z > 0 and previous[1] > 0:
                    visible, a, b = cv2.clipLine((0, 0, width, height), previous[0], pixel_i)
                    if visible:
                        cv2.line(background, a, b, color, 1, cv2.LINE_AA)
                previous = pixel_i, z


def draw_pose_axis_frame(output, camera, arm, arm_data, arm_color, width, height):
    """在轨迹关键帧面板上绘制夹爪抓取中心、默认 pad_center 基准点、+X 前向插入箭头与 ±Y 夹爪闭合平面横杠。"""
    origin, rot = _extract_grasp_center(arm_data)
    if origin is None:
        return None
    axis_x = rot[:, 0]
    axis_y = rot[:, 1]
    grasp_center = origin + GRASP_CENTER_OFFSET_M * axis_x
    pad_center = origin + PAD_CENTER_OFFSET_M * axis_x
    fingertip = origin + FINGERTIP_OFFSET_M * axis_x
    approach_tip = fingertip + APPROACH_ARROW_EXTRA_M * axis_x
    jaw_pos = grasp_center + JAW_CROSSBAR_HALF_M * axis_y
    jaw_neg = grasp_center - JAW_CROSSBAR_HALF_M * axis_y

    pts_3d = [
        origin.tolist(),
        grasp_center.tolist(),
        fingertip.tolist(),
        approach_tip.tolist(),
        jaw_pos.tolist(),
        jaw_neg.tolist(),
    ]
    pixels, depths, _ = project(camera, pts_3d)
    if not np.isfinite(pixels).all():
        return None
    px = [tuple(np.clip(np.rint(p), -100000, 100000).astype(int)) for p in pixels]
    p_origin, p_grasp, p_tip, p_arrow, p_jaw_pos, p_jaw_neg = px
    d_origin, d_grasp, d_tip, d_arrow, d_jaw_pos, d_jaw_neg = [float(d) for d in depths]

    # 1. link6 到 grasp_center 的延伸连杆
    if d_origin > 0 and d_grasp > 0:
        vis, a, b = cv2.clipLine((0, 0, width, height), p_origin, p_grasp)
        if vis:
            cv2.line(output, a, b, arm_color, 2, cv2.LINE_AA)

    # 2. ±Y 夹爪开合方向横杠 (绿色)
    crossbar_color = (90, 235, 110)
    if d_jaw_pos > 0 and d_jaw_neg > 0:
        vis, a, b = cv2.clipLine((0, 0, width, height), p_jaw_neg, p_jaw_pos)
        if vis:
            cv2.line(output, a, b, crossbar_color, 2, cv2.LINE_AA)

    # 3. +X 前向逼近/插入方向箭头 (高亮青/黄色)
    arrow_color = (255, 240, 50) if arm == "left" else (50, 230, 255)
    if d_grasp > 0 and d_arrow > 0:
        vis, a, b = cv2.clipLine((0, 0, width, height), p_grasp, p_arrow)
        if vis:
            cv2.arrowedLine(output, a, b, arrow_color, 2, line_type=cv2.LINE_AA, tipLength=0.28)

    # 4. 抓取中心圆环与指尖标记
    if d_grasp > 0 and 0 <= p_grasp[0] < width and 0 <= p_grasp[1] < height:
        cv2.circle(output, p_grasp, 5, arrow_color, 2, cv2.LINE_AA)
        cv2.putText(
            output,
            f"{arm[0]}:grasp",
            (min(width - 48, max(4, p_grasp[0] + 6)), min(height - 42, max(64, p_grasp[1] - 6))),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            arrow_color,
            1,
            cv2.LINE_AA,
        )
    if d_tip > 0 and 0 <= p_tip[0] < width and 0 <= p_tip[1] < height:
        cv2.circle(output, p_tip, 3, (255, 255, 255), -1, cv2.LINE_AA)

    return dict(
        grasp_center_m=grasp_center.round(4).tolist(),
        pad_center_m=pad_center.round(4).tolist(),
        fingertip_m=fingertip.round(4).tolist(),
        approach_axis_world=axis_x.round(4).tolist(),
        jaw_closing_axis_world=axis_y.round(4).tolist(),
    )


def draw_contact_jaw_crossbar(output, camera, selected_pos, rotation_matrix, width, height):
    """在接触点叠加图上补充绘制 ±Y 夹爪开合平面横杠，辅助判断旋转对齐。"""
    axis_y = rotation_matrix[:, 1]
    jaw_pos = (selected_pos + JAW_CROSSBAR_HALF_M * axis_y).tolist()
    jaw_neg = (selected_pos - JAW_CROSSBAR_HALF_M * axis_y).tolist()
    pixels, depths, in_img = project(camera, [jaw_neg, jaw_pos])
    if (
        len(depths) == 2
        and depths[0] > 0
        and depths[1] > 0
        and in_img[0]
        and in_img[1]
        and np.isfinite(pixels).all()
    ):
        a = tuple(np.rint(pixels[0]).astype(int))
        b = tuple(np.rint(pixels[1]).astype(int))
        if not (a[0] < 55 and a[1] < 55) and not (b[0] < 55 and b[1] < 55):
            cv2.line(output, a, b, (90, 210, 70), 2, cv2.LINE_AA)
    return axis_y.tolist()


def draw_object_long_axis_overlay(output, camera, targets_3d, width, height):
    """在轨迹预览面板上绘制匹配目标物体的 3D 主长轴与有向包围盒底面，辅助与 ±Y 夹爪闭合横杠垂直比对。"""
    if not targets_3d:
        return
    seen = set()
    for item in targets_3d:
        if not isinstance(item, dict):
            continue
        oid = item.get("id")
        if oid in seen:
            continue
        seen.add(oid)
        centre = np.asarray(item.get("centre_m", [0.0, 0.0, 0.0]), dtype=float)
        length = max(float(item.get("length_m") or 0.04), 0.02)
        obj_w = max(float(item.get("width_m") or 0.02), 0.01)
        yaw_rad = float(np.radians(float(item.get("yaw_deg") or 0.0)))
        u = np.array([np.cos(yaw_rad), np.sin(yaw_rad), 0.0])
        v = np.array([-np.sin(yaw_rad), np.cos(yaw_rad), 0.0])
        p_neg = centre - 0.5 * length * u
        p_pos = centre + 0.5 * length * u
        corners = [
            (centre - 0.5 * length * u - 0.5 * obj_w * v).tolist(),
            (centre + 0.5 * length * u - 0.5 * obj_w * v).tolist(),
            (centre + 0.5 * length * u + 0.5 * obj_w * v).tolist(),
            (centre - 0.5 * length * u + 0.5 * obj_w * v).tolist(),
        ]
        pts = [p_neg.tolist(), p_pos.tolist(), *corners]
        pixels, depths, _ = project(camera, pts)
        if not np.isfinite(pixels).all():
            continue
        px = [tuple(np.clip(np.rint(p), -100000, 100000).astype(int)) for p in pixels]
        for idx in range(4):
            c0, c1 = px[2 + idx], px[2 + (idx + 1) % 4]
            if depths[2 + idx] > 0 and depths[2 + (idx + 1) % 4] > 0:
                vis, a, b = cv2.clipLine((0, 0, width, height), c0, c1)
                if vis:
                    cv2.line(output, a, b, (220, 110, 255), 1, cv2.LINE_AA)
        if depths[0] > 0 and depths[1] > 0:
            vis, a, b = cv2.clipLine((0, 0, width, height), px[0], px[1])
            if vis:
                cv2.line(output, a, b, (255, 60, 220), 2, cv2.LINE_AA)
                if oid:
                    cv2.putText(
                        output,
                        f"{oid}:axis",
                        (min(width - 52, max(4, a[0] + 4)), min(height - 42, max(64, a[1] - 4))),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.34,
                        (255, 120, 240),
                        1,
                        cv2.LINE_AA,
                    )
