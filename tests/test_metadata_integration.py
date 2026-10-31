"""Integration boundaries for session metadata delivery."""
import json
from dataclasses import replace
from auriga.core.tool_output import StructuredText, ToolResponse
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_dual_arm_interface as harness_fixture


class MetadataHarnessTests(unittest.TestCase):
    setUp = harness_fixture.DualArmHarnessTests.setUp

    def read(self, **kwargs):
        return json.loads(self.h.observation_content(self.h.latest, {}, **kwargs)[0]["text"])

    def test_overwritten_temporal_metadata_and_program_reads_do_not_consume_baseline(self):
        self.read(metadata_delivery=False)
        self.h.observation_content(self.h.latest, {"program_id": "fixture"})
        self.read(detail="full")
        first = self.read()
        self.assertNotIn("metadata_delivery", first)
        repeated = self.read()
        self.assertIn("metadata_delivery", repeated)
        self.assertEqual(repeated["current_action_budget"], first["current_action_budget"])
        self.assertEqual(repeated["camera_pose_sample"], first["camera_pose_sample"])

    def test_model_context_and_environment_resets_restore_inline_conventions(self):
        self.read()
        self.assertIn("metadata_delivery", self.read())
        for reset in (lambda: self.store.set_state("runtime", "image_context_epoch", "compacted"),
                      lambda: setattr(self.h, "thread_id", "replacement"),
                      lambda: self.h.identity.update(env_generation=2)):
            reset()
            self.assertNotIn("metadata_delivery", self.read())
            self.assertIn("metadata_delivery", self.read())

    def test_context_loss_during_reply_assembly_falls_back_to_inline(self):
        self.read()
        with patch.object(self.h._metadata_delivery, "commit", return_value=False):
            self.assertNotIn("metadata_delivery", self.read())
        epoch = self.h._model_context_epoch()
        with patch.object(self.h, "_model_context_epoch", side_effect=[epoch, (*epoch, "compaction")]):
            self.assertNotIn("metadata_delivery", self.read())

    def test_structured_delivery_fallback_updates_the_actual_actor_payload(self):
        self.read()
        with patch.object(self.h._metadata_delivery, "commit", return_value=False):
            parts = self.h.observation_content(self.h.latest, {}, structured=True)
        self.assertIsInstance(parts[0], StructuredText)
        self.assertNotIn("metadata_delivery", parts[0].value)
        shown = self.h.actor_output.present(ToolResponse(parts))
        self.assertNotIn("metadata_delivery", json.loads(shown["contentItems"][0]["text"]))
        self.assertEqual([part["type"] for part in shown["contentItems"]],
            ["inputText"] + [part["type"] for part in parts[1:]])

    def test_invalid_optional_structured_projection_cannot_commit_a_baseline(self):
        self.read()
        owner = self.h._metadata_delivery
        prepare = owner.prepare
        def corrupt(*args, **kwargs):
            return replace(prepare(*args, **kwargs), metadata={"bad": object()})
        with patch.object(owner, "prepare", side_effect=corrupt), patch.object(owner, "commit") as commit:
            parts = self.h.observation_content(self.h.latest, {}, structured=True)
        commit.assert_not_called()
        self.assertNotIn("metadata_delivery", parts[0].value)
        self.assertIn("robot_state", parts[0].value)
        self.assertIsNot(owner, self.h._metadata_delivery)

    def test_optional_projection_errors_keep_complete_reply_and_reset_baseline(self):
        for operation in ("prepare", "commit", "serialization"):
            with self.subTest(operation=operation):
                self.read()
                owner = self.h._metadata_delivery
                options = (dict(return_value=SimpleNamespace(metadata={"bad": object()}))
                           if operation == "serialization" else dict(side_effect=RuntimeError("optional_fault")))
                with patch.object(owner, "prepare" if operation == "serialization" else operation, **options):
                    value = self.read()
                self.assertNotIn("metadata_delivery", value)
                self.assertIn("robot_state", value)
                self.assertIn("current_action_budget", value)
                self.assertIsNot(owner, self.h._metadata_delivery)
                self.assertNotIn("metadata_delivery", self.read())


if __name__ == "__main__":
    unittest.main()
