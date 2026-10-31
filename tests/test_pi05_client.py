"""The service bridge binds one saved actor observation to one bounded request."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from auriga.runtime.policy.action_policy import OpenPIProcessPolicy, TRAIN_CONFIG
from scripts.pi05_arx_client import OpenPIServiceClient


class ServiceClientCheck(unittest.TestCase):
    def setUp(self):
        self.connection = Mock()
        self.connection.recv.return_value = {"config": TRAIN_CONFIG, "action_shape": [50, 14]}
        self.connect = Mock(return_value=self.connection)
        codec = types.SimpleNamespace(packb=lambda value: value, unpackb=lambda value: value)
        self.modules = patch.dict("sys.modules", {
            "openpi_client": types.SimpleNamespace(msgpack_numpy=codec),
            "websockets": types.ModuleType("websockets"),
            "websockets.sync": types.ModuleType("websockets.sync"),
            "websockets.sync.client": types.SimpleNamespace(connect=self.connect),
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.request = dict(run_dir="/tmp/actor-run", observation_id="obs-7")
        self.observation = dict(state=[0] * 14, images={"real": "saved-image"}, prompt="public task")
        self.evidence = dict(source_run_dir="/tmp/actor-run", observation=dict(observation_id="obs-7"),
                             public_instruction="public task")
        self.loader = patch("scripts.pi05_arx_client.load_policy_observation",
                            return_value=(self.observation, self.evidence)).start()
        self.addCleanup(patch.stopall)

    def test_saved_observation_prompt_identity_and_honest_metadata(self):
        client = OpenPIServiceClient("ws://127.0.0.1:8000", timeout=3)
        self.connection.recv.return_value = dict(actions=[[0.] * 14 for _ in range(50)])
        result = client.infer({**self.request, "prompt": "precise instruction"})
        self.loader.assert_called_once_with("/tmp/actor-run", "obs-7")
        self.connection.send.assert_called_once_with(self.observation)
        self.assertEqual(result["observation"], self.evidence["observation"])
        self.assertEqual(result["public_instruction"], "public task")
        self.assertEqual(result["policy_prompt"], "precise instruction")
        self.assertEqual(result["executed_steps"], 0)
        self.assertFalse(result["checkpoint_attested_by_server"])
        self.assertFalse(result["model_loaded_by_bridge"])
        self.assertNotIn("checkpoint_files_verified", result)
        self.assertNotIn("checkpoint_revision", result)
        self.assertEqual(self.connection.recv.call_args.kwargs, {"timeout": 3})
        self.assertEqual(self.connect.call_args.kwargs["open_timeout"], 3)
        client.close()
        client.close()
        self.connection.close.assert_called_once_with()

    def test_model_prompt_strips_public_and_override_hashes_without_changing_evidence(self):
        fingerprint = 'abcdef0123456789' * 4
        public = 'Move the cup. sha256=' + fingerprint + ' Keep it upright.'
        self.evidence['public_instruction'] = public
        self.evidence['robot_state_sha256'] = fingerprint
        client = OpenPIServiceClient('ws://127.0.0.1:8000', timeout=3)
        self.addCleanup(client.close)
        actions = [[0.] * 14 for _ in range(50)]
        self.connection.recv.return_value = dict(actions=actions)
        for override in (None, 'Lift the cup. checkpoint_sha256=' + fingerprint + ' Keep it upright.'):
            with self.subTest(override=override is not None):
                observation = {**self.observation, 'prompt': public}
                self.loader.return_value = (observation, self.evidence)
                result = client.infer({**self.request, 'prompt': override})
                sent = self.connection.send.call_args.args[0]
                self.assertNotIn(fingerprint, sent['prompt'])
                self.assertNotIn('sha256', sent['prompt'])
                self.assertIn('Keep it upright.', sent['prompt'])
                self.assertEqual(sent['state'], self.observation['state'])
                self.assertEqual(sent['images'], self.observation['images'])
                self.assertEqual(result['public_instruction'], public)
                self.assertEqual(result['robot_state_sha256'], fingerprint)
                self.assertEqual(result['policy_prompt'], sent['prompt'])
                self.assertEqual(result['actions'], actions)

    def test_wrong_configuration_or_shape_is_rejected_before_inference(self):
        for metadata in ({"config": "pi0_base", "action_shape": [50, 14]},
                         {"config": TRAIN_CONFIG, "action_shape": [50, 32]}, {}):
            with self.subTest(metadata=metadata):
                self.connection.reset_mock()
                self.connection.recv.return_value = metadata
                with self.assertRaises(ValueError):
                    OpenPIServiceClient("ws://localhost:8000")
                self.connection.send.assert_not_called()
                self.connection.close.assert_called_once()

    def test_handshake_timeout_does_not_reconnect(self):
        self.connection.recv.side_effect = TimeoutError("handshake deadline")
        with self.assertRaises(TimeoutError):
            OpenPIServiceClient("ws://localhost:8000", timeout=1)
        self.connect.assert_called_once()
        self.connection.close.assert_called_once()

    def test_refused_connection_has_no_retry(self):
        self.connect.side_effect = ConnectionRefusedError("offline")
        with self.assertRaises(ConnectionRefusedError):
            OpenPIServiceClient("ws://localhost:8000", timeout=1)
        self.connect.assert_called_once()

    def test_timeout_does_not_resend_or_consume_late_response(self):
        client = OpenPIServiceClient("ws://localhost:8000", timeout=1)
        self.connection.recv.side_effect = TimeoutError("response deadline")
        with self.assertRaises(TimeoutError):
            client.infer(self.request)
        with self.assertRaisesRegex(RuntimeError, "connection_closed"):
            client.infer(self.request)
        self.connection.send.assert_called_once()
        self.connect.assert_called_once()
        self.connection.close.assert_called_once()

    def test_invalid_server_actions_and_text_error_close_connection(self):
        invalid = [[0.] * 14 for _ in range(50)]
        invalid[3][4] = float("nan")
        for response in (dict(actions=invalid), dict(actions=[[0.] * 32 for _ in range(50)]),
                         "server traceback", {}):
            with self.subTest(response_type=type(response).__name__):
                self.connection.reset_mock()
                self.connection.recv.return_value = {"config": TRAIN_CONFIG, "action_shape": [50, 14]}
                client = OpenPIServiceClient("ws://localhost:8000")
                self.connection.recv.return_value = response
                with self.assertRaises((ValueError, RuntimeError, KeyError)):
                    client.infer(self.request)
                self.connection.close.assert_called_once()
                self.connection.send.assert_called_once()

    def test_noise_seed_and_bad_identity_fail_before_send(self):
        client = OpenPIServiceClient("ws://localhost:8000")
        with self.assertRaisesRegex(ValueError, "noise_seed"):
            client.infer({**self.request, "noise_seed": 0})
        self.loader.assert_not_called()
        self.connection.send.assert_not_called()
        bad = copy.deepcopy(self.evidence)
        bad["observation"]["observation_id"] = "other"
        self.loader.return_value = (self.observation, bad)
        with self.assertRaisesRegex(ValueError, "observation_mismatch"):
            client.infer(self.request)
        self.connection.send.assert_not_called()
        client.close()

    def test_invalid_url_or_timeout_never_connects(self):
        for url, timeout in (("http://localhost:8000", 1), ("", 1), ("ws://localhost", 0),
                             ("ws://localhost", float("inf"))):
            with self.assertRaises(ValueError):
                OpenPIServiceClient(url, timeout)
        self.connect.assert_not_called()


class LocalPolicyPromptCheck(unittest.TestCase):
    def test_local_inference_receives_clean_prompt_and_keeps_source_evidence(self):
        from auriga.runtime.policy.policy_workers import pi05_arx_inference as worker
        fingerprint = 'abcdef0123456789' * 4
        public = 'Move the cup. sha256=' + fingerprint + ' Keep it upright.'
        evidence = dict(public_instruction=public, robot_state_sha256=fingerprint)
        observation = dict(state=[0] * 14, images={'camera': 'saved pixels'}, prompt=public)
        actions = [[0.] * 14 for _ in range(50)]
        policy = types.SimpleNamespace(infer=Mock(return_value=dict(actions=types.SimpleNamespace(tolist=lambda: actions))))
        modules = {'openpi': types.ModuleType('openpi'),
                   'openpi.policies': types.SimpleNamespace(policy_config=types.SimpleNamespace(create_trained_policy=lambda *_: policy)),
                   'openpi.training': types.SimpleNamespace(config=types.SimpleNamespace(get_config=lambda _: None))}
        requests = [dict(run_dir='/tmp/run', observation_id='obs', prompt=prompt)
                    for prompt in (None, 'Lift the cup. sha256=' + fingerprint + ' Keep it upright.')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stats = root / 'assets' / 'arx_x5_sim' / 'norm_stats.json'
            stats.parent.mkdir(parents=True)
            stats.write_text('{}')
            (root / 'pi05_arx_manifest.json').write_text(json.dumps(dict(revision=worker.CHECKPOINT_REVISION,
                train_config=TRAIN_CONFIG, files=[], repo_id='fixture')))
            output = io.StringIO()
            with patch.dict('sys.modules', modules), patch.object(worker, '__file__', str(root / 'worker.py')), \
                    patch.object(worker, 'NORM_SHA256', hashlib.sha256(stats.read_bytes()).hexdigest()), \
                    patch.object(worker, 'load_policy_observation', side_effect=lambda *_: (copy.deepcopy(observation), evidence)), \
                    patch('sys.argv', ['worker', '--checkpoint', str(root), '--stdio']), \
                    patch('sys.stdin', io.StringIO(''.join(json.dumps(request) + '\n' for request in requests))), \
                    patch('sys.stdout', output):
                worker.main()
        responses = [json.loads(line) for line in output.getvalue().splitlines()][1:]
        self.assertEqual(len(responses), 2)
        for call, response in zip(policy.infer.call_args_list, responses):
            sent = call.args[0]
            self.assertNotIn(fingerprint, sent['prompt'])
            self.assertNotIn('sha256', sent['prompt'])
            self.assertIn('Keep it upright.', sent['prompt'])
            self.assertEqual(sent['state'], observation['state'])
            self.assertEqual(sent['images'], observation['images'])
            self.assertEqual(response['public_instruction'], public)
            self.assertEqual(response['robot_state_sha256'], fingerprint)
            self.assertEqual(response['actions'], actions)
        self.assertEqual(policy.infer.call_count, 2)


class ProcessResponseCheck(unittest.TestCase):
    def setUp(self):
        read_fd, self.write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb")
        self.addCleanup(self.stdout.close)
        self.addCleanup(os.close, self.write_fd)
        self.policy = OpenPIProcessPolicy.__new__(OpenPIProcessPolicy)
        self.policy.process = types.SimpleNamespace(stdout=self.stdout)
        self.policy.timeout = 0.05
        self.policy._response_buffer = bytearray()

    def test_partial_line_obeys_response_deadline(self):
        os.write(self.write_fd, b'{"status":"ready"}')
        # Release even a regressed blocking readline, so this test cannot hang.
        completion = threading.Timer(0.5, os.write, args=(self.write_fd, b"\n"))
        completion.start()
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "timed out"):
                self.policy._receive()
            self.assertLess(time.monotonic() - started, 0.4)
        finally:
            completion.cancel()
            completion.join()

    def test_extra_complete_lines_are_preserved_without_another_pipe_read(self):
        os.write(self.write_fd, b'{"status":"ready"}\n{"status":"inference_completed","id":2}\n')
        self.assertEqual(self.policy._receive(), {"status": "ready"})
        with patch("auriga.runtime.policy.action_policy.select.select", side_effect=AssertionError("buffered response must not wait")):
            self.assertEqual(self.policy._receive(), {"status": "inference_completed", "id": 2})
        self.assertEqual(self.policy._response_buffer, b"")

    def test_partial_next_line_is_preserved_and_completed(self):
        os.write(self.write_fd, b'{"status":"ready"}\n{"status":')
        self.assertEqual(self.policy._receive(), {"status": "ready"})
        os.write(self.write_fd, b'"inference_completed"}\n')
        self.assertEqual(self.policy._receive(), {"status": "inference_completed"})


class ProcessSelectionCheck(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.log_path = Path(self.directory.name) / "pi05.log"
        self.process = Mock()
        self.process.poll.return_value = None
        self.popen = patch("auriga.runtime.policy.action_policy.subprocess.Popen", return_value=self.process).start()
        self.receive = patch.object(OpenPIProcessPolicy, "_receive", return_value={"status": "ready"}).start()
        self.addCleanup(patch.stopall)

    def test_service_bridge_does_not_take_checkpoint_or_gpu_flags(self):
        with patch.dict("os.environ", {}, clear=True):
            policy = OpenPIProcessPolicy("/openpi/python", "/unused/checkpoint", gpu=7,
                                         server_url="ws://localhost:8000", timeout=2, log_path=self.log_path)
        command = self.popen.call_args.args[0]
        self.assertTrue(command[1].endswith("pi05_arx_client.py"))
        self.assertNotIn("--checkpoint", command)
        self.assertNotIn("CUDA_VISIBLE_DEVICES", self.popen.call_args.kwargs["env"])
        with self.assertRaisesRegex(ValueError, "noise_seed"):
            policy.infer("/tmp/run", "obs-1", noise_seed=0)
        self.process.stdin.write.assert_not_called()
        policy.close()

    def test_local_model_mode_remains_available(self):
        policy = OpenPIProcessPolicy("/openpi/python", "/checkpoint", gpu=7, log_path=self.log_path)
        self.assertTrue(self.popen.call_args.args[0][1].endswith("pi05_arx_inference.py"))
        self.assertIn("--checkpoint", self.popen.call_args.args[0])
        self.assertEqual(self.popen.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "7")
        policy.close()

    def test_timeout_terminates_bridge(self):
        policy = OpenPIProcessPolicy("/openpi/python", server_url="ws://localhost:8000", log_path=self.log_path)
        self.receive.side_effect = TimeoutError("deadline")
        with self.assertRaises(TimeoutError):
            policy.infer("/tmp/run", "obs-1")
        self.process.terminate.assert_called_once()
        self.assertTrue(policy.log.closed)

    def test_returned_identity_is_checked(self):
        for source, observation_id in (("/tmp/other", "obs-1"), ("/tmp/run", "obs-other")):
            with self.subTest(source=source, observation_id=observation_id):
                self.receive.return_value = {"status": "ready"}
                policy = OpenPIProcessPolicy("/openpi/python", server_url="ws://localhost:8000", log_path=self.log_path)
                self.receive.return_value = dict(status="inference_completed", source_run_dir=source,
                    observation=dict(observation_id=observation_id), actions=[[0.] * 14 for _ in range(50)])
                with self.assertRaisesRegex(ValueError, "observation_mismatch"):
                    policy.infer("/tmp/run", "obs-1")
                self.assertTrue(policy.log.closed)


if __name__ == "__main__":
    unittest.main()
