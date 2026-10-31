"""Read-only OpenPI ARX X5 inference; execution remains owned by Harness."""

from contextlib import closing
import io
import json
import math
import os
from pathlib import Path
import select
import subprocess
import time

from ...core.observation_policy import require_actor_observation
from ...core.storage.store import Store


TRAIN_CONFIG = "pi05_base_aloha_full_sim_arx-x5_seed_0"
CHECKPOINT_REVISION = "35efbc7dedfdbeeb6e95fb749bd885d73d483e41"
NORM_SHA256 = "ad7dea3e3d2bcdb348945fe03422ab1adccd03baf67318b1a1d153dfe8694db5"
CAMERAS = {"cam_head": "cam_high", "cam_left_wrist": "cam_left_wrist", "cam_right_wrist": "cam_right_wrist"}


def arx_state(state):
    values = []
    for side in ("left", "right"):
        arm = state["arms"][side]
        expected_names = [f"joint{i}" for i in range(1, 7)]
        if (arm.get("source_kind") != "measurement" or len(arm["q_rad"]) != 6
                or arm.get("joint_names", expected_names) != expected_names):
            raise ValueError("policy_requires_measured_arx_x5_joint_order")
        gripper = arm["gripper"]
        if (gripper.get("command_source_kind") != "command"
                or not 0 <= gripper["command_normalized"] <= 1):
            raise ValueError("policy_requires_normalized_gripper_command")
        values.extend(arm["q_rad"] + [gripper["command_normalized"]])
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in values):
        raise ValueError("policy_state_must_be_finite")
    return values


def load_policy_observation(run_dir, observation_id):
    import numpy as np
    from PIL import Image

    with closing(Store(run_dir, {}, read_only=True)) as store:
        obs = store.read("observation", observation_id)
        require_actor_observation(obs)
        if (obs.get("data_origin") != "robodojo_runtime" or obs.get("world_kind") != "observed"
                or obs.get("robot_model_id") != "arx-x5" or not obs.get("task_instruction")):
            raise ValueError("policy_requires_real_arx_x5_observation_and_public_instruction")
        state_info, state_bytes = store.resolve_artifact(obs["robot_state_ref"], ("actor",))
        state = json.loads(state_bytes)
        timing = (obs["sim_step"], obs["step_domain"])
        if None in timing or (state.get("sim_step"), state.get("step_domain")) != timing:
            raise ValueError("policy_observation_state_timing_mismatch")
        values = arx_state(state)
        images, frames = {}, []
        for camera_id, policy_name in CAMERAS.items():
            matches = [camera for camera in obs["cameras"] if camera["camera_id"] == camera_id]
            if len(matches) != 1:
                raise ValueError("policy_requires_one_frame_per_camera")
            camera = matches[0]
            if ((camera.get("sim_step"), camera.get("step_domain")) != timing
                    or camera.get("pixel_transform") != "identity"):
                raise ValueError("policy_camera_timing_or_transform_mismatch")
            info, data = store.resolve_image(camera["rgb_ref"], ("actor",))
            with Image.open(io.BytesIO(data)) as image:
                if image.mode != "RGB" or image.size != (camera["width"], camera["height"]):
                    raise ValueError("policy_rgb_size_or_mode_mismatch")
                images[policy_name] = np.array(image, dtype=np.uint8).transpose(2, 0, 1)
            frames.append(dict(camera_id=camera_id, policy_key=policy_name, rgb_ref=camera["rgb_ref"],
                               frame_id=camera["frame_id"], sha256=info["sha256"],
                               reconstruction=info.get("reconstruction")))
    evidence = dict(source_run_dir=str(Path(run_dir).resolve()),
        observation={key: obs[key] for key in ("run_id", "episode_id", "env_instance_id", "env_generation",
                                               "observation_id", "sim_step", "step_domain")},
        public_instruction=obs["task_instruction"], robot_state_sha256=state_info["sha256"],
        state_values=values, gripper_source="last_normalized_command", images=frames)
    return dict(state=np.asarray(values, dtype=np.float32), images=images, prompt=obs["task_instruction"]), evidence


def validate_joint_chunk(actions):
    if not isinstance(actions, list) or len(actions) != 50 or any(not isinstance(row, list) or len(row) != 14 for row in actions):
        raise ValueError("policy_requires_postprocessed_50_by_14_joint_chunk")
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for row in actions for x in row):
        raise ValueError("policy_actions_must_be_finite")
    return actions


class OpenPIProcessPolicy:
    """An isolated policy process reads saved actor observations; Harness executes."""

    def __init__(self, python, checkpoint=None, *, gpu=7, log_path, timeout=600, server_url=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("policy_timeout_must_be_positive_and_finite")
        if server_url is None and checkpoint is None:
            raise ValueError("policy_checkpoint_required_for_local_process")
        self.timeout = timeout
        self._response_buffer = bytearray()
        self.server_url = server_url
        self.log = Path(log_path).open("a")
        scripts = Path(__file__).with_name("policy_workers")
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if server_url is not None:
            command = [str(python), str(scripts / "pi05_arx_client.py"), "--server-url", server_url,
                       "--timeout", str(timeout), "--stdio"]
        else:
            command = [str(python), str(scripts / "pi05_arx_inference.py"), "--checkpoint", str(checkpoint), "--stdio"]
            env.update(CUDA_VISIBLE_DEVICES=str(gpu), XLA_PYTHON_CLIENT_MEM_FRACTION="0.25",
                       XLA_PYTHON_CLIENT_PREALLOCATE="false")
        self.process = None
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=self.log, text=True, env=env)
            self.metadata = self._receive()
            if self.metadata.get("status") != "ready":
                raise RuntimeError(self.metadata)
        except BaseException:
            self.close()
            raise

    def _receive(self):
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self._response_buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError("OpenPI process response timed out; inspect policy log")
            # Read only bytes already available in the pipe. TextIO.readline()
            # can block forever after select reports an incomplete first line.
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("OpenPI process exited before a complete response; inspect policy log")
            self._response_buffer.extend(chunk)
        end = self._response_buffer.index(b"\n")
        line = bytes(self._response_buffer[:end])
        del self._response_buffer[:end + 1]
        result = json.loads(line)
        if result.get("status") == "error":
            raise RuntimeError(result["error"])
        return result

    def infer(self, run_dir, observation_id, *, prompt=None, noise_seed=None):
        if self.server_url is not None and noise_seed is not None:
            raise ValueError("policy_server_does_not_support_noise_seed")
        try:
            self.process.stdin.write(json.dumps(dict(run_dir=str(run_dir), observation_id=observation_id,
                                                    prompt=prompt, noise_seed=noise_seed)) + "\n")
            self.process.stdin.flush()
            result = self._receive()
            if result.get("status") != "inference_completed":
                raise ValueError("policy_response_status_mismatch")
            validate_joint_chunk(result["actions"])
            if result["observation"]["observation_id"] != observation_id or result["source_run_dir"] != str(Path(run_dir).resolve()):
                raise ValueError("policy_response_observation_mismatch")
            return result
        except BaseException:
            # A late response must never be consumed as the next observation's action.
            self.close()
            raise

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.process is not None:
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()
        self.log.close()
