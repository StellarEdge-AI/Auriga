"""Initialization and actor retrieval use the same physical knowledge interface."""
from program_fixture import program_services
import base64
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile
from types import MethodType
from unittest.mock import patch

from auriga.runtime.harness import Harness
from auriga.cortex.context.actor_instructions import INSTRUCTIONS, TASK_INSTRUCTIONS
from auriga.cortex.context.task_planning import PLANNING_INSTRUCTIONS
from auriga.core.presentation.actor_presentation import ActorPresentation
from auriga.core.presentation.actor_output import ActorOutput
from auriga.core.observation_policy import ACTOR_PROFILE, LEGACY_ACTOR_PROFILE
from auriga.runtime.programs.runner import ProgramRunner
from auriga.robot.model.robot_information import robot_information, robot_resource_path
from auriga.core.storage.store import Store
from test_harness import FixtureRobot
from test_task_notes import fixture_workbench
from test_robot_calibration_information import CalibratedRobot


def text_result(response):
    return json.loads(response["contentItems"][0]["text"])


def wire_information(workbench):
    harness = workbench.harness
    harness.capabilities["robot_model_id"] = "arx-x5"
    harness.store = workbench.store
    harness.actor_presentation = ActorPresentation(workbench.store)
    harness.actor_output = ActorOutput(workbench.store, harness.actor_presentation)
    harness._archive_tool_result = MethodType(Harness._archive_tool_result, harness)
    harness.robot_information = MethodType(Harness.robot_information, harness)


class RobotInformationStartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_unregistered_robot_does_not_claim_to_supply_a_urdf(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbench, terminal = fixture_workbench(Path(temporary) / "run")
            wire_information(workbench)
            workbench.harness.capabilities["robot_model_id"] = "unregistered"
            try:
                with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal):
                    await workbench.start("fixture")
                instructions = workbench.session.start.call_args.args[1]
                data = json.loads(instructions.split("\n\nHarness instructions\n", 1)[0]
                                  .removeprefix("Robot physical information\n"))
                self.assertFalse(data["available"])
                self.assertNotIn("urdf", data)
                self.assertNotIn("Robot URDF", instructions)
                self.assertNotIn("The original URDF is supplied above", instructions)
            finally:
                await workbench.close()

    async def test_default_precedes_instructions_and_task_notes_are_last(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbench, terminal = fixture_workbench(Path(temporary) / "run", default_task_notes=False)
            wire_information(workbench)
            robot = CalibratedRobot(Path(temporary))
            published = Harness(workbench.store, robot, observation_profile=ACTOR_PROFILE)
            published.start()
            # Exercise startup with the actual publication path and its exact
            # episode artifacts, while keeping the process-free Workbench shell.
            for field in ("identity", "observation_profile", "latest"):
                setattr(workbench.harness, field, deepcopy(getattr(published, field)))
            workbench.harness.latest["task_instruction"] = "Fixture native task."
            capture_count = robot.observations
            try:
                with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal):
                    await workbench.start("unused", benchmark=True, task_notes="Keep task notes last.",
                                          task_notes_source="https://example.test/unneeded-source")
                instructions = workbench.session.start.call_args.args[1]
                prefix, behavior = instructions.split("\n\nHarness instructions\n", 1)
                metadata, urdf = prefix.removeprefix("Robot physical information\n").split("\n\nRobot URDF\n", 1)
                data = json.loads(metadata)
                self.assertNotIn("urdf", data)
                self.assertNotIn("joints", data["reference"]["kinematics"])
                with zipfile.ZipFile(robot_resource_path(resource_id="geometry_model")) as archive:
                    self.assertEqual(urdf.encode("utf-8"), archive.read("X5A.urdf"))
                self.assertEqual(instructions.count(urdf), 1)
                data["urdf"] = urdf
                self.assertEqual(data, workbench.harness.robot_information())
                self.assertNotIn("dynamics", data["reference"])
                self.assertEqual(data["reference"]["identity"]["native_action_dimensions"], 14)
                self.assertNotIn("planning_collision", data["reference"]["geometry"])
                self.assertRegex(data["reference"]["geometry"]["resource_ref"], r"^artifact:ref_\d+$")
                calibration = data["calibration"]
                self.assertTrue(calibration["available"], calibration)
                self.assertEqual(workbench.harness.actor_output.restore(calibration["observation_id"]),
                                 published.latest["observation_id"])
                for arm in ("left", "right"):
                    camera = arm + "_wrist"
                    self.assertEqual(calibration["bases"][arm]["T_world_base"],
                                     robot.last_raw["camera_pose_sample"]["cameras"][camera]["T_world_base"])
                    self.assertNotEqual(calibration["bases"][arm]["T_world_base"],
                                        robot.last_raw["camera_calibration"]["cameras"][camera]["T_world_base"])
                    self.assertEqual(calibration["cameras"][camera]["K"],
                                     robot.last_raw["camera_calibration"]["cameras"][camera]["K"])
                self.assertNotIn("default_mounts", data["reference"]["kinematics"])
                self.assertEqual(robot.observations, capture_count)
                self.assertEqual(robot.executions, 0)
                self.assertTrue(behavior.startswith(TASK_INSTRUCTIONS))
                expected_execution = INSTRUCTIONS.replace(
                    "Use native shell and file tools for computation, files and memory inside your sandbox.\n"
                    "Read the public Harness implementation under /harness. Install Python packages with python3 -m pip;\n"
                    "packages and working files stay in your sandbox. Web search and general network access are unavailable.\n",
                    "Use native shell and file tools for computation, files and memory in your working directory.\n"
                    f"Read the public Harness implementation under {workbench.root / 'private' / 'actor-source'}.\n"
                    "Code runs without OS sandbox isolation. Web search is disabled.\n",
                )
                self.assertLess(behavior.index(PLANNING_INSTRUCTIONS), behavior.index(expected_execution))
                self.assertEqual(behavior.count(PLANNING_INSTRUCTIONS), 1)
                prompt = workbench.session.run_turn.call_args.args[0]
                self.assertTrue(prompt.endswith("\nTask notes:\nKeep task notes last."))
                self.assertNotIn("https://", prompt)
                self.assertNotIn("unneeded-source", prompt)
                self.assertNotRegex(instructions, r"\b[0-9a-f]{40,}\b|sha256|source_url|source_filename")
                self.assertEqual(workbench.store.read_state("runtime", "robot_information"), data)
                self.assertEqual(sum(e["type"] == "robot_information.initialized" for e in workbench.store.events()), 1)
                for name in ("execute", "reset", "evaluate"):
                    getattr(workbench.robot, name).assert_not_called()
            finally:
                await workbench.close()

    async def test_resume_reinitializes_information_without_starting_an_extra_task_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            first, terminal = fixture_workbench(root)
            wire_information(first)
            try:
                with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal):
                    await first.start("fixture")
            finally:
                await first.close()
            resumed, terminal = fixture_workbench(root, resume=True)
            wire_information(resumed)
            try:
                with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal):
                    await resumed.start("unused")
                self.assertTrue(resumed.session.start.call_args.args[1].startswith("Robot physical information\n"))
                self.assertEqual(resumed.session.start.call_args.args[1].count(robot_information()["urdf"]), 1)
                resumed.session.run_turn.assert_not_called()
                self.assertEqual(sum(e["type"] == "robot_information.initialized" for e in resumed.store.events()), 2)
            finally:
                await resumed.close()


class RobotInformationToolTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        identity = dict(run_id="physical", episode_id="ep", env_instance_id="env", env_generation=1)
        self.store = Store(self.root / "run", identity)
        self.addCleanup(self.store.close)
        self.robot = FixtureRobot(self.root)
        self.harness = Harness(self.store, self.robot)
        self.harness.start()
        self.harness.capabilities["robot_model_id"] = "arx-x5"
        self.harness.thread_id = "thread"
        self.counter = 0

    def call(self, tool="robot_information", arguments=None):
        self.counter += 1
        return self.harness.handle_tool(dict(threadId="thread", turnId="turn", callId=f"call-{self.counter}",
                                             tool=tool, arguments=arguments or {}))

    def test_tool_and_initial_read_match_including_legacy_static_knowledge(self):
        self.assertEqual(self.harness.observation_profile, LEGACY_ACTOR_PROFILE)
        default = text_result(self.call())
        self.assertEqual(default, self.harness.robot_information())
        self.assertFalse(default["calibration"]["available"])
        static = deepcopy(default)
        static.pop("calibration")
        static["reference"]["geometry"].pop("resource_ref")
        self.assertEqual(static, robot_information())
        section = text_result(self.call(arguments=dict(section="kinematics")))
        self.assertEqual(section["reference"]["kinematics"],
                         robot_information(section="all")["reference"]["kinematics"])
        self.assertIn("declared_limits", section["reference"]["kinematics"]["joints"][0])
        self.assertNotIn("joints", default["reference"]["kinematics"])
        self.assertNotIn("briefing", section)
        self.assertFalse(text_result(self.call(arguments=dict(robot_id="another-arm")))["available"])
        self.harness.capabilities["robot_model_id"] = "unregistered"
        self.assertFalse(text_result(self.call())["available"])
        self.assertEqual(self.robot.executions, 0)
        self.assertEqual(self.harness.native_action_count, 0)

    def test_illustration_and_cad_are_only_returned_when_requested(self):
        self.assertEqual(len(self.call()["contentItems"]), 1)
        image = self.call(arguments=dict(section="geometry_image"))
        self.assertEqual(image["contentItems"][1]["type"], "inputImage")
        resources = text_result(self.call(arguments=dict(section="resources")))
        self.assertEqual(len(resources["resources"]), 5)
        resource = text_result(self.call(arguments=dict(section="resource", resource_id="base_step")))
        self.assertRegex(resource["resource_ref"], r"^artifact:ref_\d+$")
        self.assertNotRegex(json.dumps(resource), r'"(?:source|sources|source_url|source_filename|sha256|hash)"|https?://|\b[0-9a-f]{40,}\b')
        raw_ref = self.harness.actor_presentation.restore(resource["resource_ref"])
        self.assertTrue(self.store.resolve_artifact(raw_ref)[1].startswith(b"ISO-10303-21;"))
        self.assertFalse(text_result(self.call(arguments=dict(section="resource", resource_id="../unknown")))["available"])
        self.assertEqual(self.robot.executions, 0)

    def test_invalid_arguments_have_explicit_errors_without_actions(self):
        cases = [
            (dict(section=[]), "invalid_tool_arguments"),
            (dict(section=""), "invalid_tool_arguments"),
            (dict(robot_id=[]), "invalid_tool_arguments"),
            (dict(section="resource"), "robot_resource_id_required"),
            (dict(resource_id="base_step"), "robot_resource_id_requires_resource_section"),
            (dict(unregistered_field=True), "invalid_tool_arguments"),
        ]
        for arguments, error in cases:
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, error):
                self.call(arguments=arguments)
        self.assertEqual(self.robot.executions, 0)

    def test_program_artifact_reads_keep_short_references_and_remove_metadata(self):
        self.harness.observation_profile = ACTOR_PROFILE
        nested = self.store.put_artifact(b"binary", "rgb", "application/octet-stream")
        raw = json.dumps(dict(source_sha256="a"*64, source_url="https://example.test/model",
                              joint_angles=[1.23456789012345], nested_ref=nested)).encode()
        reference = self.store.put_artifact(raw, "robot_model", "application/json")
        short = self.harness.actor_presentation.present(reference)
        runner = ProgramRunner(program_services(self.harness))
        self.addCleanup(runner.close)
        entry = dict(cancel=threading.Event(), deadline=time.monotonic()+10, call_lock=threading.RLock())
        result = runner.dispatch(entry, dict(artifact_ref=short))
        data = json.loads(base64.b64decode(result["data_base64"]))
        self.assertEqual(data["joint_angles"], [1.23456789012345])
        self.assertNotRegex(json.dumps(result["artifact"])+json.dumps(data), r"sha256|https://|\b[0-9a-f]{40,}\b")
        self.assertRegex(data["nested_ref"], r"^artifact:ref_\d+$")
        self.assertEqual(self.store.resolve_artifact(reference)[1], raw)
        # A public robot knowledge tool must not lift the old profile's exact
        # scene/camera calibration or evaluator-artifact restrictions.
        self.harness.observation_profile = LEGACY_ACTOR_PROFILE
        with self.assertRaisesRegex(ValueError, "calibrated_artifact"):
            runner.dispatch(entry, dict(artifact_ref=short))

    def test_observation_and_capabilities_remove_hashes_but_keep_usable_evidence(self):
        self.harness.capabilities.update(source_sha256="a"*64, source_url="https://example.test/manual")
        observation = text_result(self.call("robot_observe", dict(detail="full")))
        capabilities = text_result(self.call("robot_capabilities", dict(detail="full")))
        self.assertNotRegex(json.dumps([observation, capabilities]),
                            r'"(?:sha256|source_sha256|source_url)"|https?://|\b[0-9a-f]{40,}\b')
        self.assertEqual(observation["robot_state"]["source_kind"], "measurement")
        reference = observation["bundle"]["cameras"][0]["rgb_ref"]
        self.assertRegex(reference, r"^artifact:ref_\d+$")
        original = self.harness.actor_presentation.restore(reference)
        runner = ProgramRunner(program_services(self.harness))
        self.addCleanup(runner.close)
        entry = dict(cancel=threading.Event(), deadline=time.monotonic()+10, call_lock=threading.RLock())
        result = runner.dispatch(entry, dict(artifact_ref=reference))
        self.assertEqual(base64.b64decode(result["data_base64"]), self.store.resolve_artifact(original)[1])
        self.assertEqual(self.robot.executions, 0)

    def test_real_actor_program_can_reread_default_and_follow_an_alias(self):
        runner = ProgramRunner(program_services(self.harness))
        self.harness.programs = runner
        self.addCleanup(runner.close)
        reference = self.store.put_artifact(b'{"sha256":"discard","position_m":[1,2,3]}', "computed", "application/json")
        alias = self.harness.actor_presentation.present(reference)
        source = ("import json, io, zipfile, xml.etree.ElementTree as ET\nfrom auriga_client import AurigaClient\nc=AurigaClient()\n"
                  "r=c.call('robot_information', {})\np=json.loads(r['contentItems'][0]['text'])\n"
                  "assert p['available'] and p['reference']['identity']['native_action_dimensions']==14\n"
                  "with zipfile.ZipFile(io.BytesIO(c.artifact(p['reference']['geometry']['resource_ref']))) as z:\n"
                  "    assert p['urdf'].encode('utf-8') == z.read(p['reference']['geometry']['assembly_member'])\n"
                  "    assert 'joints' not in p['reference']['kinematics']\n"
                  "    arm=ET.fromstring(z.read(p['reference']['geometry']['assembly_member']))\n"
                  "    assert len(arm.findall('link')) == 11\n"
                  "    assert z.read('meshes/link7.STL')[84:]\n"
                  f"a=json.loads(c.artifact({alias!r}))\n"
                  "assert a == {'position_m':[1,2,3]}\nprint('physical_reference_and_alias_read')\n")
        result = runner.run(dict(source=source, timeout_s=10, max_calls=1),
            dict(thread_id="thread", turn_id="turn", tool_call_id="program", item_id="program"))
        self.assertEqual(result["status"], "succeeded", self.store.resolve_artifact(result["stderr_ref"])[1])
        self.assertIn(b"physical_reference_and_alias_read", self.store.resolve_artifact(result["stdout_ref"])[1])
        self.assertEqual(self.robot.executions, 0)

    def test_default_and_calibration_tool_deliver_current_exact_calibration(self):
        robot = CalibratedRobot(self.root)
        harness = Harness(self.store, robot, observation_profile=ACTOR_PROFILE)
        harness.start()
        harness.capabilities["robot_model_id"] = "arx-x5"
        harness.thread_id = "thread"
        self.harness = harness
        calls = robot.observations
        default = text_result(self.call())
        calibration = text_result(self.call(arguments=dict(section="calibration")))
        self.assertTrue(calibration["available"], calibration)
        self.assertEqual(default["calibration"], calibration["calibration"])
        self.assertEqual(calibration["calibration"]["cameras"]["left_wrist"]["K"],
                         robot.last_raw["camera_calibration"]["cameras"]["left_wrist"]["K"])
        self.assertEqual(harness.actor_output.restore(calibration["calibration"]["observation_id"]),
                         harness.latest["observation_id"])
        self.assertFalse(text_result(self.call(arguments=dict(section="calibration", robot_id="another-arm")))["available"])
        self.assertEqual(robot.observations, calls)
        self.assertEqual(robot.executions, 0)


if __name__ == "__main__":
    unittest.main()
