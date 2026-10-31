"""Synthetic RGB-D fixtures: analytic optical-z plane, explicit camera motion."""
from io import BytesIO

import cv2
import numpy as np

from auriga.core.contracts import canonical

IDENTITY = dict(run_id="synthetic-depth", episode_id="episode-1", env_instance_id="synthetic", env_generation=1)
WIDTH, HEIGHT = 160, 120
K = [[200., 0, 80.], [0, 200., 60.], [0, 0, 1.]]


def texture():
    rng = np.random.default_rng(13)
    grey = rng.integers(70, 190, (HEIGHT, WIDTH), dtype=np.uint8)
    grey = cv2.GaussianBlur(grey, (3, 3), 0)
    return cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR)


def observation(store, number=0, *, depth=None, valid=None, image=None, pose=None,
                arm_center=None, arm_rotation=None, arm_tcp_config_id="arx-x5-link6-v1",
                profile="rgbd_proprio_calibrated_v1"):
    from auriga.robot.model.roboprobe_pose import GRASP_POINT_OFFSET_M, quat_to_matrix
    pose = np.eye(4) if pose is None else np.asarray(pose)
    depth = np.ones((HEIGHT, WIDTH), np.float32) if depth is None else depth
    if valid is not None:
        depth = depth.copy()
        depth[~valid] = np.nan
    valid = np.isfinite(depth) & (depth >= .01) & (depth <= 10.) & (depth > 0)
    image = texture() if image is None else image
    calibration = {"reference_frame_id": "env_origin", "cameras": {"head": {
        "K": K, "distortion": {"model": "opencv", "coefficients": []}, "image_size": [WIDTH, HEIGHT]}}}
    calibration_ref = store.put_artifact(canonical(calibration).encode(), "calibration", "application/json")
    frame_id = f"depth-frame-{number}"
    state = {"source_kind": "measurement", "sim_step": number, "step_domain": "control_step", "arms": {},
             "camera_poses": {"head": {"T_world_camera": pose.tolist(), "frame_id": frame_id,
                 "sim_step": number, "step_domain": "control_step", "source_kind": "measurement"}}}
    if arm_center is not None:
        quaternion = np.array([1., 0, 0, 0]) if arm_rotation is None else np.asarray(arm_rotation)
        rotation = quat_to_matrix(quaternion)
        xyz = np.asarray(arm_center)-rotation[:, 0]*GRASP_POINT_OFFSET_M
        state["arms"]["left"] = {"source_kind": "measurement", "ee_pose": {
            "position_m": xyz.tolist(), "quaternion_xyzw": [*quaternion[1:].tolist(), float(quaternion[0])],
            "frame_id": "env_origin", "source_kind": "measurement"}, "gripper": {
                "position_m": [.015, .015], "position_source_kind": "measurement"}}
        if arm_tcp_config_id is not None:
            state["arms"]["left"]["ee_pose"]["tcp_config_id"] = arm_tcp_config_id
    state_ref = store.put_artifact(canonical(state).encode(), "robot_state", "application/json")
    stream = BytesIO()
    np.savez_compressed(stream, depth=depth, valid=valid)
    depth_ref = store.put_artifact(stream.getvalue(), "depth", "application/x-npz")
    _, png = cv2.imencode(".png", image)
    rgb_ref = store.put_artifact(png.tobytes(), "rgb", "image/png")
    return {"schema_version": "0.1", **IDENTITY, "world_kind": "observed", "data_origin": "synthetic_fixture",
            "observation_id": f"depth-obs-{number}", "sim_step": number, "step_domain": "control_step",
            "clock_domain": "synthetic-depth/generation-1", "calibration_id": "synthetic-depth-calibration",
            "observation_profile": profile, "robot_state_ref": state_ref,
            "cameras": [{"camera_id": "head", "frame_id": frame_id, "width": WIDTH, "height": HEIGHT,
                         "sim_step": number, "step_domain": "control_step", "rgb_ref": rgb_ref,
                         "calibration_ref": calibration_ref, "pixel_transform": "identity", "depth_ref": depth_ref,
                         "depth_kind": "z", "depth_unit": "m", "depth_clip_m": [.01, 10.],
                         "depth_shape": [HEIGHT, WIDTH]}]}
