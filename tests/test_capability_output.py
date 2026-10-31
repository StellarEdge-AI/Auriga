"""Capability reuse must not hide updates or current native budgets."""
from copy import deepcopy
import json
import unittest

from auriga.core.presentation.capability_output import CapabilityDelivery
from auriga.runtime.tools.registry import validator_for
import test_dual_arm_interface as harness_fixture
from test_unified_dual_execution import _DualRuntime


class CapabilityDeliveryTests(unittest.TestCase):
    def test_reuses_only_identical_capabilities_in_identical_context(self):
        delivery = CapabilityDelivery()
        caps = dict(frame_id="env_origin", dual_arm=True, warnings=["fixture"])
        original = deepcopy(caps)
        epoch = dict(thread="a", compact=0)
        first = delivery.project(caps, epoch=epoch, current_action_budget={"remaining": 10})
        second = delivery.project(caps, epoch=epoch, current_action_budget={"remaining": 3})
        self.assertEqual(first["frame_id"], "env_origin")
        self.assertEqual(second["capabilities_delivery"]["status"], "unchanged")
        self.assertEqual(second["current_action_budget"], {"remaining": 3})
        self.assertEqual(first["capabilities_delivery"]["version"], second["capabilities_delivery"]["version"])
        epoch["compact"] = 1
        restored = delivery.project(caps, epoch=epoch, current_action_budget={})
        self.assertEqual(restored["frame_id"], "env_origin")
        caps["dual_arm"] = False
        changed = delivery.project(caps, epoch=epoch, current_action_budget={})
        self.assertFalse(changed["dual_arm"])
        self.assertNotEqual(changed["capabilities_delivery"]["version"], restored["capabilities_delivery"]["version"])
        first["warnings"].append("mutated output")
        self.assertEqual(caps["warnings"], original["warnings"])


class CapabilityHarnessTests(unittest.TestCase):
    setUp = harness_fixture.DualArmHarnessTests.setUp

    def read(self, args=None, correlation=None):
        result = self.h.dispatch("robot_capabilities", args or {}, correlation or {})
        return json.loads(result["contentItems"][0]["text"])

    def test_program_and_full_reads_do_not_consume_model_first_delivery(self):
        self.assertEqual(self.read(correlation={"program_id": "fixture"}), self.h.capabilities)
        self.assertEqual(self.read({"detail": "full"}), self.h.capabilities)
        first = self.read()
        self.assertEqual(first["capabilities_delivery"]["status"], "inline")
        self.assertEqual(self.read()["capabilities_delivery"]["status"], "unchanged")
        self.assertEqual(self.read({"detail": "full"}), self.h.capabilities)

    def test_context_reset_and_live_budget_are_honored(self):
        self.h.native_budget_known, self.h.native_budget_limit, self.h.native_action_count = True, 100, 2
        self.read()
        self.h.native_action_count = 99
        self.assertEqual(self.read()["current_action_budget"]["native_steps_remaining"], 1)
        for reset in (lambda: self.store.set_state("runtime", "image_context_epoch", "new"),
                      lambda: setattr(self.h, "thread_id", "new-thread"),
                      lambda: self.h.identity.update(env_generation=2)):
            reset()
            self.assertEqual(self.read()["capabilities_delivery"]["status"], "inline")

    def test_native_pose_guidance_reaches_actor_with_current_stage_fields(self):
        self.h.capabilities = _DualRuntime().adapter.capabilities()
        public = {item['name']: item for item in self.h.tool_registry.catalog(self.h, role='actor')}
        schema = public['robot_action']['inputSchema']
        stage_schema = next(part['then'] for part in schema['properties']['stages']['items']['allOf']
                            if part['if']['properties']['kind']['const'] == 'move')
        for detail in ('summary', 'full'):
            with self.subTest(detail=detail):
                reply = self.h.invoke('robot_capabilities', {'detail': detail}, {}, role='actor')
                actor = self.h.actor_output.present(reply)
                text = json.loads(actor['contentItems'][0]['text'])['tool_geometry']['conversion']
                self.assertIn("kind='move'", text)
                self.assertNotIn('move_ee', text)
                for field in ('id', 'arm', 'position_m', 'quaternion_xyzw', 'grasp_position_m', 'angles_deg'):
                    self.assertIn(field, text)
                    self.assertIn(field, stage_schema['properties'])
        for pose in (dict(position_m=[-.3, 0, 1], quaternion_xyzw=[0, 0, 0, 1]),
                     dict(grasp_position_m=[-.3, 0, 1], angles_deg=[0, 0, 0])):
            validator_for(schema).validate(dict(mode='preview', observation_id='current-observation',
                stages=[dict(id='left_move', kind='move', arm='left', **pose)]))
        self.assertEqual(self.robot.executions, 0)


if __name__ == "__main__":
    unittest.main()
