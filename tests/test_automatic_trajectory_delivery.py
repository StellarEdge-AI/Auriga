"""The default actor policy preserves automatic q and full-resolution images."""
import base64
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import patch

from PIL import Image
from auriga.core.config import Config
from auriga.core.contracts import canonical
from auriga.runtime.actions.trajectory_feedback import build_action_trajectory_feedback
import test_trajectory_feedback as fixture


class AutomaticTrajectoryDeliveryTests(unittest.TestCase):
    setUp = fixture.ActionTrajectoryFeedbackTests.setUp
    put = fixture.ActionTrajectoryFeedbackTests.put
    receipt = fixture.ActionTrajectoryFeedbackTests.receipt
    rows = staticmethod(fixture.ActionTrajectoryFeedbackTests.rows)

    def test_default_retains_both_q_paths_but_one_measured_image_and_full_artifact(self):
        self.harness.config = Config()
        receipt = self.receipt(dict(actions=self.rows(2), completed_native_steps=2), measured_error=.01)
        receipt.update(position_error_m=.001, orientation_error_rad=.02)
        original = deepcopy(receipt)
        result, images = build_action_trajectory_feedback(self.harness, self.action, receipt, {})
        self.assertTrue(result['available'], result)
        self.assertEqual(result['delivery'], 'automatic_sampled_joints_and_image')
        self.assertEqual(receipt, original)
        self.assertEqual(len(images), 1)
        self.assertEqual(set(result['overlays']), {'measured'})
        self.assertEqual(result['planned']['samples'][-1]['arms']['left']['q_rad'], [.2]*6)
        self.assertEqual(result['measured']['samples'][-1]['arms']['left']['q_rad'], [.2+.01]*6)
        with Image.open(io.BytesIO(base64.b64decode(images[0]['imageUrl'].split(',', 1)[1]))) as image:
            self.assertEqual(image.size, (1920, 480))
        full = json.loads(self.store.resolve_artifact(result['trajectory_feedback_ref'], ('actor',))[1])
        self.assertEqual(full['source_refs']['planned_trace_ref'], receipt['planned_trace_ref'])
        self.assertEqual(len(full['paths']['planned']['samples']), 3)
        self.assertEqual(full['paths']['planned']['samples'][-1]['arms']['left']['q_rad'], [.2]*6)
        self.assertIn('link_positions_m', full['paths']['planned']['samples'][-1]['arms']['left'])
        self.assertNotIn('FORBIDDEN', canonical(full))

    def test_diagnostic_mode_preserves_optional_routine_summary_policy(self):
        self.harness.config = Config().with_overrides({'features': {'action_trajectory_delivery': 'diagnostic'}})
        receipt = self.receipt(dict(actions=self.rows(2), completed_native_steps=2))
        with patch('auriga.runtime.actions.inspection._trajectory_image', side_effect=AssertionError('routine render')):
            result, images = build_action_trajectory_feedback(self.harness, self.action, receipt, {})
        self.assertEqual(result['delivery'], 'routine_execution_summary')
        self.assertNotIn('q_rad', canonical(result))
        self.assertEqual(images, [])

    def test_failure_retains_planned_and_measured_diagnostics_in_both_modes(self):
        for mode in ('automatic', 'diagnostic'):
            with self.subTest(mode=mode):
                self.harness.config = Config().with_overrides({'features': {'action_trajectory_delivery': mode}})
                receipt = self.receipt(dict(actions=self.rows(2), completed_native_steps=2))
                receipt.update(status='stopped', reason='tracking_error')
                result, images = build_action_trajectory_feedback(self.harness, self.action, receipt, {})
                self.assertEqual(len(images), 2)
                self.assertEqual(set(result['overlays']), {'measured', 'planned'})
                self.assertIn('q_rad', canonical(result))
