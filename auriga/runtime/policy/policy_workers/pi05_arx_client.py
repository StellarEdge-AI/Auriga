"""Bridge saved Harness actor observations to an existing OpenPI WebSocket service.

Run with the OpenPI Python environment. This client never loads a model, checks
checkpoint files, executes actions, retries inference, or controls the service.
"""

import argparse
from contextlib import redirect_stdout
import json
import math
from pathlib import Path
import sys
import time
import traceback
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from auriga.runtime.policy.action_policy import TRAIN_CONFIG, load_policy_observation, validate_joint_chunk
from auriga.core.presentation.actor_presentation import strip_hashes


class OpenPIServiceClient:
    def __init__(self, server_url, timeout=120):
        if urlsplit(server_url).scheme not in {"ws", "wss"} or not urlsplit(server_url).hostname:
            raise ValueError("policy_server_requires_websocket_url")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("policy_timeout_must_be_positive_and_finite")
        # These dependencies belong exclusively to the isolated OpenPI environment.
        from openpi_client import msgpack_numpy
        from websockets.sync.client import connect

        self.codec = msgpack_numpy
        self.timeout = timeout
        self.closed = False
        # One connection attempt, bounded handshake and response waits. The parent
        # process also bounds the entire request, including encoding and send().
        self.connection = connect(server_url, compression=None, max_size=2**20,
                                  open_timeout=timeout, close_timeout=min(timeout, 5))
        try:
            handshake = self._receive()
            if not isinstance(handshake, dict) or handshake.get("config") != TRAIN_CONFIG:
                raise ValueError("policy_server_config_mismatch")
            if handshake.get("action_shape") != [50, 14]:
                raise ValueError("policy_server_action_shape_mismatch")
            # Require metadata that can be faithfully recorded in Harness JSON.
            json.dumps(handshake, allow_nan=False)
            self.metadata = dict(policy_backend="remote_websocket", server_url=server_url,
                server_handshake=handshake, train_config=TRAIN_CONFIG, robot_model_id="arx-x5",
                checkpoint_attested_by_server=False, checkpoint_verification="not_attested_by_service",
                model_loaded_by_bridge=False, observation_identity_source="Harness_saved_actor_observation",
                server_echoes_observation_identity=False, noise_seed_supported=False,
                action_semantics="absolute_joint_rad_and_normalized_gripper_left6_1_right6_1",
                execution_authority="Harness_only")
        except BaseException:
            self.close()
            raise

    def _receive(self):
        payload = self.connection.recv(timeout=self.timeout)
        if isinstance(payload, str):
            raise RuntimeError(f"policy_server_error: {payload}")
        return self.codec.unpackb(payload)

    def infer(self, request):
        if self.closed:
            raise RuntimeError("policy_server_connection_closed")
        if request.get("noise_seed") is not None:
            raise ValueError("policy_server_does_not_support_noise_seed")
        obs, evidence = load_policy_observation(request["run_dir"], request["observation_id"])
        if (evidence["observation"]["observation_id"] != request["observation_id"]
                or evidence["source_run_dir"] != str(Path(request["run_dir"]).resolve())):
            raise ValueError("policy_response_observation_mismatch")
        if request.get("prompt") is not None:
            if not isinstance(request["prompt"], str) or not request["prompt"].strip():
                raise ValueError("policy_prompt_must_be_nonempty_text")
            obs["prompt"] = request["prompt"]
        obs["prompt"] = strip_hashes(obs["prompt"])
        try:
            started = time.perf_counter()
            self.connection.send(self.codec.packb(obs))
            result = self._receive()
            actions = result["actions"]
            if hasattr(actions, "tolist"):
                actions = actions.tolist()
            actions = validate_joint_chunk(actions)
            elapsed = time.perf_counter() - started
        except BaseException:
            # Poison the connection after timeout/error; never replay a request or
            # associate its late response with a later observation.
            self.close()
            raise
        return dict(status="inference_completed", **self.metadata, **evidence, actions=actions,
                    action_shape=[50, 14], inference_seconds=elapsed, executed_steps=0,
                    policy_prompt=obs["prompt"], prompt_source="override" if request.get("prompt") else "public_task",
                    noise_seed=None)

    def close(self):
        if not self.closed:
            self.closed = True
            self.connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--stdio", action="store_true", required=True)
    args = parser.parse_args()
    client = None
    try:
        with redirect_stdout(sys.stderr):
            client = OpenPIServiceClient(args.server_url, args.timeout)
        print(json.dumps(dict(status="ready", **client.metadata), allow_nan=False), flush=True)
        for line in sys.stdin:
            with redirect_stdout(sys.stderr):
                response = client.infer(json.loads(line))
            print(json.dumps(response, allow_nan=False), flush=True)
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps(dict(status="error", error=str(error))), flush=True)
        return 1
    finally:
        if client is not None:
            client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
