"""Probe the already loaded local pi05 service with archived actor observations.

Run in lerobot-inference. Requires exclusive use of the deployed service with the
schema in docs/development/pi05-inference-service.md; never resets or loads it.
The 14+18 state packing is an explicit experiment, not a trained robot adapter.
No returned action has robot semantics or an execution path.
"""

import argparse
from contextlib import closing
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auriga.core.storage.store import Store


CAMERAS = {"cam_head": "base_0_rgb", "cam_left_wrist": "left_wrist_0_rgb",
           "cam_right_wrist": "right_wrist_0_rgb"}


def probe_state(state):
    """Preserve measured joints and command provenance; pad only unused fields."""
    values, fields = [], []
    for side in ("left", "right"):
        arm = state["arms"][side]
        if (arm["source_kind"] != "measurement"
                or arm["joint_names"] != [f"joint{i}" for i in range(1, 7)]
                or len(arm["q_rad"]) != 6):
            raise ValueError("probe_requires_measured_arx_x5_joint_order")
        gripper = arm["gripper"]
        if (gripper["command_source_kind"] != "command"
                or not 0 <= gripper["command_normalized"] <= 1):
            raise ValueError("probe_requires_normalized_gripper_command")
        values.extend(arm["q_rad"] + [gripper["command_normalized"]])
        fields.extend([f"{side}/{name}:measurement_rad" for name in arm["joint_names"]]
                      + [f"{side}/gripper:command_normalized"])
    values.extend([0.0] * 18)
    fields.extend(["padding_zero"] * 18)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) for value in values):
        raise ValueError("probe_state_must_be_finite")
    return values, fields


def load_observation(run_dir, observation_id):
    import numpy as np
    from PIL import Image

    with closing(Store(run_dir, {}, read_only=True)) as store:
        obs = store.read("observation", observation_id)
        if obs.get("data_origin") != "robodojo_runtime" or obs.get("world_kind") != "observed":
            raise ValueError("probe_requires_recorded_robodojo_observation")
        if obs.get("robot_model_id") != "arx-x5" or not obs.get("task_instruction"):
            raise ValueError("probe_requires_arx_x5_and_public_instruction")
        state_info, state_bytes = store.resolve_artifact(obs["robot_state_ref"], ("actor",))
        state = json.loads(state_bytes)
        timing = (obs["sim_step"], obs["step_domain"])
        if None in timing or (state.get("sim_step"), state.get("step_domain")) != timing:
            raise ValueError("probe_state_observation_timing_mismatch")
        values, fields = probe_state(state)
        raw = {f"state_{i}": value for i, value in enumerate(values)}
        raw["task"] = obs["task_instruction"]
        images = []
        for camera_id, policy_name in CAMERAS.items():
            matches = [camera for camera in obs["cameras"] if camera["camera_id"] == camera_id]
            if len(matches) != 1:
                raise ValueError("probe_requires_one_frame_per_camera")
            camera = matches[0]
            if ((camera.get("sim_step"), camera.get("step_domain")) != timing
                    or camera.get("pixel_transform") != "identity"):
                raise ValueError("probe_camera_observation_timing_or_transform_mismatch")
            info, data = store.resolve_artifact(camera["rgb_ref"], ("actor",))
            with Image.open(io.BytesIO(data)) as image:
                if image.mode != "RGB" or image.size != (camera["width"], camera["height"]):
                    raise ValueError("probe_rgb_size_or_mode_mismatch")
                raw[policy_name] = np.array(image, dtype=np.uint8)
            images.append(dict(camera_id=camera_id, policy_key=policy_name,
                               frame_id=camera["frame_id"], rgb_ref=camera["rgb_ref"],
                               source_png_sha256=info["sha256"],
                               raw_rgb_sha256=hashlib.sha256(raw[policy_name].tobytes()).hexdigest(),
                               raw_shape=list(raw[policy_name].shape)))
    evidence = dict(source_run_dir=str(Path(run_dir).resolve()),
                    observation={key: obs[key] for key in ("run_id", "episode_id", "env_instance_id",
                        "env_generation", "observation_id", "sim_step", "step_domain", "data_origin")},
                    public_instruction=raw["task"], robot_state_ref=obs["robot_state_ref"],
                    robot_state_sha256=state_info["sha256"], state_values=values, state_fields=fields,
                    images=images, preprocessing="Native LeRobot bilinear resize to 224x224; no client crop",
                    mapping_status="experimental_untrained_14_values_plus_18_zeros",
                    executable=False, action_semantics="unknown", executed_steps=0)
    return raw, evidence


def validate_actions(actions, timestep, timestamp):
    if len(actions) != 50:
        raise ValueError("probe_action_chunk_length_mismatch")
    values, times = [], []
    for i, action in enumerate(actions):
        value = action.get_action().tolist()
        if action.get_timestep() != timestep + i or (i == 0 and action.get_timestamp() != timestamp):
            raise ValueError("probe_action_observation_identity_mismatch")
        if len(value) != 32 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in value):
            raise ValueError("probe_action_shape_or_finite_check_failed")
        if not math.isfinite(action.get_timestamp()):
            raise ValueError("probe_action_timestamp_not_finite")
        values.append(value)
        times.append(action.get_timestamp())
    return values, times


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    parser.add_argument("--observation-id", required=True)
    parser.add_argument("--output", required=True, help="New JSON evidence file; never overwritten")
    args = parser.parse_args()
    # Reserve the output before spending GPU work; existing evidence stays untouched.
    with Path(args.output).expanduser().open("x") as output:
        try:
            raw, evidence = load_observation(args.run_dir, args.observation_id)
            import grpc
            import pickle
            from lerobot.async_inference.helpers import TimedObservation
            from lerobot.transport import services_pb2, services_pb2_grpc
            from lerobot.transport.utils import send_bytes_in_chunks

            with grpc.insecure_channel("127.0.0.1:50051") as channel:
                grpc.channel_ready_future(channel).result(timeout=10)
                stub = services_pb2_grpc.AsyncInferenceStub(channel)
                observation = TimedObservation(time.time(), time.time_ns(), raw, must_go=True)
                started = time.perf_counter()
                stub.SendObservations(send_bytes_in_chunks(pickle.dumps(observation), services_pb2.Observation), timeout=30)
                response = stub.GetActions(services_pb2.Empty(), timeout=120)
                elapsed = time.perf_counter() - started
                if not response.data:
                    raise ValueError("empty_action_response_check_service_journal")
                # Native upstream protocol uses pickle; only the trusted local service is contacted.
                actions, times = validate_actions(pickle.loads(response.data), observation.timestep, observation.timestamp)
            evidence.update(status="inference_completed", action_shape=[50, 32], actions=actions,
                            action_timestamps=times, rpc_seconds=elapsed,
                            rpc_observation_timestep=observation.timestep, rpc_observation_timestamp=observation.timestamp,
                            endpoint="127.0.0.1:50051", service_setup_performed=False,
                            service_schema="Previously loaded pi05_base with state_0..31 and three named RGB cameras",
                            scope="Inference transport/finite-output probe only; no success, mapping, or latency benchmark")
            json.dump(evidence, output, indent=2, allow_nan=False)
            print(json.dumps({key: evidence[key] for key in ("status", "action_shape", "rpc_seconds", "executable", "executed_steps")}))
        except Exception as error:
            output.seek(0)
            output.truncate()
            json.dump(dict(status="failed", error=str(error), executable=False, executed_steps=0), output)
            raise


if __name__ == "__main__":
    main()
