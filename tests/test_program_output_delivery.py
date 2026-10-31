"""Repeated log reads stay small without concealing live status or new context."""
from copy import deepcopy
import json
import unittest

from auriga.core.presentation.program_output_delivery import ProgramOutputDelivery
from auriga.runtime.programs.runner import ProgramRunner
import test_dual_arm_interface as harness_fixture


class ProgramOutputDeliveryTests(unittest.TestCase):
    def test_repetition_preserves_changed_status_and_independent_streams(self):
        delivery = ProgramOutputDelivery()
        result = dict(job_id='job', status='running', stdout_text='a' * 1000,
            stdout_ref='stdout1', stderr_text='b' * 1000, stderr_ref='stderr1')
        original = deepcopy(result)
        first = delivery.project(result, epoch=['thread', 0], call_id='first', detail='summary')
        self.assertEqual(first, result)
        self.assertEqual(delivery.project(result, epoch=['thread', 0], call_id='first', detail='summary'), first)
        result.update(status='awaiting_review', pending_review={'review_id': 'r1'}, stderr_text='new failure' * 100)
        repeated = delivery.project(result, epoch=['thread', 0], call_id='poll', detail='summary')
        self.assertNotIn('stdout_text', repeated)
        self.assertEqual(repeated['stdout_delivery']['previous_tool_call_id'], 'first')
        self.assertEqual(repeated['status'], 'awaiting_review')
        self.assertEqual(repeated['pending_review'], result['pending_review'])
        self.assertEqual(repeated['stderr_text'], result['stderr_text'])
        self.assertEqual(result['stdout_text'], original['stdout_text'])
        full = delivery.project(result, epoch=['thread', 0], call_id='full', detail='full')
        self.assertEqual(full, result)
        restored = delivery.project(result, epoch=['thread', 1], call_id='after-compaction', detail='summary')
        self.assertEqual(restored, result)

    def test_new_artifacts_jobs_and_excerpt_settings_are_not_suppressed(self):
        delivery = ProgramOutputDelivery()
        result = dict(job_id='job', stdout_text='same ends' * 100, stdout_ref='a',
            stdout_excerpt={'byte_size': 5000, 'source_complete': True})
        delivery.project(result, epoch=1, call_id='first', detail='summary')
        for changes in ({'stdout_ref': 'changed-middle'}, {'job_id': 'other-job'},
                {'stdout_excerpt': {'byte_size': 5000, 'source_complete': False}}):
            with self.subTest(changes=changes):
                changed = dict(result, **changes)
                self.assertIn('stdout_text', delivery.project(changed, epoch=1, call_id='next', detail='summary'))
        for epoch, call_id in ((None, 'call'), (1, None)):
            self.assertEqual(delivery.project(result, epoch=epoch, call_id=call_id, detail='summary'), result)
        short = dict(job_id='short', stdout_text='done', stderr_text='')
        for call_id in ('a', 'b'):
            self.assertEqual(delivery.project(short, epoch=1, call_id=call_id, detail='summary'), short)


class ProgramOutputHarnessTests(unittest.TestCase):
    setUp = harness_fixture.DualArmHarnessTests.setUp

    def call(self, name, arguments, call_id):
        wire = self.h.handle_tool(dict(threadId=self.h.thread_id, turnId='turn', callId=call_id,
            tool=name, arguments=arguments))
        self.assertTrue(wire['success'])
        return json.loads(wire['contentItems'][0]['text'])

    def test_legacy_and_program_readers_do_not_consume_actor_delivery(self):
        from auriga.runtime.tools.program_tools import program_display
        self.h.thread_id = 'program-log-reader-isolation'
        self.h.programs = ProgramRunner(self.h.program_services())
        self.addCleanup(self.h.programs.close)
        legacy = self.h.dispatch('program_run', {'source': "print('diagnostic ' * 100)", 'timeout_s': 10},
            {'thread_id': self.h.thread_id, 'turn_id': 'turn', 'tool_call_id': 'legacy-only-read'})
        data = json.loads(legacy['contentItems'][0]['text'])
        self.assertIn('stdout_text', data)
        self.assertEqual(program_display(self.h, data, 'summary', {'program_id': 'program',
            'actor_log_delivery': True, 'tool_call_id': 'program-only-read'}), data)
        polled = self.call('program_status', {'job_id': data['job_id']}, 'first-actor-read')
        self.assertIn('stdout_text', polled)
        self.assertNotIn('stdout_delivery', polled)
        self.assertEqual(self.robot.executions, 0)

    def test_failed_actor_display_does_not_mark_logs_as_delivered(self):
        from unittest.mock import patch
        self.h.thread_id = 'program-log-display-failure'
        self.h.programs = ProgramRunner(self.h.program_services())
        self.addCleanup(self.h.programs.close)
        for method in ('_present_part', 'present'):
            with self.subTest(failed_method=method):
                previous_ids = {job['job_id'] for job in self.store.states('program')}
                params = dict(threadId=self.h.thread_id, turnId='turn', callId='failed-' + method,
                    tool='program_run', arguments={'source': "print('important diagnostic ' * 100)", 'timeout_s': 10})
                with patch.object(self.h.actor_output, method, side_effect=ValueError('synthetic display failure')):
                    if method == 'present':
                        with self.assertRaisesRegex(ValueError, 'synthetic display failure'):
                            self.h.handle_tool(params)
                    else:
                        self.assertFalse(self.h.handle_tool(params)['success'])
                job = next(job for job in self.store.states('program') if job['job_id'] not in previous_ids)
                polled = self.call('program_status', {'job_id': job['job_id']}, 'poll-' + method)
                self.assertIn('important diagnostic', polled['stdout_text'])
                self.assertNotIn('stdout_delivery', polled)
                self.assertEqual(polled['status'], 'succeeded')
                self.assertEqual(polled['returncode'], 0)
        self.assertEqual(self.robot.executions, 0)

    def test_real_program_start_poll_full_and_context_recovery(self):
        self.h.thread_id = 'program-log-thread'
        self.h.programs = ProgramRunner(self.h.program_services())
        self.addCleanup(self.h.programs.close)
        started = self.call('program_run', {'source': "print('begin|' + 'x' * 9000 + '|end-diagnostic')", 'timeout_s': 10}, 'start')
        self.assertEqual(started['status'], 'succeeded')
        self.assertFalse(started['stdout_truncated'])
        self.assertIn('end-diagnostic', started['stdout_text'])
        args = {'job_id': started['job_id']}
        polled = self.call('program_status', args, 'poll')
        self.assertEqual(polled['stdout_delivery']['status'], 'unchanged')
        self.assertNotIn('stdout_text', polled)
        self.assertEqual(polled['status'], 'succeeded')
        self.assertEqual(polled['returncode'], 0)
        self.assertEqual(self.call('program_status', dict(args, detail='full'), 'full')['stdout_text'], started['stdout_text'])
        for index, reset in enumerate((
                lambda: self.store.set_state('runtime', 'image_context_epoch', 'compacted'),
                lambda: self.store.set_state('runtime', 'image_context_epoch', {'source': 'tool_reply_delivery_uncertain'}),
                lambda: self.store.set_state('runtime', 'codex', {'app_server_instance_id': 'reconnected'}))):
            reset()
            restored = self.call('program_status', args, 'reset-' + str(index))
            self.assertEqual(restored['stdout_text'], started['stdout_text'])
        self.assertEqual(len(self.store.states('program')), 1)
        self.assertEqual(self.robot.executions, 0)


if __name__ == '__main__':
    unittest.main()
