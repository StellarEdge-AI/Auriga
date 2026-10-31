"""Thin read/build-only TwinClient; never exposes observed action/reset permissions.

Replay: python -m auriga.perception.scene.scene_client replay MANIFEST --url http://127.0.0.1:8770
Manifest contains artifacts (storage_key relative to manifest), observations or obs_jsonl,
and optional requests [{path, body, idempotency_key}]. No evaluator directory is opened.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path

import aiohttp

from ...core.contracts import digest, new_id


class TwinClient:
    def __init__(self, url="http://127.0.0.1:8770", timeout_s=150):
        self.url = url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def request(self, method, path, body=None, *, idempotency_key=None, params=None):
        headers = {"Idempotency-Key":idempotency_key or new_id("request")} if method=="POST" else {}
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.request(method,self.url+path,json=body,params=params,headers=headers) as response:
                text = await response.text()
                try: result = json.loads(text)
                except ValueError: result = {"error":text}
                if response.status >= 400:
                    raise ValueError(f"scene_http_{response.status}:{result}")
                return result

    async def capabilities(self): return await self.request("GET","/v1/capabilities")
    async def observation(self, bundle): return await self.request("POST","/v1/observations",bundle,idempotency_key="obs-"+digest(bundle))
    async def head(self, identity): return await self.request("GET","/v1/scene/head",params=identity)
    async def geometry(self, request): return await self.request("POST","/v1/geometry/query",request)
    async def query(self, request): return await self.request("POST","/v1/scene/query",request)
    async def update(self, request, key=None): return await self.request("POST","/v1/scene/update-jobs",request,idempotency_key=key)
    async def alignment(self, request, key=None): return await self.request("POST","/v1/alignment-jobs",request,idempotency_key=key)
    async def snapshot(self, request, key=None): return await self.request("POST","/v1/snapshots",request,idempotency_key=key)
    async def job(self, identifier): return await self.request("GET","/v1/jobs/"+identifier)
    async def cancel(self, identifier, identity, reason="client_request"): return await self.request("POST",f"/v1/jobs/{identifier}/cancel",{**identity,"reason":reason})
    async def events(self, after=0): return await self.request("GET","/v1/events",params={"after":after})

    async def upload(self, content, kind, mime_type, source_artifact_id=None):
        sha = hashlib.sha256(content).hexdigest()
        return await self.request("POST","/v1/blobs",{"sha256":sha,"kind":kind,"mime_type":mime_type,"data_base64":base64.b64encode(content).decode(),"source_artifact_id":source_artifact_id},idempotency_key=digest([sha,kind,mime_type,source_artifact_id]))

    async def import_bundle(self, source_store, bundle):
        """Upload the complete permitted artifact closure, preserving original ref IDs."""
        seen=set()
        async def walk(value):
            if isinstance(value,dict):
                for child in value.values(): await walk(child)
            elif isinstance(value,list):
                for child in value: await walk(child)
            elif isinstance(value,str) and value.startswith("artifact:") and value not in seen:
                seen.add(value)
                metadata,content=source_store.resolve_artifact(value,allowed_scopes=("actor",))
                await self.upload(content,metadata["kind"],metadata["mime_type"],metadata["artifact_id"])
                if metadata["mime_type"]=="application/json": await walk(json.loads(content))
        await walk(bundle)
        return await self.observation(bundle)

    async def wait(self, identifier, *, timeout_s=150, poll_s=0.1):
        deadline = asyncio.get_running_loop().time()+timeout_s
        while True:
            job = await self.job(identifier)
            if job["status"] in ("succeeded","failed","cancelled","expired"):
                return job
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"job still running; reconnect with job_id={identifier}")
            await asyncio.sleep(poll_s)

    async def replay(self, manifest_path, *, delay_s=0, duplicate=False, reverse=False):
        path = Path(manifest_path).resolve()
        manifest = json.loads(path.read_text())
        references = {}
        for metadata in manifest.get("artifacts",[]):
            file = (path.parent/metadata["storage_key"]).resolve()
            if not file.is_relative_to(path.parent):
                raise ValueError("replay_artifact_outside_package")
            content = file.read_bytes()
            if hashlib.sha256(content).hexdigest()!=metadata["sha256"] or len(content)!=metadata["byte_size"] or metadata.get("access_scope")!="actor":
                raise ValueError("replay_artifact_verification_failed")
            result = await self.upload(content,metadata["kind"],metadata["mime_type"],metadata["artifact_id"])
            references["artifact:"+metadata["artifact_id"]] = result["ref"]
        def replace(value):
            if isinstance(value,dict): return {key:replace(child) for key,child in value.items()}
            if isinstance(value,list): return [replace(child) for child in value]
            return references.get(value,value) if isinstance(value,str) else value
        observations = manifest.get("observations",[])
        if manifest.get("obs_jsonl"):
            file = (path.parent/manifest["obs_jsonl"]).resolve()
            if not file.is_relative_to(path.parent): raise ValueError("replay_observations_outside_package")
            observations += [json.loads(line) for line in file.read_text().splitlines() if line.strip()]
        responses = []
        for observation in reversed(observations) if reverse else observations:
            responses.append(await self.observation(replace(observation)))
            if duplicate: responses.append(await self.observation(replace(observation)))
            if delay_s: await asyncio.sleep(delay_s)
        for index,request in enumerate(manifest.get("requests",[])):
            response=await self.request("POST",request["path"],replace(request["body"]),idempotency_key=request.get("idempotency_key",f"replay-{digest(manifest)}-{index}"))
            if response.get("job_id") and request.get("wait_for_job",True):
                response=await self.wait(response["job_id"])
            responses.append(response)
        return {"observations":len(observations),"responses":responses,"source_digest":digest(manifest)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=["replay"])
    parser.add_argument("manifest")
    parser.add_argument("--url",default="http://127.0.0.1:8770")
    parser.add_argument("--delay-s",type=float,default=0)
    parser.add_argument("--duplicate",action="store_true")
    parser.add_argument("--reverse",action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(TwinClient(args.url).replay(args.manifest,delay_s=args.delay_s,duplicate=args.duplicate,reverse=args.reverse)),indent=2))

if __name__ == "__main__": main()
