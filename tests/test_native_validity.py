"""CPU regressions for native sample validity that can change after reset."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call

from auriga.robot.backends.robodojo.robot_worker import RoboDojoAdapter


class NativeEnvironment(SimpleNamespace):
    def mark_env_unstable(self, env_idx):
        # EvalEnv records this independently of success and reward, then
        # excludes the environment from its aggregate evaluation results.
        self.unstable_envs.add(env_idx)


class NativeValidityTests(unittest.TestCase):
    def adapter(self, directory, *, success=False, ended=True):
        adapter = object.__new__(RoboDojoAdapter)
        adapter.directory = Path(directory)
        adapter.task, adapter.seed, adapter.layout_id = "fixture", 0, 4
        adapter.episode_index = adapter.evaluator_actions = adapter.control_step = 0
        adapter.versions, adapter.evaluator_failure = {}, None
        adapter.layout_acceptance = dict(valid=True, stable=True, unstable_envs=[],
                                         authority="accepted reset fixture")
        reward = float(success)
        reward_manager = SimpleNamespace(
            get_reward=Mock(return_value=[reward]),
            get_score=Mock(return_value=[100. if success else 50.]),
            step=Mock(side_effect=AssertionError("validity must not advance predicates")),
            check_once=Mock(side_effect=AssertionError("validity must not query predicates")))
        layout_manager = SimpleNamespace(layout_valid=[True],
            check_layout_stability=Mock(side_effect=AssertionError("validity must not rerun stability")))
        adapter.env = NativeEnvironment(end_flag=[ended], success=[success],
            take_action_cnt=[1], step_lim=1600, unstable_envs=set(),
            scene_manager=SimpleNamespace(layout_manager=layout_manager),
            reward_manager=reward_manager, get_score=Mock())
        return adapter

    def test_delayed_native_instability_invalidates_accepted_reset_without_rescoring(self):
        for success in (False, True):
            with self.subTest(success=success), tempfile.TemporaryDirectory() as directory:
                adapter = self.adapter(directory, success=success)
                reset_acceptance = deepcopy(adapter.layout_acceptance)
                before = adapter.evaluate()
                self.assertTrue(before["valid_episode"])
                adapter.env.mark_env_unstable(0)
                after = adapter.evaluate()
                self.assertFalse(after["valid_episode"])
                self.assertEqual(adapter.layout_acceptance, reset_acceptance)
                self.assertEqual(after["layout_acceptance"], reset_acceptance)
                for key in ("task_success", "native_reward", "native_process_score", "normalized_score",
                            "episode_terminal", "success_authority", "final_check"):
                    self.assertEqual(after[key], before[key], key)
                self.assertEqual(after["native_validity"]["unstable_envs"], [0])
                self.assertTrue(after["native_validity"]["unstable"])
                self.assertFalse(before["native_validity"]["unstable"])
                saved = json.loads(Path(after["evaluation_path"]).read_text())
                self.assertFalse(saved["valid_episode"])
                self.assertEqual(saved["native_validity"], after["native_validity"])
                # Exactly the pre-existing evaluator calls, with the same
                # terminal-safe arguments. Reading validity adds no calls.
                rm = adapter.env.reward_manager
                self.assertEqual(rm.get_reward.call_args_list, [call(final_check=False)] * 2)
                self.assertEqual(rm.get_score.call_args_list, [call([float(success)])] * 2)
                rm.step.assert_not_called()
                rm.check_once.assert_not_called()
                adapter.env.get_score.assert_not_called()
                adapter.env.scene_manager.layout_manager.check_layout_stability.assert_not_called()

    def test_live_layout_invalidation_and_reset_rejection_both_exclude_sample(self):
        for reset_valid, reset_stable, live_valid in (
                (True, True, False), (False, True, True), (True, False, True)):
            with self.subTest(reset_valid=reset_valid, reset_stable=reset_stable, live_valid=live_valid), \
                    tempfile.TemporaryDirectory() as directory:
                adapter = self.adapter(directory)
                adapter.layout_acceptance.update(valid=reset_valid, stable=reset_stable)
                adapter.env.scene_manager.layout_manager.layout_valid[0] = live_valid
                result = adapter.evaluate()
                self.assertFalse(result["valid_episode"])
                self.assertEqual(result["native_validity"]["layout_valid"], live_valid)

    def test_live_snapshot_is_read_only_and_scoped_to_native_environment_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(directory)
            adapter.env.mark_env_unstable(4)
            adapter.env.mark_env_unstable(2)
            first = adapter._native_validity()
            self.assertEqual(first["unstable_envs"], [2, 4])
            self.assertTrue(first["valid"])
            adapter.env.mark_env_unstable(0)
            second = adapter._native_validity()
            self.assertFalse(second["valid"])
            self.assertEqual(first["unstable_envs"], [2, 4])
            self.assertEqual(adapter.env.unstable_envs, {0, 2, 4})
            adapter.env.reward_manager.get_reward.assert_not_called()
            adapter.env.reward_manager.get_score.assert_not_called()
            adapter.env.reward_manager.step.assert_not_called()
            adapter.env.reward_manager.check_once.assert_not_called()
            adapter.env.scene_manager.layout_manager.check_layout_stability.assert_not_called()

    def test_nonterminal_evaluation_preserves_existing_final_check_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(directory, ended=False)
            adapter.env.mark_env_unstable(0)
            result = adapter.evaluate(final_check=True)
            self.assertFalse(result["valid_episode"])
            self.assertTrue(result["final_check"])
            self.assertEqual(result["success_authority"], "native_final_check")
            adapter.env.reward_manager.get_reward.assert_called_once_with(final_check=True)
            adapter.env.reward_manager.get_score.assert_called_once_with([0.])


if __name__ == "__main__":
    unittest.main()
