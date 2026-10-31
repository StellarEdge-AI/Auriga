"""Recorded commands or measured robot FK on calibrated, original observed RGB."""
from copy import deepcopy
import json

from ...core.contracts import IDENTITY

import cv2
import numpy as np

from ...robot.model.geometry import project, transform

CAMERA_SOURCES = {'fixed_calibration', 'robot_kinematics', 'measurement', 'estimate'}
TRAJECTORY_LABELS = {
    'candidate_command_trajectory_not_measurement': ('CANDIDATE', 'CURRENT RGB + hypothetical robot joint frames'),
    'recorded_planned_commands_not_measurement': ('RECORDED PLAN', 'START RGB + recorded commanded joint frames'),
    'measured_action_trajectory': ('MEASURED', 'START RGB + measured robot FK; scene may have changed'),
    'unexecuted_arm_candidate_not_synchronized': ('LOCAL ARM PLAN', 'START RGB + local arm plan; future timing unknown'),
}


def render_trajectory_overlay(camera, image, trajectory, *, native_steps=None):
    """Render at most three configurations plus the entire stored link6 path.

    Every panel uses the original current camera pose, including wrist cameras.
    This is not a future rendered view, observed motion or visibility test.
    """
    if camera.get('pose_source') not in CAMERA_SOURCES:
        raise ValueError('trajectory_overlay_requires_deployable_camera_calibration')
    source = trajectory.get('source_kind')
    if source not in TRAJECTORY_LABELS:
        raise ValueError('trajectory_overlay_requires_supported_trajectory')
    if trajectory.get('frame_id') != camera['reference_frame_id']:
        raise ValueError('trajectory_overlay_frame_mismatch')
    observation, frame = camera['observation'], camera['frame']
    if observation.get('world_kind') != 'observed' or frame.get('pixel_transform', 'identity') != 'identity':
        raise ValueError('trajectory_overlay_requires_original_observed_rgb')
    width, height = camera['width'], camera['height']
    if image.shape != (height, width, 3) or image.dtype != np.uint8:
        raise ValueError('trajectory_overlay_image_size_mismatch')
    transform(camera['T'])
    samples = trajectory.get('samples', [])
    by_step = {s['native_step']: s for s in samples}
    if not samples:
        raise ValueError('trajectory_overlay_empty_path')
    if native_steps is None:
        native_steps = list(dict.fromkeys([samples[0]['native_step'], samples[len(samples)//2]['native_step'], samples[-1]['native_step']]))
    if (not isinstance(native_steps, list) or not 1 <= len(native_steps) <= 3
            or any(type(i) is not int or i not in by_step for i in native_steps)):
        raise ValueError('trajectory_overlay_requires_one_to_three_existing_steps')
    joints = trajectory.get('robot_model', {}).get('joints', [])
    colors = {'left': (255, 180, 0), 'right': (40, 90, 255)}
    background = image.copy()
    path_counts = {}
    for arm, color in colors.items():
        # Every stored path sample is projected, without resizing the RGB or
        # inventing intermediate measurements. Lines only connect samples.
        positions = [sample.get('arms', {}).get(arm, {}).get('link_positions_m', {}).get('link6') for sample in samples]
        previous = None
        path_counts[arm] = sum(point is not None for point in positions)
        for start in range(0, len(positions), 2000):
            chunk = positions[start:start+2000]
            valid = [point for point in chunk if point is not None]
            if not valid:
                previous = None
                continue
            pixels, depth, _ = project(camera, valid)
            iterator = iter(zip(pixels, depth))
            for point in chunk:
                if point is None:
                    previous = None
                    continue
                pixel, z = next(iterator)
                pixel = tuple(np.clip(np.rint(pixel), -100000, 100000).astype(int))
                if previous is not None and z > 0 and previous[1] > 0:
                    visible, a, b = cv2.clipLine((0, 0, width, height), previous[0], pixel)
                    if visible:
                        cv2.line(background, a, b, color, 1, cv2.LINE_AA)
                previous = pixel, z
    from .pose_axis_overlay import (
        draw_grasp_center_path,
        draw_object_long_axis_overlay,
        draw_pose_axis_frame,
    )
    draw_grasp_center_path(background, camera, samples, width, height)
    targets_3d = trajectory.get('grasp_3d_targets')
    if targets_3d:
        draw_object_long_axis_overlay(background, camera, targets_3d, width, height)
    panels, details = [], []
    for step in native_steps:
        output = background.copy()
        record = dict(native_step=step, arms={})
        for arm, arm_data in by_step[step]['arms'].items():
            points = arm_data.get('link_positions_m', {})
            if not points:
                continue
            names = list(points)
            pixels, depth, _ = project(camera, [points[n] for n in names])
            projected = {name: (tuple(np.clip(np.rint(pixel), -100000, 100000).astype(int)), z)
                         for name, pixel, z in zip(names, pixels, depth)}
            for joint in joints:
                parent, child = joint['parent'], joint['child']
                if parent not in projected or child not in projected:
                    continue
                a, za = projected[parent]; b, zb = projected[child]
                if za <= 0 or zb <= 0:
                    continue
                visible, a, b = cv2.clipLine((0, 0, width, height), a, b)
                if visible:
                    cv2.line(output, a, b, colors[arm], 3, cv2.LINE_AA)
            for name, (pixel, z) in projected.items():
                if z > 0 and 0 <= pixel[0] < width and 0 <= pixel[1] < height:
                    cv2.circle(output, pixel, 4, colors[arm], -1, cv2.LINE_AA)
                    if name in {'link3', 'link6', 'link7', 'link8'}:
                        cv2.putText(output, arm[0]+':'+name, pixel, cv2.FONT_HERSHEY_SIMPLEX, .35, colors[arm], 1, cv2.LINE_AA)
            draw_pose_axis_frame(output, camera, arm, arm_data, colors[arm], width, height)
            record['arms'][arm] = dict(phase=arm_data.get('phase'), projected_link_count=len(points))
        label, caption = TRAJECTORY_LABELS[source]
        axis = 'boundary' if source == 'measured_action_trajectory' else 'row' if source == 'unexecuted_arm_candidate_not_synchronized' else 'native'
        execution = by_step[step].get('execution_state', '')
        if execution:
            record['execution_state'] = execution
        # An opaque caption strip avoids tiny white/black outlines becoming a
        # second apparent text layer against image detail. The original RGB is
        # separately referenced; its pixel resolution is never changed.
        cv2.rectangle(output, (0, 0), (width-1, 56), (18, 18, 18), -1)
        cv2.rectangle(output, (0, height-38), (width-1, height-1), (18, 18, 18), -1)
        display_execution = {'completed_command': 'completed',
            'not_recorded_completed': 'not completed / may be partial',
            'initial_measurement': 'measured start', 'not_executed': 'not executed'}.get(execution, execution)
        heading = f'{label} | {axis} +{step}' + (f' | {display_execution}' if display_execution else '')
        name = trajectory.get('label', '')
        for y, message, scale in ((18, heading, .49), (35, name, .43),
                (51, 'Blue: left | Orange: right | Thin line: stored link6 path', .43),
                (height-23, caption, .43),
                (height-7, 'No object geometry / occlusion / collision proof', .43)):
            text_width = cv2.getTextSize(message, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)[0][0]
            if text_width > width-12:
                scale *= (width-12)/text_width
            cv2.putText(output, message, (6,y), cv2.FONT_HERSHEY_SIMPLEX, scale, (255,255,255), 1, cv2.LINE_AA)
        panels.append(output); details.append(record)
    if not any(p['arms'] for p in details):
        raise ValueError('trajectory_overlay_link_geometry_unavailable')
    return np.concatenate(panels, axis=1), dict(
        observation_id=observation['observation_id'], camera_id=frame['camera_id'],
        source_kind='candidate_fk_projected_on_permitted_rgb' if source == 'candidate_command_trajectory_not_measurement' else source+'_on_execution_start_rgb',
        camera_pose_source=camera['pose_source'],
        original_rgb_ref=deepcopy(frame['rgb_ref']), frame_id=camera['reference_frame_id'],
        panels=details, panel_width=width, panel_height=height, path_sample_counts=path_counts,
        executed_native_steps=trajectory.get('executed_native_steps', 0), physics_advanced=False,
        limitations=('Unexecuted candidate geometry; future execution replans and may differ. ' if source == 'candidate_command_trajectory_not_measurement'
                     else 'Historical execution-start RGB and camera pose; later scene changes are not reconstructed. ')+
                    'Joint-frame skeleton has no volume. Path lines connect stored samples, not observed swept geometry. '
                    'Image overlap is not 3D collision; occlusion, held objects and task geometry are unknown.')


def deployable_camera(store, identity, observation_id, camera_id, trajectory):
    """Use static camera calibration and measured encoders, never pose samples.

    The simulation adapter supplies calibration as a hardware setup substitute;
    deployment must supply measured intrinsics/mount/extrinsics in the same frame.
    Dynamic simulator camera/base/link transforms and their residuals are ignored.
    """
    from ...robot.model.geometry import array, fk
    observation = store.read('observation', observation_id)
    if any(observation.get(key) != identity[key] for key in IDENTITY):
        raise ValueError('stale_environment_identity')

    def read_json(reference):
        artifact, raw = store.resolve_artifact(reference, allowed_scopes=('actor',))
        if artifact['mime_type'] != 'application/json':
            raise ValueError('trajectory_overlay_requires_actor_json')
        return json.loads(raw)
    frame = next((f for f in observation['cameras'] if f['camera_id'] == camera_id), None)
    if frame is None or not frame.get('calibration_ref'):
        raise ValueError('trajectory_overlay_camera_calibration_unavailable')
    config = read_json(frame['calibration_ref'])
    calibration = config.get('cameras', {}).get(camera_id, config)
    reference = config.get('reference_frame_id')
    if not reference:
        raise ValueError('trajectory_overlay_missing_calibrated_frame')
    if reference != trajectory['frame_id']:
        raise ValueError('trajectory_overlay_frame_mismatch')
    width, height = frame['width'], frame['height']
    if list(calibration.get('image_size', [])) != [width, height]:
        raise ValueError('calibration_image_size_or_crop_mismatch')
    k = array(calibration.get('K'), (3, 3))
    if k[0, 0] <= 0 or k[1, 1] <= 0 or not np.allclose(k[2], [0, 0, 1]):
        raise ValueError('invalid_camera_intrinsics')
    distortion = calibration.get('distortion')
    if distortion is None:
        raise ValueError('missing_distortion_calibration')
    if isinstance(distortion, dict):
        if distortion.get('model') != 'opencv':
            raise ValueError('unsupported_distortion_model')
        distortion = distortion['coefficients']
    arm = calibration.get('arm')
    if arm:
        state = read_json(observation['robot_state_ref'])
        if state.get('sim_step') != observation.get('sim_step') or state.get('step_domain') != observation.get('step_domain'):
            raise ValueError('unsynchronized_observation')
        measured = state['arms'][arm]
        jaw = measured['gripper']
        if measured.get('source_kind') != 'measurement' or jaw.get('position_source_kind') != 'measurement':
            raise ValueError('trajectory_overlay_requires_measured_joints')
        initial = trajectory.get('camera_start_q_rad', {}).get(arm, trajectory['samples'][0]['arms'].get(arm, {}).get('q_rad'))
        if initial is None or not np.allclose(measured['q_rad'], initial, atol=1e-8, rtol=0):
            raise ValueError('trajectory_overlay_measurement_mismatch')
        model = {**trajectory['robot_model'], 'T_world_base': trajectory['T_world_bases'][arm]}
        poses = fk(model, dict(source_kind='measurement',
            joint_names=measured['joint_names'] + jaw['joint_names'],
            q_rad=measured['q_rad'] + jaw['position_m']))
        pose = poses[calibration['link_name']] @ transform(calibration['T_link_camera'])
        source = 'robot_kinematics'
    else:
        pose = transform(calibration['T_world_camera'])
        source = 'fixed_calibration'
    if frame.get('sim_step') != observation.get('sim_step') or frame.get('step_domain') != observation.get('step_domain'):
        raise ValueError('unsynchronized_observation')
    return dict(K=k, D=array(distortion), T=pose, width=width, height=height,
        frame=frame, observation=observation, reference_frame_id=reference, pose_source=source)
