"""Offline HD rendering of archived RoboDojo actuator commands (no model calls)."""

import argparse
from contextlib import ExitStack, suppress
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from ..library.experiments import atomic_json
from ...core.storage.store import open_read_store
from ...robot.backends.robodojo.native_replay import install_recorded_controls

REPLAY_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SHARED_REPLAY_MEMORY_MIB = 14336
SHARED_REPLAY_HEADROOM_MIB = 2048


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def measured(state):
    """Command goals are not measurements and must not affect replay error."""
    return {side: {key: arm[key] for key in ("q_rad", "qdot_rad_s", "ee_pose")}
            | {"gripper_m": arm["gripper"]["position_m"]}
            for side, arm in state["arms"].items()}


def state_error(reference, actual):
    errors = dict(joint_rad=0., joint_velocity_rad_s=0., ee_position_m=0., gripper_m=0.)
    for side, original in measured(reference).items():
        current = measured(actual)[side]
        for arm in (original, current):
            for values in (arm["q_rad"], arm["qdot_rad_s"], arm["gripper_m"],
                           arm["ee_pose"]["position_m"], arm["ee_pose"]["quaternion_xyzw"]):
                if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
                    raise ValueError("nonfinite_replay_measurement")
        for key, field in (("joint_rad", "q_rad"), ("joint_velocity_rad_s", "qdot_rad_s"),
                           ("gripper_m", "gripper_m")):
            if len(original[field]) != len(current[field]):
                raise ValueError("measurement_dimensions_changed")
            errors[key] = max(errors[key], *(abs(a - b) for a, b in zip(original[field], current[field])))
        errors["ee_position_m"] = max(errors["ee_position_m"], math.dist(
            original["ee_pose"]["position_m"], current["ee_pose"]["position_m"]))
    if not all(math.isfinite(value) for value in errors.values()):
        raise ValueError("nonfinite_replay_measurement")
    return errors


def validate_samples(samples, expected_ticks, ticks_per_action):
    if (type(expected_ticks) is not int or expected_ticks <= 0
            or type(ticks_per_action) is not int or ticks_per_action <= 0
            or expected_ticks % ticks_per_action):
        raise ValueError("replay_requires_complete_native_actions")
    samples.sort(key=lambda item: item["sim_step"])
    if len(samples) != expected_ticks or any(row["sim_step"] != step for step, row in enumerate(samples, 1)):
        raise ValueError("dispatched_trace_has_gaps_duplicates_or_wrong_coverage")
    for row in samples:
        commands = row["commands"]
        for side in ("left", "right"):
            for suffix, size in (("arm_joint_state", 6), ("ee_joint_state", 2)):
                if len(commands[f"{side}_{suffix}"]["position"]) != size:
                    raise ValueError("unsupported_actuator_dimensions")
        for command in commands.values():
            if set(command) - {"position", "velocity"} or "position" not in command:
                raise ValueError("unsupported_actuator_command")
            for value in command.values():
                for number in value if isinstance(value, list) else [value]:
                    if type(number) not in (float, int) or not math.isfinite(number):
                        raise ValueError("nonfinite_actuator_command")
    return samples


def load_archive(source):
    source = Path(source).resolve()
    if source.is_file():
        from .trajectory_bundle import read_bundle
        return read_bundle(source)
    evaluation = json.loads((source / "evaluation.json").read_text())
    result = evaluation["result"]
    config = json.loads((source / "effective_config.json").read_text())
    hashes = {name: sha256(source / name) for name in ("evaluation.json", "effective_config.json", "events.sqlite")}
    samples, states = [], {}
    with open_read_store(source) as store:
        def artifact(reference):
            meta, data = store.resolve_artifact(reference, ("actor", "internal", "observer"))
            hashes["blobs/" + meta["storage_key"]] = meta["sha256"]
            return json.loads(data)

        capabilities = store.read_state("runtime", "capabilities")
        ticks = capabilities["native_physics_steps_per_action"]
        dt = capabilities["physics_dt_s"]
        if not math.isfinite(dt) or dt <= 0:
            raise ValueError("invalid_archived_physics_dt")
        observations = store.records("observation")
        for observation in observations:
            if any(observation[key] != evaluation[key] for key in
                   ("run_id", "episode_id", "env_instance_id", "env_generation")):
                raise ValueError("multiple_physical_generations_not_supported")
        receipts = store.records("action_receipt")
        for receipt in receipts:
            if receipt.get("executed_native_steps", 0) != receipt.get("completed_native_steps", 0):
                raise ValueError("partially_faulted_native_action_not_replayable")
            if receipt.get("dispatched_trace_ref"):
                trace = artifact(receipt["dispatched_trace_ref"])
                if trace and trace.get("scope") != "official interpolated actuator commands":
                    raise ValueError("unsupported_dispatch_trace_scope")
                samples.extend((trace or {}).get("samples", []))
            if receipt.get("actual_trace_ref"):
                for item in artifact(receipt["actual_trace_ref"]) or []:
                    step, state = item["sim_step"], item["state"]
                    if step in states and max(state_error(states[step], state).values()) > 1e-6:
                        raise ValueError("conflicting_measurements_at_same_tick")
                    states[step] = state
        for observation in observations:
            if observation["sim_step"] == 0 and 0 not in states:
                states[0] = artifact(observation["robot_state_ref"])
        validate_samples(samples, result["sim_step"], ticks)
        if result["evaluator_actions"] * ticks != len(samples):
            raise ValueError("native_action_and_trace_counts_disagree")
        if not set(range(0, len(samples) + 1, ticks)).issubset(states):
            raise ValueError("missing_native_boundary_measurements")
    return dict(source=str(source), evaluation=result, config=config, capabilities=capabilities,
                samples=samples, states=states, observations=observations, source_hashes=hashes,
                physics_dt_s=dt, ticks_per_action=ticks)


def archive_summary(archive):
    return {key: archive[key] for key in ("source", "evaluation", "source_hashes", "physics_dt_s", "ticks_per_action")} | dict(
        control_ticks=len(archive["samples"]), native_actions=len(archive["samples"]) // archive["ticks_per_action"],
        simulation_duration_s=len(archive["samples"]) * archive["physics_dt_s"],
        measured_states=len(archive["states"]), model_calls=0,
        source_run_id=archive.get("source_run_id"), source_file_sha256=archive.get("source_file_sha256"),
        original_image_comparison_available=bool(archive["observations"]))


def run_worker(args, archive):
    import numpy as np
    from PIL import Image, ImageDraw
    from unittest.mock import patch
    from ...robot.backends.robodojo import robot_worker

    output = Path(args.output)
    report = archive_summary(archive) | dict(status="initializing", data_origin="archived_action_resimulation",
        replay_source_sha256=REPLAY_SOURCE_SHA256,
        exact_scene_replay=False, width=args.width, height=args.height, fps=args.fps,
        limitation="No archived complete scene states; matching robot traces and scores do not prove identical object motion.")
    atomic_json(output / "report.json", report)
    settings = archive["config"]
    native_config = robot_worker.native_eval_config

    def hd_config(*positional, **keywords):
        config = native_config(*positional, **keywords)
        from env.camera_manager.camera_manager import REAL_MAP
        for template in REAL_MAP.values():
            template["resolution"] = (args.width, args.height)
        return config

    adapter = None
    started = time.monotonic()
    encoders, mapping, errors, comparisons = {}, [], [], []
    try:
        worker_args = argparse.Namespace(robodojo_root=args.robodojo_root, gpu=args.gpu,
            run_dir=str(output / "robot"), task=archive["evaluation"]["task"],
            seed=archive["evaluation"]["seed"], layout_id=archive["evaluation"]["layout_id"],
            observation_profile=settings["features"]["observation_profile"])
        with patch.object(robot_worker, "native_eval_config", hd_config):
            adapter = robot_worker.RoboDojoAdapter(worker_args)
        from env.robot_manager.control_manager import MetaControl
        differences = {key: dict(source=value, replay=adapter.versions.get(key))
                       for key, value in archive["evaluation"]["versions"].items()
                       if adapter.versions.get(key) != value}
        report.update(runtime_versions=adapter.versions, version_differences=differences)
        if differences:
            raise ValueError("runtime_versions_differ_from_archive: " + ", ".join(differences))
        if (adapter.native_step_ticks != archive["ticks_per_action"]
                or not math.isclose(adapter.env.dt, archive["physics_dt_s"], abs_tol=1e-12)):
            raise ValueError("native_clock_does_not_match_archive")
        interval = round(1 / (args.fps * adapter.env.dt))
        if interval < 1 or not math.isclose(interval * args.fps * adapter.env.dt, 1., abs_tol=1e-8):
            raise ValueError("fps_must_divide_physics_frequency")
        source_by_step = {obs["sim_step"]: obs for obs in sorted(archive["observations"],
                          key=lambda obs: (obs["sim_step"], obs["observation_id"]))}
        steps = sorted(source_by_step)
        comparison_steps = {steps[round(i * (len(steps) - 1) / 11)] for i in range(12)} if steps else set()
        group = install_recorded_controls(adapter, MetaControl)
        original_step = adapter.env.step
        (output / "comparisons").mkdir()
        cameras = adapter.env.camera_manager.camera_names[0]
        with ExitStack() as stack:
            store = (stack.enter_context(open_read_store(archive["source"]))
                if comparison_steps else None)
            states_file = stack.enter_context((output / "trajectory.jsonl").open("w"))
            for name in cameras:
                log = stack.enter_context((output / (name + ".ffmpeg.log")).open("wb"))
                command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
                    "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{args.width}x{args.height}",
                    "-framerate", str(args.fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
                    "-preset", "fast", "-crf", "18", "-threads", "2", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(output / (name + ".mp4"))]
                encoders[name] = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)

            def capture(encode):
                step = adapter.control_step
                clock = float(adapter.env.sim.sim.current_time)
                adapter.obs.render_for_capture()
                raw = adapter.obs.get_obs([0])[0]["vision"]
                if clock != float(adapter.env.sim.sim.current_time):
                    raise RuntimeError("render_advanced_physics")
                for name in cameras:
                    rgb = np.asarray(raw[name]["color"], dtype=np.uint8)
                    if rgb.shape != (args.height, args.width, 3):
                        raise ValueError("unexpected_render_dimensions")
                    if encode:
                        encoders[name].stdin.write(rgb.tobytes())
                    if step in comparison_steps:
                        source_camera = next(cam for cam in source_by_step[step]["cameras"] if cam["camera_id"] == name)
                        meta, data = store.resolve_image(source_camera["rgb_ref"], ("actor", "observer"))
                        original = Image.open(io.BytesIO(data)).convert("RGB")
                        small = Image.fromarray(rgb).resize(original.size, Image.Resampling.LANCZOS)
                        width, height = original.size
                        sheet = Image.new("RGB", (width * 2, height + 28), "white")
                        sheet.paste(original, (0, 28)); sheet.paste(small, (width, 28))
                        draw = ImageDraw.Draw(sheet)
                        draw.text((8, 7), f"Original | tick {step} | {name}", fill="black")
                        draw.text((width + 8, 7), "HD action replay (downsampled for comparison)", fill="black")
                        path = output / "comparisons" / f"{step:06d}-{name}.jpg"
                        sheet.save(path, quality=95)
                        comparisons.append(dict(sim_step=step, camera_id=name, image=str(path),
                            source_rgb_ref=source_camera["rgb_ref"], source_reconstructed=bool(meta.get("reconstruction")),
                            mean_absolute_pixel_difference=float(np.abs(np.asarray(original, dtype=float) - np.asarray(small, dtype=float)).mean())))
                if encode:
                    mapping.append(dict(video_frame=len(mapping), pts_s=len(mapping) / args.fps,
                        sim_step=step, elapsed_sim_time_s=step * adapter.env.dt, native_sim_time_s=clock))

            def step_and_capture(*positional, **keywords):
                value = original_step(*positional, **keywords)
                step = adapter.control_step
                if step % interval == 0 or step in comparison_steps:
                    capture(step % interval == 0)
                return value

            adapter.env.step = step_and_capture

            def compare_state():
                step, state = adapter.control_step, adapter.state()
                error = state_error(archive["states"][step], state)
                errors.append(dict(sim_step=step, **error))
                states_file.write(json.dumps(dict(sim_step=step, state=state, error=error)) + "\n")

            compare_state()
            if errors[0]["joint_rad"] > .03 or errors[0]["ee_position_m"] > .005:
                raise ValueError("initial_robot_state_does_not_match")
            capture(True)
            report["status"] = "rendering"
            for offset in range(0, len(archive["samples"]), adapter.native_step_ticks):
                group[:] = [row["commands"] for row in archive["samples"][offset:offset + adapter.native_step_ticks]]
                target_state = archive["states"][offset + adapter.native_step_ticks]
                row = [value for side in ("left", "right") for value in
                       [*group[-1][side + "_arm_joint_state"]["position"], target_state["arms"][side]["gripper"]["goal_normalized"]]]
                if not adapter._native_action(row):
                    report["stop_reason"] = "native_terminal_before_source_end"
                    break
                compare_state()
                if adapter.control_step % (adapter.native_step_ticks * 25) == 0:
                    report.update(executed_control_ticks=adapter.control_step, encoded_frames=len(mapping),
                                  elapsed_wall_s=time.monotonic() - started)
                    atomic_json(output / "report.json", report)
                    print(f"REPLAY {adapter.control_step}/{len(archive['samples'])} ticks; {len(mapping)} frames", flush=True)
            if mapping[-1]["sim_step"] != adapter.control_step:
                capture(True)  # Explicit final-frame timestamp; no interpolated physics.
            report.update(executed_control_ticks=adapter.control_step, evaluation_replay=adapter.evaluate(),
                          complete=adapter.control_step == len(archive["samples"]))
            for encoder in encoders.values():
                encoder.stdin.close()
            for name, encoder in encoders.items():
                if encoder.wait(timeout=120) != 0:
                    raise RuntimeError("video_encoder_failed: " + name)
        video_info = []
        for name in cameras:
            path = output / (name + ".mp4")
            probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-count_frames", "-show_entries", "stream=width,height,codec_name,r_frame_rate,nb_read_frames,pix_fmt",
                "-of", "json", str(path)]))["streams"][0]
            if (int(probe["nb_read_frames"]) != len(mapping) or probe["width"] != args.width
                    or probe["height"] != args.height or probe["codec_name"] != "h264"):
                raise RuntimeError("video_verification_failed")
            video_info.append(dict(camera_id=name, path=str(path), sha256=sha256(path), **probe))
        maxima = {key: max(row[key] for row in errors) for key in errors[0] if key != "sim_step"}
        replay_eval = report["evaluation_replay"]
        report.update(status="completed" if report["complete"] else "incomplete", videos=video_info,
            encoded_frames=len(mapping), trajectory_error_max=maxima,
            robot_trajectory_within_tolerance=(maxima["joint_rad"] <= .03 and maxima["ee_position_m"] <= .005 and maxima["gripper_m"] <= .003),
            trajectory_tolerances=dict(joint_rad=.03, ee_position_m=.005, gripper_m=.003),
            evaluation_matches=all(replay_eval[key] == archive["evaluation"][key] for key in ("task_success", "normalized_score", "valid_episode")),
            source_unchanged=(sha256(archive["source"]) == archive["source_file_sha256"] if archive.get("bundle") else
                all(sha256(Path(archive["source"]) / path) == digest for path, digest in archive["source_hashes"].items())),
            elapsed_wall_s=time.monotonic() - started)
    except BaseException as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}",
                      executed_control_ticks=getattr(adapter, "control_step", 0))
        raise
    finally:
        for encoder in encoders.values():
            if encoder.poll() is None:
                if not encoder.stdin.closed:
                    with suppress(OSError):
                        encoder.stdin.close()
                try:
                    encoder.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    encoder.kill(); encoder.wait()
        atomic_json(output / "frames.json", dict(step_domain="adapter_control_step", physics_dt_s=archive["physics_dt_s"], frames=mapping))
        atomic_json(output / "comparisons.json", comparisons)
        atomic_json(output / "report.json", report)
        if adapter is not None:
            adapter.close()
    return 0 if report.get("complete") and report.get("source_unchanged") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True, type=Path, help="Original run directory or downloaded trajectory ZIP")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1440)
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--gpu", type=int, help="Default: select an idle GPU using shared Auriga reservations")
    parser.add_argument("--allow-busy-gpu", action="store_true",
                        help="Admit a GPU with enough unreserved memory even when it is in use")
    parser.add_argument("--inspect", action="store_true", help="Verify the archive without starting a GPU or creating output")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--robodojo-root", help="Local RoboDojo installation (overrides the local/run configuration)")
    parser.add_argument("--robodojo-python", help="Local Python with RoboDojo / Isaac Sim installed")
    args = parser.parse_args()
    if min(args.width, args.height, args.fps) <= 0 or args.width % 2 or args.height % 2:
        parser.error("positive even image dimensions and positive fps required")
    if args.width * 3 != args.height * 4:
        parser.error("retain the source 4:3 aspect ratio, e.g. 1280x960 or 1920x1440")
    archive = load_archive(args.source_run)
    interval = 1 / (args.fps * archive["physics_dt_s"])
    if interval < 1 or not math.isclose(interval, round(interval), abs_tol=1e-8):
        parser.error("--fps must divide the archived physics frequency")
    if args.inspect:
        print(json.dumps(archive_summary(archive), indent=2)); return 0
    if args.output is None:
        parser.error("--output is required for rendering")
    args.source_run, args.output = args.source_run.resolve(), args.output.resolve()
    if args.output.is_relative_to(args.source_run) or args.source_run.is_relative_to(args.output):
        parser.error("source and output must be separate, non-nested directories")
    if args.worker:
        return run_worker(args, archive)
    if args.output.exists():
        parser.error("choose a new output directory")
    backend = archive["config"]["backend"]
    args.robodojo_root = str(Path(args.robodojo_root or backend["root"]).expanduser().resolve())
    python = Path(args.robodojo_python or backend["python"]).expanduser().resolve()
    if not python.is_file() or not (Path(args.robodojo_root) / "env_cfg/arx_x5.yml").is_file():
        parser.error("RoboDojo installation missing; set --robodojo-root and --robodojo-python for this machine")
    if not all(shutil.which(name) for name in ("ffmpeg", "ffprobe")):
        parser.error("ffmpeg and ffprobe are required for video export")
    from ..evaluation.queue import reserve_gpu, sample_gpus
    snapshot = sample_gpus()
    reservation = None
    memory_mib = SHARED_REPLAY_MEMORY_MIB if args.allow_busy_gpu else 32768
    headroom_mib = SHARED_REPLAY_HEADROOM_MIB if args.allow_busy_gpu else 4096
    for gpu in sorted(snapshot["gpus"], key=lambda item: (item["utilization"], -item["free_mib"])):
        if (args.gpu is not None and gpu["index"] != args.gpu) or (
                not args.allow_busy_gpu and gpu["utilization"] != 0):
            continue
        reservation, admission = reserve_gpu(gpu, memory_mib=memory_mib,
                                             headroom_mib=headroom_mib, baseline=snapshot)
        if reservation:
            args.gpu = gpu["index"]; break
    if reservation is None:
        if args.allow_busy_gpu:
            raise RuntimeError("no_eligible_gpu_available; retry when GPU memory is free")
        raise RuntimeError("no_idle_gpu_available; retry when a GPU is free")
    try:
        args.output.mkdir(parents=True)
        atomic_json(args.output / "admission.json", admission)
        project = str(Path(__file__).resolve().parents[3])
        environment = os.environ | dict(CUDA_VISIBLE_DEVICES=str(args.gpu), ROBODOJO_RENDER_GPU=str(args.gpu),
            ROBODOJO_RUN_ID="hd-replay-" + uuid.uuid4().hex,
            ACCEPT_EULA="Y", OMNI_KIT_ACCEPT_EULA="YES", PYTHONDONTWRITEBYTECODE="1",
            AURIGA_IMAGE_SETTINGS=json.dumps(archive["config"]["images"]),
            PYTHONPATH=os.pathsep.join((project, args.robodojo_root, str(Path(args.robodojo_root) / "XPolicyLab"), os.environ.get("PYTHONPATH", ""))))
        command = [str(python), "-u", "-m", "auriga.apps.replay.hd_replay", "--worker",
            "--source-run", str(args.source_run), "--output", str(args.output), "--gpu", str(args.gpu),
            "--robodojo-root", args.robodojo_root, "--width", str(args.width), "--height", str(args.height), "--fps", str(args.fps)]
        with (args.output / "worker.log").open("w") as log:
            child = subprocess.Popen(command, cwd=args.robodojo_root, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                     pass_fds=reservation.filenos())
            reservation.bind(child.pid)
            print(f"GPU {args.gpu}; output {args.output}; worker PID {child.pid}", flush=True)
            try:
                code = child.wait()
            except BaseException:
                child.terminate()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
                raise
        atomic_json(args.output / "process.json", dict(exit_code=code, command=command))
        return code
    finally:
        reservation.close()


if __name__ == "__main__":
    if "--worker" in sys.argv:
        # Kit finalizers can reference unloaded plugins after full native close.
        import traceback
        try:
            exit_code = main()
        except BaseException:
            traceback.print_exc(); exit_code = 1
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(exit_code)
    sys.exit(main())
