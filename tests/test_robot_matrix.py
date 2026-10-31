"""CPU contract tests for the fixed matrix; no real simulator acceptance claim."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from auriga.core.config import Config
from auriga.robot.backends.robodojo.robodojo import RobotBackend
from auriga.apps.evaluation.robot_matrix import run_checks, run_validation
from auriga.runtime.harness import Harness
from auriga.core.storage.store import Store
from test_harness import FixtureRobot
from test_unified_dual_execution import _DualRuntime


class MatrixChecks(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        runtime = self.runtime = _DualRuntime()
        adapter = runtime.adapter
        picture = FixtureRobot(directory.name)
        original_state = runtime.state

        def state():
            value = original_state()
            value.update(data_origin='synthetic_fixture', source_kind='measurement')
            for side, arm in value['arms'].items():
                arm['ee_pose']['position_m'][2] += arm['q_rad'][1]
                arm['gripper']['goal_normalized'] = adapter.goals[side]
            return value

        def plan(start, target, origin):
            goal = np.array(start, copy=True)
            goal[0], goal[1] = target[0], target[2] - 1.
            return dict(status='Success', position=np.stack([start, goal]))

        for planner in runtime.planners.values():
            planner.plan_path = plan
        adapter.state = state
        adapter.observe = lambda: dict(picture.observe(), robot_state=state(), sim_step=adapter.control_step,
            observation_profile='rgb_proprio_calibrated_v1',
            camera_calibration=dict(reference_frame_id='env_origin', cameras={}),
            camera_pose_sample=dict(cameras={}), robot_model=dict(base_link='base', joints=[]))
        robot = self.robot = Mock()
        robot.compile_action = RobotBackend.compile_action
        robot.validate_action = RobotBackend.validate_action
        robot.start.return_value = dict(adapter.capabilities(), data_origin='synthetic_fixture', observer_video=False)
        robot.observe.side_effect = adapter.observe
        robot.preview_batch.side_effect = adapter.preview_batch
        robot.execute = lambda action, progress_callback=None, cancel_flag=None: adapter.execute(
            action, progress_callback, cancel_flag)
        identity = runtime.harness.identity
        store = Store(Path(directory.name) / 'store', identity)
        self.addCleanup(store.close)
        self.harness = Harness(store, robot, config=Config.from_dict(dict(features=dict(
            observation_profile='rgb_proprio_calibrated_v1', motion_execution_mode='resampled',
            completion_guidance='none', enable_wait=True))))
        self.harness.start()
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch('auriga.runtime.actions.inspection._trajectory_image', return_value=({}, [])))
        stack.enter_context(patch('auriga.core.presentation.feedback_delivery.action_trajectory_feedback', return_value=(None, [])))

    def test_matrix_uses_real_compiler_harness_and_native_cpu_scheduler(self):
        report = {}
        original = self.robot.execute
        run_checks(self.harness, report)
        self.assertTrue(all(item['passed'] for item in report['checks'].values()))
        self.assertEqual(report['checks']['single_checkpoint_pause']['receipt']['status'], 'stopped')
        self.assertEqual(report['checks']['bounded_wait']['receipt']['executed_native_steps'], 3)
        self.assertGreater(report['checks']['confirmed_stop']['receipt']['executed_native_steps'], 0)
        self.assertFalse(self.harness.accepting)
        self.assertIs(self.robot.execute, original)
        self.assertEqual(len([item for item in report['calls'] if item['tool'] == 'robot_action']), 9)

    def test_preview_clock_mutation_stops_matrix_before_execution(self):
        preview = self.robot.preview_batch.side_effect
        def advances(action):
            result = preview(action)
            self.runtime.adapter.control_step += 1
            return result
        self.robot.preview_batch.side_effect = advances
        report = {}
        with self.assertRaisesRegex(RuntimeError, 'single_checkpoint_preview'):
            run_checks(self.harness, report)
        self.assertFalse(report['checks']['single_checkpoint_preview']['passed'])
        self.assertEqual(self.runtime.commands, [])

    def test_stop_failure_preserves_unknown_and_restores_backend_method(self):
        original = self.robot.execute
        def lose_ack(action, **kwargs):
            if action.get('hold_native_steps') == 50:
                raise RuntimeError('synthetic stop acknowledgement lost')
            return original(action, **kwargs)
        self.robot.execute = lose_ack
        report = {}
        with self.assertRaisesRegex(RuntimeError, 'confirmed_stop'):
            run_checks(self.harness, report)
        self.assertIs(self.robot.execute, lose_ack)
        self.assertEqual(report['checks']['confirmed_stop']['receipt']['status'], 'unknown')
        self.assertFalse(self.harness.accepting)

    def test_completed_stage_labels_do_not_replace_measured_dual_overlap(self):
        original = self.robot.execute
        def no_overlap(action, **kwargs):
            receipt = original(action, **kwargs)
            if any(stage['id'] == 'right_out' for stage in action.get('stages', [])):
                initial = receipt['actual_trace'][0]['state']['arms']['right']['q_rad']
                for sample in receipt['actual_trace']:
                    sample['state']['arms']['right']['q_rad'] = list(initial)
            return receipt
        self.robot.execute = no_overlap
        report = {}
        with self.assertRaisesRegex(RuntimeError, 'dual_measured_overlap'):
            run_checks(self.harness, report)
        self.assertTrue(report['checks']['unequal_dual_stage_coverage']['passed'])
        self.assertFalse(report['checks']['dual_measured_overlap']['passed'])


class MatrixOwnerChecks(unittest.TestCase):
    def test_invalid_parameters_fail_before_allocating_backend_or_store(self):
        for options in (dict(displacement_m=0), dict(displacement_m=float('nan')),
                        dict(jaw_goal=True), dict(jaw_goal=.49)):
            with self.subTest(options=options), patch('auriga.apps.evaluation.robot_matrix.Store') as store, \
                    patch('auriga.apps.evaluation.robot_matrix.RobotBackend') as robot:
                with self.assertRaises(ValueError):
                    run_validation('/unused-validation', gpu=0, **options)
                store.assert_not_called()
                robot.assert_not_called()

    def test_owner_persists_failure_and_releases_other_resources(self):
        for failure in ('harness_init', 'start', 'stop', 'robot_close', 'reader_exit', 'store_close', None):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                store = stack.enter_context(patch('auriga.apps.evaluation.robot_matrix.Store')).return_value
                robot = stack.enter_context(patch('auriga.apps.evaluation.robot_matrix.RobotBackend')).return_value
                constructor = stack.enter_context(patch('auriga.apps.evaluation.robot_matrix.Harness'))
                harness = constructor.return_value
                harness.capabilities = {'data_origin': 'synthetic_fixture'}
                harness.programs = None
                stack.enter_context(patch('auriga.apps.evaluation.robot_matrix.run_checks'))
                stack.enter_context(patch('auriga.apps.evaluation.provenance.source_binding', return_value={}))
                robot.save_video.return_value = {'videos': []}
                robot.close.return_value = dict(process_exit_confirmed=True, reader_exit_confirmed=True,
                    native_close_acknowledged=True)
                if failure == 'reader_exit':
                    robot.close.return_value['reader_exit_confirmed'] = False
                elif failure is not None:
                    operation = dict(harness_init=constructor, start=harness.start, stop=harness.request_stop,
                        robot_close=robot.close, store_close=store.close)[failure]
                    operation.side_effect = RuntimeError('synthetic ' + failure)
                report = run_validation(directory, gpu=0)
                self.assertEqual(report['passed'], failure is None)
                self.assertIsNone(report['task_success'])
                self.assertEqual(json.loads((Path(directory) / 'validation-report.json').read_text()), report)
                self.assertEqual(json.loads((Path(directory) / 'summary.json').read_text())['status'],
                    'completed' if failure is None else 'failed')
                robot.close.assert_called_once()
                store.close.assert_called_once()

    def test_unresolved_program_is_reconciled_after_robot_close_before_storage(self):
        for unresolved in (False, True):
            with self.subTest(unresolved=unresolved), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                store = stack.enter_context(patch('auriga.apps.evaluation.robot_matrix.Store')).return_value
                robot = stack.enter_context(patch('auriga.apps.evaluation.robot_matrix.RobotBackend')).return_value
                harness = stack.enter_context(patch('auriga.apps.evaluation.robot_matrix.Harness')).return_value
                harness.capabilities = {}
                stack.enter_context(patch('auriga.apps.evaluation.provenance.source_binding', return_value={}))
                stack.enter_context(patch('auriga.apps.evaluation.robot_matrix.run_checks',
                    side_effect=RuntimeError('program_cleanup_confirmed')))
                robot.close.return_value = dict(process_exit_confirmed=True, reader_exit_confirmed=True,
                    native_close_acknowledged=True)
                def close_programs():
                    robot.close.assert_called_once()
                    store.close.assert_not_called()
                    return dict(active_jobs=['program-x'] if unresolved else [], unresolved_jobs=[])
                harness.programs.close.side_effect = close_programs
                report = run_validation(directory, gpu=0)
                harness.programs.close.assert_called_once()
                self.assertFalse(report['passed'])
                self.assertEqual(store.close.call_count, 0 if unresolved else 1)
                self.assertEqual('store_close_deferred' in report, unresolved)


if __name__ == '__main__':
    unittest.main()
