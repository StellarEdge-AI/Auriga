"""features.object_table_observe / scene3d_observe: 3D scene information attached to robot_observe replies and to the
final observation of robot_action / robot_wait replies (text only there unless obs3d_action_overlay)
(fake VGGT service: up, down, error, timeout). python -m unittest discover -s tests -p test_obs3d.py"""
import base64
import hashlib
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from auriga.core.config import Config
from auriga.core.contracts import canonical
from auriga.perception.scene.store import SceneStore
from auriga.core.storage.store import Store
from auriga.core.tool_output import StructuredText
from auriga.runtime.tools import handlers
from auriga.runtime.tools.builtins import create_registry
from auriga.runtime.tools.definitions import public_definitions
from auriga.perception.external import obs3d, scene3d
from auriga.perception.external import scene3d_helpers as s3
from geometry_fixture import IDENTITY
from test_object_table import fake_object, two_camera_observation
from test_scene3d import TABLE, synthetic_cloud


class FakeService:
    """/object_table and /points of the VGGT service; status != 'ok' answers an error, delay_s sleeps first."""
    def __init__(self, status="ok", delay_s=0.0):
        self.requests, self.windows, owner = [], [], self
        objects = [fake_object((0.0, 0.0, 0.05), 0.10, 0.0),
                   fake_object((0.1, 0.05, 0.12), 0.14, 0.10, support=0, flags=("contact_mismatch",), label=(370, 215))]
        cloud = synthetic_cloud()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((self.path, body))
                started = time.monotonic()
                if delay_s:
                    time.sleep(delay_s)
                owner.windows.append((started, time.monotonic()))
                if status != "ok":
                    answer, code = dict(status="error", error=status), 422
                elif self.path == "/object_table":
                    answer, code = dict(status="ok", version="fake-ot", objects=objects[:body["max_objects"]],
                                        requested=[], omitted=max(0, len(objects) - body["max_objects"]),
                                        support_z_m=body["support_z_m"], timing=dict(total_s=0.01)), 200
                else:
                    answer, code = dict(status="ok", version="fake-points", n_points=3000, n_above_support=600, n_robot=25,
                                        voxel_m=body["voxel_m"], support_z_m=body["support_z_m"],
                                        bbox_m=[[-0.3, -0.3, 0.765], [0.3, 0.3, 1.055]],
                                        bbox_above_support_m=[[-0.12, -0.02, 0.77], [0.13, 0.12, 0.865]],
                                        npz_b64=base64.b64encode(cloud).decode(), timing=dict(total_s=0.01), cache_hit=True), 200
                raw = json.dumps(answer).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass  # the client gave up (timeout test)

        self.cloud = cloud
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def image_shape(part):
    data = base64.b64decode(part["imageUrl"].split(",", 1)[1])
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR).shape


class Obs3dCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.artifacts = Store(self.tmp.name, IDENTITY); self.addCleanup(self.artifacts.close)
        self.scene = SceneStore(Path(self.tmp.name) / "scene", self.artifacts.resolve_artifact, self.artifacts.put_artifact)
        self.bundle, self.images = two_camera_observation(self.artifacts)
        self.scene.import_observation(self.bundle)

    def runtime(self, ot=False, s3o=False, endpoint="http://127.0.0.1:9", timeout=5.0, rows=10, bundle=None,
                action_overlay=False, truth_log=False):
        config = Config().with_overrides({"features": {
            "external_perception": False,
            "object_table_observe": ot, "scene3d_observe": s3o, "vggt_endpoint": endpoint, "vggt_support_z_m": TABLE,
            "vggt_timeout_s": 60.0, "obs3d_timeout_s": timeout, "obs3d_max_objects": rows,
            "obs3d_action_overlay": action_overlay, "trajectory_preview": False},
            "analysis": {"scene_truth_log": truth_log}})
        bundle = self.bundle if bundle is None else bundle
        base = [StructuredText({"bundle": {"observation_id": bundle["observation_id"]}, "robot_state": {}}, "data"),
                *[dict(type="inputImage", imageUrl="data:image/png;base64," + base64.b64encode(self.images[c["camera_id"]]).decode())
                  for c in bundle["cameras"]]]
        return SimpleNamespace(config=config, scene=self.scene, image_settings=None, identity=dict(IDENTITY),
                               latest=None, active_action=None,  # main's robot_observe single-camera view path
                               store=self.artifacts, features=config.features, observe=lambda correlation: bundle,
                               observation_content=lambda *a, **k: list(base))

    def catalog_runtime(self, **kw):
        runtime = self.runtime(**kw)
        return SimpleNamespace(disabled_tools=(), observation_profile="rgb_proprio_calibrated_v1", motion_review=True,
                               enable_wait=True, vision_compare=True, visual_reviewer=True, action_policy=None,
                               capabilities=None, programs=True, config=runtime.config, features=runtime.config.features)

    def observe(self, runtime, correlation=None):
        return handlers.robot_observe(runtime, {}, correlation or {})

    def attached(self, response):
        extra = response.content[1 + len(self.bundle["cameras"]):]
        self.assertIsInstance(extra[0], StructuredText)
        return extra[0].value, extra[1:]

    # ------------------------------------------------------------------ switches off
    def test_off_reply_and_catalog_unchanged(self):
        runtime = self.runtime()
        response = self.observe(runtime)
        self.assertEqual(len(response.content), 1 + len(self.bundle["cameras"]))
        self.assertEqual(obs3d.flags(runtime), (False, False))
        self.assertEqual(canonical(public_definitions(observe3d=(False, False))), canonical(public_definitions()))
        digest = lambda rt: hashlib.sha256(canonical(create_registry(runtime=rt).catalog(rt, role="actor")).encode()).hexdigest()
        self.assertEqual(public_definitions(observe3d=(False, False, True)), public_definitions())
        self.assertEqual(digest(self.catalog_runtime()), hashlib.sha256(canonical(
            create_registry().catalog(self.catalog_runtime(), role="actor")).encode()).hexdigest())
        self.assertEqual(scene3d.workspace_modules(runtime), ())

    def test_catalog_changes_only_robot_observe_description(self):
        off = {d["name"]: d for d in public_definitions()}
        for flags in ((True, False), (False, True), (True, True), (True, False, True), (True, True, True)):
            on = {d["name"]: d for d in public_definitions(observe3d=flags)}
            for name in off:
                if name != "robot_observe":
                    self.assertEqual(canonical(on[name]), canonical(off[name]), name)
            added = obs3d.catalogue_sentence(*flags)
            self.assertEqual(on["robot_observe"]["description"].count(added), 1)
            self.assertEqual(on["robot_observe"]["description"].replace(added, "", 1), off["robot_observe"]["description"])
            self.assertLessEqual(len(added), 300)
            self.assertLessEqual(len(canonical(on["robot_observe"])) - len(canonical(off["robot_observe"])), 300)
            self.assertEqual(canonical(on["robot_observe"]["inputSchema"]), canonical(off["robot_observe"]["inputSchema"]))
        rt = self.catalog_runtime(ot=True, s3o=True)
        text = canonical(create_registry(runtime=rt).catalog(rt, role="actor"))
        self.assertIn(obs3d.catalogue_sentence(True, True), text)
        self.assertIn("robot_action / robot_wait", obs3d.catalogue_sentence(True, True))
        self.assertIn("robot_observe adds an id overlay", obs3d.catalogue_sentence(True, False))
        self.assertNotIn("overlay", obs3d.catalogue_sentence(False, True))
        rt = self.catalog_runtime(ot=True, action_overlay=True)
        self.assertIn(obs3d.catalogue_sentence(True, False, True),
                      canonical(create_registry(runtime=rt).catalog(rt, role="actor")))

    # ------------------------------------------------------------------ service up
    def test_object_table_attached_with_one_head_overlay(self):
        fake = FakeService(); self.addCleanup(fake.close)
        runtime = self.runtime(ot=True, endpoint=fake.url)
        value, images = self.attached(self.observe(runtime))
        self.assertEqual(value["observation_id"], self.bundle["observation_id"])
        table = value["object_table"]
        self.assertEqual(table["columns"][0], "id")
        self.assertNotIn("label", table["columns"])
        self.assertEqual([row[0] for row in table["rows"]], ["o1", "o2"])
        self.assertEqual(table["rows"][1][-2:], ["o1", "low:contact_mismatch"])
        self.assertEqual(table["table_z"], TABLE)
        self.assertNotIn("scene3d", value)
        self.assertEqual(len(images), 1)
        frame = cv2.imdecode(np.frombuffer(self.images["cam_head"], np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image_shape(images[0])[:2], frame.shape[:2])
        self.assertEqual([path for path, _ in fake.requests], ["/object_table"])
        sent = fake.requests[0][1]
        self.assertEqual(base64.b64decode(sent["image_b64"]), self.images["cam_head"])
        self.assertEqual(sent["max_objects"], 10)
        self.assertNotIn("labels", sent); self.assertNotIn("pixels", sent)
        self.assertLess(len(canonical(value)), 1500)

    def test_row_limit(self):
        fake = FakeService(); self.addCleanup(fake.close)
        value, _ = self.attached(self.observe(self.runtime(ot=True, endpoint=fake.url, rows=1)))
        self.assertEqual((len(value["object_table"]["rows"]), value["object_table"]["omitted"]), (1, 1))

    def test_scene3d_attached_without_image(self):
        fake = FakeService(); self.addCleanup(fake.close)
        runtime = self.runtime(s3o=True, endpoint=fake.url)
        self.assertEqual(scene3d.workspace_modules(runtime), ("scene3d_helpers.py",))
        value, images = self.attached(self.observe(runtime))
        self.assertEqual(images, [])
        cloud = value["scene3d"]
        self.assertEqual(set(cloud), {"artifact", "points", "table_z", "use"})
        self.assertEqual((cloud["points"], cloud["table_z"]), (3000, TABLE))
        info, data = self.artifacts.resolve_artifact(cloud["artifact"])
        self.assertEqual((info["kind"], data), ("scene3d_points", fake.cloud))
        self.assertIn("s3.API", cloud["use"])
        self.assertNotIn("object_table", value)
        self.assertEqual([path for path, _ in fake.requests], ["/points"])
        self.assertLess(len(canonical(value)), 400)

    def test_both(self):
        fake = FakeService(); self.addCleanup(fake.close)
        value, images = self.attached(self.observe(self.runtime(ot=True, s3o=True, endpoint=fake.url)))
        self.assertEqual(len(images), 1)
        self.assertIsInstance(value["object_table"], dict)
        self.assertIsInstance(value["scene3d"], dict)
        self.assertCountEqual([path for path, _ in fake.requests], ["/object_table", "/points"])
        bodies = [json.loads(body) for body, in self.artifacts.db.execute("SELECT body FROM events")]
        attached = [b for b in bodies if "geometry.obs3d_attached" in json.dumps(b)]
        self.assertEqual(len(attached), 1)
        self.assertIn("latency_s", json.dumps(attached[0]))

    # ------------------------------------------------------------------ service down / error / timeout
    def test_service_down(self):
        response = self.observe(self.runtime(ot=True, s3o=True))
        value, images = self.attached(response)
        self.assertEqual(value["object_table"], "unavailable: service_down")
        self.assertEqual(value["scene3d"], "unavailable: service_down")
        self.assertEqual(images, [])
        self.assertEqual(response.data, self.bundle)

    def test_service_error(self):
        fake = FakeService(status="no_horizontal_support_plane"); self.addCleanup(fake.close)
        value, images = self.attached(self.observe(self.runtime(ot=True, s3o=True, endpoint=fake.url)))
        self.assertEqual(value["object_table"], "unavailable: no_horizontal_support_plane")
        self.assertEqual(value["scene3d"], "unavailable: no_horizontal_support_plane")
        self.assertEqual(images, [])

    def test_service_timeout(self):
        fake = FakeService(delay_s=2.5); self.addCleanup(fake.close)
        started = time.monotonic()
        value, images = self.attached(self.observe(self.runtime(ot=True, endpoint=fake.url, timeout=1.0)))
        self.assertLess(time.monotonic() - started, 2.4)
        self.assertEqual(value["object_table"], "unavailable: timeout")
        self.assertEqual(images, [])

    # ------------------------------------------------------------------ scope
    def test_program_callers_and_no_head_camera_get_nothing(self):
        fake = FakeService(); self.addCleanup(fake.close)
        response = self.observe(self.runtime(ot=True, s3o=True, endpoint=fake.url), {"program_id": "job_1"})
        self.assertEqual(len(response.content), 1 + len(self.bundle["cameras"]))
        side_only = dict(self.bundle, cameras=[c for c in self.bundle["cameras"] if c["camera_id"] != "cam_head"])
        response = self.observe(self.runtime(ot=True, s3o=True, endpoint=fake.url, bundle=side_only))
        self.assertEqual(len(response.content), 2)
        self.assertEqual(fake.requests, [])

    def test_helper_api_matches_tool_api(self):
        self.assertEqual(list(s3.API), list(scene3d.API))

    def test_request_projection_treats_parts_like_other_output(self):
        from auriga.core.presentation.actor_output import ActorOutput
        from auriga.core.presentation.actor_presentation import ActorPresentation
        from auriga.core.presentation.lean_projection import _project_output
        fake = FakeService(); self.addCleanup(fake.close)
        response = self.observe(self.runtime(ot=True, s3o=True, endpoint=fake.url))
        shown = ActorOutput(self.artifacts, ActorPresentation(self.artifacts), compact=True).present(response, tool="robot_observe")
        items = shown["contentItems"]
        text = json.loads(items[1 + len(self.bundle["cameras"])]["text"])
        self.assertTrue(text["scene3d"]["artifact"].startswith("artifact:ref_"))
        output = [dict(type="input_text", text=i["text"]) if i["type"] == "inputText" else
                  dict(type="input_image", image_url=i["imageUrl"]) for i in items]
        projected, _, images = _project_output(dict(type="function_call_output", call_id="c", output=output), "robot_observe", {})
        self.assertEqual(len(images), len(self.bundle["cameras"]) + 1)
        parts = projected["output"]
        self.assertTrue(all(p["type"] == "input_text" for p in parts))
        summary = parts[1 + len(self.bundle["cameras"])]["text"]
        self.assertTrue(summary.startswith("[summary robot_observe]"), summary)
        self.assertLess(len(summary), 120)
        self.assertNotIn("o1", summary)


    # ------------------------------------------------------------------ robot_action / robot_wait replies
    def action_runtime(self, **kw):
        runtime = self.runtime(**kw)
        self.artifacts.save("observation", self.bundle["observation_id"], self.bundle)
        runtime.action_budget = lambda: {"native_steps_remaining": 200}
        runtime.depth_auto_feedback, runtime.actor_payload_profile = False, "full_v1"
        runtime._scheduling_advisory = lambda *a, **k: None
        return runtime

    def action_reply(self, runtime, correlation=None):
        from auriga.runtime.actions import unified_actions
        receipt = {"status": "completed", "executed_native_steps": 40, "result_observation_id": self.bundle["observation_id"]}
        record = {"summary": {"planned_native_steps": 40, "next_action": {"mode": "inspect", "plan_id": "plan_1"}}}
        return unified_actions._action_response(runtime, {"plan_id": "plan_1", "action_id": "act_1", "receipt": receipt},
                                                {"type": "ArmQueueAction", "action_id": "act_1"}, record, correlation or {})

    def wait_reply(self, runtime, correlation=None, obs3d_on=True):
        from auriga.runtime.tools import actions
        result = {"request": {"type": "WaitAction", "action_id": "act_w"},
                  "receipt": {"status": "completed", "executed_native_steps": 10,
                              "result_observation_id": self.bundle["observation_id"]}}
        return actions._action_response(runtime, result, correlation or {}, obs3d=obs3d_on)

    @staticmethod
    def obs3d_parts(response):
        """(obs3d text values, images after the first obs3d part, all images) of one reply."""
        content = response.content
        index = [k for k, part in enumerate(content) if isinstance(part, StructuredText) and isinstance(part.value, dict)
                 and set(part.value) & {"object_table", "scene3d"}]
        images = [part for part in content if isinstance(part, dict) and part.get("type") == "inputImage"]
        after = ([part for part in content[index[0]:] if isinstance(part, dict) and part.get("type") == "inputImage"]
                 if index else [])
        return [content[k].value for k in index], after, images

    def test_action_and_wait_replies_carry_text_without_image(self):
        for reply in (self.action_reply, self.wait_reply):
            fake = FakeService(); self.addCleanup(fake.close)
            runtime = self.action_runtime(ot=True, s3o=True, endpoint=fake.url)
            response = reply(runtime)
            values, after, images = self.obs3d_parts(response)
            self.assertEqual(len(values), 1, reply.__name__)
            self.assertEqual([row[0] for row in values[0]["object_table"]["rows"]], ["o1", "o2"])
            self.assertEqual(values[0]["scene3d"]["points"], 3000)
            self.assertEqual(after, [])
            self.assertEqual(len(images), len(self.bundle["cameras"]))  # the observation's own frames only
            self.assertEqual(response.content[-1].value, values[0])
            self.assertEqual(response.data["receipt"]["result_observation_id"], self.bundle["observation_id"])
            self.assertCountEqual([path for path, _ in fake.requests], ["/object_table", "/points"])
            bodies = [json.loads(body) for body, in self.artifacts.db.execute("SELECT body FROM events")]
            self.assertIn('"reply": "action"', json.dumps([b for b in bodies if "obs3d_attached" in json.dumps(b)][-1]))

    # ------------------------------------------------------------------ analysis.scene_truth_log: offline-scoring event
    def analysis_events(self):
        return [e for e in self.artifacts.events_by_type([obs3d.ANALYSIS_EVENT], limit=100)]

    def test_analysis_event_only_with_truth_log_and_reply_unchanged(self):
        fake = FakeService(); self.addCleanup(fake.close)
        off = self.action_runtime(ot=True, endpoint=fake.url)
        off_values = [self.obs3d_parts(self.action_reply(off))[0], self.obs3d_parts(self.observe(off))[0]]
        self.assertEqual(self.analysis_events(), [])
        on = self.action_runtime(ot=True, endpoint=fake.url, truth_log=True)
        action_reply, observe_reply = self.action_reply(on), self.observe(on)
        on_values = [self.obs3d_parts(action_reply)[0], self.obs3d_parts(observe_reply)[0]]
        self.assertEqual(canonical(on_values), canonical(off_values))           # model-facing parts identical
        self.assertNotIn(obs3d.ANALYSIS_EVENT, canonical([p.value if isinstance(p, StructuredText) else p
                                                          for r in (action_reply, observe_reply) for p in r.content]))
        events = self.analysis_events()
        self.assertEqual([(e["payload"]["reply"], e["payload"]["reused"]) for e in events], [("action", False), ("observe", True)])
        head = next(c["frame_id"] for c in self.bundle["cameras"] if c["camera_id"] == "cam_head")
        for event, value in zip(events, on_values):
            payload = event["payload"]
            self.assertEqual(event["world_kind"], "internal")
            self.assertEqual((payload["observation_id"], payload["frame_ids"], payload["available"]),
                             (self.bundle["observation_id"], [head], True))
            self.assertEqual(canonical([payload["columns"], payload["rows"], payload["table_z"]]),
                             canonical([value[0]["object_table"][k] for k in ("columns", "rows", "table_z")]))
        self.assertEqual(len([p for p, _ in fake.requests if p == "/object_table"]), 2)   # one per runtime (memo reuse)

    def test_analysis_event_records_unavailable_table(self):
        self.observe(self.runtime(ot=True, truth_log=True))
        (event,) = self.analysis_events()
        self.assertEqual((event["payload"]["available"], event["payload"]["unavailable"]), (False, "unavailable: service_down"))
        self.assertNotIn("rows", event["payload"])

    def test_observe_reply_has_text_and_overlay_and_reuses_action_result(self):
        fake = FakeService(); self.addCleanup(fake.close)
        runtime = self.action_runtime(ot=True, s3o=True, endpoint=fake.url)
        action_values, action_after, _ = self.obs3d_parts(self.action_reply(runtime))
        observe_values, observe_after, _ = self.obs3d_parts(self.observe(runtime))
        self.assertEqual((len(action_after), len(observe_after)), (0, 1))
        self.assertEqual(observe_values, action_values)  # same rows, same artifact handle
        self.assertCountEqual([path for path, _ in fake.requests], ["/object_table", "/points"])  # one call per kind

    def test_action_overlay_knob(self):
        fake = FakeService(); self.addCleanup(fake.close)
        runtime = self.action_runtime(ot=True, endpoint=fake.url, action_overlay=True)
        _, after, _ = self.obs3d_parts(self.action_reply(runtime))
        self.assertEqual(len(after), 1)
        frame = cv2.imdecode(np.frombuffer(self.images["cam_head"], np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image_shape(after[0])[:2], frame.shape[:2])

    def test_action_reply_off_service_down_and_program(self):
        base = len(self.action_reply(self.action_runtime()).content)
        self.assertEqual(self.obs3d_parts(self.action_reply(self.action_runtime()))[0], [])
        response = self.action_reply(self.action_runtime(ot=True, s3o=True))
        values, after, _ = self.obs3d_parts(response)
        self.assertEqual((values[0]["object_table"], values[0]["scene3d"]), ("unavailable: service_down",) * 2)
        self.assertEqual((len(response.content), after), (base + 1, []))
        self.assertEqual(response.data["receipt"]["status"], "completed")
        fake = FakeService(); self.addCleanup(fake.close)
        runtime = self.action_runtime(ot=True, s3o=True, endpoint=fake.url)
        self.assertEqual(self.obs3d_parts(self.action_reply(runtime, {"program_id": "job_1"}))[0], [])
        self.assertEqual(self.obs3d_parts(self.wait_reply(runtime, {"program_id": "job_1"}))[0], [])
        self.assertEqual(self.obs3d_parts(self.wait_reply(runtime, obs3d_on=False))[0], [])  # other execute_action tools
        self.assertEqual(fake.requests, [])

    def test_roi_view_gets_nothing(self):
        fake = FakeService(); self.addCleanup(fake.close)
        response = handlers.robot_observe(self.runtime(ot=True, s3o=True, endpoint=fake.url),
                                          {"camera_id": "cam_head", "roi": [0, 0, 100, 100], "scale": 2}, {})
        self.assertEqual(len(response.content), 1 + len(self.bundle["cameras"]))
        self.assertEqual(fake.requests, [])

    def test_request_projection_of_action_reply(self):
        from auriga.core.presentation.actor_output import ActorOutput
        from auriga.core.presentation.actor_presentation import ActorPresentation
        from auriga.core.presentation.lean_projection import _project_output
        fake = FakeService(); self.addCleanup(fake.close)
        response = self.action_reply(self.action_runtime(ot=True, s3o=True, endpoint=fake.url))
        shown = ActorOutput(self.artifacts, ActorPresentation(self.artifacts), compact=True).present(response, tool="robot_action")
        items = shown["contentItems"]
        self.assertEqual(sum(i["type"] == "inputImage" for i in items), len(self.bundle["cameras"]))
        full = json.loads(items[-1]["text"])
        self.assertEqual(full["object_table"]["rows"][0][0], "o1")
        output = [dict(type="input_text", text=i["text"]) if i["type"] == "inputText" else
                  dict(type="input_image", image_url=i["imageUrl"]) for i in items]
        projected, texts, images = _project_output(dict(type="function_call_output", call_id="c", output=output),
                                                   "robot_action", {"mode": "execute", "plan_id": "plan_1"})
        self.assertEqual(len(images), len(self.bundle["cameras"]))
        summary = projected["output"][-1]["text"]
        self.assertTrue(summary.startswith("[summary robot_action] "), summary)
        self.assertEqual(json.loads(summary.split("] ", 1)[1]),
                         {"obs3d_of": self.bundle["observation_id"], "object_rows": 2, "scene3d": "stored"})
        self.assertIn(items[-1]["text"], [part["text"] for part in texts])  # full text kept for the open block / anchors

    def test_program_final_observation_attaches_obs3d_and_action_evidence_parity(self):
        from auriga.runtime.programs.program_review import action_evidence
        from auriga.runtime.tools.program_tools import program_content
        from auriga.core.tool_output import ToolResponse
        fake = FakeService(); self.addCleanup(fake.close)
        runtime = self.action_runtime(ot=True, s3o=True, endpoint=fake.url)
        runtime._model_context_epoch = lambda: "epoch-1"
        runtime._program_output_delivery = SimpleNamespace(
            project=lambda result, **kw: result,
            invalidate=lambda: None,
        )
        # 1) Cache before-observation object_table so object_state_delta can be computed
        before_bundle = dict(self.bundle, observation_id="obs-before")
        self.artifacts.save("observation", "obs-before", before_bundle)
        self.observe(runtime)
        obs3d._remember(
            runtime,
            "obs-before",
            "object_table",
            (
                {
                    "columns": ["id", "centre_m", "top_z", "bottom_z", "length", "width", "yaw_deg", "tilt_deg", "support", "quality"],
                    "rows": [["o1", [0.10, -0.20, 0.78], 0.80, 0.76, 0.08, 0.04, 15, 4, "table", "ok"]],
                    "table_z": TABLE,
                },
                b"",
                {},
            ),
        )
        # 2) Verify program_id _action_response data convenience fields + action_evidence parity
        prog_resp = self.action_reply(runtime, {"program_id": "job_1"})
        self.assertEqual(prog_resp.data["status"], "completed")
        self.assertEqual(prog_resp.data["observation_id"], self.bundle["observation_id"])
        self.assertEqual(prog_resp.data["result_observation_id"], self.bundle["observation_id"])
        self.assertEqual(prog_resp.data["stages"], {})
        stopped_data = {
            "plan_id": "plan_1",
            "action_id": "act_1",
            "request": {"action_id": "act_1", "based_on_observation_id": "obs-before"},
            "receipt": {
                "action_id": "act_1",
                "status": "stopped",
                "reason": "tracking_error",
                "failed_stage_id": "grasp",
                "result_observation_id": self.bundle["observation_id"],
                "executed_native_steps": 12,
            },
            "repair_action": {"mode": "direct", "plan_id": "plan_1", "observation_id": self.bundle["observation_id"]},
            "episode_ledger": {"contact_object_3d": {"id": "o1"}},
        }
        ev = action_evidence(
            self.artifacts,
            "robot_action",
            {"mode": "direct", "observation_id": "obs-before"},
            ToolResponse([], data=stopped_data, kind="action"),
            "call_1",
            physical=True,
        )
        self.assertEqual(ev["based_on_observation_id"], "obs-before")
        self.assertEqual(ev["repair_action"]["plan_id"], "plan_1")
        self.assertEqual(ev["episode_ledger"]["contact_object_3d"]["id"], "o1")
        # 3) Verify program_content attaches obs3d (object_table, scene3d, object_state_delta) on final_observation_id
        job = {
            "job_id": "program-1",
            "status": "succeeded",
            "final_observation_id": self.bundle["observation_id"],
            "action_summary": {"action_calls": 1, "recent_actions": [ev]},
        }
        parts = program_content(runtime, job, dict(job), "summary", {})
        obs3d_items = [
            p.value for p in parts
            if isinstance(p, StructuredText) and isinstance(p.value, dict) and "object_table" in p.value
        ]
        self.assertEqual(len(obs3d_items), 1)
        self.assertEqual([r[0] for r in obs3d_items[0]["object_table"]["rows"]], ["o1", "o2"])
        self.assertEqual(obs3d_items[0]["scene3d"]["points"], 3000)
        self.assertIn("object_state_delta", obs3d_items[0])

    def test_concurrent_object_table_and_scene3d_requests_overlap_in_flight(self):
        fake = FakeService(delay_s=0.12)
        self.addCleanup(fake.close)
        runtime = self.runtime(ot=True, s3o=True, endpoint=fake.url)
        parts = obs3d.attachments(runtime, self.bundle, {})
        self.assertCountEqual([path for path, _ in fake.requests], ["/object_table", "/points"])
        (first_start, first_end), (second_start, second_end) = fake.windows
        self.assertLess(max(first_start, second_start), min(first_end, second_end))
        self.assertEqual(parts[0].value["scene3d"]["points"], 3000)
        self.assertEqual(len(parts[0].value["object_table"]["rows"]), 2)


if __name__ == "__main__":
    unittest.main()
