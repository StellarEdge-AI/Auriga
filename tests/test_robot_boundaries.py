"""Public compiler use and import isolation, with no Harness or simulator."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

from auriga.robot.motion.arx_actions import compile_action
from auriga.robot.model.arx_contract import validate_action
from auriga.robot.motion.motion_speed import resolve_action_speed
from auriga.robot.motion.execution_evidence import ExecutionEvidence


class RobotBoundaryTests(unittest.TestCase):
    identity = dict(run_id='compiler', episode_id='ep', env_instance_id='env', env_generation=3)
    capabilities = dict(frame_id='env_origin', tcp_config_id='arx-x5-link6-v1', dual_arm=True,
        dual_arm_progression=dict(available=True, modes=['paired', 'independent']))

    def test_composite_requests_cannot_enter_single_motion_preview(self):
        from auriga.robot.model.arx_contract import validate_preview_action
        from auriga.robot.backends.robodojo.robodojo import RobotBackend
        from auriga.robot.backends.robodojo.robot_worker import RoboDojoAdapter
        backend = object.__new__(RobotBackend)
        backend._request = Mock(side_effect=AssertionError('preview must not reach worker'))
        adapter = object.__new__(RoboDojoAdapter)
        adapter.observation_profile = 'rgb_proprio_calibrated_v1'
        adapter.state = Mock(side_effect=AssertionError('preview must not inspect simulator'))
        for kind in ('BatchAction', 'ArmQueueAction'):
            for boundary in (validate_preview_action, backend.preview_motion, adapter.preview_motion):
                with self.subTest(kind=kind, boundary=boundary.__name__), \
                        self.assertRaisesRegex(ValueError, '^dual_arm_motion_preview_unsupported$'):
                    boundary({'type': kind})
        backend._request.assert_not_called()
        adapter.state.assert_not_called()

    def test_partial_fault_keeps_attempted_completed_and_physics_counts_distinct(self):
        receipt = dict(status='unknown', executed_steps=0, actual_trace=[], gripper_execution={
            'left': dict(dispatch_attempted_steps=0, completed_steps=0, planned_steps=2)})
        segments = [dict(arm='left')]
        evidence = ExecutionEvidence(receipt, segments)
        evidence.attempted('gripper', None)
        evidence.completed('gripper', None)
        evidence.sample(10, {'measured': True})
        evidence.failed(evidence.attempted('gripper', None))
        evidence.finish(start_control_step=0, control_step=14, start_native_count=3, native_count=5,
            episode_terminal={'ended': False}, dispatched_trace=[{'sim_step': 14}])
        self.assertEqual(receipt['status'], 'unknown')
        self.assertEqual((receipt['executed_native_steps'], receipt['completed_native_steps'],
                          receipt['executed_control_steps']), (2, 1, 14))
        jaw = receipt['gripper_execution']['left']
        self.assertEqual((jaw['dispatch_attempted_steps'], jaw['completed_steps']), (2, 1))
        self.assertTrue(jaw['execution_uncertain'])
        self.assertFalse(jaw['command_complete'])
        self.assertEqual(receipt['actual_trace'], [{'sim_step': 10, 'state': {'measured': True}}])

    def test_explicit_data_compiles_contact_pose_jaw_and_wait(self):
        args = dict(observation_id='obs', action_id='motion', arm='left',
            grasp_position_m=[.2, -.1, .8], angles_deg=[0, 0, 0], contact_region='finger_middle',
            gripper_goal_normalized=0., gripper_motion=dict(start_fraction=.5, end_fraction=1.),
            speed_preset='slow')
        original = deepcopy(args)
        action = compile_action('grasp', args, self.identity, self.capabilities)
        self.assertEqual(args, original)
        self.assertEqual(action['env_generation'], 3)
        self.assertEqual(action['based_on_observation_id'], 'obs')
        self.assertAlmostEqual(action['path']['waypoints'][0]['left']['position_m'][2], .9226)
        self.assertEqual(action['gripper_events'], [dict(arm='left', phase='during_motion',
            goal_normalized=0., start_fraction=.5, end_fraction=1.)])
        self.assertEqual(validate_action(resolve_action_speed(action))[0], 'left')
        wait = compile_action('wait', dict(observation_id='obs', action_id='wait', native_steps=4),
            self.identity, self.capabilities)
        self.assertEqual(wait['path']['waypoints'], [])
        self.assertEqual(wait['hold_native_steps'], 4)
        self.assertNotIn('speed_scale', resolve_action_speed(wait))

    def test_pure_contract_imports_cannot_reach_process_owners_or_renderers(self):
        script = '''
import importlib.abc, sys
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'auriga.runtime.harness', 'auriga.apps.gateway', 'auriga.robot.backends.robodojo.robodojo',
            'auriga.robot.backends.robodojo.robot_process', 'auriga.robot.backends.robodojo.robot_worker', 'auriga.runtime.programs.runner',
            'auriga.robot.model.geometry', 'auriga.robot.motion.motion_review', 'cv2', 'torch'}:
            raise AssertionError('pure contract imported runtime: ' + fullname)
sys.meta_path.insert(0, NoRuntime())
from auriga.core import contracts
from auriga.robot.model import arx_contract
from auriga.robot.motion import batch_contract, arm_queue_contract, arx_actions
from auriga.robot.motion.proxy_contract import validate_proxies
assert validate_proxies({}) == {'obstacles': [], 'held_objects': {}}
assert contracts.validate_action_speed is arx_contract.validate_action_speed
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
            cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
