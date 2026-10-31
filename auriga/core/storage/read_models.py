"""Versioned, paginated projections shared by live gateways and archives."""
import asyncio

from aiohttp import web


def record_updates(store, query):
    if query.get("mode") == "history":
        return store.review_page(int(query["before"]) if query.get("before") else None,
                                 int(query.get("limit", 200)))
    limit = int(query.get("limit", 500))
    result = {"schema_version": "1", "event_cursor": store.event_cursor()}
    for name, kind, mutable in (("observations", "observation", False),
                                ("items", "codex_item", True), ("requests", "native_request", True)):
        page = (store.state_page if mutable else store.record_page)(kind, int(query.get(name, 0)), limit)
        if name == "observations":
            for entry in page["entries"]:
                entry["value"] = store.observation_view(entry["value"])
        result[name] = page
    return result


async def artifact_response(store, reference, *, image=False, allowed_scopes=("actor", "observer", "internal")):
    """File-backed media keep HEAD/Range streaming; only reconstructed RGB has a body."""
    try:
        info, path = await asyncio.to_thread(store.artifact_file, reference, allowed_scopes)
    except FileNotFoundError:
        if not image:
            raise
        info, content = await asyncio.to_thread(store.resolve_image, reference, allowed_scopes)
        return web.Response(body=content, headers={"Content-Type": info["mime_type"],
            "X-Artifact-SHA256": info["sha256"], "X-Image-Reconstructed": "video",
            "X-Source-SHA256": info["reconstruction"]["source_sha256"]})
    return web.FileResponse(path, headers={"Content-Type": info["mime_type"], "X-Artifact-SHA256": info["sha256"]})
