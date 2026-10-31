import tempfile
from pathlib import Path
import unittest

from auriga.core.storage.store import Store
from auriga.runtime.actions.strategy_checkpoint import (adaptive_action_checkpoint,
    checkpoint_triggered, mechanism_family)


class StrategyCheckpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.identity = dict(run_id="checkpoint", episode_id="episode", env_instance_id="env", env_generation=1)
        self.store = Store(Path(self.tmp.name), self.identity)
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)

    def action(self, action_id, *, contact="pad_center", kind="BatchAction"):
        return dict(schema_version="0.1", **self.identity, world_kind="observed", type=kind,
            action_id=action_id, based_on_observation_id="obs", stages=[dict(
                id="move", arm="left", kind="move", action=dict(type="move", arm="left",
                    path=dict(contact_region=contact, waypoints=[[0, 0, 0]])))])

    def fail(self, action_id, reason="path_infeasible"):
        action = self.action(action_id)
        self.store.event("action.requested", {"request": action}, {"action_id": action_id})
        self.store.event("action.failed", {"status": "failed", "reason": reason}, {"action_id": action_id})

    def terminal(self, action_id, event_type, *, status="stopped", reason=None):
        action = self.action(action_id)
        self.store.event("action.requested", {"request": action}, {"action_id": action_id})
        payload = {"status": status}
        if reason is not None:
            payload["reason"] = reason
        self.store.event(event_type, payload, {"action_id": action_id})

    def test_repeated_failed_family_returns_short_warning_without_identifiers(self):
        self.fail("a1")
        self.fail("a2")
        checkpoint = adaptive_action_checkpoint(self.store, self.identity)
        self.assertEqual(checkpoint["repeated_failed_mechanism_count"], 2)
        self.assertTrue(checkpoint_triggered(checkpoint))
        text = str(checkpoint)
        self.assertNotIn("a1", text)
        self.assertNotIn("sha256", text)
        self.assertNotIn("episode", text)

    def test_changed_family_and_completed_action_clear_warning(self):
        self.fail("a1")
        self.fail("a2")
        self.assertIsNotNone(adaptive_action_checkpoint(self.store, self.identity))
        action = self.action("a3", contact="fingertip")
        self.store.event("action.requested", {"request": action}, {"action_id": "a3"})
        self.store.event("action.completed", {"status": "completed"}, {"action_id": "a3"})
        self.assertIsNone(adaptive_action_checkpoint(self.store, self.identity))

    def test_pending_plan_or_different_episode_does_not_block_or_warn(self):
        self.fail("a1")
        self.assertIsNone(adaptive_action_checkpoint(self.store, self.identity))
        other = dict(self.identity, episode_id="other")
        self.store.event("action.requested", {"request": dict(self.action("a2"), **other)}, {"action_id": "a2"})
        self.store.event("action.failed", {"status": "failed"}, {"action_id": "a2"})
        self.assertIsNone(adaptive_action_checkpoint(self.store, self.identity))

    def test_observation_checkpoints_are_not_failures(self):
        self.terminal("a1", "action.stopped", reason="observation_checkpoint")
        self.terminal("a2", "action.stopped", reason="observation_checkpoint")
        self.assertIsNone(adaptive_action_checkpoint(self.store, self.identity))

    def test_episode_stop_and_cancellation_are_not_failures(self):
        self.terminal("a1", "action.stopped", reason="episode_ended")
        self.terminal("a2", "action.stopped", reason="episode_ended")
        self.assertIsNone(adaptive_action_checkpoint(self.store, self.identity))
        self.terminal("a3", "action.cancelled", status="cancelled", reason="user_stop")
        self.assertIsNone(adaptive_action_checkpoint(self.store, self.identity))

    def test_abnormal_stop_still_warns(self):
        self.terminal("a1", "action.stopped", reason="tracking_error")
        self.terminal("a2", "action.stopped", reason="tracking_error")
        checkpoint = adaptive_action_checkpoint(self.store, self.identity)
        self.assertEqual(checkpoint["repeated_failed_mechanism_count"], 2)

        self.fail("a3")
        continued = adaptive_action_checkpoint(self.store, self.identity)
        self.assertFalse(checkpoint_triggered(continued))

    def test_checkpoint_reads_latest_tail_after_a_long_history(self):
        for index in range(1001):
            self.terminal(f"old-{index}", "action.completed", status="completed")
        self.fail("latest-a")
        self.fail("latest-b")
        checkpoint = adaptive_action_checkpoint(self.store, self.identity)
        self.assertEqual(checkpoint["repeated_failed_mechanism_count"], 2)


if __name__ == "__main__":
    unittest.main()
