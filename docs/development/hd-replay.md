# High-Definition Offline Action Replay

`auriga.apps.replay.hd_replay` reads the exact joint and gripper commands issued during a completed run from its archive, re-executing them under the original task, seed, layout, and native simulation clock to record high-definition video without invoking any model. This is **action resimulation**: because archives do not store full simulator physics state at every tick, frame-by-frame object motion is not guaranteed to be identical to the original rollout.

## Replaying a Trajectory Bundle Downloaded from the Web UI

Select a completed experiment in the Experiment Library and click **Download Raw Trajectory** to obtain a `.zip` bundle. On a machine with Auriga and RoboDojo installed, pass the `.zip` file directly to `hd_replay`—no manual extraction or access to the original run directory is needed:

```sh
python -m auriga.apps.replay.hd_replay \
  --source-run ~/Downloads/exp-example-trajectory.zip \
  --output ~/hd-replays/example-1920 \
  --width 1920 --height 1440 --fps 25
```

Rendering requires a matching RoboDojo / Isaac Sim environment, task assets, a GPU, `ffmpeg`, and `ffprobe`. Archives record the adapter source and native dependency versions; for example, version `0.0.2` updated the worker, so older archives may be rejected from execution with `runtime_versions_differ_from_archive` unless replayed against a matching checkout and environment. Trajectory `.zip` downloads and `--inspect` are not subject to this execution admission check; never relax version checks to force cross-version replay. By default, `.zip` replay uses the runtime paths from your local Auriga configuration, which you can override explicitly if your local installation paths differ:

```sh
python -m auriga.apps.replay.hd_replay \
  --source-run ~/Downloads/exp-example-trajectory.zip \
  --output ~/hd-replays/example-1280 \
  --width 1280 --height 960 \
  --robodojo-root /path/to/RoboDojo \
  --robodojo-python /path/to/robodojo-env/bin/python
```

The trajectory bundle uses format `auriga.robodojo_trajectory.v1` and contains only three files—`manifest.json`, `commands.jsonl`, and `measured_states.jsonl`—recording the task, seed/layout, software versions and clocks, original evaluation score, per-physics-tick actuator commands, and measured robot states at native action boundaries. It never includes model transcripts, credentials, server installation paths, executable code, videos, or simulation assets. Upon import, `hd_replay` verifies archive members, file sizes, SHA-256 hashes, command continuity, and schema validity; an archive cannot specify which Python interpreter or program to execute.

The web UI only exposes the download link for completed runs that contain raw trajectory files, and strictly validates trajectory completeness when clicked, rejecting incomplete action traces. Legacy imported run copies do not currently support this endpoint; use the original run directory containing full evidence instead. Downloading a bundle neither allocates a GPU nor calls a model, and never mutates the original benchmark archive.

## Replaying from a Local Run Directory and CLI Options

The same tool also accepts an original run directory directly. Run it from the repository root inside the `Auriga` Python environment; directory inputs default to the RoboDojo root and Python interpreter recorded in the source run's `effective_config.json`, while still accepting the local path override flags shown above.

You can run a read-only inspection first without allocating a GPU or creating an output directory:

```sh
python -m auriga.apps.replay.hd_replay \
  --source-run /path/to/original-case \
  --inspect
```

`--inspect` verifies archive references, command coverage, and native action boundary measurements, printing the tick count, simulated duration, original evaluation score, and provenance hashes. Sealed directories containing a WAL file are read via a private copy through `open_read_store`, ensuring that archive loading, trajectory `.zip` export, and comparison image extraction never create `.shm` files in the source directory. Because `--inspect` does not launch the simulator, it does not verify whether the current GPU environment can reconstruct the scene.

To render three-camera video at the default **1920x1440, 25 fps** resolution:

```sh
python -m auriga.apps.replay.hd_replay \
  --source-run /path/to/original-case \
  --output /path/to/hd-replays/new-case
```

`--output` must point to a non-existent directory that neither contains nor is contained by the source run directory. The source run is opened read-only; all newly rendered videos, evaluation scores, and reports are written to the separate output directory without modifying the original benchmark results.

When `--gpu` is omitted, `hd_replay` uses Auriga's shared GPU reservation mechanism to select a GPU with `0%` utilization and sufficient free memory, exiting immediately rather than preempting active workloads if no device qualifies. You can restrict selection to a specific device with `--gpu 4`, which must still pass the admission check.

To render alongside active workloads on the same GPU, pass `--allow-busy-gpu`. This mode still checks and reserves unallocated GPU memory (reserving `14336` MiB per job with `2048` MiB headroom, compared to `32768` + `4096` MiB in idle-GPU mode) while allowing GPUs with non-zero utilization; `--gpu` can still be combined to pin a device. The shared-GPU memory threshold is based on a `12631` MiB peak observed during a 1920x1440, 50 fps replay—treat this as an empirical baseline rather than a hard upper bound across all task assets. If insufficient unreserved memory is available, the process exits so a batch wrapper script can retry later.

For smaller video files, choose any positive even width and height that preserve the native camera `4:3` aspect ratio:

```sh
python -m auriga.apps.replay.hd_replay \
  --source-run /path/to/original-case \
  --output /path/to/hd-replays/new-case-1280 \
  --width 1280 --height 960 --fps 25
```

Both `--width` and `--height` must be positive even integers maintaining a `4:3` ratio, and `--fps` must evenly divide the native physics frequency. Video frames are sampled uniformly in **simulation time**, excluding environment initialization and model thinking wall-clock time. Because standard run recordings play back sparse observations and extra observation boundaries at 5 fps, playback durations between the original recording and the HD replay may differ; align comparisons by `sim_step`.

Execution requires all version fields recorded in the source run to match the current runtime—including the RoboDojo revision, local patch hashes, related submodules, robot configuration, and adapter source—as well as matching physics `dt` and tick counts per native action. Any version mismatch halts replay immediately rather than producing unverified output.

Command gaps, duplicate ticks, trajectories spanning multiple environment generations, mid-action native faults, or missing boundary measurements cause replay to be rejected. Completed action prefixes from a run that ultimately failed the task are still valid to replay—task failure itself is not a rejection criterion. If the resimulated episode reaches a native terminal state earlier than the source run, `hd_replay` preserves the collected evidence and marks the report as `incomplete`.

| Output Artifact | Purpose |
| --- | --- |
| `cam_head.mp4`, `cam_left_wrist.mp4`, `cam_right_wrist.mp4` | H.264 HD videos; upon completion, `ffprobe` verifies resolution, codec, and frame count. |
| `report.json` | Execution status, version comparison, provenance hashes, command coverage, original vs. replay evaluation scores, robot trajectory tracking errors, and video verification results. |
| `frames.json` | Mapping between video frame indices, video timestamps, and native `sim_step` / simulation time (not a directory of individual frame images). |
| `trajectory.jsonl` | Measured robot state at each native action boundary alongside tracking errors relative to the original trajectory. |
| `comparisons/`, `comparisons.json` | For directory inputs, selects up to 12 timestamps from source observations and renders side-by-side three-camera comparisons (with the right-hand HD frame downscaled to the original resolution), indexing sources and pixel differences. Because `.zip` bundles exclude original RGB images, this directory is empty for `.zip` inputs and `report.json` sets `original_image_comparison_available=false`. |
| `worker.log`, `*.ffmpeg.log`, `process.json` | Simulator, encoder, and subprocess exit diagnostics; `admission.json` records GPU admission details. |

When validating an HD replay, first check `complete`, `source_unchanged`, `robot_trajectory_within_tolerance`, and `evaluation_matches` in `report.json`, then visually inspect object positions, grasps, releases, and final states in the side-by-side comparison images. Neither `status=completed` nor exit code `0` alone proves faithful scene reproduction, and low robot joint tracking error, matching evaluation scores, or pixel difference metrics are not a substitute for verifying physical object motion.

The output report explicitly sets `data_origin=archived_action_resimulation` and `exact_scene_replay=false`. The original benchmark evaluation score remains authoritative, while the resimulated score indicates whether the replay diverged; always judge replay fidelity from the generated report and visual comparisons.
