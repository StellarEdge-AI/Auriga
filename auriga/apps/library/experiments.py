"""Filesystem experiment catalog; run evidence remains untouched."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ...core.contracts import canonical, new_id, utc_now
from .scoring import load_effective_summary
from ...core.storage.store import open_read_store

DEFAULT_ROOT = Path.home() / "exp" / "auriga"
CATALOG_ZONE = ZoneInfo("Asia/Shanghai")
HEARTBEAT_TTL = 15
TERMINAL_STATUSES = {"completed", "succeeded", "failed", "cancelled", "stopped", "expired", "unknown", "imported"}
REPORT_NAMES = ("summary.json", "validation-report.json", "reference-episode.json", "reference-report.json", "smoke-report.json", "report.json", "final-verification.json", "audit-final.json", "audit-initial.json", "replay-validation.json")
RUN_TIME_FIELDS = ("started_at", "finished_at", "started_at_source", "finished_at_source",
                   "registered_at", "registered_at_source", "imported_at", "imported_at_source")


def utc_timestamp(value):
    """Normalize recorded wall time; naive dates and non-ISO values stay unknown."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        pass
    return None


def safe_component(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) or value in {".", ".."}:
        raise ValueError("invalid_directory_component")
    return value


def contained(root, relative):
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("path_outside_run")
    return candidate


def read_json(path, default=None, *, object_only=True):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("symlink_metadata_denied")
    try:
        value = json.loads(path.read_text())
        if object_only and not isinstance(value, dict):
            raise ValueError("metadata_requires_object")
        return value
    except FileNotFoundError:
        return default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".metadata-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(canonical(value).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def register_run(path, *, category, task, title=None, tags=(), kind="workbench", run_id=None):
    path = Path(path).resolve()
    category, task, kind = map(safe_component, (category, task, kind))
    identifier = safe_component(run_id or path.name)
    if not isinstance(tags, (list, tuple)) or any(not isinstance(tag, str) or not 0 < len(tag) <= 128 for tag in tags):
        raise ValueError("invalid_tags")
    if title is not None and (not isinstance(title, str) or not 0 < len(title) <= 512):
        raise ValueError("invalid_title")
    path.mkdir(parents=True, exist_ok=True)
    previous = read_json(path / "experiment.json")
    if previous:
        if previous["run_id"] != identifier:
            raise ValueError("experiment_run_id_conflict")
        return previous
    return atomic_json(path / "experiment.json", dict(schema_version="0.1", run_id=identifier, category=category,
                       task=task, title=title or identifier, tags=sorted(set(tags)), kind=kind, created_at=utc_now()))


def allocate_run(root=None, *, category, task, title=None, tags=(), kind="workbench", run_id=None):
    root = Path(root or DEFAULT_ROOT).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    day = datetime.now(CATALOG_ZONE).strftime("%Y/%m/%d")
    path = contained(root, Path("runs") / day / safe_component(category) / safe_component(task) / safe_component(run_id or new_id("run")))
    path.mkdir(parents=True, exist_ok=False)
    register_run(path, category=category, task=task, title=title, tags=tags, kind=kind)
    return path


def endpoint_origin(endpoint):
    if endpoint is None:
        return None
    if not isinstance(endpoint, str):
        raise ValueError("invalid_live_endpoint")
    value = urlsplit(endpoint)
    if value.scheme != "http" or value.hostname not in {"127.0.0.1", "localhost", "::1"} or value.username or value.password or value.path not in {"", "/"} or value.query or value.fragment or not value.port:
        raise ValueError("invalid_live_endpoint")
    return endpoint.rstrip("/")


def write_runtime(path, value):
    if not isinstance(value, dict):
        raise ValueError("runtime_requires_object")
    path = Path(path)
    metadata = read_json(path / "experiment.json") or {}
    identifier = metadata.get("run_id", path.name)
    if value.get("run_id", identifier) != identifier:
        raise ValueError("runtime_run_id_conflict")
    result = {**read_json(path / "runtime.json", {}), **value, "run_id": identifier, "updated_at": utc_now()}
    if result.get("endpoint") is not None:
        result["endpoint"] = endpoint_origin(result["endpoint"])
    return atomic_json(path / "runtime.json", result)


def finish_run(path, *, status, summary=None):
    if status not in TERMINAL_STATUSES:
        raise ValueError("invalid_terminal_status")
    result = dict(summary or {}, status=status, finished_at=utc_now())
    atomic_json(Path(path) / "summary.json", result)
    write_runtime(path, dict(status=status, endpoint=None, finished_at=result["finished_at"]))
    return result


def heartbeat_fresh(runtime):
    try:
        timestamp = datetime.fromisoformat(runtime["updated_at"].replace("Z", "+00:00")).timestamp()
        return -5 <= time.time() - timestamp <= HEARTBEAT_TTL
    except (KeyError, TypeError, ValueError):
        return False


def catalog_id(relative):
    return "exp-" + hashlib.sha256(str(relative).encode()).hexdigest()[:32]


def evidence_path(entry):
    return contained(entry["path"], entry["metadata"].get("evidence_root", "."))


def infer_summary(path):
    for name in REPORT_NAMES:
        value = read_json(Path(path) / name, object_only=False)
        if isinstance(value, dict):
            if name == "summary.json":
                return value
            keys = ("status", "passed", "checks", "evaluation", "task_success", "normalized_score", "benchmark_result", "full_llm_policy_reproduction", "error", "errors", "loop_verified", "termination_reason", "validation_passed")
            summary = {key: value[key] for key in keys if key in value}
            native = value.get("native_evaluation_after_reset", value.get("native_evaluation"))
            if isinstance(native, dict) and isinstance(native.get("result"), dict):
                summary["evaluation"] = {**native["result"], **{key: native[key] for key in ("episode_id", "env_generation") if key in native}}
            if isinstance(value.get("alignment_acceptance"), dict): summary["validation_passed"] = value["alignment_acceptance"].get("passed")
            return normalize_summary({**summary, "source_report": name})
    return {}


def normalize_summary(value):
    result = dict(value)
    evaluation = result.get("evaluation") or {}
    if not isinstance(evaluation, dict):
        raise ValueError("evaluation_requires_object")
    for key in ("task_success", "normalized_score", "valid_episode"):
        if key not in result and key in evaluation: result[key] = evaluation[key]
    if "normalized_score" in result: result["score"] = result["normalized_score"]
    return result


class Catalog:
    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_ROOT).expanduser().resolve()
        self.entries, self.errors, self.revision = {}, [], ""
        self._time_cache, self._event_time_cache = {}, {}

    def _run_times(self, path, evidence, metadata, runtime, fingerprint):
        imported = bool(metadata.get("imported_from"))
        live = not imported and runtime.get("status") not in TERMINAL_STATUSES and heartbeat_fresh(runtime)
        time_files = {"experiment.json", "runtime.json", *REPORT_NAMES, "events.sqlite", "events.sqlite-wal"}
        signature = (tuple(row for row in fingerprint if row[0] in time_files),
                     canonical(metadata), canonical({key: runtime.get(key) for key in ("started_at", "finished_at")}), live)
        cached = self._time_cache.get(path)
        if cached is not None and cached[0] == signature:
            return cached[1]
        result = dict.fromkeys(RUN_TIME_FIELDS)
        warnings = []

        def record(field, value, source):
            if value is None or result[field] is not None:
                return
            stamp = utc_timestamp(value)
            if stamp is None:
                warnings.append("invalid_timestamp:" + source)
            else:
                result[field], result[field + "_source"] = stamp, source

        def read_source(name):
            try:
                value = read_json(evidence / name, {}, object_only=False)
                return value if isinstance(value, dict) else {}
            except (OSError, ValueError):
                warnings.append("unreadable_time_source:" + source_prefix + name)
                return {}

        source_prefix = "" if evidence == path else evidence.relative_to(path).as_posix() + "/"
        original_metadata = read_source("experiment.json") if imported else metadata
        record("registered_at", original_metadata.get("created_at"), source_prefix + "experiment.json:created_at")
        record("imported_at", metadata.get("imported_at"), "experiment.json:imported_at")
        database_signature = tuple(row for row in fingerprint if row[0] in {"events.sqlite", "events.sqlite-wal"})
        if database_signature:
            event_signature = (database_signature, live, metadata.get("evidence_run_id", metadata["run_id"]))
            event_cached = self._event_time_cache.get(evidence)
            if event_cached is None or event_cached[0] != event_signature:
                started, event_warning = None, None
                try:
                    with open_read_store(evidence, live=live) as store:
                        row = store.db.execute("SELECT seq, json_extract(body,'$.occurred_at'), json_extract(body,'$.run_id') "
                            "FROM events WHERE json_extract(body,'$.type')='run.started' ORDER BY seq LIMIT 1").fetchone()
                    if row:
                        if row[2] not in (None, event_signature[2]):
                            event_warning = "run_started_identity_conflict"
                        else:
                            started = (row[1], source_prefix + f"events.sqlite:run.started#{row[0]}")
                except (OSError, ValueError, sqlite3.Error):
                    event_warning = "run_started_evidence_unavailable"
                event_cached = (event_signature, started, event_warning)
                self._event_time_cache[evidence] = event_cached
            if event_cached[1]:
                record("started_at", *event_cached[1])
            if event_cached[2]:
                warnings.append(event_cached[2])
        # Imported runtime describes importing; only original evidence can date its execution.
        for name in ("summary.json", "runtime.json", *REPORT_NAMES[1:]):
            value = runtime if evidence == path and name == "runtime.json" else read_source(name)
            for field in ("started_at", "finished_at"):
                record(field, value.get(field), source_prefix + name + ":" + field)
            if result["started_at"] is not None and result["finished_at"] is not None:
                break
        if result["started_at"] and result["finished_at"] and result["finished_at"] < result["started_at"]:
            warnings.append("finished_before_started:" + result["finished_at_source"])
            result["finished_at"] = result["finished_at_source"] = None
        result["time_coverage"] = dict(run_count=1, started_count=int(result["started_at"] is not None),
                                       finished_count=int(result["finished_at"] is not None))
        result["time_warnings"] = warnings
        self._time_cache[path] = (signature, result)
        return result

    def metadata_paths(self):
        for directory, directories, files in os.walk(self.root, followlinks=False):
            path = Path(directory)
            directories[:] = sorted(name for name in directories
                                    if name != "private" and not (path / name).is_symlink()
                                    and not excluded_path((path / name).relative_to(self.root)))
            if "experiment.json" in files:
                # A registered run owns its subtree, including imported evidence
                # that may contain the source run's original metadata.
                directories.clear()
                yield path / "experiment.json"

    def scan(self):
        entries, errors = {}, []
        # ponytail: linear metadata scan; use notifications when catalog size makes scans expensive.
        for metadata_path in self.metadata_paths():
            try:
                metadata = read_json(metadata_path)
                path = contained(self.root, metadata_path.relative_to(self.root)).parent
                for field in ("category", "task", "run_id", "kind"):
                    safe_component(metadata[field])
                if not isinstance(metadata.get("tags"), list) or any(not isinstance(tag, str) for tag in metadata["tags"]):
                    raise ValueError("invalid_tags")
                created = datetime.fromisoformat(metadata["created_at"].replace("Z", "+00:00"))
                if created.tzinfo is None: raise ValueError("created_at_requires_timezone")
                runtime = read_json(path / "runtime.json", {})
                if runtime.get("run_id", metadata["run_id"]) != metadata["run_id"]:
                    raise ValueError("runtime_run_id_conflict")
                identifier = catalog_id(path.relative_to(self.root).as_posix())
                entry = dict(path=path, metadata=metadata, runtime=runtime)
                evidence = evidence_path(entry)
                summary = normalize_summary(load_effective_summary(path, evidence,
                    summary=read_json(path / "summary.json") or infer_summary(evidence)))
                status = runtime.get("status") or summary.get("status") or ("imported" if metadata.get("imported_from") else "recorded")
                item = {**metadata, "id": identifier, "updated_at": runtime.get("updated_at", metadata["created_at"]),
                        "status": status, "live": False, "writable": False, "read_only": True,
                        "view_url": f"/runs/{identifier}/", "summary": summary,
                        "replay_available": (evidence / "events.sqlite").is_file(), "terminal_available": (evidence / "terminal.bin").is_file()}
                entry["fingerprint"] = [(name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                                        for name in (*REPORT_NAMES, "experiment.json", "runtime.json", "manifest.json", "events.sqlite", "events.sqlite-wal", "terminal.bin")
                                        if (target := evidence / name).is_file() and not target.is_symlink()
                                        for info in (target.stat(),)]
                item.update(self._run_times(path, evidence, metadata, runtime, entry["fingerprint"]))
                entry["item"] = item
                entries[identifier] = entry
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append(dict(path=str(metadata_path.relative_to(self.root)), error=str(error)))
        revision = hashlib.sha256(canonical({key: {"item": value["item"], "runtime": value["runtime"], "fingerprint": value["fingerprint"]} for key, value in entries.items()}).encode()).hexdigest()
        changed = revision != self.revision or errors != self.errors
        self.entries, self.errors, self.revision = entries, errors, revision
        paths = {entry["path"] for entry in entries.values()}
        self._time_cache = {path: value for path, value in self._time_cache.items() if path in paths}
        evidence_paths = {evidence_path(entry) for entry in entries.values()}
        self._event_time_cache = {path: value for path, value in self._event_time_cache.items() if path in evidence_paths}
        return changed

    def get(self, identifier):
        if not re.fullmatch(r"exp-[0-9a-f]{32}", identifier):
            raise KeyError(identifier)
        entry = self.entries[identifier]
        # A published snapshot is not permission to follow a subsequently swapped run directory.
        if entry["path"].resolve() != entry["path"]:
            raise ValueError("symlink_run_directory_denied")
        return entry

    def list(self, query=None):
        query = query or {}
        items = [entry["item"] for entry in self.entries.values()]
        facets = dict(categories=sorted({item["category"] for item in items}), tasks=sorted({item["task"] for item in items}),
                      statuses=sorted({item["status"] for item in items}), tags=sorted({tag for item in items for tag in item["tags"]}))
        for name in ("date_from", "date_to"):
            if query.get(name): datetime.strptime(query[name], "%Y-%m-%d")
        for name in ("category", "task", "status"):
            if query.get(name): items = [item for item in items if item[name] == query[name]]
        if query.get("tag"): items = [item for item in items if query["tag"] in item["tags"]]
        if query.get("q"):
            needle = query["q"].casefold()
            items = [item for item in items if needle in " ".join(str(item.get(key, "")) for key in ("title", "run_id", "category", "task", "tags")).casefold()]
        if query.get("date_from"):
            items = [item for item in items if item["started_at"] and datetime.fromisoformat(item["started_at"].replace("Z", "+00:00")).astimezone(CATALOG_ZONE).date().isoformat() >= query["date_from"]]
        if query.get("date_to"):
            items = [item for item in items if item["started_at"] and datetime.fromisoformat(item["started_at"].replace("Z", "+00:00")).astimezone(CATALOG_ZONE).date().isoformat() <= query["date_to"]]
        sort = query.get("sort", "newest")
        if sort not in {"newest", "oldest", "updated", "active"}: raise ValueError("invalid_sort")
        sort_field = "updated_at" if sort in {"updated", "active"} else "started_at"
        items.sort(key=lambda item: ((item["live"] if sort == "active" else False), item.get(sort_field) or "", item["id"]), reverse=sort != "oldest")
        items.sort(key=lambda item: item.get(sort_field) is None)
        offset, limit = int(query.get("offset", 0)), int(query.get("limit", 50))
        if offset < 0 or not 1 <= limit <= 500: raise ValueError("invalid_pagination")
        return dict(experiments=items[offset:offset + limit], total=len(items), offset=offset, limit=limit, facets=facets, errors=self.errors)


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def excluded_path(relative):
    parts = Path(relative).parts
    return ("private" in parts and "codex-home" in parts) or any(part in {"auth.json", "credentials.json", ".env", ".aws"} for part in parts)


def import_run(source, root=None, *, category="legacy", task="unknown", title=None, tags=(), kind="recording", run_id=None):
    source, root = Path(source).resolve(), Path(root or DEFAULT_ROOT).expanduser().resolve()
    if not source.is_dir() or root.is_relative_to(source): raise ValueError("invalid_import_source")
    runtime = read_json(source / "runtime.json", {})
    if heartbeat_fresh(runtime) and runtime.get("status") not in TERMINAL_STATUSES: raise ValueError("import_requires_closed_run")
    lock = (source / "gateway.lock").open("rb") if (source / "gateway.lock").exists() else None
    try:
        if lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError("import_requires_unowned_run") from None
        files, excluded = {}, []
        for item in source.rglob("*"):
            relative = item.relative_to(source).as_posix()
            if excluded_path(relative) or item.is_symlink() or not (item.is_dir() or stat.S_ISREG(item.lstat().st_mode)):
                excluded.append(relative)
            elif item.is_file(): files[relative] = file_hash(item)
        path = allocate_run(root, category=category, task=task, title=title or source.name, tags=tags, kind=kind, run_id=run_id)
        evidence = path / "evidence"
        evidence.mkdir()
        for relative, expected in files.items():
            target = contained(evidence, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
            if file_hash(source / relative) != expected or file_hash(target) != expected:
                write_runtime(path, dict(status="unknown", endpoint=None, error="import_source_changed_or_copy_mismatch"))
                raise ValueError("import_source_changed_or_copy_mismatch")
        after = {item.relative_to(source).as_posix(): file_hash(item) for item in source.rglob("*")
                 if item.is_file() and not item.is_symlink() and not excluded_path(item.relative_to(source))}
        if after != files:
            write_runtime(path, dict(status="unknown", endpoint=None, error="import_source_changed_or_copy_mismatch"))
            raise ValueError("import_source_changed_or_copy_mismatch")
        inventory = dict(source_root=str(source), evidence_root="evidence", files=files, excluded=excluded, verified_at=utc_now())
        if "events.sqlite-wal" in files:
            # Copy into a temporary workspace before SQLite opens WAL; archived bytes stay unchanged.
            replay = path / "replay"
            replay.mkdir()
            with tempfile.TemporaryDirectory() as temporary:
                for name in ("events.sqlite", "events.sqlite-wal", "events.sqlite-shm"):
                    if name in files: shutil.copy2(evidence / name, Path(temporary) / name)
                source_db = sqlite3.connect(Path(temporary) / "events.sqlite")
                destination_db = sqlite3.connect(replay / "events.sqlite")
                try: source_db.backup(destination_db)
                finally:
                    destination_db.close()
                    source_db.close()
            inventory["replay_database"] = "replay/events.sqlite"
        metadata = read_json(path / "experiment.json")
        manifest = read_json(evidence / "manifest.json", {})
        source_metadata = read_json(evidence / "experiment.json", {})
        metadata.update(imported_from=str(source), evidence_root="evidence", evidence_run_id=source_metadata.get("run_id") or manifest.get("run_id") or source.name)
        metadata["imported_at"] = utc_now()
        if source_metadata.get("created_at"):
            metadata.update(created_at=source_metadata["created_at"], date_source="source_metadata")
        elif (evidence / "events.sqlite").is_file():
            database_path = path / inventory.get("replay_database", "evidence/events.sqlite")
            database = sqlite3.connect(database_path.as_uri() + "?mode=ro&immutable=1", uri=True)
            try: row = database.execute("SELECT body FROM events ORDER BY seq LIMIT 1").fetchone()
            finally: database.close()
            if row and json.loads(row[0]).get("occurred_at"):
                metadata.update(created_at=json.loads(row[0])["occurred_at"], date_source="first_recorded_event")
        if "date_source" not in metadata:
            report = next((evidence / name for name in REPORT_NAMES if (evidence / name).is_file()), source)
            metadata.update(created_at=datetime.fromtimestamp(report.stat().st_mtime, timezone.utc).isoformat(), date_source="report_mtime")
        atomic_json(path / "experiment.json", metadata)
        atomic_json(path / "import-inventory.json", inventory)
        summary = infer_summary(evidence)
        status = summary.get("status")
        if status not in TERMINAL_STATUSES:
            status = "failed" if summary.get("error") or summary.get("errors") or summary.get("passed") is False else "completed" if summary.get("passed") is True or summary.get("loop_verified") is True else "imported"
        atomic_json(path / "summary.json", summary if (evidence / "summary.json").is_file() else {**summary, "status": status})
        write_runtime(path, dict(status=status, endpoint=None))
        return path
    finally:
        if lock: lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    for operation in ("allocate", "import"):
        command = commands.add_parser(operation)
        if operation == "import": command.add_argument("source", type=Path)
        command.add_argument("--category", default="legacy" if operation == "import" else "main")
        command.add_argument("--task", default="unknown")
        command.add_argument("--title")
        command.add_argument("--tag", action="append", default=[])
        command.add_argument("--kind", default="recording" if operation == "import" else "workbench")
        command.add_argument("--run-id")
    command = commands.add_parser("list")
    for name in ("category", "task", "status", "q"): command.add_argument("--" + name)
    args = parser.parse_args()
    if args.command == "list":
        catalog = Catalog(args.root)
        catalog.scan()
        print(json.dumps(catalog.list(vars(args)), indent=2))
    else:
        kwargs = dict(category=args.category, task=args.task, title=args.title, tags=args.tag, kind=args.kind, run_id=args.run_id)
        print(import_run(args.source, args.root, **kwargs) if args.command == "import" else allocate_run(args.root, **kwargs))


if __name__ == "__main__":
    main()
