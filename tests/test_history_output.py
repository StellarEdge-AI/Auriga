"""Status indices and trace windows preserve immutable evidence and access scope."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from auriga.core.contracts import canonical
from auriga.core.presentation.actor_payload import lean_action_result
from auriga.core.presentation.history_output import action_status, list_actions, read_trace
from auriga.core.storage.store import Store


class HistoryOutputTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name), {})
        self.addCleanup(self.store.close)
        self.actions = {}
        self.harness = SimpleNamespace(store=self.store, action=lambda key: deepcopy(self.actions[key]))

    def interaction_fixture(self):
        self.harness.identity = dict(run_id="run", episode_id="episode", env_instance_id="env", env_generation=1)
        self.harness.observation_profile = "rgb_proprio_calibrated_v1"
        self.harness.latest = None

    def observation(self, name, *, identity=None, scope="actor", synchronized=True, profile=None):
        state = dict(sim_step=3 if synchronized else 2, step_domain="native", arms=dict(left=dict(
            q_rad=[.123456789], ee_pose=dict(position_m=[.1, .2, .3], quaternion_xyzw=[0, 0, 0, 1],
            source_kind="measurement"), gripper=dict(command_normalized=0., command_source_kind="command",
            position_m=[.01, -.01], position_source_kind="measurement"))))
        ref = self.store.put_artifact(canonical(state).encode(), "robot_state", "application/json", scope)
        observation = dict(identity or self.harness.identity, observation_id=name,
            observation_profile=profile or self.harness.observation_profile,
            sim_step=3, step_domain="native", robot_state_ref=ref, cameras=[])
        self.store.save("observation", name, observation)
        return observation

    def interaction(self, number, *, status="completed", before="before", after="after", **updates):
        request = dict(self.harness.identity, action_id=f"action-{number}", based_on_observation_id=before,
            path=dict(waypoints=[dict(position_m=[.123456789, 0., 1.])]), **updates)
        receipt = dict(status=status, executed_native_steps=0 if status == "rejected" else 3,
            execution_start_observation_id=before, result_observation_id=after)
        self.store.save("action_request", request["action_id"], request)
        self.actions[request["action_id"]] = dict(request=request, receipt=receipt)
        return self.actions[request["action_id"]]

    def test_event_anchors_keep_old_failures_gripper_intent_and_exact_page_gaps(self):
        self.interaction_fixture()
        self.observation("before")
        self.harness.latest = self.observation("after")
        for index in range(9):
            self.interaction(index, status="failed" if index == 2 else "completed",
                gripper_events=[dict(arm="left", goal_normalized=0.)] if index == 5 else [])
        before = deepcopy(self.actions)
        result = list_actions(self.harness, dict(anchor="events", limit=3), {})
        self.assertEqual([b["episode_action_index"] for b in result["actions"]], [0, 2, 5])
        self.assertEqual([b["omitted_actions_before"] for b in result["actions"]], [0, 1, 2])
        self.assertEqual((result["total"], result["episode_action_total"], result["omitted_actions_after"]), (4, 9, 3))
        self.assertEqual(result["overview"], dict(
            returned_count=3, selected_count=4, episode_action_total=9,
            status_counts=dict(completed=2, failed=1), execution_issue_count=1,
            gripper_command_count=1, evidence_gap_count=0, next_start=3))
        self.assertEqual(result["actions"][0]["measured_delta"], dict(left=dict(
            ee_position_delta_m=[0., 0., 0.], ee_step_distance_m=0.,
            gripper_position_delta_m=[0., 0.])))
        self.assertEqual(set(result["observations"]), {"before", "after"})
        self.assertTrue(result["observations"]["after"]["observation_is_current"])
        self.assertEqual(result["observations"]["before"]["robot_state"]["arms"]["left"]["q_rad"], [.123456789])
        self.assertEqual(result["actions"][2]["request"],
                         {key: before["action-5"]["request"][key]
                          for key in ("action_id", "based_on_observation_id")})
        self.assertNotIn("gripper_events", result["actions"][2]["request"])
        self.assertEqual(result["actions"][2]["receipt"], before["action-5"]["receipt"])
        self.assertEqual(result["actions"][2]["gripper_command_locations"], ["/gripper_events"])
        last = list_actions(self.harness, dict(anchor="events", start=result["next_start"], limit=3), {})
        self.assertEqual(last["actions"][0]["episode_action_index"], 8)
        self.assertEqual(last["actions"][0]["omitted_actions_before"], 8)
        self.assertEqual(last["omitted_actions_after"], 0)
        self.assertIsNone(last["next_start"])
        self.assertIsNone(last["overview"]["next_start"])
        self.assertEqual(self.actions, before)

    def test_interactions_keep_unknown_rejection_missing_boundaries_and_unexecuted_stages(self):
        self.interaction_fixture()
        self.observation("before")
        self.harness.latest = self.observation("newer")
        unknown = self.interaction(0, after=None)
        unknown.pop("receipt")
        rejected = self.interaction(1, status="rejected", after=None, stages=[dict(id="close", action=dict(
            gripper_events=[dict(arm="left", goal_normalized=0.)]))])
        rejected["receipt"].update(execution_start_observation_id=None,
            stage_results={"close": dict(status="not_executed")})
        partial = self.interaction(2)
        partial["receipt"].update(execution_uncertain=True, executed_native_steps=None,
                                 stop_confirmed=False, stage_results={"end": dict(status="unknown")})
        result = list_actions(self.harness, dict(anchor="failures"), {})
        self.assertEqual(len(result["actions"]), 3)
        self.assertEqual(result["actions"][0]["receipt"], {"status": "unknown"})
        self.assertIsNone(result["actions"][0]["observation_ids"]["result"])
        self.assertIn("execution_start_observation_not_recorded", result["actions"][1]["evidence_gaps"])
        self.assertEqual(result["actions"][1]["receipt"]["executed_native_steps"], 0)
        self.assertEqual(result["actions"][1]["gripper_command_locations"], ["/stages/0/action/gripper_events"])
        self.assertIsNone(result["actions"][2]["receipt"]["executed_native_steps"])
        self.assertNotIn("newer", result["observations"])
        self.assertNotIn("physical_outcome", canonical(result))
        self.assertEqual(list_actions(self.harness, dict(anchor="gripper_commands"), {})["total"], 1)
        for idx in range(3, 10):
            self.interaction(idx, status="failed")
        latest_failures = list_actions(self.harness, dict(anchor="failures", limit=3), {})
        self.assertEqual([b["episode_action_index"] for b in latest_failures["actions"]], [7, 8, 9])
        first_failures = list_actions(self.harness, dict(anchor="failures", start=0, limit=3), {})
        self.assertEqual([b["episode_action_index"] for b in first_failures["actions"]], [0, 1, 2])

    def test_interactions_are_episode_scoped_and_recheck_observation_permissions(self):
        self.interaction_fixture()
        foreign = dict(self.harness.identity, episode_id="another-episode")
        self.observation("foreign", identity=foreign)
        self.observation("private", scope="observer")
        self.observation("unsynchronized", synchronized=False)
        self.observation("wrong-profile", profile="rgb_proprio_v1")
        for number, reference in enumerate(("foreign", "private", "unsynchronized", "wrong-profile", "missing")):
            self.interaction(number, before=reference, after=None)
        for key, value in (("episode_id", "old-episode"), ("env_generation", 2), ("run_id", "old-run")):
            request = dict(self.harness.identity, action_id=key, **{key: value})
            self.store.save("action_request", key, request)
        result = list_actions(self.harness, dict(anchor="all"), {})
        self.assertEqual(result["episode_action_total"], 5)
        self.assertEqual(len(result["actions"]), 5)
        for observation in result["observations"].values():
            self.assertEqual(observation["status"], "unavailable")
            self.assertNotIn("robot_state", observation)
        self.assertNotIn("another-episode", canonical(result))

    def test_interaction_decision_and_execution_start_are_not_conflated(self):
        self.interaction_fixture()
        for reference in ("decision", "start", "result"):
            self.observation(reference)
        action = self.interaction(1, before="decision", after="result")
        action["receipt"]["execution_start_observation_id"] = "start"
        result = list_actions(self.harness, dict(anchor="all"), dict(program_id="program"))
        self.assertEqual(result["actions"][0]["observation_ids"],
                         dict(decision="decision", execution_start="start", result="result"))
        self.assertEqual(result["actions"][0]["evidence_gaps"], [])
        for arguments in (dict(anchor="invalid"), dict(anchor="all", detail="full")):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                list_actions(self.harness, arguments, {})

    def test_rejected_malformed_request_remains_retrievable(self):
        self.interaction_fixture()
        self.interaction(0, status="rejected", after=None, stages=[None, {}], arm_queues={"left": None})
        result = list_actions(self.harness, dict(anchor="events"), {})
        self.assertEqual(result["actions"][0]["receipt"]["status"], "rejected")
        self.assertEqual(result["actions"][0]["request"],
                         {"action_id": "action-0", "based_on_observation_id": "before"})
        self.assertNotIn("stages", result["actions"][0]["request"])

    def test_interaction_summary_compacts_request_and_receipt_provenance(self):
        self.interaction_fixture()
        self.observation("before")
        self.harness.latest = self.observation("after")
        action = self.interaction(0, status="stopped", after="after",
            input_digest="a" * 64,
            stages=[dict(id="close", action=dict(gripper_events=[dict(arm="left")]))])
        action["request"]["path"] = dict(waypoints=[dict(position_m=[.123456789] * 3)] * 20)
        action["receipt"].update(reason="partial_execution", actual_trace_ref="artifact:trace",
                                   failure_feedback=dict(witness=[1, 2, 3]))
        before = deepcopy(self.actions)
        result = list_actions(self.harness, dict(anchor="all"), {})
        block = result["actions"][0]
        self.assertEqual(block["request"], {"action_id": "action-0", "based_on_observation_id": "before"})
        self.assertNotIn("input_digest", canonical(block))
        self.assertNotIn("path", block["request"])
        self.assertNotIn("stages", block["request"])
        self.assertEqual(block["receipt"]["status"], "stopped")
        self.assertEqual(block["receipt"]["reason"], "partial_execution")
        self.assertEqual(block["receipt"]["failure_feedback"], {"witness": [1, 2, 3]})
        self.assertNotIn("actual_trace_ref", block["receipt"])
        self.assertTrue(block["receipt"]["actual_trace_available"])
        self.assertEqual(block["full_result"],
                         dict(tool="robot_action_status", action_id="action-0", detail="full"))
        self.assertEqual(self.actions, before)

    def add_action(self, number, trace=None, scope="actor"):
        action_id = "action-" + str(number)
        request = dict(action_id=action_id, based_on_observation_id="obs-" + str(number),
                       path=dict(waypoints=[dict(pose=[.123456789] * 7)]))
        receipt = dict(action_id=action_id, status="stopped", reason="tracking_guard",
                       executed_native_steps=4, completed_native_steps=3,
                       step_semantics=dict(executed_native_steps="native count"),
                       execution_interval=dict(start_sim_step=10, end_sim_step=22),
                       stop_confirmation=dict(status="unknown"), result_observation_id=None,
                       failure_feedback=dict(witness=[1, 2, 3]), versions=dict(worker="build"),
                       future_diagnostic=dict(unknown=True))
        if trace is not None:
            receipt["actual_trace_ref"] = self.store.put_artifact(canonical(trace).encode(), "actual_trace", "application/json", scope)
        self.store.save("action_request", action_id, request)
        self.actions[action_id] = dict(request=request, receipt=receipt)
        return action_id

    def test_status_keeps_failure_unknown_fields_precision_and_full_lookup(self):
        action_id = self.add_action(1, [])
        before = deepcopy(self.actions)
        result = action_status(self.harness, dict(action_id=action_id), {})
        self.assertEqual(result["request"], self.actions[action_id]["request"])
        for key in ("reason", "failure_feedback", "future_diagnostic", "stop_confirmation", "execution_interval"):
            self.assertEqual(result["receipt"][key], self.actions[action_id]["receipt"][key])
        self.assertEqual(result["full_result"], dict(tool="robot_action_status", action_id=action_id, detail="full"))
        for arguments, correlation in ((dict(action_id=action_id, detail="full"), {}),
                                       (dict(action_id=action_id), dict(program_id="p"))):
            self.assertEqual(action_status(self.harness, arguments, correlation), self.actions[action_id])
        self.assertEqual(self.actions, before)

    def test_list_paging_keeps_order_partial_counts_and_readable_full_receipts(self):
        ids = [self.add_action(index) for index in range(37)]
        first = list_actions(self.harness, {}, {})
        last = list_actions(self.harness, dict(start=first["next_start"]), {})
        self.assertEqual(first["total"], 37)
        self.assertEqual(len(first["actions"]), 25)
        self.assertIsNone(last["next_start"])
        self.assertEqual([item["action_id"] for item in first["actions"] + last["actions"]], ids)
        entry = first["actions"][0]
        self.assertEqual(entry["executed_native_steps"], 4)
        self.assertEqual(entry["completed_native_steps"], 3)
        self.assertEqual(entry["stop_confirmation"], dict(status="unknown"))
        self.assertTrue(entry["failure_feedback_available"])
        complete = list_actions(self.harness, dict(detail="full"), {})
        self.assertEqual(complete, list(self.actions.values()))
        self.assertEqual(list_actions(self.harness, {}, dict(program_id="p")), complete)
        self.assertIsInstance(list_actions(self.harness, dict(limit=1), dict(program_id="p")), dict)

    def test_trace_small_values_unchanged_large_sequences_paged_exactly(self):
        short = self.add_action("short", dict(samples=[dict(q_rad=[.123456789])]))
        self.assertEqual(read_trace(self.harness, dict(action_id=short, kind="actual_trace"), {}),
                         dict(samples=[dict(q_rad=[.123456789])]))
        samples = [dict(sim_step=i, q_rad=[.123456789 + i], status="unknown" if i == 27 else "measured") for i in range(61)]
        trace = dict(scope="measured", samples=samples, diagnostic=dict(reason="partial_execution"))
        action_id = self.add_action("long", trace)
        args = dict(action_id=action_id, kind="actual_trace")
        first = read_trace(self.harness, args, {})
        self.assertEqual(first["trace"]["samples"], samples[:25])
        self.assertEqual(first["trace"]["diagnostic"], trace["diagnostic"])
        self.assertEqual(first["windows"][0]["path"], "/samples")
        gathered = first["trace"]["samples"][:]
        cursor = first["windows"][0]["next_start"]
        while cursor is not None:
            page = read_trace(self.harness, {**args, "path": "/samples", "start": cursor}, {})
            gathered.extend(page["trace"])
            cursor = page["windows"][0]["next_start"]
        self.assertEqual(gathered, samples)
        self.assertEqual(read_trace(self.harness, {**args, "detail": "full"}, {}), trace)
        self.assertEqual(read_trace(self.harness, args, dict(program_id="p")), trace)

    def test_nested_paths_select_segment_arrays_without_executing_or_losing_metadata(self):
        trace = dict(segments=[dict(q_rad=[[i, i+.000000123] for i in range(40)], status="planned")],
                     **{"a/b": {"~x": [1, 2]}})
        action_id = self.add_action(1, trace)
        base = dict(action_id=action_id, kind="actual_trace")
        result = read_trace(self.harness, base, {})
        self.assertEqual(result["windows"][0]["path"], "/segments/0/q_rad")
        selected = read_trace(self.harness, {**base, "path": "/a~1b/~0x", "limit": 1}, {})
        self.assertEqual(selected["trace"], [1])
        for controls in (dict(path="not-pointer"), dict(path="/bad~2escape"), dict(path="/unknown"),
                         dict(start=1), dict(detail="full", limit=1), dict(limit=True), dict(start=-1)):
            with self.subTest(controls=controls), self.assertRaises(ValueError):
                read_trace(self.harness, {**base, **controls}, {})

    def test_trace_artifact_access_scope_is_not_weakened(self):
        action_id = self.add_action(1, dict(samples=[1, 2]), scope="private")
        with self.assertRaisesRegex(ValueError, "artifact_access_scope_denied"):
            read_trace(self.harness, dict(action_id=action_id, kind="actual_trace"), {})

    def test_limit_one_preserves_whole_samples_joint_vectors_poses_and_matrices(self):
        samples = [dict(sim_step=i, robot_state=dict(arms=dict(left=dict(
            q_rad=[i + j*.123456789 for j in range(6)],
            ee_pose=dict(position_m=[.1, .2, .3], quaternion_xyzw=[0, 0, 0, 1]),
            T_world_link=[[1, 0, 0, .1], [0, 1, 0, .2], [0, 0, 1, .3], [0, 0, 0, 1]]))))
                   for i in range(3)]
        for suffix, trace, sequence_path in (("root", samples, ""),
                                              ("samples", dict(samples=samples), "/samples")):
            action_id = self.add_action(suffix, trace)
            args = dict(action_id=action_id, kind="actual_trace", limit=1)
            first = read_trace(self.harness, args, {})
            actual = first["trace"] if suffix == "root" else first["trace"]["samples"]
            self.assertEqual(actual, samples[:1])
            self.assertEqual(len(first["windows"]), 1)
            gathered = []
            for index in range(len(samples)):
                page = read_trace(self.harness, {**args, "path": sequence_path, "start": index}, {})
                gathered.extend(page["trace"])
            self.assertEqual(gathered, samples)
        planned = dict(segments=[dict(arm="left", q_rad=[[i+j*.123456789 for j in range(6)] for i in range(3)],
                                     target_position_m=[.1, .2, .3], target_quaternion_xyzw=[0, 0, 0, 1])])
        action_id = self.add_action("planned", planned)
        result = read_trace(self.harness, dict(action_id=action_id, kind="actual_trace", limit=1), {})
        segment = result["trace"]["segments"][0]
        self.assertEqual(segment["q_rad"], planned["segments"][0]["q_rad"][:1])
        self.assertEqual(len(segment["q_rad"][0]), 6)
        self.assertEqual(segment["target_position_m"], [.1, .2, .3])
        self.assertEqual(segment["target_quaternion_xyzw"], [0, 0, 0, 1])
        self.assertEqual([window["path"] for window in result["windows"]], ["/segments/0/q_rad"])

    def test_passed_guard_is_summarized_but_rejection_and_unknown_diagnostics_survive(self):
        from auriga.robot.motion.motion_safety import assess_motion
        passed = assess_motion([0]*6, [[.123456789]*6], joint_names=list("abcdef"),
                               joint_types=["continuous"]*6, request_remaining=100, episode_remaining=90)
        rejected = assess_motion([0]*6, [[7]*6], joint_names=list("abcdef"),
                                 joint_types=["continuous"]*6, request_remaining=100, episode_remaining=90)
        # Even in a passed guard, skipped or future per-joint diagnostic values
        # cannot disappear merely because the parent was admitted.
        passed["periodic_checks"][0] = dict(joint_name="a", status="unknown_limits", eligible=False)
        passed["periodic_checks"][1]["future_warning"] = "unrecognized"
        checks = [dict(status="pass", compiled_sim_step=12, waypoint_index=0,
                       prior_executed_native_steps=0, arms=dict(left=passed)),
                  dict(status="reject", compiled_sim_step=17, waypoint_index=1,
                       prior_executed_native_steps=5, arms=dict(left=rejected))]
        original = dict(request=dict(action_id="action"), receipt=dict(status="failed", reason="motion_safety_rejected",
                            executed_native_steps=5, stop_confirmation=dict(status="held"),
                            motion_safety_checks=checks, motion_safety=checks[1]))
        before = deepcopy(original)
        result = lean_action_result(original)
        actual = result["receipt"]["motion_safety_checks"]
        self.assertEqual(actual[1], checks[1])
        self.assertEqual(result["receipt"]["motion_safety"], checks[1])
        self.assertEqual(actual[0]["compiled_sim_step"], 12)
        arm = actual[0]["arms"]["left"]
        self.assertNotIn("joint_travel_rad", arm)
        self.assertEqual(arm["request_remaining_native_steps"], 100)
        self.assertEqual(arm["episode_remaining_native_steps"], 90)
        self.assertEqual(arm["periodic_checks"], passed["periodic_checks"][:2])
        self.assertEqual(arm["eligible_periodic_check_count"], 4)
        summary = result["receipt"]["motion_safety_check_summary"]
        self.assertEqual((summary["count"], summary["pass_count"], summary["last_status"]), (2, 1, "reject"))
        self.assertEqual(summary["full_result"], dict(tool="robot_action_status", action_id="action", detail="full"))
        self.assertEqual(original, before)


if __name__ == "__main__":
    unittest.main()
