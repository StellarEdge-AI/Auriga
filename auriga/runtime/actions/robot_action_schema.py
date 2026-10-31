"""robot_action input schema and the field help delivered with it; dispatch and planning live in unified_actions."""

from copy import deepcopy

from ...robot.motion.grip_guard import DEFAULT_MAX_APERTURE_M
from ...robot.motion.motion_tolerance import MAX_ORIENTATION_TOLERANCE_RAD, MAX_POSITION_TOLERANCE_M, MAX_TRACKING_DIVERGENCE_M, MAX_UPWARD_DZ_M, MIN_TRACKING_DIVERGENCE_M
from ...robot.model.grasp_contact import MIN_CONTACT_DEPTH_M, MAX_CONTACT_DEPTH_M
from ...robot.model.roboprobe_pose import ANGLE_BOUNDS


_NUMBER = {'type': 'number'}
_POSE = {'type': 'array', 'items': _NUMBER, 'minItems': 3, 'maxItems': 3}
_BOX_POSE = {'type': 'array', 'items': _NUMBER, 'maxItems': 3}
_BOX = dict(type='object', properties={
    'min_m': _BOX_POSE, 'max_m': _BOX_POSE, 'margin_m': {'type':'number','minimum':0,'maximum':.2},
    'evidence': {'type':'string','minLength':1,'maxLength':1000}})
_STAGE_COMMON = {
    'id': {'type': 'string', 'minLength': 1, 'maxLength': 80},
    'arm': {'type': 'string', 'enum': ['left', 'right'],
        'description': 'Required for move/gripper. For observe, omit to wait for both arms\' prior stages; name an arm to pause both when that arm reaches this checkpoint.'},
    'depends_on': {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True,
        'description': 'Stage IDs that must finish before this stage starts. Same-arm order is automatic; add cross-arm dependencies only when physically needed. Independent left/right stages need none.'}}
_JAW_SPEED = {
    'gripper_speed_scale': {'type': 'number', 'minimum': .25, 'maximum': 1},
    'gripper_speed_preset': {'type': 'string', 'enum': ['slower', 'slow', 'normal']}}
_GRIP_GUARD_SCHEMA = {
    'grip_guard_preset': {'type': 'string', 'enum': ['none', 'non_empty', 'thin_object', 'rigid_object', 'wide_object']},
    'min_aperture_m': {'type': 'number', 'minimum': 0, 'maximum': DEFAULT_MAX_APERTURE_M},
    'max_aperture_m': {'type': 'number', 'minimum': 0, 'maximum': DEFAULT_MAX_APERTURE_M}}
_JOINT_TRAVEL_SCHEMA = {
    'joint_travel_preset': {'type': 'string', 'enum': ['standard', 'extended', 'unconstrained']},
    'max_joint_travel_rad': {'type': 'number', 'exclusiveMinimum': 0},
    'max_backtracking_rad': {'type': 'number', 'exclusiveMinimum': 0}}


def _exclusive_fields(first, second):
    return {'not': {'required': [first, second]}}


def _stage_schema(kind, properties, required=(), **constraints):
    return dict(type='object', additionalProperties=False,
        required=['id', 'kind', *required],
        properties={field: deepcopy(value) for field, value in
            dict(_STAGE_COMMON, kind={'const': kind}, **properties).items()}, **constraints)


# Both the public schema and compiler field admission use these declarations.
# Runtime state, dependencies, capabilities and relational numeric checks stay
# with the existing action/batch owners.
_STAGE_SCHEMAS = {
    'move': _stage_schema('move', {
        'grasp_position_m': _POSE,
        'angles_deg': dict(type='array', minItems=3, maxItems=3,
            prefixItems=[dict(type='number', minimum=lower, maximum=upper)
                for lower, upper in ANGLE_BOUNDS.values()], items=_NUMBER),
        'position_m': _POSE,
        'quaternion_xyzw': {'type': 'array', 'items': _NUMBER, 'minItems': 4, 'maxItems': 4},
        'contact_region': {'type': 'string', 'enum': ['pad_center', 'finger_middle', 'fingertip']},
        'contact_depth_m': {'type': 'number', 'minimum': MIN_CONTACT_DEPTH_M, 'maximum': MAX_CONTACT_DEPTH_M},
        'speed_scale': {'type': 'number', 'minimum': .25, 'maximum': 2},
        'speed_preset': {'type': 'string', 'enum': ['slower', 'slow', 'careful', 'normal', 'fast', 'faster']},
        **_JAW_SPEED,
        'gripper_goal_normalized': {'type': 'number', 'minimum': 0, 'maximum': 1},
        'gripper_motion': dict(type='object', additionalProperties=False, required=['start_fraction', 'end_fraction'],
            properties={'start_fraction': {'type': 'number', 'minimum': 0, 'exclusiveMaximum': 1},
                'end_fraction': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1}}),
        'checkpoint_after_gripper': {'type': 'boolean'},
        **_GRIP_GUARD_SCHEMA,
        **_JOINT_TRAVEL_SCHEMA,
        'tolerance_preset': {'type': 'string', 'enum': ['tight', 'normal', 'relaxed', 'compliant', 'coarse', 'unconstrained_orientation']},
        'position_tolerance_m': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': MAX_POSITION_TOLERANCE_M},
        'orientation_tolerance_rad': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': MAX_ORIENTATION_TOLERANCE_RAD},
        'max_upward_dz_m': {'type': 'number', 'minimum': 0, 'maximum': MAX_UPWARD_DZ_M},
        'stall_guard_preset': {'type': 'string', 'enum': ['off', 'contact_stop', 'strict']},
        'max_tracking_divergence_m': {'type': 'number', 'minimum': MIN_TRACKING_DIVERGENCE_M, 'maximum': MAX_TRACKING_DIVERGENCE_M},
        'clearance_preset': {'type': 'string', 'enum': ['none', 'low', 'normal', 'high']},
        'clearance_height_m': {'type': 'number', 'minimum': 0, 'maximum': 0.25},
    }, required=['arm'], oneOf=[
        {'required': ['grasp_position_m', 'angles_deg'],
            'not': {'anyOf': [{'required': ['position_m']}, {'required': ['quaternion_xyzw']}]}},
        {'required': ['position_m', 'quaternion_xyzw'],
            'not': {'anyOf': [{'required': [field]} for field in
                ('grasp_position_m', 'angles_deg', 'contact_region', 'contact_depth_m')]}}],
        dependentRequired={field: ['gripper_goal_normalized'] for field in
            ('gripper_motion', 'gripper_speed_scale', 'gripper_speed_preset', 'checkpoint_after_gripper')},
        allOf=[_exclusive_fields('contact_region', 'contact_depth_m'),
            _exclusive_fields('speed_scale', 'speed_preset'),
            _exclusive_fields('gripper_speed_scale', 'gripper_speed_preset'),
            _exclusive_fields('clearance_preset', 'clearance_height_m')]),
    'gripper': _stage_schema('gripper', {
        'goal_normalized': {'type': 'number', 'minimum': 0, 'maximum': 1},
        'checkpoint_after_gripper': {'type': 'boolean'}, **_GRIP_GUARD_SCHEMA, **_JAW_SPEED},
        required=['arm', 'goal_normalized'],
        allOf=[_exclusive_fields('gripper_speed_scale', 'gripper_speed_preset')]),
    'observe': _stage_schema('observe', {
        'reason': {'type': 'string', 'minLength': 1, 'pattern': r'\S'}}, required=['reason'])}
_STAGE_FIELD_HELP = {
    'id': 'Unique ID within this batch; referenced by depends_on and returned stage results.',
    'grasp_position_m': 'Move only: target center between fingers at the selected contact section [x,y,z] in env_origin metres, paired with angles_deg. This is not either individual finger surface. Tool offset is converted once; do not add a manual offset.',
    'angles_deg': 'Move only: [pitch,roll,yaw] extrinsic world +x,+y,+z rotations from the top-down reference, in degrees; [0,0,0] points tool +x down world -z, jaw opening along world -x. Bounds [-180,180], [-90,90], [-180,180]; not absolute Euler angles. The requested orientation is preserved during planning; if infeasible, explicitly choose different angles and preview again.',
    'position_m': 'Move only: target link6/flange [x,y,z] in env_origin metres, paired with quaternion_xyzw; mutually exclusive with grasp pose/contact fields.',
    'quaternion_xyzw': 'Move only: link6/flange orientation [x,y,z,w], a unit quaternion in env_origin; paired with position_m.',
    'contact_region': 'Grasp move only: pad_center (default, fingertips 7.5 mm beyond target along tool +x), finger_middle (27.5 mm toward palm, fingertips 35 mm beyond target), or fingertip (at target). Selects a longitudinal section, not a contacting finger or proof of object contact; keep fingertips above rigid supports on top-down grasps. Choose this or contact_depth_m.',
    'contact_depth_m': 'Grasp move only: custom depth from pad_center in metres [-0.0075,0.0625], positive toward palm; choose this or contact_region.',
    'speed_scale': 'Move only: arm trajectory speed multiplier [0.25,2]; choose this or speed_preset. Omission uses run default.',
    'speed_preset': 'Move only: named arm speed; slower=0.5, slow=0.8, careful=0.9, normal=1, fast=1.25, faster=1.5. Choose this or speed_scale.',
    'gripper_speed_scale': 'Jaw speed multiplier [0.25,1], default 1; choose this or gripper_speed_preset. On move requires gripper_goal_normalized.',
    'gripper_speed_preset': 'Jaw speed: slower=0.5, slow=0.8 or normal=1; choose this or gripper_speed_scale. On move requires gripper_goal_normalized.',
    'gripper_goal_normalized': 'Move only: jaw target [0,1], 0 closed and 1 open; omission preserves the last commanded goal, not the measured opening or a guaranteed grip force. Runs after measured arrival unless gripper_motion is supplied. A tracking failure stops the batch before this after-arrival command.',
    'gripper_motion': 'Move only: synchronize jaw motion over start_fraction..end_fraction of this move, with start < end; requires gripper_goal_normalized.',
    'checkpoint_after_gripper': 'Move (with gripper_goal_normalized) or gripper stage only: optional on-demand boolean (default inherits batch checkpoint_after_gripper, which defaults to false). Set true to unconditionally pause both arms at the stage boundary before dependent move stages so you can inspect RGB and grip_facts and resume the cached plan_id.',
    'grip_guard_preset': 'Move or gripper stage: optional aperture guard preset (none, non_empty [min 0.002m], thin_object [min 0.0005m], rigid_object [min 0.004m], wide_object [min 0.015m]). Choose from object thickness/rigidity: use rigid_object/wide_object (or min_aperture_m) on rigid blocks/bottles/tubes so an empty closure pauses automatically while a valid grasp continues; use thin_object or none on deformable cloth/thin sheets where a pinched jaw closes nearly to 0 m.',
    'min_aperture_m': 'Move or gripper stage: optional minimum expected fingertip aperture in metres [0, 0.088] (full open 88mm, closed 0m; default none/disabled). Pauses with aperture_below_min_m if measured_aperture_m < min_aperture_m; overrides preset lower bound. Note that thin parts (<=3mm such as coins, cards, thin rims or cloth) can read 0.000m when firmly held, so omit min_aperture_m for thin parts.',
    'max_aperture_m': 'Move or gripper stage: optional maximum expected fingertip aperture in metres [0, 0.088] (full open 88mm, closed 0m). Pauses with aperture_above_max_m if measured_aperture_m > max_aperture_m; overrides preset upper bound.',
    'joint_travel_preset': 'Move only: optional joint-travel admission preset: standard (default, total<=pi or excess<pi/2), extended (total<=1.5*pi or excess<pi for wide-arc sweeps, large orientation flips or deformable folding), or unconstrained (disables travel/backtracking rejection while keeping step-budget checks).',
    'max_joint_travel_rad': 'Move only: optional custom per-joint total travel threshold in radians (>0) before backtracking/winding checks apply; overrides joint_travel_preset.',
    'max_backtracking_rad': 'Move only: optional custom per-joint backtracking/periodic-winding excess threshold in radians (>0); overrides joint_travel_preset.',
    'goal_normalized': 'Gripper stage only, required: jaw target [0,1], 0 closed and 1 open. Command completion and measured aperture do not establish object retention. Use this exact field, not gripper, goal, or gripper_goal_normalized.',
    'tolerance_preset': 'Move only: named arrival tolerance preset; tight=(0.003 m, 0.05 rad) strictly for final millimeter-clearance pin/slot insertion (avoid tight on tabletop grasps or pushing where PD droop/friction is ~4-6mm), normal=(0.010 m, 0.15 rad, default; requires <=10 mm horizontal alignment with vertical contact/load compliance), relaxed/compliant=(0.015 m, 0.20 rad) for compliant placement, coarse=(0.025 m, 0.35 rad) for large-clearance transit/retract, unconstrained_orientation=(0.015 m, 3.14159 rad) when wrist orientation around a symmetric target is free. Explicit position_tolerance_m, orientation_tolerance_rad or max_upward_dz_m overrides the corresponding component.',
    'position_tolerance_m': 'Move only: measured link6 arrival position tolerance in metres, positive and at most 0.10. Overrides the position component of tolerance_preset (default normal=0.010 m). Tracking failure stops later stages, including gripper commands, and returns endpoint_residual_m={dx_m,dy_m,dz_m,position_error_m,orientation_error_rad}, first_divergence, contact_block_summary, in_hand_tracking_error (true when an object remains held between the fingers at a blocked pose) and next_action.',
    'orientation_tolerance_rad': 'Move only: measured link6 arrival orientation tolerance in radians, in (0, pi]. Overrides the orientation component of tolerance_preset (default normal=0.15 rad).',
    'max_upward_dz_m': 'Move only: optional vertical Z compliance window override in metres [0, 0.15] when horizontal (dx, dy) and orientation are within tolerance and measured Z rests above target (+dz_m <= max_upward_dz_m). Defaults when omitted: 0.040 m with stall_guard_preset="contact_stop" (0.022 m on open-jaw pre-grasp), max(1.2*pos_tol, 0.025 m) on closed-jaw moves, 0.018 m on normal open-jaw moves, and 0 m on tight moves without contact_stop. Omit to use the preset default, set 0 for strict 3D Z arrival, or set a larger value (e.g. 0.05-0.08 m) when stacking/seating high above target Z. Downward error remains bounded by position_tolerance_m; enabling vertical compliance does not relax orientation_tolerance_rad. Resolved effective_tolerances are reported in stage_results.',
    'stall_guard_preset': 'Move only: real-time tracking divergence early-stop preset; off (default for batch, free-space moves, and pressing spring-loaded levers through resistance), contact_stop (0.020 m stall trigger during motion; stops at first support/object contact when horizontal (dx, dy) is within tolerance and holds the contact pose so attached gripper stages execute), or strict (0.010 m).',
    'max_tracking_divergence_m': 'Move only: custom real-time tracking divergence threshold in metres [0.005, 0.15]; overrides stall_guard_preset.',
    'clearance_preset': 'Move only: optional obstacle-crossing vertical clearance preset (none=0 m, low=0.09 m, normal=0.13 m default, high=0.18 m) when obstacles are supplied; mutually exclusive with clearance_height_m.',
    'clearance_height_m': 'Move only: optional custom obstacle-crossing vertical clearance height in metres [0, 0.25] above obstacle top when obstacles are supplied; mutually exclusive with clearance_preset.',
    'reason': 'Observe stage only, required nonblank text: what evidence to inspect at this checkpoint.'}
for _kind, _schema in _STAGE_SCHEMAS.items():
    for _field, _help in _STAGE_FIELD_HELP.items():
        if _field in _schema['properties']:
            _schema['properties'][_field]['description'] = _help
    _schema['properties']['kind']['description'] = 'Stage type: ' + _kind + '.'
_STAGE_SCHEMAS['move']['properties']['gripper_motion']['properties']['start_fraction']['description'] = 'Start fraction of move in [0,1), strictly before end_fraction.'
_STAGE_SCHEMAS['move']['properties']['gripper_motion']['properties']['end_fraction']['description'] = 'End fraction of move in (0,1], strictly after start_fraction.'
# Discoverable fields are flat; the same conditional branches still enforce kind-specific admission.
_STAGE_PRESET_ENUM_OMIT = frozenset({
    'tolerance_preset', 'grip_guard_preset', 'stall_guard_preset', 'joint_travel_preset',
    'clearance_preset', 'speed_preset', 'gripper_speed_preset', 'contact_region',
})
_STAGE_MIN_ITEMS_OMIT = frozenset({'grasp_position_m', 'position_m', 'quaternion_xyzw'})
_STAGE = dict(type='object', required=['kind'],
    properties={**{field: {k: deepcopy(v) for k, v in value.items()
            if not (k == 'enum' and field in _STAGE_PRESET_ENUM_OMIT)
            and not (k == 'minItems' and field in _STAGE_MIN_ITEMS_OMIT)
            and k != 'minLength'}
        for schema in _STAGE_SCHEMAS.values()
        for field, value in schema['properties'].items()},
        'kind': {'type': 'string', 'enum': list(_STAGE_SCHEMAS),
            'description': 'move requires arm and one complete pose pair; gripper requires arm and goal_normalized; observe requires reason. joint/home/reset are not supported kinds.'}},
    allOf=[{'if': {'properties': {'kind': {'const': kind}}, 'required': ['kind']},
        'then': schema} for kind, schema in _STAGE_SCHEMAS.items()])


def _validate_stage(stage):
    from ..tools.registry import validator_for
    issue = next(validator_for(_STAGE_SCHEMAS[stage['kind']]).iter_errors(stage), None)
    if issue is not None:
        raise ValueError('invalid_robot_action_stage: ' + issue.message)

ROBOT_ACTION_TOOL = dict(type='function', name='robot_action', description=(
    'Unified bounded robot action. Default mode=preview plans the COMPLETE single/dual-arm stage batch without physics, '
    'returns plan_id, cost, exact trajectory and calibrated robot-geometry evidence. Review it, then call this SAME tool '
    'with mode=execute and plan_id to execute those exact cached rows. No silent replanning or changed commands. '
    'mode=execute_if_clean (or auto_execute_if_clean=true) plans the complete batch, runs motion_safety, grasp_3d_review and geometry_review, '
    'and immediately executes in the SAME call when all checks pass with zero warnings/risks (automatically returning the preview plan_id without moving if any warning or risk is detected). '
    'mode=direct explicitly previews and immediately executes without returning for model review. '
    'Optional plan_update submits or updates task_plan inline in the same call without a separate task_plan round trip. '
    'stages are ordered per arm; IDs are unique and depends_on adds cross-arm completion dependencies. '
    'move uses grasp_position_m in env_origin metres and angles_deg=[pitch,roll,yaw] world rotations from the top-down '
    'reference, not conventional absolute Euler angles. Choose contact_region/contact_depth_m at each stage. '
    'Alternatively move uses position_m + quaternion_xyzw for a measured link6/flange target. Supply exactly one complete pose pair. '
    'Routes use successive move stages, not via fields. There are no joint/home/reset kinds; a return uses saved measured poses. '
    'Every move has its own arm speed and optional gripper_goal_normalized/speed; gripper_motion fractions synchronize that jaw '
    'within THIS move. Without fractions its jaw runs after measured arrival. A gripper stage requires goal_normalized (0 closed, 1 open), not gripper or goal. '
    'observe requires reason and stops BOTH arms; omit arm for a barrier after both prior arm stages, or name an arm '
    'to checkpoint when only that arm arrives. Later stages remain cached. After inspecting RGB, execute the same '
    'plan_id to continue only if its current-state validity check passes; otherwise preview a new plan. '
    'Optional checkpoint_after_gripper=true (on a grasp/gripper stage or the batch) pauses after a jaw stage '
    'before the next move stage (pre-lift) to inspect finger contact and grip_facts in RGB before continuing '
    'the cached plan_id; or use grip_guard_preset (non_empty [>=2mm], thin_object [>=0.5mm], rigid_object [>=4mm], wide_object [>=15mm], none) and '
    'min_aperture_m/max_aperture_m [0..0.088m] to conditionally pause only when measured_aperture_m falls outside '
    'the expected bounds (valid grasps continue into lift without pausing). Use tolerance_preset (tight, normal, relaxed, coarse, unconstrained_orientation) '
    'or position_tolerance_m/orientation_tolerance_rad per stage, and set stall_guard_preset="contact_stop" specifically on contact descent/press stages '
    '(keep batch default "off" for free-space transits). Use joint_travel_preset (standard, extended, unconstrained) or '
    'max_joint_travel_rad/max_backtracking_rad on move stages or the batch when legitimate large wrist rotations need wider travel thresholds. '
    'Independent arm sequences overlap. Express paired barriers using depends_on; arrival/closed-jaw commands do not '
    'prove contact, support or retention. Shared rows cost one native step; the complete batch retains one 250-step '
    'budget including resumed portions. mode=inspect accepts plan_id for cached evidence, action_id for executed '
    'measured/planned paths, or trajectory_ref for an existing artifact; path_kind selects measured/planned. '
    'Supply observed/estimated obstacles world AABBs '
    'and held_objects={left/right: current world AABB} for '
    'full-timeline proxy clearance estimates; contact declarations annotate overlaps without suppressing risks. '
    'Auxiliary review is reserved; the main model owns execution approval. Geometry risks are advisory, not a '
    'joint-collision, environment-collision or shared-object constraint proof. '
    'Usage: preview/execute_if_clean/direct require current observation_id and either 1..32 stages, source_plan_id + start_from_stage_id (with optional stage_overrides to recompile remaining stages from current measured state), or plan_id + skip_failed_stage/partial stages (from repair_action); execute accepts plan_id, optional action_id and expected_cursor/expected_revision; '
    'inspect requires exactly one plan_id/action_id/trajectory_ref plus inspection options. '
    'Example checkpoint preview: {"observation_id":"obs1","stages":[{"id":"check","kind":"observe","reason":"inspect current support"}]}. '
    'After reviewing a returned plan: {"mode":"execute","plan_id":"p1","expected_cursor":0,"expected_revision":0}. '
    'Explicit continuation after a paused/failed stage: {"mode":"preview","observation_id":"obs2","source_plan_id":"p1","start_from_stage_id":"carry","stage_overrides":{"carry":{"position_tolerance_m":0.015}}} or {"mode":"preview","observation_id":"obs2","plan_id":"p1","skip_failed_stage":true}. '
    'Gripper stage syntax: {"id":"open","kind":"gripper","arm":"left","goal_normalized":1}; choose the goal from the intended action.'), inputSchema=dict(
    type='object', additionalProperties=False, properties={
        'mode': {'type': 'string', 'enum': ['preview', 'execute', 'inspect', 'direct', 'execute_if_clean']},
        'auto_execute_if_clean': {'type': 'boolean'},
        'plan_update': {'type': 'object'},
        'observation_id': {'type': 'string'}, 'action_id': {'type': 'string'}, 'plan_id': {'type': 'string'},
        'source_plan_id': {'type': 'string', 'minLength': 1},
        'start_from_stage_id': {'type': 'string', 'minLength': 1},
        'stage_overrides': {'type': 'object'},
        'skip_failed_stage': {'type': 'boolean'},
        'expected_cursor': {'type':'integer','minimum':0},
        'expected_revision': {'type':'integer','minimum':0},
        'trajectory_ref': {'type':'string'}, 'path_kind': {'type':'string'},
        'stages': {'type': 'array', 'items': _STAGE, 'minItems': 1, 'maxItems': 32,
            'description': '1..32 stages total, at most 16 per arm, on one shared execution timeline. Include both arms here for simultaneous motion. List order applies within each arm, not across arms; a global observe creates a barrier between preceding and following stages. A move with an after-arrival jaw goal expands into a motion and a jaw stage and consumes two stage slots.'},
        'checkpoint_after_gripper': {'type': 'boolean'},
        'grip_guard_preset': {'type': 'string', 'minLength': 1},
        'min_aperture_m': {'type': 'number', 'minimum': 0, 'maximum': DEFAULT_MAX_APERTURE_M},
        'max_aperture_m': {'type': 'number', 'minimum': 0, 'maximum': DEFAULT_MAX_APERTURE_M},
        'joint_travel_preset': {'type': 'string', 'minLength': 1},
        'max_joint_travel_rad': {'type': 'number', 'exclusiveMinimum': 0},
        'max_backtracking_rad': {'type': 'number', 'exclusiveMinimum': 0},
        'tolerance_preset': {'type': 'string', 'minLength': 1},
        'position_tolerance_m': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': MAX_POSITION_TOLERANCE_M},
        'orientation_tolerance_rad': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': MAX_ORIENTATION_TOLERANCE_RAD},
        'max_upward_dz_m': {'type': 'number', 'minimum': 0, 'maximum': MAX_UPWARD_DZ_M},
        'stall_guard_preset': {'type': 'string', 'minLength': 1},
        'max_tracking_divergence_m': {'type': 'number', 'minimum': MIN_TRACKING_DIVERGENCE_M, 'maximum': MAX_TRACKING_DIVERGENCE_M},
        'timeout_s': {'type': 'number', 'minimum': .1, 'maximum': 300},
        'settle': dict(type='object', properties={
            'camera_id': {'type': 'string'},
            'roi': {'type': 'array', 'items': {'type':'integer'}, 'maxItems': 4},
            'max_native_steps': {'type': 'integer', 'minimum': 1, 'maximum': 50},
            'sample_steps': {'type': 'integer', 'minimum': 1, 'maximum': 10},
            'stable_samples': {'type': 'integer', 'minimum': 2, 'maximum': 5},
            'max_motion_px': {'type': 'number', 'minimum': 0, 'maximum': 5}}),
        'obstacles': {'type':'array','maxItems':40,'items': dict(_BOX,
            properties=dict(_BOX['properties'],id={'type':'string','minLength':1,'maxLength':160}))},
        'obstacle_object_ids': {'type': 'array', 'maxItems': 40, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 80}},
        'obstacle_policy': {'type': 'string', 'minLength': 1},
        'ignore_obstacle_ids': {'type': 'array', 'maxItems': 40, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 160}},
        'target_exemption_preset': {'type': 'string', 'minLength': 1},
        'target_exemption_radius_m': {'type': 'number', 'minimum': 0, 'maximum': 0.15},
        'clearance_preset': {'type': 'string', 'minLength': 1},
        'clearance_height_m': {'type': 'number', 'minimum': 0, 'maximum': 0.25},
        'held_objects': dict(type='object',properties={'left':_BOX,'right':_BOX}),
        'held_object': dict(_BOX,properties=dict(_BOX['properties'],arm={'type':'string','enum':['left','right']})),
        'contact_entity_id': {'type':'string'},
        'initial_contact': dict(type='object',properties={
            'entity_ids':{'type':'array','items':{'type':'string'}},'evidence':{'type':'string','minLength':1}}),
        'placement_contact': dict(type='object',properties={
            'entity_id':{'type':'string'},'evidence':{'type':'string','minLength':1},
            'max_penetration_m':{'type':'number','minimum':0,'maximum':.003}}),
        'camera_id': {'type': 'string'}, 'detail': {'type': 'string', 'enum': ['summary', 'full']},
        'native_steps': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1, 'maxItems': 3},
        'start_native_step': {'type': 'integer', 'minimum': 0},
        'max_samples': {'type': 'integer', 'minimum': 1, 'maximum': 25}}))

_ACTION_FIELD_HELP = {
    'mode': 'preview (default) plans without physics; execute_if_clean plans and immediately executes when motion_safety, grasp_3d_review and geometry_review have zero warnings/risks (otherwise returns preview plan_id for inspection); execute runs cached plan_id; direct plans and immediately executes; inspect reads saved trajectories.',
    'auto_execute_if_clean': 'Preview/execute_if_clean only: optional boolean (default false). When true, automatically executes the planned batch in the same call if status is planned, motion_safety is pass, grasp_3d_review has no warnings/risk_flags, and geometry_review has no risks; otherwise returns the preview plan_id for inspection.',
    'plan_update': 'Optional inline task_plan payload {plan?, attention?, ledger?}: atomically submits (if no task_plan exists yet) or updates task_plan in the same call without a separate task_plan round trip.',
    'observation_id': 'Required for preview/execute_if_clean/direct: actual current observation ID grounding the complete batch.',
    'action_id': 'Optional caller-chosen unique execution ID for preview/direct/execute; inspect selects this existing executed action. Never reuse for different commands.',
    'plan_id': 'Required for execute; inspect may select it. Use the ID returned by preview in this active run; only 16 recent plans remain cached.',
    'source_plan_id': 'Preview/direct only: existing plan_id whose stage definitions are reused for explicit continuation from start_from_stage_id. Mutually exclusive with stages.',
    'start_from_stage_id': 'Preview/direct with source_plan_id: stage ID in source_plan_id to start recompiling from (inclusive of this stage and all subsequent stages) using the current observation_id measured state.',
    'stage_overrides': 'Preview/direct with source_plan_id: optional object {stage_id: {field: value}} overriding target_pose/pose fields, position_tolerance_m, speed_preset, etc. on selected remaining stages without rewriting all stages.',
    'skip_failed_stage': 'Preview/direct with plan_id only: optional boolean (default false). Set true to skip the failed stage and recompile remaining uncompleted stages from current measured state.',
    'expected_cursor': 'Execute only: expected executed native-step count; use next_action returned by preview/checkpoint. Omission uses current cached cursor.',
    'expected_revision': 'Execute only: expected plan revision; use returned next_action. Omission uses current cached revision.',
    'trajectory_ref': 'Inspect only: existing actor-accessible trajectory artifact reference; mutually exclusive with plan_id/action_id.',
    'path_kind': 'Inspect action feedback only: measured (default) or planned, if listed in available_paths. Omit for preview trajectory artifacts.',
    'checkpoint_after_gripper': 'Preview/direct only: optional boolean (default false). Set true to unconditionally pause at stage boundaries after jaw stages before dependent move stages unless overridden on a stage.',
    'grip_guard_preset': 'Preview/direct only: optional batch-default aperture guard preset (none, non_empty [min 0.002m], thin_object [min 0.0005m], rigid_object [min 0.004m], wide_object [min 0.015m]) applied to closing jaw stages unless overridden.',
    'min_aperture_m': 'Preview/direct only: optional batch-default minimum expected fingertip aperture in metres [0, 0.088]; overridden by stage-level min_aperture_m.',
    'max_aperture_m': 'Preview/direct only: optional batch-default maximum expected fingertip aperture in metres [0, 0.088]; overridden by stage-level max_aperture_m.',
    'joint_travel_preset': 'Preview/direct only: optional batch-default joint-travel admission preset: standard (default), extended (1.5*pi total / pi excess), or unconstrained.',
    'max_joint_travel_rad': 'Preview/direct only: optional batch-default per-joint total travel threshold in radians (>0); overridden by stage-level settings.',
    'max_backtracking_rad': 'Preview/direct only: optional batch-default per-joint backtracking/winding excess threshold in radians (>0); overridden by stage-level settings.',
    'tolerance_preset': 'Preview/direct only: batch-wide default move arrival tolerance preset (tight, normal, relaxed, compliant, coarse, unconstrained_orientation); overridden by stage-level tolerance settings.',
    'position_tolerance_m': 'Preview/direct only: batch-wide default move arrival position tolerance in metres (0, 0.10]; overrides batch tolerance_preset.',
    'orientation_tolerance_rad': 'Preview/direct only: batch-wide default move arrival orientation tolerance in radians (0, pi]; overrides batch tolerance_preset.',
    'max_upward_dz_m': 'Preview/direct only: optional batch-wide default vertical Z compliance window in metres [0, 0.15]; overridden by stage-level max_upward_dz_m.',
    'stall_guard_preset': 'Preview/direct only: batch-wide default move stall guard preset (off, contact_stop, strict); overridden by stage-level stall guard settings.',
    'max_tracking_divergence_m': 'Preview/direct only: optional batch-wide default real-time tracking divergence threshold in metres [0.005, 0.15]; overrides batch stall_guard_preset.',
    'timeout_s': 'Preview/direct only: action timeout in seconds, default 120, range 0.1..300. In programs, omission is capped to remaining work time minus the 5-second action reserve and 1-second dispatch margin before preview. Explicit values and cached plans are unchanged; final admission still checks remaining time. Read-only preview remains available without execution reserve.',
    'settle': 'Preview/direct only: optional bounded RGB stability check after the complete batch, never at a checkpoint; adds separate native wait steps. Requires robot_wait and a visible ROI.',
    'obstacles': 'Preview/direct only: up to 40 observed/estimated world AABBs, each with ID and evidence, for advisory whole-timeline clearance checks.',
    'obstacle_object_ids': 'Preview/direct only: optional list of detected 3D object IDs from the current observation (e.g. ["o1", "o2"]) to convert into world AABB obstacles.',
    'obstacle_policy': 'Preview/direct only: obstacle persistence policy across actions in the episode: persist (default, retains registered obstacles), stateless (applies only to this call without reading/writing persistent obstacles), or clear (clears previously persisted obstacles first).',
    'ignore_obstacle_ids': 'Preview/direct only: optional list of obstacle IDs to exclude from obstacle checks in this batch.',
    'target_exemption_preset': 'Preview/direct only: optional XY target-neighborhood obstacle exemption preset (none=0 m, tight=0.030 m, normal=0.055 m default, wide=0.080 m); mutually exclusive with target_exemption_radius_m.',
    'target_exemption_radius_m': 'Preview/direct only: optional custom XY target-neighborhood obstacle exemption radius in metres [0, 0.15]; mutually exclusive with target_exemption_preset.',
    'clearance_preset': 'Preview/direct only: optional batch-default obstacle-crossing vertical clearance preset (none=0 m, low=0.09 m, normal=0.13 m default, high=0.18 m); overridden by stage-level settings.',
    'clearance_height_m': 'Preview/direct only: optional batch-default obstacle-crossing vertical clearance height in metres [0, 0.25] above obstacle top; overridden by stage-level settings.',
    'held_objects': 'Preview/direct only: left/right current held-object world AABBs with evidence. Choose this or held_object.',
    'held_object': 'Preview/direct only: single held-object AABB; arm required for a dual-arm batch, otherwise inferred. Choose this or held_objects.',
    'contact_entity_id': 'Preview/direct only: ID of the intended contact entity among supplied obstacles; annotates overlap without hiding risks.',
    'initial_contact': 'Preview/direct only: obstacle entity IDs and evidence explaining pre-existing contact.',
    'placement_contact': 'Preview/direct only: intended support entity ID and evidence, with optional permitted penetration estimate.',
    'camera_id': 'Overlay camera ID, default cam_head, for preview/direct or inspect. Must exist in the referenced observation.',
    'detail': 'Inspect: summary (default) or full including selected geometry. Accepted but has no effect on preview/direct.',
    'native_steps': 'Inspect only: 1..3 distinct existing native-step IDs; mutually exclusive with pagination. Default selects first/middle/last.',
    'start_native_step': 'Inspect pagination: existing native-step ID to start from, default 0. Follow returned next_native_step when present.',
    'max_samples': 'Inspect pagination: 1..25 samples per page, default 25. Mutually exclusive with native_steps.'}
for _field, _help in _ACTION_FIELD_HELP.items():
    ROBOT_ACTION_TOOL['inputSchema']['properties'][_field]['description'] = _help
_settle_help = {
    'camera_id': 'RGB camera ID, default cam_head.', 'roi': 'Target pixel [x,y,width,height]; nonnegative origin, width/height at least 4, inside the image.',
    'max_native_steps': 'Maximum additional wait steps, default 25, range 1..50.',
    'sample_steps': 'Wait steps per RGB sample, default 5, range 1..10.',
    'stable_samples': 'Required consecutive quiet comparisons, default 3, range 2..5.',
    'max_motion_px': 'Maximum tracked-feature motion in pixels for a quiet sample, default 0.5, range 0..5; not proof of physical stability.'}
for _field, _help in _settle_help.items():
    ROBOT_ACTION_TOOL['inputSchema']['properties']['settle']['properties'][_field]['description'] = _help
for _box in (ROBOT_ACTION_TOOL['inputSchema']['properties']['obstacles']['items'],
        *ROBOT_ACTION_TOOL['inputSchema']['properties']['held_objects']['properties'].values(),
        ROBOT_ACTION_TOOL['inputSchema']['properties']['held_object']):
    _box['properties'] = {field: deepcopy(value) for field, value in _box['properties'].items()}
    for _field, _help in {'min_m': 'World AABB lower [x,y,z] corner in metres.',
            'max_m': 'World AABB upper [x,y,z] corner in metres; each coordinate must exceed min_m. Both corners must lie in [-3,3] metres.',
            'margin_m': 'Nonnegative clearance inflation in metres, default 0.01, at most 0.2.',
            'evidence': 'Required nonblank description of current permitted evidence and estimation uncertainty.',
            'id': 'Unique obstacle entity ID referenced by contact declarations.',
            'arm': 'Holding arm; required when a single held_object is used with both arms.'}.items():
        if _field in _box['properties']:
            _box['properties'][_field]['description'] = _help
for _arm in ('left', 'right'):
    ROBOT_ACTION_TOOL['inputSchema']['properties']['held_objects']['properties'][_arm]['description'] = 'Current held-object world AABB for this arm, derived from permitted evidence.'
for _name in ('initial_contact', 'placement_contact'):
    for _field, _help in {'entity_ids': 'IDs of existing obstacles in pre-existing contact.',
            'entity_id': 'ID of the existing obstacle supporting placement.',
            'evidence': 'Required nonblank observed basis for the contact declaration.',
            'max_penetration_m': 'Maximum declared placement penetration in metres, default 0.002, range 0..0.003.'}.items():
        if _field in ROBOT_ACTION_TOOL['inputSchema']['properties'][_name]['properties']:
            ROBOT_ACTION_TOOL['inputSchema']['properties'][_name]['properties'][_field]['description'] = _help
ROBOT_ACTION_TOOL['inputSchema']['examples'] = [
    {'observation_id': 'obs1', 'stages': [{'id': 'check', 'kind': 'observe', 'reason': 'inspect current support'}]},
    {'mode': 'execute', 'plan_id': 'p1', 'expected_cursor': 0, 'expected_revision': 0},
    {'mode': 'inspect', 'action_id': 'a1', 'path_kind': 'measured', 'detail': 'full'}]
