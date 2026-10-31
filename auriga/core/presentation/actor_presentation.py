"""Present public robot evidence without exposing storage/build provenance.

The store, its integrity checks and execution identifiers remain unchanged.
Only publication copies are projected. Artifact aliases are persisted so that
model-authored programs and resumed sessions can still address exact evidence.
"""

from copy import deepcopy
import json
import re

from ..contracts import canonical


_ARTIFACT = re.compile(r"artifact:[0-9a-f]{64}-[0-9a-f]{16}(?![0-9a-f])")
_ARTIFACT_ID = re.compile(r"[0-9a-f]{64}-[0-9a-f]{16}\Z")
_ALIAS = re.compile(r"artifact:ref_[A-Za-z0-9_-]+")
# A run of decimal digits is a number (counter, timestamp), not a checksum.
_HASH = re.compile(r"(?=[0-9]*[a-fA-F])[0-9a-fA-F]{16,128}\Z")
_HASH_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:0[xX])?(?=[0-9]*[a-fA-F])[0-9a-fA-F]{16,128}(?![A-Za-z0-9])")
_LABELED_HASH = re.compile(r"\b(?:[A-Za-z][A-Za-z0-9_-]*[_-])?(?:sha(?:[-_ ]?\d+)?|md5|crc32|(?:git[-_ ]?)?commit|hash|digest|checksum|fingerprint)"
    r"\s*(?:[:=_-]\s*|\s+)(?:[0-9a-f]{7,128}|[A-Za-z0-9+/=_-]{22,})(?![A-Za-z0-9])", re.IGNORECASE)
_LONG_ID = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z][A-Za-z0-9]*)*?)[-_]"
    r"[0-9a-f]{12,128}(?:[-_][0-9a-f]{12,128})*\b")
_CLOCK_DOMAIN = re.compile(_LONG_ID.pattern + r"/generation-[0-9]+\b")
_SHORT_ID = re.compile(r"\b(?:obs|act|job|call|plan|exp|review|clock|id)_[0-9]+\b")
_STAGE_MAPS = frozenset({"stages", "stage_results", "checkpoint_reasons", "stage_costs", "stage_overrides"})
_STAGE_LISTS = frozenset({"pending_stage_ids", "completed_stage_ids", "remaining_stage_ids", "checkpoint_stage_ids", "depends_on"})
_ID_PREFIX = {"observation": "obs", "obs": "obs", "action": "act", "action-plugin": "act",
    "program": "job", "job": "job", "review": "review", "task-plan": "plan", "grasp-plan": "plan",
    "preview": "plan", "batch": "plan", "plan": "plan", "shared_experience": "exp",
    **{name: "id" for name in ("snapshot", "entity", "grasp", "grasp-memory", "scene-job")}}
_HASH_FIELD = re.compile(r"(?:^|_)(?:sha(?:_?\d+)?|sh_?265|md5|crc(?:_?\d+)?|blake\d*|shasum|hash(?:es)?|digests?|checksums?|fingerprints?)(?:_|$)")
_DOCUMENT_FILE = re.compile(r"(?:^|/)[^/\s]+\.(?:urdf|usd|stl|glb|py|json|ya?ml|toml|md|pdf)(?::\d+(?:-\d+)?)?\Z", re.IGNORECASE)
_DOCUMENT_FIELDS = frozenset({
    "source_url", "source_urls", "source_link", "source_links", "source_filename",
    "source_file", "source_path", "official_sources", "repository", "repository_url",
    "repo", "repo_url", "commit", "commit_id", "commit_hash", "revision_commit", "filename",
    "documentation_url", "documentation_urls", "reference_url", "reference_urls",
    "urdf_path", "robot_model_urdf_path", "source_fingerprints",
})


def _alias_prefix(identifier):
    """Short-ID namespace for a matched long ID; a program call is not its job."""
    name = identifier.group(1)
    if name == "program" and identifier.group().count("-") > 1:
        return "call"
    return _ID_PREFIX.get(name, "act" if name.startswith("action-") else None)


def _document_location(value):
    if not isinstance(value, str):
        return False
    return (value.startswith(("https://", "http://", "file://", "/", "./", "../"))
            or _DOCUMENT_FILE.search(value) is not None)


def _document_collection(value):
    """Recognize source-link collections, preserving observation evidence lists."""
    if isinstance(value, str):
        return _document_location(value)
    if isinstance(value, dict):
        return bool(value) and all(_document_collection(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_document_collection(item) for item in value)
    return False


def _omit_field(key, value):
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower()
    if (_HASH_FIELD.search(normalized) or normalized in _DOCUMENT_FIELDS
            or normalized.startswith("repository_")
            or normalized.endswith(("_repository", "_filename", "_commit"))):
        return True
    if normalized not in {"image_url", "audio_url"} and normalized.endswith(("_url", "_urls")):
        return True
    if normalized in {"version", "calibration_id"} and isinstance(value, str) and _HASH.fullmatch(value):
        return True
    if normalized in {"source", "sources", "reference", "references", "provenance"} and _document_collection(value):
        return True
    # A URL inside source metadata is documentary; source_kind and measured
    # observation references have independent physical meaning and stay intact.
    return normalized in {"url", "link"} and _document_location(value)


def _replace_hashes(text, replacement):
    numbers = [match.span() for match in re.finditer(
        r"(?<![A-Za-z0-9_])[-+]?(?:(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?|\d+[eE][-+]?\d+)(?![A-Za-z0-9_])", text)]
    index = 0
    def replace(match):
        nonlocal index
        while index < len(numbers) and numbers[index][1] <= match.start():
            index += 1
        if index < len(numbers) and numbers[index][0] <= match.start() and match.end() <= numbers[index][1]:
            return match.group()
        return replacement(match)
    return _HASH_TOKEN.sub(replace, text)


def metadata_findings(text):
    """Name the Harness metadata kinds still present in model-readable text; read-only.

    JSON text is decoded first, as presentation does, so escaped log text is
    judged by its actual characters.
    """
    kinds = set()

    def scan(value):
        if _ARTIFACT.search(value):
            kinds.add("raw_artifact_reference")
        if _CLOCK_DOMAIN.search(value) or any(_alias_prefix(match) for match in _LONG_ID.finditer(value)):
            kinds.add("raw_identifier")
        if _LABELED_HASH.search(value):
            kinds.add("labeled_checksum")
        _replace_hashes(value, lambda match: kinds.add("checksum") or match.group())

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"imageUrl", "image_url", "audioUrl", "audio_url", "data_base64"} or (
                        key == "url" and value.get("type") in {"image", "audio", "input_image", "input_audio"}):
                    continue
                if _omit_field(key, item) or isinstance(key, str) and _HASH.fullmatch(key):
                    kinds.add("provenance_field")
                if isinstance(key, str):
                    scan(key)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            text_value(value)

    def text_value(value):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, (dict, list)):
            walk(decoded)
            return
        for line in value.splitlines() or [value]:
            try:
                decoded = json.loads(line)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, (dict, list)):
                walk(decoded)
            else:
                scan(line)

    text_value(text)
    return sorted(kinds)


def strip_hashes(text):
    """Remove checksum prose and bare digests; keep numeric measurements exact."""
    return _replace_hashes(_LABELED_HASH.sub("", text), lambda match: "")


class ActorPresentation:
    """Copy evidence for an actor, with stable reversible artifact addresses."""

    def __init__(self, store):
        self.store = store
        self._aliases = {}

    def _alias(self, reference):
        # All owners sharing this Store serialize allocation through its RLock.
        # Reloading state also covers independently created presentation objects.
        with self.store.lock:
            if reference in self._aliases:
                return self._aliases[reference]
            records = self.store.states("actor_reference")
            for record in records:
                self._aliases[record["reference"]] = record["alias"]
            if reference in self._aliases:
                return self._aliases[reference]
            number = len(records) + 1
            while self.store.read_state("actor_reference", "ref_" + str(number)) is not None:
                number += 1
            identifier = "ref_" + str(number)
            alias = "artifact:" + identifier
            self.store.set_state("actor_reference", identifier,
                                 dict(alias=alias, reference=reference))
            self._aliases[reference] = alias
            return alias

    def _reference(self, alias):
        identifier = alias.removeprefix("artifact:")
        record = self.store.read_state("actor_reference", identifier)
        if (not isinstance(record, dict) or record.get("alias") != alias
                or not isinstance(record.get("reference"), str)
                or _ARTIFACT.fullmatch(record["reference"]) is None):
            raise ValueError("unknown_actor_artifact_reference")
        return record["reference"]

    def _id_alias(self, value, prefix="id"):
        with self.store.lock:
            records = self.store.states("actor_id")
            existing = next((record for record in records if record["original"] == value), None)
            if existing:
                return existing["alias"]
            number = 1 + sum(record["alias"].startswith(prefix + "_") for record in records)
            alias = prefix + "_" + str(number)
            self.store.set_state("actor_id", alias, dict(alias=alias, original=value))
            return alias

    def _log_paths(self, text):
        """Name program files relative to their workspace; run-directory paths are storage layout."""
        root = getattr(self.store, "root", None)
        if root is None or str(root) not in text:
            return text
        text = re.sub(re.escape(str(root)) + r"/private/program-workspaces/[^/\s\"']+/", "", text)
        return text.replace(str(root), "<run>")

    def present_identifiers(self, text):
        """Bind executable references to the same aliases as program replies."""
        text = _ARTIFACT.sub(lambda match: self._alias(match.group()), text)
        text = _CLOCK_DOMAIN.sub(lambda match: self._id_alias(match.group(), "clock"), text)
        def identifier(match):
            prefix = _alias_prefix(match)
            if prefix:
                return self._id_alias(match.group(), prefix)
            existing = next((record for record in self.store.states("actor_id")
                             if record["original"] == match.group()), None)
            return existing["alias"] if existing else match.group()
        return _LONG_ID.sub(identifier, text)

    def _present_text(self, text, *, json_text=False, alias_values=False):
        if json_text:
            try:
                value = json.loads(text)
            except (TypeError, ValueError):
                # Programs commonly print complete JSON objects among ordinary
                # log lines. Project those lines; leave other text exact.
                lines = []
                for line in text.splitlines(keepends=True):
                    try:
                        value = json.loads(line)
                    except (TypeError, ValueError):
                        lines.append(line)
                    else:
                        if isinstance(value, (dict, list)):
                            body = line.rstrip("\r\n")
                            lines.append(canonical(self._present(value)) + line[len(body):])
                        else:
                            lines.append(line)
                text = "".join(lines)
            else:
                if isinstance(value, (dict, list)):
                    return canonical(self._present(value))
        if not alias_values:
            text = _LABELED_HASH.sub("", text)
        text = self.present_identifiers(text)
        if alias_values:
            text = _LONG_ID.sub(lambda match: self._id_alias(match.group()), text)
            return _replace_hashes(text, lambda match: self._id_alias(match.group()))
        text = _LONG_ID.sub(lambda match: strip_hashes(match.group()), text)
        return strip_hashes(text)

    def _present(self, value, *, log_excerpt=False, documentary=True, label_keys=False):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == "url" and value.get("type") in {"image", "audio", "input_image", "input_audio"}:
                    result[key] = deepcopy(item)
                    continue
                if documentary and not label_keys and (_omit_field(key, item) or isinstance(key, str) and _HASH.fullmatch(key)):
                    continue
                presented_key = self._present_text(key, alias_values=True) if isinstance(key, str) else deepcopy(key)
                json_text = documentary and (key in {"stdout_text", "stderr_text", "stdout_excerpt", "stderr_excerpt"}
                             or (key == "text" and (value.get("type") in {"inputText", "text", "input_text", "output_text"} or log_excerpt)))
                if isinstance(item, str):
                    if key in {"stdout_text", "stderr_text"}:
                        item = self._log_paths(item)
                    if key in {"imageUrl", "image_url", "audioUrl", "audio_url", "data_base64"}:
                        result[presented_key] = item
                    elif key == "artifact_id" and _ARTIFACT_ID.fullmatch(item):
                        result[presented_key] = self._alias("artifact:" + item).removeprefix("artifact:")
                    elif (key == "id" or key.endswith(("_id", "_ref"))) and (identifier := _LONG_ID.fullmatch(item)):
                        result[presented_key] = self._id_alias(item, _alias_prefix(identifier) or "id")
                    elif _HASH.fullmatch(item):
                        if key in {"description", "title", "$comment"}:
                            continue
                        if not documentary or key == "id" or key.endswith(("_id", "_ref")):
                            result[presented_key] = self._id_alias(item)
                    else:
                        result[presented_key] = self._present_text(item, json_text=json_text,
                            alias_values=not documentary and key not in {"description", "title", "$comment"})
                elif key in _STAGE_LISTS and isinstance(item, list):
                    result[presented_key] = [self._present_text(label, alias_values=True)
                        if isinstance(label, str) else self._present(label, documentary=documentary) for label in item]
                else:
                    result[presented_key] = self._present(item,
                        log_excerpt=key in {"stdout", "stderr", "stdout_excerpt", "stderr_excerpt"},
                        documentary=documentary, label_keys=key in _STAGE_MAPS)
            return result
        if isinstance(value, list):
            return [self._present(item, documentary=documentary) for item in value
                    if not (documentary and isinstance(item, str) and _HASH.fullmatch(item))]
        if isinstance(value, tuple):
            return tuple(self._present(item, documentary=documentary) for item in value)
        if isinstance(value, str):
            return self._present_text(value, alias_values=not documentary)
        return deepcopy(value)

    def present_data(self, value):
        """Alias typed business/display data without omitting or parsing fields."""
        return self._present(value, documentary=False)

    def present_response(self, response):
        """Render typed parts at the program boundary; retain legacy policy."""
        from .actor_output import project_metadata
        from ..tool_output import StructuredText, ToolResponse, wire_content
        if isinstance(response, ToolResponse):
            content = [StructuredText(project_metadata(item.value, item.projection))
                       if isinstance(item, StructuredText) else item for item in response.content]
            return dict(success=response.success, contentItems=wire_content(content, present=self.present))
        return self.present(response)

    def present_program_response(self, response):
        """Deliver business data separately from display; never infer it from text."""
        from .actor_output import project_metadata
        from ..tool_output import ToolResponse, validate_display, output_contract_error
        result = self.present_response(response)
        if isinstance(response, ToolResponse):
            try:
                validate_display(response.data)
                data = self.present(project_metadata(response.data, response.data_projection))
                validate_display(data)
            except (TypeError, ValueError, OverflowError):
                # A malformed auxiliary value after execution cannot erase the receipt.
                kept, omitted = {}, []
                if isinstance(response.data, dict):
                    for key, value in response.data.items():
                        if key == 'receipt' and isinstance(value, dict):
                            receipt = {}
                            for name, field in value.items():
                                try:
                                    validate_display({name: field})
                                    receipt[name] = field
                                except (TypeError, ValueError, OverflowError):
                                    omitted.append('/data/receipt/' + str(name))
                            kept[key] = receipt
                            continue
                        try:
                            validate_display({key: value})
                            kept[key] = value
                        except (TypeError, ValueError, OverflowError):
                            omitted.append('/data/' + str(key))
                else:
                    omitted.append('/data')
                response = output_contract_error(response, None, 'program_data_not_json', '/data')
                response.contract_error['omitted_paths'] = omitted
                result['success'] = False
                data = self.present(project_metadata(kept, response.data_projection)) if kept else None
            result.update(kind=response.kind, data=data)
            if response.contract_error is not None:
                result['output_contract_error'] = self.present(response.contract_error)
        else:
            result.update(kind='untyped', data=None)
        return result

    def present_text(self, text):
        """Keep useful text and addressable IDs without checksum metadata."""
        return self._present_text(text, json_text=True)

    def present(self, value):
        """Project model-visible copies; preserve measurements and raw evidence."""
        return self._present(value)

    def restore(self, value):
        """Resolve aliases in arguments, including embedded Python source text.

        This does not restore removed provenance fields. Unknown aliases fail
        before dispatch rather than becoming a guessed or redirected address.
        """
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                restored_key = self.restore(key)
                if key == "artifact_id" and isinstance(item, str) and item.startswith("ref_"):
                    result[restored_key] = self._reference("artifact:" + item).removeprefix("artifact:")
                else:
                    result[restored_key] = self.restore(item)
            return result
        if isinstance(value, list):
            return [self.restore(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.restore(item) for item in value)
        if isinstance(value, str):
            def original(match):
                record = self.store.read_state("actor_id", match.group())
                return record["original"] if record else match.group()
            return _SHORT_ID.sub(original, _ALIAS.sub(lambda match: self._reference(match.group()), value))
        return deepcopy(value)

    def present_artifact(self, info, content):
        """Return public metadata and bytes, projecting only JSON document bodies.

        Text artifacts use the same model projection. Binary artifacts retain exact bytes. Metadata omits original byte counts
        and storage paths because a projected JSON body can have another size.
        The caller must run access-scope/profile validation on the original first.
        """
        metadata = self.present(info)
        for key in ("storage_key", "byte_size"):
            metadata.pop(key, None)
        mime = str(info.get("mime_type", "")).split(";", 1)[0].strip().lower()
        if mime == "application/json" or mime.endswith("+json"):
            content = canonical(self.present(json.loads(content))).encode("utf-8")
        elif mime.startswith("text/") or mime in {"application/xml", "application/x-python", "application/javascript"}:
            content = self.present_text(content.decode("utf-8")).encode("utf-8")
        return metadata, content
