"""Rejected calls keep codes and expose field-specific, evidence-safe repairs."""
from program_fixture import program_services

import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from jsonschema import Draft202012Validator

from auriga.core.tool_errors import error_payload, error_response, schema_input_error


class ToolErrorsTests(unittest.TestCase):
    def test_boolean_schema_rejections_remain_actionable_failures(self):
        for schema, value in [({'type': 'object', 'properties': {'blocked': False}}, {'blocked': 1}),
                ({'oneOf': [False, {'type': 'number'}]}, 'bad'),
                ({'not': True}, 1),
                ({'type': 'object', 'required': ['kind'], 'properties': {'kind': False}}, {}),
                ({'type': 'object', 'required': ['blocked'], 'properties': {'blocked': False}}, {})]:
            error = next(Draft202012Validator(schema).iter_errors(value))
            result = error_payload(schema_input_error(error))
            self.assertEqual(result['code'], 'invalid_tool_arguments')
            self.assertTrue(result['suggestion'])

    def test_action_errors_explain_actual_legal_fields_and_options(self):
        from auriga.runtime.tools.builtins import create_registry
        from auriga.core.tool_errors import ToolInputError
        registry = create_registry()
        cases = [
            ({'id': 'jaw', 'kind': 'gripper', 'arm': 'left', 'gripper': 1}, 'goal_normalized'),
            ({'id': 'look', 'kind': 'observe'}, 'nonblank reason'),
            ({'id': 'home', 'kind': 'home', 'arm': 'left'}, 'joint/home/reset'),
            ({'id': 'move', 'kind': 'move', 'arm': 'left'}, 'grasp_position_m, angles_deg OR position_m, quaternion_xyzw'),
            ({'id': 'move', 'kind': 'move', 'arm': 'left', 'grasp_position_m': [0,0,1], 'angles_deg': [0,91,0]}, 'maximum'),
            ({'id': 'jaw', 'kind': 'gripper', 'arm': 'left', 'goal_normalized': 1, 'speed_preset': 'slow'}, 'gripper_speed_preset')]
        for stage, expected in cases:
            with self.subTest(stage=stage):
                with self.assertRaises(ToolInputError) as caught:
                    registry.validate('robot_action', {'observation_id': 'obs1', 'stages': [stage]}, role='actor')
                payload = error_payload(caught.exception)
                self.assertIn(expected, payload['suggestion'])
                self.assertTrue(payload['field_path'].startswith('/stages/0'))
                self.assertNotIn('executed_native_steps', payload)

    def test_runtime_action_modes_give_repair_without_replay_claim(self):
        for code, required in [('cached_batch_commands_cannot_be_changed', 'expected_cursor'),
                ('robot_action_plan_unknown_or_expired', 'never replay'),
                ('inspect_requires_one_plan_action_or_trajectory_reference', 'exactly one'),
                ('trajectory_native_steps_and_pagination_are_mutually_exclusive', 'max_samples')]:
            payload = error_payload(ValueError(code), tool='robot_action', arguments={'mode': 'execute'})
            self.assertEqual(payload['code'], code)
            self.assertIn(required, payload['suggestion'])

    def test_unavailable_and_unknown_tools_supply_catalog_guidance(self):
        from auriga.runtime.tools.builtins import create_registry
        from auriga.core.tool_errors import ToolInputError
        registry = create_registry()
        with self.assertRaises(ToolInputError) as caught:
            registry.validate('robot_home', {}, role='actor')
        self.assertIn('robot_action', caught.exception.suggestion)
        from unittest.mock import patch
        with patch.object(registry, 'catalog', return_value=[{'name': 'robot_observe'}]):
            issue = registry._unavailable_error('depth_not_enabled_for_run', 'geometry_query', None, 'actor')
        self.assertIn('robot_observe', issue.suggestion)
        self.assertIn('cannot enable', issue.suggestion)
        self.assertIn('do not', issue.suggestion.lower())

    def test_schema_arrays_keep_index_and_actual_without_full_schema_echo(self):
        schema = dict(type="object", properties=dict(values=dict(type="array", items=dict(type="number", minimum=0))))
        error = next(Draft202012Validator(schema).iter_errors(dict(values=[1, -2])))
        payload = error_payload(error, tool="fixture", arguments=dict(values=[1, -2]))
        self.assertEqual(payload["field_path"], "/values/1")
        self.assertEqual(payload["actual"], -2)
        self.assertEqual(payload["code"], "invalid_tool_arguments")
        self.assertNotIn("Failed validating", payload["error"])

    def test_release_fix_does_not_fabricate_evidence_or_invalid_released_state(self):
        for state in ("retained", "released_unverified"):
            args = dict(plan=dict(stages=[dict(operation_context=dict(state=state))]))
            payload = error_payload(ValueError("operation_context_release_evidence_required"), tool="task_plan", arguments=args)
            self.assertEqual(payload["field_path"], "/plan/stages/0/operation_context/release_observation_id")
            self.assertEqual(payload["minimal_example"]["state"], "unknown")
            self.assertNotIn("release_observation_id", payload["minimal_example"])
            self.assertIn("do not fabricate", payload["suggestion"])

    def test_plain_runtime_error_stays_failure_and_does_not_claim_zero_motion(self):
        response = error_response(ValueError("worker_connection_lost"), tool="robot_action", arguments=dict(mode="direct"))
        self.assertFalse(response["success"])
        value = json.loads(response["contentItems"][0]["text"])
        self.assertEqual(value["error"], "worker_connection_lost")
        self.assertEqual(value["field_path"], "")
        self.assertIn("no motion or task outcome is inferred", value["outcome_semantics"])
        self.assertNotIn("executed_native_steps", value)

    def test_depth_refresh_guidance_is_accepted_and_keeps_registered_camera(self):
        from auriga.perception.depth.depth_runtime import DepthRuntime
        payload = error_payload(ValueError('depth_target_requires_refresh'), tool='depth_query',
            arguments={'request': {'kind': 'region_geometry', 'target_id': 'target'}})
        request = payload['minimal_example']['request']
        self.assertEqual(request['kind'], 'refresh_target')
        self.assertNotIn('camera_id', request)
        measured = []
        def measure(arguments, correlation):
            measured.append(arguments)
            return {'status': 'supported', 'measurement_id': 'measurement'}
        owner = SimpleNamespace(_current=lambda observation_id: None, _targets=lambda: {},
            _target=lambda identifier, current=False: {'target_id': identifier, 'camera_id': 'original_camera'},
            _measure=measure, _save_targets=lambda targets: None,
            store=SimpleNamespace(event=lambda *args: None))
        result = DepthRuntime._register(owner, request, {})
        self.assertEqual(result['target_status'], 'tracked')
        self.assertEqual(measured[0]['camera_id'], 'original_camera')
        with self.assertRaisesRegex(ValueError, 'invalid_depth_target_fields'):
            DepthRuntime._register(owner, dict(request, camera_id='replacement_camera'), {})

    def test_malformed_arguments_cannot_break_error_formatting(self):
        args = dict(stages=None, plan=[])
        response = error_response(ValueError("invalid_task_plan: provide plan"), tool="robot_action", arguments=args)
        self.assertFalse(response["success"])
        self.assertIn("error", json.loads(response["contentItems"][0]["text"]))

    def test_model_and_program_boundaries_keep_failure_status_and_machine_fields(self):
        from auriga.cortex.session.codex import RecordedClient
        from auriga.runtime.programs.runner import ProgramRunner
        from auriga.core.storage.store import Store
        from auriga.runtime.tools.builtins import create_registry
        registry = create_registry()
        def failure(*args):
            registry.validate('task_plan', arguments, role='actor')
            raise ValueError("operation_context_release_evidence_required")
        arguments = dict(plan=dict(stages=[dict(operation_context=dict(state="retained"))]))
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory), dict(run_id="errors", episode_id="ep", env_instance_id="env", env_generation=1))
            runner=ProgramRunner(program_services(SimpleNamespace(store=store, dispatch=failure)))
            entry=dict(cancel=threading.Event(), deadline=time.monotonic()+30, call_lock=threading.RLock(),
                       job_id="job", calls=0, max_calls=1, correlation={})
            try:
                model = RecordedClient._approval(SimpleNamespace(tool_handler=failure, store=store), "item/tool/call",
                    dict(tool="task_plan", arguments=arguments))
                self.assertFalse(model["success"])
                model_value = json.loads(model["contentItems"][0]["text"])
                result=runner.dispatch(entry, dict(operation_id="call", tool="task_plan", arguments=arguments))
                self.assertFalse(result["success"])
                self.assertEqual(json.loads(result["contentItems"][0]["text"]), model_value)
                self.assertEqual(runner.dispatch(entry, dict(operation_id="call", tool="task_plan", arguments=arguments)), result)
                self.assertEqual(entry["calls"], 1)
            finally:
                runner.close();store.close()


if __name__ == "__main__":
    unittest.main()
