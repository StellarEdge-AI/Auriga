"""Loopback experiment library, verified live proxy, and passive evidence replay."""
import argparse
import asyncio
import base64
from contextlib import contextmanager
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import signal
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web
from aiohttp.web_response import ContentCoding

from ...core.contracts import IDENTITY, canonical, digest, utc_now
from .experiments import (Catalog, TERMINAL_STATUSES, contained, endpoint_origin,
                          evidence_path, excluded_path, heartbeat_fresh, read_json, atomic_json)
from .experiment_comparisons import ExperimentGroups
from ...core.storage.store import open_read_store, file_digest
from .task_review import TaskReviewService
from ...cortex.session.native_terminal_replay import NativeTerminalReplay
from ...cortex.session.terminal import terminal_bootstrap_status
from ...core.storage.read_models import artifact_response, record_updates
from .static_assets import static_directory

MEDIA_SUFFIXES = {".mp4", ".webm", ".png", ".jpg", ".jpeg", ".gif", ".avif", ".pdf"}
STATIC = static_directory()
PROXY_PATH = re.compile(r"(?:run|records|task-review|events|terminal|terminal-log|control|observations(?:/[A-Za-z0-9_.-]+)?|items(?:/[A-Za-z0-9_.-]+)?|requests|artifacts/[A-Za-z0-9_.-]+|actions/[A-Za-z0-9_.-]+|scene/(?:head|versions(?:/[0-9]+)?|query|snapshot|alignment|update)|jobs/[A-Za-z0-9_.-]+(?:/cancel)?|observer/(?:snapshot|evaluate|video|record)|runs/[A-Za-z0-9_.-]+(?:/events|/control)?)")
SCORE_FIELDS = ("normalized_score", "score", "score_status", "scored_episode", "valid_episode",
                "primary_outcome", "infrastructure_error", "task_success")


def json_response(request, value):
    # The observation clock is not a change to the published business data.
    data = {key: item for key, item in value.items() if key != "observed_at"}
    revision = hashlib.sha256((request.path_qs + "\n" + json.dumps(data, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()
    headers = {"ETag": f'W/"{revision}"', "Cache-Control": "private, no-cache", "Vary": "Accept-Encoding"}
    if (request.headers.get("If-None-Match", "").strip() == "*"
            or any(tag.value == revision for tag in request.if_none_match or ())):
        return web.Response(status=304, headers=headers)
    response = web.json_response(value, headers=headers)
    qualities = {}
    for item in ",".join(request.headers.getall("Accept-Encoding", [])).lower().split(","):
        coding, *parameters = item.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            name, _, amount = parameter.strip().partition("=")
            if name == "q":
                try: quality = float(amount)
                except ValueError: quality = 0.0
        qualities[coding] = quality if 0 <= quality <= 1 else 0.0
    coding = max((ContentCoding.gzip, ContentCoding.deflate, ContentCoding.identity),
                 key=lambda choice: qualities.get(choice.value, 0 if choice is ContentCoding.identity else qualities.get("*", 0)))
    quality = qualities.get(coding.value, qualities.get("*", 0))
    if coding is not ContentCoding.identity and quality > 0:
        response.enable_compression(force=coding)
    elif qualities.get("identity", 0 if qualities.get("*") == 0 else 1) == 0:
        raise web.HTTPNotAcceptable(text="json_encoding_unavailable")
    return response


def group_summary(group):
    return {**{key: value for key, value in group.items() if key != "execution_groups"}, "details_loaded": False, "cases": [
        {"case_key": case["case_key"], "run": {"summary": {
            key: case["run"]["summary"][key] for key in SCORE_FIELDS if key in case["run"]["summary"]
        }} if case["run"] else None} for case in group["cases"]]}


def https_origin(value):
    if not isinstance(value, str) or not value.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("public_origin_requires_https_authority_without_path")
    authority = urlsplit(value)
    if (authority.scheme != "https" or value != "https://" + authority.netloc
            or not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:]+\])(?::[0-9]+)?", authority.netloc)):
        raise ValueError("public_origin_requires_https_authority_without_path")
    if authority.port is not None and not 1 <= authority.port <= 65535:
        raise ValueError("invalid_public_origin_port")
    return value


class Library:
    def __init__(self, root=None, *, scan_interval=None, public_origin=None, task_source_root=None, config=None):
        from ...core.config import Config
        self.config = config if config is not None else Config.load()
        root = self.config.library.root if root is None else root
        scan_interval = self.config.library.scan_interval if scan_interval is None else scan_interval
        public_origin = self.config.library.public_origin if public_origin is None else public_origin
        task_source_root = self.config.library.task_source_root if task_source_root is None else task_source_root
        self.public_origin = https_origin(public_origin) if public_origin is not None else None
        self.catalog = Catalog(root)
        self.experiment_groups = ExperimentGroups(self.catalog.root)
        self.group_snapshot = dict(groups=[], errors=[], ungrouped_count=0)
        self.scan_interval = scan_interval
        self.session = None
        self.scan_task = None
        self.refresh_lock = asyncio.Lock()
        self.changed = asyncio.Condition()
        self.revision = ""
        self.refreshed_at = None
        self.refresh_error = None
        self.task_reviews = TaskReviewService(task_source_root, config=self.config)
        self.native_terminals = NativeTerminalReplay(self.catalog.root / "service" / "private" / "native-terminal-cache")
        self.trajectory_locks = {}

    async def start(self, app):
        self.session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=1, sock_read=None))
        await self.refresh()
        self.scan_task = asyncio.create_task(self.watch())

    async def close(self, app):
        if self.scan_task:
            self.scan_task.cancel()
            await asyncio.gather(self.scan_task, return_exceptions=True)
        if self.session:
            await self.session.close()

    async def watch(self):
        while True:
            await asyncio.sleep(self.scan_interval)
            try:
                await self.refresh()
            except Exception as error:
                # Keep the last published snapshot and retry; cancellation still propagates.
                message = f"{type(error).__name__}: {error}"
                if message != self.refresh_error:
                    self.refresh_error = message
                    self.revision = digest(dict(content_revision=self.revision, refresh_error=message))
                    async with self.changed: self.changed.notify_all()

    async def verify_live(self, entry):
        item, runtime = entry["item"], entry["runtime"]
        entry["endpoint"] = None
        active = heartbeat_fresh(runtime) and runtime.get("status") not in TERMINAL_STATUSES and not entry["metadata"].get("imported_from")
        item.update(live=False, writable=False, read_only=True, view_mode="replay")
        if not active:
            if runtime.get("status") not in TERMINAL_STATUSES and runtime.get("status") is not None:
                item.update(status="disconnected", connection_status="heartbeat_stale")
            return
        if runtime.get("source") == "recorded_stream" and not runtime.get("endpoint"):
            item.update(live=True, view_mode="live_recording")
            return
        item.update(status="disconnected", connection_status="backend_unverified")
        try:
            endpoint = endpoint_origin(runtime.get("endpoint"))
            if not endpoint: return
            async with self.session.get(endpoint + "/v1/run", timeout=ClientTimeout(total=1), allow_redirects=False) as response:
                value = await response.json()
                if response.status != 200 or not isinstance(value, dict) or value.get("run_id") != item["run_id"]: return
            entry["endpoint"] = endpoint
            item.update(status=runtime.get("status", "running"), connection_status="verified", live=True, writable=not value.get("read_only", False) and value.get("status") != "replay", view_mode="live")
            item["read_only"] = not item["writable"]
            item["terminal_available"] = value.get("terminal") is not None
        except (ClientError, asyncio.TimeoutError, ValueError, TypeError): return

    async def refresh(self):
        async with self.refresh_lock:
            # Publish only complete scans: readers never observe half-verified entries.
            catalog = Catalog(self.catalog.root)
            await asyncio.to_thread(catalog.scan)
            await asyncio.gather(*(self.verify_live(entry) for entry in catalog.entries.values()))
            groups = await asyncio.to_thread(self.experiment_groups.snapshot, catalog)
            groups["groups"] = [{**group, "revision": digest(group), "details_loaded": True} for group in groups["groups"]]
            revision = digest({"catalog": catalog.revision, "items": [entry["item"] for entry in catalog.entries.values()], "errors": catalog.errors, "groups": groups})
            self.catalog = catalog
            self.group_snapshot = groups
            self.refreshed_at, self.refresh_error = utc_now(), None
            if revision != self.revision:
                self.revision = revision
                async with self.changed: self.changed.notify_all()

    @contextmanager
    def store(self, entry, scene=False):
        if entry["path"].resolve() != entry["path"]:
            raise ValueError("symlink_run_directory_denied")
        evidence = evidence_path(entry)
        root = evidence / "twin-service" if scene else evidence
        if not scene:
            inventory = read_json(entry["path"] / "import-inventory.json", {})
            if inventory.get("replay_database"):
                root = contained(entry["path"], inventory["replay_database"]).parent
        if not (root / "events.sqlite").is_file(): raise web.HTTPNotFound(text="protocol_replay_unavailable")
        with open_read_store(root, live=entry["item"]["live"]) as store:
            if not scene and root != evidence: store.blobs = evidence / "blobs"
            yield store

    def identity(self, entry, store=None):
        manifest = read_json(evidence_path(entry) / "manifest.json", {})
        identity = {key: manifest[key] for key in IDENTITY if key in manifest}
        if store:
            identity.update(store.read_state("runtime", "identity", {}))
            if len(identity) != len(IDENTITY):
                events = store.events(limit=1)
                if events: identity.update({key: events[0][key] for key in IDENTITY if key in events[0]})
        identity.setdefault("run_id", entry["metadata"].get("evidence_run_id", entry["item"]["run_id"]))
        return identity

    def assets(self, entry):
        evidence = evidence_path(entry)
        assets = {}
        for path in evidence.rglob("*"):
            relative = path.relative_to(evidence).as_posix()
            if path.is_symlink() or excluded_path(relative) or not path.is_file(): continue
            report = "/" not in relative and path.suffix == ".json" and relative not in {"experiment.json", "runtime.json", "manifest.json", "terminal.json", "import-inventory.json"}
            if path.suffix.lower() in MEDIA_SUFFIXES or report:
                contained(evidence, relative)
                identifier = hashlib.sha256(relative.encode()).hexdigest()[:32]
                assets[identifier] = dict(path=path, name=relative, url=f'/v1/experiments/{entry["item"]["id"]}/files/{identifier}',
                                          mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        return assets

    def detail(self, entry):
        reports, media = [], []
        for value in self.assets(entry).values():
            item = {key: value[key] for key in ("name", "url", "mime_type")}
            if value["mime_type"] == "application/json":
                if value["path"].stat().st_size <= 2 * 1024 * 1024: item["content"] = read_json(value["path"], object_only=False)
                else: item.update(content=None, download_required=True)
                reports.append(item)
            else: media.append(item)
        return {**entry["item"], "metadata": entry["metadata"], "runtime": entry["runtime"],
                "manifest": read_json(evidence_path(entry) / "manifest.json", {}), "reports": reports, "media": media,
                "task_review": self.task_reviews.get(entry),
                "trajectory_download": self.trajectory_download(entry),
                "evidence_url": f'/v1/experiments/{entry["item"]["id"]}'}

    def media(self, entry):
        reports, media = [], []
        for value in self.assets(entry).values():
            if value["mime_type"].startswith("video/"):
                media.append({key: value[key] for key in ("name", "url", "mime_type")})
            elif value["mime_type"] == "application/json" and value["path"].stat().st_size <= 2 * 1024 * 1024:
                report = read_json(value["path"], {}, object_only=False)
                videos = report.get("videos") if isinstance(report, dict) else None
                if isinstance(videos, list):
                    reports.append({"content": {"videos": [
                        {**({"camera_id": video["camera_id"]} if isinstance(video.get("camera_id"), str) else {}),
                         **({"video_path": Path(video["video_path"]).name} if isinstance(video.get("video_path"), str) else {}),
                         **({"url": video["url"]} if isinstance(video.get("url"), str) else {})}
                        for video in videos if isinstance(video, dict)]}})
        return {**{key: entry["item"][key] for key in ("id", "view_url", "replay_available")},
                "media": media, "reports": reports}

    def trajectory_download(self, entry):
        source, item = evidence_path(entry), entry["item"]
        reason = None
        if entry["metadata"].get("imported_from") or (entry["path"] / "import-inventory.json").exists():
            reason = "暂不支持已导入的历史归档，请使用原始运行目录导出"
        elif item.get("live") or item.get("status") not in {"completed", "succeeded", "failed", "cancelled", "stopped", "expired"}:
            reason = "运行尚未确认结束，暂不提供轨迹下载"
        elif any(not (source / name).is_file() or (source / name).is_symlink()
                 for name in ("events.sqlite", "evaluation.json", "effective_config.json")):
            reason = "缺少完整的原生轨迹归档"
        elif not (source / "blobs").is_dir() or (source / "blobs").is_symlink() or (source / "events.sqlite-wal").is_symlink():
            reason = "轨迹归档目录不完整或包含不支持的符号链接"
        return dict(status="unavailable" if reason else "available", reason=reason,
                    url=None if reason else f'/v1/experiments/{item["id"]}/trajectory-bundle')

    def trajectory_bundle(self, entry):
        from ..replay.trajectory_bundle import export_bundle
        source = evidence_path(entry)

        def fingerprint():
            paths = [source / name for name in ("events.sqlite", "events.sqlite-wal", "evaluation.json",
                     "effective_config.json", "manifest.json", "runtime.json", "summary.json")]
            paths.extend(sorted((source / "blobs").iterdir()))
            return digest([str(source), [(str(path.relative_to(source)), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                           for path in paths if path.exists() for stat in [path.lstat()]]])

        key = fingerprint()
        cache = contained(self.catalog.root, "service/trajectory-bundles")
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / (entry["item"]["id"] + "-" + key + ".zip")
        metadata_path = path.with_suffix(".json")
        metadata = read_json(metadata_path, {})
        if path.is_symlink():
            raise ValueError("trajectory_cache_symlink_denied")
        if path.is_file() and metadata.get("sha256") == file_digest(path):
            if fingerprint() != key:
                raise ValueError("trajectory_source_changed_during_download")
            return path, metadata["sha256"]
        path.unlink(missing_ok=True)
        metadata = export_bundle(source, path)
        if fingerprint() != key:
            path.unlink()
            raise ValueError("trajectory_source_changed_during_export")
        atomic_json(metadata_path, metadata)
        return path, metadata["sha256"]

    def snapshot(self, entry):
        item, runtime = entry["item"], entry["runtime"]
        library = {key: item[key] for key in ("id", "live", "read_only", "writable", "view_mode", "replay_available", "terminal_available")}
        value = dict(run_id=entry["metadata"].get("evidence_run_id", item["run_id"]), status=runtime.get("status") if item["live"] else "replay",
                     codex_status=None, thread_id=None, turn_id=None, action_count=0, accepting_actions=False, active_action=runtime.get("active_action"),
                     motion_status="unknown", pending_requests=[], controller=dict(owner_client_id=None, expires_at=None, can_claim=False),
                     capabilities=runtime.get("capabilities", {}), terminal=None, library=library,
                     **{key: item[key] for key in ("read_only", "writable", "view_mode", "replay_available", "terminal_available")})
        if not item["replay_available"]: return value
        with self.store(entry) as store:
            value.update(self.identity(entry, store))
            codex = store.read_state("runtime", "codex", {})
            value.update(thread_id=codex.get("thread_id"), codex_status="replay" if not item["live"] else None,
                         capabilities=store.read_state("runtime", "capabilities", value["capabilities"]),
                         action_count=len(store.records("action_request")), pending_requests=store.states("native_request"))
            requests = store.records("action_request")
            if requests:
                try: value["motion_status"] = store.read("action_receipt", requests[-1]["action_id"]).get("status", "unknown")
                except KeyError: pass
        terminal = self.terminal_archive(entry, value.get("thread_id"))
        if terminal:
            value["terminal"] = terminal["status"]
            value["terminal_available"] = value["library"]["terminal_available"] = True
        return value

    def terminal_archive(self, entry, thread_id=None):
        """Prefer original PTY bytes; recover failed/missing archives with native CLI."""
        evidence = evidence_path(entry)
        if (evidence / "terminal.bin").is_symlink(): raise ValueError("terminal_symlink_denied")
        path = contained(evidence, "terminal.bin")
        status = read_json(evidence / "terminal.json", {})
        thread_id = thread_id or status.get("thread_id")
        if not thread_id and entry["item"].get("replay_available"):
            with self.store(entry) as store:
                thread_id = store.read_state("runtime", "codex", {}).get("thread_id")
        original_exists = path.is_file()
        if original_exists and "bootstrap_failed" not in status:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                status.update(terminal_bootstrap_status(stream.read()))
        can_restore = (not entry["item"]["live"] and (not original_exists or status.get("bootstrap_failed"))
                       and self.native_terminals.available(evidence, thread_id))
        if can_restore:
            recovered = self.native_terminals.peek(evidence, thread_id)
            if recovered: return recovered
            return dict(path=None, restore=(evidence, thread_id), status=dict(
                session_id="recovering-" + entry["item"]["id"], cursor=0, mode="read_only", pid=None,
                exit_code=None, bootstrap_failed=False,
                replay=dict(kind="native_resume_pending", original_bootstrap_failed=bool(status.get("bootstrap_failed")))))
        if not original_exists: return None
        status.update(cursor=path.stat().st_size, mode="read_only", pid=None)
        status.setdefault("replay", dict(kind="original_pty", original_recording=True))
        status.setdefault("session_id", "recorded-" + entry["item"]["id"])
        return dict(path=path, status=status)

    async def proxy(self, request, entry, tail):
        endpoint = entry["endpoint"]
        target = endpoint + "/v1/" + tail
        if request.query_string: target += "?" + request.query_string
        headers = {key: request.headers[key] for key in ("Content-Type", "Range", "Last-Event-ID", "Idempotency-Key") if key in request.headers}
        headers["X-Auriga-Run-ID"] = entry["item"]["run_id"]
        if request.headers.get("Origin"): headers["Origin"] = endpoint
        if request.headers.get("Upgrade", "").lower() == "websocket": return await self.proxy_terminal(request, target, headers)
        body = await request.read() if request.can_read_body else None
        try:
            upstream = await self.session.request(request.method, target, headers=headers, data=body, allow_redirects=False)
        except (ClientError, asyncio.TimeoutError):
            entry["endpoint"] = None
            entry["item"].update(live=False, writable=False, read_only=True, view_mode="replay")
            if request.method not in {"GET", "HEAD"}: raise web.HTTPServiceUnavailable(text="live_backend_unavailable_do_not_retry_writes")
            return await self.replay(request, entry, tail)
        async with upstream:
            if tail == "run" or tail.startswith("runs/") and tail.count("/") == 1:
                value = await upstream.json()
                if value.get("run_id") != entry["item"]["run_id"]: raise web.HTTPConflict(text="live_run_identity_changed")
                value.update(library={key: entry["item"][key] for key in ("id", "live", "read_only", "writable", "terminal_available", "view_mode")},
                             read_only=entry["item"]["read_only"], writable=entry["item"]["writable"], view_mode="live")
                return web.json_response(value, status=upstream.status)
            response_headers = {key: upstream.headers[key] for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "X-Artifact-SHA256", "X-Image-Reconstructed", "X-Source-SHA256", "Cache-Control") if key in upstream.headers}
            response = web.StreamResponse(status=upstream.status, headers=response_headers)
            await response.prepare(request)
            try:
                async for data in upstream.content.iter_chunked(65536): await response.write(data)
            except (ClientError, ConnectionResetError, asyncio.CancelledError): pass
            return response

    async def proxy_terminal(self, request, target, headers):
        try: upstream = await self.session.ws_connect(target, headers=headers, max_msg_size=1024 * 1024)
        except (ClientError, asyncio.TimeoutError): raise web.HTTPServiceUnavailable(text="live_terminal_unavailable")
        ws = web.WebSocketResponse(max_msg_size=4096)
        await ws.prepare(request)
        async def forward(source, destination, browser=False):
            async for message in source:
                if message.type == WSMsgType.TEXT:
                    if browser:
                        try:
                            value = json.loads(message.data)
                            if value.get("type") != "resize" or type(value.get("cols")) is not int or type(value.get("rows")) is not int or not 20 <= value["cols"] <= 500 or not 5 <= value["rows"] <= 200:
                                raise ValueError("terminal_input_is_read_only")
                        except (ValueError, TypeError):
                            await ws.send_json(dict(type="error", message="terminal_input_is_read_only"))
                            continue
                    await destination.send_str(message.data)
                elif message.type == WSMsgType.BINARY and not browser: await destination.send_bytes(message.data)
        tasks = [asyncio.create_task(forward(upstream, ws)), asyncio.create_task(forward(ws, upstream, True))]
        try: await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream.close()
            await ws.close()
        return ws

    async def replay_events(self, request, entry):
        after = max(int(request.query.get("after", "0")), int(request.headers.get("Last-Event-ID", "0")))
        if after < 0: raise ValueError("invalid_event_cursor")
        with self.store(entry) as store: cursor = store.event_cursor()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await response.prepare(request)
        if after > cursor:
            after = cursor
            await response.write(f'event: gap\ndata: {canonical(dict(reason="cursor_ahead_or_run_reset", current_seq=cursor))}\n\n'.encode())
        try:
            while True:
                with self.store(entry) as store: batch = store.events(after)
                for event in batch:
                    await response.write(f'id: {event["server_seq"]}\ndata: {canonical(event)}\n\n'.encode())
                    after = event["server_seq"]
                if not batch:
                    await response.write(b": recorded\n\n")
                    await asyncio.sleep(.2)
        except (ConnectionResetError, asyncio.CancelledError): pass
        return response

    async def replay_terminal(self, request, entry):
        archive = self.terminal_archive(entry)
        if archive is None: raise web.HTTPNotFound(text="native_terminal_not_recorded")
        after = int(request.query.get("after", "0"))
        ws = web.WebSocketResponse(max_msg_size=4096)
        await ws.prepare(request)
        if archive.get("restore"):
            await ws.send_json(dict(type="restoring", message="原生 Codex 正在从保存的会话归档恢复显示"))
            try:
                archive = await asyncio.to_thread(self.native_terminals.get, *archive["restore"])
            except Exception:
                # Native failures remain failures; never replace terminal bytes
                # with invented rendering or send private process diagnostics.
                await ws.send_json(dict(type="error", terminal=True, message="原生 TUI 恢复失败，原始日志仍然保留；可点击重新读取以重试"))
                await ws.close()
                return ws
        path, status = archive["path"], archive["status"]
        await ws.send_json(dict(type="ready", status=status))
        if after < 0 or after > status["cursor"] or request.query.get("session_id", status["session_id"]) != status["session_id"]:
            await ws.send_json(dict(type="gap", reason="terminal_cursor_outside_retained_stream", current_cursor=status["cursor"]))
            after = 0
        with path.open("rb") as stream:
            stream.seek(after)
            while data := stream.read(65536):
                cursor = after + len(data)
                await ws.send_json(dict(type="output", cursor=cursor, after=after, data=base64.b64encode(data).decode()))
                after = cursor
        await ws.send_json(dict(type="status", status=status))
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                try: value = json.loads(message.data)
                except ValueError: value = {}
                if value.get("type") != "resize": await ws.send_json(dict(type="error", message="replay_is_read_only"))
        return ws

    def scene_value(self, entry, tail):
        with self.store(entry) as store: identity = self.identity(entry, store)
        if any(key not in identity for key in IDENTITY): raise web.HTTPNotFound(text="scene_identity_not_recorded")
        namespace = digest({key: identity[key] for key in IDENTITY})
        evidence = evidence_path(entry)
        roots = (evidence / "twin-service" / "scene", evidence / "scene")
        root = next((root for root in roots if (root / namespace).is_dir()), roots[0])
        base = contained(evidence, root.relative_to(evidence) / namespace)
        head = read_json(base / "head.json", {**identity, "scene_version": 0, "world_kind": "internal"})
        if tail == "scene/head": return head
        if tail == "scene/versions":
            versions = []
            for version in range(1, head["scene_version"] + 1):
                manifest = read_json(base / "versions" / f"{version:012}.json")
                if manifest is None: raise KeyError("scene_version")
                versions.append(dict(scene_version=version, digest=digest(manifest)))
            return dict(versions=versions)
        version = int(tail.split("/")[-1])
        if version < 1: raise ValueError("invalid_scene_version")
        value = read_json(base / "versions" / f"{version:012}.json")
        if value is None: raise KeyError("scene_version")
        return value

    async def replay(self, request, entry, tail):
        if request.method not in {"GET", "HEAD"}: raise web.HTTPForbidden(text="replay_is_read_only")
        if tail.startswith("runs/"):
            pieces = tail.split("/")
            identity = entry["metadata"].get("evidence_run_id", entry["item"]["run_id"])
            if pieces[1] != identity: raise web.HTTPNotFound(text="run_identity_mismatch")
            tail = "/".join(pieces[2:]) or "run"
        if tail == "run": return web.json_response(self.snapshot(entry))
        if tail == "events": return await self.replay_events(request, entry)
        if tail == "terminal": return await self.replay_terminal(request, entry)
        if tail == "terminal-log":
            evidence = evidence_path(entry)
            if (evidence / "terminal.bin").is_symlink(): raise ValueError("terminal_symlink_denied")
            path = contained(evidence, "terminal.bin")
            if not path.is_file(): raise web.HTTPNotFound(text="native_terminal_not_recorded")
            return web.FileResponse(path, headers={"Content-Type": "application/octet-stream", "Content-Disposition": 'attachment; filename="original-terminal.bin"'})
        if tail.startswith("scene/"): return web.json_response(self.scene_value(entry, tail))
        if tail.startswith("artifacts/"):
            reference = "artifact:" + tail.split("/")[1]
            try:
                with self.store(entry) as store:
                    return await artifact_response(store, reference, image=request.query.get("view") == "image")
            except KeyError:
                with self.store(entry, scene=True) as store:
                    return await artifact_response(store, reference, allowed_scopes=("actor", "internal"))
        with self.store(entry) as store:
            if tail == "records": value = record_updates(store, request.query)
            elif tail == "observations": value = dict(observations=[store.observation_view(o) for o in store.records("observation")])
            elif tail.startswith("observations/"):
                identifier = tail.split("/")[1]
                observations = store.records("observation") if identifier == "latest" else []
                value = observations[-1] if identifier == "latest" and observations else None if identifier == "latest" else store.read("observation", identifier)
                value = store.observation_view(value)
            elif tail == "items": value = dict(items=store.states("codex_item"), cursor=store.event_cursor())
            elif tail.startswith("items/"):
                value = store.read_state("codex_item", tail.split("/")[1])
                if value is None: raise KeyError("item")
            elif tail == "requests": value = dict(requests=store.states("native_request"))
            elif tail.startswith("actions/"):
                identifier = tail.split("/")[1]
                request_value = store.read("action_request", identifier)
                try: receipt = store.read("action_receipt", identifier)
                except KeyError: receipt = dict(status="unknown", reason="terminal_receipt_not_recorded")
                value = dict(request=request_value, receipt=receipt)
            elif tail.startswith("jobs/program-"):
                value = store.read_state("program", tail.split("/")[1])
                if value is None: raise KeyError("job")
            elif tail.startswith("jobs/"):
                from ...perception.scene.scene_service import read_scene_job
                with self.store(entry, scene=True) as scene_store:
                    try:
                        value = read_scene_job(scene_store, tail.split("/")[1])
                    except KeyError:
                        raise KeyError("job") from None
                    identity = self.identity(entry, store)
                    if any(value.get(key) != identity.get(key) for key in IDENTITY): raise ValueError("job_identity_conflict")
            else: raise web.HTTPNotFound(text="operation_not_recorded")
        return web.json_response(value)

    def app(self):
        @web.middleware
        async def guard(request, handler):
            try:
                authority = urlsplit("http://" + request.host)
                if authority.hostname not in {"localhost", "127.0.0.1", "::1"} or authority.username or authority.password:
                    raise web.HTTPForbidden(text="loopback_host_required")
                if request.headers.get("Origin") not in {None, "http://" + request.host, self.public_origin}: raise web.HTTPForbidden(text="same_origin_required")
                return await handler(request)
            except FileNotFoundError: return web.json_response(dict(error="artifact_original_unavailable"), status=404)
            except KeyError as error: return web.json_response(dict(error=f"missing:{error}"), status=404)
            except (ValueError, TypeError) as error: return web.json_response(dict(error=str(error)), status=409 if "conflict" in str(error) else 400)

        app = web.Application(middlewares=[guard], client_max_size=1024 * 1024)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.close)
        async def listing(request):
            value = self.catalog.list(request.query)
            if self.refresh_error:
                value["errors"] = [*value["errors"], dict(error="catalog_refresh_failed", message=self.refresh_error)]
            return json_response(request, {**value, "revision": self.revision, "observed_at": self.refreshed_at})
        async def detail(request):
            return json_response(request, await asyncio.to_thread(self.detail, self.catalog.get(request.match_info["id"])))
        async def media(request):
            return json_response(request, await asyncio.to_thread(self.media, self.catalog.get(request.match_info["id"])))
        async def groups(request):
            view = request.query.get("view", "full")
            if view not in {"full", "summary"}: raise ValueError("invalid_group_view")
            value = self.group_snapshot
            if view == "summary":
                value = {**value, "groups": [group_summary(group) for group in value["groups"]]}
            if self.refresh_error:
                value = {**value, "errors": [*value["errors"], dict(error="catalog_refresh_failed")]}
            return json_response(request, {**value, "revision": self.revision, "observed_at": self.refreshed_at})
        async def group_detail(request):
            identifier = request.match_info["id"]
            group = next((group for group in self.group_snapshot["groups"]
                          if group["id"] == identifier or identifier in group.get("id_aliases", [])), None)
            if group is None: raise web.HTTPNotFound(text="experiment_group_not_found")
            return json_response(request, group)
        async def events(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
            await response.prepare(request)
            revision = None
            try:
                while True:
                    if revision != self.revision:
                        event = "snapshot" if revision is None else "changed"
                        revision = self.revision
                        data = canonical(dict(revision=revision, total=len(self.catalog.entries)))
                        await response.write(f'event: {event}\ndata: {data}\n\n'.encode())
                    async with self.changed:
                        try: await asyncio.wait_for(self.changed.wait(), timeout=5)
                        except asyncio.TimeoutError: await response.write(b": library\n\n")
            except (ConnectionResetError, asyncio.CancelledError): pass
            return response
        async def files(request):
            entry = self.catalog.get(request.match_info["id"])
            asset = self.assets(entry).get(request.match_info["asset"])
            if not asset: raise web.HTTPNotFound(text="unindexed_evidence_file")
            inventory = read_json(entry["path"] / "import-inventory.json", {})
            sha = await asyncio.to_thread(file_digest, asset["path"])
            expected = inventory.get("files", {}).get(asset["name"])
            if expected and sha != expected: raise ValueError("evidence_digest_mismatch")
            return web.FileResponse(asset["path"], headers={"Content-Type": asset["mime_type"], "X-Evidence-SHA256": sha})
        async def trajectory_bundle(request):
            await self.refresh()
            identifier = request.match_info["id"]
            entry = self.catalog.get(identifier)
            availability = self.trajectory_download(entry)
            if availability["status"] != "available":
                return web.json_response(dict(error=availability["reason"]), status=409)
            async with self.trajectory_locks.setdefault(identifier, asyncio.Lock()):
                await self.refresh()
                entry = self.catalog.get(identifier)
                availability = self.trajectory_download(entry)
                if availability["status"] != "available":
                    return web.json_response(dict(error=availability["reason"]), status=409)
                path, sha = await asyncio.to_thread(self.trajectory_bundle, entry)
            return web.FileResponse(path, headers={"Content-Type": "application/zip",
                "Content-Disposition": f'attachment; filename="{identifier}-trajectory.zip"', "X-SHA256": sha})
        async def route(request):
            entry = self.catalog.get(request.match_info["id"])
            if entry["item"]["live"] and not heartbeat_fresh(entry["runtime"]):
                # Snapshot age can never extend a live control lease.
                await self.verify_live(entry)
            tail = request.match_info["tail"]
            if not PROXY_PATH.fullmatch(tail) or any(component in {".", ".."} for component in tail.split("/")) or "\\" in tail:
                raise web.HTTPNotFound(text="unsupported_run_route")
            if tail.startswith("runs/") and tail.split("/")[1] != entry["metadata"].get("evidence_run_id", entry["item"]["run_id"]):
                raise web.HTTPNotFound(text="run_identity_mismatch")
            if tail == "task-review":
                if request.method not in {"GET", "HEAD"}: raise web.HTTPForbidden(text="task_review_is_read_only")
                # Human viewers read observer evidence directly, including live runs;
                # this is never registered as an actor tool or added to prompts.
                return web.json_response(await asyncio.to_thread(self.task_reviews.get, entry))
            if request.method not in {"GET", "HEAD"} and not entry["item"]["writable"]: raise web.HTTPForbidden(text="replay_is_read_only")
            return await self.proxy(request, entry, tail) if entry.get("endpoint") else await self.replay(request, entry, tail)
        async def index(request):
            self.catalog.get(request.match_info["id"])
            text = (STATIC / "index.html").read_text()
            for name in ("app.js", "app.css", "styles.css"): text = text.replace(f'"/{name}"', f'"/assets/{name}"')
            return web.Response(text=text, content_type="text/html", headers={"Cache-Control": "no-store"})
        async def home(request): return web.FileResponse(STATIC / "library.html", headers={"Cache-Control": "no-store"})
        async def static(request):
            path = contained(STATIC, request.match_info["name"])
            if not path.is_file() or path.name not in {"app.js", "app.css", "styles.css", "library.js", "library.css"}: raise web.HTTPNotFound()
            return web.FileResponse(path, headers={"Cache-Control": "no-cache, max-age=0, must-revalidate"})
        app.router.add_get("/", home)
        app.router.add_get("/v1/experiments", listing)
        app.router.add_get("/v1/experiment-groups", groups)
        app.router.add_get("/v1/experiment-groups/{id}", group_detail)
        app.router.add_get("/v1/experiments/events", events)
        app.router.add_get("/v1/experiments/{id}", detail)
        app.router.add_get("/v1/experiments/{id}/media", media)
        app.router.add_get("/v1/experiments/{id}/files/{asset}", files)
        app.router.add_get("/v1/experiments/{id}/trajectory-bundle", trajectory_bundle)
        app.router.add_get("/runs/{id}/", index)
        app.router.add_route("*", "/runs/{id}/v1/{tail:.*}", route)
        app.router.add_get("/assets/{name}", static)
        app.router.add_get("/{name:app.js|app.css|styles.css}", static)
        return app


async def serve(args):
    library = Library(args.root, scan_interval=args.scan_interval, public_origin=getattr(args, "public_origin", None),
                      task_source_root=getattr(args, "task_source_root", None), config=getattr(args, "resolved_config", None))
    runner = web.AppRunner(library.app())
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", args.port).start()
        print(f"Experiment library http://127.0.0.1:{args.port}; root {library.catalog.root}", flush=True)
        await stopped.wait()
    finally: await runner.cleanup()


def main(argv=None):
    from ...core.config import Config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Unified Auriga JSON configuration")
    parser.add_argument("--print-config", action="store_true", help="Print resolved configuration and exit")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--scan-interval", type=float)
    parser.add_argument("--public-origin", type=https_origin, help="Exact HTTPS origin for an authenticated reverse proxy; no trailing slash")
    parser.add_argument("--task-source-root", type=Path)
    args = parser.parse_args(argv)
    overrides = {key: str(value) if isinstance(value, Path) else value
                 for key in ("root", "port", "scan_interval", "public_origin", "task_source_root")
                 if (value := getattr(args, key)) is not None}
    try:
        args.resolved_config = Config.load(args.config, overrides={"library": overrides})
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.print_config:
        print(json.dumps(args.resolved_config.to_dict(), indent=2))
        return
    for key, value in args.resolved_config.library.to_dict().items():
        setattr(args, key, value)
    asyncio.run(serve(args))


if __name__ == "__main__": main()
