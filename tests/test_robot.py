from pathlib import Path
import hashlib
import json
import os
import subprocess
import signal
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from auriga.robot.backends.robodojo.robodojo import FRAME_ID, TCP_CONFIG_ID, RobotBackend, validate_action
from auriga.robot.backends.robodojo.robot_worker import RoboDojoAdapter


class RobotTests(unittest.TestCase):
    def test_machine_paths_can_be_overridden_without_starting_a_worker(self):
        with patch.dict(os.environ, AURIGA_ROBODOJO_ROOT="~/simulation/robodojo",
                        AURIGA_ROBODOJO_PYTHON="/opt/robot-env/bin/python"):
            backend = RobotBackend()
            self.assertEqual(backend.root, str(Path.home() / "simulation/robodojo"))
            self.assertEqual(backend.python, "/opt/robot-env/bin/python")
            explicit = RobotBackend(robodojo_root="/opt/explicit", python="/opt/explicit/python")
            self.assertEqual((explicit.root, explicit.python), ("/opt/explicit", "/opt/explicit/python"))
            self.assertIsNone(backend.process)

    def action(self):
        return {"mode": "absolute", "frame_id": FRAME_ID, "tcp_config_id": TCP_CONFIG_ID,
            "limits_profile": "simulation-slow-v1", "timeout_s": 10,
            "path": {"kind": "backend_free", "waypoints": [{"left": {"position_m": [-0.3, 0, 1],
                       "quaternion_xyzw": [0, 0, 0, 1]}}], "position_tolerance_m": 0.005}, "gripper_events": []}

    def test_capability_refusal(self):
        action = self.action()
        self.assertEqual(validate_action(action)[0], "left")
        for kind in ("linear", "polyline", "arc", "spline"):
            action["path"]["kind"] = kind
            with self.assertRaisesRegex(ValueError, "unsupported_capability"):
                validate_action(action)
        action = self.action()
        action["path"]["waypoints"][0]["left"]["position_m"][0] = float("nan")
        with self.assertRaisesRegex(ValueError, "invalid_pose"):
            validate_action(action)
        action = self.action()
        action["path"]["waypoints"][0]["right"] = action["path"]["waypoints"][0]["left"]
        self.assertEqual(validate_action(action), ("both", action["path"]["waypoints"][0]))
        action["path"]["waypoints"].append({"left": action["path"]["waypoints"][0]["left"]})
        with self.assertRaisesRegex(ValueError, "same arms"):
            validate_action(action)

    def planning_fixture(self, outcome):
        import numpy as np
        adapter = object.__new__(RoboDojoAdapter)
        adapter.np = np
        adapter.env = SimpleNamespace(dt=1.0, reward_manager=SimpleNamespace(step=lambda _: None),
                                      end_flag=[False], take_action_cnt=[0], step_lim=200, success=[True])
        robot = SimpleNamespace(robot_name="fixture", arm_joints_name=["joint"], arm_joint_indices=[0], entity_origin_pose=object())
        calls, failure = [], RuntimeError("synthetic native failure")

        class BadStatus:
            def __str__(self): raise ValueError("synthetic diagnostic conversion failure")

        result = SimpleNamespace(status=BadStatus() if outcome == "diagnostic_error" else "SUCCESS" if outcome == "success" else "IK_FAIL_PRIVATE",
                                 success=np.array([outcome == "success"]))

        class NativePlanner:
            def plan_pose(self, *args, **kwargs):
                calls.append((args, kwargs))
                if outcome == "exception": raise failure
                return result

        native = NativePlanner()
        received = []
        def plan_path(current, target, origin):
            received.append((current, target, origin))
            actual = native.plan_pose(goal_tool_poses=target, current_state=current,
                                      max_attempts=5, enable_graph_attempt=5)
            self.assertIs(actual, result)
            return {"status": "Success", "position": [[0.0], [0.1]]} if bool(actual.success[0]) else {"status": "Fail"}

        class Limits:
            def __getitem__(self, key): return self
            def cpu(self): return self
            def numpy(self): return np.array([[-1.0, 1.0]])

        adapter.robot = SimpleNamespace(planner={robot.robot_name: SimpleNamespace(motion_planner=native, plan_path=plan_path)},
            get_joint=lambda *args: [np.array([0.0])], robot_list=[robot],
            robot_key=[SimpleNamespace(data=SimpleNamespace(soft_joint_pos_limits=Limits()))],
            get_robot_by_arm_name=lambda name: robot)
        return adapter, robot, native, calls, received, failure

    def test_native_planning_diagnostics_preserve_call_result_and_restore_instance(self):
        for outcome in ("success", "failure", "exception", "diagnostic_error"):
            for instance_override in (False, True):
                with self.subTest(outcome=outcome, instance_override=instance_override):
                    adapter, robot, native, calls, received, failure = self.planning_fixture(outcome)
                    original = native.plan_pose
                    if instance_override: native.plan_pose = original
                    before = dict(vars(native))
                    diagnostic = []
                    target = self.action()["path"]["waypoints"][0]["left"]
                    if outcome == "exception":
                        with self.assertRaises(RuntimeError) as raised:
                            adapter._plan_segment(robot, target, diagnostic, waypoint_index=3)
                        self.assertIs(raised.exception, failure)
                    else:
                        segment = adapter._plan_segment(robot, target, diagnostic, waypoint_index=3)
                        if outcome == "success":
                            self.assertEqual(segment[0].tolist(), [[0.0], [0.1]])
                            self.assertEqual(segment[1], 1)
                        else: self.assertIsNone(segment)
                    self.assertEqual(vars(native), before)
                    self.assertEqual(native.plan_pose, original)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(len(received), 1)
                    self.assertEqual(calls[0][0], ())
                    self.assertIs(calls[0][1]["current_state"], received[0][0])
                    self.assertIs(calls[0][1]["goal_tool_poses"], received[0][1])
                    self.assertIs(received[0][2], robot.entity_origin_pose)
                    self.assertEqual(calls[0][1]["max_attempts"], 5)
                    self.assertEqual(calls[0][1]["enable_graph_attempt"], 5)
                    self.assertEqual(len(diagnostic), 1)
                    self.assertEqual(diagnostic[0]["waypoint_index"], 3)
                    self.assertEqual(diagnostic[0]["stage"], "plan_pose")
                    self.assertIs(diagnostic[0]["returned_none"], None if outcome == "exception" else False)
                    if outcome == "diagnostic_error":
                        self.assertEqual(diagnostic[0]["diagnostic_error"], "ValueError")
                    else:
                        self.assertGreaterEqual(diagnostic[0]["elapsed_wall_s"], 0)
                        self.assertEqual(diagnostic[0]["exception_type"], "RuntimeError" if outcome == "exception" else None)
                        self.assertEqual(diagnostic[0]["success"], None if outcome == "exception" else [outcome == "success"])
                        self.assertEqual(diagnostic[0]["status"], {"success": "SUCCESS", "failure": "IK_FAIL_PRIVATE", "exception": None}[outcome])

    def test_native_planning_stages_preserve_retries_and_optional_results(self):
        import numpy as np
        for outcome in ("success", "ik_failure", "graph_failure", "trajopt_failure", "exception"):
            with self.subTest(outcome=outcome):
                adapter, robot, native, _, _, failure = self.planning_fixture("success")
                ik_result = SimpleNamespace(success=np.array([[outcome != "ik_failure"]]))
                trajopt_result = SimpleNamespace(success=np.array([[outcome == "success"]]))
                graph_result = None if outcome == "graph_failure" else np.zeros((1, 2, 1))
                seed, calls = object(), []

                class Solver:
                    def __init__(self, stage, result): self.stage, self.result = stage, result
                    def solve_pose(self, *args, **kwargs):
                        calls.append((self.stage, args, kwargs))
                        if self.stage == "trajopt" and outcome == "exception": raise failure
                        return self.result

                native.ik_solver, native.trajopt_solver = Solver("ik", ik_result), Solver("trajopt", trajopt_result)
                def graph(*args, **kwargs):
                    calls.append(("graph_seed", args, kwargs))
                    return graph_result
                native._get_graph_seed_trajectories = graph
                def plan_pose(goal_tool_poses, current_state, max_attempts, enable_graph_attempt):
                    result = None
                    for _ in range(max_attempts):
                        ik = native.ik_solver.solve_pose(goal_tool_poses, current_state=current_state, return_seeds=2)
                        self.assertIs(ik, ik_result)
                        if not ik.success.any(): continue
                        graph_seed = native._get_graph_seed_trajectories(current_state, seed_config=seed)
                        self.assertIs(graph_seed, graph_result)
                        if graph_seed is None: continue
                        result = native.trajopt_solver.solve_pose(goal_tool_poses, current_state,
                                                                 seed_config=seed, seed_traj=graph_seed)
                        self.assertIs(result, trajopt_result)
                        if result.success.any(): break
                    return result
                native.plan_pose = plan_pose
                def plan_path(current, target, origin):
                    result = native.plan_pose(target, current, max_attempts=5, enable_graph_attempt=0)
                    return {"status": "Success", "position": [[0.1]]} if result and result.success.any() else {"status": "Fail"}
                adapter.robot.planner[robot.robot_name].plan_path = plan_path
                owners = (native, native.ik_solver, native.trajopt_solver)
                before = [dict(vars(owner)) for owner in owners]
                diagnostic = []
                target = self.action()["path"]["waypoints"][0]["left"]
                if outcome == "exception":
                    with self.assertRaises(RuntimeError) as raised:
                        adapter._plan_segment(robot, target, diagnostic)
                    self.assertIs(raised.exception, failure)
                else:
                    result = adapter._plan_segment(robot, target, diagnostic)
                    self.assertEqual(result is not None, outcome == "success")
                self.assertEqual([vars(owner) for owner in owners], before)
                attempt = ["ik"] if outcome == "ik_failure" else ["ik", "graph_seed"] if outcome == "graph_failure" else ["ik", "graph_seed", "trajopt"]
                expected = attempt * (1 if outcome in ("success", "exception") else 5)
                self.assertEqual([item[0] for item in calls], expected)
                self.assertEqual([item["stage"] for item in diagnostic], expected + ["plan_pose"])
                self.assertTrue(all(item["status"] is None for item in diagnostic))
                for item, (_, args, kwargs) in zip(diagnostic, calls):
                    if item["stage"] == "ik": self.assertEqual(item["success"], ik_result.success.tolist())
                    if item["stage"] == "graph_seed": self.assertIs(item["returned_none"], graph_result is None)
                    if item["stage"] == "trajopt":
                        self.assertIs(kwargs["seed_config"], seed)
                        self.assertIs(kwargs["seed_traj"], graph_result)
                self.assertIs(diagnostic[-1]["returned_none"], None if outcome == "exception" else outcome in ("ik_failure", "graph_failure"))

    def test_planning_diagnostics_only_enter_private_development_file(self):
        for outcome in ("success", "failure", "exception"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                adapter, robot, native, calls, received, failure = self.planning_fixture(outcome)
                original_plan = adapter._plan_segment
                def plan_with_private_debug(*args, **kwargs):
                    try:
                        return original_plan(*args, **kwargs)
                    finally:
                        for item in args[2]:
                            item["private_debug"] = "PRIVATE_DIAGNOSTIC_SENTINEL"
                adapter._plan_segment = plan_with_private_debug
                adapter.directory = Path(directory)
                adapter.observation_profile = "rgb_proprio_v1"
                adapter.control_step = adapter.evaluator_actions = 0
                adapter.versions = {}
                target = self.action()["path"]["waypoints"][0]["left"]
                adapter.state = lambda: {"arms": {side: {"q_rad": [0.0] * 6, "ee_pose": target}
                                                   for side in ("left", "right")}}
                adapter.observe = lambda: {"robot_state": adapter.state()}
                adapter.capabilities = lambda: {"collision_scope": "synthetic fixture"}
                adapter.control_hz, adapter.gripper_step_limit, adapter.eef_gripper_dwell_step = 25, 0.25, 0.18
                adapter.arm_step_limits = {"left": __import__("numpy").full(6, .05)}
                adapter.last_dispatched_goals = {"left": 1.0, "right": 1.0}
                adapter._resample_plan = lambda positions, arm: __import__("numpy").zeros((1, 6))
                def native_action(row):
                    adapter.control_step += 10
                    adapter.env.take_action_cnt[0] += 1
                    adapter.evaluator_actions += 1
                    return True
                adapter._native_action = native_action
                adapter._hold = lambda: {"clock": "paused"}
                progress = []
                action = {**self.action(), "action_id": "private-boundary"}
                receipt = adapter.execute(action, progress.append, threading.Event())
                private_path, = (Path(directory) / "development-diagnostics").glob("action-*.json")
                private = json.loads(private_path.read_text())
                self.assertEqual(private["audience"], "observer_only")
                self.assertEqual(private["action_id"], "private-boundary")
                self.assertEqual(len(private["planning_diagnostics"]), 1)
                self.assertEqual(private["planning_diagnostics"][0]["private_debug"], "PRIVATE_DIAGNOSTIC_SENTINEL")
                self.assertNotIn("planning_diagnostics", private["receipt"])
                published = json.dumps({"receipt": receipt, "progress": progress, "observe": adapter.observe()})
                self.assertNotIn("planning_diagnostics", published)
                self.assertNotIn("PRIVATE_DIAGNOSTIC_SENTINEL", published)
                # Native status is now an explicitly public solver summary;
                # arbitrary raw diagnostic fields remain private in all profiles.
                without_public_status = json.loads(published)
                if outcome == "failure":
                    summary, = without_public_status["receipt"]["failure_feedback"]["solver_summary"]
                    self.assertEqual(summary.pop("native_statuses"), ["IK_FAIL_PRIVATE"])
                self.assertNotIn("IK_FAIL_PRIVATE", json.dumps(without_public_status))
                self.assertEqual(receipt["status"], "completed" if outcome == "success" else "failed")
                self.assertEqual(len(calls), 1)
                self.assertNotIn("plan_pose", vars(native))

                # A diagnostic archive failure must not replace the actuator receipt.
                with patch.object(Path, "write_text", side_effect=ValueError("synthetic archive error")):
                    second = adapter.execute(action, lambda _: None, threading.Event())
                self.assertEqual(second["status"], receipt["status"])
                self.assertEqual(second["reason"], receipt["reason"])
                self.assertEqual(len(calls), 2)
        action = self.action()
        action["gripper_events"] = [{"arm": "left", "phase": "during_motion", "goal_normalized": 0}]
        with self.assertRaisesRegex(ValueError, "unsupported_capability"):
            validate_action(action)

    def test_stop_does_not_dispatch_pending_gripper_event(self):
        # Reproduce cancellation after the arm path ends but before the first gripper dispatch.
        adapter = object.__new__(RoboDojoAdapter)
        adapter.goals = {"left": 0.0, "right": 1.0}
        adapter.last_dispatched_goals = {"left": 0.5, "right": 1.0}
        adapter.state = lambda: {"arms": {"left": {"q_rad": [0.0] * 6, "qdot_rad_s": [0.0] * 6}}}
        dispatched = []
        adapter._control = lambda holds: dispatched.append(dict(adapter.goals))
        stopped = adapter._hold()
        self.assertEqual(dispatched, [])
        self.assertEqual(stopped["settling_steps"], 0)
        self.assertEqual(adapter.goals, adapter.last_dispatched_goals)
        self.assertEqual(stopped["clock"], "paused")

    def test_multiple_waypoints_and_standalone_gripper(self):
        action = self.action()
        action["path"]["waypoints"] *= 8
        self.assertEqual(validate_action(action)[0], "left")
        action["path"]["waypoints"].append(action["path"]["waypoints"][0])
        with self.assertRaisesRegex(ValueError, "one to eight"):
            validate_action(action)

        action = self.action()
        action["path"]["waypoints"].append({"right": action["path"]["waypoints"][0]["left"]})
        with self.assertRaisesRegex(ValueError, "same arm"):
            validate_action(action)
        action = self.action()
        action["path"] = {"kind": "hold", "waypoints": []}
        with self.assertRaisesRegex(ValueError, "requires a standalone"):
            validate_action(action)
        action["gripper_events"] = [{"arm": "right", "phase": "after_motion", "goal_normalized": 0.5}]
        self.assertEqual(validate_action(action), (None, None))
        action["gripper_events"][0]["goal_normalized"] = True
        with self.assertRaisesRegex(ValueError, "invalid_gripper"):
            validate_action(action)

    def test_standalone_gripper_freezes_measured_joints(self):
        # Real executor and a native-action sink; cancellation spends no extra ticks.
        from copy import deepcopy
        import numpy as np
        for cancel_at in (None, 0, 1):
            with self.subTest(cancel_at=cancel_at):
                adapter = object.__new__(RoboDojoAdapter)
                adapter.np = np
                adapter.observation_profile = "rgb_proprio_calibrated_v1"
                measured = {"left": [0.1] * 6, "right": [-0.2] * 6}
                adapter.goals = {"left": 1.0, "right": 0.3}
                adapter.last_dispatched_goals = dict(adapter.goals)
                adapter.control_step = adapter.evaluator_actions = 0
                adapter.control_hz, adapter.gripper_step_limit, adapter.eef_gripper_dwell_step = 25, 0.25, 0.18
                adapter.versions = {}
                adapter.env = SimpleNamespace(end_flag=[False], take_action_cnt=[0], step_lim=200, success=[True])
                adapter.capabilities = lambda: {"collision_scope": "synthetic_fixture"}
                adapter.state = lambda: {"arms": {name: {"q_rad": list(q), "qdot_rad_s": [0.0] * 6}
                                                   for name, q in measured.items()}}
                adapter.observe = lambda: {"robot_state": adapter.state()}
                cancellation = threading.Event()
                dispatched = []

                def native_action(row):
                    dispatched.append(deepcopy(row))
                    adapter.goals = {"left": row[6], "right": row[13]}
                    adapter.last_dispatched_goals = dict(adapter.goals)
                    adapter.control_step += 10
                    adapter.env.take_action_cnt[0] += 1
                    adapter.evaluator_actions += 1
                    measured["left"][0] += 0.001
                    if len(dispatched) == cancel_at:
                        cancellation.set()
                    return True

                adapter._native_action = native_action
                if cancel_at == 0:
                    cancellation.set()
                initial = deepcopy(measured)
                action = self.action()
                action["path"] = {"kind": "hold", "waypoints": []}
                action["gripper_events"] = [{"arm": "left", "phase": "after_motion", "goal_normalized": 0.0}]
                receipt = adapter.execute(action, lambda progress: None, cancellation)
                self.assertEqual(receipt["before_state"]["arms"]["left"]["q_rad"], initial["left"])
                self.assertEqual(receipt["status"], "completed" if cancel_at is None else "stopped")
                self.assertIsNotNone(receipt["terminal_observation"])
                self.assertEqual(len(dispatched), {None: 6, 0: 0, 1: 1}[cancel_at])
                for row in dispatched:
                    self.assertEqual(row, initial["left"] + [0.0] + initial["right"] + [0.3])
                self.assertEqual(receipt["executed_control_steps"], 10 * len(dispatched))
                if cancel_at == 1:
                    self.assertEqual(receipt["stop_confirmation"]["actuator_target"], "last_native_command_retained")
                    self.assertEqual(receipt["stop_confirmation"]["settling_steps"], 0)

    def test_bootstrap_cuda_fault_restarts_the_worker_once(self):
        cuda = "bootstrap_failed: RuntimeError: CUDA error: operation failed due to a previous error during capture"
        for errors, expected_starts, raises in (
                ([cuda], 2, False),
                ([cuda, cuda], 2, True),
                (["bootstrap_failed: OutOfMemoryError: CUDA out of memory"], 1, True),
                (["bootstrap_failed: ValueError: bad layout"], 1, True)):
            with self.subTest(errors=errors), tempfile.TemporaryDirectory() as directory:
                backend = RobotBackend(robodojo_root=directory, run_dir=Path(directory) / "worker")
                pending, starts, closes = list(errors), [], []
                def start_process(command, **kwargs):
                    starts.append(command)
                    backend.process = object()
                    if pending:
                        raise RuntimeError(pending.pop(0))
                    backend.capabilities = {"ready": True}
                    return backend.capabilities
                backend.start_process = start_process
                backend.close = lambda: closes.append(1) or {"process_exit_confirmed": True}
                if raises:
                    with self.assertRaises(RuntimeError):
                        backend.start()
                else:
                    self.assertEqual(backend.start(), {"ready": True})
                    record = json.loads((Path(directory) / "worker" / "bootstrap-retry.json").read_text())
                    self.assertEqual(record["attempt"], 2)
                self.assertEqual(len(starts), expected_starts)
                self.assertEqual(len(closes), expected_starts - 1)

    def test_reset_preflight_refuses_without_worker_or_world_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = RobotBackend(robodojo_root=directory, run_dir=Path(directory) / "worker")
            requests = []
            backend._request = lambda operation, payload=None, **kwargs: requests.append((operation, payload)) or {"reset": True}
            backend.close = lambda: self.fail("preflight must not close the current worker")
            for seed, layout_id in ((True, 0), (0, False), (-1, 0), (0, -1), (0.0, 0), (0, "0")):
                with self.subTest(seed=seed, layout_id=layout_id), self.assertRaisesRegex(ValueError, "nonnegative integers"):
                    backend.validate_reset(seed, layout_id)
            with self.assertRaisesRegex(ValueError, "reset_layout_unavailable"):
                backend.reset(seed=0, layout_id=999)
            self.assertEqual(requests, [])
            self.assertEqual((backend.seed, backend.layout_id), (0, 0))
            self.assertIsNone(backend.process)
            self.assertFalse(backend.run_dir.exists())

            layout = Path(directory) / "Assets/Eval_Layout/RoboDojo/arx_x5/0/stack_bowls_0.json"
            layout.parent.mkdir(parents=True)
            layout.write_text("{}")
            self.assertEqual(backend.validate_reset(0, 0), layout)
            self.assertEqual(requests, [])
            backend._request = lambda operation, payload=None, **kwargs: requests.append((operation, payload)) or {"robot_state": {"arms": {}}}
            lifecycle = []
            backend.close = lambda: lifecycle.append("close") or {"process_exit_confirmed": True}
            backend.start = lambda: lifecycle.append("start")
            result = backend.reset()
            self.assertEqual(result["reset_strategy"], "fresh_worker_official_eval_reset")
            self.assertEqual(lifecycle, ["close", "start"])
            self.assertEqual(requests, [("observe", None), ("observe", None)])

    def test_pinned_reference_tcp_and_orientation(self):
        import numpy as np
        from auriga.robot.model import roboprobe_pose as pose
        provenance = json.loads((Path(__file__).resolve().parent.parent / "third_party/roboprobe/provenance.json").read_text())
        self.assertEqual(hashlib.sha256(Path(pose.__file__).read_bytes()).hexdigest(), provenance["source_files"]["pose.py"]["sha256"])
        quaternion = pose.angles_to_quat(0, 0, 0)
        np.testing.assert_allclose(pose.tool_axis(quaternion), [0, 0, -1], atol=1e-12)
        original = np.r_[[0.1, 0.2, 1.0], quaternion]
        values = pose.pose_to_values(original)
        self.assertAlmostEqual(values["z"], 1.0 - 0.1501)
        np.testing.assert_allclose(pose.values_to_pose(values), original, atol=1e-12)
        # Hold the grasp point while yaw changes: the inverse uses the target tool axis.
        values["pitch_deg"], values["yaw_deg"] = 30, 45
        rebuilt = pose.values_to_pose(values)
        np.testing.assert_allclose(rebuilt[:3] + 0.1501 * pose.tool_axis(rebuilt[3:]), [values[axis] for axis in ("x", "y", "z")], atol=1e-12)

    def test_moving_camera_uses_measured_link_instead_of_stale_usd(self):
        import numpy as np
        adapter = object.__new__(RoboDojoAdapter)
        adapter.np = np
        def matrix(pose):
            result = np.eye(4)
            result[:3, 3] = pose[:3]
            return result
        adapter._matrix = matrix
        camera = SimpleNamespace(get_world_pose=lambda camera_axes: (np.zeros(3), np.array([1, 0, 0, 0])))
        manager = SimpleNamespace(camera_names=[["cam_left_wrist"]], cameras=[[camera]],
            camera_config={"cam_left_wrist": SimpleNamespace(camera={"mount_link": "robot0/camera"})})
        origin = SimpleNamespace(cpu=lambda: SimpleNamespace(numpy=lambda: np.zeros(3)))
        adapter.env = SimpleNamespace(camera_manager=manager, env_origins=[origin])
        robot = SimpleNamespace()
        adapter.robot = SimpleNamespace(robot_list=[robot], robot_key=[SimpleNamespace(body_names=["camera", "base_link"])],
            get_robot_by_arm_name=lambda name: robot,
            get_link_pose=lambda robot, link, ids, is_relative: {0: np.array([1 if link == "camera" else 0, 0, 0, 1, 0, 0, 0])})
        adapter.robot_model = {"base_link": "base_link", "joints": []}
        mount = np.eye(4)
        mount[1, 3] = 0.2
        adapter.camera_calibration = {"cameras": {"cam_left_wrist": {"T_link_camera": mount.tolist()}}}
        sample = adapter._camera_pose("cam_left_wrist")
        self.assertEqual(sample["source_kind"], "robot_kinematics")
        np.testing.assert_allclose(np.array(sample["T_world_camera"])[:3, 3], [1, 0.2, 0])
        self.assertGreater(sample["native_usd_pose_diagnostic"]["translation_residual_m"], 1)

    def test_camera_calibration_enforces_square_pixel_vertical_focal_length(self):
        import numpy as np
        adapter = object.__new__(RoboDojoAdapter)
        adapter.np = np
        adapter._camera_pose = lambda camera_id: {"T_world_camera": np.eye(4).tolist()}
        head_k = np.array([[288.13253784, 0.0, 320.0], [0.0, 336.46432495, 240.0], [0.0, 0.0, 1.0]])
        wrist_k = np.array([[397.04125977, 0.0, 320.0], [0.0, 397.04125977, 240.0], [0.0, 0.0, 1.0]])
        head = SimpleNamespace(get_intrinsics_matrix=lambda: head_k, get_resolution=lambda: (640, 480))
        wrist = SimpleNamespace(get_intrinsics_matrix=lambda: wrist_k, get_resolution=lambda: (640, 480))
        adapter.env = SimpleNamespace(camera_manager=SimpleNamespace(
            camera_names=[["cam_head", "cam_left_wrist"]], cameras=[[head, wrist]]))
        calibration = adapter._camera_calibration()
        np.testing.assert_allclose(calibration["cameras"]["cam_head"]["K"],
            [[288.13253784, 0.0, 320.0], [0.0, 288.13253784, 240.0], [0.0, 0.0, 1.0]])
        np.testing.assert_allclose(calibration["cameras"]["cam_left_wrist"]["K"], wrist_k)
        self.assertEqual(head_k[1, 1], 336.46432495)

    def test_ipc_separates_native_logs_and_cancellation(self):
        # This fixture verifies transport only; it does not claim simulated motion.
        code = '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request['op'] == 'execute':
        print('native simulator log', flush=True)
        print('AURIGA_JSON ' + json.dumps({'id': request['id'], 'progress': {'phase': 'started'}}), flush=True)
        cancel = json.loads(sys.stdin.readline())
        assert cancel['op'] == 'cancel' and cancel['target'] == request['id']
        print('AURIGA_JSON ' + json.dumps({'id': request['id'], 'result': {'status': 'stopped'}}), flush=True)
    else:
        print('AURIGA_JSON ' + json.dumps({'id': request['id'], 'result': {'closed': True}}), flush=True)
        break
'''
        with tempfile.TemporaryDirectory() as directory:
            backend = RobotBackend(run_dir=directory)
            backend.log = (Path(directory) / "robot-worker.log").open("w", buffering=1)
            backend.process = subprocess.Popen([sys.executable, "-u", "-c", code], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=backend.log, text=True)
            threading.Thread(target=backend._read, daemon=True).start()
            cancel = threading.Event()
            updates = []
            def progress(update):
                updates.append(update)
                cancel.set()
            try:
                receipt = backend.execute(self.action(), progress, cancel)
                self.assertEqual(receipt["status"], "stopped")
                self.assertEqual(updates, [{"phase": "started"}])
            finally:
                backend.close()
            self.assertIn("native simulator log", (Path(directory) / "robot-worker.log").read_text())

    def test_native_gpu_oom_interrupts_pending_request_without_exposing_logs(self):
        # Exercise real file-backed stderr and IPC: OOM need not kill the worker.
        signatures = ["ERROR_OUT_OF_DEVICE_MEMORY", "Out of GPU memory allocating resource '/private/scene.usd'",
                      "CUDA error: out of memory", "PxgCudaDeviceMemoryAllocator failed to allocate memory 268435456 bytes! Result = 2",
                      "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 200.00 MiB. GPU 0 has /private"]
        popen = subprocess.Popen
        for signature in signatures:
            with self.subTest(signature=signature), tempfile.TemporaryDirectory() as directory:
                code = '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request['op'] == 'ready':
        print('Warning: failed to open display; GPU memory statistics unavailable', file=sys.stderr, flush=True)
        print('AURIGA_JSON ' + json.dumps({'id': request['id'], 'result': {'ready': True}}), flush=True)
    else:
        print(sys.argv[1], file=sys.stderr, flush=True)
'''
                backend = RobotBackend(robodojo_root=directory, run_dir=directory)
                def launch(*args, **kwargs):
                    return popen([sys.executable, "-u", "-c", code, signature], **kwargs)
                started = time.monotonic()
                try:
                    with patch("auriga.robot.backends.robodojo.robot_process.subprocess.Popen", side_effect=launch):
                        self.assertEqual(backend.start(), {"ready": True})
                    with self.assertRaisesRegex(RuntimeError, "robot_gpu_out_of_memory.*robot status unknown") as raised:
                        backend.execute(self.action())
                    self.assertNotIn(signature, str(raised.exception))
                    self.assertEqual(backend.pending, {})
                    with self.assertRaisesRegex(RuntimeError, "robot_gpu_out_of_memory"):
                        backend.observe()
                finally:
                    backend.close()
                self.assertLess(time.monotonic() - started, 5)
                self.assertIn(signature, (Path(directory) / "robot-worker.log").read_text())

    def test_planner_oom_reply_marks_robot_failed_without_log_evidence(self):
        # cuRobo/torch OOM is caught by the worker: a failed receipt or error reply, nothing on stderr.
        popen = subprocess.Popen
        replies = {
            "receipt": ("execute", {"result": {"status": "failed", "reason": "backend_error: OutOfMemoryError: CUDA out of memory. /private"}}),
            "error": ("observe", {"error": "OutOfMemoryError: CUDA out of memory. Tried to allocate 20.00 MiB"}),
            "quoted": ("execute", {"result": {"status": "failed", "reason": "backend_error: ValueError: label 'OutOfMemoryError: CUDA out of memory'"}}),
        }
        for name, (operation, reply) in replies.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                code = '''import json, sys
reply = json.loads(sys.argv[1])
for line in sys.stdin:
    request = json.loads(line)
    body = {'result': {'ready': True}} if request['op'] in ('ready', 'close') else reply
    print('AURIGA_JSON ' + json.dumps({'id': request['id'], **body}), flush=True)
    if request['op'] == 'close': break
'''
                backend = RobotBackend(robodojo_root=directory, run_dir=directory)
                def launch(*args, **kwargs):
                    return popen([sys.executable, "-u", "-c", code, json.dumps(reply)], **kwargs)
                try:
                    with patch("auriga.robot.backends.robodojo.robot_process.subprocess.Popen", side_effect=launch):
                        backend.start()
                    call = (lambda: backend.execute(self.action())) if operation == "execute" else backend.observe
                    if name == "quoted":
                        self.assertEqual(call()["status"], "failed")
                        self.assertIsNone(backend.failure)
                        continue
                    with self.assertRaisesRegex(RuntimeError, "^robot_gpu_out_of_memory:.*do not replay action") as raised:
                        call()
                    self.assertNotIn("/private", str(raised.exception))
                    self.assertTrue(backend.failure.startswith("robot_gpu_out_of_memory:"))
                    with self.assertRaisesRegex(RuntimeError, "robot_gpu_out_of_memory"):
                        backend.observe()
                finally:
                    backend.close()

    def test_native_oom_precedes_bootstrap_error_or_eof(self):
        popen = subprocess.Popen
        code = '''import json, sys
request = json.loads(sys.stdin.readline())
print("Out of GPU memory allocating resource '/private/scene.usd'", file=sys.stderr, flush=True)
if sys.argv[1] == 'reply':
    print('AURIGA_JSON ' + json.dumps({'id': request['id'], 'error': 'bootstrap_failed: downstream NoneType'}), flush=True)
'''
        for mode in ("reply", "eof"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                backend = RobotBackend(robodojo_root=directory, run_dir=directory)
                def launch(*args, **kwargs):
                    return popen([sys.executable, "-u", "-c", code, mode], **kwargs)
                try:
                    with patch("auriga.robot.backends.robodojo.robot_process.subprocess.Popen", side_effect=launch):
                        with self.assertRaisesRegex(RuntimeError, "^robot_gpu_out_of_memory:.*robot status unknown") as raised:
                            backend.start()
                    self.assertNotIn("NoneType", str(raised.exception))
                    self.assertNotIn("scene.usd", str(raised.exception))
                    self.assertEqual(backend.pending, {})
                finally:
                    backend.close()

    def test_previous_worker_oom_does_not_poison_new_worker(self):
        popen = subprocess.Popen
        code = '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    print('AURIGA_JSON ' + json.dumps({'id': request['id'], 'result': {'healthy': True}}), flush=True)
    if request['op'] == 'close': break
'''
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "robot-worker.log"
            log.write_text("previous worker: CUDA error: out of memory\n")
            backend = RobotBackend(robodojo_root=directory, run_dir=directory)
            def launch(*args, **kwargs):
                return popen([sys.executable, "-u", "-c", code], **kwargs)
            try:
                with patch("auriga.robot.backends.robodojo.robot_process.subprocess.Popen", side_effect=launch):
                    self.assertEqual(backend.start(), {"healthy": True})
                self.assertEqual(backend.observe(), {"healthy": True})
                self.assertIsNone(backend.failure)
            finally:
                backend.close()
            self.assertIn("previous worker", log.read_text())

    def test_cleanup_does_not_wait_for_descendant_inheriting_stderr(self):
        popen = subprocess.Popen
        code = '''import json, subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
for line in sys.stdin:
    request = json.loads(line)
    print('AURIGA_JSON ' + json.dumps({'id': request['id'], 'result': {'child': child.pid}}), flush=True)
    if request['op'] == 'close': break
'''
        with tempfile.TemporaryDirectory() as directory:
            backend = RobotBackend(robodojo_root=directory, run_dir=directory)
            descendant, closer = None, None
            def launch(*args, **kwargs):
                return popen([sys.executable, "-u", "-c", code], **kwargs)
            try:
                with patch("auriga.robot.backends.robodojo.robot_process.subprocess.Popen", side_effect=launch):
                    descendant = backend.start()["child"]
                closer = threading.Thread(target=backend.close)
                closer.start()
                closer.join(timeout=2)
                self.assertFalse(closer.is_alive(), "cleanup waited for an inherited stderr descriptor")
                self.assertIsNotNone(backend.process.poll())
                os.kill(descendant, 0)  # The descendant remains alive; only its parent was closed.
            finally:
                if descendant is not None:
                    try: os.kill(descendant, signal.SIGTERM)
                    except ProcessLookupError: pass
                if closer is not None: closer.join(timeout=5)
                else: backend.close()


if __name__ == "__main__":
    unittest.main()
