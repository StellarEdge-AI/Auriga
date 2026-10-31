"""Recovery advice neither invents a budget nor reads task/evaluator truth."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from auriga.cortex.supervision.recovery_guidance import NEXT_STEP_INSTRUCTIONS, recovery_guidance


class RecoveryGuidanceTests(unittest.TestCase):
    def test_public_context_whitelist_and_full_attempt_budget(self):
        harness = SimpleNamespace(latest=dict(observation_id="obs-current", object_poses="forbidden"),
            action_budget=lambda: dict(native_steps_used=935, native_steps_limit=1050,
                native_steps_remaining=115, task_success="forbidden"),
            robot=Mock(), observer=Mock())
        context, prompt = recovery_guidance(harness, dict(status="away", task_score="forbidden"))
        self.assertEqual(context["observation_id"], "obs-current")
        self.assertEqual(context["action_budget"]["native_steps_remaining"], 115)
        self.assertEqual(context["measured_home_status"], "away")
        self.assertNotIn("forbidden", prompt)
        self.assertIn("release or verification", prompt)
        self.assertIn("withdrawal", prompt)
        self.assertIn("required final return", prompt)
        self.assertIn("one concrete recovery objective", prompt)
        self.assertIn("complete submitted batch", prompt)
        self.assertIn("settling has a separate additional step limit", prompt)
        self.assertNotIn("single-segment", prompt)
        harness.robot.assert_not_called()
        harness.observer.assert_not_called()

    def test_unknown_is_not_zero_and_fault_does_not_obscure_continuation(self):
        for budget in (None, lambda: {}, lambda: dict(native_steps_remaining=True),
                       Mock(side_effect=ValueError("missing budget"))):
            context, prompt = recovery_guidance(SimpleNamespace(action_budget=budget))
            self.assertIsNone(context["observation_id"])
            self.assertTrue(all(value is None for value in context["action_budget"].values()))
            self.assertEqual(context["measured_home_status"], "unknown")
            self.assertNotIn("native_steps_remaining\":0", prompt)

    def test_evidence_context_is_compact_and_does_not_generalize_failure(self):
        context, prompt = recovery_guidance(SimpleNamespace(), evidence=dict(
            version="recovery-evidence-v1", native_action_count=99, native_steps_remaining=11,
            tested_candidate_count=9, earlier_candidate_count=3, task_success="forbidden",
            tested_candidates=[dict(label=str(i), nominal_native_steps=30,
                exceeds_episode_budget=True, object_pose="forbidden") for i in range(9)]))
        self.assertEqual(context["version"], "recovery-guidance-v4")
        # The main request reminder supplies this once, alongside the current plan.
        self.assertNotIn(NEXT_STEP_INSTRUCTIONS, prompt)
        self.assertEqual(len(context["tested_recovery"]["tested_candidates"]), 6)
        self.assertNotIn("forbidden", prompt)
        self.assertIn("never all routes", prompt)
        self.assertIn("does not require consuming all remaining steps", prompt)

    def test_absent_evidence_preserves_previous_prompt(self):
        from auriga.cortex.supervision.recovery_guidance import INSTRUCTIONS
        context, prompt = recovery_guidance(SimpleNamespace(), evidence=None)
        self.assertEqual(context["version"], "recovery-guidance-v1")
        self.assertTrue(prompt.endswith(INSTRUCTIONS))
        self.assertNotIn(NEXT_STEP_INSTRUCTIONS, prompt)
