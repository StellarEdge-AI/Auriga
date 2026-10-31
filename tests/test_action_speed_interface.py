"""Actor speed arguments survive conversion and fail closed at shared boundaries."""
import unittest


from auriga.core.contracts import validate_action_speed
from auriga.robot.backends.robodojo.robodojo import validate_action as validate_native
from test_joint_replan_runtime import motion_action
from test_native_wait import wait_action


class ActionSpeedInterfaceTests(unittest.TestCase):
    def test_invalid_rates_and_contexts_rejected_before_execution(self):
        for field, bad_values in (("speed_scale", [True, None, "fast", 0, .24, 2.01, float("nan"), float("inf")]),
                                  ("gripper_speed_scale", [False, None, 0, 1.01, float("nan")])):
            for value in bad_values:
                with self.subTest(field=field, value=value):
                    action = motion_action(gripper=True)
                    action[field] = value
                    with self.assertRaises(ValueError): validate_action_speed(action)
                    with self.assertRaises(ValueError): validate_native(action)
        for action in (dict(wait_action(1), speed_scale=1),
                       dict(wait_action(1), gripper_speed_scale=.5),
                       dict(type="JointPrefixAction", speed_scale=.5),
                       dict(motion_action(), gripper_speed_scale=.5)):
            with self.assertRaises(ValueError): validate_action_speed(action)

    def test_unsupported_backend_refuses_nondefault_speed(self):
        action = dict(motion_action(), speed_scale=.5)
        for caps in ({}, {"action_speed": {"available": False}}):
            with self.assertRaisesRegex(ValueError, "unsupported_capability:action_speed"):
                validate_action_speed(action, caps)
        action["speed_scale"] = 1
        validate_action_speed(action, {})


if __name__ == "__main__":
    unittest.main()
