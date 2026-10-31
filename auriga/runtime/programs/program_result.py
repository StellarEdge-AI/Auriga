"""Model-facing program output without treating arbitrary stdout as evidence.

ProgramRunner and its stored job, source, logs and tool replies remain intact.
Only explicitly identified fixed skills have a structured stdout interpretation;
ordinary Python output remains opaque, including strings that happen to be JSON.
"""

from copy import deepcopy
import re

from ...core.presentation.actor_output import project_metadata
from ...core.tool_output import bounded_text, resolve_detail


_IMAGE_DATA_URL = re.compile(rb'''(?<![a-z0-9_])data:(image/[a-z0-9.+-]+);base64,([^\s"'<>()\[\]{},;]+)''', re.IGNORECASE)
_BASE64_PAYLOAD = re.compile(rb"[A-Za-z0-9+/]+={0,2}")


def _output_bytes(store, job, name):
    reference = job.get(name + "_ref")
    if reference:
        info, raw = store.resolve_artifact(reference, allowed_scopes=("actor",))
        if info.get("kind") != "program_" + name:
            raise ValueError("program_output_artifact_kind_mismatch")
        return raw, True
    text = job.get(name + "_text")
    if not isinstance(text, str):
        return None, False
    return text.encode("utf-8"), not bool(job.get(name + "_truncated"))


def _log_excerpt(raw, max_bytes, *, source_complete):
    """Omit explicit standard image data URLs without interpreting log fields.

    With media omission, byte_ranges/byte_size/omitted_bytes describe the UTF-8
    presentation before bounded_text inserts its marker. Removed-media ranges
    and original_byte_size describe the original source bytes instead. Keep an
    incomplete source untouched: a terminal data URL may itself be cut short.
    """
    matches = [match for match in _IMAGE_DATA_URL.finditer(raw)
               if len(match[2]) % 4 == 0 and _BASE64_PAYLOAD.fullmatch(match[2])] if source_complete else []
    if not matches:
        return bounded_text(raw, max_bytes=max_bytes)
    pieces, start = [], 0
    for match in matches:
        placeholder = (b"[inline " + match[1] + b" data URL omitted; "
                       + str(match.end() - match.start()).encode("ascii")
                       + b" original bytes; read full artifact]")
        pieces.extend((raw[start:match.start()], placeholder))
        start = match.end()
    pieces.append(raw[start:])
    presentation = b"".join(pieces)
    excerpt = bounded_text(presentation, max_bytes=max_bytes)
    excerpt.update(presentation="inline_image_data_urls_omitted", byte_ranges_source="presentation_utf8",
        original_byte_size=len(raw), removed_media_count=len(matches),
        removed_media_bytes=sum(match.end() - match.start() for match in matches),
        removed_media_byte_ranges=[[match.start(), match.end()] for match in matches],
        removed_media_byte_ranges_source="original_log_bytes", truncated=True)
    return excerpt


def program_result(store, job, *, detail="summary", max_output_bytes=None):
    """Return a compact job or full original job with complete log bytes decoded.

    A caller may set skill only from the selected built-in skill_run parameters,
    never by recognizing strings in user-provided program source or stdout.
    """
    detail = resolve_detail(detail)
    result = deepcopy(job)
    summary = result.get('action_summary')
    if isinstance(summary, dict):
        if isinstance(summary.get('recent_actions'), list):
            summary['recent_actions'] = project_metadata(summary['recent_actions'], 'program_action_evidence')
        if (summary.get('status_counts') or {}).get('failed', 0) > 0:
            result['physical_action_failures_detected'] = True
    review = result.get('pending_review')
    if isinstance(review, dict) and isinstance(review.get('preview'), dict):
        review['preview'] = project_metadata(review['preview'], 'action_plan')
    outputs = {name: _output_bytes(store, job, name) for name in ("stdout", "stderr")}
    if detail == "full":
        for name, (raw, complete) in outputs.items():
            if raw is not None:
                result[name + "_text"] = raw.decode("utf-8", errors="replace")
                result[name + "_truncated"] = not complete
        return result
    for key in ("source_sha256", "input_digest", "correlation", "review_decisions"):
        result.pop(key, None)
    for name, (raw, complete) in outputs.items():
        if raw is None:
            continue
        excerpt = _log_excerpt(raw, max_output_bytes, source_complete=complete)
        result[name + "_text"] = excerpt.pop("text")
        result[name + "_truncated"] = excerpt["truncated"] or not complete
        excerpt["source_complete"] = complete
        result[name + "_excerpt"] = excerpt
    if isinstance(job.get("job_id"), str):
        result["full_result"] = dict(tool="program_status", job_id=job["job_id"], detail="full")
    review = result.get('pending_review')
    if isinstance(review, dict):
        result['resume'] = dict(tool='program_run', operation='resume', job_id=job['job_id'],
            review_id=review['review_id'], decision='approve')
    result["result_semantics"] = ("Program status describes process execution only; stdout/stderr are program-authored text, "
        "not verified robot or task outcomes. action_summary and final_observation come from broker tool replies. "
        "awaiting_review retains the paused program; approve or reject with program_run resume, without executing the plan separately.")
    return result
