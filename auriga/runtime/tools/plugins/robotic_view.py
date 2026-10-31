"""Optional, read-only inverse wrist-camera pose calculation.

The plugin receives one owner-bound callback. It cannot execute, observe, select
an obstacle-free view or read arbitrary artifacts. Target coordinates are the
caller's hypotheses, not estimated object truth.
"""
from copy import deepcopy

import numpy as np
from scipy.spatial.transform import Rotation

from ....core.action_validation import finite_vector
from ....core.contracts import IDENTITY
from ....robot.model.geometry import array, project, transform
from ....core.observation_policy import actor_robot_state, has_calibration, reject_privileged, require_actor_observation
from ....robot.model.roboprobe_pose import quat_to_matrix
from ....core.tool_output import ToolOutput
from ..registry import ToolSpec, validator_for


def _object(properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def _vector(length, description=None):
    value = dict(type='array', items={'type': 'number'}, minItems=length, maxItems=length)
    if description:
        value['description'] = description
    return value


INPUT_SCHEMA = _object({
    'observation_id': dict(type='string', minLength=1, maxLength=200,
        description='Current idle formal observation ID, 1–200 characters; historical or in-motion observations are rejected.'),
    'camera_id': dict(type='string', minLength=1, maxLength=100,
        description='Existing calibrated wrist camera ID, 1–100 characters, rigidly attached to native link6 through a disclosed fixed-joint chain. Fixed head cameras are unsupported.'),
    'eye_position_m': _vector(3, 'Exactly three finite numbers [x,y,z]: caller-selected desired camera optical center in metres in the observation calibration reference frame (native env_origin). This is not the link6 position.'),
    'target_position_m': _vector(3, 'Exactly three finite numbers [x,y,z]: caller-estimated or hypothesized look-at point in the SAME reference frame/metres. Must differ from eye_position_m by at least 1e-6 m. No object location is inferred.'),
    'up_vector': _vector(3, 'Exactly three finite numbers: nonzero desired image-up direction in the SAME reference frame; magnitude is ignored. Its angle to the view axis must have sine greater than 1e-6. Choose explicitly; no default or automatic roll is selected.')})
_MOVE = _object({'id': {'const': 'view'}, 'kind': {'const': 'move'},
    'arm': {'enum': ['left', 'right']}, 'position_m': _vector(3), 'quaternion_xyzw': _vector(4)})
OUTPUT_SCHEMA = _object({
    'status': {'const': 'calculated'}, 'observation_id': {'type': 'string'}, 'camera_id': {'type': 'string'},
    'frame_id': {'type': 'string'}, 'tcp_config_id': {'const': 'arx-x5-link6-v1'},
    'robot_action_preview': _object({'tool': {'const': 'robot_action'}, 'arguments': _object({
        'mode': {'const': 'preview'}, 'observation_id': {'type': 'string'},
        'stages': dict(type='array', items=_MOVE, minItems=1, maxItems=1)})}),
    'checks': _object({'ik': {'const': 'not_checked'}, 'collision': {'const': 'not_checked'},
        'visibility': {'const': 'unknown'}, 'retention': {'const': 'unknown'}})})


def _unavailable(availability, arguments):
    if not has_calibration(availability.observation_profile):
        return 'robotic_view_requires_permitted_calibration'
    return None


def make_robotic_view_tool(read):
    """Expose a narrow read callback, never a robot/Store/general tool dispatcher."""
    if not callable(read):
        raise TypeError('robotic_view_reader_required')

    def handle(context, arguments, correlation):
        return read(arguments, correlation)

    definition = dict(type='function', name='robotic_view', description=(
        'Calculate a wrist-camera look-at pose from your chosen eye position, target point and image-up direction, '
        'using the current formal camera-to-link6 calibration. Optical axes are right/down/forward. '
        'Returns a link6 position and quaternion_xyzw inside suggested robot_action preview arguments; '
        'this tool does not call preview or execute. Review a separate robot_action preview before choosing to move. '
        'Prefer an otherwise free wrist for a useful complementary view; do not disturb a holding arm merely for a picture. '
        'Choose eye/target coordinates from permitted evidence, optionally using existing geometry_query; '
        'this tool does not infer target depth or select a viewpoint. No new image, model call, IK, obstacle avoidance, '
        'visibility, contact, retention or task-success check is performed. A calculated pose is not proof of a safe '
        'or useful view. Camera motion uses the existing action path and consumes its normal budget if you execute it. '
        'The suggested preview is grounded in this observation and may become stale after another action.'),
        inputSchema=deepcopy(INPUT_SCHEMA))
    return ToolSpec(definition, handle, roles=frozenset({'actor', 'program'}), effect='read',
        unavailable=_unavailable, api_version='1.0', output_schemas={'robotic_view': deepcopy(OUTPUT_SCHEMA)})


def _unit(vector, name):
    vector = array(vector, (3,))
    scale = float(np.max(np.abs(vector)))
    if scale == 0:
        raise ValueError('robotic_view_zero_' + name)
    scaled = vector / scale
    return scaled / np.linalg.norm(scaled)


def _solve_pose(mount, eye, target, up):
    """Return desired link6/camera transforms. No model or world-state inference."""
    mount = transform(mount)
    for name, value in [('eye_position_m', eye), ('target_position_m', target), ('up_vector', up)]:
        finite_vector(value, 3, name)
    eye, target = array(eye, (3,)), array(target, (3,))
    with np.errstate(over='ignore', invalid='ignore'):
        delta = target - eye
        distance = np.linalg.norm(delta)
    if not np.isfinite(distance) or distance < 1e-6:
        raise ValueError('robotic_view_target_distance_invalid')
    forward = _unit(delta, 'view_axis')
    right = np.cross(forward, _unit(up, 'up_vector'))
    if float(np.linalg.norm(right)) <= 1e-6:
        raise ValueError('robotic_view_up_parallel_to_view_axis')
    right = _unit(right, 'right_axis')
    down = np.cross(forward, right)
    desired_camera = np.eye(4)
    desired_camera[:3, :3] = np.column_stack((right, down, forward))
    desired_camera[:3, 3] = eye
    with np.errstate(over='ignore', invalid='ignore'):
        desired_link = desired_camera @ np.linalg.inv(mount)
    return transform(desired_link), transform(desired_camera)


def _link6_optical_mount(model, calibration):
    """Compose the disclosed fixed chain, without evaluating any movable joint."""
    link = calibration.get('link_name')
    if not isinstance(link, str) or not link:
        raise ValueError('robotic_view_missing_mount_link')
    joints = model.get('joints')
    if not isinstance(joints, list):
        raise ValueError('robotic_view_missing_robot_model_joints')
    by_child = {}
    for joint in joints:
        child = joint.get('child')
        if not isinstance(child, str) or child in by_child:
            raise ValueError('robotic_view_ambiguous_robot_model_chain')
        by_child[child] = joint
    mount = transform(calibration.get('T_link_camera')).copy()
    seen = set()
    while link != 'link6':
        if link in seen or link not in by_child:
            raise ValueError('robotic_view_no_fixed_link6_mount_chain')
        seen.add(link)
        joint = by_child[link]
        if joint.get('type') != 'fixed':
            raise ValueError('robotic_view_mount_chain_is_movable')
        mount = transform(joint.get('T_parent_joint')) @ mount
        link = joint.get('parent')
        if not isinstance(link, str):
            raise ValueError('robotic_view_invalid_mount_parent')
    return transform(mount)


def _identity(bundle, identity):
    if any(bundle.get(k) != identity[k] for k in IDENTITY):
        raise ValueError('robotic_view_outside_current_episode')


def _require_current(harness, identifier):
    if not harness.accepting or harness.active_action or not harness.latest or harness.latest.get('observation_id') != identifier:
        raise ValueError('robotic_view_requires_current_idle_observation')
    _identity(harness.latest, harness.identity)


def read_robotic_view(harness, arguments, correlation):
    """Trusted owner adapter. Read only permitted current calibration/proprio."""
    validator_for(INPUT_SCHEMA).validate(arguments)
    if not has_calibration(harness.observation_profile):
        raise ValueError('robotic_view_requires_permitted_calibration')
    identifier, camera_id = arguments['observation_id'], arguments['camera_id']
    _require_current(harness, identifier)
    bundle = harness.store.read('observation', identifier)
    if bundle.get('observation_id') != identifier or bundle.get('world_kind') != 'observed':
        raise ValueError('robotic_view_requires_formal_observation')
    _identity(bundle, harness.identity)
    require_actor_observation(bundle, harness.observation_profile)
    frame = next((f for f in bundle.get('cameras', []) if f.get('camera_id') == camera_id), None)
    if frame is None:
        raise ValueError('robotic_view_unknown_camera')
    # These are the existing geometry reader's inputs. Check their actor scope
    # before allowing the owner-owned SceneStore adapter to resolve them. RGB is
    # not needed for this calculation and is not decoded or returned.
    references = [bundle.get('robot_state_ref'), bundle.get('robot_model_ref'),
                  frame.get('calibration_ref'), frame.get('pose_sample_ref')]
    if any(not reference for reference in references):
        raise ValueError('robotic_view_missing_formal_calibration_input')
    for reference in references:
        harness.store.resolve_artifact(reference, allowed_scopes=('actor',))
    camera = harness.geometry.camera(harness.identity, identifier, camera_id)
    if camera['observation'] != bundle:
        raise ValueError('robotic_view_scene_observation_mismatch')
    calibration, state = camera['calibration'], camera['state']
    actor_robot_state(state, harness.observation_profile)
    arm = calibration.get('arm')
    if arm not in ('left', 'right'):
        raise ValueError('robotic_view_requires_wrist_link6_mount')
    pose = state.get('arms', {}).get(arm, {}).get('ee_pose', {})
    frame_id = camera['reference_frame_id']
    caps = harness.capabilities or {}
    if (pose.get('source_kind') != 'measurement' or pose.get('frame_id') != frame_id
            or pose.get('tcp_config_id') != 'arx-x5-link6-v1'
            or caps.get('frame_id') != frame_id or caps.get('tcp_config_id') != 'arx-x5-link6-v1'):
        raise ValueError('robotic_view_requires_measured_native_link6_frame')
    model = harness.scene.resolve_json(bundle['robot_model_ref'])
    mount = _link6_optical_mount(model, calibration)
    measured_link = np.eye(4)
    measured_link[:3, 3] = array(pose.get('position_m'), (3,))
    xyzw = array(pose.get('quaternion_xyzw'), (4,))
    if abs(float(np.linalg.norm(xyzw)) - 1.) > 1e-4:
        raise ValueError('robotic_view_requires_normalized_measured_quaternion')
    measured_link[:3, :3] = quat_to_matrix(np.r_[xyzw[3], xyzw[:3]])
    # Prevent a camera on another link/arm from being interpreted as this EE.
    if not np.allclose(measured_link @ mount, camera['T'], rtol=0, atol=1e-4):
        raise ValueError('robotic_view_camera_native_link6_mismatch')
    desired_link, desired_camera = _solve_pose(mount, arguments['eye_position_m'],
                                               arguments['target_position_m'], arguments['up_vector'])
    # Reuse the existing calibrated projection for a mathematical self-check;
    # it is NOT a visibility test and contributes no synthetic observation.
    pixels, depths, _ = project({**camera, 'T': desired_camera}, [arguments['target_position_m']])
    if not np.isfinite(pixels).all() or not np.isfinite(depths).all() or depths[0] <= 0:
        raise ValueError('robotic_view_invalid_target_projection')
    quaternion = Rotation.from_matrix(desired_link[:3, :3]).as_quat(canonical=True).tolist()
    stage = dict(id='view', kind='move', arm=arm, position_m=desired_link[:3, 3].tolist(), quaternion_xyzw=quaternion)
    result = dict(status='calculated', observation_id=identifier, camera_id=camera_id,
        frame_id=frame_id, tcp_config_id=pose['tcp_config_id'],
        robot_action_preview=dict(tool='robot_action', arguments=dict(mode='preview', observation_id=identifier, stages=[stage])),
        checks=dict(ik='not_checked', collision='not_checked', visibility='unknown', retention='unknown'))
    _require_current(harness, identifier)
    reject_privileged(result, harness.observation_profile)
    return ToolOutput(result, kind='robotic_view', projection='owner_record').response()
