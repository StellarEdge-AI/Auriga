"""Synthetic textured, calibrated plane. This is explicitly not a RoboDojo recording."""
import json
import cv2
import numpy as np
from auriga.core.contracts import canonical

IDENTITY = dict(run_id="synthetic-geometry",episode_id="episode-1",env_instance_id="synthetic",env_generation=1)


def recorded_plane(store):
    rng = np.random.default_rng(52)
    texture = rng.integers(0,256,(480,640,3),dtype=np.uint8)
    texture = cv2.GaussianBlur(texture,(5,5),0)
    k = [[500.,0,320.],[0,500.,240.],[0,0,1.]]
    calibration = {"cameras":{"head":{"K":k,"distortion":{"model":"opencv","coefficients":[]},"image_size":[640,480]}}}
    calibration_ref = store.put_artifact(canonical(calibration).encode(),"calibration","application/json")
    model_ref = store.put_artifact(canonical({"base_link":"base","joints":[]}).encode(),"robot_model","application/json")
    observations = []
    for i,x in enumerate([0.,0.10,0.05]):
        image = cv2.warpAffine(texture,np.array([[1.,0.,-x*250],[0.,1.,0.]]),(640,480))
        _,png = cv2.imencode(".png",image)
        t = np.eye(4); t[0,3] = x
        frame_id = f"frame-{i}"
        state = {"source_kind":"measurement","sim_step":i,"step_domain":"control_step","camera_poses":{"head":{"T_world_camera":t.tolist(),"frame_id":frame_id,"sim_step":i,"step_domain":"control_step","source_kind":"robot_kinematics"}}}
        state_ref = store.put_artifact(canonical(state).encode(),"robot_state","application/json")
        observations.append({"schema_version":"0.1",**IDENTITY,"world_kind":"observed","data_origin":"synthetic_fixture","observation_id":f"obs-{i}","sim_step":i,"step_domain":"control_step","clock_domain":"synthetic/generation-1","calibration_id":"synthetic-calibration-v1","robot_model_id":"synthetic-camera-rig","robot_model_ref":model_ref,"observation_profile":"rgb_proprio_calibrated_synthetic_v1","robot_state_ref":state_ref,"cameras":[{"camera_id":"head","frame_id":frame_id,"width":640,"height":480,"sim_step":i,"step_domain":"control_step","rgb_ref":store.put_artifact(png.tobytes(),"rgb","image/png"),"calibration_ref":calibration_ref,"pixel_transform":"identity"}]})
    return observations
