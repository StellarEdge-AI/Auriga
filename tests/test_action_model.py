"""The probe preserves state semantics and rejects misbound model responses."""

import copy
from types import SimpleNamespace
import unittest

from scripts.probes.action_model_probe import probe_state, validate_actions


class ActionModelProbeCheck(unittest.TestCase):
    def test_state_provenance_and_response_identity(self):
        state = {"arms": {side: dict(source_kind="measurement",
            joint_names=[f"joint{i}" for i in range(1, 7)], q_rad=[offset + i / 10 for i in range(6)],
            gripper=dict(command_source_kind="command", command_normalized=offset,
                         position_source_kind="measurement", position_m=[0.123, 0.456]))
            for side, offset in (("left", 0), ("right", 1))}}
        values, fields = probe_state(state)
        self.assertEqual(values[:14], [0, .1, .2, .3, .4, .5, 0, 1, 1.1, 1.2, 1.3, 1.4, 1.5, 1])
        self.assertEqual(values[14:], [0] * 18)
        self.assertEqual(fields[6], "left/gripper:command_normalized")
        for field, value in (("q_rad", [float("nan")] * 6), ("source_kind", "command"),
                             ("joint_names", list(reversed(state["arms"]["left"]["joint_names"])))):
            bad = copy.deepcopy(state); bad["arms"]["left"][field] = value
            with self.assertRaises(ValueError): probe_state(bad)
        bad = copy.deepcopy(state); bad["arms"]["left"]["gripper"]["command_normalized"] = 2
        with self.assertRaises(ValueError): probe_state(bad)
        rows = [[i / 10] * 32 for i in range(50)]
        actions = [SimpleNamespace(get_action=lambda row=row: SimpleNamespace(tolist=lambda: row),
                    get_timestep=lambda i=i: 100 + i, get_timestamp=lambda i=i: 10.0 + i / 30)
                   for i, row in enumerate(rows)]
        self.assertEqual(validate_actions(actions, 100, 10.0)[0], rows)
        with self.assertRaisesRegex(ValueError, "identity"): validate_actions(actions, 101, 10.0)
        with self.assertRaisesRegex(ValueError, "identity"): validate_actions(actions, 100, 9.0)
        with self.assertRaisesRegex(ValueError, "length"): validate_actions(actions[:-1], 100, 10.0)
        rows[0][0] = float("inf")
        with self.assertRaisesRegex(ValueError, "finite"): validate_actions(actions, 100, 10.0)


if __name__ == "__main__":
    unittest.main()
