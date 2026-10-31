"""Operation review consumes permitted evidence without executing recovery."""
from program_fixture import program_services
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np
from jsonschema import ValidationError

from auriga.core.contracts import canonical, digest
from auriga.core.config import Config
from auriga.apps.gateway.workbench import Workbench
from auriga.runtime.harness import Harness
from auriga.cortex.context.operation_verification import PHASES, TOOL_REQUIRED
from auriga.runtime.programs.runner import ProgramRunner, model_wait, program_remaining
from auriga.core.storage.store import Store
from auriga.cortex.session.visual_review import MODEL, VisualReviewer


def answer(**changes):
    return dict(assessment="met", same_target_identified="supported", visibility_after="visible",
                target_motion="displaced", gripper_relation="held", support_relation="visibly_separated",
                destination_relation="not_applicable", gripper_withdrawal="not_applicable",
                before_evidence="The striped target is visible beside the fingers on the table.",
                after_evidence="The same stripe is visible between the raised fingers above the table.",
                execution_evidence="One completed move has fresh measured arm state; closure is a command only.",
                uncertainties=[], follow_up="none", follow_up_reason="", recovery_feedback="", **changes)


class OperationVerificationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.identity = dict(run_id="r", episode_id="e", env_instance_id="i", env_generation=1)
        self.store = Store(temporary.name, self.identity)
        self.addCleanup(self.store.close)
        self.reviewer = VisualReviewer(SimpleNamespace(base_url="http://127.0.0.1:1", _token="fixture", model=MODEL))
        self.observation("before", 10, [0, 0, .1])
        self.observation("after", 15, [0, 0, .12])
        self.request = dict(**self.identity, action_id="lift", based_on_observation_id="before", type="EEAction",
                            path=dict(kind="backend_free", waypoints=[dict(left=dict(position_m=[0, 0, .12],
                                      quaternion_xyzw=[0, 0, 0, 1]))]),
                            gripper_events=[dict(arm="left", phase="after_motion", goal_normalized=0)], timeout_s=20)
        self.receipt = dict(status="completed", result_observation_id="after", executed_steps=5,
                            execution_interval=dict(start_sim_step=10, end_sim_step=15, step_domain="fixture"))
        self.store.save("action_request", "lift", self.request)
        self.args = dict(phase="short_lift", target="striped visible block", expected_result="Block visibly moves with gripper off table",
                         action_ids=["lift"], before_observation_id="before", after_observation_id="after",
                         camera_id="head", roi=[1, 1, 8, 8])

    def observation(self, name, step, position, *, scope="actor"):
        pixels = np.zeros((20, 20, 3), np.uint8)
        pixels[3:7, 3:7] = [255, step, 100]
        rgb = self.store.put_artifact(cv2.imencode(".png", pixels)[1].tobytes(), "rgb", "image/png", scope)
        state = dict(arms=dict(left=dict(q_rad=[0]*6,
                     ee_pose=dict(position_m=position, quaternion_xyzw=[0, 0, 0, 1], frame_id="env_origin", tcp_config_id="tcp"),
                     gripper=dict(goal_normalized=0, goal_source_kind="command"))), sim_step=step, step_domain="fixture")
        state_ref = self.store.put_artifact(canonical(state).encode(), "robot_state", "application/json")
        self.store.save("observation", name, dict(**self.identity, observation_id=name, observation_profile="rgb_proprio_v1",
                        sim_step=step, step_domain="fixture", robot_state_ref=state_ref,
                        cameras=[dict(camera_id="head", frame_id=name, rgb_ref=rgb, width=20, height=20)]))

    def raw(self, value, *, model=MODEL, status="completed", tool="record_operation_verification", calls=1):
        result = dict(model=model, status=status, id="response-fixture", usage=dict(output_tokens=80),
                      answer=value, thread_id="fresh-fixture", turn_id="fixture-turn",
                      context_policy="fresh_ephemeral_thread_selected_evidence_only_v1", backend="official_codex_app_server_sdk")
        return result

    def review(self, value=None, **changes):
        with patch.object(self.reviewer, "_request", return_value=self.raw(value or answer())) as request:
            result = self.reviewer.review_operation(self.store, self.identity, **{**self.args, **changes})
        return result, request

    def test_positive_review_consumes_actual_images_receipt_and_measured_state(self):
        self.store.save("action_receipt", "lift", self.receipt)
        cancel = threading.Event()
        result, request = self.review(cancel_event=cancel, deadline=time.monotonic()-10)
        self.assertEqual(result["answer"]["assessment"], "met")
        self.assertEqual(result["execution"]["status"], "completed")
        self.assertEqual(result["assessment_constraints"], [])
        evidence = result["execution"]["actions"][0]
        self.assertEqual(evidence["receipt_sha256"], digest(self.receipt))
        self.assertEqual(evidence["request_sha256"], digest(self.request))
        self.assertEqual(result["execution"]["after_robot_state"]["state"]["arms"]["left"]["ee_pose"]["position_m"], [0, 0, .12])
        self.assertEqual([source["observation_id"] for source in result["sources"]], ["before", "after"])
        self.assertTrue(all(source["sha256"] for source in result["sources"]))
        payload = request.call_args.args[0]
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["effort"], "high")
        self.assertEqual(sum(content["type"] == "image" for content in payload["input"]), 2)
        prompt = payload["input"][0]["text"]
        self.assertIn('"executed_steps":5', prompt)
        self.assertIn('"position_m":[0,0,0.12]', prompt)
        self.assertIn("closed jaw never proves success", prompt.lower())
        self.assertEqual(request.call_args.kwargs, dict(cancel_event=cancel))
        self.assertNotIn("usage", result)
        self.assertTrue(result["actor_decides_next_action"])
        self.assertEqual(result["task_completion"], "not_assessed")
        event = self.store.events()[-1]
        self.assertEqual(event["type"], "visual.model_reviewed")
        self.assertEqual(event["payload"]["review_kind"], "operation_verify")
        self.assertEqual(event["payload"]["usage"]["output_tokens"], 80)
        self.assertEqual(self.store.artifact(event["payload"]["request_ref"])[0]["access_scope"], "observer")

    def test_unconfirmed_execution_cannot_be_promoted_by_positive_model_answer(self):
        result, _ = self.review()
        self.assertEqual(result["execution"]["status"], "unknown")
        self.assertFalse(result["execution"]["actions"][0]["receipt_recorded"])
        self.assertIsNone(result["execution"]["actions"][0]["receipt_sha256"])
        self.assertEqual(result["answer"]["assessment"], "uncertain")
        self.assertEqual(result["answer"]["follow_up"], "return_to_actor")
        self.assertIn("execution_not_completed:lift", result["execution"]["blockers"])

    def test_partial_execution_is_preserved_and_negative_feedback_is_model_led(self):
        self.store.save("action_receipt", "lift", {**self.receipt, "status": "failed", "reason": "tracking_divergence", "executed_steps": 2})
        value = answer()
        value.update(assessment="not_met", gripper_relation="separated", uncertainties=["Target partly occluded"],
                     follow_up="inspect_fresh_rgb", follow_up_reason="Resolve target location before choosing recovery",
                     recovery_feedback="Review the visible target and measured arm pose before a new approach.")
        result, _ = self.review(value)
        self.assertEqual(result["answer"], value)
        self.assertEqual(result["execution"]["actions"][0]["feedback"]["executed_steps"], 2)
        self.assertEqual(result["execution"]["status"], "incomplete")
        result, _ = self.review()
        self.assertEqual(result["answer"]["assessment"], "met")
        self.assertEqual(result["execution"]["status"], "incomplete")
        self.assertEqual(result["execution"]["actions"][0]["feedback"]["status"], "failed")

    def test_different_result_observation_and_replayed_frame_remain_unconfirmed(self):
        self.observation("newer", 16, [0, 0, .13])
        self.store.save("action_receipt", "lift", self.receipt)
        result, _ = self.review(after_observation_id="newer")
        self.assertEqual(result["answer"]["assessment"], "uncertain")
        self.assertIn("final_action_result_observation_mismatch", result["assessment_constraints"])
        result, _ = self.review(after_observation_id="before")
        self.assertIn("no_new_simulation_step", result["assessment_constraints"])

    def test_phases_are_independent_model_judgments_without_fixed_sequence(self):
        self.store.save("action_receipt", "lift", self.receipt)
        for phase in PHASES:
            with self.subTest(phase=phase):
                value = answer()
                if phase == "approach":
                    value.update(gripper_relation="near", support_relation="visibly_supported")
                if phase in ("place", "release", "retreat"):
                    value.update(support_relation="visibly_supported", destination_relation="visibly_at_destination")
                if phase in ("release", "retreat"):
                    value.update(gripper_relation="separated", target_motion="roughly_stationary")
                if phase == "retreat":
                    value["gripper_withdrawal"] = "visually_supported"
                result, _ = self.review(value, phase=phase, destination="visible tray" if phase in ("place", "release", "retreat") else None)
                self.assertEqual(result["phase"], phase)
                self.assertEqual(result["answer"]["assessment"], "met")
        value = answer()
        value.update(assessment="uncertain", follow_up="inspect_another_camera", follow_up_reason="Support is occluded")
        result, _ = self.review(value)
        self.assertEqual(result["answer"], value)

    def test_multiple_actions_need_linked_observation_chain(self):
        self.observation("middle", 12, [0, 0, .11])
        self.store.save("action_receipt", "lift", {**self.receipt, "result_observation_id": "middle"})
        self.store.save("action_request", "second", {**self.request, "action_id": "second", "based_on_observation_id": "before"})
        self.store.save("action_receipt", "second", self.receipt)
        result, _ = self.review(action_ids=["lift", "second"])
        self.assertEqual(result["answer"]["assessment"], "uncertain")
        self.assertIn("action_observation_chain_mismatch:second", result["assessment_constraints"])

    def test_bad_schema_wrong_model_and_extra_calls_fail_with_private_provenance(self):
        self.store.save("action_receipt", "lift", self.receipt)
        invalid = answer()
        invalid["invented_contact_truth"] = True
        for raw, error in [(self.raw(invalid), ValidationError), (self.raw(answer(), model="substitute"), ValueError),
                           (self.raw(answer(), status="incomplete"), ValueError)]:
            with self.subTest(error=error), patch.object(self.reviewer, "_request", return_value=raw):
                with self.assertRaises(error):
                    self.reviewer.review_operation(self.store, self.identity, **self.args)
                event = self.store.events()[-1]
                self.assertEqual(event["type"], "visual.model_review_failed")
                self.assertTrue(event["payload"]["response_ref"])

    def test_non_actor_images_and_privileged_receipts_never_reach_model(self):
        self.observation("private", 15, [0, 0, .12], scope="observer")
        self.store.save("action_receipt", "lift", {**self.receipt, "contact_points": [[0, 0, 0]]})
        with patch.object(self.reviewer, "_request") as request:
            with self.assertRaisesRegex(ValueError, "access_scope_denied"):
                self.reviewer.review_operation(self.store, self.identity, **{**self.args, "after_observation_id": "private"})
            with self.assertRaisesRegex(ValueError, "privileged_data"):
                self.reviewer.review_operation(self.store, self.identity, **self.args)
            request.assert_not_called()

    def test_wrong_episode_action_is_rejected_before_model_and_cancel_is_recorded(self):
        self.store.save("action_request", "old", {**self.request, "action_id": "old", "env_generation": 2})
        with patch.object(self.reviewer, "_request") as request:
            with self.assertRaisesRegex(ValueError, "action_identity_mismatch"):
                self.reviewer.review_operation(self.store, self.identity, **{**self.args, "action_ids": ["old"]})
            request.assert_not_called()
        cancel = threading.Event()
        cancel.set()
        with patch.object(self.reviewer, "_request", side_effect=TimeoutError("cancelled")):
            with self.assertRaises(TimeoutError):
                self.reviewer.review_operation(self.store, self.identity, **self.args, cancel_event=cancel)
        event = self.store.events()[-1]
        self.assertEqual(event["type"], "visual.model_review_cancelled")
        self.assertIsNone(event["payload"]["usage"])

    def test_invalid_requests_do_not_invoke_model(self):
        for changes in (dict(phase="made_up"), dict(target=" "), dict(action_ids=[]), dict(action_ids=["lift", "lift"])):
            with self.subTest(changes=changes), patch.object(self.reviewer, "_request") as request:
                with self.assertRaises(ValueError):
                    self.reviewer.review_operation(self.store, self.identity, **{**self.args, **changes})
                request.assert_not_called()

    def test_transport_delegates_to_official_agent_without_response_deadline(self):
        self.reviewer.client = SimpleNamespace(model=MODEL, review_evidence=Mock(return_value={"fixture": True}))
        payload = dict(model=MODEL, input=[dict(type="text", text="Evidence only")], outputSchema={})
        cancel = threading.Event()
        self.assertEqual(self.reviewer._request(payload, "fixture", cancel_event=cancel, deadline=time.monotonic()-100), {"fixture": True})
        self.reviewer.client.review_evidence.assert_called_once_with(input_items=payload["input"], output_schema={},
                                                                     request_id="fixture", cancel_event=cancel)

    def harness(self):
        robot = SimpleNamespace(execute=Mock(), observe=Mock())
        config = Config.from_dict(dict(features=dict(observation_profile="rgb_proprio_v1", motion_review=False,
                                                     completion_guidance="none")))
        harness = Harness(self.store, robot, config=config, motion_review=False)
        harness.visual_reviewer = self.reviewer
        return harness, robot

    def test_harness_dispatch_reviews_evidence_without_changing_action_or_receipt(self):
        self.store.save("action_receipt", "lift", self.receipt)
        harness, robot = self.harness()
        with patch.object(self.reviewer, "_request", return_value=self.raw(answer())) as request:
            returned = harness.dispatch("vision_review", dict(self.args, evidence_mode="operation"),
                                        dict(tool_call_id="verification"))
        self.assertTrue(returned["success"])
        result = json.loads(returned["contentItems"][0]["text"])
        self.assertEqual(result["answer"]["assessment"], "met")
        self.assertEqual(result["execution"]["actions"][0]["receipt_sha256"], digest(self.receipt))
        self.assertEqual(result["sources"][1]["observation_id"], "after")
        self.assertEqual(self.store.read("action_request", "lift"), self.request)
        self.assertEqual(self.store.read("action_receipt", "lift"), self.receipt)
        self.assertEqual(harness.states["lift"], self.receipt)
        self.assertEqual(harness.action_count, 0)
        robot.execute.assert_not_called()
        robot.observe.assert_not_called()
        self.assertIs(request.call_args.kwargs["cancel_event"], harness.cancel)
        self.assertNotIn("deadline", request.call_args.kwargs)

    def test_harness_binds_program_cancel_without_model_deadline(self):
        self.store.save("action_receipt", "lift", self.receipt)
        harness, robot = self.harness()
        cancel = threading.Event()
        harness.programs = ProgramRunner(program_services(harness))
        harness.programs.entries['program-fixture'] = dict(cancel=cancel, deadline=time.monotonic()+60)
        with patch.object(self.reviewer, "_request", return_value=self.raw(answer())) as request:
            returned = harness.dispatch("vision_review", dict(self.args, evidence_mode="operation"),
                                        dict(tool_call_id="program-verification", program_id="program-fixture"))
        self.assertTrue(returned["success"])
        self.assertEqual(request.call_args.kwargs, dict(cancel_event=cancel))
        robot.execute.assert_not_called()
        cancel.set()
        with patch.object(self.reviewer, "_request") as request:
            with self.assertRaisesRegex(ValueError, "program_channel_expired"):
                harness.dispatch("vision_review", dict(self.args, evidence_mode="operation"),
                                 dict(tool_call_id="expired-verification", program_id="program-fixture"))
            request.assert_not_called()

    def test_unified_visual_review_routes_operation_and_preserves_full_evidence(self):
        self.store.save("action_receipt", "lift", self.receipt)
        harness, robot = self.harness()
        with patch.object(self.reviewer, "_request", return_value=self.raw(answer())):
            response = harness.invoke("vision_review", dict(self.args, evidence_mode="operation"),
                                        dict(tool_call_id="unified-review"), role="actor")
        raw = response.data
        shown = json.loads(harness.actor_output.present(response)["contentItems"][0]["text"])
        self.assertEqual(shown["answer"]["assessment"], "met")
        self.assertEqual(shown["execution"]["action_ids"], ["lift"])
        self.assertNotIn("actions", shown["execution"])
        self.assertTrue(shown["details_ref"].startswith("artifact:ref_"))
        self.assertIn("actions", raw["execution"])
        self.assertEqual(self.store.read("action_receipt", "lift"), self.receipt)
        robot.execute.assert_not_called()

    def test_program_helper_can_exceed_nominal_budget_and_next_call_still_runs(self):
        self.store.save("action_receipt", "lift", self.receipt)
        harness, robot = self.harness()
        runner = harness.programs = ProgramRunner(program_services(harness))
        self.addCleanup(runner.close)
        source = ("from auriga_client import AurigaClient\nc=AurigaClient()\n"
                  "print(c.call('vision_review', " + repr(dict(self.args, evidence_mode="operation")) + ")['success'])\n"
                  "print(c.call('robot_action_status', {'action_id':'lift'})['success'])\n")
        observed = {}
        def slow_request(*args, **kwargs):
            entry = next(iter(runner.entries.values()))
            observed["remaining_at_start"] = program_remaining(entry)
            observed["wait_started"] = entry["model_wait_started_at"]
            # The program monitor remains active and sees the original deadline
            # pass while only this program's Sol request is in flight.
            time.sleep(1.25)
            observed["cancelled"] = entry["cancel"].is_set()
            observed["remaining_at_end"] = program_remaining(entry)
            return self.raw(answer())
        with patch.object(self.reviewer, "_request", side_effect=slow_request):
            result = runner.run(dict(source=source, timeout_s=1, max_calls=2),
                                dict(thread_id="thread", turn_id="turn", tool_call_id="slow-sol"))
        self.assertEqual(result["status"], "succeeded", result.get("stderr_text"))
        self.assertEqual(result["calls"], 2)
        self.assertIn("True\nTrue", result["stdout_text"])
        self.assertIsNotNone(observed["wait_started"])
        self.assertFalse(observed["cancelled"])
        self.assertEqual(observed["remaining_at_start"], observed["remaining_at_end"])
        self.assertGreaterEqual(result["model_wait_s"], 1.25)
        self.assertEqual(result["timeout_semantics"], "work_wall_time_excluding_own_model_and_main_review_waits")
        robot.execute.assert_not_called()

    def test_explicit_program_cancel_still_interrupts_waiting_sol(self):
        self.store.save("action_receipt", "lift", self.receipt)
        harness, robot = self.harness()
        runner = harness.programs = ProgramRunner(program_services(harness))
        self.addCleanup(runner.close)
        entered = threading.Event()
        def waiting_request(*args, **kwargs):
            entered.set()
            self.assertTrue(kwargs["cancel_event"].wait(4), "Program cancellation did not reach Sol")
            raise TimeoutError("visual_review_program_cancelled")
        source = ("from auriga_client import AurigaClient\n"
                  "AurigaClient().call('vision_review', " + repr(dict(self.args, evidence_mode="operation")) + ")\n")
        results = []
        with patch.object(self.reviewer, "_request", side_effect=waiting_request):
            worker = threading.Thread(target=lambda: results.append(runner.run(dict(source=source, timeout_s=1),
                                      dict(thread_id="thread", turn_id="turn", tool_call_id="cancel-sol"))))
            worker.start()
            self.assertTrue(entered.wait(4))
            entry = next(iter(runner.entries.values()))
            runner.cancel(entry["job_id"])
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertTrue(results[0]["stop_confirmed"])
        self.assertIsNone(entry["model_wait_started_at"])
        robot.execute.assert_not_called()

    def test_harness_channel_restores_own_work_time_after_sol_wait(self):
        self.store.save("action_receipt", "lift", self.receipt)
        harness, robot = self.harness()
        clock = [100.0]
        entry = dict(cancel=threading.Event(), deadline=120.0)
        harness.programs = ProgramRunner(program_services(harness))
        harness.programs.entries['budget-fixture'] = entry
        correlation = dict(program_id="budget-fixture")
        with patch("auriga.runtime.programs.runner.time.monotonic", side_effect=lambda: clock[0]):
            with model_wait(entry):
                clock[0] += 1000
                self.assertIs(harness._review_controls(correlation)['cancel_event'], entry['cancel'])
            self.assertIs(harness._review_controls(correlation)['cancel_event'], entry['cancel'])
            self.assertEqual(program_remaining(entry), 20.0)
            clock[0] += 21
            with self.assertRaisesRegex(ValueError, "program_channel_expired"):
                harness._review_controls(correlation)

    def test_legacy_vision_review_pauses_only_its_program_request_wait(self):
        self.store.save("action_receipt", "lift", self.receipt)
        harness, robot = self.harness()
        clock = [100.0]
        entry = dict(cancel=threading.Event(), deadline=120.0)
        harness.programs = ProgramRunner(program_services(harness))
        harness.programs.entries['legacy-fixture'] = entry
        value = dict(visibility_after="visible", motion="displaced", after_center_px=[10, 10],
                     retained_with_gripper="visually_supported", evidence="Visible target moved with fingers.")
        raw = dict(model=MODEL, status="completed", answer=value)
        def request(*args, **kwargs):
            clock[0] += 1000
            self.assertEqual(program_remaining(entry), 20.0)
            return raw
        args = {key: self.args[key] for key in ("before_observation_id", "after_observation_id", "camera_id", "roi", "target")}
        with patch("auriga.runtime.programs.runner.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(self.reviewer, "_request", side_effect=request):
            result = harness.dispatch("vision_review", args, dict(program_id="legacy-fixture", tool_call_id="legacy-sol"))
            self.assertEqual(program_remaining(entry), 20.0)
        self.assertTrue(result["success"])
        self.assertEqual(entry["model_wait_s"], 1000.0)
        robot.execute.assert_not_called()

    def test_harness_disabled_or_absent_reviewer_rejects_before_processing(self):
        self.store.save("action_receipt", "lift", self.receipt)
        harness, robot = self.harness()
        with patch.object(self.reviewer, "_request") as request:
            harness.disabled_tools = frozenset({"operation_verify"})
            with self.assertRaisesRegex(ValueError, "tool_disabled_by_config:operation_verify"):
                harness.dispatch("vision_review", dict(self.args, evidence_mode="operation"),
                                 dict(tool_call_id="disabled"))
            harness.disabled_tools = frozenset({"vision_review"})
            with self.assertRaisesRegex(ValueError, "tool_disabled_by_config:vision_review"):
                harness.dispatch("vision_review", dict(self.args, evidence_mode="operation"),
                                 dict(tool_call_id="disabled-unified"))
            harness.disabled_tools = frozenset()
            harness.visual_reviewer = None
            with self.assertRaisesRegex(ValueError, "visual_review_not_enabled_for_run"):
                harness.dispatch("vision_review", dict(self.args, evidence_mode="operation"),
                                 dict(tool_call_id="unavailable"))
            request.assert_not_called()
        robot.execute.assert_not_called()


class OperationToolExposureTest(unittest.IsolatedAsyncioTestCase):
    async def test_actor_tools_follow_default_off_explicit_sol_and_disabled_tool(self):
        for overrides, enabled in (({}, False), ({"visual_review_model": MODEL}, True),
                                   ({"visual_review_model": None}, False),
                                   ({"visual_review_model": MODEL, "disabled_tools": ["operation_verify"]}, False)):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as temporary:
                config = Config.from_dict(dict(features=dict(motion_review=False, **overrides)))
                workbench = Workbench(Path(temporary) / "run", config=config)
                try:
                    self.assertNotIn("vision_review", {tool["name"] for tool in workbench.actor_tools()})
                    self.assertIsNone(workbench.harness.visual_reviewer)
                    if enabled:
                        workbench.harness.visual_reviewer = Mock()
                    catalog = workbench.actor_tools()
                    names = {tool["name"] for tool in catalog}
                    self.assertNotIn("operation_verify", names)
                    self.assertEqual("vision_review" in names, enabled)
                    if "vision_review" in names:
                        schema = next(tool["inputSchema"] for tool in catalog if tool["name"] == "vision_review")
                        self.assertEqual("oneOf" in schema, enabled)
                    self.assertEqual(workbench.visual_review_model, config.features.visual_review_model)
                    self.assertIsNone(workbench.robot.process)
                    if not overrides:
                        self.assertIsNone(workbench.visual_review_model)
                    self.assertIn("action_ids", TOOL_REQUIRED)
                finally:
                    await workbench.close()
