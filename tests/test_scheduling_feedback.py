"""Scheduling advice cannot hide receipts, create actions, or force arm ownership."""
import json
import unittest
from unittest.mock import patch

import test_dual_arm_interface as fixture


class SchedulingFeedbackTests(unittest.TestCase):
    setUp = fixture.DualArmHarnessTests.setUp

    def test_failed_dispatch_exposes_advice_without_claiming_physical_state_or_retry(self):
        args = dict(observation_id=self.h.latest['observation_id'], native_steps=5)
        receipt = dict(status='failed', reason='planning_failed', executed_steps=0)
        def failed(action, correlation):
            return dict(request=action, receipt=receipt)
        with patch.object(self.h, 'execute', side_effect=failed) as execute:
            reply = self.h.dispatch('robot_wait', args, {'tool_call_id': 'failed'})
        result = json.loads(reply['contentItems'][0]['text'])
        self.assertEqual(result['receipt']['status'], 'failed')
        self.assertEqual(result['scheduling_advisory']['source_kind'], 'advisory')
        self.assertIn('if one holds an object', result['scheduling_advisory']['suggestion'])
        execute.assert_called_once()
        self.assertEqual(self.robot.executions, 0)

    def test_success_off_and_program_calls_do_not_read_plan_or_emit_advice(self):
        with patch.object(self.h.task_planning, 'current', side_effect=AssertionError('unnecessary read')):
            self.assertIsNone(self.h._scheduling_advisory({'status': 'completed'}, {}))
            self.assertIsNone(self.h._scheduling_advisory({'status': 'failed'}, {'program_id': 'nested'}))
            for flag in ('task_scheduling', 'manipulation_protocol'):
                original = self.h.config
                self.h.config = original.with_overrides({'features': {flag: False}})
                self.assertIsNone(self.h._scheduling_advisory({'status': 'failed'}, {}))
                self.h.config = original

    def test_unavailable_plan_does_not_hide_the_failed_action(self):
        with patch.object(self.h.task_planning, 'current', side_effect=KeyError('unavailable')):
            result = self.h._scheduling_advisory({'status': 'rejected'}, {})
        self.assertEqual(result['kind'], 'scheduling_review')
        self.assertNotIn('dependency_free_plan_stages_to_reassess', result)

