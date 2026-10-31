"""Independent aiohttp scene API (CPU only). Run: python -m auriga.perception.scene.scene_service --root PATH.

POST operations require Idempotency-Key. Geometry requests contain environment identity,
kind, observation_ids, camera_id (and camera_id_b for stereo), plus tool arguments.
update-jobs additionally freezes base_scene_version and optional entity_id/ROI/primitive.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import hashlib
import json
import multiprocessing
import os
import signal
import re
import time
from pathlib import Path

from aiohttp import web

from ...core.contracts import IDENTITY, canonical, digest, new_id, utc_now, validate_identity
from ...robot.model.geometry import GeometryTools
from ...core.storage.images import image_settings as resolve_image_settings
from .store import SceneStore
from .scene_contracts import (GET_CONTRACTS, POST_CONTRACTS,
    openapi, validate_request, validate_response, validate as validate_contract, http_error)
from ...core.storage.store import Store
from ...core.storage.read_models import artifact_response
from ...core.tool_errors import ToolInputError, error_payload

TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
MAX_BLOB = 32 * 1024 * 1024
GET_PATHS = tuple(GET_CONTRACTS)
POST_PATHS = tuple(POST_CONTRACTS)


def read_scene_job(store, identifier):
    """Read an existing job, including sealed archives, without service startup."""
    with store.lock:
        row = store.db.execute("SELECT body FROM scene_jobs WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise KeyError(identifier)
    return json.loads(row[0])


def _compute(root, request, connection, parent_pid, image_settings=None):
    """A job owns this process; cancellation never terminates unrelated workers."""
    # Linux parent-death signal closes the crash-before-recovery orphan window.
    if ctypes.CDLL(None).prctl(1,signal.SIGTERM,0,0,0)!=0:
        connection.send({"error":"worker_parent_death_guard_unavailable"}); connection.close(); return
    if os.getppid()!=parent_pid: return
    store = Store(root, {key: request[key] for key in IDENTITY})
    try:
        scene = SceneStore(Path(root)/"scene", store.resolve_artifact, store.put_artifact)
        connection.send({"result": GeometryTools(scene, settings=image_settings).execute(request)})
    except BaseException as error:
        connection.send({"error":type(error).__name__+":"+str(error)})
    finally:
        connection.close()
        store.close()


class SceneService:
    def __init__(self, root, *, max_workers=2, max_queue=4, max_blob_store_bytes=512*1024*1024, image_settings=None):
        self.image_settings = resolve_image_settings(image_settings)
        self.root = Path(root)
        self.store = Store(root, dict(run_id="scene-service",episode_id="service",env_instance_id="internal",env_generation=1))
        self.scene = SceneStore(self.root/"scene",self.store.resolve_artifact,self.store.put_artifact)
        self.store.source_instance_id = new_id("scene-api")
        self.db = self.store.db
        with self.db:
            self.db.executescript("CREATE TABLE IF NOT EXISTS scene_jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL); CREATE TABLE IF NOT EXISTS scene_requests (scope TEXT PRIMARY KEY, digest TEXT NOT NULL, status INTEGER NOT NULL, body TEXT NOT NULL);")
        self.max_queue = max_queue
        self.max_blob_store_bytes = max_blob_store_bytes
        self.semaphore = asyncio.Semaphore(max_workers)
        self.tasks = {}
        self.processes = {}
        self.closing = False
        self.closed = False

    def import_bundle(self, source_store, bundle):
        """Local counterpart of TwinClient.import_bundle; copies verified immutable inputs."""
        seen=set()
        def walk(value):
            if isinstance(value,dict):
                for child in value.values(): walk(child)
            elif isinstance(value,list):
                for child in value: walk(child)
            elif isinstance(value,str) and value.startswith("artifact:") and value not in seen:
                seen.add(value)
                metadata,content=source_store.resolve_artifact(value,allowed_scopes=("actor",))
                ref=self.store.put_artifact(content,metadata["kind"],metadata["mime_type"],"actor")
                copied=self.store.artifact(ref)[0]
                if copied["artifact_id"]!=metadata["artifact_id"]:
                    copied={**copied,"artifact_id":metadata["artifact_id"]}
                    with self.store.lock,self.db:
                        existing=self.db.execute("SELECT body FROM artifacts WHERE id=?",(copied["artifact_id"],)).fetchone()
                        if existing and json.loads(existing[0])!=copied: raise ValueError("immutable_artifact_conflict")
                        self.db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?)",(copied["artifact_id"],canonical(copied)))
                if metadata["mime_type"]=="application/json": walk(json.loads(content))
        walk(bundle)
        return self.scene.import_observation(bundle)

    def jobs(self):
        with self.store.lock:
            rows = self.db.execute("SELECT body FROM scene_jobs ORDER BY rowid").fetchall()
        return [json.loads(body) for body, in rows]

    def event_page(self, after=0):
        """Scene's local cursor is distinct from forwarded global server_seq."""
        with self.store.lock:
            rows = self.db.execute("SELECT seq,body FROM events WHERE seq>? ORDER BY seq LIMIT 500", (after,)).fetchall()
        return dict(events=[json.loads(body) for _, body in rows], next_cursor=rows[-1][0] if rows else after)

    def job(self, identifier):
        try:
            return read_scene_job(self.store, identifier)
        except KeyError:
            raise ValueError("unknown_job") from None

    def _append_event(self, event_type, payload, identity, correlation=None):
        """Caller holds the Store lock and transaction, including its state mutation."""
        self.store.source_seq += 1
        event={"schema_version":"0.1",**{key:identity[key] for key in IDENTITY},"event_id":new_id("evt"),"source":"scene_api","source_instance_id":self.store.source_instance_id,"source_seq":self.store.source_seq,"world_kind":"internal","type":event_type,"occurred_at":utc_now(),"received_at":utc_now(),"clock_domain":"scene_service_wall","sim_step":None,"step_domain":None,"sim_time_s":None,"correlation":correlation or {},"payload":payload}
        self.db.execute("INSERT INTO events(body) VALUES (?)",(canonical(event),))
        return event

    def transition(self, job, status, **fields):
        if job["status"] in TERMINAL:
            return job
        job = {**job,**fields,"status":status,"revision":job["revision"]+1}
        if status in TERMINAL:
            job["finished_at"] = utc_now()
        event_type = "job.cancel_requested" if fields.get("cancel_requested") else {"running":"job.started","succeeded":"job.completed","failed":"job.failed","cancelled":"job.cancelled","expired":"job.expired"}.get(status,"job.queued")
        # Job update and outbox event use the same SQLite connection/transaction.
        with self.store.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO scene_jobs VALUES (?,?)",(job["job_id"],canonical(job)))
            self._append_event(event_type,job,job,{"job_id":job["job_id"]})
            if status=="succeeded" and job.get("result",{}).get("scene"):
                self._append_event("scene.updated",job["result"]["scene"],job,{"job_id":job["job_id"],"scene_version":job["result"]["scene"]["scene_version"]})
        return job

    async def start(self, app=None):
        for (body,) in self.db.execute("SELECT body FROM scene_jobs").fetchall():
            job = json.loads(body)
            if job["status"] == "running":
                self.transition(job,"failed",error="worker_lost_on_restart",phase="recovery",partial=True)
            elif job["status"] == "queued":
                self.tasks[job["job_id"]] = asyncio.create_task(self.run(job["job_id"]))

    async def stop_jobs(self):
        """Persist cancellation and confirm owned workers stopped; keep outbox readable."""
        if self.closed:
            return
        self.closing = True
        for (body,) in self.db.execute("SELECT body FROM scene_jobs").fetchall():
            job = json.loads(body)
            if job["status"] not in TERMINAL and not job.get("cancel_requested"):
                self.transition(job,job["status"],cancel_requested=True)
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        # run() normally performs this cleanup; cover a worker without a live task too.
        for identifier, process in list(self.processes.items()):
            if process.is_alive(): process.terminate()
            await asyncio.to_thread(process.join,2)
            if process.is_alive(): process.kill(); await asyncio.to_thread(process.join)
            self.processes.pop(identifier,None)
        for (body,) in self.db.execute("SELECT body FROM scene_jobs").fetchall():
            job = json.loads(body)
            if job["status"] not in TERMINAL:
                self.transition(job,"cancelled",phase="shutdown",stop_confirmed=True,partial=True)

    async def close(self, app=None):
        if self.closed:
            return
        await self.stop_jobs()
        self.store.close()
        self.closed = True

    async def run(self, identifier):
        async with self.semaphore:
            job = self.job(identifier)
            if job["status"] in TERMINAL:
                return
            if job.get("cancel_requested"):
                self.transition(job,"cancelled",phase="queued",stop_confirmed=True)
                return
            if time.time() > job.get("queue_deadline_epoch_s",job["deadline_epoch_s"]):
                self.transition(job,"expired",error="deadline_exceeded",phase="queued")
                return
            started = time.monotonic()
            token = new_id("attempt")
            job = self.transition(job,"running",started_at=utc_now(),phase="building",attempt_token=token,attempt=job.get("attempt",0)+1)
            context = multiprocessing.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_compute,args=(str(self.root),job["request"],sender,os.getpid(),self.image_settings),daemon=True)
            self.processes[identifier] = process
            try:
                process.start()
            except Exception as error:
                self.transition(job,"failed",error="worker_start_failed:"+str(error),partial=True)
                receiver.close(); sender.close(); self.processes.pop(identifier,None)
                return
            sender.close()
            outcome = None
            result = None
            try:
                while process.is_alive() or receiver.poll():
                    job = self.job(identifier)
                    elapsed = time.monotonic()-started
                    if job.get("cancel_requested") or elapsed > job["budget"]["wall_time_s"] or time.time() > job["deadline_epoch_s"]:
                        process.terminate()
                        await asyncio.to_thread(process.join,2)
                        if process.is_alive(): process.kill(); process.join()
                        status = "cancelled" if job.get("cancel_requested") else "failed"
                        self.transition(job,status,error="cancelled" if status=="cancelled" else "budget_exceeded",stop_confirmed=True,partial=True,actual_resources={"wall_time_s":elapsed})
                        return
                    if receiver.poll():
                        try: outcome = receiver.recv()
                        except EOFError: pass
                        break
                    await asyncio.sleep(0.02)
                await asyncio.to_thread(process.join,2)
                if process.is_alive():
                    process.terminate(); await asyncio.to_thread(process.join,2)
                    if process.is_alive(): process.kill(); await asyncio.to_thread(process.join,2)
                job = self.job(identifier)
                if job.get("attempt_token") != token or job["status"] != "running":
                    return
                if job.get("cancel_requested"):
                    self.transition(job,"cancelled",stop_confirmed=True,partial=True)
                    return
                if time.monotonic()-started > job["budget"]["wall_time_s"] or time.time()>job["deadline_epoch_s"]:
                    self.transition(job,"failed",error="budget_exceeded",partial=True,stop_confirmed=True)
                    return
                if outcome is None or "error" in outcome:
                    self.transition(job,"failed",error=(outcome or {}).get("error","worker_crashed"),partial=True)
                    return
                result = outcome["result"]
                if result["status"] in ("unknown","invalid_input"):
                    self.transition(job,"failed",error=result.get("reason","insufficient_geometry"),result=result)
                    return
                if job["kind"] == "build_scene":
                    result["scene"] = self.scene.apply_patch(result["value"]["proposal"])
                reference = self.store.put_artifact(canonical(result).encode(),"scene_job_result","application/json","internal")
                self.transition(job,"succeeded",phase="completed",result_ref=reference,result=result,partial=False,actual_resources={"wall_time_s":time.monotonic()-started})
            except Exception as error:
                if not self.closing:
                    self.transition(self.job(identifier),"failed",error=str(error),partial=True,result=result,actual_resources={"wall_time_s":time.monotonic()-started})
            finally:
                receiver.close()
                if process.is_alive():
                    process.terminate(); await asyncio.to_thread(process.join,2)
                    if process.is_alive(): process.kill(); await asyncio.to_thread(process.join,2)
                self.processes.pop(identifier,None)

    def submit(self, request, kind, identifier=None):
        if self.closing:
            raise web.HTTPServiceUnavailable(text="service_closing")
        validate_identity({"schema_version":"0.1",**request})
        validate_contract("GeometryJobRequest", {**request, "kind": kind})
        if identifier is not None:
            row = self.db.execute("SELECT body FROM scene_jobs WHERE id=?",(identifier,)).fetchone()
            if row:
                previous = json.loads(row[0])
                if previous["input_digest"] != digest({**request,"kind":kind}):
                    raise ValueError("idempotency_conflict")
                return previous
        queued = sum(json.loads(row[0])["status"] not in TERMINAL for row in self.db.execute("SELECT body FROM scene_jobs"))
        if queued >= self.max_queue:
            raise web.HTTPTooManyRequests(text="resource_exhausted")
        budget = request.get("budget",{})
        wall = budget.get("wall_time_s",30)
        queue = budget.get("queue_time_s",30)
        if type(wall) not in (int,float) or type(queue) not in (int,float) or not 0 < wall <= 120 or not 0 <= queue <= 600:
            raise ValueError("invalid_budget")
        request = {**request,"kind":kind}
        observations=[self.scene.observation(request,identifier) for identifier in request.get("observation_ids",[])]
        frozen_inputs={"observations":{item["observation_id"]:digest(item) for item in observations},"artifacts":self.scene.closure(observations)}
        if request.get("scene_version") is not None:
            frozen_inputs["scene_digest"]=digest(self.scene.version(request,request["scene_version"]))
        job = {"schema_version":"0.1",**{key:request[key] for key in IDENTITY},"world_kind":"internal","job_id":identifier or new_id("job"),"kind":kind,"status":"new","revision":0,"phase":"queued","request":request,"input_digest":digest(request),"frozen_inputs":frozen_inputs,"budget":{"wall_time_s":wall,"queue_time_s":queue},"deadline_epoch_s":time.time()+wall+queue,"queue_deadline_epoch_s":time.time()+queue,"created_at":utc_now(),"cancel_requested":False}
        job = self.transition(job,"queued")
        self.tasks[job["job_id"]] = asyncio.create_task(self.run(job["job_id"]))
        return job

    async def post(self, http):
        request = await http.json()
        if not isinstance(request,dict): raise ValueError("request_must_be_object")
        route = http.match_info.route.resource.canonical.removeprefix("/v1/")
        validate_request(route, request)
        key = http.headers.get("Idempotency-Key")
        if not key or len(key)>256:
            raise ValueError("missing_or_invalid_idempotency_key")
        scope = digest([http.path,[request.get(k) for k in IDENTITY],key])
        request_digest = digest(request)
        previous = self.db.execute("SELECT digest,status,body FROM scene_requests WHERE scope=?",(scope,)).fetchone()
        if previous:
            if previous[0] != request_digest:
                raise web.HTTPConflict(text="idempotency_conflict")
            return web.json_response(json.loads(previous[2]),status=previous[1])
        status = 201
        event_type = None
        path = http.path
        if path == "/v1/blobs":
            data = base64.b64decode(request["data_base64"],validate=True)
            if len(data)>MAX_BLOB or hashlib.sha256(data).hexdigest()!=request["sha256"]:
                raise ValueError("blob_size_or_digest_mismatch")
            if request.get("access_scope","actor") != "actor":
                raise ValueError("only_actor_artifacts_accepted")
            used = self.db.execute("SELECT COALESCE(SUM(size),0) FROM (SELECT MAX(json_extract(body,'$.byte_size')) AS size FROM artifacts GROUP BY json_extract(body,'$.sha256'))").fetchone()[0]
            if used+len(data)>self.max_blob_store_bytes and not (self.store.blobs/request["sha256"]).exists():
                raise web.HTTPTooManyRequests(text="artifact_storage_quota_exceeded")
            reference = self.store.put_artifact(data,request["kind"],request["mime_type"])
            metadata = self.store.artifact(reference)[0]
            alias = request.get("source_artifact_id")
            if alias is not None:
                if not isinstance(alias,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}",alias):
                    raise ValueError("invalid_source_artifact_id")
                metadata = {**metadata,"artifact_id":alias}
                with self.store.lock, self.db:
                    existing = self.db.execute("SELECT body FROM artifacts WHERE id=?",(alias,)).fetchone()
                    if existing and json.loads(existing[0]) != metadata: raise ValueError("immutable_artifact_conflict")
                    self.db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?)",(alias,canonical(metadata)))
                reference = "artifact:"+alias
            response = {"ref":reference,"artifact":metadata}
        elif path == "/v1/observations":
            response = self.scene.import_observation(request)
            event_type = "observation.imported"
        elif path == "/v1/scene/patches":
            try:
                response = self.scene.apply_patch({**request,"idempotency_token":scope})
            except ValueError as error:
                diagnostic={"request":request,"reason":str(error),"received_at":utc_now()}
                self.store.save("rejected_scene_patch",new_id("rejected"),diagnostic)
                with self.store.lock,self.db: self._append_event("scene.patch_rejected",diagnostic,request)
                raise
            event_type = "scene.updated"
        elif path in ("/v1/scene/update-jobs","/v1/alignment-jobs","/v1/geometry-jobs"):
            response = self.submit(request,{"/v1/scene/update-jobs":"build_scene","/v1/alignment-jobs":"alignment"}.get(path,request.get("kind")),identifier="job-"+scope)
            status = 202
        elif path == "/v1/scene/query":
            response = self.scene.query(request,request["scene_version"],request)
            status = 200
        elif path == "/v1/geometry/query":
            if request.get("kind") in ("sparse_track","triangulate","build_scene","alignment","preview_ee","project_geometry","project_points"):
                response = self.submit(request,request["kind"],identifier="job-"+scope)
                status = 202
            else:
                response = GeometryTools(self.scene, settings=self.image_settings).execute(request)
                status = 200
        elif path == "/v1/snapshots":
            response = self.scene.snapshot(request,request["scene_version"],request["anchor_observation_id"])
            event_type = "snapshot.created"
        elif path in ("/v1/branches","/v1/rollout-jobs","/v1/compare-jobs"):
            response = self.scene.rollout(request)
            status = 422
        elif path.endswith("/cancel"):
            identifier = http.match_info["identifier"]
            job = self.job(identifier)
            if any(request.get(key)!=job[key] for key in IDENTITY):
                raise ValueError("identity_conflict")
            if job["status"] not in TERMINAL:
                job = self.transition(job,job["status"],cancel_requested=True,cancel_reason=request.get("reason","client_request"))
            response = job
            status = 200
        else:
            raise web.HTTPNotFound()
        validate_response(route, response, method="POST", status=status)
        with self.store.lock,self.db:
            self.db.execute("INSERT INTO scene_requests VALUES (?,?,?,?)",(scope,request_digest,status,canonical(response)))
            if event_type: self._append_event(event_type,response,request)
        return web.json_response(response,status=status)

    async def get(self,http):
        path = http.path
        route = http.match_info.route.resource.canonical.removeprefix("/v1/")
        # Query strings need parsing before the same typed identity rules apply.
        identity = None
        if route.startswith(("scene/", "snapshots/")):
            identity = {key: http.query.get(key) for key in IDENTITY}
            try:
                identity["env_generation"] = int(identity["env_generation"])
            except (TypeError, ValueError):
                raise ToolInputError("invalid_env_generation", field_path="/env_generation",
                                     actual=identity["env_generation"])
            validate_identity(dict(schema_version="0.1", **identity))
        if path == "/v1/openapi.json": response = openapi()
        elif path == "/v1/health": response = {"schema_version":"0.1","status":"ready","physics_worker_ready":False}
        elif path == "/v1/capabilities": response = {**self.scene.capabilities(),"cpu_jobs":True,"max_queue":self.max_queue,"max_blob_bytes":MAX_BLOB,"max_blob_store_bytes":self.max_blob_store_bytes}
        elif path == "/v1/events":
            try:
                after = int(http.query.get("after", 0))
                if after < 0: raise ValueError()
            except ValueError:
                raise ToolInputError("invalid_event_cursor", field_path="/after", actual=http.query.get("after"))
            response = self.event_page(after)
        elif path.startswith("/v1/jobs/"): response = self.job(http.match_info["identifier"])
        elif path.startswith("/v1/artifacts/"):
            return await artifact_response(self.store, "artifact:" + http.match_info["identifier"], allowed_scopes=("actor", "internal"))
        else:
            if path=="/v1/scene/head": response=self.scene.head(identity)
            elif path=="/v1/scene/versions": response={"versions":self.scene.versions(identity)}
            elif path.startswith("/v1/snapshots/"): response=self.scene.read_snapshot(identity,http.match_info["identifier"])
            else: response=self.scene.version(identity,int(http.match_info["version"]))
        if route != "openapi.json":
            validate_response(route, response)
        return web.json_response(response)

    def app(self):
        @web.middleware
        async def errors(request,handler):
            try: return await handler(request)
            except web.HTTPException as error:
                payload = error_payload(ToolInputError(error.text or error.reason))
                payload["message"] = payload["error"]
                return web.json_response(payload, status=error.status)
            except (ValueError,KeyError,TypeError) as error:
                payload, status = http_error(error)
                if payload["code"] == "invalid_scene_response":
                    status = 500
                return web.json_response(payload, status=status)
        app = web.Application(middlewares=[errors],client_max_size=MAX_BLOB*2)
        for path in (*GET_PATHS,"openapi.json"):
            app.router.add_get("/v1/"+path,self.get)
        for path in POST_PATHS:
            app.router.add_post("/v1/"+path,self.post)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.close)
        return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Unified Auriga JSON configuration")
    parser.add_argument("--print-config", action="store_true", help="Print resolved configuration and exit")
    parser.add_argument("--root", default=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--max-workers", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--max-queue", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--max-blob-store-bytes", type=int, default=argparse.SUPPRESS)
    args = parser.parse_args()
    from ...core.config import Config, ConfigError
    overrides = {key: value for key, value in vars(args).items() if key not in {"config", "print_config"}}
    try:
        config = Config.load(args.config, overrides={"scene": overrides})
    except ConfigError as error:
        parser.error(str(error))
    if args.print_config:
        print(json.dumps(config.to_dict(), indent=2))
        return
    scene = config.scene
    web.run_app(SceneService(scene.root, max_workers=scene.max_workers, max_queue=scene.max_queue,
                             max_blob_store_bytes=scene.max_blob_store_bytes, image_settings=config.images.to_dict()).app(),
                host="127.0.0.1", port=scene.port)

if __name__ == "__main__": main()
