"""Model-free end-to-end check of analysis.scene_truth_log and scripts/score_geometry_truth.py.

One fresh native RoboDojo environment, no model, no task scoring, no robot motion. The probe is an
ANALYSIS tool: it reads the truth log it asked the worker to write (no model is involved), projects the
true object centres / top-centres into cam_head with geometry_query project_points, and then runs the
same geometry_query calls a model would make (role "actor", archived like any tool result):
pixel_plane at each projected top-centre pixel with the caller plane z = 0.765 (and the VGGT plane when
features.vggt_height is on and the service answers), and a two-view triangulate where both views see the
point. It captures a second observation, closes the worker and runs the scorer on the run directory.

    python scripts/probes/scene_truth_probe.py --config CONFIG --run-dir FRESH_DIR --gpu N [--task T --seed S --layout-id L]

Outputs: the run directory (events.sqlite, private/scene_truth.jsonl, private/probe_overlay_cam_head.jpg),
probe_report.json there, and the scorer's Markdown on stdout.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from auriga.core.config import Config
from auriga.core.contracts import new_id
from auriga.runtime.harness import Harness
from auriga.robot.backends.robodojo.robodojo import RobotBackend
from auriga.robot.backends.robodojo.scene_truth import FILENAME
from auriga.core.storage.store import Store

TABLE_Z = 0.765


def call(harness, request, label):
    response = harness.invoke("geometry_query", {"request": request}, {"tool_call_id": "probe-" + label}, role="actor")
    data = getattr(response, "data", None)
    return data if isinstance(data, dict) else {"status": "error", "response": str(response)[:400]}


def truth_line(root, raw):
    path = root / "private" / FILENAME
    for line in reversed(path.read_text().splitlines()):
        item = json.loads(line)
        if item.get("raw_observation_id") == raw:
            return item
    raise RuntimeError("no truth line for " + raw)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--gpu", type=int, required=True)
    parser.add_argument("--task", default="classify_objects")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--layout-id", type=int, default=0)
    parser.add_argument("--max-objects", type=int, default=6)
    args = parser.parse_args()
    root = Path(args.run_dir).resolve()
    if root.exists() and any(root.iterdir()):
        parser.error("choose a fresh run directory")
    root.mkdir(parents=True, exist_ok=True)
    config = Config.load(args.config, overrides={"analysis": {"scene_truth_log": True},
        "run": {"task": args.task, "seed": args.seed, "layout_id": args.layout_id, "gpu": args.gpu}})
    report = dict(task=args.task, seed=args.seed, layout_id=args.layout_id, gpu=args.gpu, config=args.config,
                  vggt_height=getattr(config.features, "vggt_height", None), calls=[], passed=False)
    identity = dict(run_id=root.name, episode_id=new_id("ep"), env_instance_id=new_id("truth-probe-env"), env_generation=1)
    store = Store(root, identity)
    backend = RobotBackend(config=config, run_dir=root / "private" / "robot")
    harness = None
    try:
        harness = Harness(store, backend, config=config, motion_review=False)
        observation = harness.start()
        obs_id = observation["observation_id"]
        raw = observation["cameras"][0]["frame_id"][:40]
        truth = truth_line(root, raw)
        objects = [o for o in truth["objects"] if o.get("centroid_m") and o.get("top_z_m") is not None][:args.max_objects]
        report.update(observation_id=obs_id, raw_observation_id=raw, sim_step=truth["sim_step"], table=truth.get("table"),
                      objects=[{k: o.get(k) for k in ("name", "category", "centroid_m", "top_z_m", "bottom_z_m", "bbox_size_m", "bbox_source")}
                               for o in truth["objects"]], truth_errors=truth.get("errors"))
        tops = [[o["centroid_m"][0], o["centroid_m"][1], o["top_z_m"]] for o in objects]
        projected = {}
        for camera in ("cam_head", "cam_left_wrist", "cam_right_wrist"):
            result = call(harness, dict(kind="project_points", observation_id=obs_id, camera_id=camera, points_m=tops), "project-" + camera)
            value = result.get("value") or {}
            projected[camera] = list(zip(value.get("pixels") or [], value.get("in_image") or []))
            if camera == "cam_head" and value.get("overlay_ref"):
                _, data = store.resolve_artifact(value["overlay_ref"])
                (root / "private" / "probe_overlay_cam_head.jpg").write_bytes(data)
        for index, obj in enumerate(objects):
            pixel, visible = projected["cam_head"][index] if index < len(projected["cam_head"]) else (None, False)
            if not visible:
                continue
            result = call(harness, dict(kind="pixel_plane", observation_id=obs_id, camera_id="cam_head", pixel=pixel,
                                        plane_origin_m=[0.0, 0.0, TABLE_Z], plane_normal=[0.0, 0.0, 1.0]), f"plane-{index}")
            value = result.get("value") or {}
            report["calls"].append(dict(kind="pixel_plane", object=obj["name"], pixel=pixel, status=result.get("status"),
                                        reason=result.get("reason"), point_m=value.get("point_m"),
                                        plane_estimate_status=(value.get("plane_estimate") or {}).get("status")))
            for camera in ("cam_left_wrist", "cam_right_wrist"):
                other, seen = projected[camera][index] if index < len(projected[camera]) else (None, False)
                if seen:
                    result = call(harness, dict(kind="triangulate", observation_ids=[obs_id], camera_id="cam_head", camera_id_b=camera,
                                                pixels_a=[pixel], pixels_b=[other], max_error_px=3.0), f"tri-{index}-{camera}")
                    value = result.get("value") or {}
                    report["calls"].append(dict(kind="triangulate", object=obj["name"], camera_b=camera, status=result.get("status"),
                                                points_m=value.get("points_m"), reprojection_error_px=value.get("reprojection_error_px")))
                    break
        second = harness.observe({"tool_call_id": "probe-observe-2"})
        report["second_observation_id"] = second["observation_id"]
        report["passed"] = True
    except Exception as error:
        report.update(error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())
    finally:
        try:
            report["close"] = backend.close()
        except Exception as error:
            report["close_error"] = f"{type(error).__name__}: {error}"
        store.close()
    truth_file = root / "private" / FILENAME
    report["truth_lines"] = len(truth_file.read_text().splitlines()) if truth_file.exists() else 0
    spec = importlib.util.spec_from_file_location("score_geometry_truth", Path(__file__).resolve().parents[1] / "score_geometry_truth.py")
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    score = scorer.score_run(root)
    report["score_summary"] = dict(geometry=score["geometry"], leak_audit=score["leak_audit"], truth=score["truth"])
    (root / "probe_report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: report.get(k) for k in ("passed", "error", "truth_lines", "sim_step", "table")}, default=str))
    print(scorer.markdown([score]))
    return 0 if report["passed"] and report["truth_lines"] >= 2 else 1


if __name__ == "__main__":
    sys.exit(main())
