"""Native joint policy inputs cannot silently borrow base-model semantics."""

import copy
import unittest

from auriga.runtime.policy.action_policy import arx_state, validate_joint_chunk


class ActionPolicyCheck(unittest.TestCase):
    def test_native_joint_order_and_command_provenance(self):
        state = {"arms": {side: dict(source_kind="measurement", q_rad=[i + offset for i in range(6)],
            gripper=dict(command_source_kind="command", command_normalized=offset,
                         position_m=[0.02, -0.02])) for side, offset in (("left", 0), ("right", 1))}}
        self.assertEqual(arx_state(state), [0, 1, 2, 3, 4, 5, 0, 1, 2, 3, 4, 5, 6, 1])
        for field, value in (("source_kind", "command"), ("q_rad", [float("nan")] * 6),
                             ("joint_names", [f"joint{i}" for i in range(6, 0, -1)])):
            bad = copy.deepcopy(state)
            bad["arms"]["left"][field] = value
            with self.assertRaises(ValueError):
                arx_state(bad)
        bad = copy.deepcopy(state)
        bad["arms"]["right"]["gripper"]["command_source_kind"] = "measurement"
        with self.assertRaises(ValueError):
            arx_state(bad)

    def test_no_implicit_base_model_slicing_or_nonfinite_commands(self):
        with self.assertRaisesRegex(ValueError, "50_by_14"):
            validate_joint_chunk([[0] * 32 for _ in range(50)])
        chunk = [[0] * 14 for _ in range(50)]
        self.assertEqual(validate_joint_chunk(chunk), chunk)
        chunk[4][7] = float("inf")
        with self.assertRaisesRegex(ValueError, "finite"):
            validate_joint_chunk(chunk)


if __name__ == "__main__":
    unittest.main()
