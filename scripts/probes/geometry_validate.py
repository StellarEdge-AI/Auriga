#!/usr/bin/env python3
"""Validate CPU reconstruction on recorded permitted RGB; writes measurements, never oracle inputs.

python scripts/probes/geometry_validate.py --recording runs/CALIBRATED --output runs/geometry-report
python scripts/probes/geometry_validate.py --synthetic --output runs/geometry-synthetic
"""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from auriga.core.contracts import IDENTITY,canonical,digest
from auriga.robot.model.geometry import GeometryTools,triangulate,fit_geometry
from auriga.perception.scene.store import SceneStore
from auriga.core.storage.store import Store
from auriga.apps.library.experiments import allocate_run, register_run
from auriga.apps.evaluation.recording import ExperimentProgress


def degeneracy_matrix():
    camera={"K":np.array([[500.,0,320],[0,500.,240],[0,0,1]]),"D":np.zeros(5),"T":np.eye(4),"width":640,"height":480}
    other={**camera,"T":np.eye(4)}; other["T"][0,3]=0.1
    cases=[("pure_rotation",lambda:triangulate(camera,camera,[[320,240]],[[300,240]]),"insufficient_parallax"),
           ("low_parallax",lambda:triangulate(camera,other,[[320,240]],[[319.9,240]]),"insufficient_parallax"),
           ("wrong_correspondence",lambda:triangulate(camera,other,[[320,240]],[[295,270]]),"inconsistent_correspondence"),
           ("negative_depth",lambda:triangulate(camera,other,[[320,240]],[[345,240]]),"negative_depth"),
           ("box_single_surface",lambda:fit_geometry([[i%3,i//3,1] for i in range(6)],"box"),"box_thickness_unobserved"),
           ("collinear_plane",lambda:fit_geometry([[i,0,0] for i in range(6)],"plane"),"inconsistent_plane_support")]
    rows=[]
    for name,function,expected in cases:
        try:
            result=function(); outcome=",".join(result.get("point_status",[]))
        except ValueError as error: outcome=str(error)
        rows.append({"case":name,"expected":expected,"actual":outcome,"passed":expected in outcome,"data_origin":"synthetic_fault_injection"})
    return rows


def read_raw_observations(path):
    content=Path(path).read_text()
    try:
        parsed=json.loads(content)
        return parsed if isinstance(parsed,list) else [parsed]
    except json.JSONDecodeError:
        return [json.loads(line) for line in content.splitlines() if line.strip()]


def import_raw_recording(path, store):
    path=Path(path).resolve()
    raw_observations=read_raw_observations(path)
    observations=[]
    for raw in raw_observations:
        if any(key in raw and raw[key]!=store.identity[key] for key in IDENTITY): raise ValueError("raw_mixed_environment_identities")
        if raw.get("observation_profile")!="rgb_proprio_calibrated_v1": raise ValueError("raw_recording_requires_disclosed_calibration")
        calibration=raw["camera_calibration"]
        calibration_ref=store.put_artifact(canonical(calibration).encode(),"calibration","application/json")
        pose_ref=store.put_artifact(canonical(raw["camera_pose_sample"]).encode(),"camera_pose","application/json")
        model_ref=store.put_artifact(canonical(raw["robot_model"]).encode(),"robot_model","application/json")
        state_ref=store.put_artifact(canonical(raw["robot_state"]).encode(),"robot_state","application/json")
        cameras=[]
        for frame in raw["cameras"]:
            from auriga.core.storage.images import camera_path
            image_path=camera_path(frame).resolve()
            if not image_path.is_relative_to(path.parent): raise ValueError("raw_image_outside_recording")
            cameras.append({"camera_id":frame["camera_id"],"frame_id":frame["frame_id"],"width":frame["width"],"height":frame["height"],"pixel_transform":"identity","sim_step":raw["sim_step"],"step_domain":raw["step_domain"],"rgb_ref":store.put_artifact(image_path.read_bytes(),"rgb",frame.get("image_encoding",{}).get("mime_type","image/png")),"calibration_ref":calibration_ref,"pose_sample_ref":pose_ref})
        observations.append({"schema_version":"0.1",**store.identity,"world_kind":"observed","data_origin":"robodojo_runtime","observation_id":raw["raw_observation_id"],"sim_step":raw["sim_step"],"step_domain":raw["step_domain"],"clock_domain":f"{store.identity['env_instance_id']}/generation-{store.identity['env_generation']}","sim_time_s":raw.get("sim_time_s"),"robot_state_ref":state_ref,"robot_model_ref":model_ref,"robot_model_id":raw["robot_model_id"],"calibration_id":"cal-"+digest(calibration),"observation_profile":raw["observation_profile"],"cameras":cameras})
    return observations


def validate(recording, output, synthetic=False, camera_name=None, raw_recording=None, heldout_index=-1, roi=None, train_indices=None):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    if (output/"report.json").exists(): raise ValueError("output_report_already_exists")
    if raw_recording:
        path=Path(raw_recording).resolve()
        first=read_raw_observations(path)[0]
        identity={"run_id":path.parent.name,"episode_id":"calibrated-recording-episode","env_instance_id":"recorded-robodojo","env_generation":1}
        identity.update({key:first[key] for key in IDENTITY if key in first})
        artifacts=Store(output/"artifacts",identity)
        observations=import_raw_recording(path,artifacts)
        resolve=artifacts.resolve_artifact
        source_digest=digest(observations)
    elif synthetic:
        sys.path.insert(0,str(ROOT/"tests"))
        from geometry_fixture import IDENTITY as identity,recorded_plane
        artifacts=Store(output/"artifacts",identity)
        observations=recorded_plane(artifacts)
        resolve=artifacts.resolve_artifact
        source_digest=digest(observations)
    else:
        recording=Path(recording).resolve()
        database=sqlite3.connect(f"file:{recording/'events.sqlite'}?mode=ro",uri=True)
        observations=[json.loads(row[0]) for row in database.execute("SELECT body FROM records WHERE kind='observation' ORDER BY rowid")]
        if not observations: raise ValueError("recording_has_no_observations")
        identity={key:observations[0][key] for key in IDENTITY}
        observations=[observation for observation in observations if all(observation[key]==identity[key] for key in IDENTITY)]
        artifacts=Store(output/"artifacts",identity)
        def resolve(reference):
            row=database.execute("SELECT body FROM artifacts WHERE id=?",(reference.removeprefix("artifact:"),)).fetchone()
            if not row: return artifacts.resolve_artifact(reference)
            metadata=json.loads(row[0])
            path=recording/"blobs"/metadata["sha256"]
            if metadata["storage_key"]!=metadata["sha256"]: raise ValueError("invalid_recorded_storage_key")
            return metadata,path.read_bytes()
        source_digest=digest(observations)
    scene=SceneStore(output/"scene",resolve,artifacts.put_artifact)
    for observation in observations: scene.import_observation(observation)
    geometry=GeometryTools(scene)
    cameras=[camera["camera_id"] for camera in observations[0]["cameras"]]
    if camera_name: cameras=[camera_name]
    selected_index=heldout_index if heldout_index>=0 else len(observations)-1
    heldout=observations[selected_index]
    if train_indices is not None and any(index<0 or index>=selected_index for index in train_indices): raise ValueError("training_must_precede_declared_heldout")
    training=[observations[index] for index in train_indices] if train_indices is not None else observations[:selected_index]
    if not training: raise ValueError("recording_requires_training_and_heldout_observations")
    candidates=[]
    for camera in cameras:
        for first,second in itertools.combinations(training,2):
            try:
                a=geometry.camera(identity,first["observation_id"],camera)
                b=geometry.camera(identity,second["observation_id"],camera)
                baseline=float(np.linalg.norm(a['T'][:3,3]-b['T'][:3,3]))
                if baseline>0.003:
                    candidates.append((baseline,{**identity,"kind":"build_scene","camera_id":camera,"observation_ids":[first["observation_id"],second["observation_id"]],"static_scene_assumption":True,"base_scene_version":0,"entity_id":"local-plane","primitive":"plane"}))
            except ValueError: pass
    # Prioritize larger permitted translation and retain a bounded, documented search.
    candidates=sorted(candidates,key=lambda item:item[0],reverse=True)[:8]
    for first_camera,second_camera in itertools.permutations(cameras,2):
        candidates.append((None,{**identity,"kind":"build_scene","camera_id":first_camera,"camera_id_b":second_camera,"observation_ids":[training[0]["observation_id"]],"base_scene_version":0,"entity_id":"local-plane","primitive":"plane"}))
    attempts=[]
    for baseline,request in candidates:
        if roi is not None: request["roi"]=roi
        result=geometry.execute(request)
        filename=f"attempt-{len(attempts):02}.json"
        (output/filename).write_text(json.dumps({"request":request,"result":result},indent=2,allow_nan=False))
        accepted=result.get("value",{}).get("geometry_result",{}).get("accepted",0) if result.get("value") else 0
        attempts.append({"request":request,"baseline_m":baseline,"status":result["status"],"reason":result.get("reason"),"accepted":accepted,"artifact":filename})
    valid=sorted([item for item in attempts if item["status"]=="conditional" and item["accepted"]>=6],key=lambda item:item["accepted"],reverse=True)
    report={"schema_version":"0.1",**identity,"source_kind":"synthetic_fixture" if synthetic else "recorded_robodojo_rgb_calibrated","source_observations_digest":source_digest,"observation_count":len(observations),"heldout_observation_id":heldout["observation_id"],"heldout_index":selected_index,"training_observation_ids":[item["observation_id"] for item in training],"attempts":attempts,"oracle_used":False,"roi":roi,"roi_source":"developer_selected_from_permitted_rgb" if roi is not None else "full_frame","train_indices":train_indices,"camera_pose_strategy":"FK from recorded measured q, permitted URDF and disclosed fixed camera mount; native USD pose retained only as diagnostic","physics":"unsupported","degeneracy_matrix":degeneracy_matrix(),"status":"no_supported_reconstruction"}
    if valid:
        selected=valid[0]
        result=json.loads((output/selected["artifact"]).read_text())["result"]
        committed=scene.apply_patch(result["value"]["proposal"])
        camera=selected["request"].get("camera_id_b",selected["request"]["camera_id"])
        alignment=geometry.execute({**identity,"kind":"alignment","scene_version":1,"observation_ids":[heldout["observation_id"]],"camera_id":camera})
        snapshot=scene.snapshot(identity,1,selected["request"]["observation_ids"][-1])
        report.update(status="reconstruction_and_heldout_measured" if alignment["status"]=="supported" else "reconstruction_only_heldout_unavailable",selected_attempt=selected["artifact"],scene_digest=digest(committed),scene_version=1,entities=committed["entities"],alignment=alignment,snapshot_id=snapshot["snapshot_id"],snapshot_digest=digest(snapshot),closed_artifact_count=len(snapshot["resolved_artifacts"]))
        measurement=alignment.get("value") or {}
        source_camera=geometry.camera(identity,selected["request"]["observation_ids"][-1],camera)
        heldout_camera=geometry.camera(identity,heldout["observation_id"],camera)
        heldout_translation=float(np.linalg.norm(source_camera["T"][:3,3]-heldout_camera["T"][:3,3]))
        report["heldout_camera_translation_from_last_training_m"]=heldout_translation
        thresholds={"min_points":6,"max_rms_px":2.0,"max_error_px":5.0,"min_coverage":0.25,"min_heldout_camera_translation_m":0.001}
        report["alignment_acceptance"]={"thresholds":thresholds,"passed":bool(measurement and heldout_translation>=0.001 and measurement["count"]>=6 and measurement["rms_px"]<=2.0 and measurement["max_px"]<=5.0 and all(view["coverage"]>=0.25 for view in measurement["views"]))}
        (output/"snapshot.json").write_text(json.dumps(snapshot,indent=2))
    report["camera_checks"]=[geometry.execute({**identity,"kind":"camera_pose","observation_ids":[observations[0]["observation_id"]],"camera_id":camera}) for camera in cameras]
    report["fk_checks"]=[geometry.execute({**identity,"kind":"fk","observation_ids":[observations[0]["observation_id"]],"camera_id":camera}) for camera in cameras]
    closure=scene.closure(observations)
    replay={"schema_version":"0.1","data_origin":report["source_kind"],"source_observations_digest":source_digest,"artifacts":[{**metadata,"storage_key":"scene/resolved_blobs/"+metadata["sha256"]} for metadata in closure.values()],"obs_jsonl":"observations.jsonl","requests":[]}
    if valid:
        replay["requests"]=[{"path":"/v1/scene/update-jobs","body":selected["request"],"wait_for_job":True},{"path":"/v1/alignment-jobs","body":{**identity,"kind":"alignment","scene_version":1,"observation_ids":[heldout["observation_id"]],"camera_id":camera},"wait_for_job":True}]
    (output/"observations.jsonl").write_text("".join(canonical(observation)+"\n" for observation in observations))
    (output/"replay-package.json").write_text(json.dumps(replay,indent=2))
    report["replay_package"]="replay-package.json"
    (output/"report.json").write_text(json.dumps(report,indent=2,allow_nan=False))
    artifacts.close()
    if not synthetic and not raw_recording: database.close()
    alignment=report.get("alignment",{}).get("value") or {}
    return {"report":str(output/"report.json"),"status":report["status"],"attempt_count":len(attempts),"alignment":{key:alignment.get(key) for key in ("rms_px","median_px","max_px","count")}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording")
    parser.add_argument("--raw-recording",help="Permitted raw observation JSONL from one generation; never an evaluator report.")
    parser.add_argument("--synthetic",action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--experiment-root")
    parser.add_argument("--category",default="geometry")
    parser.add_argument("--title")
    parser.add_argument("--tag",action="append",default=[])
    parser.add_argument("--camera")
    parser.add_argument("--heldout-index",type=int,default=-1,help="Predeclared heldout capture; train only on preceding observations.")
    parser.add_argument("--roi",help="Explicit image ROI x,y,width,height; recorded as developer-selected input.")
    parser.add_argument("--train-indices",help="Optional comma-separated training indices, all before heldout.")
    args=parser.parse_args()
    if not args.synthetic and not args.recording and not args.raw_recording: parser.error("requires --recording or --synthetic")
    output=Path(args.output).expanduser().resolve() if args.output else allocate_run(args.experiment_root,category=args.category,task="local-scene",title=args.title,tags=args.tag,kind="geometry")
    if (output/"report.json").exists(): parser.error("choose a fresh output directory")
    register_run(output,category=args.category,task="local-scene",title=args.title,tags=args.tag,kind="geometry")
    progress=ExperimentProgress(output,kind="geometry",phase="reconstruction")
    try:
        progress.update(status="running")
        result=validate(args.recording,output,args.synthetic,args.camera,args.raw_recording,args.heldout_index,[int(x) for x in args.roi.split(",")] if args.roi else None,[int(x) for x in args.train_indices.split(",")] if args.train_indices else None)
        report=json.loads((output/"report.json").read_text())
        progress.finish("completed",{**result,"data_origin":report["source_kind"],"validation_passed":report.get("alignment_acceptance",{}).get("passed"),"task_success":None,"normalized_score":None})
        print(json.dumps(result,indent=2))
    except BaseException as error:
        progress.finish("failed",{"error":type(error).__name__+": "+str(error),"task_success":None,"normalized_score":None})
        raise

if __name__=="__main__": main()
