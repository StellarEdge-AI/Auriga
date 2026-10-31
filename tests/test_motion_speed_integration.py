"""Run defaults and actor presets reach canonical requests and cached execution."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


from auriga.core.config import Config, ConfigError
from auriga.robot.motion.arx_actions import compile_action
from auriga.runtime.harness import Harness
from auriga.core.storage.store import Store
from scripts import benchmark_queue as queue
from test_harness import FixtureRobot


class MotionSpeedIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def harness(self, default=1.25):
        store = Store(self.root / "store", dict(run_id="speed", episode_id="ep", env_instance_id="env", env_generation=1))
        self.addCleanup(store.close)
        robot = FixtureRobot(self.root)
        original_observe = robot.observe
        robot.observe = lambda: dict(original_observe(),
            camera_calibration=dict(reference_frame_id="env_origin", cameras={}),
            camera_pose_sample=dict(cameras={}), robot_model=dict(base_link="base", joints=[]))
        caps = dict(robot.start(), path_kinds=["backend_free", "hold"],
                    action_speed=dict(available=True, speed_scale=dict(default=1.)))
        robot.start = lambda: caps
        config = Config.from_dict(dict(features=dict(motion_review=False, default_motion_speed_scale=default)))
        harness = Harness(store, robot, config=config)
        harness.start()
        return harness, robot, caps

    def move(self, harness, **values):
        return dict(observation_id=harness.latest["observation_id"], arm="left",
                    position_m=[.1, 0, 1.], quaternion_xyzw=[0, 0, 0, 1], **values)

    def test_actual_default_capability_is_published_without_mutating_backend(self):
        h, _, caps = self.harness(1.5)
        self.assertEqual(caps["action_speed"]["speed_scale"]["default"], 1.)
        expected = h.capabilities["action_speed"]
        self.assertEqual(expected["speed_scale"]["default"], 1.5)
        self.assertEqual(expected["speed_presets"], dict(slower=.5, slow=.8, careful=.9, normal=1., fast=1.25, faster=1.5))
        self.assertEqual(h.store.read_state("runtime", "capabilities")["action_speed"], expected)

    def test_equivalent_preset_and_numeric_retry_is_idempotent(self):
        h, robot, _ = self.harness()
        args = self.move(h, speed_preset="normal", action_id="same")
        h.execute(compile_action("move", args, h.identity, h.capabilities), {"tool_call_id": "one"})
        equivalent = {key: value for key, value in args.items() if key != "speed_preset"}
        equivalent["speed_scale"] = 1
        h.execute(compile_action("move", equivalent, h.identity, h.capabilities), {"tool_call_id": "two"})
        self.assertEqual(robot.executions, 1)

    def test_queue_cli_config_and_archived_snapshot_preserve_default(self):
        from auriga.apps.gateway import cli as gateway
        snapshot = self.root / "config.json"
        config = Config.from_dict(dict(features=dict(default_motion_speed_scale=1.25)))
        snapshot.write_text(json.dumps(config.to_dict()))
        case = dict(cwd=str(Path(__file__).resolve().parents[1]), run_dir=str(self.root / "run"),
                    task="put_bottles_into_dustbin", seed=0, layout_id=0, title="default speed", tag=[])
        for override in (None, 1.5):
            value = {**case, **({"default_motion_speed_scale": override} if override else {})}
            command = queue.command(value, 0, config=config, config_path=snapshot)
            with patch.object(gateway, "serve", new_callable=AsyncMock) as serve, patch.object(sys, "argv", ["gateway", *command[4:]]):
                gateway.main()
            resolved = serve.call_args.args[0].resolved_config
            self.assertEqual(resolved.features.default_motion_speed_scale, override or 1.25)
            self.assertEqual(Config.from_dict(json.loads(json.dumps(resolved.to_dict()))).features.default_motion_speed_scale, override or 1.25)

    def test_invalid_defaults_and_feedback_combination_fail_before_launch(self):
        for value in (True, None, 0, .24, 2.01, float("nan"), float("inf"), "fast"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Config.from_dict(dict(features=dict(default_motion_speed_scale=value)))
        with self.assertRaisesRegex(ConfigError, "requires_default_speed_1"):
            Config.from_dict(dict(features=dict(motion_execution_mode="feedback", motion_review=False, default_motion_speed_scale=1.25)))


if __name__ == "__main__":
    unittest.main()
