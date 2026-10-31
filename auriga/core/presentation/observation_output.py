"""Observation delivery preserves current geometry and labels historical budgets."""
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import threading


# These are delivery conventions or calibration constants, never measurements.
# Unknown fields and all live source/status markers remain inline. In particular,
# no transform in camera_pose_sample is eligible, including the fixed head view.
_CALIBRATION_FIELDS = frozenset({"profile_id", "reference_frame_id", "schema_version"})
_CALIBRATION_CAMERA_FIELDS = frozenset({
    "K", "distortion", "distortion_model", "image_size", "optical_axes",
    "calibration_source", "arm", "link_name",
})
_ROBOT_FIELDS = frozenset({"frame_id", "tcp_config_id", "units", "unit_conventions"})
_POSE_FIELDS = frozenset({"frame_id", "tcp_config_id", "units", "unit_conventions"})
_GRASP_FIELDS = _POSE_FIELDS | frozenset({
    "convention", "contact_region", "contact_depth_m", "contact_point_meaning",
})
_UNSET_EPOCH = object()
_BUDGET_SEMANTICS = "official native steps; separate from tool requests and model-call budget; reserves are not included"
_OBSERVATION_BUDGET_SEMANTICS = "Observation action_budget is the capture-time snapshot; use current_action_budget for remaining execution budget."
_DELIVERY_SEMANTICS = "Only unchanged calibration/convention fields use earlier delivered observations; measurements, dynamic camera poses and budgets remain inline."
_GEOMETRY_SEMANTICS = frozenset({
    "Reconstructive mount/base/link matrices; current camera world transforms and intrinsics remain inline.",
    "Reconstructive mount/base/link matrices; current camera world transforms remain inline; unchanged intrinsics use metadata_delivery.static_refs.",
})


def same_frame_robot_state(observation, state):
    """Bind permitted proprioception to its image time, never to a newer state.

    Optional image/geometry deliveries can survive unavailable legacy state,
    but must say so rather than attaching an unsynchronized measurement.
    """
    from ..observation_policy import ACTOR_PROFILE, actor_robot_state
    result = {key: deepcopy(observation[key]) for key in
              ("observation_id", "sim_step", "step_domain", "sim_time_s") if key in observation}
    step, domain = observation.get("sim_step"), observation.get("step_domain")
    if (type(step) is not int or not isinstance(domain, str) or not domain
            or not isinstance(state, dict) or state.get("sim_step") != step
            or state.get("step_domain") != domain):
        return dict(result, status="unavailable", reason="same_frame_robot_state_not_synchronized")
    if not state.get("arms"):
        return dict(result, status="unavailable", reason="robot_measurements_unavailable")
    try:
        public = actor_robot_state(state, observation.get("observation_profile", ACTOR_PROFILE))
    except (ValueError, TypeError, KeyError):
        return dict(result, status="unavailable", reason="permitted_robot_measurements_unavailable")
    return dict(result, status="available", robot_state=public,
                semantics="Robot measurements at this image's capture time; gripper commands and measured finger positions do not establish contact or object retention.")


def saved_observation_state(store, observation_id):
    """Read a saved image's own state without capturing or querying the robot."""
    try:
        observation = store.read("observation", observation_id)
        info, raw = store.resolve_artifact(observation["robot_state_ref"], allowed_scopes=("actor",))
        if info.get("kind") != "robot_state" or info.get("mime_type") != "application/json":
            raise ValueError("invalid_robot_state_artifact")
        return same_frame_robot_state(observation, json.loads(raw))
    except (KeyError, ValueError, TypeError, OSError):
        return dict(observation_id=observation_id, status="unavailable", reason="saved_robot_state_unavailable")


def _fields(value, names):
    return {key: deepcopy(value[key]) for key in sorted(names) if key in value
            and value[key] is not None and value[key] != "" and value[key] != [] and value[key] != {}}


def _static_groups(metadata):
    """Extract only known conventions; preserve every unfamiliar diagnostic."""
    groups = {}
    calibration = metadata.get("camera_calibration")
    if isinstance(calibration, dict):
        static = _fields(calibration, _CALIBRATION_FIELDS)
        cameras = calibration.get("cameras")
        if isinstance(cameras, dict):
            selected = {name: _fields(camera, _CALIBRATION_CAMERA_FIELDS)
                        for name, camera in cameras.items() if isinstance(camera, dict)}
            if any(selected.values()):
                static["cameras"] = {name: value for name, value in selected.items() if value}
        if static:
            groups["camera_calibration"] = static
    state = metadata.get("robot_state")
    if isinstance(state, dict):
        static = _fields(state, _ROBOT_FIELDS)
        if state.get("step_domain") == "adapter_control_step":
            static["step_domain"] = state["step_domain"]
        arms = {}
        for name, arm in (state.get("arms") or {}).items() if isinstance(state.get("arms"), dict) else ():
            if not isinstance(arm, dict):
                continue
            selected = _fields(arm, {"joint_names", "units", "unit_conventions"})
            for key, names in (("ee_pose", _POSE_FIELDS), ("grasp_pose", _GRASP_FIELDS),
                               ("gripper", {"joint_names", "units", "unit_conventions"})):
                if isinstance(arm.get(key), dict):
                    child = _fields(arm[key], names)
                    if child:
                        selected[key] = child
            if selected:
                arms[name] = selected
        if arms:
            static["arms"] = arms
        if static:
            groups["robot_state"] = static
    return groups


def _budget_conventions(value):
    selected = {}
    if isinstance(value, dict):
        if value.get("semantics") == _BUDGET_SEMANTICS:
            selected["semantics"] = value["semantics"]
        if value.get("unknown_values_are_null") is True:
            selected["unknown_values_are_null"] = True
    return selected


def _bundle_conventions(metadata):
    """One existing static_refs group covers explicit observation-wide paths.

    The bundle group includes display prose and clock/frame conventions in the
    other metadata sections. Camera selections are keyed by camera_id, never
    list position. Source, availability and synchronization evidence, artifact
    addresses, task text, numeric budgets and every measurement stay inline.
    """
    bundle = metadata.get("bundle")
    if not isinstance(bundle, dict):
        return {}
    # An incomplete reset identity cannot safely identify these conventions.
    if not all(isinstance(bundle.get(key), str) and bundle[key]
               for key in ("run_id", "episode_id", "env_instance_id")):
        return {}
    if type(bundle.get("env_generation")) is not int or bundle["env_generation"] < 0:
        return {}
    selected = {key: bundle[key] for key in
                ("run_id", "episode_id", "env_instance_id", "env_generation")}
    for key, known in (("schema_version", "0.1"), ("robot_model_id", "arx-x5"),
                       ("observation_profile", "rgb_proprio_calibrated_v1"),
                       ("step_domain", "adapter_control_step")):
        if bundle.get(key) == known:
            selected[key] = known
    clock = f'{bundle["env_instance_id"]}/generation-{bundle["env_generation"]}'
    if bundle.get("clock_domain") == clock:
        selected["clock_domain"] = clock
    calibration_id = bundle.get("calibration_id")
    if (isinstance(calibration_id, str) and len(calibration_id) == 64
            and all(char in "0123456789abcdef" for char in calibration_id)):
        selected["calibration_id"] = calibration_id
    budget = _budget_conventions(bundle.get("action_budget"))
    if budget:
        selected["action_budget"] = budget
    cameras = bundle.get("cameras")
    if (isinstance(cameras, list) and cameras
            and all(isinstance(camera, dict) and isinstance(camera.get("camera_id"), str)
                    and camera["camera_id"] for camera in cameras)
            and len({camera["camera_id"] for camera in cameras}) == len(cameras)):
        camera_fields = {}
        for camera in cameras:
            fields = {key: camera[key] for key in ("width", "height")
                      if type(camera.get(key)) is int and camera[key] > 0}
            for key, known in (("pixel_transform", "identity"),
                               ("step_domain", "adapter_control_step"), ("control_dt_s", .004)):
                if camera.get(key) == known and type(camera.get(key)) is type(known):
                    fields[key] = known
            if fields:
                camera_fields[camera["camera_id"]] = fields
        if camera_fields:
            selected["cameras"] = camera_fields
    group = {"bundle": selected}
    budget = _budget_conventions(metadata.get("current_action_budget"))
    if budget:
        group["current_action_budget"] = budget
    poses = metadata.get("camera_pose_sample")
    if isinstance(poses, dict):
        fields = {key: known for key, known in (("reference_frame_id", "env_origin"),
                  ("step_domain", "adapter_control_step")) if poses.get(key) == known}
        if fields:
            group["camera_pose_sample"] = fields
    if metadata.get("observation_budget_semantics") == _OBSERVATION_BUDGET_SEMANTICS:
        group["observation_budget_semantics"] = _OBSERVATION_BUDGET_SEMANTICS
    geometry = metadata.get("geometry_delivery")
    if (isinstance(geometry, dict) and isinstance(geometry.get("omitted"), str)
            and geometry["omitted"] in _GEOMETRY_SEMANTICS):
        group["geometry_delivery"] = {"omitted": geometry["omitted"]}
    delivery = metadata.get("metadata_delivery")
    if isinstance(delivery, dict) and delivery.get("semantics") == _DELIVERY_SEMANTICS:
        group["metadata_delivery"] = {"semantics": _DELIVERY_SEMANTICS}
    return group


def _remove_bundle_conventions(metadata, selected):
    # The only list special case is the known bundle camera descriptor list.
    # Keep its order, camera IDs and every unselected field exactly as delivered.
    bundle_selected = deepcopy(selected["bundle"])
    cameras = bundle_selected.pop("cameras", {})
    _remove_static(metadata["bundle"], bundle_selected)
    for camera in metadata["bundle"].get("cameras", []):
        if isinstance(camera, dict):
            _remove_static(camera, cameras.get(camera.get("camera_id"), {}))
    for name, fields in selected.items():
        if name == "bundle":
            continue
        if isinstance(fields, dict):
            _remove_static(metadata[name], fields)
        else:
            metadata.pop(name, None)


def _delivery_metadata(references, observation_id):
    return dict(mode="unchanged_static_fields_referenced", static_refs=references,
                semantics=_DELIVERY_SEMANTICS,
                read_full=dict(tool="history_view_observation", observation_id=observation_id, detail="full"))


def _remove_static(value, selected):
    for key, child in selected.items():
        if isinstance(child, dict) and isinstance(value.get(key), dict):
            _remove_static(value[key], child)
        else:
            value.pop(key, None)


@dataclass
class MetadataProjection:
    """Prepared reply; preparing alone never records static data as delivered."""
    metadata: dict
    _generation: int = field(repr=False)
    _updates: dict = field(repr=False)
    _owner: object = field(repr=False)


class MetadataDelivery:
    """Reference unchanged static fields already delivered in this model context.

    The harness owns one instance and uses it only for compact model replies.
    prepare() receives the image/context epoch plus environment reset identity;
    commit() runs only when the prepared text survives final reply assembly.
    Full/program replies bypass this owner. Failed or overwritten preparations
    do not teach a baseline, and an older pending reply cannot restore a context
    invalidated by compaction, transport loss or an environment reset.
    """

    def __init__(self):
        self._epoch = _UNSET_EPOCH
        self._generation = 0
        self._delivered = {}
        self._lock = threading.RLock()

    def invalidate(self):
        with self._lock:
            self._epoch = _UNSET_EPOCH
            self._generation += 1
            self._delivered.clear()

    def prepare(self, metadata, *, epoch, force_full=False):
        result = deepcopy(metadata)
        if epoch is None or "metadata_delivery" in metadata:
            # An unknown context or an unfamiliar projection must remain full.
            self.invalidate()
            return MetadataProjection(result, -1, {}, self)
        bundle = metadata.get("bundle", metadata)
        observation_id = bundle.get("observation_id")
        # Epoch owners should include reset identity even when a compact profile
        # omits it. This additionally guards ordinary unprojected/lean bundles.
        identity = tuple((key, bundle[key]) for key in
                         ("run_id", "episode_id", "env_instance_id", "env_generation") if key in bundle)
        context = (epoch, identity)
        with self._lock:
            if self._epoch is _UNSET_EPOCH or self._epoch != context:
                self._epoch = deepcopy(context)
                self._generation += 1
                self._delivered.clear()
            generation = self._generation
            delivered = deepcopy(self._delivered)
        updates, references = {}, {}
        for name, static in _static_groups(metadata).items():
            try:
                raw = json.dumps(static, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":"), allow_nan=False).encode()
            except (TypeError, ValueError):
                # Unusual/nonfinite conventions stay complete and never form a
                # baseline. No JSON or optional delivery optimization failure
                # may turn a previously executed action into another request.
                continue
            version = hashlib.sha256(raw).hexdigest()
            previous = delivered.get(name)
            if (not force_full and observation_id and previous
                    and previous["version"] == version):
                _remove_static(result[name], static)
                references[name] = deepcopy(previous)
            elif observation_id:
                updates[name] = dict(version=version, observation_id=observation_id)
        if references:
            geometry = result.get("geometry_delivery")
            if ("camera_calibration" in references and isinstance(geometry, dict)
                    and geometry.get("omitted") == "Reconstructive mount/base/link matrices; current camera world transforms and intrinsics remain inline."):
                geometry["omitted"] = "Reconstructive mount/base/link matrices; current camera world transforms remain inline; unchanged intrinsics use metadata_delivery.static_refs."
            result["metadata_delivery"] = _delivery_metadata(references, observation_id)
        # Extract after ordinary references adjust geometry/delivery prose. A
        # changed or newly delivered string refreshes this exact group inline;
        # prose is never referenceable before it actually enters model history.
        static = _bundle_conventions(result)
        if static:
            try:
                raw = json.dumps(static, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":"), allow_nan=False).encode()
                version = hashlib.sha256(raw).hexdigest()
                previous = delivered.get("bundle")
                if (not force_full and observation_id and previous
                        and previous["version"] == version):
                    candidate = deepcopy(result)
                    _remove_bundle_conventions(candidate, static)
                    if "metadata_delivery" not in candidate:
                        candidate["metadata_delivery"] = _delivery_metadata({}, observation_id)
                    candidate["metadata_delivery"]["static_refs"]["bundle"] = deepcopy(previous)
                    # Match contracts.canonical, including escaped Unicode.
                    encode = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=True,
                                                     separators=(",", ":"), allow_nan=False).encode()
                    # Small/partial profiles must not grow a reply merely to
                    # reuse conventions. Include every added reference byte.
                    if len(encode(candidate)) < len(encode(result)):
                        result = candidate
                elif observation_id:
                    updates["bundle"] = dict(version=version, observation_id=observation_id)
            except (TypeError, ValueError):
                pass
        return MetadataProjection(result, generation, updates, self)

    def commit(self, projection):
        """Remember inline fields only if this reply still belongs to this epoch."""
        if projection._owner is not self:
            raise ValueError("metadata_projection_owner_mismatch")
        with self._lock:
            if projection._generation != self._generation:
                return False
            self._delivered.update(deepcopy(projection._updates))
            return True


def observation_metadata(metadata, *, current_observation_id, current_action_budget, compact_geometry=False):
    result = deepcopy(metadata)
    bundle = result.get("bundle", result)
    result["current_observation_id"] = current_observation_id
    result["observation_is_current"] = bool(current_observation_id and bundle.get("observation_id") == current_observation_id)
    result["current_action_budget"] = deepcopy(current_action_budget)
    result["observation_budget_semantics"] = _OBSERVATION_BUDGET_SEMANTICS
    if not compact_geometry:
        return result
    calibration, poses = result.get("camera_calibration"), result.get("camera_pose_sample")
    frames = bundle.get("cameras", [])
    # Keep intrinsics, distortion, axes, source labels and each camera's CURRENT
    # T_world_camera. Only move reconstructive FK/mount matrices to exact refs.
    if isinstance(calibration, dict) and isinstance(poses, dict):
        pose_cameras = poses.get("cameras", {})
        calibrated_cameras = calibration.get("cameras", {})
        if not isinstance(pose_cameras, dict) or not isinstance(calibrated_cameras, dict):
            return result
        for name, camera in calibrated_cameras.items():
            if isinstance(camera, dict) and isinstance(pose_cameras.get(name), dict) and "T_world_camera" in pose_cameras[name]:
                for field in ("T_link_camera", "T_world_base", "T_world_camera"):
                    camera.pop(field, None)
        for camera in pose_cameras.values():
            if isinstance(camera, dict) and "T_world_camera" in camera:
                camera.pop("T_world_link", None)
                camera.pop("T_world_base", None)
        if frames:
            if frames[0].get("calibration_ref"):
                calibration["full_ref"] = frames[0]["calibration_ref"]
            if frames[0].get("pose_sample_ref"):
                poses["full_ref"] = frames[0]["pose_sample_ref"]
        result["geometry_delivery"] = dict(mode="current_camera_geometry",
            omitted="Reconstructive mount/base/link matrices; current camera world transforms and intrinsics remain inline.",
            read_full=dict(tool="history_view_observation", observation_id=bundle.get("observation_id"), detail="full"))
    return result
