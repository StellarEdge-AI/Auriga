"""Portable RoboDojo trajectories: validated data only, never archive-provided code."""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

from ...core import legacy_names as legacy
from ...core.contracts import canonical, finite_vector
from ...core.observation_policy import ACTOR_PROFILES

FORMAT = "auriga.robodojo_trajectory.v1"
MEMBERS = {"manifest.json", "commands.jsonl", "measured_states.jsonl"}
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
VERSION_HASHES = ("adapter_source_sha256", "robodojo_local_diff_sha256", "isaaclab_local_diff_sha256",
                  "robot_config_sha256", "curobo_config_sha256")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_bundle_json_key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("invalid_bundle_" + name)
    return value


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid_bundle_" + name)
    return value


def _text(value, name, limit=4096):
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ValueError("invalid_bundle_" + name)
    return value


def _versions(value):
    result = {key: value[key] for key in (*VERSION_HASHES, "robodojo_commit", "python", "submodules")}
    for key in VERSION_HASHES:
        if not isinstance(result[key], str) or not re.fullmatch(r"[0-9a-f]{64}", result[key]):
            raise ValueError("invalid_bundle_runtime_version:" + key)
    if not re.fullmatch(r"[0-9a-f]{40}", _text(result["robodojo_commit"], "robodojo_commit")):
        raise ValueError("invalid_bundle_robodojo_commit")
    for key in ("python", "submodules"):
        _text(result[key], key)
    result["packages"] = {key: _text(value["packages"][key], "package_version", 128)
                          for key in ("isaaclab", "isaacsim", "torch")}
    return result


def _evaluation(value):
    task = _text(value["task"], "task", 128)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", task):
        raise ValueError("invalid_bundle_task")
    result = dict(task=task, versions=_versions(value["versions"]))
    for key in ("seed", "layout_id", "sim_step", "evaluator_actions"):
        result[key] = _integer(value[key], key)
    for key in ("task_success", "valid_episode"):
        if type(value[key]) is not bool:
            raise ValueError("invalid_bundle_" + key)
        result[key] = value[key]
    result["normalized_score"] = _number(value["normalized_score"], "normalized_score")
    if not 0 <= result["normalized_score"] <= 1:
        raise ValueError("invalid_bundle_normalized_score")
    for key in ("native_reward", "native_process_score"):
        if key in value:
            result[key] = None if value[key] is None else _number(value[key], key)
    if "episode_terminal" in value:
        terminal = value["episode_terminal"]
        if (type(terminal["ended"]) is not bool
                or (terminal["success"] is not None and type(terminal["success"]) is not bool)
                or terminal["reason"] not in (None, "native_success", "native_failure", "native_step_limit")):
            raise ValueError("invalid_bundle_episode_terminal")
        result["episode_terminal"] = {key: terminal[key] for key in ("ended", "success", "reason")}
        result["episode_terminal"].update({key: _integer(terminal[key], key)
                                         for key in ("native_action_count", "native_step_limit")})
        if terminal["native_action_count"] != result["evaluator_actions"]:
            raise ValueError("bundle_terminal_action_count_mismatch")
    return result


def _state(value, step):
    if "sim_step" in value and value["sim_step"] != step:
        raise ValueError("bundle_measurement_tick_mismatch")
    result = dict(sim_step=step, arms={})
    for side in ("left", "right"):
        arm = value["arms"][side]
        cleaned = {key: arm[key] for key in ("q_rad", "qdot_rad_s")}
        for key in cleaned:
            finite_vector(cleaned[key], 6, "bundle_" + key)
        cleaned["ee_pose"] = {key: arm["ee_pose"][key] for key in ("position_m", "quaternion_xyzw")}
        finite_vector(cleaned["ee_pose"]["position_m"], 3, "bundle_ee_position")
        finite_vector(cleaned["ee_pose"]["quaternion_xyzw"], 4, "bundle_ee_quaternion")
        if not math.isclose(math.hypot(*cleaned["ee_pose"]["quaternion_xyzw"]), 1., abs_tol=1e-3):
            raise ValueError("invalid_bundle_ee_quaternion_norm")
        gripper = arm["gripper"]
        finite_vector(gripper["position_m"], 2, "bundle_gripper_position")
        goal = _number(gripper["goal_normalized"], "gripper_goal")
        if not 0 <= goal <= 1:
            raise ValueError("invalid_bundle_gripper_goal")
        cleaned["gripper"] = dict(position_m=gripper["position_m"], goal_normalized=goal)
        result["arms"][side] = cleaned
    return result


def _validated(manifest, samples, measurements):
    from .hd_replay import state_error, validate_samples

    if manifest["format"] not in (FORMAT, legacy.TRAJECTORY_FORMAT):
        raise ValueError("unsupported_trajectory_bundle_format")
    run_id = _text(manifest["source_run_id"], "source_run_id", 128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
        raise ValueError("invalid_bundle_source_run_id")
    evaluation = _evaluation(manifest["evaluation"])
    dt = _number(manifest["physics_dt_s"], "physics_dt")
    ticks = _integer(manifest["ticks_per_action"], "ticks_per_action", 1)
    if dt <= 0 or manifest["observation_profile"] not in ACTOR_PROFILES:
        raise ValueError("invalid_bundle_clock_or_profile")
    cleaned_samples = []
    for row in samples:
        commands = {}
        for name, command in row["commands"].items():
            if not re.fullmatch(r"(?:(?:left|right)_(?:arm|ee)|support_(?:arm|ee)[0-9]+)_joint_state", name):
                raise ValueError("unsupported_bundle_actuator")
            position = command["position"]
            if not isinstance(position, list) or not 1 <= len(position) <= 32:
                raise ValueError("invalid_bundle_actuator_dimensions")
            finite_vector(position, len(position), "bundle_actuator_position")
            velocity = command.get("velocity", 0)
            if isinstance(velocity, list):
                finite_vector(velocity, len(position), "bundle_actuator_velocity")
            else:
                _number(velocity, "actuator_velocity")
            commands[name] = dict(position=position, velocity=velocity)
        cleaned_samples.append(dict(sim_step=_integer(row["sim_step"], "sim_step", 1), commands=commands))
    validate_samples(cleaned_samples, evaluation["sim_step"], ticks)
    if evaluation["evaluator_actions"] * ticks != len(cleaned_samples):
        raise ValueError("bundle_native_action_count_mismatch")
    states = {}
    for row in measurements:
        step = _integer(row["sim_step"], "measurement_tick")
        if step in states or step > len(cleaned_samples):
            raise ValueError("duplicate_or_out_of_range_bundle_measurement")
        states[step] = _state(row["state"], step)
        state_error(states[step], states[step])
    if not set(range(0, len(cleaned_samples) + 1, ticks)).issubset(states):
        raise ValueError("missing_bundle_native_boundary_measurements")
    return evaluation, cleaned_samples, states


def export_bundle(source, target):
    """Publish a complete bundle without exporting model history or host settings."""
    from .hd_replay import load_archive

    source, target = Path(source).resolve(), Path(target).absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    if target.resolve().is_relative_to(source):
        raise ValueError("bundle_output_must_be_outside_source")
    try:
        archive = load_archive(source)
        manifest = dict(format=FORMAT, source_run_id=json.loads((source / "evaluation.json").read_text())["run_id"],
            evaluation=archive["evaluation"], physics_dt_s=archive["physics_dt_s"],
            ticks_per_action=archive["ticks_per_action"], observation_profile=archive["config"]["features"]["observation_profile"])
        evaluation, samples, states = _validated(manifest, archive["samples"],
            [dict(sim_step=step, state=state) for step, state in archive["states"].items()])
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("incomplete_trajectory_bundle_evidence") from error
    manifest["evaluation"] = evaluation
    payloads = {"commands.jsonl": b"".join((canonical(row) + "\n").encode() for row in samples),
        "measured_states.jsonl": b"".join((canonical(dict(sim_step=step, state=states[step])) + "\n").encode()
                                           for step in sorted(states))}
    manifest["members"] = {name: dict(byte_size=len(data), sha256=_sha(data)) for name, data in payloads.items()}
    payloads["manifest.json"] = canonical(manifest).encode()
    if (sum(map(len, payloads.values())) > MAX_UNCOMPRESSED_BYTES
            or len(payloads["manifest.json"]) > MAX_MANIFEST_BYTES):
        raise ValueError("trajectory_bundle_too_large")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".trajectory-", suffix=".zip", dir=target.parent, delete=False) as file:
            temporary = Path(file.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
            for name, data in payloads.items():
                zipped.writestr(name, data)
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return dict(path=str(target), byte_size=target.stat().st_size, sha256=_sha(target.read_bytes()),
                format=FORMAT, source_run_id=manifest["source_run_id"])


def read_bundle(path):
    """Read only fixed ZIP members; no extraction or archive-provided executable paths."""
    from ...core.config import Config

    path = Path(path).resolve()
    if path.stat().st_size > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("trajectory_bundle_too_large")
    try:
        with zipfile.ZipFile(path) as zipped:
            members = zipped.infolist()
            if len(members) != len(MEMBERS) or {member.filename for member in members} != MEMBERS:
                raise ValueError("invalid_trajectory_bundle_members")
            if any(stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1 for member in members):
                raise ValueError("unsupported_trajectory_bundle_member_type")
            if (sum(member.file_size for member in members) > MAX_UNCOMPRESSED_BYTES
                    or zipped.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES):
                raise ValueError("trajectory_bundle_too_large")
            manifest = _json(zipped.read("manifest.json"))
            payloads = {}
            for name in MEMBERS - {"manifest.json"}:
                data = zipped.read(name)
                expected = manifest["members"][name]
                if len(data) != expected["byte_size"] or _sha(data) != expected["sha256"]:
                    raise ValueError("trajectory_bundle_member_hash_mismatch")
                payloads[name] = [_json(line) for line in data.splitlines() if line.strip()]
        evaluation, samples, states = _validated(manifest, payloads["commands.jsonl"], payloads["measured_states.jsonl"])
    except (KeyError, TypeError, OverflowError, zipfile.BadZipFile, NotImplementedError) as error:
        raise ValueError("invalid_trajectory_bundle") from error
    local = Config.load()
    return dict(source=str(path), bundle=True, source_file_sha256=_sha(path.read_bytes()),
        source_run_id=manifest["source_run_id"], evaluation=evaluation, samples=samples, states=states,
        config=dict(features=dict(observation_profile=manifest["observation_profile"]),
                    backend=local.backend.to_dict(), images=local.images.to_dict()),
        capabilities=dict(native_physics_steps_per_action=manifest["ticks_per_action"],
                          physics_dt_s=manifest["physics_dt_s"], observation_profile=manifest["observation_profile"]),
        observations=[], source_hashes={}, physics_dt_s=manifest["physics_dt_s"], ticks_per_action=manifest["ticks_per_action"])
