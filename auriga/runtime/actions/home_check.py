"""Compare permitted bimanual link6 measurements with an explicit initial sample.

This module has no robot/evaluator interface. The caller owns the observation
lifecycle: capture the first sample after start/reset, invalidate on admission,
and update only from complete current or terminal observations (never history
or action-progress samples). Results describe home geometry, not task success.
"""

from copy import deepcopy
import json
import math
import threading

from ...core.contracts import IDENTITY


HOME_CHECK_POLICY = {
    "version": "measured-bimanual-link6-home-v1",
    "source": "actor_measured_ee_pose",
    "frame_id": "env_origin",
    "tcp_config_id": "arx-x5-link6-v1",
    "arms": ["left", "right"],
    "position_tolerance_m": 0.15,
    "position_comparison": "absolute_delta_per_axis",
    "rotation_tolerance_deg": 20.0,
    "rotation_comparison": "shortest_quaternion_angle",
    "boundary": "inclusive",
    "roundoff": "At each tolerance, at most 8 binary64 ULPs absorb arithmetic rounding; no physical slack.",
    "not_checked": ["joint_angles", "joint_winding", "gripper", "object_placement", "task_success"],
}


class _Invalid(ValueError):
    pass


def _identity(value):
    if not isinstance(value, dict):
        raise _Invalid("invalid_identity")
    if any(not isinstance(value.get(k), str) or not value[k] for k in IDENTITY[:-1]):
        raise _Invalid("invalid_identity")
    if type(value.get("env_generation")) is not int or value["env_generation"] < 1:
        raise _Invalid("invalid_identity")
    return {key: value[key] for key in IDENTITY}


def _vector(value, length, reason):
    if not isinstance(value, list) or len(value) != length:
        raise _Invalid(reason)
    try:
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in value):
            raise _Invalid(reason)
        return [float(x) for x in value]
    except OverflowError:
        raise _Invalid(reason) from None


def _normalized(quaternion):
    # Scaling before the norm avoids overflow and underflow for finite inputs.
    scale = max(abs(x) for x in quaternion)
    if not scale:
        raise _Invalid("zero_quaternion")
    scaled = [x / scale for x in quaternion]
    norm = math.hypot(*scaled)
    return [x / norm for x in scaled]


def _angle_degrees(initial, current):
    # inverse(initial) * current, xyzw; atan2 is stable near zero rotation.
    x, y, z, w = initial
    a, b, c, d = current
    vector = (w*a - x*d - y*c + z*b,
              w*b + x*c - y*d - z*a,
              w*c - x*b + y*a - z*d)
    scalar = w*d + x*a + y*b + z*c
    return math.degrees(2 * math.atan2(math.hypot(*vector), abs(scalar)))


def _exceeds(value, tolerance):
    return value > tolerance and value - tolerance > 8 * math.ulp(tolerance)


class HomePoseCheck:
    """Thread-safe snapshots; only explicit capture can establish a reference.

    The first capture for an identity is immutable, including a failed capture.
    A reset must change the store identity before capturing its first sample.
    Repeated checks reuse a paused world's sample without an age timeout.
    """

    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._policy = deepcopy(HOME_CHECK_POLICY)
        self._reference = None
        self._initial = None
        self._current = None
        self._watermark = None
        self._invalidated_observation_id = None
        self._current_reason = "missing_current_observation"

    def _load(self, bundle):
        identity = _identity(bundle)
        if identity != _identity(self.store.identity):
            raise _Invalid("observation_identity_mismatch")
        if bundle.get("schema_version") != "0.1" or bundle.get("world_kind") != "observed":
            raise _Invalid("invalid_observation_metadata")
        observation_id, reference = bundle.get("observation_id"), bundle.get("robot_state_ref")
        if not isinstance(observation_id, str) or not observation_id:
            raise _Invalid("missing_observation_id")
        if not isinstance(reference, str) or not reference.startswith("artifact:"):
            raise _Invalid("missing_robot_state_ref")
        step, domain = bundle.get("sim_step"), bundle.get("step_domain")
        if type(step) is not int or step < 0 or not isinstance(domain, str) or not domain:
            raise _Invalid("invalid_observation_timing")
        try:
            metadata, data = self.store.resolve_artifact(reference, allowed_scopes=("actor",))
        except (KeyError, ValueError, OSError, TypeError):
            raise _Invalid("robot_state_artifact_unavailable") from None
        if (not isinstance(metadata, dict) or metadata.get("access_scope") != "actor" or metadata.get("kind") != "robot_state"
                or metadata.get("mime_type") != "application/json"):
            raise _Invalid("invalid_robot_state_artifact_metadata")
        try:
            state = json.loads(data)
        except (ValueError, TypeError, UnicodeError):
            raise _Invalid("invalid_robot_state_json") from None
        if not isinstance(state, dict):
            raise _Invalid("invalid_robot_state")
        if (type(state.get("sim_step")) is not int or state["sim_step"] != step
                or state.get("step_domain") != domain):
            raise _Invalid("robot_state_timing_mismatch")
        if (state.get("frame_id") != self._policy["frame_id"]
                or state.get("tcp_config_id") != self._policy["tcp_config_id"]):
            raise _Invalid("invalid_robot_state_frame_or_tcp")
        arms = state.get("arms")
        if not isinstance(arms, dict):
            raise _Invalid("missing_both_arms")
        poses = {}
        for name in self._policy["arms"]:
            arm = arms.get(name)
            pose = arm.get("ee_pose") if isinstance(arm, dict) else None
            if not isinstance(pose, dict):
                raise _Invalid("missing_" + name + "_ee_pose")
            if pose.get("source_kind") != "measurement":
                raise _Invalid("unmeasured_" + name + "_ee_pose")
            if (pose.get("frame_id") != self._policy["frame_id"]
                    or pose.get("tcp_config_id") != self._policy["tcp_config_id"]):
                raise _Invalid("invalid_" + name + "_frame_or_tcp")
            position = _vector(pose.get("position_m"), 3, "invalid_" + name + "_position")
            quaternion = _vector(pose.get("quaternion_xyzw"), 4, "invalid_" + name + "_quaternion")
            poses[name] = dict(position_m=position, quaternion_xyzw=_normalized(quaternion),
                               frame_id=self._policy["frame_id"], tcp_config_id=self._policy["tcp_config_id"],
                               source_kind="measurement")
        return dict(identity=identity, observation_id=observation_id, robot_state_ref=reference,
                    sim_step=step, step_domain=domain, arms=poses)

    def _unknown_reference(self, reason, identity=None, bundle=None):
        bundle = bundle if isinstance(bundle, dict) else {}
        return dict(status="unknown", reason=reason, policy=deepcopy(self._policy), identity=identity,
                    initial_observation_id=bundle.get("observation_id") if isinstance(bundle.get("observation_id"), str) else None,
                    initial_robot_state_ref=bundle.get("robot_state_ref") if isinstance(bundle.get("robot_state_ref"), str) else None,
                    sim_step=None, step_domain=None, arms={})

    def capture_initial(self, bundle):
        """Capture only a trusted first observation, returning a detached audit record."""
        with self._lock:
            try:
                identity = _identity(bundle)
                if identity != _identity(self.store.identity):
                    raise _Invalid("observation_identity_mismatch")
            except _Invalid as error:
                return self._unknown_reference(str(error), bundle=bundle)
            if self._reference is not None and self._reference["identity"] == identity:
                result = deepcopy(self._reference)
                result["capture_accepted"] = (bundle.get("observation_id") == result["initial_observation_id"]
                                              and bundle.get("robot_state_ref") == result["initial_robot_state_ref"])
                result["capture_reason"] = "already_captured" if result["capture_accepted"] else "initial_reference_immutable"
                return result
            self._initial = None
            self._reference = self._unknown_reference("missing_initial_observation", identity, bundle)
            try:
                sample = self._load(bundle)
            except _Invalid as error:
                self._reference["reason"] = str(error)
                return deepcopy(self._reference)
            self._initial = sample
            self._reference = dict(status="available", reason="initial_measurement_captured", policy=deepcopy(self._policy),
                                   identity=identity, initial_observation_id=sample["observation_id"],
                                   initial_robot_state_ref=sample["robot_state_ref"], sim_step=sample["sim_step"],
                                   step_domain=sample["step_domain"], arms=deepcopy(sample["arms"]))
            if self._watermark is None or self._watermark["identity"] != identity:
                self._current = deepcopy(sample)
                self._watermark = deepcopy(sample)
                self._invalidated_observation_id = None
                self._current_reason = None
            return deepcopy(self._reference)

    def update_current(self, bundle):
        """Accept a complete current/terminal sample; failures make status unknown."""
        with self._lock:
            self._current = None
            try:
                sample = self._load(bundle)
                previous = self._watermark
                if previous is not None and previous["identity"] == sample["identity"]:
                    if sample["step_domain"] != previous["step_domain"]:
                        raise _Invalid("step_domain_changed")
                    if sample["sim_step"] < previous["sim_step"]:
                        raise _Invalid("observation_step_regressed")
                    if (sample["observation_id"] == previous["observation_id"] and sample != previous):
                        raise _Invalid("observation_id_content_changed")
                if sample["observation_id"] == self._invalidated_observation_id:
                    raise _Invalid("observation_invalidated")
                self._current = sample
                self._watermark = deepcopy(sample)
                self._current_reason = None
            except _Invalid as error:
                self._current_reason = str(error)
            return self.check(self.store.identity)

    def invalidate_current(self, reason):
        """Call before an admitted action or reset, including actions that fail."""
        with self._lock:
            if self._watermark is not None:
                self._invalidated_observation_id = self._watermark["observation_id"]
            self._current = None
            self._current_reason = reason if isinstance(reason, str) and reason else "current_observation_invalidated"
            return self.check(self.store.identity)

    def reference_record(self):
        with self._lock:
            return deepcopy(self._reference) if self._reference is not None else self._unknown_reference("missing_initial_observation")

    def check(self, identity):
        with self._lock:
            result = dict(status="unknown", reason="missing_initial_observation", policy=deepcopy(self._policy),
                          identity=None, initial_observation_id=self._reference["initial_observation_id"] if self._reference else None,
                          current_observation_id=self._current["observation_id"] if self._current else None, arms={})
            try:
                wanted = _identity(identity)
                result["identity"] = wanted
                if wanted != _identity(self.store.identity):
                    raise _Invalid("requested_identity_mismatch")
                if self._reference is None:
                    raise _Invalid("missing_initial_observation")
                if self._reference["identity"] != wanted:
                    raise _Invalid("initial_identity_mismatch")
                if self._initial is None:
                    raise _Invalid("initial_" + self._reference["reason"])
                if self._current is None:
                    raise _Invalid(self._current_reason or "missing_current_observation")
                initial, current = self._initial, self._current
                if current["identity"] != wanted:
                    raise _Invalid("current_identity_mismatch")
                if current["step_domain"] != initial["step_domain"]:
                    raise _Invalid("initial_current_step_domain_mismatch")
                if current["sim_step"] < initial["sim_step"]:
                    raise _Invalid("current_precedes_initial")
                result.update(initial_robot_state_ref=initial["robot_state_ref"],
                              current_robot_state_ref=current["robot_state_ref"],
                              initial_sim_step=initial["sim_step"], current_sim_step=current["sim_step"],
                              step_domain=current["step_domain"])
                arms = {}
                for name in self._policy["arms"]:
                    origin, pose = initial["arms"][name], current["arms"][name]
                    errors = [abs(a-b) for a, b in zip(pose["position_m"], origin["position_m"])]
                    if not all(math.isfinite(x) for x in errors):
                        raise _Invalid("position_difference_not_finite")
                    angle = _angle_degrees(origin["quaternion_xyzw"], pose["quaternion_xyzw"])
                    position_home = not any(_exceeds(x, self._policy["position_tolerance_m"]) for x in errors)
                    rotation_home = not _exceeds(angle, self._policy["rotation_tolerance_deg"])
                    arms[name] = dict(position_error_m=errors, rotation_error_deg=angle,
                                      position_within_tolerance=position_home, rotation_within_tolerance=rotation_home,
                                      status="home" if position_home and rotation_home else "away")
                result["arms"] = arms
                result["status"] = "home" if all(arm["status"] == "home" for arm in arms.values()) else "away"
                result["reason"] = "both_arms_within_tolerance" if result["status"] == "home" else "arm_pose_outside_tolerance"
            except _Invalid as error:
                result["reason"] = str(error)
            return result
