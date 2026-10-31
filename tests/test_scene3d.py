"""features.scene3d: geometry_query kind scene3d (fake /points service) and the scene3d_helpers queries.
python -m unittest discover -s tests -p test_scene3d.py"""
import base64
import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from auriga.core.config import Config
from auriga.core.contracts import canonical
from auriga.perception.scene.store import SceneStore
from auriga.core.storage.store import Store
from auriga.runtime.tools import handlers
from auriga.runtime.tools.builtins import create_registry
from auriga.runtime.tools.definitions import public_definitions
from auriga.perception.external import scene3d
from auriga.perception.external import scene3d_helpers as s3
from geometry_fixture import IDENTITY
from test_object_table import K, T_HEAD, two_camera_observation, _branch

TABLE = 0.765


def synthetic_cloud(seed=0):
    """Table grid (1 cm) at z=TABLE over [-0.3, 0.3]^2 with two objects and a few robot points.
    box: 6 x 4 cm footprint centred (0.10, 0.00), top TABLE+0.04 (top face + side walls);
    cylinder: r 2 cm at (-0.10, 0.10), top TABLE+0.10; robot: a vertical line at (0.2, 0.2) up to +0.3 m.
    uv: a top-down pinhole with 500 px/m about (320, 240)."""
    rng = np.random.default_rng(seed)
    g = np.arange(-0.30, 0.3001, 0.01)
    X, Y = np.meshgrid(g, g)
    table = np.stack([X.ravel(), Y.ravel(), np.full(X.size, TABLE)], 1)
    box_top = [[x, y, TABLE + 0.04] for x in np.arange(0.07, 0.1301, 0.005) for y in np.arange(-0.02, 0.0201, 0.005)]
    box_side = [[0.07, y, z] for y in np.arange(-0.02, 0.0201, 0.005) for z in np.arange(TABLE + 0.005, TABLE + 0.0301, 0.005)]
    ang = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    cyl = [[-0.10 + 0.02 * np.cos(a), 0.10 + 0.02 * np.sin(a), z] for a in ang for z in np.arange(TABLE + 0.01, TABLE + 0.1001, 0.01)]
    cyl += [[-0.10 + r * np.cos(a), 0.10 + r * np.sin(a), TABLE + 0.10] for r in (0.0, 0.01) for a in ang[::3]]
    robot = [[0.2, 0.2, TABLE + z] for z in np.arange(0.05, 0.3, 0.01)]
    objects = np.asarray(box_top + box_side + cyl, float)
    # objects occlude the table under them
    keep = np.ones(len(table), bool)
    keep &= ~((np.abs(table[:, 0] - 0.10) <= 0.03) & (np.abs(table[:, 1]) <= 0.02))
    keep &= np.hypot(table[:, 0] + 0.10, table[:, 1] - 0.10) > 0.02
    xyz = np.concatenate([table[keep], objects, np.asarray(robot)])
    robot_flag = np.zeros(len(xyz), bool)
    robot_flag[-len(robot):] = True
    conf = rng.integers(60, 256, len(xyz)).astype(np.uint8)
    conf[5] = 0                                    # one low-confidence table point
    uv = np.stack([320 + 500 * xyz[:, 0], 240 - 500 * xyz[:, 1]], 1).round().astype(np.uint16)
    buf = io.BytesIO()
    np.savez_compressed(buf, xyz=xyz.astype(np.float32), conf=conf, uv=uv, count=np.ones(len(xyz), np.uint16), robot=robot_flag,
                        support_z=np.float64(TABLE), voxel_m=np.float64(0.01), image_size=np.asarray([640, 480], np.int32))
    return buf.getvalue()


class HelperQueries(unittest.TestCase):
    def setUp(self):
        self.cloud = s3.use(s3.from_bytes(synthetic_cloud()))

    def test_info_and_table(self):
        info = s3.info()
        self.assertEqual(info["table_z"], TABLE)
        self.assertEqual(info["near_robot"], 25)
        self.assertEqual(s3.table_z(), TABLE)
        local = s3.table_z([0.0, -0.2], radius=0.05)
        self.assertAlmostEqual(local["z"], TABLE, places=4)
        self.assertGreater(local["n"], 20)

    def test_top_z_regions(self):
        box = s3.top_z(([0.06, -0.03, 0.0], [0.14, 0.03, 2.0]))
        self.assertAlmostEqual(box["top_z"], TABLE + 0.04, places=3)
        self.assertAlmostEqual(box["height_above_table"], 0.04, places=3)
        cyl = s3.top_z({"center": [-0.10, 0.10], "radius": 0.03})
        self.assertAlmostEqual(cyl["top_z"], TABLE + 0.10, places=3)
        poly = s3.top_z({"polygon": [[0.06, -0.03], [0.14, -0.03], [0.14, 0.03], [0.06, 0.03]]})
        self.assertEqual(poly["n"], box["n"])
        clipped = s3.top_z({"center": [-0.10, 0.10], "radius": 0.03, "z_max": TABLE + 0.05})
        self.assertLess(clipped["top_z"], TABLE + 0.051)
        empty = s3.top_z({"center": [0.0, -0.2], "radius": 0.03})
        self.assertEqual((empty["n"], empty["top_z"]), (0, None))
        # robot points are excluded unless asked for
        self.assertEqual(s3.top_z({"center": [0.2, 0.2], "radius": 0.01})["n"], 0)
        self.assertAlmostEqual(s3.top_z({"center": [0.2, 0.2], "radius": 0.01}, robot=True, q=100)["top_z"], TABLE + 0.29, places=3)

    def test_points_in_box_and_cylinder(self):
        inside = s3.points_in_box([0.06, -0.03, TABLE + 0.035], [0.14, 0.03, TABLE + 0.05], xyz=True)
        self.assertEqual(inside["n"], 13 * 9)
        self.assertEqual(inside["xyz"].shape, (117, 3))
        self.assertAlmostEqual(inside["centroid"][0], 0.10, places=3)
        self.assertNotIn("xyz", s3.points_in_box([0, 0, 0], [1, 1, 1]))
        cyl = s3.points_in_cylinder([-0.10, 0.10], 0.025, z_min=TABLE + 0.004)
        self.assertGreater(cyl["n"], 200)
        self.assertAlmostEqual(cyl["max"][2], TABLE + 0.10, places=3)
        # confidence filter: the one conf-0 table point is dropped by default
        everything = s3.points_in_box([-1, -1, 0], [1, 1, 2], min_conf=0.0, robot=True)["n"]
        self.assertEqual(everything, len(self.cloud.xyz))
        self.assertEqual(s3.points_in_box([-1, -1, 0], [1, 1, 2], robot=True)["n"], everything - 1)

    def test_nearest(self):
        near = s3.nearest([0.10, 0.0, TABLE + 0.04], k=3)
        self.assertEqual(len(near["xyz"]), 3)
        self.assertLessEqual(near["dist"][0], 1e-6)
        self.assertEqual(near["uv"][0], [370, 240])
        horizontal = s3.nearest([-0.10, 0.10], k=1)
        self.assertLess(horizontal["dist"][0], 0.011)

    def test_cluster_at_pixel_separates_objects(self):
        box = s3.cluster_at_pixel(370, 240)
        self.assertAlmostEqual(box["top_z"], TABLE + 0.04, places=3)
        self.assertLess(box["max"][0], 0.131)
        self.assertGreater(box["min"][0], 0.069)
        self.assertEqual(box["seed"]["uv"], [370, 240])
        cyl = s3.cluster_at_pixel(270, 190)
        self.assertAlmostEqual(cyl["max"][2], TABLE + 0.10, places=3)
        self.assertLess(abs(cyl["centroid"][0] + 0.10), 0.01)
        self.assertNotIn("xyz", cyl)

    def test_not_loaded(self):
        s3.use(None)
        with self.assertRaises(ValueError):
            s3.info()


class FakePoints:
    def __init__(self, status="ok"):
        self.requests, owner = [], self
        data = synthetic_cloud()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((self.path, body))
                if status != "ok":
                    answer, code = dict(status="error", error=status), 422
                else:
                    answer, code = dict(status="ok", version="fake-points", n_points=3000, n_above_support=600, n_robot=25,
                                        voxel_m=body["voxel_m"], support_z_m=body["support_z_m"],
                                        bbox_m=[[-0.3, -0.3, 0.765], [0.3, 0.3, 1.055]],
                                        bbox_above_support_m=[[-0.12, -0.02, 0.77], [0.13, 0.12, 0.865]],
                                        npz_b64=base64.b64encode(data).decode(), npz_bytes=len(data),
                                        timing=dict(total_s=0.01), support_fit=dict(tilt_deg=0.5), scale=1.0), 200
                raw = json.dumps(answer).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.data = data
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Scene3dTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.artifacts = Store(self.tmp.name, IDENTITY); self.addCleanup(self.artifacts.close)
        self.scene = SceneStore(Path(self.tmp.name) / "scene", self.artifacts.resolve_artifact, self.artifacts.put_artifact)
        observation, self.images = two_camera_observation(self.artifacts)
        self.scene.import_observation(observation)

    def runtime(self, on=True, endpoint="http://127.0.0.1:9", object_table=False):
        config = Config().with_overrides({"features": {"external_perception": False, "scene3d": on, "object_table": object_table, "vggt_endpoint": endpoint,
                                                       "vggt_support_z_m": TABLE, "vggt_timeout_s": 5.0}})
        return SimpleNamespace(config=config, scene=self.scene, image_settings=None, identity=dict(IDENTITY), store=self.artifacts,
                               features=config.features)

    def catalog_runtime(self, **kw):
        runtime = self.runtime(**kw)
        return SimpleNamespace(disabled_tools=(), observation_profile="rgb_proprio_calibrated_v1", motion_review=True,
                               enable_wait=True, vision_compare=True, visual_reviewer=True, action_policy=None,
                               capabilities=None, programs=True, config=runtime.config, features=runtime.config.features)

    def request(self, **changes):
        return dict(kind="scene3d", observation_id="obs-ot") | changes

    def test_off_catalog_identical_and_request_rejected(self):
        self.assertEqual(canonical(public_definitions(scene3d=False)), canonical(public_definitions()))
        digest = lambda runtime: hashlib.sha256(canonical(create_registry(runtime=runtime).catalog(runtime, role="actor")).encode()).hexdigest()
        self.assertEqual(digest(self.catalog_runtime(on=False)), hashlib.sha256(canonical(
            create_registry().catalog(self.catalog_runtime(on=False), role="actor")).encode()).hexdigest())
        self.assertNotIn("scene3d", canonical(create_registry(runtime=self.runtime(on=False)).catalog(self.catalog_runtime(on=False), role="actor")))
        from auriga.core.tool_errors import ToolInputError
        with self.assertRaises(ToolInputError):
            create_registry(runtime=self.runtime(on=False)).validate("geometry_query", {"request": self.request()}, role="actor")
        self.assertEqual(scene3d.workspace_modules(self.runtime(on=False)), ())
        self.assertEqual(scene3d.workspace_modules(self.runtime()), ("scene3d_helpers.py",))
        from auriga.runtime.programs.runner import ProgramServices
        self.assertEqual(ProgramServices.__dataclass_fields__["workspace_modules"].default, ())

    def test_on_definition_adds_only_the_scene3d_branch(self):
        off = {d["name"]: d for d in public_definitions()}
        on = {d["name"]: d for d in public_definitions(scene3d=True)}
        for name in off:
            if name != "geometry_query":
                self.assertEqual(canonical(on[name]), canonical(off[name]), name)
        geometry = on["geometry_query"]
        branch = _branch(geometry, "scene3d")
        self.assertEqual(branch["properties"]["kind"]["description"], scene3d.KIND_SENTENCE)
        self.assertEqual(branch["required"], ["kind"])
        self.assertEqual(canonical(_branch(geometry, "pixel_plane")), canonical(_branch(off["geometry_query"], "pixel_plane")))
        added = len(canonical(geometry)) - len(canonical(off["geometry_query"]))
        self.assertLess(added, 1550)
        both = {d["name"]: d for d in public_definitions(object_table=True, scene3d=True)}
        self.assertIsNotNone(_branch(both["geometry_query"], "object_table"))
        self.assertIsNotNone(_branch(both["geometry_query"], "scene3d"))
        registry = create_registry(runtime=self.runtime())
        registry.validate("geometry_query", {"request": self.request(camera_id="cam_head", voxel_m=0.02)}, role="actor")
        registry.validate("geometry_query", {"request": self.request()}, role="program")
        from auriga.core.tool_errors import ToolInputError
        for bad in (self.request(extra=1), self.request(voxel_m=0.5), {"kind": "scene3d"}):
            with self.assertRaises(ToolInputError):
                registry.validate("geometry_query", {"request": bad}, role="actor")

    def test_query_stores_artifact_and_returns_summary(self):
        fake = FakePoints(); self.addCleanup(fake.close)
        response = handlers.geometry_query(self.runtime(endpoint=fake.url), {"request": self.request()}, {})
        result = response.data
        self.assertEqual(result["status"], "conditional", result)
        value = result["value"]
        self.assertTrue(value["artifact"].startswith("artifact:"))
        info, content = self.artifacts.resolve_artifact(value["artifact"])
        self.assertEqual((info["kind"], info["mime_type"], content), ("scene3d_points", "application/x-npz", fake.data))
        self.assertEqual((value["points"], value["points_above_table"], value["near_robot"], value["table_z"]), (3000, 600, 25, TABLE))
        self.assertEqual(value["api"], scene3d.API)
        text = json.dumps(response.content, default=str)
        self.assertNotIn("npz_b64", text)
        self.assertNotIn("diagnostics", text)
        self.assertLess(len(text), 4000)
        path, sent = fake.requests[0]
        self.assertEqual(path, "/points")
        self.assertEqual(base64.b64decode(sent["image_b64"]), self.images["cam_head"])
        np.testing.assert_allclose(sent["K"], K); np.testing.assert_allclose(sent["T_world_camera"], T_HEAD)
        self.assertEqual((sent["voxel_m"], sent["support_z_m"], sent["params"]), (0.01, TABLE, dict(z_above=0.6, z_below=0.05)))
        cloud = s3.from_bytes(content)
        self.assertAlmostEqual(s3.top_z({"center": [-0.10, 0.10], "radius": 0.03}, cloud=cloud)["top_z"], TABLE + 0.10, places=3)

    def test_unavailable(self):
        result = handlers.geometry_query(self.runtime(), {"request": self.request()}, {}).data
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["reason"].startswith("scene3d_unavailable:"), result)
        failing = FakePoints(status="no_horizontal_support_plane"); self.addCleanup(failing.close)
        failed = handlers.geometry_query(self.runtime(endpoint=failing.url), {"request": self.request()}, {}).data
        self.assertIn("no_horizontal_support_plane", failed["reason"])

    def test_in_hand_axis_and_stereo_triangulate_rays(self):
        from auriga.perception.external.object_table import triangulate_object_centres
        from test_object_table import T_SIDE
        # Build a synthetic vertical rod held in the gripper at [0.0, -0.25, 0.88] with robot=True
        zs = np.linspace(0.84, 0.94, 40)
        rod_xyz = np.column_stack([np.zeros(40), np.full(40, -0.25), zs]).astype(np.float32)
        cloud = s3.Cloud({
            "xyz": rod_xyz,
            "conf": np.full(40, 220, dtype=np.uint8),
            "uv": np.zeros((40, 2), dtype=np.uint16),
            "robot": np.ones(40, dtype=bool),
            "support_z": TABLE,
        })
        ih = s3.in_hand_axis([0.0, -0.25, 0.89], radius=0.065, cloud=cloud)
        self.assertEqual(ih["n"], 40)
        self.assertEqual(ih["posture"], "upright")
        self.assertLess(ih["tilt_deg"], 5.0)
        self.assertGreater(ih["elongation"], 5.0)

        # Stereo triangulation between cam_head (T_HEAD) and cam_side (T_SIDE) for true 3D point [0.1, -0.05, 0.2]
        true_pt = np.array([0.10, -0.05, 0.20])
        from auriga.robot.model.geometry import project
        cam1 = dict(K=np.asarray(K, float), D=np.zeros(5), T=np.asarray(T_HEAD, float), width=640, height=480)
        cam2 = dict(K=np.asarray(K, float), D=np.zeros(5), T=np.asarray(T_SIDE, float), width=640, height=480)
        uv1, _, _ = project(cam1, [true_pt.tolist()])
        uv2, _, _ = project(cam2, [true_pt.tolist()])
        tri = s3.triangulate_rays(uv1[0][0], uv1[0][1], K, T_HEAD, uv2[0][0], uv2[0][1], K, T_SIDE)
        self.assertEqual(tri["status"], "ok")
        np.testing.assert_allclose(tri["point_m"], true_pt, atol=1e-3)
        self.assertLess(tri["skew_gap_m"], 1e-4)

        anchored = triangulate_object_centres(
            cam1, cam2,
            [dict(centre_m=[0.10, -0.05, 0.175], top_z_m=0.215, bottom_z_m=0.135, label_px=uv1[0].tolist())],
            {"o1": uv2[0].tolist()},
        )
        self.assertEqual(len(anchored), 1)
        self.assertAlmostEqual(anchored[0]["dz_correction_m"], 0.025, places=3)
        self.assertAlmostEqual(anchored[0]["stereo_top_z_m"], 0.240, places=3)
        self.assertAlmostEqual(ih["bottom_point_m"][2], 0.84, places=2)
        self.assertAlmostEqual(ih["tip_offset_from_grasp_m"][2], -0.05, places=2)

        surf = s3.surface_extrema({"center": [0.10, 0.0], "radius": 0.05}, cloud=s3.from_bytes(synthetic_cloud()))
        self.assertAlmostEqual(surf["floor_z"], TABLE, places=3)
        self.assertAlmostEqual(surf["rim_z"], TABLE + 0.04, places=3)

    def test_in_process_scene3d_queries_and_batch_geometry_query(self):
        fake = FakePoints(); self.addCleanup(fake.close)
        runtime = self.runtime(endpoint=fake.url)
        registry = create_registry(runtime=runtime)
        first = handlers.geometry_query(runtime, {"request": self.request()}, {}).data
        artifact_ref = first["value"]["artifact"]
        self.assertEqual(len(fake.requests), 1)
        # In-process queries against stored artifact without any additional HTTP round-trip
        q_req = {
            "kind": "scene3d",
            "artifact": artifact_ref,
            "queries": [
                {"op": "top_z", "region": {"center": [-0.10, 0.10], "radius": 0.03}},
                {"op": "surface_extrema", "center": [0.10, 0.0], "radius": 0.05},
                {"op": "cluster_at_pixel", "pixel": [370, 240]},
                {"op": "in_hand_axis", "grasp_center_m": [0.0, -0.25, 0.25], "radius": 0.065},
                {"op": "in_hand_axis", "arm": "left", "radius": 0.065},
                {"op": "triangulate_rays", "pixel_a": [370, 240], "pixel_b": [120, 240], "camera_id_b": "cam_side"},
            ],
        }
        runtime.latest = {
            **self.scene.observation(runtime.identity, "obs-ot"),
            "robot_state": {
                "arms": {
                    "left": {
                        "ee_pose": {
                            "position_m": [0.0, -0.25, 0.40],
                            "quaternion_xyzw": [0.0, 0.70710678, 0.0, 0.70710678],
                        }
                    }
                }
            },
        }
        registry.validate("geometry_query", {"request": q_req}, role="actor")
        q_resp = handlers.geometry_query(runtime, {"request": q_req}, {}).data
        self.assertEqual(q_resp["status"], "conditional")
        self.assertEqual(len(fake.requests), 1)
        results = q_resp["value"]["query_results"]
        self.assertEqual(len(results), 6)
        self.assertAlmostEqual(results[0]["top_z"], TABLE + 0.10, places=3)
        self.assertAlmostEqual(results[1]["floor_z"], TABLE, places=3)
        self.assertAlmostEqual(results[1]["rim_z"], TABLE + 0.04, places=3)
        self.assertAlmostEqual(results[2]["top_z"], TABLE + 0.04, places=3)
        self.assertEqual(results[3]["op"], "in_hand_axis")
        self.assertNotEqual(results[3].get("status"), "error")
        self.assertEqual(results[4]["op"], "in_hand_axis")
        self.assertNotEqual(results[4].get("status"), "error")
        self.assertEqual(results[5]["op"], "triangulate_rays")
        self.assertEqual(results[5]["status"], "ok")
        # Top-level batch query combining pixel_ray and scene3d in one call
        batch_req = {
            "kind": "batch",
            "observation_id": "obs-ot",
            "artifact": artifact_ref,
            "queries": [
                {"kind": "pixel_ray", "camera_id": "cam_head", "pixel": [320, 240]},
                {"op": "top_z", "region": {"center": [-0.10, 0.10], "radius": 0.03}},
            ],
        }
        registry.validate("geometry_query", {"request": batch_req}, role="actor")
        b_resp = handlers.geometry_query(runtime, {"request": batch_req}, {}).data
        self.assertEqual(b_resp["query_type"], "batch")
        self.assertEqual(b_resp["value"]["count"], 2)
        self.assertEqual(b_resp["value"]["batch_results"][0]["query_type"], "pixel_ray")
        self.assertAlmostEqual(
            b_resp["value"]["batch_results"][1]["value"]["query_result"]["top_z"], TABLE + 0.10, places=3
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "program jobs need Linux")
    def test_program_job_loads_the_artifact_through_the_helper(self):
        """Real ProgramRunner (sandbox off): geometry_query scene3d inside the job, then the helpers on the alias."""
        from auriga.runtime.programs.runner import ProgramRunner, ProgramServices
        fake = FakePoints(); self.addCleanup(fake.close)
        runtime = self.runtime(endpoint=fake.url)
        registry = create_registry(runtime=runtime)
        services = ProgramServices(store=self.artifacts, validate_tool=lambda name: registry.spec(name, role="program"),
            validate_arguments=lambda name, arguments: registry.validate(name, arguments, role="program"),
            invoke=lambda name, arguments, correlation: handlers.geometry_query(runtime, arguments, correlation),
            effect=lambda name, arguments: registry.spec(name, role="program").effect_for(arguments),
            observation=lambda: dict(observation_id="obs-ot", observation_profile="rgb_proprio_calibrated_v1"),
            action_active=lambda identifier: False, cancel_action=lambda identifier: False, sandbox_enabled=False,
            workspace_modules=scene3d.workspace_modules(runtime))
        runner = ProgramRunner(services); self.addCleanup(runner.close)
        source = ("import json\nfrom auriga_client import AurigaClient\nimport scene3d_helpers as s3\n"
                  "r = AurigaClient().call('geometry_query', {'request': {'kind': 'scene3d', 'observation_id': 'obs-ot'}})\n"
                  "a = r['data']['value']['artifact']\ns3.load(a)\n"
                  "print(json.dumps(dict(alias=a, info=s3.info(), top=s3.top_z({'center': [-0.10, 0.10], 'radius': 0.03}),"
                  " cl=s3.cluster_at_pixel(370, 240)['top_z'])))\n")
        job = runner.run(dict(source=source, timeout_s=60), dict(thread_id="t", turn_id="u", tool_call_id="c1"))
        self.assertEqual(job["status"], "succeeded", job)
        log = Path(self.artifacts.root) / "private" / "program-logs" / (job["job_id"] + ".stdout")
        out = json.loads(log.read_text().strip().splitlines()[-1])
        self.assertTrue(out["alias"].startswith("artifact:ref_"), out)
        self.assertAlmostEqual(out["top"]["top_z"], TABLE + 0.10, places=3)
        self.assertAlmostEqual(out["cl"], TABLE + 0.04, places=3)


if __name__ == "__main__":
    unittest.main()
