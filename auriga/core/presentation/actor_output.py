"""Model-facing projection; raw evidence and program protocol stay intact."""
from copy import deepcopy
from functools import partial
import json

from .actor_payload import lean_v2_display
from ..contracts import canonical
from ..tool_output import StructuredText, ToolResponse, output_contract_error, validate_display, wire_response

# These are known owner-envelope metadata, never a recursive field blacklist.
_ENVELOPE_METADATA = frozenset({"schema_version", "version", "versions", "revision",
    "correlation", "run_id", "episode_id", "env_instance_id", "env_generation",
    "source_sha256", "input_digest", "created_at", "updated_at",
    "sha256", "sha_256", "md5", "hash", "hashes", "source_url", "source_filename",
    "filename", "repository", "repository_heads", "source_fingerprints", "commit_hash", "calibration_id"})


def owner_envelope(value):
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {key: deepcopy(part) for key, part in value.items() if key not in _ENVELOPE_METADATA}
    identity = result.get("identity")
    if isinstance(identity, dict):
        # Robot physical identity survives alongside any enclosing run metadata.
        result["identity"] = {key: part for key, part in identity.items()
                              if key not in {"run_id", "episode_id", "env_instance_id", "env_generation"}}
        if not result["identity"]:
            result.pop("identity")
    return result


def _request_metadata(value):
    result = owner_envelope(value)
    if isinstance(result, dict) and result.get("type") == "BatchAction":
        for stage in result.get("stages", []):
            if isinstance(stage, dict) and isinstance(stage.get("action"), dict):
                stage["action"] = _request_metadata(stage["action"])
    return result


def _receipt_metadata(value):
    result = owner_envelope(value)
    if isinstance(result, dict) and isinstance(result.get("failure_feedback"), dict):
        # This is the known diagnostic envelope, not a map of caller labels.
        result["failure_feedback"].pop("schema_version", None)
    return result


def project_metadata(value, projection):
    """Copy known owner metadata for actor/program delivery without reducing evidence.

    Producers select the projection. Unknown business fields, label maps and
    executable proposals are not classified by their names or recursively pruned.
    This function never writes artifacts or changes the original typed result.
    """
    if projection == "data":
        return deepcopy(value)
    if projection in {"owner_record", "operation_review", "contact_overlay"}:
        return owner_envelope(value)
    if projection == "capabilities":
        result = owner_envelope(value)
        if isinstance(result.get("capabilities_delivery"), dict):
            result["capabilities_delivery"].pop("version", None)
        return result
    if projection == "action_plan":
        return _receipt_metadata(value)
    if projection in {"action_result", "action_display"}:
        result = _receipt_metadata(value)
        for key in ("request", "receipt", "settling"):
            if isinstance(result.get(key), dict):
                result[key] = (_request_metadata(result[key]) if key == "request"
                               else _receipt_metadata(result[key]))
        return result
    if projection == "action_history":
        if isinstance(value, list):
            return [project_metadata(item, "action_result") for item in value]
        result = owner_envelope(value)
        if isinstance(result.get("actions"), list):
            result["actions"] = [project_metadata(item, "action_result") for item in result["actions"]]
        return result
    if projection == "program_action_evidence":
        return ([_receipt_metadata(item) for item in value] if isinstance(value, list)
                else _receipt_metadata(value))
    if projection == "observation":
        result = owner_envelope(value)
        for key in ("bundle", "robot_state", "camera_calibration", "camera_pose_sample"):
            if isinstance(result.get(key), dict):
                result[key] = owner_envelope(result[key])
        bundle = result.get("bundle")
        if isinstance(bundle, dict) and isinstance(bundle.get("cameras"), list):
            bundle["cameras"] = [owner_envelope(camera) for camera in bundle["cameras"]]
            for camera in bundle["cameras"]:
                if isinstance(camera, dict) and isinstance(camera.get("image_encoding"), dict):
                    camera["image_encoding"] = owner_envelope(camera["image_encoding"])
        if isinstance(result.get("image_view"), dict):
            result["image_view"] = owner_envelope(result["image_view"])
            if isinstance(result["image_view"].get("encoding"), dict):
                result["image_view"]["encoding"] = owner_envelope(result["image_view"]["encoding"])
        for key in ("camera_calibration", "camera_pose_sample"):
            document = result.get(key)
            if isinstance(document, dict) and isinstance(document.get("cameras"), dict):
                document["cameras"] = {name: owner_envelope(camera)
                    for name, camera in document["cameras"].items()}
        delivery = result.get("metadata_delivery")
        if isinstance(delivery, dict) and isinstance(delivery.get("static_refs"), dict):
            delivery["static_refs"] = {name: owner_envelope(reference)
                for name, reference in delivery["static_refs"].items()}
        return result
    if projection == "geometry_result":
        result = owner_envelope(value)
        for key in ("algorithm", "implementation_digest", "library_versions", "duration_s"):
            result.pop(key, None)
        # value.proposal remains a complete ScenePatch for program consumers.
        return result
    if projection == "robot_information":
        result = owner_envelope(value)
        if isinstance(result.get("calibration"), dict):
            result["calibration"] = owner_envelope(result["calibration"])
        return result
    raise ValueError("unknown_actor_projection:" + str(projection))


COMPACT_DROPPED_TEXT_KEYS = frozenset({
    "semantics", "result_semantics", "delivery_semantics", "timeout_semantics",
    "contact_semantics", "execution_semantics", "observation_budget_semantics",
    "interpretation", "limitations", "synchronization", "calibration_consistency",
    "geometry_source", "base_source", "finger_geometry", "omission_meaning",
    "assessment_role", "image_scope", "unexecuted_scope", "detail_access"})
COMPACT_KEPT_REFS = frozenset({"trajectory_ref", "trajectory_feedback_ref", "resource_ref"})
COMPACT_FLOAT_DIGITS = 4


def compact_model_value(value):
    """Deterministic compact copy of a model-facing JSON value."""
    if isinstance(value, float):
        return round(value, COMPACT_FLOAT_DIGITS)
    if isinstance(value, list):
        return [compact_model_value(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str):
                if key == "unknown_values_are_null" and isinstance(item, bool):
                    continue
                if isinstance(item, str) and (key in COMPACT_DROPPED_TEXT_KEYS or key.endswith("_semantics")):
                    continue
                if key.endswith("_ref") and key not in COMPACT_KEPT_REFS:
                    continue
            result[key] = compact_model_value(item)
        return result
    return value


class ActorOutput:
    def __init__(self, store, presentation, payload_profile=None, *, compact=False):
        self.store, self.presentation = store, presentation
        self.payload_profile = payload_profile
        self.compact = compact

    def _project(self, value, projection):
        """Keep actor-only summarization separate from shared metadata cleanup."""
        result = project_metadata(value, projection)
        if projection == "capabilities":
            return actor_capabilities(result)
        if projection == "operation_review":
            reference = self.store.put_artifact(canonical(value).encode(),
                "operation_review_evidence", "application/json")
            execution = result.get("execution", {})
            result["execution"] = {k: deepcopy(execution[k]) for k in ("status", "blockers") if k in execution}
            result["execution"]["action_ids"] = [a["action_id"] for a in execution.get("actions", []) if "action_id" in a]
            result["details_ref"] = reference
            return result
        if projection == "contact_overlay":
            reference = self.store.put_artifact(canonical(value).encode(),
                "contact_overlay_details", "application/json")
            result["details_ref"] = reference
        return result

    def _present_part(self, item, display=None, compact=False):
        if isinstance(item, StructuredText):
            value = self.presentation.present(self._project(item.value, item.projection))
            if display:
                value = display(value)
            if compact:
                value = compact_model_value(value)
            return dict(type="inputText", text=canonical(value))
        # JSON text receives the same metadata projection as structured parts;
        # this never gives untyped text authority over execution evidence.
        part = self.presentation.present(item)
        if display and isinstance(part, dict) and part.get("type") == "inputText" and isinstance(part.get("text"), str):
            try:
                value = json.loads(part["text"])
            except ValueError:
                return part
            if isinstance(value, (dict, list)):
                part = dict(part, text=canonical(display(value)))
        return part

    def present(self, response, *, projection="data", tool=None, exact=False):
        if isinstance(response, ToolResponse):
            content, errors = [], []
            display = (partial(lean_v2_display, tool=tool)
                       if self.payload_profile == "lean_v2" and not exact else None)
            compact = self.compact and tool is not None
            for index, item in enumerate(response.content):
                try:
                    content.append(self._present_part(item, display, compact))
                except Exception:
                    # A failed optional presentation must not hide a physical
                    # receipt or become an input error suggesting another call.
                    errors.append(index)
            if errors:
                if response.kind == "action" and isinstance(response.data, dict):
                    receipt = response.data.get("receipt")
                    if isinstance(receipt, dict):
                        kept = {}
                        for key, value in receipt.items():
                            try:
                                validate_display(value)
                            except (TypeError, ValueError, OverflowError):
                                continue
                            kept[key] = value
                        if kept:
                            content.insert(0, self._present_part(StructuredText(dict(receipt=kept), "action_display")))
                failure = output_contract_error(ToolResponse(content, data=response.data,
                    kind=response.kind, success=False), tool, "actor_display_unavailable",
                    "/content/" + str(errors[0]))
                return wire_response(failure)
            return dict(success=response.success, contentItems=content)
        if isinstance(response, dict) and "contentItems" in response:
            # Explicit wire-only compatibility adapter. It never creates
            # typed evidence or dispatches a semantic projection by keys.
            return self.presentation.present(response)
        return self.presentation.present(self._project(response, projection))

    def restore(self, arguments):
        return self.presentation.restore(arguments)


def actor_capabilities(value):
    """Advertise current actor semantics while preserving native limits intact."""
    retired = {"arm_queue", "motion_cost_preview", "motion_preview", "trajectory_preview",
        "max_waypoints", "dual_arm_progression", "dual_arm_semantics", "waypoint_semantics",
        "waypoint_failure_semantics", "reset", "native_evaluator", "observer_video"}
    result = {key: deepcopy(part) for key, part in value.items() if key not in retired}
    speed = result.get("action_speed")
    if isinstance(speed, dict):
        speed.pop("waypoint_speed_scales", None)
        speed["semantics"] = "Each stage chooses its arm and jaw speed; scaled command spacing is not a measured physical speed guarantee."
        speed["gripper"] = "Slow jaws start from measured opening in the initial stage, then prior commanded goals in later compiled stages. Cached execution never rebases the jaw trajectory; invalid measured first steps reject before dispatch."
    result["stage_execution"] = "The entire batch is planned before dispatch. Planning failure executes nothing; physical failure can leave partial execution. Independent arm stages overlap; depends_on and observe express synchronization."
    return result
