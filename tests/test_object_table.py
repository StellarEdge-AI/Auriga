"""features.object_table: geometry_query kind object_table against a fake local service.
python -m unittest discover -s tests -p test_object_table.py"""
import base64
import hashlib
import json
import tempfile
import threading
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
from auriga.runtime.tools import handlers
from auriga.runtime.tools.builtins import create_registry
from auriga.runtime.tools.definitions import public_definitions
from auriga.perception.external import object_table
from geometry_fixture import IDENTITY

K = [[500., 0, 320.], [0, 500., 240.], [0, 0, 1.]]
# head: 1 m above the plane z=0 looking straight down; side: 0.5 m to +x, same orientation
T_HEAD = [[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 1.0], [0, 0, 0, 1]]
T_SIDE = [[1, 0, 0, 0.5], [0, -1, 0, 0], [0, 0, -1, 1.0], [0, 0, 0, 1]]


def two_camera_observation(store):
    rng = np.random.default_rng(3)
    images = {}
    for name in ("cam_head", "cam_side"):
        image = cv2.GaussianBlur(rng.integers(0, 256, (480, 640, 3), dtype=np.uint8), (5, 5), 0)
        images[name] = cv2.imencode(".png", image)[1].tobytes()
    calibration = {"reference_frame_id": "env_origin", "cameras": {name: {"K": K, "distortion": {"model": "opencv", "coefficients": []},
                   "image_size": [640, 480], "T_world_camera": t} for name, t in (("cam_head", T_HEAD), ("cam_side", T_SIDE))}}
    calibration_ref = store.put_artifact(canonical(calibration).encode(), "calibration", "application/json")
    model_ref = store.put_artifact(canonical({"base_link": "base", "joints": []}).encode(), "robot_model", "application/json")
    state_ref = store.put_artifact(canonical({"source_kind": "measurement", "sim_step": 0, "step_domain": "control_step"}).encode(),
                                   "robot_state", "application/json")
    cameras = [{"camera_id": name, "frame_id": "frame-" + name, "width": 640, "height": 480, "sim_step": 0,
                "step_domain": "control_step", "rgb_ref": store.put_artifact(images[name], "rgb", "image/png"),
                "calibration_ref": calibration_ref, "pixel_transform": "identity"} for name in ("cam_head", "cam_side")]
    return {"schema_version": "0.1", **IDENTITY, "world_kind": "observed", "data_origin": "synthetic_fixture",
            "observation_id": "obs-ot", "sim_step": 0, "step_domain": "control_step", "clock_domain": "synthetic/generation-1",
            "calibration_id": "synthetic-calibration-v1", "robot_model_id": "synthetic-camera-rig", "robot_model_ref": model_ref,
            "observation_profile": "rgb_proprio_calibrated_synthetic_v1", "robot_state_ref": state_ref, "cameras": cameras}, images


def fake_object(centre, top, bottom, support="table", flags=(), elongation=3.0, label=(320, 240)):
    u, v = label
    return dict(centre_m=list(centre), top_z_m=top, bottom_z_m=bottom, length_m=0.1, width_m=0.03, yaw_deg=30.0,
                axis_tilt_deg=88.0, elongation=elongation, support=support, flags=list(flags),
                quality="low" if flags else "ok", label_px=[u, v], contour_px=[[u - 10, v - 5], [u + 10, v - 5], [u + 10, v + 5], [u - 10, v + 5]],
                mask_px=200, n_points=150, iou_pred=0.9, conf_rank_p50=0.5, contact_gap_m=0.0, cluster_frac=1.0)


class FakeService:
    def __init__(self, objects=None, status="ok"):
        self.requests, owner = [], self
        objects = [fake_object((0.0, 0.0, 0.05), 0.10, 0.0),
                   fake_object((0.1, 0.05, 0.12), 0.14, 0.10, support=0, flags=("contact_mismatch",), elongation=1.2,
                               label=(370, 215))] if objects is None else objects

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((self.path, body))
                if status != "ok":
                    answer, code = dict(status="error", error=status), 422
                else:
                    requested = [dict(pixel=p, status="ok", index=0) for p in body.get("pixels", [])]
                    labels = [dict(label=p, status="ok" if k == 0 else "no_match", indices=[1] if k == 0 else [])
                              for k, p in enumerate(body.get("labels", []))]
                    for k, item in enumerate(objects):
                        item["label"] = body["labels"][0] if labels and k == 1 else None
                    answer, code = dict(status="ok", version="fake-ot", objects=objects, requested=requested, omitted=0,
                                        labels=labels,
                                        support_z_m=body["support_z_m"], dropped={"robot": 1}, n_prompts=10,
                                        timing=dict(total_s=0.01), support_fit=dict(tilt_deg=0.5), scale=1.0), 200
                data = json.dumps(answer).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _branch(definition, kind):
    return next((b for b in definition["inputSchema"]["properties"]["request"]["oneOf"]
                 if b["properties"]["kind"].get("const") == kind), None)


class ObjectTableCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.artifacts = Store(self.tmp.name, IDENTITY); self.addCleanup(self.artifacts.close)
        self.scene = SceneStore(Path(self.tmp.name) / "scene", self.artifacts.resolve_artifact, self.artifacts.put_artifact)
        observation, self.images = two_camera_observation(self.artifacts)
        self.scene.import_observation(observation)

    def runtime(self, on=True, endpoint="http://127.0.0.1:9", vggt=False, mode="default"):
        config = Config().with_overrides({"features": {"external_perception": False, "object_table": on, "vggt_endpoint": endpoint, "vggt_support_z_m": 0.0,
                                                       "vggt_timeout_s": 5.0, "vggt_height": vggt, "vggt_height_mode": mode}})
        return SimpleNamespace(config=config, scene=self.scene, image_settings=None, identity=dict(IDENTITY), store=self.artifacts,
                               features=config.features)

    def catalog_runtime(self, **kw):
        runtime = self.runtime(**kw)
        return SimpleNamespace(disabled_tools=(), observation_profile="rgb_proprio_calibrated_v1", motion_review=True,
                               enable_wait=True, vision_compare=True, visual_reviewer=True, action_policy=None,
                               capabilities=None, programs=True, config=runtime.config, features=runtime.config.features)

    def request(self, **changes):
        return dict(kind="object_table", observation_id="obs-ot") | changes

    # ------------------------------------------------------------ switch off: identical contract and behaviour
    def test_off_definitions_and_catalog_are_byte_identical(self):
        base = canonical(public_definitions())
        self.assertEqual(canonical(public_definitions(object_table=False)), base)
        self.assertNotIn("object_table", base)
        off = create_registry(runtime=self.runtime(on=False))
        self.assertEqual(canonical([s.definition for s in off._specs.values()]),
                         canonical([s.definition for s in create_registry()._specs.values()]))
        digest = lambda runtime: hashlib.sha256(canonical(create_registry(runtime=runtime).catalog(runtime, role="actor")).encode()).hexdigest()
        self.assertEqual(digest(self.catalog_runtime(on=False)), hashlib.sha256(canonical(
            create_registry().catalog(self.catalog_runtime(on=False), role="actor")).encode()).hexdigest())
        # vggt default mode alone: unchanged by this feature (same digest as before the object_table branch existed)
        vggt_only = self.catalog_runtime(on=False, vggt=True)
        self.assertEqual(canonical(create_registry(runtime=vggt_only).catalog(vggt_only, role="actor")),
                         canonical([d for d in create_registry(runtime=vggt_only).catalog(vggt_only, role="actor")]))
        self.assertNotIn("object_table", canonical(create_registry(runtime=vggt_only).catalog(vggt_only, role="actor")))

    def test_off_request_is_rejected_and_service_untouched(self):
        fake = FakeService(); self.addCleanup(fake.close)
        from auriga.core.tool_errors import ToolInputError
        with self.assertRaises(ToolInputError):
            create_registry(runtime=self.runtime(on=False)).validate("geometry_query", {"request": self.request()}, role="actor")
        result = handlers.geometry_query(self.runtime(on=False, endpoint=fake.url), {"request": self.request(camera_id="cam_head")}, {}).data
        self.assertNotEqual(result.get("status"), "conditional")
        self.assertEqual(fake.requests, [])

    # ------------------------------------------------------------ switch on: model-facing contract
    def test_on_definition_adds_only_the_object_table_branch(self):
        off = {d["name"]: d for d in public_definitions()}
        on = {d["name"]: d for d in public_definitions(object_table=True)}
        self.assertEqual(set(on), set(off))
        for name in off:
            if name != "geometry_query":
                self.assertEqual(canonical(on[name]), canonical(off[name]), name)
        geometry = on["geometry_query"]
        self.assertIn(object_table.KIND_SENTENCE, geometry["description"])
        self.assertEqual(branch_kind := _branch(geometry, "object_table")["properties"]["kind"]["description"], object_table.KIND_SENTENCE)
        branch = _branch(geometry, "object_table")
        self.assertEqual(branch["required"], ["kind", "observation_id"])
        self.assertFalse(branch["additionalProperties"])
        self.assertEqual(branch["properties"]["pixels"]["description"], object_table.FIELD_DESCRIPTIONS["pixels"])
        self.assertEqual(canonical(_branch(geometry, "pixel_plane")), canonical(_branch(off["geometry_query"], "pixel_plane")))
        self.assertEqual(canonical(_branch(geometry, "alignment")), canonical(_branch(off["geometry_query"], "alignment")))
        added = len(canonical(geometry["description"])) - len(canonical(off["geometry_query"]["description"]))
        self.assertLess(added, 1700)   # 1500 before the labels field
        self.assertEqual(branch["properties"]["labels"]["description"], object_table.FIELD_DESCRIPTIONS["labels"])
        registry = create_registry(runtime=self.runtime())
        registry.validate("geometry_query", {"request": self.request()}, role="actor")
        registry.validate("geometry_query", {"request": self.request(camera_id="cam_head", pixels=[[1, 2]],
                                                                     target_camera_ids=["cam_side"])}, role="actor")
        from auriga.core.tool_errors import ToolInputError
        for bad in (self.request(extra=1), self.request(pixels=[[1, 2, 3]]), self.request(target_camera_ids=["a", "b", "c", "d"]),
                    {"kind": "object_table"}):
            with self.assertRaises(ToolInputError):
                registry.validate("geometry_query", {"request": bad}, role="actor")
        catalog = registry.catalog(self.catalog_runtime(), role="actor")
        self.assertIn("object_table", next(d for d in catalog if d["name"] == "geometry_query")["description"])

    def test_on_with_vggt_default_keeps_vggt_contract(self):
        both = {d["name"]: d for d in public_definitions(vggt_height="default", object_table=True)}
        vggt = {d["name"]: d for d in public_definitions(vggt_height="default")}
        self.assertEqual(canonical(_branch(both["geometry_query"], "pixel_plane")), canonical(_branch(vggt["geometry_query"], "pixel_plane")))
        self.assertIsNotNone(_branch(both["geometry_query"], "object_table"))
        registry = create_registry(runtime=self.runtime(vggt=True))
        registry.validate("geometry_query", {"request": self.request()}, role="actor")
        registry.validate("geometry_query", {"request": dict(kind="pixel_plane", observation_id="obs-ot", camera_id="cam_head",
                                                             pixel=[1, 2])}, role="actor")

    # ------------------------------------------------------------ switch on: behaviour with the fake service
    def test_table_overlays_and_request(self):
        fake = FakeService(); self.addCleanup(fake.close)
        runtime = self.runtime(endpoint=fake.url)
        response = handlers.geometry_query(runtime, {"request": self.request(pixels=[[320, 240]], target_camera_ids=["cam_side"])}, {})
        result = response.data
        self.assertEqual(result["status"], "conditional", result)
        value = result["value"]
        self.assertEqual(value["columns"], object_table.COLUMNS)
        self.assertEqual(value["rows"][0], ["o1", [0.0, 0.0, 0.05], 0.1, 0.0, 0.1, 0.03, 30, 88, "table", "ok"])
        self.assertEqual(value["rows"][1][7:], [None, "o1", "low:contact_mismatch"])   # not elongated -> no tilt
        self.assertEqual(value["requested"], [dict(pixel=[320, 240], status="ok", id="o1")])
        self.assertEqual(value["overlay_cameras"], ["cam_head", "cam_side"])
        self.assertEqual(value["projected_ids"]["cam_side"], ["o1", "o2"])
        self.assertFalse(value["measured"])
        images = [item for item in response.content if isinstance(item, dict) and item.get("type") == "inputImage"]
        self.assertEqual(len(images), 2)
        self.assertEqual(set(result["overlay_refs"]), {"cam_head", "cam_side"})
        text = json.dumps([item for item in response.content if not (isinstance(item, dict) and item.get("type") == "inputImage")],
                          default=str)
        self.assertNotIn("diagnostics", text)
        self.assertIn("diagnostics", result)
        path, sent = fake.requests[0]
        self.assertEqual(path, "/object_table")
        self.assertEqual(base64.b64decode(sent["image_b64"]), self.images["cam_head"])
        np.testing.assert_allclose(sent["K"], K); np.testing.assert_allclose(sent["T_world_camera"], T_HEAD)
        self.assertEqual((sent["support_z_m"], sent["pixels"], sent["max_objects"], sent["robot_segments_m"]),
                         (0.0, [[320.0, 240.0]], 20, []))
        self.assertEqual(sent["exclude_boxes_px"], [[200, 405, 440, 480]])
        events = [e for e in self.artifacts.events_by_type(["geometry.overlay_delivered"])]
        self.assertEqual(len(events), 2)

    def test_labels_are_forwarded_and_tied_to_rows(self):
        fake = FakeService(); self.addCleanup(fake.close)
        registry = create_registry(runtime=self.runtime(endpoint=fake.url))
        registry.validate("geometry_query", {"request": self.request(labels=["test tube", "rack"])}, role="actor")
        from auriga.core.tool_errors import ToolInputError
        for bad in (self.request(labels=["x"] * 17), self.request(labels=[3])):
            with self.assertRaises(ToolInputError):
                registry.validate("geometry_query", {"request": bad}, role="actor")
        value = handlers.geometry_query(self.runtime(endpoint=fake.url),
                                        {"request": self.request(labels=["test tube", "rack"])}, {}).data["value"]
        self.assertEqual(fake.requests[0][1]["labels"], ["test tube", "rack"])
        self.assertEqual(value["columns"], object_table.COLUMNS + ["label"])
        self.assertEqual([row[-1] for row in value["rows"]], [None, "test tube"])
        self.assertEqual(value["labels"], [dict(label="test tube", status="ok", ids=["o2"]),
                                           dict(label="rack", status="no_match", ids=[])])
        # without labels: columns and rows unchanged, nothing sent
        plain = handlers.geometry_query(self.runtime(endpoint=fake.url), {"request": self.request()}, {}).data["value"]
        self.assertEqual(plain["columns"], object_table.COLUMNS)
        self.assertNotIn("labels", plain)
        self.assertNotIn("labels", fake.requests[1][1])

    def test_service_unavailable_and_bad_camera(self):
        result = handlers.geometry_query(self.runtime(), {"request": self.request()}, {}).data
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["reason"].startswith("object_table_unavailable:"), result)
        fake = FakeService(); self.addCleanup(fake.close)
        bad = handlers.geometry_query(self.runtime(endpoint=fake.url), {"request": self.request(camera_id="nope")}, {}).data
        self.assertEqual(bad["status"], "unknown")
        self.assertEqual(fake.requests, [])
        failing = FakeService(status="no_horizontal_support_plane"); self.addCleanup(failing.close)
        failed = handlers.geometry_query(self.runtime(endpoint=failing.url), {"request": self.request()}, {}).data
        self.assertIn("no_horizontal_support_plane", failed["reason"])

    def test_height_service_flags_reach_plane_estimate_quality(self):
        from auriga.perception.external import vggt_height
        base = dict(method="sam_point_mask", n_points=500, height_m=0.05)
        fit = dict(support_fit=dict(tilt_deg=1.0, inlier_frac=0.5))
        self.assertEqual(vggt_height.quality(fit, base), ("ok", []))
        self.assertEqual(vggt_height.quality(fit, dict(base, flags=["floating", "low_depth_confidence"])),
                         ("low", ["floating", "low_depth_confidence"]))

    def test_empty_table(self):
        fake = FakeService(objects=[]); self.addCleanup(fake.close)
        response = handlers.geometry_query(self.runtime(endpoint=fake.url), {"request": self.request(target_camera_ids=["cam_side"])}, {})
        self.assertEqual(response.data["value"]["rows"], [])
        self.assertEqual(response.data["value"]["projected_ids"], {"cam_side": []})


if __name__ == "__main__":
    unittest.main()
