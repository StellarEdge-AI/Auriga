"""Episode-local RGB-D measurements, target handles and optional action feedback.

Raw depth stays in binary artifacts. The actor sees bounded numerical answers;
candidate handles retain full precision and are bound to one observation.
"""
from copy import deepcopy
import threading

from ...core.contracts import IDENTITY, digest
from ...core.observation_policy import allows_depth


GEOMETRY_KINDS = frozenset({"depth_points", "region_geometry", "grasp_candidates",
                            "place_candidates", "compare_regions"})
DEPTH_KINDS = GEOMETRY_KINDS | {"register_target", "refresh_target", "target_status", "forget_target", "skill_help"}
DEPTH_TOOLS = frozenset({"depth_query"})
INSTRUCTIONS = (
    "RGB-D numerical tools are explicitly enabled for this run. Use depth_query for "
    "small metric answers from saved observations; raw depth and point clouds stay in artifacts. "
    "Coordinates are env_origin metres and angles use the grasp convention of robot_action. Select a target in RGB; "
    "register_target binds its ROI, optional seed pixel and arm to a target_id. "
    "Use region_geometry or grasp_candidates with this target_id; submit chosen numerical "
    "targets to robot_action for preview. Candidate IDs expire after observation changes. "
    "Compare saved before/after regions to measure lift and object-relative-to-gripper motion. "
    "A visible surface is not a full object, a centroid is not a mass centre, and a fitted pose "
    "does not establish contact, retention or task success. On uncertain correspondence or "
    "occlusion inspect fresh RGB or another view. Repeating the same saved query adds no evidence."
)


def _name(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("invalid_depth_" + field)
    return value


def request_schema():
    """Describe the same measurement/target branches accepted by this owner."""
    from .depth_geometry import REQUEST_PARAMETERS, request_fields
    name = dict(type="string", minLength=1, maxLength=200, pattern=r"\S")
    branches = []
    for kind in sorted(GEOMETRY_KINDS):
        properties = {key: deepcopy(REQUEST_PARAMETERS[key]) for key in
                      sorted(request_fields(kind) | {"camera_id", "frame_id"})}
        properties["kind"] = dict(type="string", const=kind)
        required = ["kind", "camera_id"]
        required += (["before_observation_id", "after_observation_id"] if kind == "compare_regions"
                     else ["observation_id"])
        required.append("pixels" if kind == "depth_points" else "roi")
        branch = dict(type="object", properties=properties, required=required, additionalProperties=True)
        if kind == "place_candidates":
            required.extend(("held_dimensions_m", "grasp_offset_m"))
        if kind in {"region_geometry", "grasp_candidates", "place_candidates"}:
            properties["target_id"] = deepcopy(name)
            # Stored target geometry supplies these fields; callers cannot override it.
            explicit = ["observation_id", "camera_id", "roi", "seed_pixel"]
            branch["required"] = [key for key in required if key not in explicit]
            branch["oneOf"] = [dict(required=["observation_id", "camera_id", "roi"],
                                    **{"not": {"required": ["target_id"]}}),
                dict(required=["target_id"], **{"not": {"anyOf": [{"required": [key]} for key in explicit]}})]
        branches.append(branch)
    for kind, fields, required in (
        ("help", {}, []), ("skill_help", {}, []),
        ("target_status", dict(target_id=name), []),
        ("forget_target", dict(target_id=name), ["target_id"]),
        ("register_target", dict(camera_id=name, observation_id=REQUEST_PARAMETERS["observation_id"],
            roi=REQUEST_PARAMETERS["roi"], seed_pixel=REQUEST_PARAMETERS["seed_pixel"],
            arm=dict(type="string", enum=["left", "right"]), label=name),
            ["observation_id", "camera_id", "roi"]),
        ("refresh_target", dict(target_id=name, observation_id=REQUEST_PARAMETERS["observation_id"],
            roi=REQUEST_PARAMETERS["roi"], seed_pixel=REQUEST_PARAMETERS["seed_pixel"]),
            ["target_id", "observation_id", "roi"]),
    ):
        branches.append(dict(type="object", properties=dict(deepcopy(fields), kind=dict(type="string", const=kind)),
            required=["kind", *required], additionalProperties=kind in {"help", "skill_help"}))
    return dict(type="object", oneOf=branches)


class DepthRuntime:
    def __init__(self, harness):
        self.harness = harness
        self._engine = None
        self._scene = None
        self._lock = threading.RLock()

    def _next_id(self, kind):
        key = "depth_sequence_" + kind
        count = self.store.read_state("runtime", key, 0) + 1
        self.store.set_state("runtime", key, count)
        return "d" + str(self.harness.identity["env_generation"]) + kind + str(count)

    @property
    def store(self):
        return self.harness.store

    def require_enabled(self):
        if not allows_depth(self.harness.observation_profile):
            raise ValueError("depth_not_enabled_for_run")

    def _engine_for_scene(self):
        self.require_enabled()
        if self._engine is None or self._scene is not self.harness.scene:
            from .depth_geometry import DepthGeometryTools
            self._scene = self.harness.scene
            self._engine = DepthGeometryTools(self._scene)
        return self._engine

    def _state_key(self):
        return "depth_targets_" + digest(self.harness.identity)

    def _targets(self):
        return self.store.read_state("runtime", self._state_key(), {})

    def _save_targets(self, targets):
        self.store.set_state("runtime", self._state_key(), targets)

    def _current(self, observation_id):
        if not self.harness.latest or observation_id != self.harness.latest["observation_id"]:
            raise ValueError("depth_requires_current_observation")

    def _target(self, identifier, *, current=True):
        target = self._targets().get(_name(identifier, "target_id"))
        if target is None:
            raise ValueError("unknown_depth_target")
        if current:
            self._current(target["observation_id"])
            if target["status"] != "tracked":
                raise ValueError("depth_target_requires_refresh")
        return deepcopy(target)

    def query(self, request, correlation=None):
        with self._lock:
            return self._query(request, correlation)

    def _query(self, request, correlation=None):
        self.require_enabled()
        if not isinstance(request, dict):
            raise ValueError("depth_request_requires_object")
        request = deepcopy(request)
        if any(key in request and request[key] != self.harness.identity[key] for key in IDENTITY):
            raise ValueError("depth_identity_mismatch")
        kind = request.get("kind")
        if kind == "skill_help":
            from .depth_skills import CATALOG
            return dict(depth_derived=True, enabled=self.harness.depth_skills, **deepcopy(CATALOG))
        if kind == "help":
            return dict(depth_derived=True, frame_id="env_origin", unit="m",
                queries=dict(
                    depth_points="observation_id,camera_id,pixels:[[u,v],...] (1..16)",
                    region_geometry="observation_id,camera_id,roi:[x,y,w,h], optional seed_pixel; or target_id",
                    grasp_candidates="region fields, optional count (1..3), grasp_depth_m, clearance_m",
                    place_candidates="region fields, held_dimensions_m, grasp_offset_m, optional yaw_deg/count/margin_m",
                    compare_regions="before_observation_id,after_observation_id,camera_id,roi, optional arm",
                    register_target="current observation_id,camera_id,roi, optional seed_pixel/arm/label; at most 4 active targets",
                    refresh_target="target_id,current observation_id,roi, optional seed_pixel",
                    target_status="optional target_id", forget_target="target_id", skill_help="bounded RGB-D skill catalog and parameters"),
                limits="Visible geometry only; no raw point clouds, contact or task truth. Candidate IDs require the same current observation.")
        if kind == "target_status":
            if set(request) - {"kind", "target_id"}:
                raise ValueError("invalid_depth_target_status_fields")
            values = [self._target(request["target_id"], current=False)] if "target_id" in request else list(self._targets().values())
            return dict(depth_derived=True, targets=values)
        if kind == "forget_target":
            if set(request) != {"kind", "target_id"}:
                raise ValueError("invalid_depth_forget_fields")
            target = self._target(request["target_id"], current=False)
            targets = self._targets()
            del targets[target["target_id"]]
            self._save_targets(targets)
            return dict(depth_derived=True, status="forgotten", target_id=target["target_id"])
        if kind in {"register_target", "refresh_target"}:
            return self._register(request, correlation)
        if kind not in GEOMETRY_KINDS:
            raise ValueError("unknown_depth_query")
        target_id = request.pop("target_id", None)
        if target_id:
            if kind in {"compare_regions", "depth_points"}:
                raise ValueError("depth_query_requires_explicit_observation_fields")
            target = self._target(target_id)
            for key in ("observation_id", "camera_id", "roi", "seed_pixel"):
                if key in request:
                    raise ValueError("depth_target_fields_cannot_be_overridden")
                if key in target:
                    request[key] = target[key]
        result = self._measure(request, correlation)
        if target_id:
            result["target_id"] = target_id
        return result

    def _register(self, request, correlation):
        refresh = request["kind"] == "refresh_target"
        required = {"kind", "observation_id", "roi"} | ({"target_id"} if refresh else {"camera_id"})
        allowed = required | {"seed_pixel"} | (set() if refresh else {"arm", "label"})
        if required - request.keys() or request.keys() - allowed:
            raise ValueError("invalid_depth_target_fields")
        self._current(request["observation_id"])
        targets = self._targets()
        if refresh:
            target = self._target(request["target_id"], current=False)
            target.pop("seed_pixel", None)
        else:
            if len(targets) >= 4:
                raise ValueError("depth_target_limit_forget_unused_target")
            target = dict(target_id=self._next_id("t"), camera_id=_name(request["camera_id"], "camera_id"))
            if "arm" in request:
                if request["arm"] not in ("left", "right"):
                    raise ValueError("invalid_depth_target_arm")
                target["arm"] = request["arm"]
            if "label" in request:
                target["label"] = _name(request["label"], "label")
        for key in ("observation_id", "roi", "seed_pixel"):
            if key in request:
                target[key] = request[key]
        measurement = self._measure(dict(kind="region_geometry", **{key: target[key] for key in
            ("observation_id", "camera_id", "roi", "seed_pixel") if key in target}), correlation)
        target.update(status="tracked" if measurement.get("status") in ("supported", "partial") else "uncertain",
                      measurement_id=measurement["measurement_id"])
        targets[target["target_id"]] = target
        self._save_targets(targets)
        self.store.event("depth.target_registered" if not refresh else "depth.target_refreshed", target, correlation)
        return {**measurement, "target_id": target["target_id"], "target_status": target["status"]}

    def _measure(self, request, correlation=None):
        with self._lock:
            return self._measure_locked(request, correlation)

    def _measure_locked(self, request, correlation=None):
        query = {**request, **self.harness.identity, "schema_version": "0.1"}
        key = "depth-" + digest(query)
        # The numerical engine validates source identity and artifact hashes
        # before its own bounded cache. Persisted IDs must not bypass that gate.
        answer = self._engine_for_scene().execute(query)
        old = self.store.read_state("depth_measurement", key)
        if old is not None:
            self.store.event("depth.measurement_reused", dict(measurement_id=old["measurement_id"]), correlation)
            return deepcopy(old)
        measurement_id = self._next_id("m")
        result = {**answer, "measurement_id": measurement_id, "depth_derived": True}
        candidates = result.get("value", {}).get("candidates", []) if isinstance(result.get("value"), dict) else []
        for index, candidate in enumerate(candidates):
            candidate_id = measurement_id + "c" + str(index + 1)
            candidate["candidate_id"] = candidate_id
            record = dict(depth_derived=True, candidate_id=candidate_id, identity=dict(self.harness.identity),
                          observation_id=request.get("observation_id"), measurement_id=measurement_id,
                          kind=request["kind"], candidate=deepcopy(candidate))
            self.store.save("depth_candidate", candidate_id, record)
        self.store.save("depth_measurement", measurement_id, dict(identity=dict(self.harness.identity), request=query, result=result))
        self.store.set_state("depth_measurement", key, result)
        self.store.event("depth.measured", dict(measurement_id=measurement_id, kind=request["kind"], status=result.get("status")), correlation)
        return deepcopy(result)

    def feedback(self, action, receipt, correlation=None):
        """No new capture or motion; uncertain tracking invalidates the handle."""
        with self._lock:
            return self._feedback(action, receipt, correlation)

    def _feedback(self, action, receipt, correlation=None):
        self.require_enabled()
        if any(action.get(key) != self.harness.identity[key] for key in IDENTITY):
            raise ValueError("depth_feedback_action_identity_mismatch")
        try:
            saved = self.store.read("depth_action_feedback", action["action_id"])
            if saved.get("identity") != self.harness.identity:
                raise ValueError("depth_feedback_identity_mismatch")
            return deepcopy(saved["measurements"])
        except KeyError:
            pass
        targets = self._targets()
        after = receipt.get("result_observation_id")
        results = []
        # A historical receipt can be replayed for idempotency. It must never
        # mutate target handles registered against a later physical state.
        if not self.harness.latest or after != self.harness.latest["observation_id"]:
            self.store.save("depth_action_feedback", action["action_id"], dict(depth_derived=True,
                identity=dict(self.harness.identity), action_id=action["action_id"],
                observation_id=after, measurements=results))
            return results
        for target in targets.values():
            before = target["observation_id"]
            if target["status"] != "tracked" or before != action.get("based_on_observation_id"):
                target["status"] = "stale"
                continue
            if not after:
                target["status"] = "uncertain"
                continue
            request = dict(kind="compare_regions", before_observation_id=before,
                           after_observation_id=after, camera_id=target["camera_id"], roi=target["roi"])
            if target.get("arm"):
                request["arm"] = target["arm"]
            if "seed_pixel" in target:
                request["seed_pixel"] = target["seed_pixel"]
            try:
                result = self._measure(request, correlation)
            except Exception as error:
                result = dict(status="uncertain", reason="measurement_failed:" + type(error).__name__, depth_derived=True)
            result["target_id"] = target["target_id"]
            results.append(result)
            roi = (result.get("value") or {}).get("after_roi")
            if result.get("status") == "supported" and roi:
                target.update(observation_id=after, roi=roi, status="tracked")
                seed = (result.get("value") or {}).get("after_seed_pixel")
                if seed is not None:
                    target["seed_pixel"] = seed
                elif "seed_pixel" in target:
                    target["status"] = "uncertain"
                    result.update(status="uncertain", reason="target_seed_correspondence_unavailable")
            else:
                target["status"] = "uncertain"
        self._save_targets(targets)
        self.store.save("depth_action_feedback", action["action_id"], dict(depth_derived=True,
            identity=dict(self.harness.identity), action_id=action["action_id"],
            observation_id=after, measurements=results))
        if results:
            self.store.event("depth.action_feedback", dict(action_id=action["action_id"], measurements=results), correlation)
        return results
