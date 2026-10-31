"""Project disclosed X5 contact references onto an original observed RGB image.

Inputs are a GeometryTools.camera result and its actor-permitted original image.
There is no simulator, object detector, scene query or contact inference here.
"""

from copy import deepcopy

import cv2
import numpy as np

from ...robot.model.geometry import array, project, transform
from ...robot.model.grasp_contact import CONTACT_DEPTH_PRESETS_M, CONTACT_FIELDS, resolve_contact_spec
from ...robot.model.roboprobe_pose import quat_to_matrix


ARMS = ('left', 'right')
COLORS = {'pad_center': (30, 220, 250), 'finger_middle': (90, 210, 70),
          'fingertip': (240, 170, 30), 'selected': (255, 255, 255), 'requested': (210, 60, 240)}


def _vector(value, size, name):
    if (not isinstance(value, (list, tuple, np.ndarray)) or len(value) != size
            or any(isinstance(v, (bool, np.bool_)) for v in value)):
        raise ValueError('invalid_contact_overlay_' + name)
    try:
        return array(value, (size,))
    except (ValueError, TypeError) as error:
        raise ValueError('invalid_contact_overlay_' + name) from error


def render_contact_overlay(camera, image_bgr, *, arms=ARMS, selections=None, targets=None):
    """Return ``(new_bgr_image, metadata)``; leave all inputs unchanged.

    ``selections`` maps arms to contact_region OR contact_depth_m. ``targets``
    maps arms to caller-requested selected-contact ``position_m`` and optional
    ``approach_axis_world``. Target positions are contact references, not link6.
    Current references derive only from the camera observation's measured link6.
    """
    if (not isinstance(arms, (tuple, list)) or not 1 <= len(arms) <= 2
            or any(not isinstance(arm, str) or arm not in ARMS for arm in arms) or len(set(arms)) != len(arms)):
        raise ValueError('contact_overlay_requires_unique_left_right_arms')
    selections = {} if selections is None else selections
    targets = {} if targets is None else targets
    if any(not isinstance(value, dict) or set(value) - set(arms) for value in (selections, targets)):
        raise ValueError('contact_overlay_options_require_selected_arms')
    observation, frame, state = camera['observation'], camera['frame'], camera['state']
    if observation.get('world_kind') != 'observed':
        raise ValueError('contact_overlay_requires_observed_image')
    step, domain = observation.get('sim_step'), observation.get('step_domain')
    if (step is None or not domain or any(value.get('sim_step') != step or value.get('step_domain') != domain
                                       for value in (frame, state))):
        raise ValueError('contact_overlay_requires_synchronized_proprioception')
    if frame.get('pixel_transform', 'identity') != 'identity':
        raise ValueError('contact_overlay_requires_original_image_coordinates')
    width, height = camera['width'], camera['height']
    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0
            or not isinstance(image_bgr, np.ndarray) or image_bgr.dtype != np.uint8
            or image_bgr.shape != (height, width, 3)):
        raise ValueError('contact_overlay_original_rgb_size_mismatch')
    transform(camera['T'])
    intrinsic = array(camera['K'], (3, 3))
    if intrinsic[0, 0] <= 0 or intrinsic[1, 1] <= 0 or not np.allclose(intrinsic[2], [0, 0, 1]):
        raise ValueError('contact_overlay_invalid_intrinsics')
    reference_frame = camera['reference_frame_id']
    output = image_bgr.copy()
    points, lines, result_arms = [], [], {}

    def add(arm, name, position, source, color, label=None, marker='circle'):
        index = len(points)
        points.append(dict(arm=arm, reference=name, position_m=position.tolist(), source_kind=source,
                           label=label, marker=marker, color_bgr=color))
        return index

    for arm in arms:
        selection = selections.get(arm, {})
        if not isinstance(selection, dict) or set(selection) - CONTACT_FIELDS:
            raise ValueError('contact_overlay_invalid_contact_selection')
        spec = resolve_contact_spec(selection)
        pose = state.get('arms', {}).get(arm, {}).get('ee_pose', {})
        if (pose.get('source_kind') != 'measurement' or pose.get('frame_id') != reference_frame
                or pose.get('tcp_config_id') != 'arx-x5-link6-v1'):
            raise ValueError('contact_overlay_requires_measured_link6:' + arm)
        origin = _vector(pose.get('position_m'), 3, 'measured_position')
        xyzw = _vector(pose.get('quaternion_xyzw'), 4, 'measured_quaternion')
        if abs(float(np.linalg.norm(xyzw)) - 1.) > 1e-4:
            raise ValueError('contact_overlay_requires_normalized_xyzw_quaternion')
        rotation = quat_to_matrix(np.r_[xyzw[3], xyzw[:3]])
        axis = rotation[:, 0]
        prefix = 'L' if arm == 'left' else 'R'
        references = {}
        for region in CONTACT_DEPTH_PRESETS_M:
            reference = resolve_contact_spec({'contact_region': region})
            position = origin + reference['offset_m'] * axis
            index = add(arm, region, position, 'derived_from_measured_link6', COLORS[region],
                        f'{prefix} actual ' + {'pad_center': 'pad', 'finger_middle': 'middle', 'fingertip': 'tip'}[region])
            references[region] = dict(**reference, position_m=position.tolist(), point_index=index)
        selected = origin + spec['offset_m'] * axis
        current_index = add(arm, 'selected', selected, 'derived_from_measured_link6', COLORS['selected'],
                            f'{prefix} actual selected ({spec["contact_region"]})', 'ring')
        current_end = add(arm, 'actual_approach_end', selected + .06 * axis,
                          'derived_from_measured_link6', COLORS['selected'])
        lines.append(dict(arm=arm, kind='actual_approach', start=current_index, end=current_end,
                          source_kind='derived_from_measured_link6', color_bgr=COLORS['selected']))
        entry = dict(selected_contact=spec, measured_link6_position_m=origin.tolist(),
                     actual_references=references, actual_selected_position_m=selected.tolist(),
                     actual_selected_point_index=current_index, actual_approach_axis_world=axis.tolist())
        if arm in targets:
            target = targets[arm]
            if (not isinstance(target, dict) or 'position_m' not in target
                    or set(target) - {'position_m', 'approach_axis_world'}):
                raise ValueError('contact_overlay_invalid_requested_target')
            wanted = _vector(target['position_m'], 3, 'requested_position')
            target_index = add(arm, 'requested_selected', wanted, 'caller_request', COLORS['requested'],
                               f'{prefix} REQUESTED selected', 'cross')
            entry.update(requested_selected_position_m=wanted.tolist(), requested_point_index=target_index,
                         selected_position_error_m=float(np.linalg.norm(selected-wanted)),
                         target_semantics='caller requested selected contact reference; not a measured object or arrival claim')
            if 'approach_axis_world' in target:
                wanted_axis = _vector(target['approach_axis_world'], 3, 'requested_approach')
                norm = float(np.linalg.norm(wanted_axis))
                if norm <= 1e-9:
                    raise ValueError('contact_overlay_requested_approach_must_be_nonzero')
                wanted_axis = wanted_axis / norm
                target_end = add(arm, 'requested_approach_end', wanted + .06*wanted_axis,
                                 'caller_request', COLORS['requested'])
                lines.append(dict(arm=arm, kind='requested_approach', start=target_index, end=target_end,
                                  source_kind='caller_request', color_bgr=COLORS['requested']))
                entry['requested_approach_axis_world'] = wanted_axis.tolist()
        result_arms[arm] = entry

    pixels, depths, in_image = project(camera, [point['position_m'] for point in points])
    for index, point in enumerate(points):
        pixel = pixels[index]
        finite = bool(np.isfinite(pixel).all())
        point.update(pixel_xy=pixel.tolist() if finite else None, depth_m=float(depths[index]),
                     in_front_of_camera=bool(depths[index] > 0), in_image=bool(in_image[index] and finite),
                     occlusion='not_evaluated')
    for line in lines:
        start, end = (points[line[key]] for key in ('start', 'end'))
        line['drawn'] = start['in_image'] and end['in_image']
        if line['drawn']:
            cv2.arrowedLine(output, tuple(np.rint(start['pixel_xy']).astype(int)),
                            tuple(np.rint(end['pixel_xy']).astype(int)), line['color_bgr'], 2,
                            line_type=cv2.LINE_AA, tipLength=.25)
    label_count = {}
    # Put two arm labels on opposite outer sides of their measured points.
    # Their actual references are only millimetres apart; overlapping long
    # "selected" labels would otherwise hide which arm owns each marker.
    ordered = sorted(arms, key=lambda arm: points[result_arms[arm]['actual_selected_point_index']]['pixel_xy'][0]
                     if points[result_arms[arm]['actual_selected_point_index']]['pixel_xy'] is not None else 0.)
    label_side = {arm: (-1 if len(ordered) == 2 and index == 0 else 1) for index, arm in enumerate(ordered)}
    for point in points:
        if not point['in_image'] or point['label'] is None:
            continue
        pixel = tuple(np.rint(point['pixel_xy']).astype(int))
        color = point['color_bgr']
        if point['marker'] == 'cross':
            cv2.drawMarker(output, pixel, color, cv2.MARKER_TILTED_CROSS, 15, 2, cv2.LINE_AA)
        else:
            cv2.circle(output, pixel, 9 if point['marker'] == 'ring' else 4, color, 2, cv2.LINE_AA)
        count = label_count.get(point['arm'], 0)
        label_count[point['arm']] = count+1
        y = max(12, min(height-3, pixel[1]-28+count*15))
        text_size = cv2.getTextSize(point['label'], cv2.FONT_HERSHEY_SIMPLEX, .4, 1)[0]
        x = pixel[0]+14 if label_side[point['arm']] > 0 else pixel[0]-14-text_size[0]
        x = max(2, min(x, width-text_size[0]-2))
        cv2.putText(output, point['label'], (x,y), cv2.FONT_HERSHEY_SIMPLEX, .4, (0,0,0), 3, cv2.LINE_AA)
        cv2.putText(output, point['label'], (x,y), cv2.FONT_HERSHEY_SIMPLEX, .4, color, 1, cv2.LINE_AA)
    for index, legend in enumerate(('ACTUAL: measured robot pose | REQUESTED: caller target',
                                     'Contact references do not establish object contact or retention')):
        position = (5, max(12,height-23+index*15))
        cv2.putText(output, legend, position, cv2.FONT_HERSHEY_SIMPLEX, .38, (0,0,0), 3, cv2.LINE_AA)
        cv2.putText(output, legend, position, cv2.FONT_HERSHEY_SIMPLEX, .38, (255,255,255), 1, cv2.LINE_AA)
    metadata = dict(world_kind='observed', observation_id=observation['observation_id'],
        camera_id=frame['camera_id'], frame_id=reference_frame, image_frame_id=frame.get('frame_id'),
        sim_step=step, step_domain=domain, original_rgb_ref=deepcopy(frame.get('rgb_ref')),
        width=width, height=height, original_image_coordinates=True, image_resized=False,
        source_kind='derived_from_permitted_measured_robot_pose_and_disclosed_camera_calibration',
        camera_pose_source=camera.get('pose_source'), arms=result_arms, points=points, arrows=lines,
        approach_arrow_length_m=.06, physics_advanced=False,
        executed_native_steps=0, executed_control_steps=0,
        semantics='actual labels use the synchronized measured link6 pose; requested labels use only caller inputs; selected references lie between finger faces, not on a measured object',
        limitation='Projection does not infer objects, certify contact/retention/free space, or resolve visual occlusion. In-image means camera frustum only.')
    return output, metadata
