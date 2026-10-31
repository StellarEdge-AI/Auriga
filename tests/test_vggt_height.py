"""features.vggt_height: pixel_plane with plane_source='vggt' against a fake local service.
python -m unittest discover -s tests -p test_vggt_height.py"""
import base64
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from auriga.core.config import Config
from auriga.core.contracts import canonical
from auriga.robot.model.geometry import GeometryTools
from auriga.perception.scene.store import SceneStore
from auriga.core.storage.store import Store
from auriga.runtime.tools import handlers
from auriga.runtime.tools.builtins import create_registry
from auriga.runtime.tools.definitions import public_definitions
from auriga.perception.external import vggt_height
from geometry_fixture import IDENTITY, recorded_plane


class FakeService:
    """Answers /height like vggt_height_service.py; records what the harness sent."""

    def __init__(self, center=2.0, top=2.5, status="ok", method="sam_point_mask"):
        self.requests, owner = [], self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(body)
                if status != "ok":
                    answer, code = dict(status="error", error=status), 422
                else:
                    u, v = body["pixels"][0]
                    answer, code = dict(status="ok", version="fake", scale=0.8,
                        support_fit=dict(tilt_deg=1.0, residual_p50_m=.003, residual_p90_m=.01, inlier_frac=.5),
                        pixels=[dict(pixel=[u, v], status="ok", method=method, top_z_m=top, center_z_m=center,
                                     support_z_m=body["support_z_m"], height_m=top - body["support_z_m"],
                                     n_points=500, mask_px=900)]), 200
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
    return next(b for b in definition["inputSchema"]["properties"]["request"]["oneOf"]
                if b["properties"]["kind"].get("const") == kind)


class VggtHeightCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.artifacts = Store(self.tmp.name, IDENTITY); self.addCleanup(self.artifacts.close)
        self.scene = SceneStore(Path(self.tmp.name) / "scene", self.artifacts.resolve_artifact, self.artifacts.put_artifact)
        self.obs = recorded_plane(self.artifacts)
        for obs in self.obs:
            self.scene.import_observation(obs)

    def runtime(self, on=True, endpoint="http://127.0.0.1:9", support=1.0, mode="optional"):
        config = Config().with_overrides({"features": {"external_perception": False, "vggt_height": on, "vggt_endpoint": endpoint,
                                                       "vggt_support_z_m": support, "vggt_timeout_s": 5.0,
                                                       "vggt_height_mode": mode}})
        return SimpleNamespace(config=config, scene=self.scene, image_settings=None, identity=dict(IDENTITY),
                               store=self.artifacts)

    def request(self, **changes):
        return dict(kind="pixel_plane", observation_id="obs-1", camera_id="head", pixel=[345, 265],
                    plane_source="vggt") | changes

    # ------------------------------------------------------------ switch off: identical contract and behaviour
    def test_off_definitions_are_unchanged(self):
        base = canonical(public_definitions())
        self.assertEqual(canonical(public_definitions(vggt_height=False)), base)
        off = create_registry(runtime=self.runtime(on=False))
        self.assertEqual(canonical([s.definition for s in off._specs.values()]),
                         canonical([s.definition for s in create_registry()._specs.values()]))
        self.assertNotIn("plane_source", base)

    def test_off_ignores_plane_source(self):
        fake = FakeService(); self.addCleanup(fake.close)
        runtime = self.runtime(on=False, endpoint=fake.url)
        args = {"request": self.request(plane_origin_m=[0, 0, 2], plane_normal=[0, 0, 1])}
        response = handlers.geometry_query(runtime, args, {})
        plain = GeometryTools(self.scene).execute({**args["request"], **IDENTITY, "schema_version": "0.1"})
        for item in (response.data, plain):
            item.pop("duration_s")
        self.assertEqual(canonical(response.data), canonical(plain))
        self.assertEqual(fake.requests, [])

    # ------------------------------------------------------------ switch on: model-facing contract
    def test_on_definition_adds_only_plane_source(self):
        off = {d["name"]: d for d in public_definitions()}
        on = {d["name"]: d for d in public_definitions(vggt_height=True)}
        self.assertEqual(set(on), set(off))
        for name in off:
            if name != "geometry_query":
                self.assertEqual(canonical(on[name]), canonical(off[name]), name)
        geometry = on["geometry_query"]
        self.assertIn(vggt_height.DESCRIPTION_SENTENCE, geometry["description"])
        self.assertIn("plane_source", geometry["description"])  # input field guide
        branch = _branch(geometry, "pixel_plane")
        self.assertEqual(branch["properties"]["plane_source"]["enum"], ["caller", "vggt"])
        self.assertNotIn('plane_source="vggt"', geometry["description"])  # not read as a branch selector
        self.assertNotIn("plane_origin_m", branch["required"])
        self.assertEqual(canonical(_branch(geometry, "triangulate")), canonical(_branch(off["geometry_query"], "triangulate")))
        registry = create_registry(runtime=self.runtime(on=True))
        registry.validate("geometry_query", {"request": self.request()}, role="actor")
        registry.validate("geometry_query", {"request": self.request(plane_origin_m=[0, 0, 1], plane_normal=[0, 0, 1])}, role="actor")
        from auriga.core.tool_errors import ToolInputError
        with self.assertRaises(ToolInputError):
            registry.validate("geometry_query", {"request": self.request(plane_source=None)}, role="actor")
        bare = self.request(); bare.pop("plane_source")
        with self.assertRaises(ToolInputError):
            registry.validate("geometry_query", {"request": bare}, role="actor")
        with self.assertRaises(ToolInputError):
            registry.validate("geometry_query", {"request": self.request(plane_source="lidar")}, role="actor")
        caller = registry.validate("geometry_query", {"request": self.request(plane_source="caller",
            plane_origin_m=[0, 0, 2], plane_normal=[0, 0, 1])}, role="actor")
        with self.assertRaises(ToolInputError):  # off: plane_origin_m stays required
            create_registry(runtime=self.runtime(on=False)).validate("geometry_query", {"request": self.request()}, role="actor")

    # ------------------------------------------------------------ switch on: behaviour with the fake service
    def test_estimated_plane_replaces_height_and_top_is_alternate(self):
        fake = FakeService(center=2.0, top=2.5); self.addCleanup(fake.close)
        runtime = self.runtime(endpoint=fake.url, support=1.0)
        response = handlers.geometry_query(runtime, {"request": self.request()}, {})
        result = response.data
        self.assertEqual(result["status"], "conditional", result)
        value = result["value"]
        np.testing.assert_allclose(value["point_m"], [.2, .1, 2], atol=1e-10)
        np.testing.assert_allclose(value["plane_origin_m"], [0, 0, 2.0])
        np.testing.assert_allclose(value["plane_normal"], [0, 0, 1])
        np.testing.assert_allclose(value["alternate_point_m"], [.225, .125, 2.5], atol=1e-10)
        estimate = value["plane_estimate"]
        self.assertEqual((estimate["source"], estimate["measured"], estimate["quality"]), ("vggt_estimate", False, "ok"))
        self.assertEqual(estimate["alternate_plane"], "estimated_top_plane")
        self.assertAlmostEqual(estimate["object_height_m"], 1.5)
        self.assertIn("not measured depth", result["assumptions"][0])
        # what the harness sent: the saved RGB bytes, calibrated K / pose and the configured support height
        sent = fake.requests[0]
        camera = GeometryTools(self.scene).camera(IDENTITY, "obs-1", "head")
        _, image = self.scene.resolve_artifact(camera["frame"]["rgb_ref"])
        self.assertEqual(base64.b64decode(sent["image_b64"]), image)
        np.testing.assert_allclose(sent["K"], camera["K"])
        np.testing.assert_allclose(sent["T_world_camera"], camera["T"])
        self.assertEqual((sent["support_z_m"], sent["pixels"]), (1.0, [[345.0, 265.0]]))

    def test_caller_plane_becomes_alternate(self):
        fake = FakeService(center=2.0, top=2.5); self.addCleanup(fake.close)
        result = vggt_height.pixel_plane_with_estimate(self.runtime(endpoint=fake.url),
            self.request(plane_origin_m=[0, 0, 3], plane_normal=[0, 0, 1]))
        value = result["value"]
        np.testing.assert_allclose(value["point_m"], [.2, .1, 2], atol=1e-10)
        np.testing.assert_allclose(value["alternate_point_m"], [.25, .15, 3], atol=1e-10)
        self.assertEqual(value["plane_estimate"]["alternate_plane"], "caller_plane_origin_m")
        explicit = vggt_height.pixel_plane_with_estimate(self.runtime(endpoint=fake.url),
            self.request(plane_origin_m=[0, 0, 3], plane_normal=[0, 0, 1], plane_origin_alt_m=[0, 0, 4]))
        np.testing.assert_allclose(explicit["value"]["alternate_point_m"], [.3, .2, 4], atol=1e-10)
        self.assertEqual(explicit["value"]["plane_estimate"]["alternate_plane"], "caller_plane_origin_alt_m")

    def test_caller_source_is_plain_pixel_plane(self):
        fake = FakeService(); self.addCleanup(fake.close)
        request = self.request(plane_source="caller", plane_origin_m=[0, 0, 2], plane_normal=[0, 0, 1])
        result = handlers.geometry_query(self.runtime(endpoint=fake.url), {"request": request}, {}).data
        plain = dict(request); plain.pop("plane_source")
        plain = GeometryTools(self.scene).execute({**plain, **IDENTITY, "schema_version": "0.1"})
        for item in (result, plain):
            item.pop("duration_s")
        self.assertEqual(canonical(result), canonical(plain))
        self.assertEqual(fake.requests, [])

    def test_window_estimate_is_flagged_low_quality(self):
        fake = FakeService(method="window"); self.addCleanup(fake.close)
        result = vggt_height.pixel_plane_with_estimate(self.runtime(endpoint=fake.url), self.request())
        self.assertEqual(result["value"]["plane_estimate"]["quality"], "low")
        self.assertIn("no_object_mask_window_estimate", result["value"]["plane_estimate"]["quality_reasons"])

    def test_service_unavailable(self):
        # no plane from the caller -> unknown with a reason; caller plane -> unchanged pixel_plane + note
        result = vggt_height.pixel_plane_with_estimate(self.runtime(endpoint="http://127.0.0.1:9"), self.request())
        self.assertEqual(result["status"], "unknown", result)
        self.assertIsNone(result["value"])
        self.assertTrue(result["reason"].startswith("vggt_estimate_unavailable:"), result)
        fallback = vggt_height.pixel_plane_with_estimate(self.runtime(endpoint="http://127.0.0.1:9"),
            self.request(plane_origin_m=[0, 0, 2], plane_normal=[0, 0, 1]))
        self.assertEqual(fallback["status"], "conditional")
        np.testing.assert_allclose(fallback["value"]["point_m"], [.2, .1, 2], atol=1e-10)
        self.assertEqual(fallback["value"]["plane_estimate"]["status"], "unavailable")
        fake = FakeService(status="no_horizontal_support_plane"); self.addCleanup(fake.close)
        failed = vggt_height.pixel_plane_with_estimate(self.runtime(endpoint=fake.url), self.request())
        self.assertIn("no_horizontal_support_plane", failed["reason"])

    def test_invalid_camera_is_reported_by_geometry(self):
        fake = FakeService(); self.addCleanup(fake.close)
        result = vggt_height.pixel_plane_with_estimate(self.runtime(endpoint=fake.url), self.request(camera_id="nope"))
        self.assertNotEqual(result["status"], "conditional")
        self.assertEqual(fake.requests, [])


    # ------------------------------------------------------------ vggt_height_mode = "default"
    def catalog_runtime(self, mode):
        return SimpleNamespace(disabled_tools=(), observation_profile="rgb_proprio_calibrated_v1", motion_review=True,
                               enable_wait=True, vision_compare=True, visual_reviewer=True, action_policy=None,
                               capabilities=None, programs=True, config=self.runtime(mode=mode).config,
                               features=self.runtime(mode=mode).config.features)

    def test_default_mode_definition(self):
        optional = {d["name"]: d for d in public_definitions(vggt_height="optional")}
        default = {d["name"]: d for d in public_definitions(vggt_height="default")}
        self.assertEqual(canonical(optional), canonical({d["name"]: d for d in public_definitions(vggt_height=True)}))
        for name in default:
            if name != "geometry_query":
                self.assertEqual(canonical(default[name]), canonical(optional[name]), name)
        geometry = default["geometry_query"]
        self.assertIn(vggt_height.DEFAULT_DESCRIPTION_SENTENCE, geometry["description"])
        self.assertNotIn(vggt_height.DESCRIPTION_SENTENCE, geometry["description"])
        branch = _branch(geometry, "pixel_plane")
        self.assertEqual(branch["properties"]["plane_source"]["default"], "vggt")
        self.assertNotIn("anyOf", branch)
        registry = create_registry(runtime=self.runtime(mode="default"))
        bare = self.request(); bare.pop("plane_source")
        registry.validate("geometry_query", {"request": bare}, role="actor")
        catalog = registry.catalog(self.catalog_runtime("default"), role="actor")
        self.assertIn(vggt_height.DEFAULT_DESCRIPTION_SENTENCE,
                      next(d for d in catalog if d["name"] == "geometry_query")["description"])

    def test_default_mode_replaces_caller_plane_and_keeps_it_as_alternate(self):
        fake = FakeService(center=2.0, top=2.5); self.addCleanup(fake.close)
        runtime = self.runtime(endpoint=fake.url, mode="default")
        request = self.request(plane_origin_m=[0, 0, 3], plane_normal=[0, 0, 1], plane_origin_alt_m=[0, 0, 4])
        request.pop("plane_source")
        value = handlers.geometry_query(runtime, {"request": request}, {}).data["value"]
        np.testing.assert_allclose(value["point_m"], [.2, .1, 2], atol=1e-10)
        np.testing.assert_allclose(value["alternate_point_m"], [.25, .15, 3], atol=1e-10)
        self.assertAlmostEqual(value["assumption_delta_m"], np.linalg.norm([.05, .05, 1]), places=10)
        self.assertEqual((value["plane_estimate"]["mode"], value["plane_estimate"]["alternate_plane"]),
                         ("default", "caller_plane_origin_m"))
        bare = self.request(); bare.pop("plane_source")
        top = handlers.geometry_query(runtime, {"request": bare}, {}).data["value"]
        self.assertEqual(top["plane_estimate"]["alternate_plane"], "estimated_top_plane")
        explicit = handlers.geometry_query(runtime, {"request": self.request()}, {}).data["value"]
        np.testing.assert_allclose(explicit["point_m"], [.2, .1, 2], atol=1e-10)
        self.assertEqual(len(fake.requests), 3)
        # 当默认模式下调用方显式传入了 plane_origin_m 且像素未命中 SAM 物体掩码（method="window"）时，保留调用方平面为主平面
        fake_win = FakeService(center=2.0, top=2.5, method="window"); self.addCleanup(fake_win.close)
        runtime_win = self.runtime(endpoint=fake_win.url, mode="default")
        req_win = self.request(plane_origin_m=[0, 0, 3], plane_normal=[0, 0, 1]); req_win.pop("plane_source")
        val_win = handlers.geometry_query(runtime_win, {"request": req_win}, {}).data["value"]
        np.testing.assert_allclose(val_win["point_m"], [.25, .15, 3], atol=1e-10)
        np.testing.assert_allclose(val_win["alternate_point_m"], [.2, .1, 2], atol=1e-10)
        self.assertEqual(val_win["plane_estimate"]["alternate_plane"], "estimated_centre_plane")
        # batch 模式下的子查询应自动继承顶层的 plane_origin_m 与 plane_normal
        batch_req = {
            "kind": "batch",
            "observation_id": "obs-1",
            "camera_id": "head",
            "plane_source": "caller",
            "plane_origin_m": [0, 0, 2],
            "plane_normal": [0, 0, 1],
            "queries": [{"kind": "pixel_plane", "pixel": [345, 265]}],
        }
        batch_res = handlers.geometry_query(runtime_win, {"request": batch_req}, {}).data
        np.testing.assert_allclose(batch_res["value"]["batch_results"][0]["value"]["point_m"], [.2, .1, 2], atol=1e-10)

    def test_default_mode_caller_source_and_non_horizontal_plane_are_unchanged(self):
        fake = FakeService(); self.addCleanup(fake.close)
        runtime = self.runtime(endpoint=fake.url, mode="default")
        caller = self.request(plane_source="caller", plane_origin_m=[0, 0, 2], plane_normal=[0, 0, 1])
        tilted = self.request(plane_origin_m=[0, 0, 2], plane_normal=[0, 1, 1]); tilted.pop("plane_source")
        for request, estimate in ((caller, None), (tilted, "not_applied")):
            result = handlers.geometry_query(runtime, {"request": request}, {}).data
            plain = dict(request); plain.pop("plane_source", None)
            plain = GeometryTools(self.scene).execute({**plain, **IDENTITY, "schema_version": "0.1"})
            if estimate:
                self.assertEqual(result["value"].pop("plane_estimate")["status"], estimate)
            for item in (result, plain):
                item.pop("duration_s")
            self.assertEqual(canonical(result), canonical(plain))
        self.assertEqual(fake.requests, [])

    def test_default_mode_service_unavailable_falls_back_to_caller_plane(self):
        runtime = self.runtime(endpoint="http://127.0.0.1:9", mode="default")
        request = self.request(plane_origin_m=[0, 0, 2], plane_normal=[0, 0, 1]); request.pop("plane_source")
        result = handlers.geometry_query(runtime, {"request": request}, {}).data
        self.assertEqual(result["status"], "conditional")
        np.testing.assert_allclose(result["value"]["point_m"], [.2, .1, 2], atol=1e-10)
        self.assertEqual(result["value"]["plane_estimate"]["status"], "unavailable")
        bare = self.request(); bare.pop("plane_source")
        missing = handlers.geometry_query(runtime, {"request": bare}, {}).data
        self.assertEqual(missing["status"], "unknown")
        self.assertTrue(missing["reason"].startswith("vggt_estimate_unavailable:"))

    def test_default_mode_with_switch_off_is_unchanged(self):
        fake = FakeService(); self.addCleanup(fake.close)
        runtime = self.runtime(on=False, endpoint=fake.url, mode="default")
        request = self.request(plane_origin_m=[0, 0, 2], plane_normal=[0, 0, 1]); request.pop("plane_source")
        result = handlers.geometry_query(runtime, {"request": request}, {}).data
        plain = GeometryTools(self.scene).execute({**request, **IDENTITY, "schema_version": "0.1"})
        for item in (result, plain):
            item.pop("duration_s")
        self.assertEqual(canonical(result), canonical(plain))
        self.assertEqual(canonical([s.definition for s in create_registry(runtime=runtime)._specs.values()]),
                         canonical([s.definition for s in create_registry()._specs.values()]))
        self.assertEqual(fake.requests, [])


if __name__ == "__main__":
    unittest.main()
