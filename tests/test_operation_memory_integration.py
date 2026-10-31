"""A real Harness receipt remains immutable through actor aliases and memory record."""
import json
from pathlib import Path
import tempfile
import unittest

from auriga.core.contracts import digest
from auriga.runtime.harness import Harness
from auriga.robot.backends.robodojo.robodojo import FRAME_ID, TCP_CONFIG_ID
from auriga.core.storage.store import Store
from test_harness import FixtureRobot


class OperationMemoryIntegrationTests(unittest.TestCase):
    def test_actor_records_the_actual_backend_receipt_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            identity = dict(run_id="memory-integration", episode_id="fixture", env_instance_id="env", env_generation=1)
            store = Store(Path(temporary) / "task", identity)
            self.addCleanup(store.close)
            robot = FixtureRobot(temporary)
            harness = Harness(store, robot)
            harness.start()
            action = dict(schema_version="0.1", **identity, world_kind="observed",
                action_id="action-" + "a" * 32, based_on_observation_id=harness.latest["observation_id"],
                frame_id=FRAME_ID, tcp_config_id=TCP_CONFIG_ID, mode="absolute",
                limits_profile="simulation-slow-v1", timeout_s=10,
                path=dict(kind="backend_free", orientation="backend_free", blend_radius_m=0,
                    position_tolerance_m=.005,
                    waypoints=[dict(left=dict(position_m=[.01, 0, 1], quaternion_xyzw=[0, 0, 0, 1]))]),
                gripper_events=[])
            result = harness.execute(action)
            receipt = store.read("action_receipt", action["action_id"])
            self.assertNotIn("action_id", receipt)
            original_digests = (digest(store.read("action_request", action["action_id"])), digest(receipt))
            shown = harness.actor_output.present(dict(action_id=action["action_id"],
                observation_id=result["receipt"]["result_observation_id"]))
            self.assertEqual(shown["action_id"], "act_1")
            harness.thread_id = "thread"
            response = harness.handle_tool(dict(threadId="thread", turnId="turn", callId="remember",
                tool="operation_memory", arguments=dict(request=dict(kind="record", scope="task",
                    operation="retreat", summary="Inspect the measured end state after withdrawing.",
                    action_ids=[shown["action_id"]], observation_ids=[shown["observation_id"]]))))
            self.assertTrue(response["success"])
            record = json.loads(response["contentItems"][0]["text"])["record"]
            self.assertEqual(record["physical_outcome"], "not_verified")
            self.assertEqual(record["facts"][0]["action_id"], shown["action_id"])
            self.assertEqual(len(store.records("operation_memory")), 1)
            self.assertEqual(original_digests,
                (digest(store.read("action_request", action["action_id"])),
                 digest(store.read("action_receipt", action["action_id"]))))
            self.assertEqual(robot.executions, 1)


if __name__ == "__main__":
    unittest.main()
