"""Exact image reuse within one Responses request, retaining each original."""
from copy import deepcopy
import json

IMAGE_REUSE_POLICY = "exact_image_part_current_request_v1"


def reuse_exact_images(request):
    """Keep original image bytes, replacing later equal blocks with references.

Only typed image parts in model input message/tool-output arrays are candidates.
Identity covers the complete image part (including detail), not approximate
pixels. The map is local to this request: compaction/resume cannot create a
reference to an image omitted from the current context. No input is mutated.
    """
    result = deepcopy(request)
    seen = {}
    images = replaced = saved_url_chars = displayed = 0
    items = result.get("input")
    if not isinstance(items, list):
        return result, dict(images=0, replaced=0, saved_url_chars=0)
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type", "message") == "message":
            key = "content"
        elif item.get("type") in ("function_call_output", "custom_tool_call_output"):
            key = "output"
        else:
            continue
        parts = item.get(key)
        if not isinstance(parts, list):
            continue
        for index, part in enumerate(parts):
            if not isinstance(part, dict) or part.get("type") != "input_image":
                continue
            url = part.get("image_url")
            if not isinstance(url, str) or not url.startswith("data:image/"):
                displayed += 1
                continue
            images += 1
            identity = json.dumps(part, sort_keys=True, separators=(",", ":"))
            if identity not in seen:
                displayed += 1
                seen[identity] = displayed
                continue
            number = seen[identity]
            parts[index] = dict(type="input_text", text=(
                f"[Exact duplicate image: identical pixels to image {number} "
                "earlier in this context, counting actual images only. "
                "Use that image for this observation; no visual change.]"))
            replaced += 1
            saved_url_chars += len(url)
    return result, dict(images=images, replaced=replaced, saved_url_chars=saved_url_chars)
