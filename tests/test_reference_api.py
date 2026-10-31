"""Actual local Responses HTTP and upstream actor loop, with synthetic actuator evidence."""

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import cv2
import numpy as np

from auriga.apps.reference.reference_api import ResponsesClient, action_spec, make_policy, observation, run_episode, upstream


class ReferenceApiCheck(unittest.TestCase):
    def test_explicit_cancellation_interrupts_continuously_streaming_provider(self):
        entered = threading.Event()
        class Provider(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    entered.set()
                    for _ in range(1000):
                        self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                        time.sleep(.02)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args): pass

        with tempfile.TemporaryDirectory() as temporary:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                client = ResponsesClient(f"http://127.0.0.1:{server.server_port}/v1", "synthetic-credential", temporary, max_retries=0, model="gpt-6-astra")
                failures = []
                def pending():
                    try: client.complete([], [])
                    except upstream().joint.InfrastructureFailure as error: failures.append(type(error).__name__)
                request = threading.Thread(target=pending)
                request.start()
                self.assertTrue(entered.wait(3))
                client.cancel_pending()
                request.join(2)
                self.assertFalse(request.is_alive())
                self.assertEqual(len(failures), 1)
                self.assertEqual(client.attempt_count, 1)
                transport = (Path(temporary) / "call-001/attempt-001/transport.jsonl").read_text()
                self.assertIsNone(client.blocked_error)
                self.assertNotIn("synthetic-credential", transport)
            finally:
                server.shutdown(); server.server_close(); worker.join(3)

    def test_explicit_model_failure_retries_but_malformed_next_attempt_stops(self):
        attempts = []

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                attempts.append(self.path)
                response = {"model": "gpt-6-astra", "status": "failed", "output": [],
                            "error": {"code": "server_overloaded"}}
                if len(attempts) == 1:
                    payload = ("data: " + json.dumps({"type": "response.failed", "response": response}) + "\n\n").encode()
                else:
                    payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args): pass

        with tempfile.TemporaryDirectory() as temporary:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                client = ResponsesClient(f"http://127.0.0.1:{server.server_port}/v1", "synthetic-credential", temporary, model="gpt-6-astra")
                with mock.patch.object(upstream().joint, '_retry_delay_seconds', return_value=.001):
                    with self.assertRaises(upstream().joint.InfrastructureFailure):
                        client.complete([], [])
                with self.assertRaises(upstream().joint.InfrastructureFailure):
                    client.complete([], [])
                self.assertEqual(len(attempts), 2)
                self.assertFalse(client.blocked_error['manual_retry_allowed'])
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(client.calls[0]["attempts"][0]["response_status"], "failed")
                self.assertEqual(client.calls[0]["attempts"][1]["status"], "provider_transport_or_json_error")
                self.assertTrue((Path(temporary) / "call-001/attempt-001/response.sse").is_file())
                self.assertTrue((Path(temporary) / "call-001/attempt-002/response.sse").is_file())
            finally:
                server.shutdown(); server.server_close(); worker.join(3)

    def test_stream_requires_terminal_and_does_not_invent_delta_output(self):
        with self.assertRaises(EOFError):
            ResponsesClient._stream_response(b'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call"}}\n\n')
        raw = b'data: {"type":"response.function_call_arguments.delta","delta":"unsafe partial"}\n\ndata: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n'
        terminal, assembled, evidence = ResponsesClient._stream_response(raw)
        self.assertEqual(terminal, assembled)
        self.assertEqual(assembled["output"], [])
        self.assertEqual(evidence["output_source"], "terminal_response.output")

    def test_retry_budget_auth_and_unknown_outcomes_have_distinct_termination(self):
        attempts, behavior = [], ['503']
        class Provider(BaseHTTPRequestHandler):
            def do_POST(self):
                attempts.append(self.rfile.read(int(self.headers['Content-Length'])))
                status = int(behavior[0]) if behavior[0].isdigit() else 503 if behavior[0] == 'truncated_error' else 200
                if behavior[0] == 'partial':
                    body = b'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","name":"unsafe"}}\n\n'
                elif behavior[0] == 'incomplete':
                    body = b'data: {"type":"response.incomplete","response":{"status":"incomplete","output":[]}}\n\n'
                else:
                    body = b'{"error":{"code":"fixture_error"}}'
                self.send_response(status)
                self.send_header('Content-Length', str(len(body) + (10 if behavior[0] == 'truncated_error' else 0)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        with tempfile.TemporaryDirectory() as temporary:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for name, count in (('503', 3), ('truncated_error', 3), ('401', 1), ('partial', 1), ('incomplete', 1)):
                    with self.subTest(behavior=name):
                        behavior[0], attempts[:] = name, []
                        client = ResponsesClient(f'http://127.0.0.1:{server.server_port}/v1', 'synthetic',
                                                 Path(temporary)/name, max_retries=2, model="gpt-6-astra")
                        with mock.patch.object(upstream().joint, '_retry_delay_seconds', return_value=.001):
                            with self.assertRaises(upstream().joint.InfrastructureFailure): client.complete([], [])
                        self.assertEqual(len(attempts), count)
                        self.assertEqual(client.attempt_count, count)
                        self.assertTrue(all(body == attempts[0] for body in attempts))
                        with self.assertRaises(upstream().joint.InfrastructureFailure): client.complete([], [])
                        self.assertEqual(len(attempts), count)
                        self.assertEqual(client.blocked_error['manual_retry_allowed'], name in ('503', 'truncated_error', '401'))
            finally:
                server.shutdown(); server.server_close(); worker.join(3)

    def test_http_error_retries_automatically_and_terminal_does_not_wait_for_eof(self):
        attempts, release = [], threading.Event()
        class Provider(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                attempts.append(self.path)
                if len(attempts) == 1:
                    self.send_response(429)
                    self.send_header('Retry-After', '.001')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: {"type":"response.completed","response":{"model":"gpt-6-astra","status":"completed","output":[]}}\n\n')
                self.wfile.flush()
                release.wait(5)
            def log_message(self, *args): pass
        with tempfile.TemporaryDirectory() as temporary:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
            server.daemon_threads = True
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                client = ResponsesClient(f'http://127.0.0.1:{server.server_port}/v1', 'synthetic', temporary, model="gpt-6-astra")
                started = time.monotonic()
                result = client.complete([], [])
                self.assertLess(time.monotonic()-started, 3)
                self.assertEqual(client.attempt_count, 2)
                self.assertEqual(len(client.calls), 1)
                self.assertIsNone(client.blocked_error)
                self.assertEqual(client.calls[0]['attempts'][0]['retry_delay_s'], .001)
                self.assertFalse(release.is_set())
                self.assertEqual(result['choices'][0]['message']['content'], '')
            finally:
                release.set()
                server.shutdown(); server.server_close(); worker.join(3)

    def test_rgb_model_motion_feedback_score_and_repair_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            robot_root = root / "fixture-install"

            class Robot:
                """No physics or GPU: the reference actor still chooses the actual tool input."""
                def __init__(self):
                    self.root, self.task, self.seed, self.layout_id = str(robot_root), "stack_bowls", 0, 0
                    self.step, self.executions, self.serial = 100, [], 0
                    self.left = {"q_rad": [0.0] * 6, "qdot_rad_s": [12345.0] * 6,
                                 "ee_pose": {"position_m": [-0.3, -0.35, 0.95], "quaternion_xyzw": [-0.5, 0.5, 0.5, 0.5]},
                                 "gripper": {"command_normalized": 1.0, "goal_normalized": 1.0, "position_m": [12345.0]}}
                    self.right = deepcopy(self.left)
                    self.right["ee_pose"]["position_m"][0] = 0.3

                def start(self):
                    return {"data_origin": "synthetic_fixture", "observation_profile": "rgb_proprio_v1",
                            "native_control_hz": 25, "gripper_max_step": .25,
                            "arm_joint_names": {side: [f"joint{i}" for i in range(1, 7)] for side in ("left", "right")},
                            "arm_joint_limits_rad": {side: [[-3.14, 3.14]] * 6 for side in ("left", "right")},
                            "arm_max_step_rad": {side: [.05] * 6 for side in ("left", "right")}}

                def observe(self):
                    self.serial += 1
                    identifier = f"raw-fixture-{self.serial}"
                    image = root / f"{identifier}.png"
                    cv2.imwrite(str(image), np.full((8, 8, 3), self.step % 255, np.uint8))
                    return {"raw_observation_id": identifier, "data_origin": "synthetic_fixture", "observation_profile": "rgb_proprio_v1",
                            "sim_step": self.step, "step_domain": "adapter_control_step", "control_dt_s": 0.004,
                            "instruction": "Stack the three bowls together.",
                            "cameras": [{"camera_id": camera, "frame_id": identifier + camera, "png_path": str(image), "width": 8, "height": 8}
                                        for camera in ("cam_head", "cam_left_wrist", "cam_right_wrist")],
                            "robot_state": {"arms": {"left": deepcopy(self.left), "right": deepcopy(self.right)}}}

                def record(self, enabled=True): return {"enabled": enabled}

                def execute(self, action):
                    self.executions.append(deepcopy(action))
                    self.left["ee_pose"] = deepcopy(action["path"]["waypoints"][0]["left"])
                    self.step += 162
                    return {"status": "completed", "reason": "synthetic_fixture", "executed_steps": 62,
                            "waypoint_results": [{"waypoint_index": 0, "position_error_m": .001, "private_sentinel": "receipt-private-sentinel"}],
                            "stop_confirmation": {"clock": "paused", "max_joint_velocity_rad_s": 12345.0},
                            "executed_control_steps": 162, "executed_native_steps": 16,
                            "episode_terminal": {"ended": False, "success": None, "reason": None,
                                                 "native_action_count": len(self.executions) * 16, "native_step_limit": 800},
                            "terminal_observation": self.observe()}

                def evaluate(self): return {"task_success": False, "normalized_score": 0.0, "private_sentinel": "not-an-actor-input"}
                def save_video(self):
                    videos = []
                    for camera in ("cam_head", "cam_left_wrist", "cam_right_wrist"):
                        path = root / f"{camera}.mp4"
                        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5, (8, 8))
                        try:
                            for _ in range(2): writer.write(np.zeros((8, 8, 3), np.uint8))
                        finally:
                            writer.release()
                        videos.append({"camera_id": camera, "video_path": str(path), "frames": 2, "synthetic": True})
                    return {"data_origin": "synthetic_fixture", "videos": videos}

            requests = []
            attempt_payloads = []
            repair_only = False

            class Provider(BaseHTTPRequestHandler):
                def do_POST(self):
                    self.assert_path = self.path
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    attempt_payloads.append(request)
                    requests.append(request)
                    index = len(requests)
                    if index == 1 or repair_only:
                        name, arguments = "move_eef", {"targets": {"left_z": 0.81, "right_z": 0.81}, "note": "synthetic dual-arm refusal"}
                    elif index == 2 or index > 3:
                        name, arguments = "move_eef", {"targets": {"left_z": 0.805}, "note": "synthetic model selected a different legal target"}
                    else:
                        name, arguments = "give_up", {"reason": "synthetic fixture complete", "hindsight": "I inspected the actual completed receipt and fresh RGB."}
                    item = {"id": f"fc-{index}", "type": "function_call", "status": "completed",
                            "call_id": f"call-{index}", "name": name, "arguments": json.dumps(arguments)}
                    response = {"id": f"response-{index}", "model": "gpt-6-astra", "status": "completed", "output": [],
                                          "usage": {"input_tokens": 100, "output_tokens": 20}}
                    # Actual provider shape: complete items are streamed, terminal output is empty.
                    response = ("event: response.output_item.done\ndata: " + json.dumps({"type": "response.output_item.done", "output_index": 0, "item": item}) +
                                "\n\nevent: response.completed\ndata: " + json.dumps({"type": "response.completed", "response": response}) + "\n\ndata: [DONE]\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)

                def log_message(self, *args): pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                robot = Robot()
                raw = robot.observe()
                public = observation(raw, 0)
                self.assertEqual(set(public.state), {arm + suffix for arm in ("left", "right")
                    for suffix in ("_arm_joint_state", "_ee_joint_state", "_ee_pose")})
                self.assertNotIn("task", public.extra)
                self.assertIsNone(public.remaining_steps)
                spec = action_spec(robot.start(), raw)
                self.assertEqual(spec.control_hz, 25)
                self.assertEqual(spec.max_step, tuple([.05] * 6 + [.25]) * 2)
                for injected in ({"camera_calibration": {"K": []}}, {"robot_model": {}}, {"instruction": ""}):
                    with self.subTest(injected=injected), self.assertRaises(ValueError):
                        observation({**raw, **injected}, 0)
                client = ResponsesClient(f"http://127.0.0.1:{server.server_port}/v1", "synthetic-credential", root / "run/api", model="gpt-6-astra")
                original = upstream()
                # Exercise actual HTTP requests while all three upstream prompt injection paths are poisoned.
                with mock.patch.object(original.docs, "ARX_X5_EEF_DOCS", "fixedheight-private-sentinel"), \
                     mock.patch.object(original.eef.EefAgentPolicy, "_system_message", return_value="scoringthreshold-private-sentinel"), \
                     mock.patch.object(original.joint, "_goal_content", side_effect=AssertionError("upstream task goal invoked")):
                    robot.task = "press_by_number"  # Upstream adds a recipe-independent task procedure for this name.
                    result = run_episode(robot, client, root / "run", max_calls=4)
                class NativeTerminalRobot(Robot):
                    terminal_after = 3

                    def execute(self, action):
                        receipt = super().execute(action)
                        if len(self.executions) == self.terminal_after:
                            receipt["episode_terminal"].update(ended=True, success=True, reason="native_success",
                                                               private_sentinel="native-terminal-private-sentinel")
                        return receipt

                    def evaluate(self):
                        result = super().evaluate()
                        return {**result, "task_success": True, "normalized_score": 1.0}
                terminal_robot = NativeTerminalRobot()
                terminal_client = ResponsesClient(client.base_url, "synthetic-credential", root / "native-terminal/api", model="gpt-6-astra")
                stopped = run_episode(terminal_robot, terminal_client, root / "native-terminal")
                first_terminal = NativeTerminalRobot()
                first_terminal.terminal_after = 1
                first_client = ResponsesClient(client.base_url, "synthetic-credential", root / "first-terminal/api", model="gpt-6-astra")
                first_stopped = run_episode(first_terminal, first_client, root / "first-terminal")
                repair_only = True
                capped_robot = Robot()
                capped_client = ResponsesClient(client.base_url, "synthetic-credential", root / "capped/api", model="gpt-6-astra")
                capped = run_episode(capped_robot, capped_client, root / "capped", max_calls=1)
                incomplete_policy = make_policy(robot.start(), raw, client, max_calls=1)
                incomplete = upstream().responses.responses_to_chat_completion(dict(status="incomplete", output=[
                    dict(type="function_call", call_id="partial", name="move_eef", arguments="{")]))
                with mock.patch.object(client, "complete", return_value=incomplete) as complete:
                    chunk = incomplete_policy.act(public)
                self.assertEqual(incomplete_policy.calls, 1)
                complete.assert_called_once()
                self.assertEqual(chunk.actions[0].meta["stop_reason"], "llm_call_budget_exhausted")
            finally:
                server.shutdown(); server.server_close(); worker.join(3)
            self.assertTrue(result["loop_verified"], result["errors"])
            self.assertEqual(result["llm_calls"], 3)
            self.assertEqual(result["http_attempt_count"], 3)
            self.assertEqual(client.max_retries, 12)
            self.assertEqual(client.calls[0]["http_attempt_count"], 1)
            self.assertTrue(client.calls[0]["attempts"][0]["usage_known"])
            self.assertEqual(len(robot.executions), 1)  # The refused dual-arm call never dispatches.
            self.assertEqual(result["termination_reason"], "give_up")
            self.assertAlmostEqual(robot.executions[0]["path"]["waypoints"][0]["left"]["position_m"][2], 0.9551)
            self.assertEqual(result["executed_adapter_control_steps"], 162)
            self.assertEqual(result["feedback_model_calls"], 1)
            self.assertFalse(result["evaluation"]["task_success"])
            self.assertFalse(result["actor_input_policy"]["camera_calibration"])
            payloads = json.dumps(requests)
            for forbidden in ("fixedheight-private-sentinel", "scoringthreshold-private-sentinel", "receipt-private-sentinel",
                              "TASK RECIPE", "table surface is at", "0.765", "0.7725", "within 0.15 m", "20 degrees",
                              "press the blue confirmation", "12345", "max_joint_velocity", "joint_names", "qdot_rad_s",
                              "episode_terminal", "native_step_limit", "native_action_count", "Env steps remaining",
                              "native-terminal-private-sentinel", "model-call limit", "0.5 rad/s", "250 Hz"):
                self.assertNotIn(forbidden, payloads)
            self.assertIn("0.1501 m", payloads)  # Public tool geometry remains available.
            self.assertIn("Goal: Stack the three bowls together.", payloads)
            self.assertEqual(result["actor_input_policy"]["instruction"], "native public task instruction only")
            second = json.dumps(requests[1])
            third = json.dumps(requests[2])
            self.assertIn("Neither arm moved", second)
            self.assertIn("Actual backend receipt", third)
            self.assertIn("completed", third)
            self.assertIn("Arrival check for the previous move_eef target", third)
            self.assertNotIn("not-an-actor-input", third)
            self.assertNotIn("synthetic-credential", third)
            self.assertEqual(client.calls[0]["input_observation_id"], client.calls[1]["input_observation_id"])
            self.assertNotEqual(client.calls[1]["input_observation_id"], client.calls[2]["input_observation_id"])
            self.assertEqual(len(client.calls[0]["model_images"]), 3)
            self.assertEqual(len(client.calls[2]["model_images"]), 6)  # Original policy retains image history.
            self.assertTrue(all(image["mime_type"] == "image/jpeg" for image in client.calls[2]["model_images"]))
            self.assertEqual({tool["name"] for tool in requests[0]["tools"]}, {"move_eef", "give_up", "done"})
            self.assertFalse(requests[0]["parallel_tool_calls"])
            self.assertTrue(requests[0]["stream"])
            self.assertEqual(client.calls[0]["output_source"], "response.output_item.done")
            self.assertEqual(client.calls[0]["terminal_event"], "response.completed")
            self.assertEqual(json.loads((Path(client.calls[0]["terminal_response_path"])).read_text())["output"], [])
            self.assertEqual(json.loads((Path(client.calls[0]["assembled_response_path"])).read_text())["output"][0]["id"], "fc-1")
            self.assertEqual(len(terminal_robot.executions), 3)
            self.assertEqual(len(terminal_client.calls), 3)
            self.assertEqual(stopped["termination_reason"], "native_success")
            self.assertTrue(stopped["episode_terminal"]["success"])
            self.assertEqual(stopped["action_count"], 3)
            self.assertTrue(stopped["loop_verified"], stopped["errors"])
            self.assertEqual(stopped["budgets"]["max_llm_calls"], 170)
            self.assertIsNone(stopped["budgets"]["model_request_timeout_s"])
            self.assertTrue(first_stopped["episode_verified"])
            self.assertFalse(first_stopped["loop_verified"])
            self.assertEqual(first_stopped["feedback_model_calls"], 0)
            self.assertEqual(len(first_client.calls), 1)
            self.assertTrue(first_stopped["evaluation"]["task_success"])
            self.assertEqual(capped["termination_reason"], "llm_call_budget_exhausted")
            self.assertEqual(len(capped_client.calls), 1)
            self.assertEqual(capped_robot.executions, [])
            self.assertIn("framework_stop", capped["decisions"][0])


if __name__ == "__main__":
    unittest.main()
