"""Archived commands stay exact; incomplete or invalid evidence is rejected."""

from collections import deque
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from auriga.apps.replay.hd_replay import load_archive, sha256, state_error, validate_samples
from auriga.robot.backends.robodojo.native_replay import install_recorded_controls
from auriga.core.storage.store import Store


def commands():
    return {f"{side}_{kind}_joint_state": {"position": [0.02] * size, "velocity": 0}
            for side in ("left", "right") for kind, size in (("arm", 6), ("ee", 2))}


def robot_state():
    return {"arms": {side: {"q_rad": [0.] * 6, "qdot_rad_s": [0.] * 6,
        "ee_pose": {"position_m": [0., 0., 0.], "quaternion_xyzw": [0., 0., 0., 1.]},
        "gripper": {"position_m": [0., 0.], "goal_normalized": 0., "command_normalized": 0.}}
        for side in ("left", "right")}}


class HDReplayTests(unittest.TestCase):
    def test_untrusted_tick_count_cannot_allocate_a_declared_size_range(self):
        row = {"sim_step": 1, "commands": commands()}
        # Keep the regression safe even if range(expected_ticks) is reintroduced.
        with patch("auriga.apps.replay.hd_replay.range", create=True,
                   side_effect=AssertionError("must not allocate from an untrusted tick count")), \
                self.assertRaisesRegex(ValueError, "wrong_coverage"):
            validate_samples([row], 10**12, 10)

    def test_trace_requires_exact_coverage_and_complete_native_groups(self):
        rows = [{"sim_step": step, "commands": commands()} for step in range(1, 21)]
        self.assertEqual(validate_samples(list(reversed(rows)), 20, 10), rows)
        for invalid, total in ((rows[:-1], 20), (rows + [rows[-1]], 20),
                               (rows[:8] + rows[9:], 20), (rows[:-1], 19)):
            with self.subTest(total=total, count=len(invalid)), self.assertRaises(ValueError):
                validate_samples(invalid, total, 10)

    def test_commands_reject_nonfinite_values_and_wrong_dimensions(self):
        for value in (float("nan"), float("inf"), -float("inf"), True, "0.02"):
            row = {"sim_step": 1, "commands": commands()}
            row["commands"]["left_ee_joint_state"]["position"][0] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_samples([row], 1, 1)
        row = {"sim_step": 1, "commands": commands()}
        row["commands"]["right_arm_joint_state"]["position"].pop()
        with self.assertRaisesRegex(ValueError, "dimensions"):
            validate_samples([row], 1, 1)

    def test_state_error_ignores_goals_but_measures_motion(self):
        original, replay = robot_state(), robot_state()
        replay["arms"]["left"]["gripper"].update(goal_normalized=1., command_normalized=1.)
        self.assertEqual(set(state_error(original, replay).values()), {0.})
        replay["arms"]["left"]["q_rad"][2] = .04
        replay["arms"]["right"]["ee_pose"]["position_m"][0] = .006
        replay["arms"]["right"]["gripper"]["position_m"][0] = .004
        error = state_error(original, replay)
        self.assertAlmostEqual(error["joint_rad"], .04)
        self.assertAlmostEqual(error["ee_position_m"], .006)
        self.assertAlmostEqual(error["gripper_m"], .004)

    def test_state_error_rejects_nan_in_either_measurement(self):
        for field in ("q_rad", "qdot_rad_s", "ee_position", "ee_quaternion", "gripper"):
            for source_bad in (False, True):
                original, replay = robot_state(), robot_state()
                arm = (original if source_bad else replay)["arms"]["left"]
                values = (arm["ee_pose"]["position_m"] if field == "ee_position" else
                          arm["ee_pose"]["quaternion_xyzw"] if field == "ee_quaternion" else
                          arm["gripper"]["position_m"] if field == "gripper" else arm[field])
                values[0] = float("nan")
                with self.subTest(field=field, source_bad=source_bad), self.assertRaises(ValueError):
                    state_error(original, replay)

    def test_injection_preserves_side_effects_without_second_clamp(self):
        class NativeControl:
            def __init__(self, command):
                self.control_info_dict = command

            def get_action(self, robot_manager, env_idx):
                raise AssertionError("recorded gripper target was clamped again")

        pending, side_effects = deque(), []

        def original_process(control, env_idx):
            side_effects.append((control, env_idx))
            return [{"wrong": "recomputed interpolation must be discarded"}]

        manager = SimpleNamespace(control_queue=[SimpleNamespace(pop=pending.popleft)], prev_control=[{}])
        adapter = SimpleNamespace(env=SimpleNamespace(process_control_info=original_process),
                                  robot=SimpleNamespace(control_manager=manager))
        group = install_recorded_controls(adapter, NativeControl)
        for target in (.02, .07):
            recorded = commands()
            recorded["left_ee_joint_state"]["position"] = [target, -target]
            group[:] = [recorded]
            expanded = adapter.env.process_control_info({"native": "dummy"}, 0)
            self.assertIs(expanded, group)
            pending.extend(NativeControl(item) for item in expanded)
            control = manager.pop([0])[0]
            self.assertIsInstance(control, NativeControl)
            self.assertIs(control.get_action(adapter.robot, 0), recorded)
            self.assertIs(manager.prev_control[0], recorded)
        self.assertEqual(side_effects, [({"native": "dummy"}, 0)] * 2)
        self.assertEqual(len(pending), 0)
        with self.assertRaisesRegex(ValueError, "one_environment"):
            manager.pop([1])

    def test_archive_loading_keeps_sealed_sources_with_and_without_wal_unchanged(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            wal_archive = Path(directory) / "wal-archive"
            identity = dict(run_id="run", episode_id="episode", env_instance_id="env", env_generation=0)
            (source / "evaluation.json").write_text(json.dumps(identity | {
                "result": dict(sim_step=10, evaluator_actions=1)}))
            (source / "effective_config.json").write_text("{}")
            store = Store(source, identity)
            try:
                store.db.execute("PRAGMA wal_autocheckpoint=0")
                store.set_state("runtime", "capabilities", dict(native_physics_steps_per_action=10, physics_dt_s=.004))
                trace = dict(scope="official interpolated actuator commands", samples=[
                    dict(sim_step=step, commands=commands()) for step in range(1, 11)])
                def artifact(data):
                    return store.put_artifact(json.dumps(data).encode(), "trace", "application/json")
                store.save("action_receipt", "receipt", dict(executed_native_steps=1, completed_native_steps=1,
                    dispatched_trace_ref=artifact(trace), actual_trace_ref=artifact([
                        dict(sim_step=10, state=robot_state())])))
                store.save("observation", "initial", identity | dict(sim_step=0,
                    robot_state_ref=artifact(robot_state())))
                shutil.copytree(source, wal_archive, ignore=shutil.ignore_patterns("events.sqlite-shm"))
            finally:
                store.close()
            for root in (source, wal_archive):
                with self.subTest(wal=root == wal_archive):
                    self.assertEqual((root / "events.sqlite-wal").exists(), root == wal_archive)
                    def fingerprint():
                        return {str(path.relative_to(root)): (sha256(path), path.stat().st_mtime_ns)
                                for path in root.rglob("*") if path.is_file()}
                    before = fingerprint()
                    archive = load_archive(root)
                    self.assertEqual(len(archive["samples"]), 10)
                    self.assertEqual(set(archive["states"]), {0, 10})
                    self.assertEqual(fingerprint(), before)


if __name__ == "__main__":
    unittest.main()
