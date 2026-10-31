"""Bounded waits stop on uncertain evidence, budget, or consecutive quiet samples."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import numpy as np

from auriga.runtime.actions.stability import bounded_settle, validate_options


class StabilityTests(unittest.TestCase):
    def harness(self, budget=50):
        harness = SimpleNamespace(latest={"observation_id": "o0"}, store=object(), identity={},
                                  enable_wait=True, disabled_tools=set(), used=0)
        harness.action_budget = lambda: dict(native_steps_remaining=budget - harness.used)
        harness._action_from_args = lambda name, args: dict(args, action_id="wait" + str(harness.used))
        def execute(action, correlation):
            harness.used += action["native_steps"]
            harness.latest = dict(observation_id="o" + str(harness.used))
            return dict(receipt=dict(status="completed", completed_native_steps=action["native_steps"]))
        harness.execute = Mock(side_effect=execute)
        return harness

    def comparison(self, motion=0, status="tracked_features"):
        return dict(target=dict(status=status, points_before_px=[[0, 0], [1, 1]],
                                points_after_px=[[motion, 0], [motion+1, 1]]))

    def run_settle(self, harness, evidence=None, **options):
        with patch("auriga.runtime.actions.stability._camera_pose", return_value=(np.eye(4), dict(width=100, height=100))), \
             patch("auriga.perception.visual_feedback.compare_observations", return_value=evidence or self.comparison()):
            return bounded_settle(harness, {"roi": [0, 0, 50, 50], **options}, {})

    def test_three_consecutive_qualified_samples_and_real_cost(self):
        harness = self.harness()
        result = self.run_settle(harness)
        self.assertEqual(result["status"], "low_visible_motion")
        self.assertEqual(result["executed_native_steps"], 15)
        self.assertEqual(harness.execute.call_count, 3)
        self.assertEqual(result["final_observation_id"], "o15")

    def test_ambiguous_visibility_is_unknown_and_stops(self):
        harness = self.harness()
        result = self.run_settle(harness, self.comparison(status="lost"))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(harness.execute.call_count, 1)

    def test_budget_and_explicit_limit_bound_actual_waits(self):
        harness = self.harness(budget=7)
        result = self.run_settle(harness, self.comparison(2), max_native_steps=20)
        self.assertEqual(result["status"], "limit_reached")
        self.assertEqual(harness.used, 7)
        harness = self.harness()
        result = self.run_settle(harness, self.comparison(2), max_native_steps=6)
        self.assertEqual(result["status"], "limit_reached")
        self.assertEqual(harness.used, 6)

    def test_unknown_camera_and_bad_roi_do_not_advance_world(self):
        harness = self.harness()
        with patch("auriga.runtime.actions.stability._camera_pose", side_effect=ValueError("camera_unknown")):
            result = bounded_settle(harness, dict(roi=[0, 0, 20, 20]), {})
        self.assertEqual(result["status"], "unknown")
        harness.execute.assert_not_called()
        result = self.run_settle(harness, roi=[90, 0, 50, 50])
        self.assertEqual(result["reason"], "settle_roi_out_of_bounds")
        harness.execute.assert_not_called()

    def test_invalid_request_rejected_before_wait(self):
        for options in ({}, dict(roi=[0, 0, 20, 20], max_native_steps=51),
                        dict(roi=[0, 0, 20, 20], max_motion_px=float("nan"))):
            with self.assertRaises(ValueError):
                validate_options(options)
