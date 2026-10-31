"""Feedback hooks preserve exact action outcomes, phase identity and opt-outs."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auriga.core.config import Config
from auriga.core.presentation.feedback_delivery import action_trajectory_feedback
from auriga.core.storage.store import Store
import test_dual_arm_interface as base
import test_image_feedback_delivery as image_base


class FeedbackCompositionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.identity = dict(run_id="r", episode_id="e", env_instance_id="env", env_generation=1)
        self.store = Store(Path(directory.name), self.identity)
        self.addCleanup(self.store.close)
        self.h = SimpleNamespace(store=self.store, identity=self.identity, config=Config())

    def test_program_internal_and_disabled_feature_do_not_render(self):
        with patch("auriga.runtime.actions.trajectory_feedback.build_action_trajectory_feedback") as build:
            self.assertEqual(action_trajectory_feedback(self.h, {}, {}, {"program_id":"p"}), (None, []))
            self.h.config = self.h.config.with_overrides({"features":{"trajectory_preview":False}})
            self.assertEqual(action_trajectory_feedback(self.h, {}, {}, {}), (None, []))
        build.assert_not_called()


class ActionFeedbackDispatchTests(unittest.TestCase):
    setUp = base.DualArmHarnessTests.setUp

    def test_renderer_failure_does_not_hide_completed_receipt_or_repeat_execution(self):
        self.h.capabilities["bounded_wait"] = dict(max_native_steps=50)
        with patch("auriga.runtime.actions.trajectory_feedback.build_action_trajectory_feedback", side_effect=RuntimeError("image rendering failed")):
            reply = self.h.dispatch("robot_wait", dict(observation_id=self.h.latest["observation_id"],
                native_steps=5), {"tool_call_id":"move"})
        result = json.loads(reply["contentItems"][0]["text"])
        self.assertTrue(reply["success"])
        self.assertEqual(result["receipt"]["status"], "completed")
        self.assertFalse(result["trajectory_feedback"]["available"])
        self.assertEqual(self.robot.executions, 1)
        stored = self.store.read("action_receipt", result["request"]["action_id"])
        self.assertEqual(self.h.execute(result["request"])["receipt"], stored)
        self.assertEqual(self.robot.executions, 1)


class OptionalImageBookkeepingTests(unittest.TestCase):
    setUp = image_base.ImageFeedbackDeliveryTests.setUp

    def move(self):
        original = self.robot.execute
        def completed(*args, **kwargs):
            result = original(*args, **kwargs)
            result["reason"] = None
            return result
        self.robot.execute = completed
        self.h.capabilities["bounded_wait"] = dict(max_native_steps=50)
        return self.h.dispatch("robot_wait", dict(observation_id=self.h.latest["observation_id"],
            native_steps=5), {"tool_call_id":"bookkeeping-move"})

    def test_baseline_failure_preserves_receipt_images_and_future_full_delivery(self):
        with patch("auriga.perception.view_selection.baseline_record", side_effect=RuntimeError("optional baseline failed")):
            reply = self.move()
        result = json.loads(reply["contentItems"][0]["text"])
        self.assertTrue(reply["success"])
        self.assertEqual(result["receipt"]["status"], "completed")
        self.assertEqual(sum(item["type"] == "inputImage" for item in reply["contentItems"]), 3)
        self.assertEqual(self.h._last_delivered_views, {})
        self.assertEqual(self.robot.executions, 1)

    def test_counterfactual_byte_failure_does_not_hide_already_executed_motion(self):
        self.h.observation_content(self.first, {})
        with patch("auriga.runtime.harness.image_url", side_effect=RuntimeError("optional telemetry failed")):
            reply = self.move()
        result = json.loads(reply["contentItems"][0]["text"])
        self.assertTrue(reply["success"])
        self.assertEqual(result["receipt"]["status"], "completed")
        self.assertEqual(self.robot.executions, 1)
        metrics = self.store.records("delivery")[-1]["image_metrics"]
        self.assertIsNone(metrics["omitted_transport_bytes"])
        self.assertEqual(metrics["omitted_transport_bytes_unavailable"], "RuntimeError")
        stored = self.store.read("action_receipt", result["request"]["action_id"])
        self.assertEqual(stored["status"], result["receipt"]["status"])
        self.assertEqual(stored["result_observation_id"], result["receipt"]["result_observation_id"])
        self.assertEqual(self.h.execute(result["request"])["receipt"], stored)
        self.assertEqual(self.robot.executions, 1)

    def test_all_policy_does_not_depend_on_adaptive_baseline_bookkeeping(self):
        self.h.image_settings["feedback_policy"] = "all"
        with patch("auriga.perception.view_selection.baseline_record", side_effect=RuntimeError("unused")) as record:
            reply = self.move()
        record.assert_not_called()
        self.assertTrue(reply["success"])
        self.assertEqual(json.loads(reply["contentItems"][0]["text"])["receipt"]["status"], "completed")
