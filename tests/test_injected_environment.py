"""RoboDojoAdapter over a host-owned environment (no Isaac launch, no evaluator).

The XPolicyLab deployment constructs the same adapter in the policy server over an environment
owned by the RoboDojo eval client. These CPU fixtures check that injection bypasses only the
native launch/creation/evaluator steps and that dispatch, accounting and observation still go
through the adapter's own code.
"""

from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from auriga.robot.backends.robodojo import robot_worker
from auriga.robot.backends.robodojo.robot_worker import RoboDojoAdapter


class HostArray(np.ndarray):
    def cpu(self):
        return self

    def numpy(self):
        return np.asarray(self)


def host(values):
    return np.array(values, dtype=np.float32).view(HostArray)


class FakeEnvironment:
    """Two ARX X5 target arms, one environment, synchronous joint dispatch."""

    def __init__(self, step_lim=5, end_at=None):
        self.step_lim, self.end_at, self.dt = step_lim, end_at, 0.004
        self.take_action_cnt, self.end_flag, self.success = [0], [False], [True]
        self.q = {"left": np.zeros(6), "right": np.zeros(6)}
        self.jaw = {"left": 1.0, "right": 1.0}
        self.ticks, self.actions, self.closed = 0, [], False
        self.sim = SimpleNamespace(sim=SimpleNamespace(current_time=0.0))
        robots = []
        for side in ("left", "right"):
            robots.append(SimpleNamespace(
                arm_name=f"{side}_arm", gripper_name=f"{side}_ee", robot_name="x5", type="target",
                arm_joints_name=[f"joint{i}" for i in range(1, 7)], gripper_joints_name=["joint7", "joint8"],
                arm_joint_indices=list(range(6)), gripper_scale=[-0.01, 0.044],
                gripper_move={"base": "joint7", "sign": 1.0, "mimic": ["joint8", 1.0, 0.0]}))
        assets = [SimpleNamespace(data=SimpleNamespace(
            joint_vel_limits=host([[5.0] * 8]), soft_joint_pos_limits=host([[[-10.0, 10.0]] * 8]),
            joint_vel=host([[0.0] * 8]))) for _ in robots]
        env = self

        class Manager:
            robot_list, robot_key, planner = robots, assets, {}
            control_manager = SimpleNamespace(prev_control=[{}])

            @staticmethod
            def side(robot):
                return robot.arm_name.split("_")[0]

            def process_name(self, name):
                return name if name.endswith("_state") else name + "_joint_state"

            def get_robot_by_arm_name(self, arm_name):
                return next(robot for robot in robots if robot.arm_name == arm_name)

            def get_joint(self, robot, env_idx_list=None):
                return {0: np.asarray(env.q[self.side(robot)], dtype=np.float32)}

            def get_real_endpose(self, robot, env_idx_list=None):
                return {0: np.asarray([0.1, 0.2, 0.9, 1.0, 0.0, 0.0, 0.0], dtype=np.float32)}

            def get_end_effector_real_val(self, robot, env_idx_list=None):
                opening = -0.01 + 0.054 * env.jaw[self.side(robot)]
                return {0: np.asarray([opening, opening], dtype=np.float32)}

        self.robot_manager = Manager()
        self._command()
        self.obs_manager = SimpleNamespace(collect_freq=25, collect_interval=10, render_for_capture=lambda: None,
            get_obs=lambda env_idx_list=None: {0: {"instruction": "stack the bowls", "vision": {
                name: {"color": np.full((8, 12, 3), 40 * index, dtype=np.uint8)}
                for index, name in enumerate(("cam_head", "cam_left_wrist", "cam_right_wrist"))}}})

    def _command(self):
        controls = {}
        for side in ("left", "right"):
            controls[f"{side}_arm_joint_state"] = {"position": self.q[side].tolist()}
            value = -0.01 + 0.054 * self.jaw[side]
            controls[f"{side}_ee_joint_state"] = {"position": [value, value]}
        self.robot_manager.control_manager.prev_control[0] = controls

    def step(self, env_idx_list, decimation=1):
        self.ticks += decimation
        self.sim.sim.current_time = self.ticks * self.dt

    def take_action(self, action):
        if self.end_flag[0]:
            return
        self.actions.append(action)
        self.take_action_cnt[0] += 1
        for side in ("left", "right"):
            self.q[side] = np.asarray(action[f"{side}_arm_joint_state"], dtype=float)
            self.jaw[side] = float(np.clip(action[f"{side}_ee_joint_state"][0], 0, 1))
        self._command()
        for _ in range(10):
            self.step([0])
        if self.take_action_cnt[0] >= self.step_lim or self.take_action_cnt[0] == self.end_at:
            self.end_flag[0] = True
            self.success[0] = self.take_action_cnt[0] == self.end_at

    def close(self):
        self.closed = True


class InjectedEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.arguments = SimpleNamespace(run_dir=str(self.root / "robot"), robodojo_root=str(self.root), task="stack_bowls",
            seed=0, layout_id=None, gpu=None, observation_profile="rgb_proprio_v1", robot_model_id="arx-x5",
            tracking_guard=False, joint_replan=False, motion_safety_guard=True, trajectory_preview=True,
            motion_execution_mode="resampled", scene_truth_log=None)

    def binding(self, env):
        from auriga.core.config import motion_thresholds
        app = SimpleNamespace(closed=False, config={})
        app.close = lambda: setattr(app, "closed", True)
        return SimpleNamespace(env=env, app=app, layout_acceptance={"valid": None, "stable": None},
            versions={"environment_host": "fixture"}, image_settings={}, motion_thresholds=motion_thresholds())

    def adapter(self, env):
        with patch.object(robot_worker, "launch_application", side_effect=AssertionError("no Isaac launch")), \
             patch.object(robot_worker, "native_eval_config", side_effect=AssertionError("no native config")), \
             patch.object(robot_worker, "create_environment", side_effect=AssertionError("no native env")), \
             patch.object(RoboDojoAdapter, "_initialize_evaluator", side_effect=AssertionError("no evaluator")), \
             patch.object(RoboDojoAdapter, "_versions", side_effect=AssertionError("no checkout provenance")):
            return RoboDojoAdapter(self.arguments, self.binding(env))

    def test_injected_environment_keeps_native_capabilities_and_limits(self):
        env = FakeEnvironment(step_lim=7)
        adapter = self.adapter(env)
        capabilities = adapter.capabilities()
        self.assertEqual(capabilities["action_budget"], {"native_step_limit": 7, "source": "native_task_configuration"})
        self.assertEqual(capabilities["native_control_hz"], 25.0)
        self.assertEqual(capabilities["native_physics_steps_per_action"], 10)
        self.assertEqual(capabilities["physics_dt_s"], 0.004)
        self.assertEqual(capabilities["gripper_max_step"], 0.25)
        self.assertEqual(capabilities["arm_max_step_rad"]["left"], [np.float32(0.05).item()] * 6)
        self.assertEqual(capabilities["versions"], {"environment_host": "fixture"})
        self.assertFalse(adapter.legacy_png_ipc)
        observation = adapter.observe()
        self.assertEqual(observation["instruction"], "stack the bowls")
        self.assertEqual([camera["camera_id"] for camera in observation["cameras"]],
                         ["cam_head", "cam_left_wrist", "cam_right_wrist"])
        self.assertTrue(all(Path(camera["rgb_path"]).is_file() for camera in observation["cameras"]))
        self.assertEqual(adapter.state()["arms"]["left"]["gripper"]["command_normalized"], 1.0)

    def test_injected_dispatch_counts_native_steps_and_physics_ticks(self):
        env = FakeEnvironment(step_lim=10)
        adapter = self.adapter(env)
        rows = [[0.01 * index] * 6 + [0.5] + [0.0] * 6 + [1.0] for index in range(1, 4)]
        receipt = adapter.execute(dict(type="JointPrefixAction", actions=rows,
                                       policy_provenance={"fixture": "injected"}), lambda value: None, threading.Event())
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["executed_native_steps"], 3)
        self.assertEqual(receipt["executed_control_steps"], 30)
        self.assertEqual(len(env.actions), 3)
        self.assertEqual(env.actions[-1]["left_ee_joint_state"], [0.5])
        self.assertEqual(adapter.control_step, 30)
        self.assertEqual(adapter.evaluator_actions, 3)
        self.assertEqual(receipt["episode_terminal"]["native_action_count"], 3)
        self.assertIsNotNone(receipt["terminal_observation"])

    def test_injected_environment_end_stops_without_extra_dispatch(self):
        env = FakeEnvironment(step_lim=10, end_at=2)
        adapter = self.adapter(env)
        receipt = adapter.execute(dict(type="JointPrefixAction", actions=[[0.0] * 14] * 5,
                                       policy_provenance={"fixture": "injected_end"}), lambda value: None, threading.Event())
        self.assertEqual((receipt["status"], receipt["reason"]), ("stopped", "episode_ended"))
        self.assertEqual(len(env.actions), 2)
        self.assertEqual(receipt["episode_terminal"]["reason"], "native_success")

    def test_injected_environment_has_no_evaluator_and_closes_host_objects(self):
        env = FakeEnvironment()
        adapter = self.adapter(env)
        with self.assertRaisesRegex(RuntimeError, "native evaluator unavailable"):
            adapter.evaluate()
        adapter.close()
        self.assertTrue(env.closed)
        self.assertTrue(adapter.app.closed)

    def test_worker_path_still_launches_and_initializes_native_environment(self):
        env = FakeEnvironment(step_lim=4)
        calls = []
        app = SimpleNamespace(close=lambda: None, config={})
        with patch.dict("os.environ", {"AURIGA_IMAGE_SETTINGS": "{}"}), \
             patch.object(robot_worker, "launch_application", side_effect=lambda gpu: calls.append(("launch", gpu)) or app), \
             patch.object(robot_worker, "native_eval_config", side_effect=lambda *a, **k: calls.append("config") or {}), \
             patch.object(robot_worker, "create_environment", side_effect=lambda *a: calls.append("create") or (env, {"valid": True})), \
             patch.object(RoboDojoAdapter, "_initialize_evaluator", side_effect=lambda: calls.append("evaluator")), \
             patch.object(RoboDojoAdapter, "_versions", side_effect=lambda root: {"native": True}):
            adapter = RoboDojoAdapter(SimpleNamespace(**{**vars(self.arguments), "gpu": 3}))
        self.assertEqual(calls, [("launch", 3), "config", "create", "evaluator"])
        self.assertIs(adapter.env, env)
        self.assertIs(adapter.app, app)
        self.assertEqual(adapter.versions, {"native": True})
        self.assertEqual(adapter.layout_acceptance, {"valid": True})



class HostPackageNamespaceTests(unittest.TestCase):
    """The model relay's vendored retry policy loads inside a process that imported XPolicyLab."""

    def test_vendored_reference_modules_load_beside_a_host_xpolicylab_package(self):
        script = """
import sys, types
host = types.ModuleType("XPolicyLab"); host.__path__ = []
child = types.ModuleType("XPolicyLab.model_template")
sys.modules.update({"XPolicyLab": host, "XPolicyLab.model_template": child})
from auriga.apps.reference.reference_api import upstream
vendored = upstream()
assert sys.modules["XPolicyLab"] is host and sys.modules["XPolicyLab.model_template"] is child
assert not any(name.startswith("XPolicyLab.policy") for name in sys.modules)
assert vendored.joint.__name__ == "XPolicyLab.policy.RoboDojo_Agent_L3_Inspect.policy"
assert issubclass(vendored.joint.InfrastructureFailure, BaseException)
assert upstream() is vendored
print("ok")
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip(), "ok")

    def test_without_a_host_package_the_vendored_package_stays_registered(self):
        script = """
import sys
from auriga.apps.reference.reference_api import upstream
vendored = upstream()
assert sys.modules["XPolicyLab"].__path__[0].endswith("third_party/roboprobe/source")
assert sys.modules["XPolicyLab.policy.RoboDojo_Agent_L3_Inspect.policy"] is vendored.joint
print("ok")
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
