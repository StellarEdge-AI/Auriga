"""CPU regressions for generation ownership, observations, and program stop ACKs."""
from program_fixture import program_services
import asyncio
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auriga.core.config import Config
from auriga.apps.gateway.workbench import Workbench
from auriga.runtime.harness import Harness
from auriga.runtime.programs.runner import ProgramRunner
from auriga.robot.backends.robodojo.robodojo import RobotBackend, FRAME_ID, TCP_CONFIG_ID
from auriga.core.storage.store import Store
from test_harness import FixtureRobot


class RuntimeLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.identity = dict(run_id='lifecycle', episode_id='ep', env_instance_id='env', env_generation=1)
        self.store = Store(root / 'run', self.identity)
        self.addCleanup(self.store.close)
        self.robot = FixtureRobot(root / 'robot')
        self.harness = Harness(self.store, self.robot, config=Config.from_dict({'features': {
            'observation_profile': 'rgb_proprio_v1', 'completion_guidance': 'none'}}))
        self.initial = self.harness.start()['observation_id']

    def action(self, identifier, observation=None):
        return dict(schema_version='0.1', **self.identity, world_kind='observed', action_id=identifier,
            based_on_observation_id=observation or self.initial, frame_id=FRAME_ID, tcp_config_id=TCP_CONFIG_ID,
            mode='absolute', limits_profile='simulation-slow-v1', timeout_s=10,
            path=dict(kind='backend_free', orientation='backend_free', blend_radius_m=0,
                position_tolerance_m=.005, waypoints=[dict(left=dict(
                    position_m=[.01, 0, 1], quaternion_xyzw=[0, 0, 0, 1]))]), gripper_events=[])

    def test_reset_retains_old_worker_until_exit_is_confirmed(self):
        backend = object.__new__(RobotBackend)
        backend.operation_lock = threading.Lock()
        old = SimpleNamespace(alive=True)
        backend.process, backend.failure, backend.capabilities = old, None, {'old': True}
        backend.seed, backend.layout_id = 0, 0
        backend.validate_reset = Mock()
        backend._request = Mock(return_value={'robot_state': {'synthetic': True}})
        backend.close = Mock(return_value={'process_exit_confirmed': False})
        backend.start = Mock()
        with self.assertRaisesRegex(RuntimeError, 'reset_requires_previous_worker_exit'):
            backend.reset(seed=1, layout_id=2)
        self.assertIs(backend.process, old)
        self.assertEqual((backend.seed, backend.layout_id), (0, 0))
        backend.start.assert_not_called()

    def test_missing_terminal_capture_requires_fresh_observation_without_replaying_action(self):
        execute = self.robot.execute
        def missing_capture(*args, **kwargs):
            result = execute(*args, **kwargs)
            result.update(terminal_observation=None, terminal_observation_error='synthetic capture failure')
            return result
        with patch.object(self.robot, 'execute', side_effect=missing_capture):
            first = self.harness.execute(self.action('first'))
            self.assertEqual(first['receipt']['status'], 'completed')
            self.assertIsNone(first['receipt']['result_observation_id'])
            self.assertIsNone(self.harness.latest)
            self.assertEqual(self.harness.execute(self.action('first')), first)
            refused = self.harness.execute(self.action('stale'))['receipt']
            self.assertEqual(refused['status'], 'rejected')
            self.assertEqual(refused['reason'], 'current_observation_unavailable')
            self.assertEqual(self.robot.executions, 1)
        fresh = self.harness.observe()
        self.assertNotEqual(fresh['observation_id'], self.initial)
        completed = self.harness.execute(self.action('after-fresh', fresh['observation_id']))
        self.assertEqual(completed['receipt']['status'], 'completed')
        self.assertEqual(self.robot.executions, 2)

    def test_zero_motion_rejection_keeps_observation_and_unknown_cannot_be_unblocked_by_observe(self):
        with patch.object(self.robot, 'execute', return_value=dict(status='rejected',
                physics_advanced=False, executed_native_steps=0, terminal_observation=None)):
            self.harness.execute(self.action('rejected'))
        self.assertEqual(self.harness.latest['observation_id'], self.initial)
        with patch.object(self.robot, 'execute', side_effect=RuntimeError('lost acknowledgement')):
            result = self.harness.execute(self.action('unknown'))
        self.assertEqual(result['receipt']['status'], 'unknown')
        fresh = self.harness.observe()
        self.assertFalse(self.harness.accepting)
        self.assertEqual(self.harness.execute(self.action('after-unknown', fresh['observation_id']))[
            'receipt']['status'], 'rejected')

    def test_worker_terminal_sample_failure_preserves_receipt_and_requires_fresh_observe(self):
        from auriga.robot.motion.execution_evidence import ExecutionEvidence
        from test_native_wait import fake_adapter, wait_action
        adapter, dispatched = fake_adapter()
        fixture_observe, native_state = self.robot.observe, adapter.state
        pose = fixture_observe()['robot_state']['arms']['left']['ee_pose']
        def state():
            value = native_state()
            for arm in value['arms'].values():
                arm['ee_pose'] = dict(pose)
            return value
        adapter.state = state
        adapter.observe = lambda: dict(fixture_observe(), robot_state=adapter.state(), sim_step=adapter.control_step)
        self.robot.observe = adapter.observe
        self.robot.execute = Mock(side_effect=lambda action, progress_callback, cancel_flag:
            adapter.execute(action, progress_callback, cancel_flag))
        self.harness.capabilities.update(path_kinds=['backend_free', 'hold'],
            bounded_wait=adapter.capabilities()['bounded_wait'])
        action = wait_action(1, **self.identity, world_kind='observed', based_on_observation_id=self.initial)
        finish = ExecutionEvidence.finish
        def fail_final_sample(evidence, **kwargs):
            finish(evidence, **kwargs)
            adapter.state = Mock(side_effect=OSError('terminal state unavailable'))
        with patch.object(ExecutionEvidence, 'finish', fail_final_sample):
            result = self.harness.execute(action)
        receipt = result['receipt']
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(receipt['completed_native_steps'], 1)
        self.assertEqual(receipt['executed_native_steps'], 1)
        self.assertEqual(receipt['executed_control_steps'], 10)
        self.assertFalse(receipt['trace_sampling']['actual_boundary_samples'])
        self.assertIsNone(receipt['result_observation_id'])
        self.assertIsNone(self.harness.latest)
        self.assertEqual(self.store.read('action_receipt', action['action_id']), receipt)
        self.assertEqual(self.harness.execute(action), result)
        refused = self.harness.execute(dict(action, action_id='stale-after-worker-sample'))['receipt']
        self.assertEqual(refused['reason'], 'current_observation_unavailable')
        self.assertEqual(len(dispatched), 1)
        adapter.state = state
        fresh = self.harness.observe()
        completed = self.harness.execute(dict(action, action_id='fresh-after-worker-sample',
            based_on_observation_id=fresh['observation_id']))
        self.assertEqual(completed['receipt']['status'], 'completed')
        self.assertEqual(len(dispatched), 2)

    def test_completed_ack_survives_terminal_image_publication_failure(self):
        self._check_acknowledged_terminal_image_failure("completed")

    def test_partial_ack_survives_terminal_image_publication_failure(self):
        self._check_acknowledged_terminal_image_failure("partial")

    def _check_acknowledged_terminal_image_failure(self, status):
        execute = self.robot.execute
        initial = self.harness.current_observation()['observation_id']
        action = self.action('missing-image-' + status, initial)

        def missing_image(*args, **kwargs):
            result = execute(*args, **kwargs)
            result.update(status=status, executed_native_steps=3,
                executed_control_steps=9, completed_native_steps=2 if status == 'partial' else 3)
            result['terminal_observation']['cameras'][0]['png_path'] = str(
                self.store.root / ('missing-terminal-' + status + '.png'))
            return result

        before = self.robot.executions
        with patch.object(self.robot, 'execute', side_effect=missing_image):
            result = self.harness.execute(action)
        receipt = result['receipt']
        self.assertEqual(receipt['status'], status)
        self.assertEqual(receipt['executed_native_steps'], 3)
        self.assertEqual(receipt['executed_control_steps'], 9)
        self.assertEqual(receipt['completed_native_steps'], 2 if status == 'partial' else 3)
        self.assertIsNone(receipt['result_observation_id'])
        self.assertEqual(receipt['post_execution_error']['phase'],
            'after_execution_acknowledgement')
        self.assertEqual(receipt['post_execution_error']['error_type'], 'FileNotFoundError')
        self.assertFalse(receipt['post_execution_error']['action_replay_permitted'])
        self.assertEqual(self.store.read('action_receipt', action['action_id']), receipt)
        self.assertIsNone(self.harness.latest)
        self.assertIsNone(self.harness.active_action)
        self.assertEqual(self.harness.execute(action), result)
        rejected = self.harness.execute(self.action('blocked-image-' + status, initial))['receipt']
        self.assertEqual(rejected['status'], 'rejected')
        self.assertEqual(rejected['reason'], 'current_observation_unavailable')
        self.assertEqual(self.robot.executions, before + 1)
        fresh = self.harness.observe()
        self.assertTrue(self.harness.accepting)
        completed = self.harness.execute(self.action('fresh-image-' + status,
            fresh['observation_id']))
        self.assertEqual(completed['receipt']['status'], 'completed')
        self.assertEqual(self.robot.executions, before + 2)

    def test_completed_ack_survives_trace_artifact_publication_failure(self):
        execute = self.robot.execute
        put_artifact = self.store.put_artifact

        def acknowledged(*args, **kwargs):
            result = execute(*args, **kwargs)
            result['executed_native_steps'] = 3
            return result

        def failed_trace(data, kind, mime_type, access_scope='actor'):
            if kind == 'actual_trace':
                raise OSError('synthetic trace artifact write failure')
            return put_artifact(data, kind, mime_type, access_scope)

        action = self.action('trace-publication-failed')
        with patch.object(self.robot, 'execute', side_effect=acknowledged), \
                patch.object(self.store, 'put_artifact', side_effect=failed_trace):
            result = self.harness.execute(action)
        receipt = result['receipt']
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(receipt['executed_native_steps'], 3)
        self.assertEqual(receipt['post_execution_error']['error_type'], 'OSError')
        self.assertTrue(receipt['planned_trace_ref'])
        self.assertTrue(receipt['dispatched_trace_ref'])
        self.assertTrue(set(receipt).isdisjoint(
            {'planned_trace', 'dispatched_trace', 'actual_trace', 'before_state'}))
        self.assertIsNone(self.harness.latest)
        self.assertEqual(self.harness.execute(action), result)
        self.assertEqual(self.robot.executions, 1)
        failures = [event for event in self.store.events()
                    if event['type'] == 'action.evidence_publication_failed']
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['correlation']['action_id'], action['action_id'])
        fresh = self.harness.observe()
        self.assertEqual(self.harness.execute(self.action('after-trace-failure', fresh['observation_id']))[
            'receipt']['status'], 'completed')

    def test_unknown_program_after_entry_removal_blocks_stop_evaluation_and_new_actions(self):
        runner = self.harness.programs = ProgramRunner(program_services(self.harness))
        identifier = 'program-unconfirmed'
        job = dict(job_id=identifier, status='running', identity=dict(self.identity))
        self.store.set_state('program', identifier, job)
        entry = dict(job_id=identifier, cancel=threading.Event(), process_ready=threading.Event(),
            review_event=threading.Event(), broker=Mock(is_alive=Mock(return_value=True)))
        runner.entries[identifier] = entry
        def publish(entry, **changes):
            job.update(changes)
            self.store.set_state('program', identifier, job)
        with patch.object(runner, '_execute', side_effect=RuntimeError('synthetic broker failure')), \
                patch.object(runner, '_publish', side_effect=publish):
            runner._execute_guarded(entry)
        self.assertFalse(runner.entries)
        self.assertEqual(runner.status(identifier)['status'], 'unknown')
        owner = Workbench.__new__(Workbench)
        owner.harness, owner.store, owner.identity = self.harness, self.store, self.identity
        owner.session = SimpleNamespace(state='completed', interrupt=Mock())
        owner.control_responses, owner.starting, owner.replay = {}, False, False
        owner.scene_service = None
        with self.assertRaisesRegex(ValueError, 'observer_operation_requires_idle_run'):
            owner.require_observer_idle()
        self.assertTrue(owner.program_continuation_state()['blocked'])
        refused = self.harness.execute(self.action('after-program-unknown'))['receipt']
        self.assertEqual(refused['status'], 'rejected')
        self.assertEqual(refused['reason'], 'program_stop_unconfirmed')
        self.assertEqual(asyncio.run(owner.stop('stop'))['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
