"""Scene HTTP contracts shared by request validation, replies and OpenAPI.

Incoming commands are closed objects. Stored evidence replies retain optional
extensions so reading an older archive does not rewrite or discard its facts.
Geometry/observation semantic and artifact-permission checks remain in their
owners; a schema never grants observed-world execution permission.
"""
from copy import deepcopy
import math
import re

from jsonschema import Draft202012Validator, validators

from ...core.contracts import IDENTITY, OBSERVATION_PROFILES
from ...core.tool_errors import ToolInputError, error_payload, schema_input_error


def obj(properties, required=(), *, extensions=False, **rules):
    return dict(type="object", properties=properties, required=list(required),
                additionalProperties=extensions, **rules)


def array(items, minimum=0, maximum=None):
    return dict(type="array", items=items, minItems=minimum,
                **({"maxItems": maximum} if maximum is not None else {}))


def ref(name):
    return {"$ref": "#/$defs/" + name}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


STRING = {"type": "string", "minLength": 1}
TEXT = {"type": "string"}
NUMBER = {"type": "number"}
NONNEGATIVE = {"type": "number", "minimum": 0}
BOOL = {"type": "boolean"}
COUNT = {"type": "integer", "minimum": 0}
VERSION = {"const": "0.1"}
ARTIFACT = {"type": "string", "pattern": "^artifact:.+"}
DIGEST = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
V2, V3, V4 = (array(NUMBER, size, size) for size in (2, 3, 4))
M4 = array(V4, 4, 4)
POINTS = array(V3, 1, 2000)
PIXELS = array(V2, 1, 2000)
STRINGS = array(STRING)
IDENTITY_FIELDS = {key: STRING for key in IDENTITY[:-1]} | {
    "env_generation": {"type": "integer", "minimum": 1}}
BASE = {"schema_version": VERSION, **IDENTITY_FIELDS,
        "world_kind": {"const": "internal"}}
ROI = {"type": "array", "prefixItems": [COUNT, COUNT,
    {"type": "integer", "minimum": 1}, {"type": "integer", "minimum": 1}],
    "minItems": 4, "maxItems": 4}
BUDGET = obj({"wall_time_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 120},
              "queue_time_s": {"type": "number", "minimum": 0, "maximum": 600}})
EVIDENCE = {"type": "object", "description": "Algorithm-specific permitted evidence; never hidden task truth."}
SCHEMAS = {}

SCHEMAS["Artifact"] = obj(dict(artifact_id=STRING, kind=STRING, mime_type=STRING,
    byte_size=COUNT, sha256=DIGEST, storage_key=STRING,
    access_scope={"enum": ["actor", "internal"]}),
    ("artifact_id", "kind", "mime_type", "byte_size", "sha256", "storage_key", "access_scope"), extensions=True)
SCHEMAS["Camera"] = obj(dict(camera_id=STRING, frame_id=STRING, rgb_ref=ARTIFACT,
    width={"type": "integer", "minimum": 1, "maximum": 4096},
    height={"type": "integer", "minimum": 1, "maximum": 4096},
    sim_step=nullable(COUNT), step_domain=nullable(STRING), sim_time_s=nullable(NONNEGATIVE),
    pixel_transform={"type": ["string", "object"]}, calibration_ref=nullable(ARTIFACT),
    pose_sample_ref=nullable(ARTIFACT), capture_time_ref=nullable(ARTIFACT),
    depth_ref=ARTIFACT, depth_kind={"const": "z"}, depth_unit={"const": "m"},
    depth_shape=array({"type": "integer", "minimum": 1}, 2, 2), depth_clip_m=V2,
    image_encoding=EVIDENCE), ("camera_id", "frame_id", "rgb_ref", "width", "height"), extensions=True)
SCHEMAS["Observation"] = obj(dict(BASE, world_kind={"const": "observed"},
    observation_id=STRING, robot_state_ref=ARTIFACT, cameras=array(ref("Camera")),
    observation_profile={"enum": list(OBSERVATION_PROFILES)}, calibration_id=nullable(STRING),
    robot_model_id=STRING, robot_model_ref=ARTIFACT, last_action_id=nullable(STRING),
    sim_step=nullable(COUNT), step_domain=nullable(STRING), sim_time_s=nullable(NONNEGATIVE),
    clock_domain=STRING, data_origin=STRING, task_instruction=TEXT),
    ("schema_version", *IDENTITY, "world_kind", "observation_id", "robot_state_ref", "cameras"), extensions=True)
SCHEMAS["Geometry"] = {"oneOf": [
    obj(dict(kind={"const": "box"}, min_m=V3, max_m=V3), ("kind", "min_m", "max_m")),
    obj(dict(kind={"const": "plane"}, origin_m=V3, axis_u=V3, axis_v=V3,
             bounds_uv_m=array(V2, 2, 2)), ("kind", "origin_m", "axis_u", "axis_v", "bounds_uv_m"))]}
operation = dict(op=STRING, source_kind={"enum": ["observation_inference", "explicit_prior"]},
                 assumptions=STRINGS, entity_id=STRING)
operations = []
for names, properties, required, rules in (
    (("upsert_geometry",), dict(geometry=ref("Geometry"), observation_ids=array(STRING, 1),
        uncertainty={"type": ["string", "object"]}, evidence=EVIDENCE, support_points_m=array(V3),
        support_polygon_uv_m=nullable(array(V2)), association=EVIDENCE), ("entity_id", "geometry"), {}),
    (("update_pose",), dict(translation_m=V3, T_delta=M4), ("entity_id",),
        {"oneOf": [{"required": ["translation_m"]}, {"required": ["T_delta"]}]}),
    (("set_physics_hypotheses",), dict(hypotheses={"type": "object", "minProperties": 1}),
        ("entity_id", "hypotheses"), {}),
    (("mark_unknown",), dict(region={"type": ["string", "object"]}, reason=STRING), ("region", "reason"), {}),
    (("mark_lost", "retire_entity", "mark_occluded", "mark_dirty"),
        dict(reason=STRING, association=EVIDENCE), ("entity_id", "reason"), {}),
):
    operations.append(obj(dict(operation, op={"enum": list(names)}, **properties),
                          ("op", "source_kind", *required), **rules))
SCHEMAS["Patch"] = obj(dict(BASE, base_scene_version=COUNT, observation_ids=array(STRING, 1),
    builder_digest=STRING, frame_id=STRING, rebuild=BOOL, operations=array({"oneOf": operations}, 1, 64),
    data_origin=STRING, build_kind=STRING),
    ("schema_version", *IDENTITY, "base_scene_version", "observation_ids", "builder_digest", "operations"))

# Geometry computation accepts existing observation aliases, with mutually
# exclusive single/multiple references. Temporal and calibration checks need
# stored evidence and therefore still belong to GeometryTools.
GEOMETRY_FIELDS = dict(BASE, kind=STRING, observation_id=STRING,
    observation_ids=array(STRING, 1), camera_id=STRING, frame_id=STRING, budget=BUDGET)
SINGLE_OR_MULTIPLE = {"oneOf": [{"required": ["observation_id"]}, {"required": ["observation_ids"]}]}
TRACK_FIELDS = dict(camera_id_b=STRING, pixels_a=PIXELS, pixels_b=PIXELS, roi=ROI,
    tracking_method={"enum": ["lk", "sift"]}, static_scene_assumption=BOOL,
    min_angle_deg={"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 90},
    max_error_px={"type": "number", "exclusiveMinimum": 0, "maximum": 100})
pose = obj(dict(position_m=dict(V3, description="Required link6/flange position [x,y,z] in the selected calibration frame, in metres."),
    quaternion_xyzw=dict(V4, description="Required normalized orientation quaternion [x,y,z,w].")),
    ("position_m", "quaternion_xyzw"), extensions=True)
waypoint = obj(dict(left=dict(pose, description="Optional left-arm pose to project; at least one arm is required."),
    right=dict(pose, description="Optional right-arm pose to project; at least one arm is required.")), minProperties=1)
preview_action = obj(dict(frame_id=dict(STRING, description="Required calibration reference frame, normally env_origin."),
    mode={"type": "string", "const": "absolute", "description": "Required absolute poses; this request only projects them into saved images."},
    path=obj(dict(waypoints=dict(array(waypoint, 1, 100),
        description="Required ordered projection waypoints; 1..100, each specifying left and/or right poses.")),
        ("waypoints",), extensions=True, description="Required projection path; no physical action is dispatched.")),
    ("frame_id", "mode", "path"), extensions=True)
GEOMETRY_KINDS = {}
for kind, fields, required in (
    ("fk", {}, ()), ("camera_pose", {}, ()),
    ("pixel_ray", dict(pixel=V2), ("pixel",)),
    ("pixel_plane", dict(pixel=V2, plane_origin_m=V3, plane_normal=V3,
                           plane_origin_alt_m=V3, target_camera_id=STRING),
        ("pixel", "plane_origin_m", "plane_normal")),
    ("ray_surface", dict(pixel=V2, scene_version={"type": "integer", "minimum": 1}, entity_id=STRING),
        ("pixel", "scene_version", "entity_id")),
    ("project_points", dict(points_m=POINTS, connect_points=BOOL), ("points_m",)),
    ("project_geometry", dict(scene_version={"type": "integer", "minimum": 1},
        entity_id=STRING, entity_ids=array(STRING, 1), points_m=POINTS, connect_points=BOOL), ("scene_version",)),
    ("preview_ee", dict(action=preview_action, connect_points=BOOL), ("action",)),
    ("sparse_track", TRACK_FIELDS, ()), ("triangulate", TRACK_FIELDS, ()),
    ("build_scene", dict(TRACK_FIELDS, base_scene_version=COUNT, primitive={"enum": ["plane", "box"]},
        entity_id=STRING, rebuild=BOOL, plane_threshold_m={"type": "number", "exclusiveMinimum": 0},
        max_displacement_m=NONNEGATIVE), ("base_scene_version",)),
    ("alignment", dict(scene_version={"type": "integer", "minimum": 1}, points_m=POINTS,
        pixels=PIXELS, correspondence_source=STRING), ("scene_version",)),
):
    schema = obj(dict(GEOMETRY_FIELDS, kind={"const": kind}, **fields),
                 (*IDENTITY, "kind", "camera_id", *required), **SINGLE_OR_MULTIPLE)
    if kind in {"sparse_track", "triangulate", "build_scene"}:
        schema["dependentRequired"] = {"pixels_a": ["pixels_b"], "pixels_b": ["pixels_a"]}
    if kind == "alignment":
        schema["dependentRequired"] = {"points_m": ["pixels"], "pixels": ["points_m"]}
    if kind == "pixel_plane":
        schema["properties"].pop("observation_ids")
        schema.pop("oneOf")
        schema["required"].append("observation_id")
    GEOMETRY_KINDS[kind] = schema
GEOMETRY_KINDS["help"] = obj(dict(BASE, kind={"const": "help"}), (*IDENTITY, "kind"))
SCHEMAS["GeometryRequest"] = {"oneOf": list(GEOMETRY_KINDS.values())}
SCHEMAS["GeometryJobRequest"] = {"oneOf": [value for kind, value in GEOMETRY_KINDS.items() if kind != "help"]}
for name, kind in (("BuildRequest", "build_scene"), ("AlignmentRequest", "alignment")):
    SCHEMAS[name] = deepcopy(GEOMETRY_KINDS[kind])
    SCHEMAS[name]["required"].remove("kind")  # Route supplies its established default.

query_fields = dict(BASE, scene_version={"type": "integer", "minimum": 1}, kind=STRING,
                    entity_id=STRING, frame_id=STRING)
SCHEMAS["SceneQuery"] = {"oneOf": [
    obj(dict(query_fields, kind={"const": "entity_pose"}), (*IDENTITY, "scene_version", "kind", "entity_id")),
    obj(dict(query_fields, kind={"const": "ray_surface"}, origin_m=V3, direction=V3),
        (*IDENTITY, "scene_version", "kind", "entity_id", "origin_m", "direction")),
    obj(dict(query_fields, kind={"const": "distance"}, point_m=V3, path_points_m=POINTS),
        (*IDENTITY, "scene_version", "kind", "entity_id"),
        oneOf=[{"required": ["point_m"]}, {"required": ["path_points_m"]}]) ]}
SCHEMAS["BlobRequest"] = obj(dict(sha256=DIGEST, kind=STRING, mime_type=STRING,
    data_base64=TEXT, source_artifact_id=nullable({"type": "string", "pattern": "^[A-Za-z0-9_-]{1,160}$"}),
    access_scope={"const": "actor"}), ("sha256", "kind", "mime_type", "data_base64"))
SCHEMAS["SnapshotRequest"] = obj(dict(BASE, scene_version={"type": "integer", "minimum": 1},
    anchor_observation_id=STRING), (*IDENTITY, "scene_version", "anchor_observation_id"))
SCHEMAS["CancelRequest"] = obj(dict(BASE, reason=STRING), IDENTITY)
SCHEMAS["UnsupportedRequest"] = obj(dict(BASE, snapshot_id=nullable(STRING)), extensions=True,
    **{"not": {"required": ["submit_to_observed"]},
       "description": "Reserved physics operation. Additional proposal fields are never executed; always returns unsupported."})

# Replies describe stable fields plus versioned/algorithm-specific evidence.
# They deliberately accept extension fields in historical immutable records.
SCHEMAS["Entity"] = obj(dict(geometry=ref("Geometry"), state={"enum": ["tracked", "lost", "retired", "occluded", "dirty"]},
    source_kind={"enum": ["observation_inference", "explicit_prior"]}, observation_ids=STRINGS,
    assumptions=STRINGS, uncertainty={"type": ["string", "object"]}, last_observation_id=STRING,
    last_sim_step=nullable(COUNT), step_domain=nullable(STRING), clock_domain=nullable(STRING),
    evidence=EVIDENCE, support_points_m=array(V3), support_polygon_uv_m=nullable(array(V2)),
    association=EVIDENCE, velocity={"type": ["string", "object"]}, contact_state=STRING,
    state_reason=STRING, possible_position=STRING, physics_hypotheses=EVIDENCE),
    ("geometry", "state", "source_kind", "observation_ids", "assumptions"), extensions=True)
SCHEMAS["Scene"] = obj(dict(BASE, scene_version={"type": "integer", "minimum": 1},
    patch_digest=DIGEST, idempotency_token=nullable(STRING), frame_id=STRING,
    observation_ids=STRINGS, calibration_id=nullable(STRING), robot_model_id=nullable(STRING),
    builder_digest=STRING, entities={"type": "object", "additionalProperties": ref("Entity")},
    unknown_regions=array(EVIDENCE), build_kind=STRING, limitations=STRINGS, data_origin=STRING),
    (*IDENTITY, "scene_version", "world_kind", "frame_id", "entities", "observation_ids"), extensions=True)
SCHEMAS["Snapshot"] = obj(dict(BASE, snapshot_id=STRING, manifest_digest=DIGEST,
    scene_version={"type": "integer", "minimum": 1}, scene_digest=DIGEST, scene=ref("Scene"),
    anchor_observation_id=STRING, anchor_digest=DIGEST, calibration_id=nullable(STRING),
    robot_model_id=nullable(STRING), observation_profile=nullable({"enum": list(OBSERVATION_PROFILES)}),
    initial_state=EVIDENCE, quality=EVIDENCE, execution_settings=EVIDENCE,
    resolved_artifacts={"type": "object", "additionalProperties": ref("Artifact")},
    frozen_observations=array(ref("Observation")), data_origin=STRING),
    (*IDENTITY, "snapshot_id", "manifest_digest", "scene_version", "scene", "initial_state", "quality"), extensions=True)
SCHEMAS["QueryResult"] = obj(dict(BASE, query_type=STRING,
    status={"enum": ["supported", "conditional", "unknown", "invalid_input"]},
    value=nullable(EVIDENCE), reason=TEXT, observation_ids=STRINGS, observation_states=array(EVIDENCE),
    algorithm=STRING, implementation_digest=DIGEST, library_versions=EVIDENCE,
    frame_id=STRING, frame_ids=STRINGS, unit={"const": "m"}, calibration_id=STRING,
    camera_pose_source=STRING, camera_pose_diagnostics=EVIDENCE,
    unknown_regions=array({"type": ["string", "object"]}), assumptions=STRINGS,
    scene_version={"type": "integer", "minimum": 1}, scene_digest=DIGEST,
    unknown_entity_ids=STRINGS, uncertainty={"type": ["string", "object"]},
    limitations=STRINGS, applicability=STRING, duration_s=NONNEGATIVE, diagnostics=EVIDENCE,
    scene=ref("Scene"), data_origin=STRING),
    (*IDENTITY, "world_kind", "query_type", "status", "value", "observation_ids", "frame_id", "unit"), extensions=True)
SCHEMAS["Tracks"] = obj(dict(pixels_a=array(V2), pixels_b=array(V2), accepted=COUNT,
    detected=COUNT, reason=STRING, method=STRING, source_kind=STRING,
    descriptor_distance=array(NONNEGATIVE), ratio_threshold=NUMBER,
    laplacian_variance=array(NONNEGATIVE), forward_backward_error_px=array(NONNEGATIVE),
    photometric_error=array(NUMBER)), ("pixels_a", "pixels_b"), extensions=True)
SCHEMAS["Triangulation"] = obj(dict(points_m=array(V3), accepted_indices=array(COUNT),
    reprojection_error_px=array(NONNEGATIVE), parallax_deg=array(NUMBER), point_status=STRINGS,
    baseline_m=NONNEGATIVE, epipolar_sampson_error_normalized=array(NONNEGATIVE), accepted=COUNT,
    total=COUNT, assumptions=STRINGS, thresholds=obj(dict(min_angle_deg=NUMBER, max_error_px=NUMBER)),
    tracks=ref("Tracks"), source_views=array(obj(dict(observation_id=STRING, camera_id=STRING,
        frame_id=STRING), ("observation_id", "camera_id", "frame_id"))), association_failure=EVIDENCE),
    ("points_m", "accepted_indices", "accepted", "total", "assumptions"), extensions=True)
projection = obj(dict(pixels=array(V2), depth_m=array(NUMBER), in_image=array(BOOL),
    overlay_ref=ARTIFACT, original_rgb_ref=ARTIFACT, frame_id=STRING,
    tool_axes=array(EVIDENCE), ik=STRING, collision=STRING, semantics=STRING, limitation=STRING),
    ("pixels", "depth_m", "in_image", "overlay_ref", "original_rgb_ref", "frame_id"), extensions=True)
camera_pose = obj(dict(T_world_camera=M4, source_kind=STRING, T_world_camera_fk=M4,
    link_poses={"type": "object", "additionalProperties": M4}, fk_path=STRING,
    translation_residual_m=nullable(NUMBER), rotation_residual_rad=nullable(NUMBER),
    validation_reference=STRING, pose_diagnostics=EVIDENCE), ("T_world_camera", "source_kind"), extensions=True)
alignment_view = obj(dict(observation_id=STRING, camera_id=STRING, frame_id=STRING,
    errors_px=array(NONNEGATIVE), rms_px=NONNEGATIVE, median_px=NONNEGATIVE, max_px=NONNEGATIVE,
    visible_count=COUNT, count=COUNT, attempted_count=COUNT, coverage=NONNEGATIVE,
    correspondence_source=STRING, projected_pixels=array(V2), measured_pixels=array(V2)),
    ("observation_id", "camera_id", "errors_px", "count"), extensions=True)
cross_projection = obj(dict(pixel=V2, depth_m=NUMBER, in_image=BOOL,
    d_pixel_d_normal_offset_px=V2,
    alternate_pixel=V2, alternate_depth_m=NUMBER, alternate_in_image=BOOL),
    ("pixel", "depth_m", "in_image"))
QUERY_VALUES = {
    "fk": camera_pose, "camera_pose": camera_pose,
    "pixel_ray": obj(dict(origin_m=V3, direction=V3, depth={"const": "unknown"}),
                     ("origin_m", "direction", "depth"), extensions=True),
    "pixel_plane": obj(dict(point_m=V3, ray_distance_m=NONNEGATIVE,
        plane_origin_m=V3, plane_normal=V3, incidence_angle_deg=NONNEGATIVE,
        ray_plane_condition_factor=NONNEGATIVE, d_point_d_normal_offset_m=V3,
        cross_camera_projections={"type": "object", "additionalProperties": cross_projection},
        scope={"const": "caller_assumed_infinite_plane"},
        plane_origin_alt_m=V3, alternate_point_m=V3,
        alternate_ray_distance_m=NONNEGATIVE, assumption_delta_m=NONNEGATIVE,
        target_camera_id=STRING, pixels=array(V2), depth_m=array(NUMBER),
        in_image=array(BOOL), overlay_ref=ARTIFACT, original_rgb_ref=ARTIFACT),
        ("point_m", "ray_distance_m", "plane_origin_m", "plane_normal",
         "incidence_angle_deg", "ray_plane_condition_factor",
         "d_point_d_normal_offset_m", "cross_camera_projections", "scope"), extensions=True),
    "ray_surface": obj(dict(point_m=V3, ray_distance_m=NONNEGATIVE, scope=STRING,
        incidence_angle_deg=NONNEGATIVE, ray_plane_condition_factor=NONNEGATIVE),
        ("point_m", "ray_distance_m", "scope"), extensions=True),
    **dict.fromkeys(("project_points", "project_geometry", "preview_ee"), projection),
    "sparse_track": ref("Tracks"), "triangulate": ref("Triangulation"),
    "build_scene": obj(dict(proposal=ref("Patch"), geometry_result=ref("Triangulation")),
                       ("proposal", "geometry_result"), extensions=True),
    "alignment": obj(dict(views=array(alignment_view), rms_px=NONNEGATIVE, median_px=NONNEGATIVE,
        max_px=NONNEGATIVE, count=COUNT, heldout={"const": True}, scene_digest=DIGEST, scope=STRING),
        ("views", "rms_px", "median_px", "max_px", "count", "heldout", "scene_digest", "scope"), extensions=True),
    "entity_pose": obj(dict(geometry=ref("Geometry"), state=STRING, source_kind=STRING),
                       ("geometry", "state", "source_kind"), extensions=True),
    "distance": obj(dict(distance_m=NONNEGATIVE, nearest_point_m=V3, sample_index=COUNT,
        sample_count=COUNT, max_sample_spacing_m=NONNEGATIVE, scope=STRING),
        ("distance_m", "nearest_point_m", "sample_index", "sample_count", "max_sample_spacing_m", "scope"), extensions=True),
}
SCHEMAS["QueryResult"]["allOf"] = [
    {"if": {"properties": {"query_type": {"const": kind}}, "required": ["query_type"]},
     "then": {"properties": {"value": nullable(value)}}} for kind, value in QUERY_VALUES.items()]
SCHEMAS["GeometryHelp"] = obj(dict(status={"const": "supported"}, world_kind={"const": "internal"},
    frame_id=STRING, unit={"const": "m"}, common=STRING,
    requests={"type": "object", "additionalProperties": STRING}, limitation=STRING),
    ("status", "world_kind", "requests", "limitation"), extensions=True)
SCHEMAS["Job"] = obj(dict(BASE, job_id=STRING, kind=STRING,
    status={"enum": ["new", "queued", "running", "succeeded", "failed", "cancelled", "expired"]},
    revision=COUNT, phase=STRING, request=ref("GeometryRequest"), input_digest=DIGEST,
    frozen_inputs=EVIDENCE, budget=BUDGET, deadline_epoch_s=NUMBER, queue_deadline_epoch_s=NUMBER,
    created_at=STRING, started_at=STRING, finished_at=STRING, cancel_requested=BOOL, cancel_reason=STRING,
    attempt_token=STRING, attempt=COUNT, stop_confirmed=BOOL, partial=BOOL, error=TEXT,
    result=nullable(ref("QueryResult")), result_ref=ARTIFACT, actual_resources=EVIDENCE),
    (*IDENTITY, "job_id", "kind", "status", "revision", "request", "cancel_requested"), extensions=True)
# Historical Workbench jobs also retained their HTTP correlation wrapper. It
# is evidence, not a second accepted command shape at the public HTTP boundary.
SCHEMAS["RecordedGeometryRequest"] = deepcopy(SCHEMAS["GeometryRequest"])
for branch in SCHEMAS["RecordedGeometryRequest"]["oneOf"]:
    branch["properties"].update(request_id=STRING, request=EVIDENCE)
SCHEMAS["Job"]["properties"]["request"] = ref("RecordedGeometryRequest")
SCHEMAS["Event"] = obj(dict(BASE, event_id=STRING, source=STRING, source_instance_id=STRING,
    source_seq={"type": "integer", "minimum": 1}, type=STRING, occurred_at=nullable(STRING),
    received_at=STRING, clock_domain=STRING, sim_step=nullable(COUNT), step_domain=nullable(STRING),
    sim_time_s=nullable(NONNEGATIVE), correlation=EVIDENCE, payload=EVIDENCE),
    (*IDENTITY, "event_id", "world_kind", "source", "source_instance_id", "source_seq", "type", "payload"), extensions=True)
SCHEMAS["Error"] = obj(dict(error=TEXT, code=STRING, field_path=TEXT, message=TEXT,
    actual={}, suggestion=nullable(TEXT), minimal_example={}, outcome_semantics=TEXT, tool=STRING),
    ("error", "code", "field_path"), extensions=True)
SCHEMAS["Unsupported"] = obj(dict(schema_version=VERSION, world_kind={"const": "internal"},
    status={"const": "unsupported"}, error={"const": "unsupported_capability"},
    reason=STRING, snapshot_id=nullable(STRING), executed={"const": False}),
    ("schema_version", "world_kind", "status", "error", "reason", "executed"), extensions=True)
SCHEMAS["Head"] = obj(dict(IDENTITY_FIELDS, world_kind={"const": "internal"}, scene_version=COUNT, digest=DIGEST),
    (*IDENTITY, "world_kind", "scene_version"), extensions=True)
SCHEMAS["Versions"] = obj(dict(versions=array(obj(dict(scene_version={"type": "integer", "minimum": 1},
    digest=DIGEST), ("scene_version", "digest")))), ("versions",))
SCHEMAS["Events"] = obj(dict(events=array(ref("Event")), next_cursor=COUNT), ("events", "next_cursor"))
SCHEMAS["ImportedObservation"] = obj(dict(observation_id=STRING, digest=DIGEST), ("observation_id", "digest"))
SCHEMAS["UploadedBlob"] = obj(dict(ref=ARTIFACT, artifact=ref("Artifact")), ("ref", "artifact"))
SCHEMAS["Health"] = obj(dict(schema_version=VERSION, status={"const": "ready"},
    physics_worker_ready={"const": False}), ("schema_version", "status", "physics_worker_ready"))
SCHEMAS["Capabilities"] = obj(dict(schema_version=VERSION, world_kind={"const": "internal"},
    primitives=STRINGS, queries=STRINGS, patch_operations=STRINGS, rgb_reconstruction=STRING,
    geometry_tools=STRINGS, physics={"const": "unsupported"}, observed_execution={"const": "unsupported"},
    worker_ready={"const": False}, cpu_jobs={"const": True}, max_queue=COUNT,
    max_blob_bytes=COUNT, max_blob_store_bytes=COUNT),
    ("schema_version", "world_kind", "physics", "observed_execution", "worker_ready", "cpu_jobs"), extensions=True)

# The same table drives routing, boundary validation and the published document.
GET_CONTRACTS = {"health": "Health", "capabilities": "Capabilities", "events": "Events",
    "scene/head": "Head", "scene/versions": "Versions", "scene/versions/{version}": "Scene",
    "snapshots/{identifier}": "Snapshot", "jobs/{identifier}": "Job", "artifacts/{identifier}": None}
POST_CONTRACTS = {
    "blobs": ("BlobRequest", {201: "UploadedBlob"}),
    "observations": ("Observation", {201: "ImportedObservation"}),
    "scene/patches": ("Patch", {201: "Scene"}),
    "scene/update-jobs": ("BuildRequest", {202: "Job"}),
    "alignment-jobs": ("AlignmentRequest", {202: "Job"}),
    "geometry-jobs": ("GeometryJobRequest", {202: "Job"}),
    "geometry/query": ("GeometryRequest", {200: ("QueryResult", "GeometryHelp"), 202: "Job"}),
    "scene/query": ("SceneQuery", {200: "QueryResult"}),
    "snapshots": ("SnapshotRequest", {201: "Snapshot"}),
    **{name: ("UnsupportedRequest", {422: "Unsupported"}) for name in ("branches", "rollout-jobs", "compare-jobs")},
    "jobs/{identifier}/cancel": ("CancelRequest", {200: "Job"}),
}


def _finite_number(checker, value):
    return type(value) in (int, float) and (type(value) is int or math.isfinite(value))


Validator = validators.extend(Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("number", _finite_number)
        .redefine("array", lambda checker, value: isinstance(value, (list, tuple))))
VALIDATORS = {name: Validator({"$ref": "#/$defs/" + name, "$defs": SCHEMAS}) for name in SCHEMAS}


def validate(name, value, *, response=False):
    # Select discriminated request variants first so diagnostics point at the
    # actual field instead of printing every unrelated operation's schema.
    schema = SCHEMAS[name]
    if not response and isinstance(value, dict) and "kind" in value and "oneOf" in schema:
        branch = next((item for item in schema["oneOf"]
                       if item.get("properties", {}).get("kind", {}).get("const") == value.get("kind")), None)
        validator = Validator(dict(branch, **{"$defs": SCHEMAS})) if branch is not None else VALIDATORS[name]
    else:
        validator = VALIDATORS[name]
    error = next(validator.iter_errors(value), None)
    if error is not None:
        raise schema_input_error(error, code="invalid_scene_response" if response else "invalid_scene_request")
    return value


def validate_request(route, value):
    return validate(POST_CONTRACTS[route][0], value)


def validate_response(route, value, *, method="GET", status=200):
    names = GET_CONTRACTS[route] if method == "GET" else POST_CONTRACTS[route][1][status]
    if isinstance(names, tuple):
        if not any(VALIDATORS[name].is_valid(value) for name in names):
            validate(names[0], value, response=True)
    elif names is not None:
        validate(names, value, response=True)
    return value


CONFLICT_CODES = frozenset({"identity_conflict", "idempotency_conflict", "scene_version_conflict",
    "immutable_artifact_conflict", "immutable_content_conflict", "late_observation_cannot_overwrite_entity"})


def http_error(error):
    """Stable codes retain the old error string and established HTTP categories."""
    payload = error_payload(error)
    if re.fullmatch(r"[a-z][a-z0-9_]*", payload["code"]) is None:
        payload["code"] = "invalid_scene_input"
    payload["message"] = payload["error"]
    return payload, 409 if payload["code"] in CONFLICT_CODES else 422


def openapi():
    def published(value):
        if isinstance(value, dict):
            return {key: (item.replace("#/$defs/", "#/components/schemas/") if key == "$ref" else published(item))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [published(item) for item in value]
        return value

    def reply(names):
        schema = {"oneOf": [ref(name) for name in names]} if isinstance(names, tuple) else ref(names)
        return {"description": "Versioned scene result", "content": {"application/json": {"schema": schema}}}

    paths = {}
    for method, routes in (("get", GET_CONTRACTS), ("post", POST_CONTRACTS)):
        for route in routes:
            parameters = [dict(name=key, **{"in": "path"}, required=True,
                schema={"type": "integer", "minimum": 1} if key == "version" else STRING)
                for key in re.findall(r"\{(\w+)\}", route)]
            operation = dict(parameters=parameters, responses={str(code): reply("Error") for code in (400, 404, 409, 422, 429, 500, 503)})
            if method == "get":
                if route.startswith(("scene/", "snapshots/")):
                    parameters.extend(dict(name=key, **{"in": "query"}, required=True, schema=schema)
                                      for key, schema in IDENTITY_FIELDS.items())
                if route == "events":
                    parameters.append(dict(name="after", **{"in": "query"}, required=False, schema=COUNT))
                operation["responses"]["200"] = reply(routes[route]) if routes[route] else {
                    "description": "Immutable permitted artifact bytes", "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}
            else:
                name, responses = routes[route]
                parameters.append(dict(name="Idempotency-Key", **{"in": "header"}, required=True,
                                       schema=dict(STRING, maxLength=256)))
                operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": ref(name)}}}
                for code, response in responses.items():
                    operation["responses"][str(code)] = reply((response, "Error") if code == 422 else response)
                if name == "UnsupportedRequest":
                    operation["description"] = "Physics is unsupported; this endpoint cannot submit observed-world actions."
            paths.setdefault("/v1/" + route, {})[method] = operation
    return published(dict(openapi="3.1.0", info=dict(title="Auriga independent scene API", version="0.1"),
                          paths=paths, components=dict(schemas=deepcopy(SCHEMAS))))
