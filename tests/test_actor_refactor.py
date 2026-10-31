"""Public planning/metadata contracts, independent of robot execution fixtures."""
import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from auriga.core.presentation.actor_output import ActorOutput, project_metadata
from auriga.core.tool_output import ToolOutput
from auriga.core.presentation.actor_presentation import ActorPresentation
from auriga.cortex.context.actor_instructions import instructions
from auriga.runtime.harness import Harness
from auriga.core.storage.store import Store
from auriga.runtime.tools.definitions import public_definitions
from test_completion_guidance import RETIRED_TOOLS
from test_harness import FixtureRobot


class ActorRefactorTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.identity = dict(run_id="freeform", episode_id="episode", env_instance_id="fixture", env_generation=1)
        self.store = Store(Path(self.directory.name) / "store", self.identity)
        self.addCleanup(self.store.close)
        self.harness = Harness(self.store, FixtureRobot(self.directory.name), task_plan_required=True)
        self.harness.start()

    def call(self, name, args):
        return json.loads(self.harness.dispatch(name, args, {"tool_call_id": "fixture-call"})["contentItems"][0]["text"])

    def test_first_action_gate_then_freeform_short_ack_and_revision(self):
        args = dict(observation_id=self.harness.latest["observation_id"], native_steps=1)
        rejected = self.call("robot_wait", args)
        self.assertEqual(rejected["receipt"]["status"], "rejected")
        self.assertIn("task_plan_required", rejected["receipt"]["reason"])
        self.assertEqual(self.harness.robot.executions, 0)
        text = "First observe the demonstration; then choose the ordering. Check each placement and adapt if it moves."
        saved = self.call("task_plan", dict(plan=text, ledger=[]))
        self.assertEqual(saved, dict(plan_id="P1", status="saved"))
        self.assertEqual(self.call("task_plan", dict(operation="read"))["plan"], text)
        self.harness.task_planning.require(self.identity)
        second = self.call("task_plan", dict(operation="update", plan="The demonstration is clear; reverse the two placements."))
        self.assertEqual(second["plan_id"], "P2")
        self.assertEqual(self.store.read("task_plan", "P1")["plan"], text)
        with self.assertRaisesRegex(ValueError, "task_plan_required"):
            self.harness.task_planning.require(dict(self.identity, env_generation=2))

    def test_invalid_plan_cannot_unlock_and_readonly_does_not_unlock(self):
        self.call("robot_observe", {})
        for plan in (" ", "x" * 16001):
            with self.assertRaises(ValueError):
                self.call("task_plan", dict(plan=plan, ledger=[]))
        with self.assertRaisesRegex(ValueError, "task_plan_required"):
            self.harness.task_planning.require(self.identity)

    def test_actor_catalog_contains_one_motion_interface_and_no_old_forms(self):
        catalog = public_definitions()
        names = [item["name"] for item in catalog]
        self.assertFalse(set(names) & RETIRED_TOOLS)
        self.assertFalse(any(name.startswith("scene_") for name in names))
        self.assertEqual(names.count("robot_action"), 1)
        plan = next(item for item in catalog if item["name"] == "task_plan")
        self.assertEqual(plan["inputSchema"]["properties"]["plan"]["type"], "string")
        program = next(item for item in catalog if item["name"] == "program_run")
        self.assertNotIn("max_calls", program["inputSchema"]["properties"])
        text = instructions("RGB and proprioception.", "Use visible support.")
        for name in RETIRED_TOOLS:
            self.assertNotIn(name, text)

    def test_metadata_projection_keeps_evidence_and_restores_functional_ids(self):
        output = ActorOutput(self.store, ActorPresentation(self.store))
        original_id = "obs-" + "a" * 32
        value = dict(schema_version="0.1", observation_id=original_id,
            receipt=dict(status="failed", completed_native_steps=4, reason="tracking_divergence"),
            robot_state=dict(q_rad=[.1], source_kind="measurement"), version="internal", sha256="b" * 64)
        original = deepcopy(value)
        reply = output.present(ToolOutput(value, projection="observation").response())
        shown = json.loads(reply["contentItems"][0]["text"])
        self.assertEqual(shown["observation_id"], "obs_1")
        self.assertEqual(shown["receipt"], value["receipt"])
        self.assertEqual(shown["robot_state"], value["robot_state"])
        self.assertTrue({"schema_version", "version", "sha256"}.isdisjoint(shown))
        self.assertEqual(output.restore(dict(observation_id="obs_1")), dict(observation_id=original_id))
        self.assertEqual(value, original)
        rebuilt = ActorOutput(self.store, ActorPresentation(self.store))
        self.assertEqual(rebuilt.restore(dict(source="client.call('observe', {'observation_id':'obs_1'})"))["source"],
            "client.call('observe', {'observation_id':'" + original_id + "'})")

    def test_startup_capability_projection_keeps_limits_without_old_execution_contracts(self):
        output = ActorOutput(self.store, ActorPresentation(self.store))
        native = dict(motion_execution_mode="resampled", unified_action=dict(max_stages=32),
            arm_joint_limits_rad={"left": [[-1., 1.]]}, arm_max_step_rad={"left": [.1]},
            action_speed=dict(speed_scale=dict(min=.25, max=2.),
                waypoint_speed_scales=dict(available=True), gripper="legacy rebased jaws"),
            versions=dict(robot="old-build"), max_waypoints=8,
            motion_preview=dict(max_waypoints=1), waypoint_failure_semantics="late planning can partly move")
        original = deepcopy(native)
        shown = output.present(native, projection="capabilities")
        for field in ("arm_joint_limits_rad", "arm_max_step_rad", "unified_action"):
            self.assertEqual(shown[field], native[field])
        self.assertEqual(shown["action_speed"]["speed_scale"], native["action_speed"]["speed_scale"])
        self.assertFalse({"versions", "motion_preview", "max_waypoints", "waypoint_failure_semantics"} & shown.keys())
        self.assertNotIn("waypoint_speed_scales", shown["action_speed"])
        self.assertEqual(native, original)

    def test_shared_action_metadata_removes_only_known_nested_owner_fields(self):
        owner = dict(schema_version="0.1", run_id="run", episode_id="episode",
                     env_instance_id="environment", env_generation=2)
        business = dict(version="semantic-v2", revision=7, source="measured", schema_version="business-schema")
        receipt = dict(owner, status="unknown", executed_native_steps=None, stop_confirmed=False,
            batch_revision=3, plan_revision=4, stage_results={"revision": deepcopy(business)},
            failure_feedback=dict(schema_version="1", reason="tracking_error", diagnostic=deepcopy(business)))
        value = dict(owner, request=dict(owner, type="BatchAction", batch_expected_revision=3,
            stages=[dict(id="revision", kind="move", action=dict(owner,
                based_on_observation_id="obs-current", timeout_s=120, business=deepcopy(business)))]),
            receipt=receipt)
        original = deepcopy(value)
        before = self.store.db.total_changes
        shown = project_metadata(value, "action_result")
        child = shown["request"]["stages"][0]["action"]
        self.assertTrue(set(owner).isdisjoint(child))
        self.assertEqual(child, dict(based_on_observation_id="obs-current", timeout_s=120, business=business))
        self.assertEqual(shown["receipt"]["stage_results"], {"revision": business})
        self.assertEqual(shown["receipt"]["failure_feedback"], dict(reason="tracking_error", diagnostic=business))
        self.assertEqual((shown["receipt"]["batch_revision"], shown["receipt"]["plan_revision"]), (3, 4))
        self.assertIsNone(shown["receipt"]["executed_native_steps"])
        self.assertFalse(shown["receipt"]["stop_confirmed"])
        self.assertEqual(project_metadata([value], "action_history"), [shown])
        self.assertEqual(project_metadata(dict(actions=[value]), "action_history"), dict(actions=[shown]))
        self.assertEqual(project_metadata([receipt] * 9, "program_action_evidence"), [shown["receipt"]] * 9)
        plan = dict(owner, status="failed", physics_advanced=False, executed_native_steps=0,
                    plan_revision=4, failure_feedback=receipt["failure_feedback"])
        self.assertEqual(project_metadata(plan, "action_plan")["failure_feedback"], shown["receipt"]["failure_feedback"])
        self.assertEqual(project_metadata(plan, "action_plan")["plan_revision"], 4)
        self.assertEqual(value, original)
        self.assertEqual(self.store.db.total_changes, before)

    def test_shared_metadata_does_not_apply_actor_only_evidence_summaries(self):
        review = dict(version="owner-build", execution=dict(status="stopped", blockers=["partial"],
            actions=[dict(action_id="a", status="unknown", executed_native_steps=None,
                          failure_feedback=dict(witnesses=[.123456789]*20))]),
            answer=dict(revision=2, source="visible evidence"))
        shown = project_metadata(review, "operation_review")
        self.assertEqual(shown["execution"], review["execution"])
        self.assertEqual(shown["answer"], review["answer"])
        self.assertNotIn("details_ref", shown)
        self.assertNotIn("version", shown)
        caps = dict(version="owner-build", max_waypoints=8, motion_preview={"available": True})
        self.assertEqual(project_metadata(caps, "capabilities"),
                         dict(max_waypoints=8, motion_preview={"available": True}))
