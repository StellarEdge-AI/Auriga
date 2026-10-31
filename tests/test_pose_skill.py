"""features.pose_skill: read-only pose_plan tool (targets / grasp / place). python -m unittest tests/test_pose_skill.py"""
import hashlib
import json
import math
import unittest
from types import SimpleNamespace

import numpy as np

from auriga.perception.external import pose_skill
from auriga.robot.model import roboprobe_pose
from auriga.core.config import Config
from auriga.core.contracts import canonical
from auriga.runtime.tools import handlers
from auriga.runtime.tools.builtins import create_registry
from auriga.runtime.tools.definitions import public_definitions

COLUMNS = ["id", "centre_m", "top_z", "bottom_z", "length", "width", "yaw_deg", "tilt_deg", "support", "quality"]
TABLE = dict(columns=COLUMNS, table_z=0.765, rows=[
    ["o1", [0.10, 0.05, 0.780], 0.792, 0.765, 0.120, 0.030, 40, 88, "table", "ok"],     # pen-like, lying
    ["o2", [-0.10, 0.02, 0.800], 0.835, 0.765, 0.150, 0.120, -20, None, "table", "ok"],  # too wide across
    ["o3", [0.00, -0.05, 0.771], 0.776, 0.765, 0.200, 0.150, 0, None, "table", "low:depth"],  # both too wide
    ["o4", [0.20, 0.10, 0.790], 0.815, 0.765, 0.050, 0.045, 10, None, "table", "ok"],   # compact
])


def closing_axis(yaw_deg):
    """World direction of the jaw closing line (link6 local y) for angles_deg [0, 0, yaw]."""
    q = roboprobe_pose.angles_to_quat(0.0, 0.0, yaw_deg)
    return roboprobe_pose.quat_to_matrix(q)[:, 1]


class FakeStore:
    def __init__(self, state):
        self.state, self.events = state, []

    def resolve_artifact(self, reference, allowed_scopes=None):
        return {}, json.dumps(self.state).encode()

    def event(self, name, payload, correlation=None, world_kind="observed", **_):
        self.events.append((name, payload, world_kind))


def state_with_yaw(arm, yaw_deg):
    flange = roboprobe_pose.values_to_pose(dict(x=0.1, y=0.0, z=0.9, pitch_deg=0.0, roll_deg=0.0, yaw_deg=yaw_deg))
    w, x, y, z = flange[3:7]
    return {"arms": {arm: {"ee_pose": {"position_m": list(flange[:3]), "quaternion_xyzw": [x, y, z, w]}}}}


def runtime(on=True, yaw=0.0, arm="right", table=TABLE, observation_id="obs-1"):
    config = Config().with_overrides({"features": {"pose_skill": on, "trajectory_preview": False}})
    rt = SimpleNamespace(config=config, features=config.features,
                         latest={"observation_id": observation_id, "robot_state_ref": "artifact:state"},
                         store=FakeStore(state_with_yaw(arm, yaw)))
    if table is not None:
        rt._obs3d_memo = (observation_id, {"object_table": (table, None, {})})
    return rt


def catalog_runtime(on):
    config = Config().with_overrides({"features": {"external_perception": False, "pose_skill": on, "trajectory_preview": False}})
    return SimpleNamespace(disabled_tools=(), observation_profile="rgb_proprio_calibrated_v1", motion_review=True,
                           enable_wait=True, vision_compare=True, visual_reviewer=True, action_policy=None,
                           capabilities=None, programs=True, config=config, features=config.features)


def call(rt, request):
    return handlers.pose_plan(rt, {"request": request}, {"tool_call_id": "c1"}).data


class Geometry(unittest.TestCase):
    def test_angle_convention_closing_line_is_yaw(self):
        for yaw in (-170, -90, -45, 0, 30, 90, 135):
            axis = closing_axis(yaw)
            self.assertAlmostEqual(axis[2], 0.0, places=9)
            self.assertAlmostEqual(abs(axis[0] * math.cos(math.radians(yaw)) + axis[1] * math.sin(math.radians(yaw))),
                                   1.0, places=9)

    def test_closure_candidates_are_perpendicular_to_long_axis(self):
        for object_yaw in (-90, -60, -1, 0, 40, 89.5):
            candidates = pose_skill.closure_yaws(object_yaw)
            self.assertEqual(len(candidates), 2)
            self.assertAlmostEqual(abs(pose_skill.wrap180(candidates[0] - candidates[1])), 180.0, places=6)
            long_axis = np.array([math.cos(math.radians(object_yaw)), math.sin(math.radians(object_yaw)), 0.0])
            for yaw in candidates:
                self.assertTrue(-180 <= yaw < 180)
                self.assertAlmostEqual(float(np.dot(closing_axis(yaw), long_axis)), 0.0, places=2)
                self.assertAlmostEqual(pose_skill.axis_difference(yaw, object_yaw), 90.0, places=6)

    def test_wraps(self):
        self.assertEqual(pose_skill.wrap90(100), -80)
        self.assertEqual(pose_skill.wrap90(-90), -90)
        self.assertEqual(pose_skill.wrap180(180), -180)
        self.assertEqual(pose_skill.axis_difference(10, 170), 20)

    def test_nearest_uses_measured_yaw(self):
        self.assertEqual(pose_skill.nearest([130.0, -50.0], None), -50.0)
        self.assertEqual(pose_skill.nearest([130.0, -50.0], 170.0), 130.0)
        self.assertEqual(pose_skill.nearest([130.0, -50.0], -10.0), -50.0)

    def test_grasp_facts_fit(self):
        row = pose_skill.row_from_table(TABLE, "o1")
        result = pose_skill.grasp_facts(row, object_id="o1")
        self.assertNotIn("grasp_position_m", result)                     # no default grasp point
        self.assertIsNone(result["chosen_point"])
        self.assertEqual(result["facts"]["centre_m"], [0.10, 0.05, 0.780])
        self.assertEqual(result["pad_z_reference_m"], 0.780)                # centre z, not the visible top
        self.assertEqual(result["jaws"]["angles_deg"], [0.0, 0.0, -50.0])
        self.assertEqual(result["jaws"]["closure_yaw_candidates_deg"], [130.0, -50.0])
        self.assertEqual(result["jaws"]["closing_to_long_axis_deg"], 90.0)
        self.assertEqual(result["opening_check"], "ok")
        self.assertTrue(result["elongated"])
        prompt = result["self_prompt"]
        for phrase in ("centre [0.1, 0.05, 0.78]", "angles_deg [0.0, 0.0, -50.0]", "across the long axis",
                       "88.8 mm", "Pad height reference: centre z 0.780", "yours to choose", "handle"):
            self.assertIn(phrase, prompt)
        self.assertNotIn("grasp_position_m", prompt)
        self.assertTrue(any("12 mm below the visible top" in note for note in result["notes"]))

    def test_chosen_point_relative_to_footprint(self):
        row = dict(centre_m=[0.0, 0.0, 0.8], length=0.30, width=0.03, yaw_deg=0)   # e.g. a broom along world x
        handle = pose_skill.grasp_facts(row, grasp_point=[0.12, 0.004], reason="handle end")
        self.assertEqual(handle["chosen_point"]["along_long_axis_m"], 0.12)
        self.assertEqual(handle["chosen_point"]["across_long_axis_m"], 0.004)
        self.assertTrue(handle["chosen_point"]["within_footprint"])
        self.assertEqual(handle["chosen_point"]["reason"], "handle end")
        outside = pose_skill.grasp_facts(row, grasp_point=[0.0, 0.05, 0.8])
        self.assertFalse(outside["chosen_point"]["within_footprint"])
        rotated = pose_skill.grasp_facts(dict(row, yaw_deg=90), grasp_point=[0.0, 0.1])
        self.assertAlmostEqual(rotated["chosen_point"]["along_long_axis_m"], 0.1, places=6)

    def test_opening_checks(self):
        wide = pose_skill.grasp_facts(pose_skill.row_from_table(TABLE, "o2"), object_id="o2")
        self.assertEqual(wide["opening_check"], "too_wide")
        self.assertIn("exceeds the 88.8 mm opening", wide["self_prompt"])
        tight = pose_skill.grasp_facts(dict(centre_m=[0, 0, 0.8], length=0.12, width=0.082, yaw_deg=0))
        self.assertEqual(tight["opening_check"], "tight")
        both = pose_skill.grasp_facts(pose_skill.row_from_table(TABLE, "o3"), object_id="o3")
        self.assertEqual(both["opening_check"], "too_wide")
        self.assertTrue(any("quality low:depth" in note for note in both["notes"]))
        compact = pose_skill.grasp_facts(pose_skill.row_from_table(TABLE, "o4"), object_id="o4")
        self.assertFalse(compact["elongated"])
        self.assertEqual(compact["opening_check"], "ok")

    def test_thin_object_pad_raised_above_support(self):
        row = dict(centre_m=[0.0, 0.0, 0.770], top_z=0.775, bottom_z=0.765, length=0.10, width=0.02, yaw_deg=0)
        result = pose_skill.grasp_facts(row)
        self.assertAlmostEqual(result["pad_z_reference_m"], 0.765 + 0.0075 + 0.002, places=6)
        self.assertTrue(any("raised" in note for note in result["notes"]))

    def test_place_conversion(self):
        # grasped with jaws at -50 while the object lay at 40; the object must end at 0 -> gripper -90
        out = pose_skill.place_conversion(0, object_yaw_at_grasp=40, gripper_yaw_at_grasp=-50)
        self.assertEqual(out["release_yaw_deg"], -90.0)
        # held object currently at 70 (table), gripper now at -20, target 80 -> rotate +10
        out = pose_skill.place_conversion(80, gripper_yaw_now=-20, object_yaw_now=70)
        self.assertEqual(out["release_yaw_deg"], -10.0)
        self.assertEqual(out["rotation_from_current_deg"], 10.0)
        # axis symmetry: target 170 == -10
        out = pose_skill.place_conversion(170, gripper_yaw_now=0, object_yaw_now=0)
        self.assertEqual(out["release_yaw_deg"], -10.0)
        with self.assertRaises(ValueError):
            pose_skill.place_conversion(0)

    def test_place_conversion_3d_posture_transition_lying_to_upright(self):
        out = pose_skill.place_conversion(
            0, object_yaw_at_grasp=40, gripper_yaw_at_grasp=-50,
            source_posture="lying", target_posture="upright",
        )
        self.assertEqual(out["posture_transition"], "lying->upright")
        self.assertEqual(len(out["release_angles_deg"]), 3)
        self.assertEqual(len(out["alternative_angles_deg"]), 3)
        pitch, roll, yaw = out["release_angles_deg"]
        self.assertTrue(-180.0 <= pitch <= 180.0)
        self.assertTrue(-90.0 <= roll <= 90.0)
        self.assertTrue(-180.0 <= yaw <= 180.0)
        # Tool approach axis becomes horizontal and points toward +y (away from arm bases)
        self.assertAlmostEqual(out["tool_approach_axis_world"][2], 0.0, places=2)
        self.assertGreaterEqual(out["tool_approach_axis_world"][1], 0.0)


class Catalog(unittest.TestCase):
    def test_off_is_identical(self):
        self.assertEqual(canonical(public_definitions(pose_skill=False)), canonical(public_definitions()))
        digest = lambda rt: hashlib.sha256(canonical(create_registry(runtime=rt).catalog(rt, role="actor")).encode()).hexdigest()
        self.assertEqual(digest(catalog_runtime(False)), hashlib.sha256(canonical(
            create_registry().catalog(catalog_runtime(False), role="actor")).encode()).hexdigest())
        names = [d["name"] for d in create_registry(runtime=catalog_runtime(False)).catalog(catalog_runtime(False))]
        self.assertNotIn("pose_plan", names)
        self.assertFalse(Config().features.pose_skill)

    def test_on_adds_only_pose_plan(self):
        off = {d["name"]: d for d in public_definitions()}
        on = {d["name"]: d for d in public_definitions(pose_skill=True)}
        self.assertEqual(set(on) - set(off), {"pose_plan"})
        for name in off:
            self.assertEqual(canonical(on[name]), canonical(off[name]), name)
        rt = catalog_runtime(True)
        names = [d["name"] for d in create_registry(runtime=rt).catalog(rt, role="actor")]
        self.assertIn("pose_plan", names)
        self.assertIn("pose_plan", [d["name"] for d in create_registry(runtime=rt).catalog(rt, role="program")])
        self.assertIn("Input field guide", on["pose_plan"]["description"])

    def test_instructions_text(self):
        text = pose_skill.INSTRUCTIONS
        for phrase in ("success criteria", "kind:'targets'", "needed:false", "kind:'grasp'", "attention",
                       "final waypoint", "kind:'place'"):
            self.assertIn(phrase, text)


class Handler(unittest.TestCase):
    def test_grasp_by_id_uses_attached_table_and_measured_yaw(self):
        rt = runtime(yaw=170.0)
        data = call(rt, dict(kind="grasp", object="o1", arm="right", grasp_point_m=[0.11, 0.06], reason="mid-shaft"))
        self.assertEqual(data["jaws"]["angles_deg"], [0.0, 0.0, 130.0])      # nearer to the measured 170
        self.assertEqual(data["measured_arm_yaw_deg"], 170.0)
        self.assertEqual(data["row_source"], "attached_table")
        self.assertEqual(data["observation_id"], "obs-1")
        name, payload, world = rt.store.events[-1]
        self.assertEqual((name, world), (pose_skill.EVENT_GRASP, "internal"))
        self.assertEqual(payload["jaws"]["angles_deg"], [0.0, 0.0, 130.0])
        self.assertEqual(payload["jaws"]["closing_to_long_axis_deg"], 90.0)
        self.assertEqual(payload["chosen_point"]["grasp_point_m"], [0.11, 0.06])
        self.assertEqual(payload["chosen_point"]["reason"], "mid-shaft")
        with self.assertRaises(ValueError):
            call(rt, dict(kind="grasp", object="o1", grasp_point_m=[0.1]))

    def test_grasp_errors_and_copied_row(self):
        rt = runtime()
        with self.assertRaises(ValueError):
            call(rt, dict(kind="grasp", object="o9"))
        with self.assertRaises(ValueError):
            call(rt, dict(kind="grasp", object="o1", observation_id="obs-old"))
        data = call(rt, dict(kind="grasp", row=dict(centre_m=[0, 0, 0.8], length=0.1, width=0.02, yaw_deg=90)))
        self.assertEqual(data["row_source"], "copied_row")
        self.assertEqual(data["jaws"]["closing_to_long_axis_deg"], 90.0)

    def test_targets_and_attention(self):
        rt = runtime()
        data = call(rt, dict(kind="targets", needed=True, reason="tubes go upright into the rack", targets=[
            dict(object="o1 test tube", destination="rack hole 2", yaw_deg=None, posture="upright"),
            dict(object="o4 block", yaw_deg=100, posture="lying", tolerance_deg=5)]))
        self.assertEqual(data["targets"][1]["yaw_deg"], -80.0)
        self.assertIn("o1 test tube -> rack hole 2: upright", data["attention_text"])
        self.assertIn("long axis yaw -80+/-5 deg, lying", data["attention_text"])
        self.assertIn("final waypoint", data["attention_text"])
        self.assertLessEqual(len(data["attention_text"]), 1600)
        self.assertEqual(rt.store.events[-1][0], pose_skill.EVENT_TARGETS)
        skip = call(rt, dict(kind="targets", needed=False, reason="pure transport into baskets"))
        self.assertIsNone(skip["attention_text"])
        with self.assertRaises(ValueError):
            call(rt, dict(kind="targets", needed=True, targets=[]))

    def test_place_uses_remembered_grasp(self):
        rt = runtime(yaw=0.0)
        call(rt, dict(kind="grasp", object="o1", arm="right"))           # object 40, jaws -50
        data = call(rt, dict(kind="place", object="o1", arm="right", target_yaw_deg=0))
        self.assertEqual(data["release_yaw_deg"], -90.0)
        self.assertEqual(rt.store.events[-1][0], pose_skill.EVENT_PLACE)

    def test_no_robot_state_is_tolerated(self):
        rt = runtime()
        rt.latest = {"observation_id": "obs-1"}
        data = call(rt, dict(kind="grasp", object="o1", arm="left"))
        self.assertIsNone(data["measured_arm_yaw_deg"])
        self.assertEqual(data["jaws"]["angles_deg"], [0.0, 0.0, -50.0])


if __name__ == "__main__":
    unittest.main()
