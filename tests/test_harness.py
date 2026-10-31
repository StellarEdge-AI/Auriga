"""CPU synthetic fixture: mamba run -n Auriga python -m unittest discover -s tests -p test_harness.py -v."""

from copy import deepcopy
import asyncio
import hashlib
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
import zlib

from auriga.runtime.harness import Harness
from auriga.apps.gateway.workbench import Workbench
from auriga.robot.backends.robodojo.robodojo import FRAME_ID, TCP_CONFIG_ID
from auriga.core.storage.store import Store


class FixtureRobot:
    """Deterministic protocol fixture; no physics, IK, task state, or GPU."""

    def __init__(self, root, outcome="completed"):
        self.executions = 0
        self.outcome = outcome
        self.path = Path(root) / "synthetic-rgb.png"
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        self.path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                             + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))

    def start(self):
        return dict(data_origin="synthetic_fixture", frame_id=FRAME_ID, tcp_config_id=TCP_CONFIG_ID,
                    path_kinds=["backend_free"], max_waypoints=1, dual_arm=False, gripper_after_motion=True)

    def observe(self):
        return dict(data_origin="synthetic_fixture", world_kind="observed", sim_step=self.executions,
                    step_domain="adapter_control_step", sim_time_s=None, captured_at="2026-09-30T00:00:00Z",
                    cameras=[dict(camera_id="fixture_camera", png_path=str(self.path), width=1, height=1)],
                    robot_state=dict(data_origin="synthetic_fixture", source_kind="measurement",
                                     arms=dict(left=dict(q_rad=[0.0], ee_pose=dict(position_m=[0.009, 0, 1],
                                               quaternion_xyzw=[0, 0, 0, 1], source_kind="measurement")))))

    def execute(self, action, progress_callback=None, cancel_flag=None):
        self.executions += 1
        if progress_callback:
            progress_callback(dict(phase="started", sim_step=self.executions, data_origin="synthetic_fixture"))
        return dict(status=self.outcome, reason="synthetic_fixture", data_origin="synthetic_fixture",
                    terminal_observation=self.observe(), planned_trace=dict(data_origin="synthetic_fixture", targets=action["path"]),
                    dispatched_trace=dict(data_origin="synthetic_fixture", steps=1),
                    actual_trace=[dict(data_origin="synthetic_fixture", measured_fixture_state=self.observe()["robot_state"])])


class HarnessCheck(unittest.TestCase):
    def test_execution_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            identity = dict(run_id="synthetic-harness", episode_id="fixture-episode", env_instance_id="fixture-env", env_generation=1)
            store = Store(Path(temporary) / "store", identity)
            self.addCleanup(store.close)
            robot = FixtureRobot(temporary)
            harness = Harness(store, robot, max_actions=2)
            initial = harness.start()
            self.assertEqual(initial["data_origin"], "synthetic_fixture")
            instructed = harness.publish_observation(dict(robot.observe(), instruction="Pick up the visible target by 10 cm."))
            self.assertEqual(store.read("observation", instructed["observation_id"])["task_instruction"], "Pick up the visible target by 10 cm.")

            def candidate(identifier, **updates):
                action = dict(schema_version="0.1", **identity, world_kind="observed", action_id=identifier,
                              based_on_observation_id=harness.latest["observation_id"], frame_id=FRAME_ID,
                              tcp_config_id=TCP_CONFIG_ID, mode="absolute", limits_profile="simulation-slow-v1", timeout_s=10,
                              path=dict(kind="backend_free", orientation="backend_free", blend_radius_m=0,
                                        position_tolerance_m=0.005, waypoints=[dict(left=dict(position_m=[0.01, 0, 1], quaternion_xyzw=[0, 0, 0, 1]))]),
                              gripper_events=[])
                action.update(updates)
                return action

            action = candidate("fixture-action-1")
            first = harness.execute(action)
            self.assertEqual(first["receipt"]["status"], "completed")
            self.assertEqual(harness.execute(deepcopy(action)), first)
            self.assertEqual(robot.executions, 1)
            with self.assertRaisesRegex(ValueError, "action_id_content_conflict"):
                harness.execute(dict(action, timeout_s=11))
            terminal = store.read("observation", first["receipt"]["result_observation_id"])
            self.assertEqual(terminal["last_action_id"], action["action_id"])
            self.assertEqual(terminal["data_origin"], "synthetic_fixture")
            info, image = store.artifact(terminal["cameras"][0]["rgb_ref"])
            self.assertTrue(terminal["cameras"][0]["rgb_ref"].startswith("artifact:"))
            self.assertEqual(info["mime_type"], "image/jpeg")
            self.assertEqual(info["sha256"], hashlib.sha256(image.read_bytes()).hexdigest())
            delivered = harness.observation_content(terminal, dict(tool_call_id="fixture-call"))
            self.assertTrue(delivered[1]["imageUrl"].startswith("data:image/webp;base64,"))
            for identifier, updates, reason in [
                ("stale", dict(env_generation=2), "stale_environment_identity"),
                ("internal", dict(world_kind="internal"), "internal_action_cannot_execute_observed_world"),
                ("unsupported", dict(path=dict(kind="linear", waypoints=action["path"]["waypoints"])), "unsupported_capability"),
            ]:
                rejected = harness.execute(candidate(identifier, **updates))["receipt"]
                self.assertEqual(rejected["status"], "rejected")
                self.assertIn(reason, rejected["reason"])
            harness.request_stop()
            rejected = harness.execute(candidate("after-stop"))["receipt"]
            self.assertEqual(rejected["status"], "rejected")
            self.assertIn("run_stopping", rejected["reason"])
            self.assertEqual(robot.executions, 1)

            # A normally returned unknown acknowledgement must close admission too.
            unknown_store = Store(Path(temporary) / "unknown-store", identity)
            self.addCleanup(unknown_store.close)
            unknown_robot = FixtureRobot(Path(temporary) / "unknown-robot", outcome="unknown")
            unknown = Harness(unknown_store, unknown_robot)
            unknown.start()
            unresolved = candidate("unknown")
            unresolved["based_on_observation_id"] = unknown.latest["observation_id"]
            self.assertEqual(unknown.execute(unresolved)["receipt"]["status"], "unknown")
            self.assertFalse(unknown.accepting)
            followup = candidate("after-unknown")
            followup["based_on_observation_id"] = unknown.latest["observation_id"]
            self.assertEqual(unknown.execute(followup)["receipt"]["status"], "rejected")
            self.assertEqual(unknown_robot.executions, 1)
            # Supervision fixture: no Codex process. Clearing active_action is not
            # a stop confirmation when the last worker acknowledgement is unknown.
            supervisor = Workbench.__new__(Workbench)
            supervisor.harness, supervisor.store = unknown, unknown_store
            supervisor.session = SimpleNamespace(state="completed", interrupt=lambda: None)
            supervisor.control_responses = {}
            supervisor.status = "running"
            supervisor.starting = False
            response = asyncio.run(supervisor.stop("fixture-stop"))
            self.assertEqual(response["status"], "unknown")
            self.assertEqual(asyncio.run(supervisor.stop("fixture-stop")), response)
            supervisor.replay = False
            supervisor.robot = unknown_robot
            for operation in (supervisor.observer_evaluate, supervisor.observer_video):
                with self.assertRaisesRegex(ValueError, "observer_operation_requires_idle_run"):
                    operation()
            self.assertFalse(any(event["type"] == "observer.evaluated" for event in unknown_store.events()))


if __name__ == "__main__":
    unittest.main()

class RecoveryBudgetCheck(unittest.TestCase):
    def test_accepted_without_receipt_survives_fresh_generation_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            identity=dict(run_id='recovery',episode_id='old',env_instance_id='env',env_generation=1)
            store=Store(Path(temporary)/'store',identity)
            try:
                old=dict(schema_version='0.1',**identity,world_kind='observed',action_id='lost')
                store.save('action_request','lost',old)
                store.event('action.accepted',{},dict(action_id='lost'))
                identity.update(episode_id='fresh',env_generation=2)
                robot=FixtureRobot(temporary)
                harness=Harness(store,robot,max_actions=1)
                harness.accepting=True  # Explicitly loaded fresh physical generation.
                harness.start()
                self.assertEqual(harness.action_count,1)
                harness.capabilities.update(bounded_wait=dict(max_native_steps=50),path_kinds=['backend_free','hold'])
                result=harness.dispatch('robot_wait',dict(observation_id=harness.latest['observation_id'],native_steps=5),dict(tool_call_id='new'))
                receipt=__import__('json').loads(result['contentItems'][0]['text'])['receipt']
                self.assertEqual(receipt['status'],'rejected')
                self.assertIn('action_budget_exhausted',receipt['reason'])
                self.assertEqual(robot.executions,0)
                supervisor=Workbench.__new__(Workbench)
                supervisor.identity,supervisor.harness,supervisor.store=identity,harness,store
                self.assertFalse(supervisor.current_unknown_actions())
                self.assertEqual(harness.states['lost']['status'],'unknown')
            finally:store.close()

class ResetSafetyCheck(unittest.TestCase):
    def test_preflight_and_stop_during_reset(self):
        import threading
        with tempfile.TemporaryDirectory() as temp:
            identity=dict(run_id='reset-check',episode_id='old',env_instance_id='env',env_generation=1)
            store=Store(Path(temp)/'store',identity)
            robot=FixtureRobot(temp)
            entered,release=threading.Event(),threading.Event()
            try:
                harness=Harness(store,robot);harness.start();harness.capabilities['reset']=True
                def validate(seed,layout):
                    if layout==999:raise ValueError('reset_layout_unavailable')
                robot.validate_reset=validate
                with self.assertRaisesRegex(ValueError,'reset_layout_unavailable'):harness.reset(layout_id=999)
                self.assertTrue(harness.accepting);self.assertEqual(identity['env_generation'],1)
                def reset(seed=0,layout_id=0):
                    entered.set();release.wait(3);return {'observation':robot.observe()}
                robot.reset=reset
                results=[]
                thread=threading.Thread(target=lambda:results.append(harness.reset()))
                thread.start();self.assertTrue(entered.wait(2));harness.request_stop();release.set();thread.join(3)
                self.assertFalse(thread.is_alive());self.assertFalse(harness.accepting);self.assertTrue(harness.cancel.is_set())
                self.assertEqual(results[0]['env_generation'],2)
            finally:release.set();store.close()
