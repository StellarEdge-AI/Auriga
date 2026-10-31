"""Small shared conventions for readable tool replies and explicit detail reads.

These helpers format evidence; they neither interpret task success nor authorize
an action. Existing result objects remain the first JSON text item unchanged.
"""

from copy import deepcopy
from dataclasses import dataclass, field
import math

from .contracts import canonical


DETAIL_SCHEMA = dict(type="string", enum=["summary", "full"])


def resolve_detail(detail=None, *, program=False, default="summary"):
    if detail is None:
        detail = "full" if program else default
    if detail not in ("summary", "full"):
        raise ValueError("invalid_output_detail")
    return detail


@dataclass(frozen=True)
class StructuredText:
    """An owner-selected display value, before actor projection or wire JSON."""
    value: object
    projection: str = "data"


class ToolResponse:
    """Internal business evidence and independent display parts.

    This is intentionally not a mapping. Serialization is explicit at actor,
    program, evidence and trusted-legacy boundaries through wire_response.
    """
    def __init__(self, content=(), *, data=None, kind="data", success=True, data_projection="data"):
        self.content = list(content)
        self.data = data
        self.kind = kind
        self.success = success
        self.data_projection = data_projection
        self.contract_error = None

    def get(self, key, default=None):
        # Existing broker/validation callers query dispatch success only.
        # Display parts are not an implicitly materialized wire mapping.
        return self.success if key == "success" else default


def validate_display(value):
    """Check ordinary JSON representability without producing wire text.

    This validates transport types only; owners/ToolSpec retain every business
    schema. Tuple support and sorted dictionary keys match canonical().
    """
    active = set()
    def visit(part):
        if part is None or isinstance(part, (str, bool, int)):
            return
        if isinstance(part, float):
            if not math.isfinite(part):
                raise ValueError("display_nonfinite_number")
            return
        if not isinstance(part, (dict, list, tuple)):
            raise TypeError("display_not_json")
        identity = id(part)
        if identity in active:
            raise ValueError("display_circular_reference")
        active.add(identity)
        try:
            if isinstance(part, dict):
                sorted(part)  # canonical uses sort_keys=True.
                for key, child in part.items():
                    if key is not None and not isinstance(key, (str, bool, int, float)):
                        raise TypeError("display_key_not_json")
                    visit(key)
                    visit(child)
            else:
                for child in part:
                    visit(child)
        finally:
            active.remove(identity)
    visit(value)


class InvalidContentItem(ValueError):
    def __init__(self, field_path=''):
        self.field_path = field_path
        super().__init__('invalid_native_content_item')


def validate_content_part(item):
    """Check the native Codex text/image/audio union without serializing data."""
    if isinstance(item, StructuredText):
        validate_display(item.value)
        return
    validate_display(item)
    if not isinstance(item, dict):
        raise InvalidContentItem()
    kind = item.get('type')
    if not isinstance(kind, str) or kind not in {'inputText', 'inputImage', 'inputAudio'}:
        raise InvalidContentItem('/type')
    field = {'inputText': 'text', 'inputImage': 'imageUrl', 'inputAudio': 'audioUrl'}[kind]
    if not isinstance(item.get(field), str):
        raise InvalidContentItem('/' + field)


def wire_content(content, *, present=None):
    """Serialize structured parts at an explicit compatibility boundary."""
    return [dict(type="inputText", text=canonical(present(item.value) if present else item.value))
            if isinstance(item, StructuredText) else (present(item) if present else deepcopy(item)) for item in content]


def wire_response(response, *, present=None):
    """Return the existing ordinary, stdlib-JSON-compatible wire envelope."""
    if isinstance(response, ToolResponse):
        return dict(success=response.success, contentItems=wire_content(response.content, present=present))
    if isinstance(response, dict):
        # Trusted legacy/display-only responses have no typed business authority.
        return deepcopy(response)
    raise TypeError("tool_response_requires_typed_or_wire_result")


def output_contract_error(response, tool, reason, field_path):
    """Flag a post-handler defect without replacing already recorded facts.

    Transport failure is not motion failure: the broker still consumes the
    original typed receipt. A wire-only result remains untyped and unknown.
    Never throw into the input-error path after a handler may have acted.
    """
    error = dict(code='invalid_tool_result', tool=tool, reason=reason,
        field_path=field_path, phase='after_handler', handler_returned=True,
        execution_outcome='unchanged_in_original_receipt; unknown_without_typed_evidence',
        suggestion='Read the existing action or job record; do not automatically replay this call.')
    content = response.content if isinstance(response, ToolResponse) else (
        response.get('contentItems', []) if isinstance(response, dict) else [])
    content = content if isinstance(content, list) else []
    safe_content = []
    for item in content:
        try:
            validate_content_part(item)
        except (TypeError, ValueError, OverflowError):
            # The original typed evidence survives even if its display cannot
            # be transported. Never expose repr(payload) or suggest replay.
            continue
        safe_content.append(item)
    if len(safe_content) != len(content) and isinstance(response, ToolResponse) and response.kind == "action":
        receipt = response.data.get("receipt") if isinstance(response.data, dict) else None
        if isinstance(receipt, dict):
            kept = {}
            for key in ("action_id", "status", "reason", "executed_native_steps", "completed_native_steps",
                        "stop_confirmed", "result_observation_id", "execution_uncertain", "action_budget"):
                if key not in receipt:
                    continue
                try:
                    validate_display(receipt[key])
                except (TypeError, ValueError, OverflowError):
                    continue
                kept[key] = receipt[key]
            if kept:
                safe_content.insert(0, StructuredText(dict(receipt=kept), "action_display"))
    result = ToolResponse(safe_content,
        data=response.data if isinstance(response, ToolResponse) else None,
        kind=response.kind if isinstance(response, ToolResponse) else 'untyped', success=False,
        data_projection=response.data_projection if isinstance(response, ToolResponse) else 'data')
    # Preserve malformed but JSON-representable handler output for local
    # archival; this internal link is never part of the model wire envelope.
    result.raw_response = getattr(response, 'raw_response', response)
    result.contract_error = error
    result.content.append(StructuredText(dict(output_contract_error=error)))
    return result


@dataclass(frozen=True)
class ToolOutput:
    summary: object
    details: object = None
    extra_content: tuple = field(default_factory=tuple)
    kind: str = "data"
    projection: str = "data"

    def content_parts(self):
        values = [StructuredText(self.summary, self.projection)]
        if self.details is not None:
            values.append(StructuredText(self.details))
        values.extend(deepcopy(self.extra_content))
        return values

    def content_items(self):
        """Explicit legacy adapter for consumers that require wire items."""
        return wire_content(self.content_parts())

    def response(self):
        return ToolResponse(self.content_parts(), data=self.summary, kind=self.kind, data_projection=self.projection)


def select_fields(value, names):
    """Copy present fields without manufacturing missing values or rounding."""
    return {name: deepcopy(value[name]) for name in names if name in value}


def validate_page(start=None, limit=None, *, default_limit=25, max_limit=100):
    """Validate a zero-based page; booleans and fractional indices are invalid."""
    start = 0 if start is None else start
    limit = default_limit if limit is None else limit
    if type(start) is not int or start < 0:
        raise ValueError("invalid_page_start")
    if type(limit) is not int or not 1 <= limit <= max_limit:
        raise ValueError("invalid_page_limit")
    return start, limit


def bounded_text(value, max_bytes=3072):
    """Keep opaque output with exact ranges; an explicit byte limit keeps both ends.

    max_bytes=None delivers all text, including middle-of-log diagnostics.

    Ranges refer to the original UTF-8 bytes. Replacement characters can appear
    at a cut inside a code point; full original bytes remain in the artifact.
    The inserted marker is display text and is not part of either byte range.
    """
    if not isinstance(value, (str, bytes)):
        raise TypeError("text_excerpt_requires_text_or_bytes")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 2):
        raise ValueError("invalid_excerpt_byte_limit")
    raw = value.encode("utf-8") if isinstance(value, str) else value
    size = len(raw)
    if max_bytes is None or size <= max_bytes:
        return dict(text=raw.decode("utf-8", errors="replace"), byte_size=size,
                    truncated=False, byte_ranges=[[0, size]], omitted_bytes=0)
    head = max_bytes // 2
    tail = size - (max_bytes - head)
    omitted = tail - head
    marker = "\n... [" + str(omitted) + " bytes omitted; read full artifact] ...\n"
    return dict(text=raw[:head].decode("utf-8", errors="replace") + marker
                + raw[tail:].decode("utf-8", errors="replace"),
                byte_size=size, truncated=True, byte_ranges=[[0, head], [tail, size]],
                omitted_bytes=omitted)
