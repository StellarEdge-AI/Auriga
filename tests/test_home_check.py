"""CPU-only home geometry and observation-lifecycle checks; no robot imports."""

from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auriga.runtime.actions.home_check import HOME_CHECK_POLICY, HomePoseCheck
from auriga.core.storage.store import Store


class HomePoseCheckTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.identity = dict(run_id="home-test", episode_id="episode-1", env_instance_id="env", env_generation=1)
        self.store = Store(Path(self.directory.name), self.identity)
        self.addCleanup(self.store.close)
        self.checker = HomePoseCheck(self.store)
        self.sequence = 0

    def state(self, step=0):
        pose = dict(position_m=[0, 0, 0], quaternion_xyzw=[0, 0, 0, 1],
                    frame_id="env_origin", tcp_config_id="arx-x5-link6-v1", source_kind="measurement")
        return dict(arms={name: dict(ee_pose=deepcopy(pose)) for name in ("left", "right")},
                    frame_id=pose["frame_id"], tcp_config_id=pose["tcp_config_id"],
                    sim_step=step, step_domain="adapter_control_step")

    def bundle(self, state=None, **updates):
        state = self.state() if state is None else state
        self.sequence += 1
        reference = self.store.put_artifact(json.dumps(state).encode(), "robot_state", "application/json")
        return dict(schema_version="0.1", **self.identity, world_kind="observed",
                    observation_id=f"obs-{self.sequence}", robot_state_ref=reference,
                    sim_step=state.get("sim_step"), step_domain=state.get("step_domain"), **updates)

    def initial(self, state=None):
        initial = self.bundle(state)
        self.checker.capture_initial(initial)
        return initial

    def result(self, state):
        return self.checker.update_current(self.bundle(state))

    def assert_status(self, status):
        result = self.checker.check(dict(self.identity))
        self.assertEqual(result["status"], status, result)
        json.dumps(result, allow_nan=False)
        return result

    @staticmethod
    def rotation(degrees):
        radians = math.radians(degrees) / 2
        return [0, 0, math.sin(radians), math.cos(radians)]

    def test_no_implicit_initial_reference_from_current(self):
        self.assert_status("unknown")
        bundle = self.bundle()
        self.checker.update_current(bundle)
        self.assertEqual(self.assert_status("unknown")["reason"], "missing_initial_observation")
        self.assertEqual(self.checker.capture_initial(bundle)["status"], "available")
        self.assert_status("home")

    def test_initial_and_returned_records_are_immutable(self):
        initial = self.initial()
        reference = self.checker.reference_record()
        state = self.state(1)
        state["arms"]["left"]["ee_pose"]["position_m"] = [0.2, 0, 0]
        current = self.bundle(state)
        self.checker.update_current(current)
        self.assert_status("away")
        rejected = self.checker.capture_initial(current)
        self.assertFalse(rejected["capture_accepted"])
        self.assertEqual(rejected["capture_reason"], "initial_reference_immutable")
        self.assertEqual(self.checker.reference_record(), reference)
        reference["arms"]["left"]["position_m"][0] = 100
        rejected["policy"]["arms"].clear()
        initial["observation_id"] = "tampered"
        current["sim_step"] = -1
        result = self.assert_status("away")
        result["arms"]["left"]["position_error_m"][0] = 0
        self.assertEqual(self.assert_status("away")["arms"]["left"]["position_error_m"][0], .2)
        self.assertEqual(self.checker.reference_record()["arms"]["left"]["position_m"], [0, 0, 0])

    def test_both_arms_required_and_either_can_be_away(self):
        self.initial()
        for name in ("left", "right"):
            with self.subTest(arm=name):
                state = self.state(1)
                state["arms"][name]["ee_pose"]["position_m"][1] = -.151
                result = self.result(state)
                self.assertEqual(result["status"], "away")
                self.assertEqual(result["arms"][name]["position_error_m"], [0, .151, 0])
                state["arms"].pop(name)
                self.assertEqual(self.result(state)["status"], "unknown")

    def test_axis_tolerance_is_not_euclidean(self):
        self.initial()
        state = self.state(1)
        state["arms"]["left"]["ee_pose"]["position_m"] = [.14, -.14, .14]
        self.assertGreater(math.hypot(.14, .14, .14), .15)
        result = self.result(state)
        self.assertEqual(result["status"], "home")
        self.assertEqual(result["arms"]["left"]["position_error_m"], [.14, .14, .14])

    def test_orientation_alone_and_arbitrary_initial_orientation(self):
        initial = self.state()
        initial["arms"]["left"]["ee_pose"]["quaternion_xyzw"] = self.rotation(67)
        self.initial(initial)
        current = deepcopy(initial)
        current["sim_step"] = 1
        current["arms"]["left"]["ee_pose"]["quaternion_xyzw"] = self.rotation(88)
        result = self.result(current)
        self.assertEqual(result["status"], "away")
        self.assertTrue(result["arms"]["left"]["position_within_tolerance"])
        self.assertAlmostEqual(result["arms"]["left"]["rotation_error_deg"], 21)

    def test_quaternion_sign_scale_and_extreme_finite_norms(self):
        self.initial()
        for quaternion in ([0, 0, 0, -2], [0, 0, 0, 1e300], [0, 0, 0, 1e-300]):
            with self.subTest(quaternion=quaternion):
                state = self.state(1)
                state["arms"]["left"]["ee_pose"]["quaternion_xyzw"] = quaternion
                self.assertEqual(self.result(state)["status"], "home")
        state["arms"]["left"]["ee_pose"]["quaternion_xyzw"] = [0, 0, 1, 0]
        self.assertEqual(self.result(state)["arms"]["left"]["rotation_error_deg"], 180)

    def test_exact_inclusive_boundaries_and_small_real_excess(self):
        initial = self.state()
        initial["arms"]["left"]["ee_pose"]["position_m"] = [.3, -.3, .6]
        self.initial(initial)
        state = self.state(1)
        state["arms"]["left"]["ee_pose"]["position_m"] = [.45, -.45, .75]
        state["arms"]["left"]["ee_pose"]["quaternion_xyzw"] = self.rotation(20)
        self.assertEqual(self.result(state)["status"], "home")
        state["arms"]["left"]["ee_pose"]["position_m"][0] += 1e-12
        self.assertEqual(self.result(state)["status"], "away")
        state["arms"]["left"]["ee_pose"]["position_m"][0] = .45
        state["arms"]["left"]["ee_pose"]["quaternion_xyzw"] = self.rotation(20 + 1e-10)
        self.assertEqual(self.result(state)["status"], "away")

    def test_joints_gripper_and_other_state_do_not_decide_home(self):
        self.initial()
        state = self.state(1)
        state["arms"]["right"].update(q_rad=[2 * math.pi], gripper={"goal_normalized": 0})
        self.assertEqual(self.result(state)["status"], "home")

    def test_invalid_measured_poses_fail_closed(self):
        self.initial()
        for field, value in [("position_m", [math.nan, 0, 0]), ("position_m", [math.inf, 0, 0]),
                             ("position_m", [True, 0, 0]), ("position_m", [0, 0]),
                             ("quaternion_xyzw", [0, 0, 0, 0]),
                             ("quaternion_xyzw", [0, 0, math.inf, 1]),
                             ("quaternion_xyzw", [0, 0, math.nan, 1]),
                             ("quaternion_xyzw", [0, 0, 1]),
                             ("source_kind", "command"), ("frame_id", "world"),
                             ("tcp_config_id", "grasp-center"), ("frame_id", None)]:
            with self.subTest(field=field, value=value):
                state = self.state(1)
                state["arms"]["left"]["ee_pose"][field] = value
                self.assertEqual(self.result(state)["status"], "unknown")
                self.assert_status("unknown")

    def test_invalid_initial_cannot_be_replaced_by_later_current(self):
        state = self.state()
        state["arms"].pop("right")
        record = self.checker.capture_initial(self.bundle(state))
        self.assertEqual(record["status"], "unknown")
        later = self.bundle(self.state(1))
        self.checker.update_current(later)
        self.checker.capture_initial(later)
        self.assert_status("unknown")
        self.assertEqual(self.checker.reference_record(), record)

    def test_artifact_missing_corrupt_non_actor_or_wrong_metadata(self):
        self.initial()
        for scope in ("internal", "evaluator", "observer"):
            with self.subTest(scope=scope):
                bundle = self.bundle(self.state(1))
                bundle["robot_state_ref"] = self.store.put_artifact(json.dumps(self.state(1)).encode(), "robot_state", "application/json", scope)
                self.assertEqual(self.checker.update_current(bundle)["status"], "unknown")
        for data, kind, mime in [(b"not json", "robot_state", "application/json"),
                                 (b"[]", "robot_state", "application/json"),
                                 (json.dumps(self.state(1)).encode(), "evaluation", "application/json"),
                                 (json.dumps(self.state(1)).encode(), "robot_state", "text/plain")]:
            bundle = self.bundle(self.state(1))
            bundle["robot_state_ref"] = self.store.put_artifact(data, kind, mime)
            self.assertEqual(self.checker.update_current(bundle)["status"], "unknown")
        bundle = self.bundle(self.state(2))
        path = self.store.artifact(bundle["robot_state_ref"])[1]
        path.write_bytes(b"corrupt")
        self.assertEqual(self.checker.update_current(bundle)["status"], "unknown")
        path.unlink()
        self.assertEqual(self.checker.update_current(bundle)["status"], "unknown")
        bundle["robot_state_ref"] = "artifact:missing"
        self.assertEqual(self.checker.update_current(bundle)["status"], "unknown")

    def test_only_actor_scope_is_requested(self):
        bundle = self.bundle()
        with patch.object(self.store, "resolve_artifact", wraps=self.store.resolve_artifact) as resolve:
            self.checker.capture_initial(bundle)
        resolve.assert_called_once_with(bundle["robot_state_ref"], allowed_scopes=("actor",))

    def test_missing_and_mismatched_metadata(self):
        self.initial()
        for key, value in [("frame_id", "world"), ("tcp_config_id", "other"),
                           ("sim_step", None), ("step_domain", None)]:
            with self.subTest(state_key=key):
                state = self.state(1)
                state[key] = value
                self.assertEqual(self.result(state)["status"], "unknown")
        for key, value in [("world_kind", "predicted"), ("schema_version", "other"),
                           ("observation_id", None), ("robot_state_ref", None),
                           ("sim_step", -1), ("sim_step", 1.0), ("sim_step", True),
                           ("sim_step", 20), ("step_domain", "native_step"),
                           ("run_id", "other"), ("episode_id", "old"),
                           ("env_instance_id", "other"), ("env_generation", 2)]:
            with self.subTest(bundle_key=key, value=value):
                bundle = self.bundle(self.state(1))
                bundle[key] = value
                self.assertEqual(self.checker.update_current(bundle)["status"], "unknown")
        self.assertEqual(self.checker.update_current(None)["status"], "unknown")

    def test_invalidation_needs_new_observation_but_not_step_advance(self):
        initial = self.initial()
        for _ in range(3):
            self.assert_status("home")
        result = self.checker.invalidate_current("action_in_progress")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "action_in_progress")
        self.assertEqual(self.checker.update_current(initial)["reason"], "observation_invalidated")
        terminal = self.bundle()  # A rejected action can leave the native step unchanged.
        self.assertEqual(self.checker.update_current(terminal)["status"], "home")
        self.assertEqual(self.checker.update_current(terminal)["status"], "home")

    def test_regression_domain_change_and_same_id_conflict_do_not_lower_watermark(self):
        self.initial()
        current = self.bundle(self.state(10))
        self.checker.update_current(current)
        self.checker.invalidate_current("action_in_progress")
        self.assertEqual(self.result(self.state(9))["reason"], "observation_step_regressed")
        self.assertEqual(self.result(self.state(8))["reason"], "observation_step_regressed")
        state = self.state(10)
        state["step_domain"] = "different_domain"
        self.assertEqual(self.result(state)["reason"], "step_domain_changed")
        state = self.state(11)
        changed = self.bundle(state)
        changed["observation_id"] = current["observation_id"]
        self.assertEqual(self.checker.update_current(changed)["reason"], "observation_id_content_changed")
        self.assertEqual(self.result(self.state(10))["status"], "home")

    def test_reset_requires_matching_new_reference_and_rejects_old_episode(self):
        previous = dict(self.identity)
        old = self.initial()
        self.checker.invalidate_current("environment_reset")
        self.identity.update(episode_id="episode-2", env_generation=2)
        new_state = self.state()
        new_state["arms"]["left"]["ee_pose"]["position_m"] = [1, 2, 3]
        first = self.bundle(new_state)
        self.assertEqual(self.checker.update_current(first)["reason"], "initial_identity_mismatch")
        self.checker.capture_initial(first)
        self.assert_status("home")
        self.assertEqual(self.checker.check(previous)["status"], "unknown")
        self.assertEqual(self.checker.capture_initial(old)["status"], "unknown")
        self.assertEqual(self.checker.reference_record()["initial_observation_id"], first["observation_id"])
        self.assertEqual(self.checker.update_current(old)["status"], "unknown")
        self.assertEqual(self.checker.update_current(first)["status"], "home")

    def test_nonfinite_arithmetic_stays_json_serializable(self):
        initial = self.state()
        initial["arms"]["left"]["ee_pose"]["position_m"][0] = 1e308
        self.initial(initial)
        current = self.state(1)
        current["arms"]["left"]["ee_pose"]["position_m"][0] = -1e308
        result = self.result(current)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "position_difference_not_finite")
        json.dumps(result, allow_nan=False)
        json.dumps(self.checker.reference_record(), allow_nan=False)
        json.dumps(HOME_CHECK_POLICY, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
