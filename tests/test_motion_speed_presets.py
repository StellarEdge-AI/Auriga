"""Canonical speed choice is shared by raw actions, convenience tools and previews."""

from copy import deepcopy
import unittest

from auriga.core.contracts import digest, validate_action_speed
from auriga.robot.motion.motion_speed import ARM_PRESETS, GRIPPER_SPEED_PRESETS, resolve_action_speed


def move(**selection):
    return dict(type="EEAction", path=dict(kind="backend_free", waypoints=[{"left": {}}]),
                gripper_events=[], **selection)


def grip(**selection):
    return dict(type="EEAction", path=dict(kind="hold", waypoints=[]),
                gripper_events=[dict(arm="left", phase="after_motion", goal_normalized=0.)],
                **selection)


class MotionSpeedPresetTests(unittest.TestCase):
    def test_fixed_arm_presets_override_run_default_and_resolve_to_float(self):
        self.assertEqual(ARM_PRESETS, dict(slower=.5, slow=.8, careful=.9, normal=1., fast=1.25, faster=1.5))
        for preset, scale in ARM_PRESETS.items():
            for default in (1., 1.25, 1.5, 2.):
                result = resolve_action_speed(move(speed_preset=preset), default)
                self.assertEqual(result["speed_scale"], scale)
                self.assertIs(type(result["speed_scale"]), float)
                self.assertNotIn("speed_preset", result)
                self.assertNotIn("gripper_speed_scale", result)

    def test_jaw_presets_are_independent_from_arm_numeric_or_preset(self):
        self.assertEqual(GRIPPER_SPEED_PRESETS, dict(slower=.5, slow=.8, normal=1.))
        for preset, scale in GRIPPER_SPEED_PRESETS.items():
            action = move(speed_preset="faster", gripper_speed_preset=preset)
            action["gripper_events"] = grip()["gripper_events"]
            result = resolve_action_speed(action, 2.)
            self.assertEqual(result["speed_scale"], 1.5)
            self.assertEqual(result["gripper_speed_scale"], scale)
            self.assertNotIn("gripper_speed_preset", result)
            action.pop("speed_preset")
            action["speed_scale"] = .4
            self.assertEqual(resolve_action_speed(action)["speed_scale"], .4)

    def test_default_only_applies_to_omitted_arm_selection(self):
        self.assertEqual(resolve_action_speed(move(), 1.5)["speed_scale"], 1.5)
        self.assertEqual(resolve_action_speed(move(speed_scale=1), 1.5)["speed_scale"], 1.)
        self.assertEqual(resolve_action_speed(move(speed_preset="normal"), 1.5)["speed_scale"], 1.)
        action = move()
        action["gripper_events"] = grip()["gripper_events"]
        result = resolve_action_speed(action, 2.)
        self.assertEqual((result["speed_scale"], result["gripper_speed_scale"]), (2., 1.))

    def test_numeric_ranges_and_float_canonicalization(self):
        for number in (.25, .6, 1, 1.5, 2):
            result = resolve_action_speed(move(speed_scale=number))
            self.assertEqual(result["speed_scale"], number)
            self.assertIs(type(result["speed_scale"]), float)
        for number in (.25, .6, 1):
            result = resolve_action_speed(grip(gripper_speed_scale=number), 2.)
            self.assertEqual(result["gripper_speed_scale"], number)
            self.assertIs(type(result["gripper_speed_scale"]), float)
            self.assertNotIn("speed_scale", result)

    def test_same_axis_conflict_rejected_even_if_values_agree(self):
        for action in (move(speed_preset="normal", speed_scale=1),
                       grip(gripper_speed_preset="normal", gripper_speed_scale=1)):
            with self.assertRaisesRegex(ValueError, "conflicting_"):
                resolve_action_speed(action)

    def test_explicit_invalid_preset_and_numeric_never_fall_back(self):
        for invalid in (None, True, 1, [], {}, "", "FAST", "invalid"):
            with self.subTest(preset=invalid), self.assertRaisesRegex(ValueError, "invalid_speed_preset"):
                resolve_action_speed(move(speed_preset=invalid), 1.5)
            with self.subTest(jaw_preset=invalid), self.assertRaisesRegex(ValueError, "invalid_gripper_speed_preset"):
                resolve_action_speed(grip(gripper_speed_preset=invalid), 1.5)
        for invalid in (None, True, False, "1", float("nan"), float("inf"), 0, .24, 2.01):
            with self.subTest(scale=invalid), self.assertRaisesRegex(ValueError, "invalid_speed_scale"):
                resolve_action_speed(move(speed_scale=invalid), 1.5)
        for invalid in (None, True, "1", float("nan"), .24, 1.01):
            with self.subTest(jaw_scale=invalid), self.assertRaisesRegex(ValueError, "invalid_gripper_speed_scale"):
                resolve_action_speed(grip(gripper_speed_scale=invalid), 1.5)
        for invalid in ("fast", "faster"):
            with self.assertRaisesRegex(ValueError, "invalid_gripper_speed_preset"):
                resolve_action_speed(grip(gripper_speed_preset=invalid))

    def test_invalid_default_is_rejected(self):
        for invalid in (None, True, "1", float("nan"), float("inf"), .24, 2.01):
            with self.subTest(default=invalid), self.assertRaisesRegex(ValueError, "invalid_default_motion_speed_scale"):
                resolve_action_speed(move(), invalid)

    def test_wait_and_policy_unchanged_and_reject_all_explicit_selection_fields(self):
        wait = dict(type="EEAction", path=dict(kind="hold", waypoints=[]), hold_native_steps=5)
        prefix = dict(type="JointPrefixAction", actions=[[0.] * 14])
        for action in (wait, prefix):
            self.assertEqual(resolve_action_speed(action, 2.), action)
            for field, value in (("speed_scale", 1), ("speed_preset", "normal"),
                                 ("gripper_speed_scale", 1), ("gripper_speed_preset", "normal")):
                with self.subTest(kind=action["type"], field=field), self.assertRaisesRegex(ValueError, "unsupported_for_wait_or_joint_prefix"):
                    resolve_action_speed(action | {field: value}, 2.)

    def test_hold_has_only_jaw_default_and_empty_hold_is_unchanged(self):
        action = grip()
        self.assertEqual(resolve_action_speed(action, 2.), action | dict(gripper_speed_scale=1.))
        empty = dict(type="EEAction", path=dict(kind="hold", waypoints=[]), gripper_events=[])
        self.assertEqual(resolve_action_speed(empty, 2.), empty)
        for action in (grip(speed_preset="slow"), grip(speed_scale=.6)):
            with self.assertRaisesRegex(ValueError, "speed_scale_requires_motion_path"):
                resolve_action_speed(action)
        for action in (move(gripper_speed_preset="slow"), move(gripper_speed_scale=.6)):
            with self.assertRaisesRegex(ValueError, "gripper_speed_scale_requires_gripper_event"):
                resolve_action_speed(action)

    def test_no_mutation_and_repeated_resolution_preserves_effective_speed(self):
        action = move(speed_preset="fast")
        before = deepcopy(action)
        result = resolve_action_speed(action, 2.)
        self.assertEqual(action, before)
        self.assertIsNot(result, action)
        # The helper never mutates nested structures and deliberately copies
        # only the top-level selection fields; it is not a general deep clone.
        self.assertIs(result["path"], action["path"])
        self.assertEqual(resolve_action_speed(result, 1.), result)

    def test_equivalent_preset_numeric_default_have_identical_canonical_digest(self):
        explicit = resolve_action_speed(move(speed_scale=1))
        preset = resolve_action_speed(move(speed_preset="normal"), 2.)
        omitted = resolve_action_speed(move(), 1.)
        self.assertEqual(digest(explicit), digest(preset))
        self.assertEqual(digest(explicit), digest(omitted))

    def test_numeric_backend_boundary_rejects_unresolved_presets(self):
        for action in (move(speed_preset="normal"), grip(gripper_speed_preset="normal")):
            with self.assertRaisesRegex(ValueError, "unresolved_action_speed_preset"):
                validate_action_speed(action)
            validate_action_speed(resolve_action_speed(action))


if __name__ == "__main__":
    unittest.main()
