"""Optional epipolar guidance over two permitted, current RGB frames.

The caller chooses a source pixel. This module draws its geometric constraint;
it does not choose a correspondence, triangulate an object or move a camera.
"""
from copy import deepcopy

import cv2
import numpy as np
from PIL import Image

from ....core.action_validation import finite_vector
from ....core.contracts import IDENTITY
from ....robot.model.geometry import array, transform
from ....core.storage.images import encode_image, image_url
from ....core.observation_policy import actor_robot_state, has_calibration, require_actor_observation
from ....core.tool_output import ToolOutput
from ..registry import ToolSpec, validator_for


def _object(properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def _vector(length, description=None):
    result = dict(type='array', items={'type': 'number'}, minItems=length, maxItems=length)
    if description:
        result['description'] = description
    return result


INPUT_SCHEMA = _object({
    'observation_id': dict(type='string', minLength=1, maxLength=200,
        description='Current idle formal observation ID, 1–200 characters. Both cameras must belong to this same observation; stale or in-motion observations are rejected.'),
    'source_camera_id': dict(type='string', minLength=1, maxLength=100,
        description='Source calibrated camera ID, 1–100 characters. Its original actor RGB supplies the pixel you chose.'),
    'target_camera_id': dict(type='string', minLength=1, maxLength=100,
        description='Different target calibrated camera ID, 1–100 characters, in the same reference frame and observation. Its original RGB receives the epipolar line.'),
    'source_pixel': _vector(2, 'Exactly two finite numbers [u,v], in original source-image pixels (u right, v down), with 0 <= u < width and 0 <= v < height. No resized/cropped display coordinates. Choose the same distinctive physical feature you intend to find in the target image; source localization error propagates to the line.')})
OUTPUT_SCHEMA = _object({
    'status': {'const': 'guided'}, 'observation_id': {'type': 'string'},
    'source_camera_id': {'type': 'string'}, 'target_camera_id': {'type': 'string'},
    'source_pixel': _vector(2), 'target_line': _vector(3),
    'target_endpoints': dict(type='array', minItems=2, maxItems=2, items=_vector(2)),
    'source_overlay_ref': {'type': 'string'}, 'target_overlay_ref': {'type': 'string'}})


def _unavailable(availability, arguments):
    if not has_calibration(availability.observation_profile):
        return 'stereo_guide_requires_permitted_calibration'
    return None


def make_stereo_guide_tool(read):
    """Only the owner-bound reader crosses the plugin boundary."""
    if not callable(read):
        raise TypeError('stereo_guide_reader_required')

    def handle(context, arguments, correlation):
        return read(arguments, correlation)

    definition = dict(type='function', name='stereo_guide', inputSchema=deepcopy(INPUT_SCHEMA), description=(
        'Mark your source pixel and draw its epipolar constraint on a second current formal RGB image. '
        'Images are returned in source-then-target order, at original dimensions; cyan marks the source point '
        'and a thin cyan line marks the target constraint. Originals are preserved. '
        'target_line=[a,b,c] means a*u+b*v+c=0 in original target pixels, with sqrt(a*a+b*b)=1; '
        'target_endpoints are its intersections with the pixel-center rectangle [0,width-1] x [0,height-1]. '
        'Only calibrated zero-distortion pinhole cameras are supported. Same camera, baseline below 1e-6 m, '
        'degenerate epipolar direction, or no nonzero line segment inside the target image is rejected. '
        'The entire line is a geometric constraint, not an identified match or a positive-depth interval. '
        'Select and verify the SAME physical feature yourself using image appearance and other permitted evidence, '
        'then use existing geometry_query triangulate if appropriate. A small line distance or reprojection error '
        'alone cannot validate semantic correspondence. Source-pixel and calibration errors affect the constraint. '
        'Occlusion, feature visibility, depth, object pose, collision and task success remain untested. '
        'This tool does not match points, triangulate, call a model, acquire a new observation or execute any action.'))
    return ToolSpec(definition, handle, roles=frozenset({'actor', 'program'}), effect='read',
        unavailable=_unavailable, api_version='1.0', output_schemas={'stereo_guide': deepcopy(OUTPUT_SCHEMA)})


def _clip_line(line, width, height):
    """Clip an infinite normalized line to the original pixel-center rectangle."""
    a, b, c = line
    points = []
    for coordinate, value in ((0, 0.), (0, float(width-1)), (1, 0.), (1, float(height-1))):
        denominator = b if coordinate == 0 else a
        if abs(denominator) < 1e-12:
            continue
        point = [value, -(a*value+c)/b] if coordinate == 0 else [-(b*value+c)/a, value]
        if -1e-7 <= point[0] <= width-1+1e-7 and -1e-7 <= point[1] <= height-1+1e-7:
            point = np.clip(point, [0., 0.], [width-1., height-1.])
            if not any(np.linalg.norm(point-old) < 1e-7 for old in points):
                points.append(point)
    if len(points) < 2:
        raise ValueError('stereo_guide_line_outside_target_image')
    pairs = [(np.linalg.norm(p-q), p, q) for i, p in enumerate(points) for q in points[i+1:]]
    distance, p, q = max(pairs, key=lambda item: item[0])
    if distance <= 1e-7:
        raise ValueError('stereo_guide_line_outside_target_image')
    return [p.tolist(), q.tolist()]


def _epipolar_line(source, target, pixel):
    """F = Kb^-T [t]x R Ka^-1; T is camera-optical to common world."""
    finite_vector(pixel, 2, 'source_pixel')
    pixel = array(pixel, (2,))
    for camera in (source, target):
        distortion = array(camera['D'])
        if distortion.ndim != 1 or np.any(distortion != 0):
            raise ValueError('stereo_guide_requires_zero_distortion_pinhole')
        for dimension in ('width', 'height'):
            if type(camera[dimension]) is not int or not 1 < camera[dimension] <= 4096:
                raise ValueError('stereo_guide_invalid_image_dimensions')
        k = array(camera['K'], (3, 3))
        if k[0, 0] <= 0 or k[1, 1] <= 0 or not np.allclose(k[2], [0, 0, 1], rtol=0, atol=1e-12):
            raise ValueError('stereo_guide_invalid_intrinsics')
    if not (0 <= pixel[0] < source['width'] and 0 <= pixel[1] < source['height']):
        raise ValueError('stereo_guide_source_pixel_outside_image')
    relative = transform(np.linalg.inv(transform(target['T'])) @ transform(source['T']))
    translation = relative[:3, 3]
    baseline = float(np.linalg.norm(translation))
    if not np.isfinite(baseline) or baseline < 1e-6:
        raise ValueError('stereo_guide_zero_baseline')
    # Scaling t does not change the line; normalization improves conditioning.
    x, y, z = translation / baseline
    skew = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])
    try:
        direction = relative[:3, :3] @ np.linalg.solve(source['K'], np.r_[pixel, 1.])
        normal = skew @ direction
        if np.linalg.norm(normal) <= 1e-10*np.linalg.norm(direction):
            raise ValueError('stereo_guide_epipole_degenerate')
        line = np.linalg.solve(np.asarray(target['K']).T, normal)
    except np.linalg.LinAlgError as error:
        raise ValueError('stereo_guide_invalid_intrinsics') from error
    norm = float(np.hypot(line[0], line[1]))
    if not np.isfinite(line).all() or norm <= 1e-12*np.linalg.norm(line):
        raise ValueError('stereo_guide_epipole_degenerate')
    line = line / norm
    if line[0] < -1e-12 or (abs(line[0]) <= 1e-12 and line[1] < 0):
        line = -line
    return line.tolist(), _clip_line(line, target['width'], target['height'])


def _require_current(harness, identifier):
    if not harness.accepting or harness.active_action or not harness.latest or harness.latest.get('observation_id') != identifier:
        raise ValueError('stereo_guide_requires_current_idle_observation')
    if any(harness.latest.get(key) != harness.identity[key] for key in IDENTITY):
        raise ValueError('stereo_guide_outside_current_episode')


def read_stereo_guide(harness, arguments, correlation):
    """Owner adapter: reads formal actor evidence and writes only derived images."""
    validator_for(INPUT_SCHEMA).validate(arguments)
    if not has_calibration(harness.observation_profile):
        raise ValueError('stereo_guide_requires_permitted_calibration')
    identifier = arguments['observation_id']
    camera_ids = [arguments['source_camera_id'], arguments['target_camera_id']]
    if camera_ids[0] == camera_ids[1]:
        raise ValueError('stereo_guide_requires_different_cameras')
    with harness.lock:
        _require_current(harness, identifier)
        bundle = harness.store.read('observation', identifier)
        if bundle.get('observation_id') != identifier or bundle.get('world_kind') != 'observed':
            raise ValueError('stereo_guide_requires_formal_observation')
        if any(bundle.get(key) != harness.identity[key] for key in IDENTITY):
            raise ValueError('stereo_guide_outside_current_episode')
        require_actor_observation(bundle, harness.observation_profile)
        cameras, images = [], []
        for camera_id in camera_ids:
            frames = [f for f in bundle.get('cameras', []) if f.get('camera_id') == camera_id]
            if len(frames) != 1:
                raise ValueError('stereo_guide_unknown_or_duplicate_camera')
            frame = frames[0]
            # GeometryTools owns pose/FK and clock checks. First restrict every
            # artifact it can resolve to the formal actor scope.
            references = [bundle.get('robot_state_ref'), frame.get('calibration_ref')]
            if any(not ref for ref in references):
                raise ValueError('stereo_guide_missing_calibration_input')
            references += [ref for ref in (bundle.get('robot_model_ref'), frame.get('pose_sample_ref')) if ref]
            for ref in references:
                harness.store.resolve_artifact(ref, allowed_scopes=('actor',))
            camera = harness.geometry.camera(harness.identity, identifier, camera_id)
            if camera['observation'] != bundle:
                raise ValueError('stereo_guide_scene_observation_mismatch')
            # GeometryTools treats its legacy coefficient list (or explicit
            # OpenCV coefficient object) as pinhole distortion. Never silently
            # reinterpret an explicitly different camera model, even with D=0.
            if camera['calibration'].get('distortion_model') not in (None, 'pinhole_no_distortion', 'opencv'):
                raise ValueError('stereo_guide_requires_zero_distortion_pinhole')
            actor_robot_state(camera['state'], harness.observation_profile)
            info, encoded = harness.store.resolve_image(frame['rgb_ref'], allowed_scopes=('actor',))
            if info.get('kind') != 'rgb':
                raise ValueError('stereo_guide_requires_formal_rgb')
            rgb = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
            if rgb is None or rgb.shape != (camera['height'], camera['width'], 3):
                raise ValueError('stereo_guide_invalid_rgb_dimensions')
            cameras.append(camera)
            images.append(rgb)
        if cameras[0]['reference_frame_id'] != cameras[1]['reference_frame_id']:
            raise ValueError('stereo_guide_reference_frame_mismatch')
        line, endpoints = _epipolar_line(*cameras, arguments['source_pixel'])
        source_point = tuple(np.rint(arguments['source_pixel']).astype(int))
        cv2.drawMarker(images[0], source_point, (255, 255, 0), cv2.MARKER_CROSS, 11, 1, cv2.LINE_AA)
        cv2.line(images[1], tuple(np.rint(endpoints[0]).astype(int)),
                 tuple(np.rint(endpoints[1]).astype(int)), (255, 255, 0), 1, cv2.LINE_AA)
        overlays = [encode_image(Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)),
                                 harness.image_settings, storage=True)[0] for image in images]
        _require_current(harness, identifier)
        refs = [harness.store.put_artifact(encoded, 'geometry_overlay', 'image/jpeg', 'actor') for encoded in overlays]
        result = dict(status='guided', observation_id=identifier,
            source_camera_id=camera_ids[0], target_camera_id=camera_ids[1],
            source_pixel=list(arguments['source_pixel']), target_line=line, target_endpoints=endpoints,
            source_overlay_ref=refs[0], target_overlay_ref=refs[1])
        content = tuple(dict(type='inputImage', imageUrl=image_url(encoded, harness.image_settings)[0]) for encoded in overlays)
        return ToolOutput(result, extra_content=content, kind='stereo_guide', projection='owner_record').response()
