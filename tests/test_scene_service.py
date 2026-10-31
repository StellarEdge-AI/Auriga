"""Real loopback HTTP, bounded child jobs, cancellation, expiry, restart and replay."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import web, ClientSession

from auriga.core.contracts import canonical,digest
from auriga.perception.scene.scene_client import TwinClient
from auriga.perception.scene.scene_service import SceneService
from geometry_fixture import IDENTITY,recorded_plane


class ServiceCheck(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = SceneService(self.tmp.name)
        self.obs = recorded_plane(self.service.store)
        self.runner = web.AppRunner(self.service.app()); await self.runner.setup()
        self.site = web.TCPSite(self.runner,"127.0.0.1",0); await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.client = TwinClient(f"http://127.0.0.1:{port}")
        for observation in self.obs: await self.client.observation(observation)
        self.request = {"schema_version":"0.1",**IDENTITY,"observation_ids":["obs-0","obs-1"],"camera_id":"head","static_scene_assumption":True,"base_scene_version":0,"roi":[100,80,400,320]}

    async def asyncTearDown(self):
        await self.runner.cleanup(); self.tmp.cleanup()

    async def test_http_job_idempotency_snapshot_events(self):
        specification=await self.client.request("GET","/v1/openapi.json")
        self.assertEqual(specification["openapi"],"3.1.0")
        self.assertIn("post",specification["paths"]["/v1/scene/update-jobs"])
        self.assertTrue(specification["paths"]["/v1/scene/query"]["post"]["parameters"][0]["required"])
        created = await self.client.update(self.request,"once")
        repeated = await self.client.update(self.request,"once")
        self.assertEqual(created["job_id"],repeated["job_id"])
        with self.assertRaisesRegex(ValueError,"409"):
            await self.client.update({**self.request,"roi":[120,80,400,320]},"once")
        complete = await self.client.wait(created["job_id"])
        self.assertEqual(complete["status"],"succeeded",complete)
        self.assertEqual(complete["result"]["scene"]["scene_version"],1)
        snapshot = await self.client.snapshot({**IDENTITY,"scene_version":1,"anchor_observation_id":"obs-1"})
        self.assertEqual(snapshot["quality"]["artifact_resolution"],"verified_digest_closed")
        all_events = (await self.client.events())["events"]
        events = [item for item in all_events if item["type"].startswith("job.")]
        self.assertEqual([event["type"] for event in events],["job.queued","job.started","job.completed"])
        self.assertTrue(all("server_seq" not in event for event in events))
        self.assertTrue(all(event["run_id"]==IDENTITY["run_id"] for event in events))
        self.assertEqual(len({event["event_id"] for event in events}),3)
        self.assertIn("scene.updated",[item["type"] for item in all_events])
        self.assertIn("snapshot.created",[item["type"] for item in all_events])
        self.assertFalse((await self.client.capabilities())["worker_ready"])

    async def test_cancel_budget_and_restart(self):
        created = await self.client.update(self.request,"cancel")
        cancelled = await self.client.cancel(created["job_id"],IDENTITY)
        self.assertTrue(cancelled["cancel_requested"])
        terminal = await self.client.wait(created["job_id"])
        self.assertEqual(terminal["status"],"cancelled")
        self.assertTrue(terminal["stop_confirmed"])
        created = await self.client.update({**self.request,"budget":{"wall_time_s":0.001,"queue_time_s":0}},"budget")
        terminal = await self.client.wait(created["job_id"])
        self.assertIn(terminal["status"],("failed","expired"))
        self.assertIn(terminal["error"],("budget_exceeded","deadline_exceeded"))
        created = self.service.submit(self.request,"build_scene","recovery-test")
        self.service.tasks[created["job_id"]].cancel()
        await asyncio.gather(self.service.tasks[created["job_id"]],return_exceptions=True)
        self.service.transition(created,"running",attempt_token="old-worker")
        queued=self.service.submit(self.request,"build_scene","resume-queued")
        self.service.tasks[queued["job_id"]].cancel()
        await asyncio.gather(self.service.tasks[queued["job_id"]],return_exceptions=True)
        crash_jobs=[self.service.job("recovery-test"),self.service.job("resume-queued")]
        await self.runner.cleanup()
        self.service = SceneService(self.tmp.name)
        # Restore the durable rows a hard crash would have left, without invoking cleanup.
        with self.service.db:
            for crashed in crash_jobs:
                self.service.db.execute("INSERT OR REPLACE INTO scene_jobs VALUES (?,?)",(crashed["job_id"],canonical(crashed)))
        self.runner = web.AppRunner(self.service.app()); await self.runner.setup()
        job = self.service.job("recovery-test")
        self.assertEqual(job["status"],"failed")
        self.assertEqual(job["error"],"worker_lost_on_restart")
        await self.service.tasks["resume-queued"]
        self.assertEqual(self.service.job("resume-queued")["status"],"succeeded")

    async def test_worker_crash_and_queue_deadline_are_terminal(self):
        created=self.service.submit(self.request,"build_scene","kill-owned-worker")
        await asyncio.sleep(0)
        process=self.service.processes[created["job_id"]]
        self.assertTrue(process.is_alive())
        process.kill()
        failed=await self.client.wait(created["job_id"])
        self.assertEqual(failed["status"],"failed")
        self.assertEqual(failed["error"],"worker_crashed")
        await self.service.semaphore.acquire(); await self.service.semaphore.acquire()
        queued=self.service.submit({**self.request,"budget":{"queue_time_s":0.01,"wall_time_s":30}},"build_scene","queue-expiry")
        await asyncio.sleep(0.03)
        self.service.semaphore.release(); self.service.semaphore.release()
        expired=await self.client.wait(queued["job_id"])
        self.assertEqual(expired["status"],"expired")
        self.assertEqual(expired["error"],"deadline_exceeded")
        running=self.service.submit({**self.request,"budget":{"queue_time_s":30,"wall_time_s":0.001}},"build_scene","running-budget")
        failed=await self.client.wait(running["job_id"])
        self.assertEqual(failed["status"],"failed")
        self.assertEqual(failed["error"],"budget_exceeded")

    async def test_graceful_shutdown_cancels_running_and_queued(self):
        await self.service.semaphore.acquire()
        running=self.service.submit(self.request,"build_scene","shutdown-running")
        await asyncio.sleep(0)
        process=self.service.processes[running["job_id"]]
        self.assertTrue(process.is_alive())
        queued=self.service.submit(self.request,"build_scene","shutdown-queued")
        await asyncio.sleep(0)
        self.assertEqual(self.service.job(queued["job_id"])["status"],"queued")
        await self.service.stop_jobs()
        self.assertFalse(process.is_alive())
        self.assertFalse(self.service.processes)
        for identifier in (running["job_id"],queued["job_id"]):
            job=self.service.job(identifier)
            self.assertEqual(job["status"],"cancelled")
            self.assertTrue(job["cancel_requested"])
            self.assertTrue(job["stop_confirmed"])
            events=[json.loads(row[0]) for row in self.service.db.execute("SELECT body FROM events")]
            types=[event["type"] for event in events if event.get("correlation",{}).get("job_id")==identifier]
            self.assertEqual(types[-2:],["job.cancel_requested","job.cancelled"])
        with self.assertRaises(web.HTTPServiceUnavailable):
            self.service.submit(self.request,"build_scene")
        await self.runner.cleanup()
        await self.service.close()
        self.service=SceneService(self.tmp.name)
        self.runner=web.AppRunner(self.service.app()); await self.runner.setup()
        self.assertEqual(self.service.job(running["job_id"])["status"],"cancelled")
        self.assertEqual(self.service.job(queued["job_id"])["status"],"cancelled")
        self.assertFalse(self.service.tasks)

    async def test_upload_replay_and_unknown_generation(self):
        artifact = await self.client.upload(b"checked", "text","text/plain")
        self.assertEqual(self.service.store.resolve_artifact(artifact["ref"])[1],b"checked")
        original = deepcopy(self.obs[0]); original["env_generation"]=2
        await self.client.observation(original)
        self.assertEqual((await self.client.head({**IDENTITY,"env_generation":2}))["scene_version"],0)
        manifest = {"artifacts":[],"observations":[self.obs[0]]}
        path = Path(self.tmp.name)/"replay.json"; path.write_text(canonical(manifest))
        replay = await self.client.replay(path,duplicate=True,reverse=True)
        self.assertEqual(replay["observations"],1)
        self.assertEqual(replay["responses"][0],replay["responses"][1])

    async def test_invalid_http_inputs_are_structured_and_never_admitted(self):
        before = self.service.store.event_cursor()
        invalid = [
            ("scene/update-jobs", {**self.request, "camera_id": False}),
            ("scene/update-jobs", {**self.request, "unknown_field": True}),
            ("scene/query", {**IDENTITY, "kind": "distance", "entity_id": "box", "point_m": [0, 0, 0]}),
            ("observations", {**self.obs[0], "observation_profile": "typo"}),
            ("rollout-jobs", {"world_kind": "observed", "snapshot_id": "none"}),
        ]
        async with ClientSession() as session:
            for route, body in invalid:
                with self.subTest(route=route, body=body):
                    async with session.post(self.client.url + "/v1/" + route, json=body,
                                            headers={"Idempotency-Key": "bad-" + route}) as response:
                        self.assertEqual(response.status, 422)
                        error = await response.json()
                        self.assertEqual(error["code"], "invalid_scene_request")
                        self.assertIn("field_path", error)
                        self.assertIn("error", error)  # Existing clients retain their string error.
            async with session.get(self.client.url + "/v1/events?after=-1") as response:
                self.assertEqual(response.status, 422)
                self.assertEqual((await response.json())["code"], "invalid_event_cursor")
            async with session.post(self.client.url + "/v1/rollout-jobs", json={"snapshot_id": "none"},
                                    headers={"Idempotency-Key": "unsupported"}) as response:
                self.assertEqual(response.status, 422)
                self.assertFalse((await response.json())["executed"])
        self.assertEqual(self.service.db.execute("SELECT COUNT(*) FROM scene_jobs").fetchone()[0], 0)
        self.assertEqual(self.service.store.event_cursor(), before)
        self.assertFalse(self.service.processes)

    async def test_http_geometry_values_and_response_boundary(self):
        base = dict(IDENTITY, observation_id="obs-0", camera_id="head")
        for kind, extra in (("camera_pose", {}), ("fk", {}), ("pixel_ray", {"pixel": [320, 240]})):
            response = await self.client.geometry(dict(base, kind=kind, **extra))
            if kind == "fk":
                self.assertEqual(response["status"], "unknown")
                self.assertEqual(response["reason"], "missing_robot_link_calibration")
                continue
            self.assertEqual(response["status"], "supported")
            self.assertIsInstance(response["value"], dict)
        projected = await self.client.geometry(dict(base, kind="project_points", points_m=[[0, 0, 2]]))
        finished = await self.client.wait(projected["job_id"])
        self.assertEqual(finished["status"], "succeeded", finished)
        self.assertEqual(finished["result"]["value"]["pixels"], [[320., 240.]])
        # A broken producer must not publish a valid-looking capabilities reply.
        original = self.service.scene.capabilities
        self.service.scene.capabilities = lambda: {**original(), "observed_execution": "enabled"}
        async with ClientSession() as session:
            async with session.get(self.client.url + "/v1/capabilities") as response:
                self.assertEqual(response.status, 500)
                self.assertEqual((await response.json())["code"], "invalid_scene_response")

if __name__=="__main__": unittest.main()
