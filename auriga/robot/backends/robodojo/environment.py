"""RoboDojo application, task setup and evaluator state; no robot dispatch.

Imports of Isaac and the external checkout remain lazy. These helpers describe
RoboDojo simulation only, not a real-hardware clock or stopping guarantee.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path

from auriga.robot.model.embodiments import get_robodojo_robot
from auriga.core.observation_policy import ACTOR_PROFILE, actor_policy, allows_depth

@contextmanager
def host_articulation_properties(articulation_type):
    """Keep Isaac's NumPy DOF metadata getter compatible with CUDA views."""
    from unittest.mock import patch
    from auriga.robot.backends.robodojo.native_evaluator import host_array

    native_property = articulation_type.dof_properties
    def read_properties(instance):
        view = instance._articulation_view
        with ExitStack() as restore:
            for name in ("get_dof_types", "get_dof_limits", "get_drive_types", "get_max_efforts",
                         "get_joint_max_velocities", "get_gains"):
                native = getattr(view, name)
                def read_host(*args, _native=native, **kwargs):
                    value = _native(*args, **kwargs)
                    return tuple(host_array(item) for item in value) if isinstance(value, tuple) else host_array(value)
                restore.enter_context(patch.object(view, name, read_host))
            return native_property.fget(instance)
    # Only scene-object reset reads use the shim; robot control stays native.
    with patch.object(articulation_type, "dof_properties", property(read_properties)):
        yield


def configure_depth_capture(config, observation_profile=ACTOR_PROFILE):
    """Apply the actor sensor boundary after every native task/config override."""
    from omegaconf import OmegaConf, open_dict

    actor_policy(observation_profile)
    enabled = allows_depth(observation_profile)
    with open_dict(config):
        config.eval_cfg.observation.vision.depth = enabled
        config.eval_cfg.observation.vision.approximate_depth = False
        annotators = OmegaConf.to_container(config.camera.get("annotator", {}), resolve=True) or {}
        for capture in annotators.values():
            if isinstance(capture, dict):
                for name in list(capture):
                    item = capture[name]
                    if isinstance(item, dict) and item.get("type") in ("depth", "distance_to_image_plane", "distance_to_camera"):
                        del capture[name]
        if enabled:
            # Native robot setup adds wrist camera sections later. Their explicit
            # annotator settings override common, so configure both those entries
            # and the common fallback used by other cameras added during setup.
            names = set(annotators) | {"common"} | {name for name, camera in config.camera.items()
                                                   if OmegaConf.is_dict(camera) and "camera" in camera}
            for name in names:
                capture = annotators.setdefault(name, deepcopy(annotators.get("common", {})))
                capture.setdefault("enabled", True)
                capture["distance_to_image_plane_capture"] = {"type": "distance_to_image_plane", "device": "cpu"}
        config.camera.annotator = annotators
    return config


def native_eval_config(root, task, seed, layout_id, observation_profile=ACTOR_PROFILE, *, robot_binding=None):
    """Apply native per-task setup without changing this worker's selected case."""
    from omegaconf import OmegaConf
    from utils.load_file import load_yaml
    from utils.pipeline_utils import process_config, process_randomization

    root = Path(root)
    robot_binding = robot_binding or get_robodojo_robot()
    config = load_yaml(root / robot_binding.environment_config_path)
    sections = {section: load_yaml(root / "env_cfg" / section / f"{name}.yml")
                for section, name in config["config"].items()}
    # Keep native device/Fabric/physics defaults; the CUDA scene path omits fluids.
    sections["sim"]["seed"] = [layout_id]
    sections["sim"]["scene"]["num_envs"] = 1
    sections["task_env"] = load_yaml(root / "task/RoboDojo/config" / f"{task}.yml")
    sections["eval_cfg"] = {**config, "task_name": task, "num_envs": 1,
        "eval_num": 1, "seed": seed, "policy_name": "Auriga",
        "description": {}, "additional_info": "native-harness"}
    sections["deploy_cfg"] = {"port": 0, "policy_name": "Auriga"}
    # Match eval_client.main: randomization precedes task overrides (including
    # support robots and teleop PhysX stabilization), then camera frequency.
    config = process_randomization(OmegaConf.create(sections))
    config, _native_eval_count = process_config(config, task_name=task)
    config.camera.default_frequency = config.eval_cfg.observation.collect_freq
    return configure_depth_capture(config, observation_profile)


def launch_application(gpu):
    from isaaclab.app import AppLauncher
    from env.camera_manager.capture.render_sync import add_zero_delay_kit_args

    # The IPC close acknowledgement must be sent after SimulationApp.close
    # returns. Isaac's default fast_shutdown=True exits the process inside
    # close, which makes that acknowledgement impossible.
    launcher_arguments = argparse.Namespace(headless=True, enable_cameras=True, fast_shutdown=False, device="cuda:0",
        device_id=gpu, kit_args="--enable isaacsim.replicator.behavior --enable isaacsim.sensors.camera")
    add_zero_delay_kit_args(launcher_arguments)
    return AppLauncher(launcher_arguments).app


def create_environment(arguments, app, directory, config, robot_binding):
    """Create and reset one native task; policy inference remains Harness-owned."""
    from env.global_configs import ASSETS_PATH
    from src.eval_client import eval_env
    from unittest.mock import patch
    from auriga.robot.backends.robodojo.native_evaluator import HostPoseView
    from env.scene_manager.objects.articulation import ArticulationObject

    class HarnessPolicyReset:
        def __init__(self, **kwargs): pass
        def call(self, *, func_name, **kwargs):
            if func_name != "reset":
                raise ValueError("policy_inference_is_owned_by_Harness")

    # The native environment owns reset, interpolation, scoring and limits.
    # Only its external websocket policy reset is replaced by the local owner.
    with patch.object(eval_env, "WsModelClient", HarnessPolicyReset):
        env = eval_env.create_eval_env(config, app)
    env.save_dir = str(directory / "native-eval")
    env._stream_dir = str(Path(env.save_dir) / "_stream")
    layout_acceptance = {}
    manager = env.scene_manager.layout_manager
    selected = Path(env.seed_manager.seed_info[arguments.layout_id]["scene_layout"])
    expected = Path(ASSETS_PATH) / robot_binding.layout_directory / str(arguments.seed) / f"{arguments.task}_{arguments.layout_id}.json"
    if selected.resolve() != expected.resolve():
        raise ValueError("native_seed_manager_layout_identity_mismatch")
    stability = manager.check_layout_stability
    def capture_stability(*args, **kwargs):
        result = stability(*args, **kwargs)
        layout_acceptance.update({"valid": bool(manager.layout_valid[0]), "stable": bool(result[0]),
            "unstable_envs": list(result[1]), "authority": "official EvalEnv.reset/setup_scene stability check"})
        return result
    manager.check_layout_stability = capture_stability
    parser = env.reward_manager.func_parser
    init_state = parser.init_state
    def host_init_state():
        parser.layout_manager = HostPoseView(manager)
        return init_state()
    parser.init_state = host_init_state
    with host_articulation_properties(ArticulationObject):
        env.reset(seed=[arguments.layout_id])
    return env, layout_acceptance


def episode_terminal(env):
    ended = bool(env.end_flag[0])
    success = bool(env.success[0]) if ended else None
    reason = ("native_success" if success else "native_step_limit" if env.take_action_cnt[0] >= env.step_lim
              else "native_failure") if ended else None
    return {"ended": ended, "success": success, "reason": reason,
            "native_action_count": int(env.take_action_cnt[0]), "native_step_limit": int(env.step_lim)}


def native_validity(env):
    """Snapshot native admission flags; never advance reward evaluation."""
    layout_valid = bool(env.scene_manager.layout_manager.layout_valid[0])
    unstable_envs = sorted(int(index) for index in env.unstable_envs)
    unstable = 0 in unstable_envs
    return {"env_index": 0, "layout_valid": layout_valid, "unstable": unstable,
            "unstable_envs": unstable_envs, "valid": layout_valid and not unstable,
            "authority": "live EvalEnv.unstable_envs and LayoutManager.layout_valid"}
