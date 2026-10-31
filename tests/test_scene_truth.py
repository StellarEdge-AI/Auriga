"""analysis.scene_truth_log: writer (fake env), isolation from model-facing code, offline scorer.
python -m unittest discover -s tests -p test_scene_truth.py"""
import ast
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from auriga.core.config import Config
from auriga.core.contracts import canonical
from auriga.core.observation_policy import reject_privileged
from auriga.robot.backends.robodojo.scene_truth import FILENAME, SceneTruthRecorder, truth_path
from auriga.core.storage.store import Store

_spec = importlib.util.spec_from_file_location("score_geometry_truth", ROOT / "scripts" / "score_geometry_truth.py")
scorer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scorer)

RAW = "raw-%08d-0000-4000-8000-000000000000"
IDENTITY = dict(run_id="r", episode_id="ep-1", env_instance_id="env-1", env_generation=1)


class FakeRecords:
    def __init__(self, records, metadata):
        self.layout_records_by_env, self.metadata_by_env = [records], [metadata]


class FakeLayout:
    """LayoutManager accessors used by the recorder (RoboDojo env/scene_manager/layout_manager.py)."""

    def __init__(self, poses, fail=False):
        self.poses, self.fail = poses, fail
        box = dict(geometry=dict(oriented_bbox=dict(vertices=[[x, y, z] for x in (-.04, .04) for y in (-.03, .03) for z in (-.02, .02)])))
        self.object_records_by_type = {
            "Rigid": FakeRecords([dict(inst_name="obj_0", label="red_cup", scale=[1, 1, 1]),
                                  dict(inst_name="obj_1", label=None, scale=[2, 2, 2])],
                                 {"obj_0": dict(box, model_name="cup", model_id="m0"), "obj_1": dict(box, model_name="block")})}
        self.table_info = [dict(pos=[0, 0, .74], size=[-.6, -.4, .6, .4], height=.765)]
        self.tables = [["table_0"]]

    def get_scene_object(self, env_idx, inst):
        return None

    def get_instance_pose(self, env_idx, inst_name):
        if self.fail:
            raise RuntimeError("pose unavailable")
        return self.poses[inst_name]


class FakeEnv:
    def __init__(self, layout):
        self.scene_manager = type("SM", (), {"layout_manager": layout})()


STATE = {"arms": {"left": {"ee_pose": {"position_m": [0.1, 0.2, 0.9], "quaternion_xyzw": [0, 0, 0, 1],
                                       "tcp_config_id": "arx-x5-link6-v1"}, "gripper": {"position_m": [0.02, 0.02]}}}}


class WriterTest(unittest.TestCase):
    def test_truth_path_under_private(self):
        self.assertEqual(truth_path(Path("/x/run/private/robot")), Path("/x/run/private") / FILENAME)
        self.assertEqual(truth_path(Path("/x/run")), Path("/x/run/private") / FILENAME)

    def test_record_fields_and_write_only(self):
        poses = {"obj_0": ([0.1, -0.2, 0.785], [1, 0, 0, 0]),                       # wxyz identity
                 "obj_1": ([0.3, 0.0, 0.805], [math.cos(math.pi / 4), 0, 0, math.sin(math.pi / 4)])}  # yaw 90
        with tempfile.TemporaryDirectory() as tmp:
            path = truth_path(Path(tmp) / "private" / "robot")
            recorder = SceneTruthRecorder(FakeEnv(FakeLayout(poses)), path, grasp_offset_m=0.15, usd_bbox=None)
            self.assertIsNone(recorder.record(RAW % 1, 7, STATE, 0.28))
            self.assertIsNone(recorder.record(RAW % 2, 9, STATE, 0.36))
            lines = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(lines), 2)
        line = lines[0]
        self.assertEqual((line["raw_observation_id"], line["sim_step"], line["audience"], line["errors"]), (RAW % 1, 7, "analysis_only", []))
        cup, block = line["objects"]
        self.assertEqual((cup["name"], cup["category"], cup["labelled"]), ("red_cup", "cup", True))
        self.assertEqual(cup["quaternion_xyzw"], [0, 0, 0, 1])
        self.assertAlmostEqual(cup["top_z_m"], 0.805)
        self.assertAlmostEqual(cup["bottom_z_m"], 0.765)          # resting on the table top
        self.assertEqual(cup["bbox_size_m"], [0.08, 0.06, 0.04])
        self.assertEqual(block["name"], "obj_1")                  # unlabelled: instance name
        self.assertEqual(block["bbox_size_m"], [0.16, 0.12, 0.08])  # scale applied
        self.assertAlmostEqual(block["top_z_m"], 0.845)
        self.assertAlmostEqual(line["table"]["top_z_m"], 0.765)
        self.assertEqual(line["robot"]["left"]["grasp_center_m"], [0.25, 0.2, 0.9])
        self.assertEqual(line["robot"]["left"]["gripper_position_m"], [0.02, 0.02])

    def test_failures_never_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private" / FILENAME
            recorder = SceneTruthRecorder(FakeEnv(FakeLayout({}, fail=True)), path, grasp_offset_m=0.15, usd_bbox=None)
            self.assertIsNone(recorder.record(RAW % 1, 0, None))
            line = json.loads(path.read_text())
            self.assertTrue(line["errors"] and line["objects"][0]["position_m"] is None)
            broken = SceneTruthRecorder(object(), Path(tmp) / "file_as_dir" / "x", grasp_offset_m=0.15, usd_bbox=None)
            (Path(tmp) / "file_as_dir").write_text("")
            self.assertIsNone(broken.record(RAW % 2, 0, STATE))
            self.assertIsNotNone(broken.last_error)


class IsolationTest(unittest.TestCase):
    """The truth log is write-only: no model-facing module can reach it."""

    ALLOWED = {"robot/backends/robodojo/scene_truth.py": "writer",
               "robot/backends/robodojo/robot_worker.py": "calls the writer, discards its value",
               "robot/backends/robodojo/robodojo.py": "passes the output path to the worker", "core/config.py": "switch",
               "core/observation_policy.py": "rejects a scene_truth key in actor payloads",
               "perception/external/obs3d.py": "reads the switch to log the attached (model-visible) table as an internal event; no truth"}

    def test_only_writer_side_modules_mention_it(self):
        found = {path.relative_to(ROOT / "auriga").as_posix() for path in (ROOT / "auriga").rglob("*.py")
                 if "scene_truth" in path.read_text(encoding="utf-8")}
        self.assertTrue(found.issubset(set(self.ALLOWED)), f"unexpected modules: {found - set(self.ALLOWED)}")
        readers = {path.relative_to(ROOT).as_posix() for folder in ("auriga", "scripts") for path in (ROOT / folder).rglob("*.py")
                   if FILENAME in path.read_text(encoding="utf-8")}
        self.assertTrue(readers.issubset({"auriga/robot/backends/robodojo/scene_truth.py", "scripts/score_geometry_truth.py",
                                          "scripts/probes/scene_truth_probe.py"}))

    def test_worker_discards_record_value(self):
        tree = ast.parse((ROOT / "auriga" / "robot/backends/robodojo/robot_worker.py").read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "record" and isinstance(node.func.value, ast.Attribute)
                 and node.func.value.attr == "scene_truth"]
        self.assertEqual(len(calls), 1)
        statements = [node for node in ast.walk(tree) if isinstance(node, ast.Expr) and node.value is calls[0]]
        self.assertEqual(len(statements), 1, "the recorder's return value must be discarded")

    def test_privileged_key_rejected_and_profile_unchanged(self):
        with self.assertRaises(ValueError):
            reject_privileged({"robot_state": {}, "scene_truth": {"objects": []}})
        with self.assertRaises(ValueError):
            reject_privileged({"value": [{"scene_truth": 1}]})
        on = Config.from_dict({"analysis": {"scene_truth_log": True}})
        off = Config.from_dict({})
        self.assertFalse(off.analysis.scene_truth_log)
        self.assertEqual(on.features, off.features)               # observation profile, tools, payload: unchanged


# ------------------------------------------------------------------ scorer
def _truth_line(raw, step, cup_xy=(0.1, -0.2)):
    def obj(name, p, size):
        half = [s / 2 for s in size]
        return dict(name=name, inst=name, category=name, position_m=list(p), quaternion_xyzw=[0, 0, 0, 1],
                    centroid_m=list(p), top_z_m=p[2] + half[2], bottom_z_m=p[2] - half[2],
                    bbox_local_min_m=[-h for h in half], bbox_local_max_m=half, bbox_size_m=list(size))
    return dict(raw_observation_id=raw, sim_step=step, objects=[
        obj("cup", (cup_xy[0], cup_xy[1], 0.785), (0.08, 0.08, 0.04)),
        obj("block", (0.3, 0.0, 0.79), (0.05, 0.05, 0.05)),
        obj("pen_a", (-0.2, 0.1, 0.77), (0.1, 0.01, 0.01)),
        obj("pen_b", (-0.2, 0.125, 0.77), (0.1, 0.01, 0.01)),
        dict(obj("camera_stand", (0.33, 0.0, 1.0), (0.3, 0.5, 0.1)), category="camera_stand")], table=dict(top_z_m=0.765))


V5_COLUMNS = ["id", "centre_m", "top_z", "bottom_z", "length", "width", "yaw_deg", "tilt_deg", "support", "quality"]
V5_ROWS = [["o1", [0.104, -0.197, 0.79], 0.80, 0.765, 0.08, 0.08, 0.0, None, "table", "ok"],
           ["o2", [0.3, 0.01, 0.80], 0.82, 0.765, 0.05, 0.05, 10.0, None, "table", "low:floating_rescaled_to_table"]]


def obs3d_text(observation_id, table):
    return canonical({"object_table": table, "observation_id": observation_id})


def build_run(root, with_truth=True, object_table=False, obs3d=None):
    """obs3d: None, 'reply_text' (tables only inside reply wire_response, as before the analysis event) or 'event'."""
    store = Store(root, IDENTITY)
    for k, step in ((1, 0), (2, 40)):
        store.save("observation", f"obs-{k}", dict(observation_id=f"obs-{k}", sim_step=step,
                   cameras=[dict(camera_id=c, frame_id=(RAW % k) + "-" + c) for c in ("cam_head", "cam_left_wrist")]))

    def tool(identifier, data, role="actor"):
        ref = store.put_artifact(canonical(dict(data=data, kind="json", success=True)).encode(), "tool_result", "application/json", "observer")
        store.save("tool_result", identifier, dict(tool="geometry_query", role=role, raw_ref=ref, correlation={}))
    head = (RAW % 1) + "-cam_head"
    # VGGT default-mode pixel_plane on the cup: applied plane = VGGT centre 0.79, caller said 0.765
    tool("t1", dict(query_type="pixel_plane", status="conditional", frame_ids=[head], observation_ids=["obs-1"],
                    value=dict(point_m=[0.11, -0.2, 0.79], plane_origin_m=[0, 0, 0.79], plane_normal=[0, 0, 1],
                               plane_origin_alt_m=[0, 0, 0.765], alternate_point_m=[0.13, -0.19, 0.765],
                               plane_estimate=dict(status="ok", center_z_m=0.79, top_z_m=0.81, alternate_plane="caller_plane_origin_m"))))
    # caller-only pixel_plane on the block (no plane_estimate), table plane assumed
    tool("t2", dict(query_type="pixel_plane", status="conditional", frame_ids=[head], observation_ids=["obs-1"],
                    value=dict(point_m=[0.33, 0.0, 0.765], plane_origin_m=[0, 0, 0.765], plane_normal=[0, 0, 1])), role="program")
    # triangulate: one point near pen_a (ambiguous with pen_b), one near the block; joined via observation id only
    tool("t3", dict(query_type="triangulate", status="conditional", frame_ids=[], observation_ids=["obs-2"],
                    value=dict(points_m=[[-0.2, 0.11, 0.775], [0.3, 0.005, 0.81]], accepted_indices=[0, 2],
                               reprojection_error_px=[1.0, 9.0, 2.0])))
    request = dict(action_id="a1", based_on_observation_id="obs-1", stages=[
        dict(id="cup_above", kind="move", arm="left", action={}),
        dict(id="cup_pick", kind="move", arm="left", action={}),
        dict(id="close", kind="gripper", arm="left", action=dict(gripper_events=[dict(arm="left", goal_normalized=0)])),
        dict(id="cup_lift", kind="move", arm="left", action={})])
    def feedback(target):
        return dict(grasp_contact_feedback=dict(arms=dict(left=dict(requested_contact_position_m=target, available=True, position_error_m=0.004))))
    receipt = dict(status="completed", execution_start_observation_id="obs-1", stage_results=dict(
        cup_above=feedback([0.1, -0.2, 0.9]), cup_pick=dict(motion=feedback([0.102, -0.203, 0.80])), close={},
        cup_lift=feedback([0.1, -0.2, 0.95])))
    store.save("action_request", "a1", request)
    store.save("action_receipt", "a1", receipt)
    store.save("tool_call", "c1", dict(params=dict(arguments=dict(code="print(1)"))))
    if object_table:   # object table service v5 with caller labels: extra 'label' column and 'labels' field
        tool("t4", dict(query_type="object_table", status="conditional", frame_ids=[head], observation_ids=["obs-1"],
                        value=dict(source="vggt_object_table", measured=False, camera_id="cam_head", support_z_m=0.765,
                                   columns=V5_COLUMNS + ["label"], rows=[r + [l] for r, l in zip(V5_ROWS, ("cup", "block"))],
                                   labels=[dict(label="cup", status="ok", ids=["o1"]), dict(label="block", status="ok", ids=["o2"])],
                                   omitted=0, overlay_cameras=["cam_head"])))
    if obs3d:
        table = dict(columns=V5_COLUMNS, rows=V5_ROWS, table_z=0.765, note="head camera")
        later = dict(table, rows=[["o1", [0.25, 0.0, 0.79], 0.815, 0.765, 0.05, 0.05, 0.0, None, "table", "ok"]])

        def reply(identifier, tool_name, observation_id, part, call):
            items = [dict(type="inputText", text=canonical({"bundle": {"observation_id": observation_id}})),
                     dict(type="inputText", text=obs3d_text(observation_id, part))]
            ref = store.put_artifact(canonical(dict(data={}, kind="observation", success=True,
                                     wire_response=dict(contentItems=items, success=True))).encode(),
                                     "tool_result", "application/json", "observer")
            store.save("tool_result", identifier, dict(tool=tool_name, role="actor", raw_ref=ref,
                                                       correlation=dict(tool_call_id=call)))
        # action reply attaches obs-1; robot_observe of obs-1 reuses it; the action reply of obs-2 is unavailable first
        reply("r1", "robot_action", "obs-1", table, "k1")
        reply("r2", "robot_observe", "obs-1", table, "k2")
        reply("r3", "robot_wait", "obs-2", "unavailable: timeout", "k3")
        reply("r4", "robot_observe", "obs-2", later, "k4")
        if obs3d == "event":
            for call, observation_id, frame, value, kind, reused in (
                    ("k1", "obs-1", head, table, "action", False), ("k2", "obs-1", head, table, "observe", True),
                    ("k3", "obs-2", (RAW % 2) + "-cam_head", None, "action", False),
                    ("k4", "obs-2", (RAW % 2) + "-cam_head", later, "observe", False)):
                payload = dict(observation_id=observation_id, observation_ids=[observation_id], camera_id="cam_head",
                               reply=kind, reused=reused, frame_ids=[frame], available=value is not None)
                if value is None:
                    payload["unavailable"] = "unavailable: timeout"
                else:
                    payload.update(columns=value["columns"], rows=value["rows"], table_z=0.765, omitted=0)
                store.event("analysis.obs3d_object_table", payload, dict(tool_call_id=call), world_kind="internal")
    store.close()
    if with_truth:
        private = Path(root) / "private"
        private.mkdir(exist_ok=True)
        (private / FILENAME).write_text("\n".join(json.dumps(line) for line in (_truth_line(RAW % 1, 0), _truth_line(RAW % 2, 40))) + "\n")


class ScorerTest(unittest.TestCase):
    def test_scores_geometry_and_grasp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            build_run(root)
            before = sorted(p.name for p in root.iterdir())
            result = scorer.score_run(root)
            self.assertEqual(sorted(p.name for p in root.iterdir()), before)     # read-only on the run
        rows = {(r["tool_result_id"], r.get("point_index")): r for r in result["geometry_rows"]}
        vggt = rows[("t1", None)]
        self.assertEqual((vggt["object"], vggt["plane_source"], vggt["truth_join"], vggt["unambiguous"]), ("cup", "vggt", "frame_id", True))
        self.assertAlmostEqual(vggt["xy_error_m"], 0.01)
        self.assertAlmostEqual(vggt["z_error_centre_m"], 0.005)
        self.assertAlmostEqual(vggt["z_error_top_m"], -0.015)
        self.assertAlmostEqual(vggt["caller_plane_minus_truth_centre_z_m"], -0.02)
        self.assertAlmostEqual(vggt["vggt_centre_minus_truth_centre_z_m"], 0.005)
        self.assertAlmostEqual(vggt["vggt_top_minus_truth_top_z_m"], 0.005)
        self.assertTrue(vggt["inside_xy_footprint"])
        caller = rows[("t2", None)]
        self.assertEqual((caller["object"], caller["plane_source"], caller["role"]), ("block", "caller", "program"))
        self.assertAlmostEqual(caller["plane_applied_minus_truth_centre_z_m"], -0.025)
        self.assertFalse(caller["inside_xy_footprint"])
        pen = rows[("t3", 0)]
        self.assertEqual((pen["object"], pen["unambiguous"], pen["truth_join"], pen["truth_sim_step"]), ("pen_a", False, "observation_raw_id", 40))
        self.assertEqual(rows[("t3", 2)]["reprojection_error_px"], 2.0)
        grasp = [r for r in result["grasp_rows"] if r["is_grasp"]]
        self.assertEqual([(r["stage_id"], r["object"]) for r in grasp], [("cup_pick", "cup")])
        self.assertAlmostEqual(grasp[0]["xy_error_m"], math.hypot(0.002, 0.003), places=4)
        self.assertAlmostEqual(grasp[0]["z_error_top_m"], -0.005)
        self.assertEqual(len(result["grasp_rows"]), 3)
        summary = result["geometry"]
        self.assertEqual((summary["all"]["n"], summary["all"]["unambiguous"]), (4, 3))
        self.assertEqual(set(summary["by_plane_source"]), {"vggt", "caller", "triangulate"})
        self.assertEqual(summary["by_kind"]["triangulate"]["n"], 2)
        self.assertEqual(result["leak_audit"]["tool_call_arguments"], 0)
        text = scorer.markdown([result])
        self.assertIn("| case | plane=vggt | 1 |", text)

    def test_v5_object_table_rows_scored_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            build_run(root, object_table=True)
            result = scorer.score_run(root)
        rows = {r["object_id"]: r for r in result["geometry_rows"] if r["kind"] == "object_table"}
        self.assertEqual(set(rows), {"o1", "o2"})
        cup, block = rows["o1"], rows["o2"]
        self.assertEqual((cup["object"], cup["ot_label"], cup["plane_source"], cup["truth_join"]), ("cup", "cup", "object_table", "frame_id"))
        self.assertAlmostEqual(cup["xy_error_m"], 0.005)
        self.assertAlmostEqual(cup["z_error_centre_m"], 0.005)
        self.assertAlmostEqual(cup["z_error_top_m"], -0.015)
        self.assertAlmostEqual(cup["ot_top_minus_truth_top_z_m"], -0.005)
        self.assertEqual((block["object"], block["ot_quality"]), ("block", "low:floating_rescaled_to_table"))
        self.assertAlmostEqual(block["xy_error_m"], 0.01)
        self.assertAlmostEqual(block["z_error_centre_m"], 0.01)
        self.assertAlmostEqual(block["ot_top_minus_truth_top_z_m"], 0.005)
        summary = result["geometry"]
        self.assertEqual((summary["all"]["n"], summary["by_kind"]["object_table"]["n"]), (6, 2))
        self.assertEqual(summary["by_kind"]["object_table"]["ot_top_minus_truth_top_z_m"]["p50"], 0.005)
        text = scorer.markdown([result])
        line = next(l for l in text.splitlines() if "| kind=object_table |" in l)
        header = next(l for l in text.splitlines() if l.startswith("| episode | group |"))
        self.assertEqual(line.count("|"), header.count("|"))                    # one header cell per metric
        self.assertIn("table top - top z", header)

    def test_v1_columns_without_label_unchanged(self):
        points = scorer.table_points(V5_COLUMNS, V5_ROWS, "object_table")
        self.assertEqual([(p[0], p[1]["object_id"], "ot_label" in p[1]) for p in points],
                         [([0.104, -0.197, 0.79], "o1", False), ([0.3, 0.01, 0.80], "o2", False)])

    def check_obs3d(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            build_run(root, obs3d=mode)
            result = scorer.score_run(root)
        rows = [r for r in result["geometry_rows"] if r["kind"] == "object_table_obs"]
        self.assertEqual([(r["observation_id"], r["object_id"], r["object"]) for r in rows],
                         [("obs-1", "o1", "cup"), ("obs-1", "o2", "block"), ("obs-2", "o1", "block")])
        cup = rows[0]
        self.assertAlmostEqual(cup["xy_error_m"], 0.005)
        self.assertAlmostEqual(cup["z_error_centre_m"], 0.005)
        self.assertAlmostEqual(cup["z_error_top_m"], -0.015)
        self.assertAlmostEqual(cup["ot_top_minus_truth_top_z_m"], -0.005)
        self.assertEqual((cup["obs3d_deliveries"], cup["role"], cup["obs3d_source"]), (2, "actor", mode))
        self.assertEqual((rows[2]["truth_sim_step"], rows[2]["obs3d_reply"]), (40, "observe"))
        self.assertAlmostEqual(rows[2]["xy_error_m"], 0.05)
        obs = result["obs3d"]
        self.assertEqual((obs["source"], obs["attachments"], obs["observations"], obs["unavailable_observations"],
                          obs["scored_rows"]), (mode, 4, 2, 0, 3))
        self.assertEqual(obs["reused_deliveries"], 1 if mode == "event" else None)
        summary = result["geometry"]
        self.assertEqual(summary["all"]["n"], 4)                                  # geometry_query calls only
        self.assertEqual(summary["by_kind"]["object_table_obs"]["n"], 3)
        self.assertEqual(summary["by_plane_source"]["object_table_obs"]["n"], 3)
        text = scorer.markdown([result])
        self.assertIn("| case | kind=object_table_obs | 3 |", text)
        self.assertIn(f"| case | {mode} | 4 | 2 | 0 |", text)
        return result

    def test_obs3d_attachments_from_events(self):
        result = self.check_obs3d("event")
        self.assertEqual({r["truth_join"] for r in result["geometry_rows"] if r["kind"] == "object_table_obs"}, {"frame_id"})

    def test_obs3d_attachments_from_reply_text_fallback(self):
        result = self.check_obs3d("reply_text")
        self.assertEqual({r["truth_join"] for r in result["geometry_rows"] if r["kind"] == "object_table_obs"},
                         {"observation_raw_id"})

    def test_footprint_uses_horizontal_axes(self):
        # y-up asset lying with local y vertical: footprint is local x/z
        item = dict(position_m=[0, 0, 0.8], quaternion_xyzw=[math.sin(math.pi / 4), 0, 0, math.cos(math.pi / 4)],
                    bbox_local_min_m=[-0.05, -0.01, -0.02], bbox_local_max_m=[0.05, 0.01, 0.02])
        self.assertTrue(scorer.inside_footprint(item, [0.04, 0.015, 0.81]))
        self.assertFalse(scorer.inside_footprint(item, [0.04, 0.03, 0.81]))

    def test_missing_truth_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            build_run(root, with_truth=False)
            result = scorer.score_run(root)
            self.assertIn("warning", result)
            self.assertEqual(result["geometry"]["all"]["matched"], 0)
            self.assertEqual(scorer.find_runs([tmp]), [root])

    def test_cli_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runs" / "case"
            root.parent.mkdir()
            build_run(root)
            out = Path(tmp) / "out"
            out.mkdir()
            self.assertEqual(scorer.main([str(root.parent), "--out-json", str(out / "s.json"), "--out-md", str(out / "s.md")]), 0)
            document = json.loads((out / "s.json").read_text())
            self.assertEqual(document["pooled"]["geometry"]["n"], 4)
            self.assertEqual(document["pooled"]["grasp"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
