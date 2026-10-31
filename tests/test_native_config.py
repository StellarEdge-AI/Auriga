"""Check real upstream task configuration without importing or launching Isaac."""

import json
import os
from pathlib import Path
import subprocess
import unittest


class NativeConfigCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured_root = os.environ.get("AURIGA_ROBODOJO_ROOT")
        roots = [Path(configured_root).expanduser()] if configured_root else [
            Path.home() / "code/RoboDojo", Path.home() / "code/robodojo"]
        root = next((path for path in roots if (path / "utils/pipeline_utils.py").is_file()), None)
        python = Path(os.environ.get("AURIGA_ROBODOJO_PYTHON",
            str(Path.home() / "miniforge3/envs/RoboDojo/bin/python"))).expanduser()
        if root is None or not python.is_file():
            raise unittest.SkipTest("installed RoboDojo config and Python required; no simulator is launched")
        code = r'''
import csv
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
project = Path(sys.argv[2])
sys.path.insert(0, str(project))
sys.path.insert(0, str(root))
from omegaconf import OmegaConf
from utils.load_file import load_yaml
from utils.pipeline_utils import process_config, process_randomization
from auriga.robot.backends.robodojo.robot_worker import native_eval_config, configure_depth_capture
from auriga.core.observation_policy import DEPTH_ACTOR_PROFILE

with (project / "benchmarks/robodojo-42-15/cases.csv").open(newline="") as stream:
    rows = list(csv.DictReader(stream))
tasks = list(dict.fromkeys(row["task"] + ("_random" if row["variant"] == "random" else "") for row in rows))
index = load_yaml(root / "task/RoboDojo/config/_task.yml")
base = load_yaml(root / "env_cfg/arx_x5.yml")
records = {}
for task in tasks:
    config = native_eval_config(root, task, seed=3, layout_id=2)
    rgbd = native_eval_config(root, task, seed=3, layout_id=2, observation_profile=DEPTH_ACTOR_PROFILE)
    settings = {**index.get("common", {}), **index["tasks"].get(task, {})}
    expected_robot = load_yaml(root / "env_cfg/robot" / (settings.get("robot_config", "dual_x5") + ".yml"))
    if not settings.get("robot_self_collision", True):
        for robot in expected_robot["robots"]:
            robot["enabled_self_collisions"] = False
    official = OmegaConf.create({
        **{section: load_yaml(root / "env_cfg" / section / f"{name}.yml")
           for section, name in base["config"].items()},
        "task_env": load_yaml(root / "task/RoboDojo/config" / f"{task}.yml"),
        "eval_cfg": base,
    })
    official = process_randomization(official)
    official, _ = process_config(official, task_name=task)
    official.sim.scene.num_envs = 1
    official.sim.seed = [2]
    records[task] = dict(
        config=OmegaConf.to_container(config, resolve=True),
        rgbd_config=OmegaConf.to_container(rgbd, resolve=True),
        disabled_config=OmegaConf.to_container(configure_depth_capture(rgbd), resolve=True),
        expected_robot=expected_robot,
        expected_sim=OmegaConf.to_container(official.sim, resolve=True),
        expected_scene=OmegaConf.to_container(official.scene, resolve=True),
        expected_task=OmegaConf.to_container(official.task_env, resolve=True),
        expected_stabilization=settings.get("data_source", "datagen") == "teleop",
        expected_frequency=base["observation"]["collect_freq"],
    )
print(json.dumps(dict(records=records, isaac_imported=any(
    name == "isaacsim" or name.startswith("isaacsim.") or name.startswith("isaaclab.")
    for name in sys.modules))))
'''
        project = Path(__file__).resolve().parents[1]
        result = subprocess.run([str(python), "-c", code, str(root), str(project)],
            cwd=project, text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise AssertionError("native config subprocess failed: " + result.stderr)
        cls.result = json.loads(result.stdout)

    def test_support_arms_match_upstream_task_registry(self):
        records = self.result["records"]
        self.assertEqual(len(records), 54)
        for task, record in records.items():
            with self.subTest(task=task):
                robots = record["config"]["robot"]
                self.assertEqual(robots, record["expected_robot"])
                supports = [robot for robot in robots["robots"] if robot.get("type") == "support"]
                if task in ("imitate_sorting_sequence", "make_kong", "play_tic_tac_toe"):
                    self.assertEqual(len(supports), 1)
                    self.assertEqual(supports[0]["robot_name"], "franka")
                else:
                    self.assertEqual(supports, [])

    def test_teleop_stabilization_and_ordinary_task_physics(self):
        for task, record in self.result["records"].items():
            with self.subTest(task=task):
                enabled = record["config"]["sim"].get("physx", {}).get("enable_stabilization", False)
                self.assertEqual(enabled, record["expected_stabilization"])
        self.assertTrue(self.result["records"]["classify_objects"]["expected_stabilization"])
        self.assertFalse(self.result["records"]["arrange_largest_number"]["expected_stabilization"])

    def test_simulation_scene_and_objects_match_official_defaults_for_every_task(self):
        records = self.result["records"]
        for task, record in records.items():
            with self.subTest(task=task):
                self.assertEqual(record["config"]["sim"], record["expected_sim"])
                self.assertEqual(record["config"]["scene"], record["expected_scene"])
                self.assertEqual(record["config"]["task_env"], record["expected_task"])
        for task in ("pour_liquid_into_cup", "pour_liquid_into_cup_random", "pour_by_language"):
            with self.subTest(fluid_task=task):
                config = records[task]["config"]
                self.assertTrue(config["task_env"]["Fluid"])
                self.assertEqual(config["sim"].get("device", "cpu"), "cpu")
                self.assertFalse(config["sim"].get("use_fabric", False))

    def test_selected_case_camera_and_observation_boundary_survive_native_setup(self):
        self.assertFalse(self.result["isaac_imported"])
        for task, record in self.result["records"].items():
            with self.subTest(task=task):
                config = record["config"]
                self.assertEqual(config["sim"]["seed"], [2])
                self.assertEqual(config["sim"]["scene"]["num_envs"], 1)
                self.assertEqual(config["eval_cfg"]["seed"], 3)
                self.assertEqual(config["eval_cfg"]["num_envs"], 1)
                self.assertEqual(config["eval_cfg"]["eval_num"], 1)
                self.assertEqual(config["eval_cfg"]["task_name"], task)
                self.assertEqual(config["eval_cfg"]["description"], {})
                self.assertEqual(config["deploy_cfg"], {"port": 0, "policy_name": "Auriga"})
                self.assertEqual(config["camera"]["default_frequency"], record["expected_frequency"])

    def test_depth_is_explicit_and_forced_off_when_profile_changes_back(self):
        for task, record in self.result["records"].items():
            with self.subTest(task=task):
                for key in ("config", "disabled_config"):
                    config = record[key]
                    self.assertFalse(config["eval_cfg"]["observation"]["vision"]["depth"])
                    self.assertFalse(config["eval_cfg"]["observation"]["vision"]["approximate_depth"])
                    for capture in config["camera"]["annotator"].values():
                        self.assertFalse(any(isinstance(item, dict) and item.get("type") in
                            ("depth", "distance_to_image_plane", "distance_to_camera") for item in capture.values()))
                config = record["rgbd_config"]
                self.assertTrue(config["eval_cfg"]["observation"]["vision"]["depth"])
                self.assertFalse(config["eval_cfg"]["observation"]["vision"]["approximate_depth"])
                cameras = [name for name, value in config["camera"].items() if isinstance(value, dict) and "camera" in value]
                self.assertGreater(len(cameras), 0)
                for camera in cameras:
                    self.assertEqual(config["camera"]["annotator"][camera]["distance_to_image_plane_capture"]["type"],
                                     "distance_to_image_plane")


if __name__ == "__main__":
    unittest.main()
