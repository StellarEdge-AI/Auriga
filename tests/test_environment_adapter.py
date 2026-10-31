"""RoboDojo setup keeps task authority and worker compatibility after extraction."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auriga.robot.backends.robodojo import robot_worker
from auriga.robot.model.embodiments import get_robodojo_robot
from auriga.robot.backends.robodojo import environment as robodojo
from auriga.robot.backends.robodojo.native_evaluator import HostPoseView


class EnvironmentAdapterTests(unittest.TestCase):
    def test_import_does_not_start_or_import_simulator(self):
        result = subprocess.run([sys.executable, '-c',
            'import sys; import auriga.robot.backends.robodojo.environment; '
            'assert not any(name.startswith(("isaacsim", "isaaclab", "env.")) for name in sys.modules)'],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_sim_settings_are_preserved_without_an_installed_simulator(self):
        for device_settings in ({}, {"device": "cpu", "use_fabric": False},
                                {"device": "cuda:1", "use_fabric": True}):
            with self.subTest(settings=device_settings):
                sim = {**device_settings, "dt": .004, "render_interval": 5,
                       "scene": {"num_envs": 10, "env_spacing": 7},
                       "physx": {"enable_stabilization": True}, "render": {"antialiasing_mode": "TAA"}}
                files = {"arx_x5.yml": {"config": {"sim": "sim_config"}},
                         "sim_config.yml": sim, "fixture.yml": {}}
                captured = {}
                def create(sections):
                    captured.update(sections)
                    return SimpleNamespace(camera=SimpleNamespace(),
                        eval_cfg=SimpleNamespace(observation=SimpleNamespace(collect_freq=25)))
                modules = {
                    "omegaconf": SimpleNamespace(OmegaConf=SimpleNamespace(create=create)),
                    "utils.load_file": SimpleNamespace(load_yaml=lambda path: deepcopy(files[path.name])),
                    "utils.pipeline_utils": SimpleNamespace(process_randomization=lambda config: config,
                        process_config=lambda config, task_name: (config, 25)),
                }
                with patch.dict(sys.modules, modules), patch.object(robodojo, "configure_depth_capture"):
                    robodojo.native_eval_config('/native', 'fixture', seed=3, layout_id=2)
                expected = deepcopy(sim)
                expected["seed"] = [2]
                expected["scene"]["num_envs"] = 1
                self.assertEqual(captured["sim"], expected)

    def test_worker_preserves_config_override_and_rejects_unknown_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments = SimpleNamespace(gpu=0, run_dir=directory, robodojo_root=directory,
                task='fixture', seed=3, layout_id=2, observation_profile='rgb_proprio_v1')
            with patch.object(robot_worker, 'launch_application') as launch, \
                    patch.object(robot_worker, 'native_eval_config') as config, \
                    patch.object(robot_worker, 'create_environment', side_effect=RuntimeError('setup reached')) as create:
                with self.assertRaisesRegex(RuntimeError, 'setup reached'):
                    robot_worker.RoboDojoAdapter(arguments)
                config.assert_called_once_with(Path(directory), 'fixture', 3, 2, 'rgb_proprio_v1',
                                               robot_binding=get_robodojo_robot())
                self.assertIs(create.call_args.args[3], config.return_value)
                launch.reset_mock()
                arguments.robot_model_id = 'unsupported-arm'
                with self.assertRaisesRegex(ValueError, 'unsupported'):
                    robot_worker.RoboDojoAdapter(arguments)
                launch.assert_not_called()

    def environment(self, *, mismatch=False):
        binding = replace(get_robodojo_robot(), layout_directory=Path('custom-layouts'))
        arguments = SimpleNamespace(seed=3, layout_id=2, task='fixture')
        layout = '/assets/custom-layouts/3/fixture_2.json'
        manager = SimpleNamespace(layout_valid=[True], check_layout_stability=Mock(return_value=(True, [])))
        parser = SimpleNamespace(init_state=Mock())
        native = SimpleNamespace(scene_manager=SimpleNamespace(layout_manager=manager),
            seed_manager=SimpleNamespace(seed_info={2: {'scene_layout': '/wrong' if mismatch else layout}}),
            reward_manager=SimpleNamespace(func_parser=parser))
        def reset(*, seed):
            self.assertEqual(seed, [2])
            manager.check_layout_stability()
            parser.init_state()
        native.reset = Mock(side_effect=reset)
        external_policy = object()
        eval_env = SimpleNamespace(WsModelClient=external_policy)
        def create(config, app):
            policy = eval_env.WsModelClient()
            policy.call(func_name='reset')
            with self.assertRaisesRegex(ValueError, 'policy_inference_is_owned_by_Harness'):
                policy.call(func_name='predict')
            return native
        eval_env.create_eval_env = create
        modules = {'env.global_configs': SimpleNamespace(ASSETS_PATH='/assets'),
                   'src.eval_client': SimpleNamespace(eval_env=eval_env),
                   'env.scene_manager.objects.articulation': SimpleNamespace(ArticulationObject=object)}
        return arguments, binding, native, eval_env, external_policy, modules

    def test_native_reset_records_admission_and_restores_external_policy(self):
        arguments, binding, native, eval_env, policy, modules = self.environment()
        with patch.dict(sys.modules, modules), patch.object(robodojo, 'host_articulation_properties', return_value=nullcontext()):
            result, acceptance = robodojo.create_environment(arguments, object(), Path('/run'), object(), binding)
        self.assertIs(result, native)
        self.assertIs(eval_env.WsModelClient, policy)
        self.assertEqual(native.save_dir, '/run/native-eval')
        self.assertEqual(native._stream_dir, '/run/native-eval/_stream')
        self.assertEqual(acceptance, dict(valid=True, stable=True, unstable_envs=[],
            authority='official EvalEnv.reset/setup_scene stability check'))
        self.assertIsInstance(native.reward_manager.func_parser.layout_manager, HostPoseView)
        native.reset.assert_called_once_with(seed=[2])

    def test_wrong_layout_fails_before_reset(self):
        arguments, binding, native, eval_env, policy, modules = self.environment(mismatch=True)
        with patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(ValueError, 'native_seed_manager_layout_identity_mismatch'):
                robodojo.create_environment(arguments, object(), Path('/run'), object(), binding)
        native.reset.assert_not_called()
        self.assertIs(eval_env.WsModelClient, policy)


if __name__ == '__main__':
    unittest.main()
