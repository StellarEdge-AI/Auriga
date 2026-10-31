"""CPU-only tests of bounded waits through the real native dispatch loop."""

from copy import deepcopy
import io
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from auriga.core.contracts import validate_action as validate_contract_action
from auriga.robot.motion.execution_evidence import ExecutionEvidence
from auriga.robot.backends.robodojo.robodojo import validate_action, validate_joint_prefix
from auriga.robot.backends.robodojo.robot_worker import RoboDojoAdapter


IDENTITY = dict(run_id="run", episode_id="episode", env_instance_id="env", env_generation=1)


def wait_action(steps=7, **values):
    return dict(schema_version="0.1", **IDENTITY, type="EEAction", action_id="wait",
                based_on_observation_id="observed", mode="absolute", frame_id="env_origin",
                tcp_config_id="arx-x5-link6-v1", limits_profile="robodojo-native-v1",
                timeout_s=10, path=dict(kind="hold", waypoints=[]), gripper_events=[],
                hold_native_steps=steps) | values


def fake_adapter(*, step_limit=100, success_at=None, after_step=None):
    adapter = object.__new__(RoboDojoAdapter)
    adapter.np, adapter.control_hz = np, 25
    adapter.control_step = adapter.evaluator_actions = 0
    adapter.native_step_ticks = 10
    adapter.versions = {}
    adapter.observation_profile = "rgb_proprio_calibrated_v1"
    adapter.goals = {"left": .2, "right": .6}  # Wait must use last dispatched goals instead.
    adapter.last_dispatched_goals = {"left": 1., "right": .3}
    adapter.arm_step_limits = {side: np.array([.05] * 6) for side in ("left", "right")}
    adapter.arm_joint_limits, adapter.arm_joint_names = {}, {}
    adapter.gripper_step_limit, adapter.eef_gripper_dwell_step = .25, .18
    adapter.robot = SimpleNamespace(robot_list=[], robot_key=[])
    adapter.env = SimpleNamespace(end_flag=[False], success=[True], take_action_cnt=[0],
                                  step_lim=step_limit, dt=.004)
    measured = {"left": [.1] * 6, "right": [-.2] * 6}
    adapter.state = lambda: {"arms": {side: {"q_rad": list(q)} for side, q in measured.items()}}
    adapter.observe = lambda: {"robot_state": adapter.state(), "sim_step": adapter.control_step}
    dispatched = []

    def take_action(action):
        if adapter.env.end_flag[0]:
            raise AssertionError("dispatch after native termination")
        dispatched.append(deepcopy(action))
        adapter.env.take_action_cnt[0] += 1
        adapter.control_step += 10
        count = adapter.env.take_action_cnt[0]
        measured["left"][0] += .001  # Targets must not follow measured drift during the wait.
        if count == success_at or count == step_limit:
            adapter.env.end_flag[0] = True
            adapter.env.success[0] = count == success_at
        if after_step:
            after_step(count)

    adapter.env.take_action = take_action
    return adapter, dispatched


class NativeWaitTests(unittest.TestCase):
    def validate_both(self, action, capabilities=None):
        if capabilities is None:
            capabilities = fake_adapter()[0].capabilities()
        validate_contract_action(action, IDENTITY, "observed", capabilities)
        return validate_action(action)

    def test_capability_and_both_validation_boundaries(self):
        caps = fake_adapter()[0].capabilities()
        self.assertEqual(caps["bounded_wait"]["max_native_steps"], 50)
        self.assertEqual(caps["bounded_wait"]["native_control_hz"], 25)
        for count in (1, 50):
            with self.subTest(count=count):
                self.assertEqual(self.validate_both(wait_action(count), caps), (None, None))
        for count in (0, -1, 51, True, False, 1., "1", None, float("nan")):
            for boundary in (validate_action, lambda a: validate_contract_action(a, IDENTITY, "observed", caps)):
                with self.subTest(count=count, boundary=boundary), self.assertRaisesRegex(ValueError, "hold_native_steps"):
                    boundary(wait_action(count))
        without_wait = {key: value for key, value in caps.items() if key != "bounded_wait"}
        with self.assertRaisesRegex(ValueError, "unsupported_capability:bounded_wait"):
            validate_contract_action(wait_action(), IDENTITY, "observed", without_wait)
        caps["bounded_wait"]["max_native_steps"] = 3
        with self.assertRaisesRegex(ValueError, "hold_native_steps"):
            validate_contract_action(wait_action(4), IDENTITY, "observed", caps)

    def test_wait_rejects_mixed_motion_gripper_and_joint_prefix_semantics(self):
        pose = dict(left=dict(position_m=[0, 0, 1], quaternion_xyzw=[0, 0, 0, 1]))
        invalid = [
            wait_action(path=dict(kind="backend_free", waypoints=[pose])),
            wait_action(path=dict(kind="hold", waypoints=[pose])),
            wait_action(path=dict(kind="hold", waypoints=None)),
            wait_action(path=dict(kind="hold", waypoints=[], orientation="fixed")),
            wait_action(path=dict(kind="hold", waypoints=[], blend_radius_m=.01)),
            wait_action(gripper_events=[dict(arm="left", phase="after_motion", goal_normalized=0)]),
            wait_action(gripper_events=None),
            wait_action(type="JointPrefixAction", actions=[[0.] * 14], policy_provenance={"source": "test"}),
        ]
        caps = fake_adapter()[0].capabilities()
        for action in invalid:
            for boundary in (validate_action, lambda a: validate_contract_action(a, IDENTITY, "observed", caps)):
                with self.subTest(action=action, boundary=boundary), self.assertRaises(ValueError):
                    boundary(action)
        with self.assertRaisesRegex(ValueError, "bounded wait requires EEAction"):
            validate_joint_prefix(invalid[-1])
        adapter, dispatched = fake_adapter()
        with self.assertRaises(ValueError):
            adapter.execute(invalid[3], lambda event: None, threading.Event())
        self.assertEqual(dispatched, [])

    def test_wait_exact_counts_preserves_measured_arms_and_latched_grippers(self):
        for count in (1, 7, 50):
            with self.subTest(count=count):
                adapter, dispatched = fake_adapter()
                progress = []
                result = adapter.execute(wait_action(count), progress.append, threading.Event())
                self.assertEqual((result["status"], result["reason"]), ("completed", None))
                self.assertEqual(len(dispatched), count)
                self.assertEqual(result["completed_native_steps"], count)
                self.assertEqual(result["executed_native_steps"], count)
                self.assertEqual(result["executed_control_steps"], count * 10)
                self.assertEqual(result["planned_trace"]["dt_s"], 1 / 25)
                self.assertEqual(result["planned_trace"]["actions"], [[.1] * 6 + [1.] + [-.2] * 6 + [.3]] * count)
                for action in dispatched:
                    self.assertEqual(action, dict(left_arm_joint_state=[.1] * 6, left_ee_joint_state=[1.],
                                                  right_arm_joint_state=[-.2] * 6, right_ee_joint_state=[.3]))
                observed = [e for e in progress if "observation" in e]
                self.assertEqual([e["executed_steps"] for e in observed], [i for i in range(1, count + 1) if i == 1 or i % 5 == 0])
                self.assertEqual(result["terminal_observation"]["sim_step"], count * 10)
                self.assertEqual(adapter.last_dispatched_goals, dict(left=1., right=.3))

    def test_wait_stops_at_native_limit_or_success_without_hidden_steps(self):
        for success_at, expected, reason in ((None, 3, "native_step_limit"), (2, 2, "native_success")):
            with self.subTest(success_at=success_at):
                adapter, dispatched = fake_adapter(step_limit=3, success_at=success_at)
                result = adapter.execute(wait_action(50), lambda event: None, threading.Event())
                self.assertEqual((result["status"], result["reason"]), ("stopped", "episode_ended"))
                self.assertEqual(result["episode_terminal"]["reason"], reason)
                self.assertEqual(result["executed_native_steps"], expected)
                self.assertEqual(len(dispatched), expected)
                self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)
                again = adapter.execute(wait_action(), lambda event: None, threading.Event())
                self.assertEqual(again["executed_native_steps"], 0)
                self.assertEqual(again["executed_control_steps"], 0)
                self.assertEqual(len(dispatched), expected)

    def test_wait_cancellation_before_and_during_dispatch(self):
        for cancel_at in (0, 2):
            with self.subTest(cancel_at=cancel_at):
                cancellation = threading.Event()
                adapter, dispatched = fake_adapter(after_step=lambda count: cancellation.set() if count == cancel_at else None)
                if cancel_at == 0:
                    cancellation.set()
                result = adapter.execute(wait_action(), lambda event: None, cancellation)
                self.assertEqual((result["status"], result["reason"]), ("stopped", "cancelled"))
                self.assertEqual(len(dispatched), cancel_at)
                self.assertEqual(result["completed_native_steps"], cancel_at)
                self.assertEqual(result["executed_native_steps"], cancel_at)
                self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)

    def test_wait_timeout_preserves_partial_native_counts(self):
        clock = [0.]
        adapter, dispatched = fake_adapter(after_step=lambda count: clock.__setitem__(0, float(count)))
        with patch("auriga.robot.backends.robodojo.robot_worker.time.monotonic", side_effect=lambda: clock[0]):
            result = adapter.execute(wait_action(timeout_s=2.5), lambda event: None, threading.Event())
        self.assertEqual((result["status"], result["reason"]), ("failed", "execution_timeout"))
        self.assertEqual(len(dispatched), 3)
        self.assertEqual(result["executed_native_steps"], 3)
        self.assertEqual(result["completed_native_steps"], 3)
        self.assertEqual(result["executed_control_steps"], 30)
        self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)

    def test_wait_timeout_before_dispatch_spends_no_native_steps(self):
        adapter, dispatched = fake_adapter()
        with patch("auriga.robot.backends.robodojo.robot_worker.time.monotonic", side_effect=[0., 1.]):
            result = adapter.execute(wait_action(timeout_s=.1), lambda event: None, threading.Event())
        self.assertEqual((result["status"], result["reason"]), ("failed", "planning_timeout"))
        self.assertEqual(dispatched, [])
        self.assertEqual(result["executed_native_steps"], 0)
        self.assertEqual(result["executed_control_steps"], 0)

    def test_wait_native_fault_retains_started_and_completed_step_counts(self):
        def fail_on_third_step(count):
            if count == 3:
                raise RuntimeError("native action failed after advancing physics")
        adapter, dispatched = fake_adapter(after_step=fail_on_third_step)
        with patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
            result = adapter.execute(wait_action(), lambda event: None, threading.Event())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(dispatched), 3)
        self.assertEqual(result["completed_native_steps"], 2)
        self.assertEqual(result["executed_native_steps"], 3)
        self.assertEqual(result["executed_control_steps"], 30)
        self.assertIn("native_action_failed", adapter.evaluator_failure)
        self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)

    def test_final_state_sample_failure_preserves_execution_outcome_and_measurements(self):
        for outcome in ("completed", "stopped", "unknown"):
            with self.subTest(outcome=outcome):
                cancellation = threading.Event()
                def after_step(count):
                    if outcome == "stopped":
                        cancellation.set()
                    elif outcome == "unknown" and count == 2:
                        raise RuntimeError("native action failed after advancing physics")
                adapter, dispatched = fake_adapter(after_step=after_step)
                finish = ExecutionEvidence.finish
                saved = {}
                def fail_final_sample(evidence, **kwargs):
                    finish(evidence, **kwargs)
                    saved.update(trace=deepcopy(evidence.receipt["actual_trace"]))
                    adapter.state = Mock(side_effect=OSError("terminal state unavailable"))
                with patch.object(ExecutionEvidence, "finish", fail_final_sample), \
                        patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
                    result = adapter.execute(wait_action(1 if outcome == "completed" else 3),
                        lambda _: None, cancellation)
                expected_native = 2 if outcome == "unknown" else 1
                self.assertEqual(result["status"], outcome)
                self.assertEqual(result["completed_native_steps"], 1)
                self.assertEqual(result["executed_native_steps"], expected_native)
                self.assertEqual(result["executed_control_steps"], expected_native * 10)
                self.assertEqual(len(dispatched), expected_native)
                self.assertEqual(result["actual_trace"], saved["trace"])
                self.assertIsNone(result["terminal_observation"])
                self.assertEqual(result["terminal_observation_error"], "terminal state unavailable")
                self.assertFalse(result["trace_sampling"]["actual_boundary_samples"])
                self.assertEqual(result["trace_sampling"]["terminal_state_sample_error"], "terminal state unavailable")

    def test_terminal_image_failure_does_not_discard_successful_boundary_samples(self):
        adapter, dispatched = fake_adapter()
        finish = ExecutionEvidence.finish
        def fail_terminal_image(evidence, **kwargs):
            finish(evidence, **kwargs)
            adapter.observe = Mock(side_effect=OSError("terminal camera unavailable"))
        with patch.object(ExecutionEvidence, "finish", fail_terminal_image):
            result = adapter.execute(wait_action(1), lambda _: None, threading.Event())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(dispatched), 1)
        self.assertIsNone(result["terminal_observation"])
        self.assertTrue(result["trace_sampling"]["actual_boundary_samples"])
        self.assertNotIn("terminal_state_sample_error", result["trace_sampling"])
        self.assertEqual(result["actual_trace"][-1]["sim_step"], adapter.control_step)

    def test_legacy_standalone_gripper_hold_keeps_its_existing_dwell(self):
        action = wait_action()
        del action["hold_native_steps"]
        with self.assertRaisesRegex(ValueError, "hold requires a standalone gripper event"):
            validate_action(action)
        action["gripper_events"] = [dict(arm="left", phase="after_motion", goal_normalized=0.)]
        self.assertEqual(self.validate_both(action), (None, None))
        adapter, dispatched = fake_adapter()
        result = adapter.execute(action, lambda event: None, threading.Event())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(dispatched), 6)  # ceil(abs(0 - 1) / .18), as before.
        for native in dispatched:
            self.assertEqual(native["left_ee_joint_state"], [0.])
            self.assertEqual(native["right_ee_joint_state"], [.3])
            self.assertEqual(native["left_arm_joint_state"], [.1] * 6)
            self.assertEqual(native["right_arm_joint_state"], [-.2] * 6)


if __name__ == "__main__":
    unittest.main()
