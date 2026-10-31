"""Trajectory inspection and contact-overlay rendering behind robot_action and robot_contact_overlay."""
from copy import deepcopy
import io
import json
import re

from ...core.contracts import IDENTITY
from ...core.observation_policy import has_calibration, reject_privileged
from ...core.tool_output import StructuredText, ToolOutput, resolve_detail, wire_content


def _trajectory_image(harness, data, camera_id, native_steps=None, *, require_current=True):
    import cv2
    import numpy as np
    from PIL import Image
    from ...perception.overlays.trajectory_overlay import deployable_camera, render_trajectory_overlay
    from ...core.storage.images import encode_image, image_url
    try:
        current = bool(harness.latest and data['observation_id'] == harness.latest['observation_id'])
        if require_current and not current:
            raise ValueError('trajectory_overlay_requires_current_preview')
        camera = deployable_camera(harness.store, harness.identity, data['observation_id'], camera_id, data)
        _, raw = harness.store.resolve_image(camera['frame']['rgb_ref'], allowed_scopes=('actor',))
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError('trajectory_overlay_rgb_unavailable')
        rendered, metadata = render_trajectory_overlay(camera, image, data, native_steps=native_steps)
        if data.get('execution_binding', '').startswith('exact_cached_rows'):
            metadata['limitations'] = 'Exact cached command FK; execution uses these rows or stops. No physical safety or contact proof.'
        if 'batch_geometry_review' in data:
            from ...robot.motion.batch_geometry import annotate_overlay
            rendered, metadata = annotate_overlay(camera, rendered, metadata, data)
        encoded, _ = encode_image(Image.fromarray(cv2.cvtColor(rendered, cv2.COLOR_BGR2RGB)), harness.image_settings, storage=True)
        reference = harness.store.put_artifact(encoded, 'trajectory_overlay', 'image/jpeg')
        metadata.update(status='available', overlay_ref=reference, candidate_label=data['label'], current=current)
        reject_privileged(metadata, harness.observation_profile)
        url, _ = image_url(encoded, harness.image_settings)
        return metadata, [dict(type='inputImage', imageUrl=url)]
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError, OSError, cv2.error) as error:
        return dict(status='unavailable', reason=str(error), candidate_label=data['label']), []


def inspect_trajectory(harness, args, correlation, *, cached_action=False, structured=False):
    if set(args) - {'trajectory_ref', 'path_kind', 'native_steps', 'start_native_step', 'max_samples', 'detail', 'include_geometry', 'camera_id'}:
        raise ValueError('invalid_trajectory_inspection_fields')
    if not cached_action and not harness.config.features.trajectory_preview:
        raise ValueError('trajectory_preview_disabled')
    if 'include_geometry' in args and type(args['include_geometry']) is not bool:
        raise ValueError('trajectory_include_geometry_must_be_bool')
    alias_detail = 'full' if args.get('include_geometry') else 'summary'
    detail = resolve_detail(args.get('detail'), default=alias_detail)
    if 'include_geometry' in args and detail != alias_detail:
        raise ValueError('trajectory_detail_and_include_geometry_conflict')
    include_geometry = detail == 'full'
    paged = 'start_native_step' in args or 'max_samples' in args
    if paged and 'native_steps' in args:
        raise ValueError('trajectory_native_steps_and_pagination_are_mutually_exclusive')
    if 'path_kind' in args and (not isinstance(args['path_kind'], str) or not args['path_kind']):
        raise ValueError('trajectory_path_kind_must_be_nonempty_string')
    with harness.lock:
        artifact, raw = harness.store.resolve_artifact(args['trajectory_ref'], allowed_scopes=('actor',))
        action_feedback = artifact['kind'] == 'action_trajectory_feedback'
        if artifact['kind'] not in ('candidate_trajectory', 'action_trajectory_feedback') or artifact['mime_type'] != 'application/json':
            raise ValueError('not_a_candidate_trajectory')
        original = json.loads(raw)
        if any(original.get('identity', {}).get(k) != harness.identity[k] for k in IDENTITY):
            raise ValueError('stale_environment_identity')
        reject_privileged(original, harness.observation_profile)
        if action_feedback:
            if original.get('schema_version') != 'action-trajectory-feedback-v1' or not isinstance(original.get('paths'), dict):
                raise ValueError('invalid_action_trajectory_feedback')
            available_paths = [name for name in original['paths']
                if name in ('measured', 'planned') or re.fullmatch(r'local_[0-9]+', name)]
            path_kind = args.get('path_kind', 'measured')
            if path_kind not in available_paths:
                raise ValueError('action_trajectory_path_unavailable:' + path_kind
                    + '; available_paths=' + ','.join(available_paths))
            data = original['paths'][path_kind]
            expected_source = ('measured_action_trajectory' if path_kind == 'measured' else
                'recorded_planned_commands_not_measurement' if path_kind == 'planned' else
                'unexecuted_arm_candidate_not_synchronized')
            if not isinstance(data, dict) or data.get('source_kind') != expected_source:
                raise ValueError('invalid_action_trajectory_path_source')
            if any(data.get('identity', {}).get(k) != harness.identity[k] for k in IDENTITY):
                raise ValueError('stale_environment_identity')
            if any(data.get(key) != original.get(key) for key in
                   ('observation_id', 'action_id', 'frame_id', 'tcp_config_id')):
                raise ValueError('action_trajectory_path_binding_mismatch')
        else:
            if 'path_kind' in args:
                raise ValueError('trajectory_path_kind_requires_action_feedback')
            data = original
            if data.get('source_kind') != 'candidate_command_trajectory_not_measurement':
                raise ValueError('not_a_candidate_trajectory')
        samples = data['samples']
        if not isinstance(samples, list) or not samples:
            raise ValueError('trajectory_path_has_no_samples')
        by_step = {s['native_step']: s for s in samples}
        next_step = None
        if paged:
            start, limit = args.get('start_native_step', 0), args.get('max_samples', 25)
            if type(start) is not int or start not in by_step:
                raise ValueError('trajectory_start_native_step_must_exist')
            if type(limit) is not int or not 1 <= limit <= 25:
                raise ValueError('trajectory_max_samples_requires_one_to_twenty_five')
            ordered = sorted(by_step)
            offset = ordered.index(start)
            steps = ordered[offset:offset+limit]
            if offset+len(steps) < len(ordered):
                next_step = ordered[offset+len(steps)]
        elif 'native_steps' in args:
            steps = args['native_steps']
        else:
            steps = list(dict.fromkeys([samples[0]['native_step'], samples[len(samples)//2]['native_step'], samples[-1]['native_step']]))
        if (not isinstance(steps, list) or not 1 <= len(steps) <= 25
                or any(type(i) is not int or i not in by_step for i in steps) or len(set(steps)) != len(steps)):
            raise ValueError('trajectory_requires_one_to_twenty_five_existing_steps')
        selected = [deepcopy(by_step[i]) if include_geometry else dict(
            {key: deepcopy(by_step[i][key]) for key in ('native_step', 'sim_step', 'elapsed_time_s', 'segment_index', 'execution_state') if key in by_step[i]},
            arms={arm: {k: deepcopy(command[k]) for k in
                ('q_rad', 'qdot_rad_s', 'measured_ee_pose', 'derived_motion', 'jaw_goal_normalized', 'jaw_command_normalized', 'measured_jaw', 'phase') if k in command}
                for arm, command in by_step[i]['arms'].items()}) for i in steps]
        result = {k: deepcopy(data[k]) for k in ('source_kind', 'observation_id', 'label', 'frame_id',
            'tcp_config_id', 'joint_names', 'native_dt_s', 'timing', 'measurement_timing', 'executed_native_steps', 'arm',
            'compiled_sim_step', 'segment_index', 'waypoint_index', 'stage_id', 'completed_rows',
            'remaining_rows', 'execution_uncertain', 'solver_stage', 'solver_success') if k in data}
        result.update(trajectory_ref=args['trajectory_ref'], current=bool(harness.latest and data['observation_id'] == harness.latest['observation_id']),
            samples=selected, total_samples=len(samples), next_native_step=next_step,
            selection_mode='page' if paged else 'native_steps' if 'native_steps' in args else 'key_samples',
            detail=detail, include_geometry=include_geometry,
            limitations='Candidate commands, not future measurements. Execution replans. Robot-model geometry excludes unknown object/contact truth.')
        if cached_action:
            result.update(execution_binding=data.get('execution_binding'),
                limitations='Cached commands, not future measurements. Execution uses the exact cached rows or stops. '
                    'Robot-model geometry excludes unknown object/contact truth.')
        if action_feedback:
            result.update(path_kind=path_kind, available_paths=available_paths,
                action_id=original.get('action_id'), execution_start_observation_id=original['observation_id'],
                historical=not result['current'],
                native_boundary_alignment=original.get('summary', {}).get('native_boundary_alignment'),
                recorded_limitations=deepcopy(original.get('summary', {}).get('limitations', [])),
                limitations='Archived ' + expected_source + '; timing belongs to this stored path. '
                    'RGB/camera pose belongs to execution start, not later scene state. '
                    'No grasp retention, task success, object/contact truth or synchronized local future path is inferred.')
        if include_geometry:
            result.update({k: deepcopy(data[k]) for k in ('geometry', 'inter_arm_proximity', 'robot_model',
                'hardware_provenance', 'T_world_bases', 'base_source') if k in data})
        images = []
        if args.get('camera_id') is not None:
            if len(steps) > 3:
                raise ValueError('trajectory_overlay_requires_one_to_three_existing_steps')
            if action_feedback:
                result['overlay'], images = _trajectory_image(harness, data, args['camera_id'], steps, require_current=False)
                result['overlay'].update(historical=not result['current'], current=result['current'],
                    execution_start_observation_id=original['observation_id'])
            else:
                result['overlay'], images = _trajectory_image(harness, data, args['camera_id'], steps, require_current=not cached_action)
        harness.store.event('motion.trajectory_inspected', result, correlation, world_kind='internal')
    output = ToolOutput(result, extra_content=tuple(images), projection="owner_record")
    return output.content_parts() if structured else output.content_items()


def contact_overlay(harness, args, correlation, *, structured=False):
    import cv2
    import numpy as np
    from PIL import Image
    from ...perception.overlays.contact_overlay import render_contact_overlay
    from ...core.storage.images import encode_image, image_url
    if set(args) - {'observation_id', 'camera_id', 'arms', 'selections', 'targets'}:
        raise ValueError('invalid_contact_overlay_fields')
    if not has_calibration(harness.observation_profile):
        raise ValueError('contact_overlay_requires_calibration')
    with harness.lock:
        camera = harness.geometry.camera(harness.identity, args['observation_id'], args['camera_id'])
        frame = camera['frame']
        _, data = harness.store.resolve_image(frame['rgb_ref'], allowed_scopes=('actor',))
        raw = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if raw is None:
            raise ValueError('contact_overlay_rgb_unavailable')
        overlay, metadata = render_contact_overlay(camera, raw, arms=args.get('arms', ['left', 'right']),
            selections=args.get('selections'), targets=args.get('targets'))
        encoded, _ = encode_image(Image.fromarray(cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB)),
                                  harness.image_settings, storage=True)
        reference = harness.store.put_artifact(encoded, 'contact_overlay', 'image/jpeg')
        metadata.update(overlay_ref=reference, observation_id=args['observation_id'], camera_id=args['camera_id'],
                        current=bool(harness.latest and harness.latest['observation_id'] == args['observation_id']))
        from ...core.presentation.observation_output import saved_observation_state
        metadata['observation_state'] = saved_observation_state(harness.store, args['observation_id'])
        reject_privileged(metadata, harness.observation_profile)
        harness.store.event('grasp.contact_overlay', metadata, correlation, world_kind='internal')
    url, _ = image_url(encoded, harness.image_settings)
    content = [StructuredText(metadata, projection='contact_overlay'), dict(type='inputImage', imageUrl=url)]
    return content if structured else wire_content(content)
