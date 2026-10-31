"""features.held_perception: FK robot pixel mask, held-object row (v1 and v2 pre-grasp carry), flag-off identity.
python -m unittest discover -s tests -p test_held_perception.py"""
import base64
import json
import math
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from auriga.perception.external import held_perception as hp
from auriga.perception.external import object_table
from auriga.core.config import Config
from auriga.perception.scene.store import SceneStore
from auriga.core.storage.store import Store
from auriga.runtime.tools import handlers
from auriga.core.contracts import canonical
from auriga.runtime.tools.builtins import create_registry
from geometry_fixture import IDENTITY
from test_object_table import K, T_HEAD, fake_object, two_camera_observation

KC = np.array([[300.0, 0, 320.0], [0, 300.0, 240.0], [0, 0, 1.0]])
T_CAM = np.eye(4)          # camera at the origin looking along +z (optical axes)


def box(lo, hi):
    V = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])], float)
    F = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1], [2, 3, 7], [2, 7, 6],
                  [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]])
    return V, F


def pose(t, R=None):
    T = np.eye(4)
    T[:3, :3] = np.eye(3) if R is None else R
    T[:3, 3] = t
    return T


class MaskRenderer(unittest.TestCase):
    def test_projection_of_a_known_finger_pose(self):
        # a 2 cm x 4 cm finger plate 0.5 m in front of the camera, offset 5 cm to +x: pixels u 338..350, v 228..252
        V, F = box([-0.01, -0.02, -0.002], [0.01, 0.02, 0.002])
        finger = pose([0.05, 0.0, 0.5])
        mask = hp.render_mask(KC, T_CAM, 640, 480, [(V, F, finger)], scale=1.0, dilate_px=0)
        vs, us = np.nonzero(mask)
        # expected continuous extent: u = 320 + 300 * (0.05 +- 0.01) / z, z in [0.498, 0.502]
        self.assertAlmostEqual(us.min(), 320 + 300 * 0.04 / 0.502, delta=1.0)
        self.assertAlmostEqual(us.max() + 1, 320 + 300 * 0.06 / 0.498, delta=1.0)
        self.assertAlmostEqual(vs.min(), 240 - 300 * 0.02 / 0.498, delta=1.0)
        self.assertAlmostEqual(vs.max() + 1, 240 + 300 * 0.02 / 0.498, delta=1.0)
        # half-resolution raster + 3 px dilation covers it and stays within ~4 px
        low = hp.render_mask(KC, T_CAM, 640, 480, [(V, F, finger)])
        self.assertTrue(low[mask].all())
        vl, ul = np.nonzero(low)
        self.assertLessEqual(abs(int(ul.min()) - int(us.min())), 5)
        self.assertLessEqual(abs(int(ul.max()) - int(us.max())), 5)

    def test_link_pose_and_fk_finger_opening(self):
        # the same plate as a child of link6 through a prismatic finger joint along y: the mask moves by f*dy/z
        V, F = box([-0.005, -0.005, -0.002], [0.005, 0.005, 0.002])
        link6 = pose([0.0, 0.0, 0.4])
        a = hp.render_mask(KC, T_CAM, 640, 480, [(V, F, link6 @ pose([0, 0.0, 0]))], scale=1.0, dilate_px=0)
        b = hp.render_mask(KC, T_CAM, 640, 480, [(V, F, link6 @ pose([0, 0.02, 0]))], scale=1.0, dilate_px=0)
        dv = np.nonzero(b)[0].mean() - np.nonzero(a)[0].mean()
        self.assertAlmostEqual(dv, 300 * 0.02 / 0.4, delta=0.6)

    def test_behind_camera_and_outside_are_ignored(self):
        V, F = box([-0.01, -0.01, -0.01], [0.01, 0.01, 0.01])
        self.assertFalse(hp.render_mask(KC, T_CAM, 640, 480, [(V, F, pose([0, 0, -0.5]))]).any())
        self.assertFalse(hp.render_mask(KC, T_CAM, 640, 480, [(V, F, pose([5.0, 0, 0.5]))]).any())

    def test_decimate_keeps_the_silhouette(self):
        V, F = box([-0.05, -0.05, -0.05], [0.05, 0.05, 0.05])
        # subdivide each face into many small triangles (a dense mesh) by midpoint splitting twice
        for _ in range(4):
            mids = {}
            newF = []
            Vl = list(map(tuple, V))

            def mid(i, j):
                key = (min(i, j), max(i, j))
                if key not in mids:
                    Vl.append(tuple((np.array(Vl[i]) + np.array(Vl[j])) / 2))
                    mids[key] = len(Vl) - 1
                return mids[key]
            for a, b, c in F:
                ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
                newF += [[a, ab, ca], [ab, b, bc], [ca, bc, c], [ab, bc, ca]]
            V, F = np.array(Vl), np.array(newF)
        V2, F2 = hp.decimate(V, F, cell=0.008)
        self.assertLess(len(F2), len(F))
        T = pose([0, 0, 0.6])
        m1 = hp.render_mask(KC, T_CAM, 640, 480, [(V, F, T)], scale=1.0, dilate_px=0)
        m2 = hp.render_mask(KC, T_CAM, 640, 480, [(V2, F2, T)], scale=1.0, dilate_px=0)
        self.assertGreater((m1 & m2).sum() / (m1 | m2).sum(), 0.9)

    def test_pack_mask_roundtrip(self):
        mask = np.zeros((480, 640), bool)
        mask[10:20, 30:41] = True
        b64, shape = hp.pack_mask(mask)
        bits = np.unpackbits(np.frombuffer(base64.b64decode(b64), np.uint8))[:480 * 640].reshape(480, 640)
        self.assertEqual(shape, [480, 640])
        self.assertTrue((bits.astype(bool) == mask).all())


def held_result(centre, gk=True, px=400, index=None, contour=True):
    out = dict(status="ok", arm="left", mask_source="sam31_text", mask_px=px, centre_m=list(centre), dist_px=0.0,
               grasp_px=[320.0, 240.0], grasp_depth_m=0.5, object_index=index)
    if contour:
        out.update(contour_px=[[300, 230], [340, 230], [340, 250], [300, 250]], label_px=[320, 240])
    out["gk"] = dict(top_z_m=0.95, low_z_m=0.85, length_m=0.12, width_m=0.02, yaw_deg=10.0, elongation=4.0,
                     axis_tilt_deg=5.0) if gk else None
    return out


class HeldRow(unittest.TestCase):
    T6 = pose([0.3, 0.1, 0.9], R=np.array([[0, 0, 1.0], [0, 1.0, 0], [-1.0, 0, 0]]))  # tool axis pointing down

    def test_synthetic_elongated_row(self):
        G = hp.grasp_centre(self.T6)
        np.testing.assert_allclose(G, [0.3, 0.1, 0.9 - 0.1501])
        results = dict(head=held_result(G + [0.01, 0, 0]), wrist=held_result(G + [0.004, 0.003, 0], gk=False))
        obj, meta = hp.held_object(results, "left", self.T6, camera_ids=dict(head="cam_head", wrist="cam_left_wrist"),
                                   modes={"elongated": "kfk"})
        # wrist mask usable -> its centre; extents from the head GK (wrist has none)
        np.testing.assert_allclose(obj["centre_m"], G + [0.004, 0.003, 0])
        self.assertAlmostEqual(obj["top_z_m"] - obj["bottom_z_m"], 0.10)
        self.assertEqual((obj["length_m"], obj["width_m"]), (0.12, 0.02))
        self.assertEqual((obj["support"], obj["quality"]), ("held_left", "held_fk_depth"))
        self.assertEqual((meta["source"], meta["camera_id"], meta["extents"], meta["group"]),
                         ("held_fk_depth", "cam_left_wrist", "gk_head", "elongated"))
        # static prior: link6 z axis (here world -x... sign flipped to point up when it has a z component)
        self.assertEqual(meta["long_axis"]["source"], "static_prior")
        np.testing.assert_allclose(meta["long_axis"]["direction"], [1.0, 0.0, 0.0], atol=1e-3)
        self.assertAlmostEqual(obj["axis_tilt_deg"], 90.0, places=3)
        self.assertIn("p50", meta["note"])
        row = object_table.rows_from([obj])[0]
        self.assertEqual(row[8:], ["held_left", "held_fk_depth"])
        self.assertEqual(row[7], 90)

    def test_held_end_axis_and_small_wrist_mask(self):
        G = hp.grasp_centre(self.T6)
        results = dict(head=held_result(G + [0.01, 0, 0]), wrist=held_result(G, px=50))
        obj, meta = hp.held_object(results, "left", self.T6, camera_ids=dict(head="cam_head", wrist="cam_left_wrist"),
                                   axis_measured=[0, 0, -1.0], modes={"elongated": "kfk"})
        self.assertEqual(meta["camera_id"], "cam_head")                 # wrist mask below WRIST_MIN_MASK_PX
        self.assertEqual(meta["long_axis"], dict(direction=[0.0, 0.0, 1.0], source="held_end"))
        self.assertAlmostEqual(obj["axis_tilt_deg"], 0.0, places=6)
        self.assertEqual(obj["yaw_deg"], 10.0)                           # vertical axis: footprint yaw from the points

    def test_centre_modes_and_compact_prior(self):
        G = hp.grasp_centre(self.T6)
        far = G + [0.05, 0.0, 0.0]
        np.testing.assert_allclose(hp.centre_estimate("clamp", far, G), G + [0.02, 0, 0])
        np.testing.assert_allclose(hp.centre_estimate("clamp", G + [0.01, 0, 0], G), G + [0.01, 0, 0])
        np.testing.assert_allclose(hp.centre_estimate("grasp", far, G), G)
        np.testing.assert_allclose(hp.centre_estimate("kfk", far, G), far)
        compact = held_result(far)
        compact["gk"]["elongation"] = 1.2
        obj, meta = hp.held_object(dict(head=compact), "left", self.T6, camera_ids=dict(head="cam_head"),
                                   modes={"compact": "clamp"})
        np.testing.assert_allclose(obj["centre_m"], G + [0.02, 0, 0])
        self.assertEqual((meta["group"], meta["centre"], meta["long_axis"]), ("compact", "clamp", None))
        self.assertEqual(meta["orientation_prior"], "up_axis_unchanged_since_grasp")
        self.assertIsNone(obj["axis_tilt_deg"])
        # no usable mask: only the 'grasp' estimator still gives a row
        none = dict(head=dict(status="no_mask"))
        self.assertIsNone(hp.held_object(none, "left", self.T6, camera_ids={}, modes={"compact": "kfk"}))
        obj, meta = hp.held_object(none, "left", self.T6, camera_ids={}, modes={"compact": "grasp"})
        np.testing.assert_allclose(obj["centre_m"], G)
        self.assertIsNone(obj["length_m"])

    def test_merge_replaces_the_same_mask_else_appends(self):
        objs = [fake_object((0, 0, 0.05), 0.1, 0.0), fake_object((0.2, 0, 0.9), 0.95, 0.85)]
        G = hp.grasp_centre(self.T6)
        made = hp.held_object(dict(head=held_result(G, index=1)), "left", self.T6, camera_ids=dict(head="cam_head"),
                              modes={"elongated": "kfk"})
        merged, metas = hp.merge(objs, {"left": made}, {"left": dict(object_index=1)})
        self.assertEqual(len(merged), 2)
        self.assertEqual(sorted(metas), [1])
        self.assertEqual(merged[1]["quality"], "held_fk_depth")
        merged, metas = hp.merge(objs, {"left": made}, {"left": dict(object_index=None)})
        self.assertEqual((len(merged), sorted(metas)), (3, [2]))
        # 高空夹持物 2D 掩码遮挡低处桌面目标（objs[0] bottom=0.0 vs held bottom=0.85）时，不得覆写桌面目标
        merged_occ, metas_occ = hp.merge(objs, {"left": made}, {"left": dict(object_index=0)})
        self.assertEqual(len(merged_occ), 3)
        self.assertEqual(merged_occ[0]["quality"], "ok")
        self.assertEqual(merged_occ[2]["quality"], "held_fk_depth")
        self.assertEqual(sorted(metas_occ), [2])

    def test_holding_rejects_empty_partially_open_jaw(self):
        # 正常满闭夹住物体：goal=0.0, opening=0.024m -> True
        self.assertTrue(hp._holding(dict(gripper=dict(position_m=[0.012, 0.012], goal_normalized=0.0))))
        # 空爪半开：goal=0.35, opening=0.031m <= 0.35*0.0888+0.002 -> False
        self.assertFalse(hp._holding(dict(gripper=dict(position_m=[0.0155, 0.0155], goal_normalized=0.35))))
        # 半闭指令但被更宽物体挡住：goal=0.20, opening=0.035m > 0.20*0.0888+0.002 -> True
        self.assertTrue(hp._holding(dict(gripper=dict(position_m=[0.0175, 0.0175], goal_normalized=0.20))))


class HeldService:
    """Fake hp service: /object_table answers two objects (+ held results when asked), /held a wrist result."""

    def __init__(self, held_head=None, held_wrist=None, objects=None, version="fake-hp"):
        self.requests, owner = [], self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((self.path, body))
                if self.path.startswith("/held"):
                    if version == "object-table-v5":
                        self.send_response(404)
                        self.end_headers()
                        return
                    answer = dict(status="ok", held=[held_wrist] if held_wrist else [dict(status="no_mask")])
                else:
                    answer = dict(status="ok", version=version, objects=objects or [
                                  fake_object((0.0, 0.0, 0.05), 0.10, 0.0), fake_object((0.1, 0.05, 0.9), 0.95, 0.85)],
                                  requested=[], omitted=0, labels=[],
                                  support_z_m=body["support_z_m"], dropped={}, n_prompts=1, timing={}, support_fit={},
                                  scale=1.0)
                    if version != "object-table-v5" and body.get("robot_mask_b64"):
                        answer["robot_mask"] = "pixels"
                    if version != "object-table-v5" and body.get("held"):
                        answer["held"] = [held_head]
                        answer["held_s"] = 0.01
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.artifacts = Store(self.tmp.name, IDENTITY); self.addCleanup(self.artifacts.close)
        self.scene = SceneStore(Path(self.tmp.name) / "scene", self.artifacts.resolve_artifact, self.artifacts.put_artifact)
        observation, self.images = two_camera_observation(self.artifacts)
        self.scene.import_observation(observation)

    def runtime(self, endpoint, held=None):
        features = {"external_perception": False, "object_table": True, "vggt_endpoint": endpoint, "vggt_support_z_m": 0.0, "vggt_timeout_s": 5.0}
        if held is not None:
            features["held_perception"] = held
        config = Config().with_overrides({"features": features})
        return SimpleNamespace(config=config, scene=self.scene, image_settings=None, identity=dict(IDENTITY),
                               store=self.artifacts, features=config.features)

    def test_flag_off_request_and_value_identical(self):
        fakes = []
        for held in (None, False):
            fake = HeldService(); self.addCleanup(fake.close); fakes.append(fake)
            response = handlers.geometry_query(self.runtime(fake.url, held),
                                               {"request": dict(kind="object_table", observation_id="obs-ot")}, {})
            self.assertNotIn("held", response.data["value"])
            self.assertNotIn("held_perception", response.data["diagnostics"])
        (p0, b0), (p1, b1) = fakes[0].requests[0], fakes[1].requests[0]
        self.assertEqual((p0, b0), (p1, b1))
        self.assertNotIn("robot_mask_b64", b0)
        self.assertNotIn("held", b0)
        # catalogue: identical with the switch absent, off and on (no new tool, field or description)
        def digest(held):
            config = self.runtime("http://127.0.0.1:9", held).config
            runtime = SimpleNamespace(disabled_tools=(), observation_profile="rgb_proprio_calibrated_v1",
                                      motion_review=True, enable_wait=True, vision_compare=True, visual_reviewer=True,
                                      action_policy=None, capabilities=None, programs=True, config=config,
                                      features=config.features)
            return canonical(create_registry(runtime=runtime).catalog(runtime, role="actor"))
        self.assertEqual(digest(None), digest(False))
        self.assertEqual(digest(None), digest(True))

    def test_flag_on_without_meshes_falls_back_to_capsules(self):
        fake = HeldService(); self.addCleanup(fake.close)
        response = handlers.geometry_query(self.runtime(fake.url, True),
                                           {"request": dict(kind="object_table", observation_id="obs-ot")}, {})
        _, body = fake.requests[0]
        self.assertNotIn("robot_mask_b64", body)
        self.assertEqual(response.data["diagnostics"]["held_perception"]["error"], "robot_meshes_or_fk_unavailable")

    def test_flag_on_mask_sent_and_held_row_merged(self):
        T6 = pose([0.1, 0.05, 0.6], R=np.array([[0, 0, 1.0], [0, 1.0, 0], [-1.0, 0, 0]]))
        G = hp.grasp_centre(T6)
        head = held_result(G + [0.01, 0.0, 0.0], index=1)
        head["arm"] = "left"
        wrist = held_result(G + [0.002, 0.0, 0.0], gk=False)
        fake = HeldService(head, wrist); self.addCleanup(fake.close)
        V, F = box([-0.02, -0.02, -0.02], [0.02, 0.02, 0.02])
        wrist_cam = dict(K=K, T=T_HEAD, width=640, height=480, frame=dict(rgb_ref=None))
        with mock.patch.object(hp, "arm_link_poses", return_value={"left": {"link6": T6}}), \
                mock.patch.object(hp, "mesh_dir", return_value=Path("meshes")), \
                mock.patch.object(hp, "robot_meshes", return_value={"link6": (V, F)}), \
                mock.patch.object(hp, "holding_arms", return_value=["left"]), \
                mock.patch("auriga.robot.model.geometry.GeometryTools.camera", autospec=True,
                           side_effect=lambda self_, identity, oid, cid: (_real_camera(self_, identity, oid, cid)
                                                                          if cid != "cam_left_wrist" else wrist_cam)), \
                mock.patch.object(self.scene, "resolve_artifact",
                                  side_effect=lambda ref: (None, self.images["cam_head"]) if ref is None
                                  else _real_resolve(self.scene, ref)):
            response = handlers.geometry_query(self.runtime(fake.url, True),
                                               {"request": dict(kind="object_table", observation_id="obs-ot")}, {})
        result = response.data
        self.assertEqual(result["status"], "conditional", result)
        path, body = fake.requests[0]
        self.assertEqual((path, body["robot_segments_m"]), ("/object_table", []))
        mask = np.unpackbits(np.frombuffer(base64.b64decode(body["robot_mask_b64"]), np.uint8))[:480 * 640]
        self.assertGreater(int(mask.sum()), 100)          # the link6 box 0.4 m below the head camera
        self.assertEqual(body["held"][0]["arm"], "left")
        np.testing.assert_allclose(body["held"][0]["grasp_centre_m"], G)
        self.assertEqual(fake.requests[1][0], "/held")
        value = result["value"]
        self.assertEqual(len(value["rows"]), 2)           # replaced object o2 (same mask), not appended
        row = value["rows"][1]
        self.assertEqual(row[8:], ["held_left", "held_fk_depth"])
        np.testing.assert_allclose(row[1], np.round(G, 3))       # default estimator: FK grasp centre
        self.assertEqual(value["held"]["o2"]["centre"], "grasp")
        self.assertEqual(value["held"]["o2"]["camera_id"], "cam_left_wrist")
        self.assertIn("held_perception", result["diagnostics"])

    def test_v5_service_graceful_degradation(self):
        """When the service is an un-restarted object-table-v5 (no robot_mask/held support), held_perception=True
        re-queries with robot_segments_m if needed, skips POST /held, and produces a valid FK held row without crashing
        grasp_hints_from."""
        T6 = pose([0.1, 0.05, 0.6], R=np.array([[0, 0, 1.0], [0, 1.0, 0], [-1.0, 0, 0]]))
        G = hp.grasp_centre(T6)
        fake = HeldService(version="object-table-v5"); self.addCleanup(fake.close)
        V, F = box([-0.02, -0.02, -0.02], [0.02, 0.02, 0.02])
        with mock.patch.object(hp, "arm_link_poses", return_value={"left": {"link6": T6}}), \
                mock.patch.object(hp, "mesh_dir", return_value=Path("meshes")), \
                mock.patch.object(hp, "robot_meshes", return_value={"link6": (V, F)}), \
                mock.patch.object(hp, "holding_arms", return_value=["left"]), \
                mock.patch.object(object_table, "robot_segments", return_value=[[[0.0, 0.0, 0.0], [0.1, 0.0, 0.5]]]):
            response = handlers.geometry_query(self.runtime(fake.url, True),
                                               {"request": dict(kind="object_table", observation_id="obs-ot")}, {})
        result = response.data
        self.assertEqual(result["status"], "conditional", result)
        # Re-queried with robot_segments_m and never called /held (which would 404 on v5)
        self.assertEqual([p for p, _ in fake.requests], ["/object_table", "/object_table"])
        self.assertEqual(len(fake.requests[1][1]["robot_segments_m"]), 1)
        value = result["value"]
        self.assertEqual(len(value["rows"]), 3)           # appended FK grasp-centre held row o3
        self.assertEqual(value["rows"][2][8:], ["held_left", "held_fk_depth"])
        np.testing.assert_allclose(value["rows"][2][1], np.round(G, 3))
        self.assertEqual(result["diagnostics"]["held_perception"]["service_fallback"], "object-table-v5_no_hp1")

    def test_contact_attribution_skips_held_rows(self):
        from auriga.runtime.actions import action_state_ledger
        table = {
            "columns": list(object_table.COLUMNS),
            "rows": [
                ["o1", [0.10, 0.05, 0.85], 0.90, 0.80, 0.12, 0.03, 0, 90, "held_left", "held_fk_depth"],
                ["o2", [0.12, 0.05, 0.78], 0.80, 0.76, 0.08, 0.06, 0, None, "table", "ok"],
            ],
        }
        harness = SimpleNamespace(_obs3d_memo=("obs-1", {"object_table": (table, None)}))
        attr = action_state_ledger.attribute_contact_block_3d(
            harness,
            {"based_on_observation_id": "obs-1"},
            {"contact_block_summary": {"actual_contact_position_m": [0.10, 0.05, 0.81]}},
        )
        self.assertEqual(attr["id"], "o2")


# ------------------------------------------------------------------ v2: pre-grasp row carried in the link6 frame
R_DOWN = np.array([[0, 0, 1.0], [0, 1.0, 0], [-1.0, 0, 0]])      # link6 x (approach) pointing down, y = world y
R_Z90 = np.array([[0, -1.0, 0], [1.0, 0, 0], [0, 0, 1.0]])


def ee_simple(T):
    """Quaternion xyzw of a 4x4 pose without scipy (Shepperd)."""
    R = np.asarray(T, float)[:3, :3]
    w = math.sqrt(max(0.0, 1 + R[0, 0] + R[1, 1] + R[2, 2])) / 2
    x = math.sqrt(max(0.0, 1 + R[0, 0] - R[1, 1] - R[2, 2])) / 2
    y = math.sqrt(max(0.0, 1 - R[0, 0] + R[1, 1] - R[2, 2])) / 2
    z = math.sqrt(max(0.0, 1 - R[0, 0] - R[1, 1] + R[2, 2])) / 2
    x = math.copysign(x, R[2, 1] - R[1, 2])
    y = math.copysign(y, R[0, 2] - R[2, 0])
    z = math.copysign(z, R[1, 0] - R[0, 1])
    return dict(position_m=[float(v) for v in np.asarray(T)[:3, 3]], quaternion_xyzw=[x, y, z, w])


def arm_state(T6, holding):
    return dict(ee_pose=ee_simple(T6), gripper=dict(position_m=[0.012, 0.012] if holding else [0.044, 0.044],
                                                     goal_normalized=0.0 if holding else 1.0))


class FakeRecords:
    """record_page('observation') + resolve_artifact over a list of (observation_id, sim_step, {arm: state})."""

    def __init__(self):
        self.items, self.states = [], {}

    def add(self, oid, step, arms):
        ref = "artifact:state-%s" % oid
        self.states[ref] = json.dumps(dict(arms=arms)).encode()
        self.items.append(dict(id=oid, value=dict(observation_id=oid, sim_step=step, robot_state_ref=ref),
                               position=len(self.items) + 1))

    def record_page(self, kind, after=0, limit=500):
        page = [e for e in self.items if e["position"] > after][:limit]
        return dict(entries=page, cursor=page[-1]["position"] if page else after,
                    has_more=len([e for e in self.items if e["position"] > after]) > limit, reset=False)

    def resolve_artifact(self, ref, allowed_scopes=("actor",)):
        return {}, self.states[ref]


def pregrasp_table(rows, step=10, oid="obs-pre", arms=("left", "right"), T6=None):
    return dict(observation_id=oid, sim_step=step, camera_id="cam_head", K=K, T=T_HEAD, rows=rows,
                free_arms=list(arms), link6={a: (T6 if T6 is not None else np.eye(4)).tolist() for a in arms})


def table_row(index, centre, length=0.12, width=0.02, top=0.80, bottom=0.78, yaw=30.0, tilt=88.0, contour=None):
    return dict(index=index, centre_m=list(centre), length_m=length, width_m=width, top_z_m=top, bottom_z_m=bottom,
                yaw_deg=yaw, axis_tilt_deg=tilt, elongation=length / width, contour_px=contour or [], flags=[],
                quality="ok", support="table", label=None)


class PregraspCarry(unittest.TestCase):
    T6c = pose([0.11, 0.06, 0.94], R=R_DOWN)                       # closure: grasp centre (0.11, 0.06, 0.7899)

    def runtime(self, records=None):
        return SimpleNamespace(store=records or FakeRecords())

    def test_carried_geometry_does_not_claim_retention_without_current_mask(self):
        G = hp.grasp_centre(self.T6c)
        table = pregrasp_table([table_row(0, [G[0], G[1], .79])])
        carried, why = hp.capture(dict(tables=[table]), "left",
                                 dict(observation_id="obs-c", sim_step=20, T6=self.T6c.tolist()))
        self.assertIsNone(why)
        T6 = self._lifted()
        for results, evidence in [
                (dict(head=dict(status="no_mask"), wrist=dict(status="no_mask")), "prediction_only"),
                (dict(head=held_result(hp.grasp_centre(T6)), wrist=dict(status="no_mask")), "current_mask")]:
            obj, meta = hp.held_object(results, "left", T6, camera_ids={}, carried=carried)
            self.assertIsNotNone(obj)
            self.assertEqual(meta["geometry_evidence"], evidence)
            self.assertEqual(meta["retention_status"], "unverified")
            self.assertEqual(meta["attachment_assumption"], "rigid_since_grasp")

    def test_groups(self):
        self.assertEqual(hp.pregrasp_group(0.12, 0.02), "tube")
        self.assertEqual(hp.pregrasp_group(0.09, 0.025), "tube")
        self.assertEqual(hp.pregrasp_group(0.08, 0.03), "elongated")    # 2.67 x
        self.assertEqual(hp.pregrasp_group(0.15, 0.03), "elongated")
        self.assertEqual(hp.pregrasp_group(0.06, 0.04), "compact")
        self.assertEqual(hp.pregrasp_group(None, 0.04), "compact")

    def test_capture_offset_jaw_snap_and_rigid_carry(self):
        # row yaw 25 deg: its axis is 25 deg from the static prior (world x here), inside the W1 gate
        memory = dict(tables=[pregrasp_table([table_row(0, [0.40, 0.0, 0.79]),
                                              table_row(2, [0.10, 0.05, 0.79], yaw=25.0)])])
        carried, why = hp.capture(memory, "left", dict(observation_id="obs-c", sim_step=20, T6=self.T6c.tolist()))
        self.assertIsNone(why)
        self.assertEqual((carried["row_id"], carried["group"], carried["tables_back"]), ("o3", "tube", 0))
        # row centre - link6 = (-0.01, -0.01, -0.15) -> link6 frame (0.15, -0.01, -0.01); across-jaw (y) -> 0
        np.testing.assert_allclose(carried["offset_row_local"], [0.15, -0.01, -0.01], atol=1e-9)
        np.testing.assert_allclose(carried["offset_local"], [0.15, 0.0, -0.01], atol=1e-9)
        self.assertAlmostEqual(carried["height"], 0.02)
        # lifted 0.2 m and turned 90 deg about world z: centre and axis move with link6
        T6 = pose(self.T6c[:3, 3] + [0, 0, 0.2], R=R_Z90 @ R_DOWN)
        results = dict(head=held_result(hp.grasp_centre(T6)), wrist=dict(status="no_mask"))
        obj, meta = hp.held_object_carried(results, "left", T6, camera_ids=dict(head="cam_head", wrist="cam_left_wrist"),
                                           carried=carried, modes={"tube": "carried"})
        np.testing.assert_allclose(obj["centre_m"], T6[:3, 3] + T6[:3, :3] @ [0.15, 0.0, -0.01], atol=1e-9)
        self.assertEqual((obj["length_m"], obj["width_m"]), (0.12, 0.02))
        self.assertAlmostEqual(obj["top_z_m"] - obj["bottom_z_m"], 0.02)
        self.assertEqual((meta["extent_source"], meta["pregrasp_row_id"], meta["centre"], meta["group"]),
                         ("pregrasp_row", "o3", "carried", "tube"))
        self.assertEqual(hp.held_object(results, "left", T6, camera_ids={}, carried=carried)[1]["centre"],
                         "carried_axis")                                  # default for tube-like rows
        self.assertIn("pregrasp_row_id=o3 of obs-pre", meta["note"])
        self.assertIn("extent_source=pregrasp_row", meta["note"])
        # tube: shipped axis = static prior (link6 z, perpendicular to the jaws); carried axis also given
        axis = meta["long_axis"]
        self.assertEqual(axis["source"], "static_prior")
        np.testing.assert_allclose(axis["direction"], hp._unit_up(T6[:3, 2]), atol=1e-3)
        expected = hp._unit_up(T6[:3, :3] @ self.T6c[:3, :3].T @ hp.axis_from(25.0, 88.0))
        np.testing.assert_allclose(axis["carried"], expected, atol=1e-3)
        # AXIS_MODE 'carried' ships the carried axis
        wide = dict(carried, group="elongated")
        _, meta2 = hp.held_object_carried(results, "left", T6, camera_ids={}, carried=wide,
                                          axis_modes={"elongated": "carried"})
        self.assertEqual(meta2["long_axis"]["source"], "pregrasp_row_carried")
        np.testing.assert_allclose(meta2["long_axis"]["direction"], expected, atol=1e-3)
        # carried_axis (default): grasp centre + along-axis part of the carried offset only
        obj3, _ = hp.held_object(results, "left", T6, camera_ids={}, carried=carried)
        G = hp.grasp_centre(T6)
        a = np.asarray(axis["direction"], float)
        a = a / np.linalg.norm(a)
        c = T6[:3, 3] + T6[:3, :3] @ [0.15, 0.0, -0.01]
        np.testing.assert_allclose(obj3["centre_m"], G + np.dot(c - G, a) * a, atol=2e-3)

    def test_box_extents_follow_a_reorientation(self):
        # tube lying along world x (row yaw 0), grasped top-down across it; then turned so that link6 z points up
        memory = dict(tables=[pregrasp_table([table_row(0, [0.11, 0.06, 0.79], length=0.12, width=0.02, top=0.80,
                                                        bottom=0.78, yaw=0.0, tilt=90.0)])])
        carried, _ = hp.capture(memory, "left", dict(observation_id="obs-c", sim_step=20, T6=self.T6c.tolist()))
        Ry = np.array([[0, 0, -1.0], [0, 1.0, 0], [1.0, 0, 0]])         # about world y by -90 deg: x -> z
        T6 = pose(self.T6c[:3, 3] + [0, 0, 0.2], R=Ry @ R_DOWN)
        np.testing.assert_allclose(T6[:3, 2], [0, 0, 1.0], atol=1e-9)
        obj, meta = hp.held_object(dict(head=dict(status="no_mask")), "left", T6, camera_ids={}, carried=carried)
        self.assertAlmostEqual(obj["length_m"], 0.02, places=3)
        self.assertAlmostEqual(obj["width_m"], 0.02, places=3)
        self.assertAlmostEqual(obj["top_z_m"] - obj["bottom_z_m"], 0.12, places=6)
        self.assertAlmostEqual(obj["axis_tilt_deg"], 0.0, places=6)
        self.assertIn("turned with the gripper", meta["note"])
        # EXTENT_CARRY 'row': the row values unchanged
        with mock.patch.object(hp, "EXTENT_CARRY", "row"):
            obj2, _ = hp.held_object(dict(head=dict(status="no_mask")), "left", T6, camera_ids={}, carried=carried)
        self.assertEqual((obj2["length_m"], obj2["width_m"]), (0.12, 0.02))
        self.assertAlmostEqual(obj2["top_z_m"] - obj2["bottom_z_m"], 0.02)

    def test_compact_keeps_the_grasp_centre_and_carries_the_footprint_yaw(self):
        memory = dict(tables=[pregrasp_table([table_row(0, [0.105, 0.065, 0.79], length=0.05, width=0.04, yaw=20.0)])])
        carried, _ = hp.capture(memory, "left", dict(observation_id="obs-c", sim_step=20, T6=self.T6c.tolist()))
        self.assertEqual(carried["group"], "compact")
        T6 = pose(self.T6c[:3, 3] + [0, 0, 0.1], R=R_Z90 @ R_DOWN)
        obj, meta = hp.held_object(dict(head=dict(status="no_mask")), "left", T6, camera_ids={}, carried=carried)
        np.testing.assert_allclose(obj["centre_m"], hp.grasp_centre(T6))
        self.assertAlmostEqual(obj["yaw_deg"], -70.0, places=6)          # 20 + 90 wrapped to [-90, 90)
        np.testing.assert_allclose([obj["length_m"], obj["width_m"]], [0.05, 0.04], atol=1e-9)
        self.assertIsNone(meta["long_axis"])
        self.assertEqual(meta["orientation_prior"], "up_axis_unchanged_since_grasp")

    def test_row_choice_mask_containment_then_distance(self):
        G = hp.grasp_centre(self.T6c)
        uv = hp._project(K, T_HEAD, G)
        box_px = [[uv[0] - 5, uv[1] - 5], [uv[0] + 5, uv[1] - 5], [uv[0] + 5, uv[1] + 5], [uv[0] - 5, uv[1] + 5]]
        near = table_row(0, [G[0] + 0.005, G[1], 0.79])
        containing = table_row(1, [G[0] + 0.03, G[1], 0.79], contour=box_px)
        far = table_row(2, [G[0] + 0.07, G[1], 0.79], contour=box_px)       # beyond 6 cm: never
        row, inside, d = hp.pregrasp_row(pregrasp_table([near, containing, far]), G)
        self.assertEqual((row["index"], inside), (1, True))
        self.assertAlmostEqual(d, 0.03, places=6)
        row, inside, _ = hp.pregrasp_row(pregrasp_table([near, far]), G)
        self.assertEqual((row["index"], inside), (0, False))
        self.assertIsNone(hp.pregrasp_row(pregrasp_table([far]), G))

    def test_newest_pre_closure_table_and_fallback_reasons(self):
        closure = dict(observation_id="obs-c", sim_step=20, T6=self.T6c.tolist())
        G = hp.grasp_centre(self.T6c)
        old = pregrasp_table([table_row(0, [G[0], G[1], 0.79], length=0.2)], step=5, oid="obs-old")
        newer = pregrasp_table([table_row(0, [G[0] + 0.2, G[1], 0.79])], step=15, oid="obs-new")
        after = pregrasp_table([table_row(0, [G[0], G[1], 0.79])], step=25, oid="obs-after")
        right_only = pregrasp_table([table_row(0, [G[0], G[1], 0.79])], step=18, oid="obs-r", arms=("right",))
        carried, _ = hp.capture(dict(tables=[old, newer, right_only, after]), "left", closure)
        self.assertEqual((carried["observation_id"], carried["tables_back"], carried["length"]), ("obs-old", 1, 0.2))
        self.assertEqual(hp.capture(dict(tables=[newer]), "left", closure), (None, "no_pregrasp_row_within_6cm"))
        self.assertEqual(hp.capture(dict(tables=[after]), "left", closure), (None, "no_pregrasp_table"))
        # v1 fallback row says why and has no pre-grasp row id
        T6 = pose(self.T6c[:3, 3] + [0, 0, 0.2], R=R_DOWN)
        obj, meta = hp.held_object(dict(head=held_result(hp.grasp_centre(T6)), wrist=dict(status="no_mask")), "left",
                                   T6, camera_ids=dict(head="cam_head"), carried=None,
                                   fallback="no_pregrasp_row_within_6cm")
        np.testing.assert_allclose(obj["centre_m"], hp.grasp_centre(T6))
        self.assertEqual((obj["length_m"], meta["source"], meta["pregrasp_row_id"], meta["extent_source"]),
                         (0.12, "held_fk_depth", None, "gk_head"))
        self.assertIn("v1 estimate: no pregrasp row within 6cm, pregrasp_row_id=none, extent_source=gk_head",
                      meta["note"])

    def test_track_closure_release_regrasp_and_replayed_frames(self):
        records = FakeRecords()
        runtime = self.runtime(records)
        lifted = pose(self.T6c[:3, 3] + [0, 0, 0.1], R=R_DOWN)
        free = arm_state(pose([0.3, -0.2, 1.0], R=R_DOWN), False)
        records.add("o0", 0, dict(left=arm_state(lifted, False), right=free))
        records.add("o1", 50, dict(left=arm_state(self.T6c, True), right=free))     # closure
        records.add("o2", 100, dict(left=arm_state(lifted, True), right=free))
        records.add("o-replay", 10, dict(left=arm_state(lifted, False), right=free))  # replayed frame: skipped
        memory = hp.track(runtime)
        streak = memory["arms"]["left"]
        self.assertTrue(streak["holding"])
        self.assertEqual((streak["closure"]["observation_id"], streak["closure"]["sim_step"]), ("o1", 50))
        np.testing.assert_allclose(streak["closure"]["T6"], self.T6c, atol=1e-9)
        self.assertFalse(memory["arms"]["right"]["holding"])
        # capture once per streak (cached), from a table recorded before the closure
        G = hp.grasp_centre(self.T6c)
        hp.record_table(runtime, "o0", 0, "cam_head", dict(K=K, T=T_HEAD),
                        [fake_object((G[0], G[1], 0.79), 0.80, 0.78)], {"left": {"link6": lifted}}, ["left", "right"])
        carried, why = hp.carried_for(runtime, "left", 100)
        self.assertEqual((carried["observation_id"], why), ("o0", None))
        self.assertIs(hp.carried_for(runtime, "left", 100)[0], carried)
        self.assertEqual(hp.carried_for(runtime, "left", 40), (None, "observation_before_closure"))
        self.assertEqual(hp.carried_for(runtime, "right", 100), (None, "no_closure_observation"))
        # release drops the carried state; a regrasp starts a new streak and captures again
        records.add("o3", 150, dict(left=arm_state(lifted, False), right=free))
        hp.track(runtime)
        self.assertNotIn("left", memory["carried"])
        self.assertEqual(hp.carried_for(runtime, "left", 150), (None, "no_closure_observation"))
        T6b = pose(self.T6c[:3, 3] + [0.3, 0, 0], R=R_DOWN)
        records.add("o4", 200, dict(left=arm_state(T6b, True), right=free))
        hp.track(runtime)
        self.assertEqual(memory["arms"]["left"]["closure"]["observation_id"], "o4")
        self.assertEqual(hp.carried_for(runtime, "left", 200), (None, "no_pregrasp_row_within_6cm"))
        # no record API (e.g. a replay without observation records): nothing tracked, v1 path
        bare = SimpleNamespace(store=object())
        self.assertEqual(hp.track(bare)["arms"], {})
        self.assertEqual(hp.carried_for(bare, "left"), (None, "no_closure_observation"))

    def test_record_table_skips_held_rows_and_keeps_the_newest(self):
        runtime = self.runtime()
        held = dict(fake_object((0.1, 0.1, 0.9), 0.95, 0.85), quality=hp.QUALITY, support="held_left")
        for step in range(12):
            hp.record_table(runtime, "o%d" % step, step, "cam_head", dict(K=K, T=T_HEAD),
                            [fake_object((0.0, 0.0, 0.05), 0.1, 0.0), held], {"right": {"link6": np.eye(4)}},
                            ["right"])
        tables = hp.tracker(runtime)["tables"]
        self.assertEqual([t["sim_step"] for t in tables], list(range(12 - hp.TABLES_KEPT, 12)))
        self.assertEqual([r["index"] for r in tables[-1]["rows"]], [0])
        hp.record_table(runtime, "x", 99, "cam_head", dict(K=K, T=T_HEAD), [], {}, [])     # no free arm: not kept
        self.assertEqual(tables[-1]["sim_step"], 11)

    # ---- v2.1 (hp21): W1 axis-consistency gate, W2 partial-row guard, W3 row metadata
    def _lifted(self):
        return pose(self.T6c[:3, 3] + [0, 0, 0.2], R=R_DOWN)

    def test_axis_conflict_gate_keeps_the_grasp_centre(self):
        # R_DOWN: link6 z (static prior for a lying row) = world x. Row centre 2 cm along world x from the grasp point.
        G = hp.grasp_centre(self.T6c)
        across = pregrasp_table([table_row(0, [G[0] + 0.02, G[1], 0.79], yaw=0.0, tilt=90.0)])    # axis = world x
        along = pregrasp_table([table_row(0, [G[0] + 0.02, G[1], 0.79], yaw=90.0, tilt=90.0)])    # axis = jaw line
        closure = dict(observation_id="obs-c", sim_step=20, T6=self.T6c.tolist())
        T6 = self._lifted()
        results = dict(head=held_result(hp.grasp_centre(T6)), wrist=dict(status="no_mask"))
        ok, _ = hp.capture(dict(tables=[across]), "left", closure)
        obj, meta = hp.held_object(results, "left", T6, camera_ids={}, carried=ok)
        self.assertEqual(meta["centre"], "carried_axis")
        self.assertNotIn("axis_conflict_deg", meta)
        np.testing.assert_allclose(obj["centre_m"], hp.grasp_centre(T6) + [0.02, 0, 0], atol=1e-6)
        bad, _ = hp.capture(dict(tables=[along]), "left", closure)
        obj, meta = hp.held_object(results, "left", T6, camera_ids={}, carried=bad)
        self.assertEqual(meta["centre"], "grasp")                       # no shift along the (wrong) prior axis
        np.testing.assert_allclose(obj["centre_m"], hp.grasp_centre(T6), atol=1e-9)
        self.assertAlmostEqual(meta["axis_conflict_deg"], 90.0, places=3)
        self.assertEqual(meta["long_axis"]["source"], "pregrasp_row_carried")
        np.testing.assert_allclose(np.abs(meta["long_axis"]["direction"]), [0, 1.0, 0], atol=1e-3)
        self.assertIn("axis_conflict=90 deg", meta["note"])
        self.assertIn("no shift along the axis", meta["note"])
        self.assertIn("error p50 ~9 deg", meta["note"])                 # carried-axis uncertainty (tube)
        self.assertAlmostEqual(obj["length_m"], 0.12, places=6)        # extents still from the pre-grasp row
        self.assertEqual(meta["extent_source"], "pregrasp_row")
        # v2 behaviour selectable: no gate -> shifted along the prior axis (world x, the 2 cm row offset)
        with mock.patch.object(hp, "AXIS_CONFLICT_DEG", None):
            obj2, meta2 = hp.held_object(results, "left", T6, camera_ids={}, carried=dict(bad))
        self.assertEqual((meta2["centre"], meta2["long_axis"]["source"]), ("carried_axis", "static_prior"))
        np.testing.assert_allclose(obj2["centre_m"], hp.grasp_centre(T6) + [0.02, 0, 0], atol=1e-6)
        self.assertNotIn("axis_conflict_deg", meta2)
        # 'carried_axis' option: shift along the carried axis (world y here; the row offset is along x -> no shift)
        with mock.patch.object(hp, "AXIS_CONFLICT_CENTRE", "carried_axis"):
            obj4, meta4 = hp.held_object(results, "left", T6, camera_ids={}, carried=dict(bad))
        self.assertEqual((meta4["centre"], meta4["long_axis"]["source"]), ("carried_axis", "pregrasp_row_carried"))
        np.testing.assert_allclose(obj4["centre_m"], hp.grasp_centre(T6), atol=1e-6)
        # 40 deg: above the task-sheet threshold (30), below the shipped one (45)
        tilted = pregrasp_table([table_row(0, [G[0] + 0.02, G[1], 0.79], yaw=40.0, tilt=90.0)])
        mid, _ = hp.capture(dict(tables=[tilted]), "left", closure)
        self.assertNotIn("axis_conflict_deg", hp.held_object(results, "left", T6, camera_ids={}, carried=dict(mid))[1])
        with mock.patch.object(hp, "AXIS_CONFLICT_DEG", 30.0):
            self.assertAlmostEqual(hp.held_object(results, "left", T6, camera_ids={}, carried=dict(mid))[1]
                                   ["axis_conflict_deg"], 40.0, places=1)

    def test_partial_row_guard_is_off_in_the_shipped_setting(self):
        G = hp.grasp_centre(self.T6c)
        closure = dict(observation_id="obs-c", sim_step=20, T6=self.T6c.tolist())
        T6 = self._lifted()
        results = dict(head=held_result(hp.grasp_centre(T6)), wrist=dict(status="no_mask"))
        short = pregrasp_table([table_row(0, [G[0], G[1], 0.79], length=0.05, width=0.02)])
        carried, _ = hp.capture(dict(tables=[short]), "left", closure)
        _, meta = hp.held_object(results, "left", T6, camera_ids={}, carried=carried)
        self.assertEqual(meta["extent_source"], "pregrasp_row")

    def test_partial_row_falls_back_to_gk(self):     # task-sheet setting (ratio 0.8, outline test, mask + GK)
        with mock.patch.multiple(hp, PARTIAL_ROW_RATIO=0.8, PARTIAL_ROW_CONTAINS=True, PARTIAL_ROW_REF=("mask", "gk"), GROUP_FROM_MAX=True):
            self._partial_row_falls_back_to_gk()

    def _partial_row_falls_back_to_gk(self):
        G = hp.grasp_centre(self.T6c)
        closure = dict(observation_id="obs-c", sim_step=20, T6=self.T6c.tolist())
        T6 = self._lifted()
        results = dict(head=held_result(hp.grasp_centre(T6)), wrist=dict(status="no_mask"))   # GK 0.12 x 0.02, h 0.10
        short = pregrasp_table([table_row(0, [G[0], G[1], 0.79], length=0.05, width=0.02)])
        carried, _ = hp.capture(dict(tables=[short]), "left", closure)
        self.assertIsNone(carried["closure_length_m"])
        obj, meta = hp.held_object(results, "left", T6, camera_ids=dict(head="cam_head"), carried=carried)
        self.assertAlmostEqual(carried["closure_length_m"], 0.12)       # max(GK length, GK height), set once
        self.assertEqual((meta["source"], meta["extent_source"], meta["fallback"], meta["pregrasp_row_id"]),
                         ("held_fk_depth", "gk_partial_row", "gk_partial_row", None))
        self.assertEqual(obj["length_m"], 0.12)                         # v1 (GK) extents
        self.assertEqual(meta["partial_pregrasp_row"]["reasons"], ["short"])
        self.assertEqual(meta["partial_pregrasp_row"]["row_id"], "o1")
        self.assertIn("extent_source=gk_partial_row", meta["note"])
        self.assertIn("length 0.050 m < 0.8 x 0.120 m", meta["note"])
        # v2 behaviour selectable
        with mock.patch.object(hp, "PARTIAL_ROW_RATIO", None):
            _, meta2 = hp.held_object(results, "left", T6, camera_ids={}, carried=carried)
        self.assertEqual(meta2["extent_source"], "pregrasp_row")
        # a row whose mask outline exists and misses the projected grasp centre is partial too
        uv = hp._project(K, T_HEAD, G)
        off = [[uv[0] + 40, uv[1] - 5], [uv[0] + 60, uv[1] - 5], [uv[0] + 60, uv[1] + 5], [uv[0] + 40, uv[1] + 5]]
        outside = pregrasp_table([table_row(0, [G[0] + 0.01, G[1], 0.79], contour=off)])
        carried, _ = hp.capture(dict(tables=[outside]), "left", closure)
        self.assertEqual((carried["contains"], carried["has_contour"]), (False, True))
        self.assertIsNotNone(carried["mask_length_m"])
        _, meta = hp.held_object(results, "left", T6, camera_ids={}, carried=carried)
        self.assertIn("outline", meta["partial_pregrasp_row"]["reasons"])
        with mock.patch.object(hp, "PARTIAL_ROW_CONTAINS", False), mock.patch.object(hp, "PARTIAL_ROW_REF", ()):
            _, meta3 = hp.held_object(results, "left", T6, camera_ids={}, carried=dict(carried, closure_length_m=None))
        self.assertEqual(meta3["extent_source"], "pregrasp_row")

    def test_group_from_the_longer_of_row_and_reference(self):
        with mock.patch.multiple(hp, PARTIAL_ROW_REF=("mask", "gk"), GROUP_FROM_MAX=True):
            self._group_from_the_longer_of_row_and_reference()

    def _group_from_the_longer_of_row_and_reference(self):
        G = hp.grasp_centre(self.T6c)
        closure = dict(observation_id="obs-c", sim_step=20, T6=self.T6c.tolist())
        T6 = self._lifted()
        results = dict(head=held_result(hp.grasp_centre(T6)), wrist=dict(status="no_mask"))   # reference 0.12
        row = pregrasp_table([table_row(0, [G[0], G[1], 0.79], length=0.10, width=0.045)])
        carried, _ = hp.capture(dict(tables=[row]), "left", closure)
        self.assertEqual(carried["group"], "compact")                   # 0.10 < 2.5 x 0.045
        _, meta = hp.held_object(results, "left", T6, camera_ids={}, carried=carried)
        self.assertEqual((meta["group"], meta["extent_source"]), ("elongated", "pregrasp_row"))   # 0.12 >= 0.1125
        with mock.patch.object(hp, "GROUP_FROM_MAX", False):
            _, meta2 = hp.held_object(results, "left", T6, camera_ids={}, carried=carried)
        self.assertEqual(meta2["group"], "compact")

    def test_mask_length_on_the_row_plane(self):
        # a 0.12 m x 0.02 m footprint on the plane z = 0.79, projected into the head camera and back
        corners = [[0.0, -0.01, 0.79], [0.12, -0.01, 0.79], [0.12, 0.01, 0.79], [0.0, 0.01, 0.79]]
        contour = [list(hp._project(K, T_HEAD, c)) for c in corners]
        self.assertAlmostEqual(hp.mask_length(K, T_HEAD, contour, 0.79), 0.12, places=6)
        self.assertIsNone(hp.mask_length(K, T_HEAD, [], 0.79))

    def test_row_metadata_names_the_table_and_row_centre(self):
        runtime = self.runtime()
        G = hp.grasp_centre(self.T6c)
        uv = [int(round(v)) for v in hp._project(K, T_HEAD, G)]           # mask outline around the grasp point
        hp.record_table(runtime, "o0", 5, "cam_head", dict(K=K, T=T_HEAD),
                        [fake_object((G[0], G[1], 0.79), 0.80, 0.78, label=uv)],
                        {"left": {"link6": np.eye(4)}}, ["left"], source=dict(seq=7, request="labelled"))
        carried, _ = hp.capture(hp.tracker(runtime), "left", dict(observation_id="obs-c", sim_step=20,
                                                                  T6=self.T6c.tolist()))
        T6 = self._lifted()
        _, meta = hp.held_object(dict(head=held_result(hp.grasp_centre(T6))), "left", T6, camera_ids={},
                                 carried=carried)
        self.assertEqual(meta["pregrasp_row_centre_m"], [round(G[0], 3), round(G[1], 3), 0.79])
        self.assertEqual(meta["pregrasp_table_source"], dict(seq=7, request="labelled", observation_id="o0",
                                                             camera_id="cam_head"))
        self.assertIn("pregrasp_row_id=o1 of o0, object table #7, labelled request, row centre", meta["note"])


class IntegrationV2(unittest.TestCase):
    """Two object_table requests through geometry_query: a free arm (table kept), then the same arm holding (held
    row from the carried pre-grasp row: extents of that row, centre carried with link6)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.artifacts = Store(self.tmp.name, IDENTITY); self.addCleanup(self.artifacts.close)
        self.scene = SceneStore(Path(self.tmp.name) / "scene", self.artifacts.resolve_artifact, self.artifacts.put_artifact)
        observation, self.images = two_camera_observation(self.artifacts)
        self.scene.import_observation(observation)
        later = json.loads(json.dumps(observation))
        later.update(observation_id="obs-ot2", sim_step=100, robot_state_ref=self.artifacts.put_artifact(canonical(
            {"source_kind": "measurement", "sim_step": 100, "step_domain": "control_step"}).encode(), "robot_state",
            "application/json"))
        for camera in later["cameras"]:
            camera["sim_step"] = 100
        self.scene.import_observation(later)

    def test_capture_path_end_to_end(self):
        T6c = pose([0.11, 0.06, 1.05], R=R_DOWN)                 # closure: grasp centre (0.11, 0.06, 0.8999)
        T6 = pose([0.11, 0.06, 1.25], R=R_Z90 @ R_DOWN)           # lifted and turned
        right = arm_state(pose([0.3, -0.2, 1.2], R=R_DOWN), False)
        for oid, step, left in (("obs-ot", 0, arm_state(pose([0.11, 0.06, 1.15], R=R_DOWN), False)),
                                ("obs-close", 50, arm_state(T6c, True)), ("obs-ot2", 100, arm_state(T6, True))):
            ref = self.artifacts.put_artifact(json.dumps(dict(arms=dict(left=left, right=right))).encode(),
                                              "robot_state", "application/json")
            self.artifacts.save("observation", oid, dict(observation_id=oid, sim_step=step, robot_state_ref=ref))
        head = held_result(hp.grasp_centre(T6), index=None)
        # row o2 yaw 20 deg: 20 deg from the static prior (world x), inside the v2.1 axis gate (30 deg)
        objects = [fake_object((0.0, 0.0, 0.05), 0.10, 0.0), dict(fake_object((0.1, 0.05, 0.9), 0.95, 0.85), yaw_deg=20.0)]
        fake = HeldService(head, None, objects=objects); self.addCleanup(fake.close)
        V, F = box([-0.02, -0.02, -0.02], [0.02, 0.02, 0.02])
        features = {"object_table": True, "vggt_endpoint": fake.url, "vggt_support_z_m": 0.0, "vggt_timeout_s": 5.0,
                    "held_perception": True}
        config = Config().with_overrides({"features": features})
        runtime = SimpleNamespace(config=config, scene=self.scene, image_settings=None, identity=dict(IDENTITY),
                                  store=self.artifacts, features=config.features)
        phase = {"holding": False, "T6": pose([0.11, 0.06, 1.15], R=R_DOWN)}
        with mock.patch.object(hp, "arm_link_poses", side_effect=lambda *a: {"left": {"link6": phase["T6"]},
                                                                           "right": {"link6": np.eye(4)}}), \
                mock.patch.object(hp, "mesh_dir", return_value=Path("meshes")), \
                mock.patch.object(hp, "robot_meshes", return_value={"link6": (V, F)}), \
                mock.patch.object(hp, "holding_arms", side_effect=lambda state: ["left"] if phase["holding"] else []), \
                mock.patch.object(hp.Prepared, "wrist_results", return_value={}):
            first = handlers.geometry_query(runtime, {"request": dict(kind="object_table", observation_id="obs-ot")}, {})
            self.assertNotIn("held", first.data["value"])
            self.assertEqual([t["observation_id"] for t in hp.tracker(runtime)["tables"]], ["obs-ot"])
            phase.update(holding=True, T6=T6)
            second = handlers.geometry_query(runtime, {"request": dict(kind="object_table", observation_id="obs-ot2")}, {})
        value = second.data["value"]
        self.assertEqual(second.data["status"], "conditional", second.data)
        self.assertEqual(len(value["rows"]), 3)                    # appended (index None)
        meta = value["held"]["o3"]
        self.assertEqual((meta["pregrasp_row_id"], meta["pregrasp_observation_id"], meta["closure_observation_id"],
                          meta["extent_source"], meta["group"], meta["centre"]),
                         ("o2", "obs-ot", "obs-close", "pregrasp_row", "elongated", "carried_axis"))
        # fake row o2: centre (0.1, 0.05, 0.9), length 0.1, width 0.03, top/bottom 0.95/0.85; carried offset (across
        # the jaws at the jaw centre), then only its part along the static-prior axis (link6 z) from the grasp centre
        offset = T6c[:3, :3].T @ (np.array([0.1, 0.05, 0.9]) - T6c[:3, 3])
        offset[1] = 0.0
        G = hp.grasp_centre(T6)
        a = hp._unit_up(T6[:3, 2])
        c = T6[:3, 3] + T6[:3, :3] @ offset
        row = value["rows"][2]
        np.testing.assert_allclose(row[1], np.round(G + np.dot(c - G, a) * a, 3), atol=1e-3)
        self.assertEqual((row[4], row[5]), (0.1, 0.03))
        self.assertAlmostEqual(row[2] - row[3], 0.1, places=3)
        self.assertEqual(meta["long_axis"]["source"], "static_prior")
        diag = second.data["diagnostics"]["held_perception"]
        self.assertEqual(diag["held_pregrasp"]["left"]["row"], "o2")
        self.assertEqual(meta["pregrasp_row_centre_m"], [0.1, 0.05, 0.9])
        self.assertEqual(meta["pregrasp_table_source"], dict(seq=1, request="plain", observation_id="obs-ot",
                                                             camera_id="cam_head"))


from auriga.robot.model.geometry import GeometryTools  # noqa: E402

_real_camera_fn = GeometryTools.camera


def _real_camera(self_, identity, oid, cid):
    return _real_camera_fn(self_, identity, oid, cid)


_real_resolve_fn = SceneStore.resolve_artifact


def _real_resolve(scene, ref):
    return _real_resolve_fn(scene, ref)


if __name__ == "__main__":
    unittest.main()
