"""Independent, CPU-only local scene store and finite proxy queries.

No image reconstruction or physics is implied by importing observations. Geometry
is supplied as an explicit evidence/assumption patch; all output is internal.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import hashlib
import itertools
import math
import os
from pathlib import Path
import tempfile
import time

from ...core.contracts import IDENTITY as IDENTITY_FIELDS, canonical, digest as _digest, validate_identity, validate_observation

ALGORITHM = "finite-proxy-stdlib-v1"


def _identity(value):
    result = {key: value[key] for key in IDENTITY_FIELDS}
    validate_identity({"schema_version": "0.1", **result})
    return result


def _encoded(value):
    return canonical(value).encode()


def _vector(value, length=3):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"expected a {length}-element vector")
    if any(type(item) not in (int, float) or not math.isfinite(item) for item in value):
        raise ValueError("vector values must be finite numbers")
    return tuple(float(item) for item in value)


def _sub(left, right):
    return tuple(a - b for a, b in zip(left, right))


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def _cross(left, right):
    a, b, c = left
    x, y, z = right
    return (b * z - c * y, c * x - a * z, a * y - b * x)


def _geometry(value):
    if not isinstance(value, dict):
        raise ValueError("geometry must be an object")
    geometry = deepcopy(value)
    kind = geometry.get("kind")
    if kind == "box":
        if set(geometry) != {"kind", "min_m", "max_m"}:
            raise ValueError("box accepts only kind, min_m, max_m")
        low, high = _vector(geometry["min_m"]), _vector(geometry["max_m"])
        if any(a >= b for a, b in zip(low, high)):
            raise ValueError("box requires min_m < max_m on every axis")
    elif kind == "plane":
        if set(geometry) != {"kind", "origin_m", "axis_u", "axis_v", "bounds_uv_m"}:
            raise ValueError("plane accepts only its finite analytic geometry fields")
        _vector(geometry["origin_m"])
        u, v = _vector(geometry["axis_u"]), _vector(geometry["axis_v"])
        if abs(_dot(u, u) - 1) > 1e-7 or abs(_dot(v, v) - 1) > 1e-7 or abs(_dot(u, v)) > 1e-7:
            raise ValueError("finite plane axes must be orthonormal")
        bounds = geometry["bounds_uv_m"]
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2 or any(_vector(bound, 2)[0] >= _vector(bound, 2)[1] for bound in bounds):
            raise ValueError("plane requires two finite support intervals")
    else:
        raise ValueError("unsupported_capability: only finite plane and axis-aligned box")
    return geometry


class SceneStore:
    """JSON manifests are immutable; a mutable head references one exact version."""

    def __init__(self, root, artifact_resolver=None, artifact_writer=None):
        self.artifact_resolver = artifact_resolver
        self.artifact_writer = artifact_writer
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve_artifact(self, reference):
        if self.artifact_resolver is None:
            raise ValueError("artifact_resolution_unavailable")
        metadata, content = self.artifact_resolver(reference)
        if hasattr(content, "read_bytes"):
            content = content.read_bytes()
        if metadata.get("access_scope") not in ("actor", "internal") or metadata.get("sha256") != hashlib.sha256(content).hexdigest() or metadata.get("byte_size") != len(content):
            raise ValueError("artifact_digest_size_or_access_mismatch")
        return metadata, content

    def resolve_json(self, reference):
        metadata, content = self.resolve_artifact(reference)
        if metadata.get("mime_type") != "application/json":
            raise ValueError("artifact_requires_json")
        return json.loads(content)

    def put_artifact(self, content, kind, mime_type):
        if self.artifact_writer is None:
            raise ValueError("artifact_writer_unavailable")
        return self.artifact_writer(content, kind, mime_type, access_scope="internal")

    def closure(self, value):
        """Resolve nested artifact references and copy their immutable content locally."""
        resolved = {}
        def walk(item):
            if isinstance(item, dict):
                for child in item.values(): walk(child)
            elif isinstance(item, (list, tuple)):
                for child in item: walk(child)
            elif isinstance(item, str) and item.startswith("artifact:") and item not in resolved:
                metadata, content = self.resolve_artifact(item)
                resolved[item] = metadata
                target = self.root / "resolved_blobs" / metadata["sha256"]
                target.parent.mkdir(exist_ok=True)
                if target.exists() and target.read_bytes() != content:
                    raise ValueError("immutable_artifact_conflict")
                if not target.exists():
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                        stream.write(content)
                        temporary = stream.name
                    try:
                        os.link(temporary, target)
                    except FileExistsError:
                        pass
                    finally:
                        os.unlink(temporary)
                if metadata.get("mime_type") == "application/json":
                    walk(json.loads(content))
        walk(value)
        return resolved

    @contextmanager
    def _writer(self):
        # ponytail: one store-wide lock; use namespace locks if write throughput matters.
        with (self.root / ".writer.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _namespace(self, identity):
        return self.root / _digest(_identity(identity))

    @staticmethod
    def _read(path):
        return json.loads(path.read_text())

    @staticmethod
    def _save(path, value, immutable=True):
        content = _encoded(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            stream.write(content)
            temporary = stream.name
        try:
            if immutable:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    if path.read_bytes() != content:
                        raise ValueError("immutable_content_conflict")
            else:
                os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def import_observation(self, bundle):
        bundle = deepcopy(bundle)
        _identity(bundle)
        if bundle.get("schema_version") != "0.1" or bundle.get("world_kind") != "observed":
            raise ValueError("observations must use schema 0.1 and world_kind=observed")
        if not isinstance(bundle.get("observation_id"), str) or not bundle["observation_id"]:
            raise ValueError("missing observation_id")
        validate_observation(bundle)
        if self.artifact_resolver is not None:
            artifacts = self.closure(bundle)
            if any(item.get("access_scope") != "actor" for item in artifacts.values()):
                raise ValueError("observations_require_permitted_actor_artifacts")
            validate_observation({**bundle,"resolved_payloads":[self.resolve_json(reference) for reference,item in artifacts.items() if item.get("mime_type")=="application/json"]})
        with self._writer():
            path = self._namespace(bundle) / "observations" / (_digest(bundle["observation_id"]) + ".json")
            self._save(path, bundle)
        return {"observation_id": bundle["observation_id"], "digest": _digest(bundle)}

    def observation(self, identity, observation_id):
        path = self._namespace(identity) / "observations" / (_digest(observation_id) + ".json")
        if not path.exists():
            raise ValueError("unknown_observation")
        return self._read(path)

    def head(self, identity):
        path = self._namespace(identity) / "head.json"
        if not path.exists():
            return {**_identity(identity), "scene_version": 0, "world_kind": "internal"}
        return self._read(path)

    def version(self, identity, scene_version):
        if type(scene_version) is not int or scene_version < 1:
            raise ValueError("scene_version must identify an existing positive version")
        path = self._namespace(identity) / "versions" / f"{scene_version:012}.json"
        if not path.exists():
            raise ValueError("unknown_scene_version")
        return self._read(path)

    def versions(self, identity):
        head = self.head(identity)["scene_version"]
        return [{"scene_version": version, "digest": _digest(self.version(identity, version))} for version in range(1, head+1)]

    def read_snapshot(self, identity, snapshot_id):
        if not isinstance(snapshot_id,str) or not snapshot_id.startswith("snapshot-") or len(snapshot_id)!=73 or any(char not in "0123456789abcdef" for char in snapshot_id[9:]):
            raise ValueError("invalid_snapshot_id")
        manifest = self._read(self._namespace(identity)/"snapshots"/(snapshot_id+".json"))
        expected = manifest.pop("manifest_digest")
        identifier = manifest.get("snapshot_id")
        identity_content = {key:value for key,value in manifest.items() if key!="snapshot_id"}
        if identifier != snapshot_id or identifier != "snapshot-"+_digest(identity_content):
            raise ValueError("snapshot_content_address_mismatch")
        if _digest(manifest)!=expected:
            raise ValueError("snapshot_manifest_digest_mismatch")
        manifest["manifest_digest"] = expected
        for metadata in manifest.get("resolved_artifacts",{}).values():
            content = (self.root/"resolved_blobs"/metadata["sha256"]).read_bytes()
            if hashlib.sha256(content).hexdigest()!=metadata["sha256"] or len(content)!=metadata["byte_size"]:
                raise ValueError("snapshot_artifact_digest_mismatch")
        return manifest

    def apply_patch(self, patch):
        identity = _identity(patch)
        if patch.get("schema_version") != "0.1" or patch.get("world_kind", "internal") != "internal":
            raise ValueError("scene patches must use schema 0.1 and world_kind=internal")
        if type(patch.get("base_scene_version")) is not int or patch["base_scene_version"] < 0:
            raise ValueError("invalid_base_scene_version")
        observation_ids = patch.get("observation_ids")
        operations = patch.get("operations")
        if not isinstance(observation_ids, list) or not observation_ids or any(not isinstance(item, str) or not item for item in observation_ids):
            raise ValueError("patch requires nonempty observation_ids")
        if not isinstance(operations, list) or not operations or len(operations) > 64 or any(not isinstance(item, dict) for item in operations):
            raise ValueError("patch requires 1..64 declarative operations")
        observations = [self.observation(identity, item) for item in patch.get("observation_ids", [])]
        if not observations:
            raise ValueError("scene patches require imported evidence observations")
        if not patch.get("builder_digest"):
            raise ValueError("missing builder_digest")
        with self._writer():
            head = self.head(identity)
            if patch.get("base_scene_version") != head["scene_version"]:
                if patch.get("idempotency_token") and head["scene_version"] > patch["base_scene_version"]:
                    committed = self.version(identity,patch["base_scene_version"]+1)
                    if committed.get("idempotency_token") == patch["idempotency_token"]:
                        if committed.get("patch_digest") != _digest(patch):
                            raise ValueError("idempotency_conflict")
                        return committed
                raise ValueError("scene_version_conflict")
            previous = self.version(identity, head["scene_version"]) if head["scene_version"] else {}
            calibration_ids = {observation.get("calibration_id") for observation in observations}
            robot_models = {observation.get("robot_model_id") for observation in observations}
            if len(calibration_ids) != 1 or len(robot_models) != 1:
                raise ValueError("inconsistent_calibration_or_robot_model")
            if previous and not patch.get("rebuild", False) and (previous["calibration_id"] not in calibration_ids or previous["robot_model_id"] not in robot_models):
                raise ValueError("unsupported_capability: rebuild required after calibration/model change")
            if previous and not patch.get("rebuild",False) and patch.get("frame_id", previous["frame_id"]) != previous["frame_id"]:
                raise ValueError("frame_change_requires_rebuild")
            entities = {} if patch.get("rebuild",False) else deepcopy(previous.get("entities", {}))
            unknown = [{"region":"previous_model_geometry","reason":"Explicit rebuild invalidated prior model geometry."}] if patch.get("rebuild",False) else deepcopy(previous.get("unknown_regions", []))
            for operation in patch.get("operations", []):
                source = operation.get("source_kind")
                if source not in ("observation_inference", "explicit_prior"):
                    raise ValueError("each operation needs observation_inference or explicit_prior source")
                assumptions = operation.get("assumptions", [])
                if not isinstance(assumptions, list) or any(not isinstance(item, str) or not item for item in assumptions):
                    raise ValueError("assumptions must be a list of named conditions")
                if source == "explicit_prior" and not assumptions:
                    raise ValueError("explicit_prior requires named assumptions")
                kind = operation.get("op")
                if kind == "mark_unknown":
                    unknown.append({"region": operation["region"], "reason": operation["reason"], "assumptions": assumptions})
                    continue
                entity_id = operation.get("entity_id")
                if not isinstance(entity_id, str) or not entity_id:
                    raise ValueError("missing entity_id")
                evidence = operation.get("observation_ids", observation_ids) if kind == "upsert_geometry" else observation_ids
                if (not isinstance(evidence, list) or not evidence
                        or any(not isinstance(item, str) for item in evidence)
                        or not set(evidence).issubset(observation_ids)):
                    raise ValueError("entity evidence must belong to patch observations")
                entity_observations = [item for item in observations if item["observation_id"] in evidence]
                clocks = {(item.get("step_domain"), item.get("clock_domain"))
                          for item in entity_observations if item.get("sim_step") is not None}
                if len(clocks) > 1:
                    raise ValueError("incomparable_entity_clock")
                latest = max(entity_observations, key=lambda item: (isinstance(item.get("sim_step"), int), item.get("sim_step") or 0))
                latest_step = latest.get("sim_step")
                if entity_id in entities and latest_step is None and entities[entity_id].get("last_sim_step") is not None:
                    raise ValueError("undated_observation_cannot_overwrite_entity")
                if entity_id in entities and latest_step is not None and entities[entity_id].get("last_sim_step") is not None:
                    if latest.get("step_domain") != entities[entity_id].get("step_domain") or latest.get("clock_domain") != entities[entity_id].get("clock_domain"):
                        raise ValueError("incomparable_entity_clock")
                    if latest_step < entities[entity_id]["last_sim_step"]:
                        raise ValueError("late_observation_cannot_overwrite_entity")
                if kind == "upsert_geometry":
                    entities[entity_id] = {"geometry": _geometry(operation["geometry"]), "state": "tracked", "source_kind": source,
                                           "observation_ids": evidence, "assumptions": assumptions, "uncertainty": operation.get("uncertainty", "unquantified"),
                                           "last_observation_id": latest["observation_id"], "last_sim_step": latest_step, "step_domain": latest.get("step_domain"), "clock_domain": latest.get("clock_domain"),
                                           "evidence": deepcopy(operation.get("evidence", {})), "support_points_m": operation.get("support_points_m", []),
                                           "support_polygon_uv_m": operation.get("support_polygon_uv_m"), "association": operation.get("association", {}), "velocity": operation.get("association", {}).get("velocity", "unknown"), "contact_state": "unknown"}
                elif kind == "update_pose":
                    if entity_id not in entities:
                        raise ValueError("unknown_entity")
                    if "T_delta" in operation:
                        if "translation_m" in operation: raise ValueError("ambiguous_pose_update")
                        matrix = [_vector(row,4) for row in operation["T_delta"]]
                        if len(matrix)!=4 or any(abs(a-b)>1e-7 for a,b in zip(matrix[3],(0,0,0,1))): raise ValueError("invalid_rigid_pose_update")
                        rotation = [row[:3] for row in matrix[:3]]
                        if any(abs(_dot(rotation[i],rotation[j])-(i==j))>1e-6 for i in range(3) for j in range(3)) or _dot(_cross(rotation[0],rotation[1]),rotation[2])<0.999999: raise ValueError("invalid_rigid_pose_update")
                        delta = tuple(row[3] for row in matrix[:3])
                    else:
                        delta = _vector(operation["translation_m"])
                        rotation = ((1,0,0),(0,1,0),(0,0,1))
                    def moved(point): return [_dot(row,point)+offset for row,offset in zip(rotation,delta)]
                    entity = entities[entity_id]
                    old_geometry = deepcopy(entity["geometry"])
                    geometry = deepcopy(old_geometry)
                    if geometry["kind"] == "box":
                        corners = [moved(point) for point in itertools.product(*zip(geometry["min_m"],geometry["max_m"]))]
                        geometry["min_m"] = [min(point[index] for point in corners) for index in range(3)]
                        geometry["max_m"] = [max(point[index] for point in corners) for index in range(3)]
                        if rotation != ((1,0,0),(0,1,0),(0,0,1)):
                            assumptions = assumptions+["Rotated box uses a conservative axis-aligned envelope; orientation and true faces are not represented."]
                    else:
                        geometry["origin_m"] = moved(geometry["origin_m"])
                        for field in ("axis_u","axis_v"):
                            geometry[field] = [_dot(row,geometry[field]) for row in rotation]
                    entity.update(geometry=_geometry(geometry), state="tracked", last_observation_id=latest["observation_id"], last_sim_step=latest_step,
                                  step_domain=latest.get("step_domain"), clock_domain=latest.get("clock_domain"), observation_ids=patch["observation_ids"], velocity="unknown", contact_state="unknown",
                                  assumptions=assumptions, source_kind=source)
                    entity["support_points_m"] = [moved(point) for point in entity.get("support_points_m", [])]
                    unknown.append({"region": {"previous_entity_geometry": old_geometry}, "reason": "Object moved; old region requires fresh observation."})
                elif kind == "set_physics_hypotheses":
                    if entity_id not in entities:
                        raise ValueError("unknown_entity")
                    hypotheses = operation.get("hypotheses")
                    if source != "explicit_prior" or not isinstance(hypotheses, dict) or not hypotheses:
                        raise ValueError("physics_hypotheses_require_explicit_prior")
                    entities[entity_id]["physics_hypotheses"] = {"values": hypotheses, "source_kind": source, "assumptions": assumptions}
                elif kind in ("mark_lost", "retire_entity", "mark_occluded", "mark_dirty"):
                    if entity_id not in entities:
                        raise ValueError("unknown_entity")
                    entities[entity_id]["state"] = {"mark_lost":"lost", "retire_entity":"retired", "mark_occluded":"occluded", "mark_dirty":"dirty"}[kind]
                    entities[entity_id].update(last_sim_step=latest_step, step_domain=latest.get("step_domain"), clock_domain=latest.get("clock_domain"), last_observation_id=latest["observation_id"], possible_position="unquantified", contact_state="unknown")
                    entities[entity_id]["state_reason"] = operation["reason"]
                    if operation.get("association"): entities[entity_id]["association"] = operation["association"]
                else:
                    raise ValueError("unsupported_capability: unsupported patch operation")
            scene = {"schema_version": "0.1", **identity, "world_kind": "internal", "scene_version": head["scene_version"] + 1,
                     "patch_digest": _digest(patch), "idempotency_token": patch.get("idempotency_token"), "frame_id": patch.get("frame_id", previous.get("frame_id")), "observation_ids": sorted(set(previous.get("observation_ids", []) + patch["observation_ids"])),
                     "calibration_id": next(iter(calibration_ids)), "robot_model_id": next(iter(robot_models)), "builder_digest": patch["builder_digest"],
                     "entities": entities, "unknown_regions": unknown, "build_kind": patch.get("build_kind", previous.get("build_kind", "supplied_geometry")), "limitations": ["Unknown space is not free space.", "Finite proxies do not imply complete object extent.", "No physics, IK, or observed-world execution."]}
            if not isinstance(scene["frame_id"], str) or not scene["frame_id"]:
                raise ValueError("missing frame_id")
            origins = {observation.get("data_origin") for observation in observations}
            if "synthetic_fixture" in origins or previous.get("data_origin") == "synthetic_fixture":
                scene["data_origin"] = "synthetic_fixture"
            path = self._namespace(identity)
            self._save(path / "versions" / f"{scene['scene_version']:012}.json", scene)
            self._save(path / "head.json", {**identity, "scene_version": scene["scene_version"], "world_kind": "internal", "digest": _digest(scene)}, immutable=False)
        return scene

    def snapshot(self, identity, scene_version, anchor_observation_id):
        scene = self.version(identity, scene_version)
        anchor = self.observation(identity, anchor_observation_id)
        if anchor_observation_id not in scene["observation_ids"]:
            raise ValueError("anchor observation is not evidence for this scene")
        if self.artifact_resolver and not anchor.get("robot_model_ref"):
            raise ValueError("snapshot_requires_resolved_robot_model")
        closure = self.closure({"scene": scene, "observations": [self.observation(identity, item) for item in scene["observation_ids"]]}) if self.artifact_resolver else None
        robot_state = self.resolve_json(anchor["robot_state_ref"]) if closure is not None else None
        qdot = {name:{"joint_names":arm.get("joint_names"),"qdot_rad_s":arm.get("qdot_rad_s","unknown"),"source_kind":arm.get("source_kind","unknown")} for name,arm in (robot_state or {}).get("arms",{}).items()} or (robot_state or {}).get("qdot","unknown")
        manifest = {"schema_version": "0.1", **_identity(identity), "world_kind": "internal", "scene_version": scene_version,
                    "scene_digest": _digest(scene), "scene": scene, "anchor_observation_id": anchor_observation_id, "anchor_digest": _digest(anchor),
                    "calibration_id": scene["calibration_id"], "robot_model_id": scene["robot_model_id"], "observation_profile": anchor.get("observation_profile"),
                    "initial_state": {"robot_state_ref": anchor.get("robot_state_ref"), "robot_state": robot_state, "qdot": qdot, "object_velocity": "unknown", "contact_state": "unknown"},
                    "quality": {"artifact_resolution": "not_loaded", "limitation": "Geometry-only snapshot. Robot state and RGB artifact references require content verification before any future physics initialization."},
                    "execution_settings": {"initialization_strategy": "geometry_only", "physics": "unsupported", "controller": "unsupported", "algorithm": ALGORITHM}}
        if closure is not None:
            manifest["resolved_artifacts"] = closure
            manifest["quality"] = {"artifact_resolution": "verified_digest_closed", "limitation": "Geometry-only initialization; physical contact and missing velocities remain unknown."}
            manifest["frozen_observations"] = [self.observation(identity, item) for item in scene["observation_ids"]]
        if scene.get("data_origin"):
            manifest["data_origin"] = scene["data_origin"]
        manifest["snapshot_id"] = "snapshot-" + _digest(manifest)
        manifest["manifest_digest"] = _digest(manifest)
        with self._writer():
            self._save(self._namespace(identity) / "snapshots" / (manifest["snapshot_id"] + ".json"), manifest)
        return manifest

    def query(self, identity, scene_version, request):
        started = time.monotonic()
        scene = self.version(identity, scene_version)
        result = {"schema_version": "0.1", **_identity(identity), "world_kind": "internal", "scene_version": scene_version,
                  "scene_digest": _digest(scene), "query_type": request.get("kind"), "frame_id": scene["frame_id"], "unit": "m", "algorithm": ALGORITHM,
                  "status": "unknown", "value": None, "observation_ids": scene["observation_ids"], "unknown_regions": scene["unknown_regions"], "assumptions": [],
                  "limitations": scene["limitations"], "applicability": "requires_main_harness_current_observation_check"}
        if scene.get("data_origin"):
            result["data_origin"] = scene["data_origin"]
        entity = scene["entities"].get(request.get("entity_id"))
        try:
            if request.get("frame_id", scene["frame_id"]) != scene["frame_id"]:
                raise ValueError("unsupported_frame: queries must use the frozen scene frame")
            if request.get("kind") not in ("ray_surface", "distance", "entity_pose"):
                result["reason"] = "unsupported_capability"
            elif not entity or entity["state"] != "tracked":
                result["reason"] = "entity_unknown_or_untracked"
            else:
                result["assumptions"] = entity["assumptions"]
                result["observation_ids"] = entity["observation_ids"]
                result["uncertainty"] = entity["uncertainty"]
                geometry = entity["geometry"]
                if request["kind"] == "entity_pose":
                    result["value"] = {"geometry": geometry, "state": entity["state"], "source_kind": entity["source_kind"]}
                elif request["kind"] == "distance":
                    points = [_vector(item) for item in request["path_points_m"]] if "path_points_m" in request else [_vector(request["point_m"])]
                    if not points or len(points)>2000: raise ValueError("invalid_distance_samples")
                    distances = [(math.dist(point,_nearest(geometry,point)),index,_nearest(geometry,point)) for index,point in enumerate(points)]
                    distance,index,nearest = min(distances)
                    result["value"] = {"distance_m": distance, "nearest_point_m": nearest, "sample_index":index, "sample_count":len(points), "max_sample_spacing_m":max((math.dist(a,b) for a,b in zip(points,points[1:])),default=0), "scope": "specified_known_proxy_only; discrete samples, not continuous collision"}
                else:
                    result["value"] = _ray_hit(geometry, _vector(request["origin_m"]), _vector(request["direction"]))
                    if result["value"] is None:
                        result["reason"] = "no_hit_on_finite_proxy; other_space_unknown"
                if result["value"] is not None:
                    result["status"] = "conditional" if entity["assumptions"] else "supported"
        except (KeyError, TypeError, ValueError) as error:
            result.update(status="invalid_input", reason=str(error))
        result["duration_s"] = time.monotonic() - started
        return result

    def rollout(self, request):
        if request.get("world_kind", "internal") != "internal" or "submit_to_observed" in request:
            raise ValueError("internal service has no observed-world execution permission")
        return {"schema_version": "0.1", "world_kind": "internal", "status": "unsupported", "error": "unsupported_capability",
                "reason": "No independent Isaac worker or validated motion/physics backend is installed in this module.", "snapshot_id": request.get("snapshot_id"), "executed": False}

    @staticmethod
    def capabilities():
        return {"schema_version": "0.1", "world_kind": "internal", "primitives": ["finite_plane", "axis_aligned_box"],
                "queries": ["ray_surface", "distance", "entity_pose"], "patch_operations": ["upsert_geometry", "update_pose", "set_physics_hypotheses", "mark_unknown", "mark_lost", "mark_occluded", "mark_dirty", "retire_entity"],
                "rgb_reconstruction": "calibrated_sparse_finite_plane_box", "geometry_tools": ["fk", "camera_pose", "pixel_ray", "project_points", "project_geometry", "preview_ee", "sparse_track", "triangulate", "build_scene", "alignment"], "physics": "unsupported", "observed_execution": "unsupported", "worker_ready": False}


def _nearest(geometry, point):
    if geometry["kind"] == "box":
        return tuple(max(low, min(high, value)) for value, low, high in zip(point, geometry["min_m"], geometry["max_m"]))
    origin, u, v = geometry["origin_m"], geometry["axis_u"], geometry["axis_v"]
    offset = _sub(point, origin)
    coordinates = [max(low, min(high, _dot(offset, axis))) for axis, (low, high) in zip((u, v), geometry["bounds_uv_m"])]
    return tuple(origin[index] + coordinates[0] * u[index] + coordinates[1] * v[index] for index in range(3))


def _ray_hit(geometry, origin, direction):
    length = math.hypot(*direction)
    if length == 0 or not math.isfinite(length):
        raise ValueError("ray direction must have a finite nonzero magnitude")
    direction = tuple(value / length for value in direction)
    if geometry["kind"] == "plane":
        normal = _cross(geometry["axis_u"], geometry["axis_v"])
        divisor = _dot(direction, normal)
        if abs(divisor) < 1e-9:
            return None
        distance = _dot(_sub(geometry["origin_m"], origin), normal) / divisor
        point = tuple(value + distance * delta for value, delta in zip(origin, direction))
        if distance < 0 or math.dist(point, _nearest(geometry, point)) > 1e-8:
            return None
    else:
        entry, exit_ = -math.inf, math.inf
        for value, delta, low, high in zip(origin, direction, geometry["min_m"], geometry["max_m"]):
            if abs(delta) < 1e-12:
                if not low <= value <= high:
                    return None
                continue
            first, last = sorted(((low - value) / delta, (high - value) / delta))
            entry, exit_ = max(entry, first), min(exit_, last)
        if exit_ < max(entry, 0):
            return None
        distance = entry if entry >= 0 else exit_
        point = tuple(value + distance * delta for value, delta in zip(origin, direction))
    result = {"point_m": point, "ray_distance_m": distance, "scope": "specified_finite_proxy_only"}
    if geometry["kind"]=="plane":
        result.update(incidence_angle_deg=math.degrees(math.acos(min(1,abs(divisor)))),ray_plane_condition_factor=1/abs(divisor))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capabilities", "replay", "query", "rollout"))
    parser.add_argument("request", nargs="?")
    parser.add_argument("--store", default="runs/local_scene")
    arguments = parser.parse_args()
    store = SceneStore(arguments.store)
    if arguments.command == "capabilities":
        output = store.capabilities()
    else:
        request = json.loads(Path(arguments.request).read_text())
        if arguments.command == "replay":
            if request.get("artifacts"):
                package = Path(arguments.request).resolve().parent
                metadata = {"artifact:"+item["artifact_id"]:item for item in request["artifacts"]}
                def resolve(reference):
                    item = metadata[reference]
                    path = (package/item["storage_key"]).resolve()
                    if not path.is_relative_to(package): raise ValueError("artifact_outside_replay_package")
                    return item,path.read_bytes()
                store = SceneStore(arguments.store, resolve)
            for observation in request["observations"]:
                store.import_observation(observation)
            scenes = [store.apply_patch(patch) for patch in request["patches"]]
            output = {"scenes": scenes, "results": [store.query(query, query["scene_version"], query) for query in request.get("queries", [])]}
        elif arguments.command == "query":
            output = store.query(request, request["scene_version"], request)
        else:
            output = store.rollout(request)
    print(json.dumps(output, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
