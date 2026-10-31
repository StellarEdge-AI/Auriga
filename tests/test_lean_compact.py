"""Lossless compact encoding of model-facing tool replies (features.lean_compact)."""
import collections
import copy
import json
import unittest

from auriga.core.config import FeaturesConfig
from auriga.core.contracts import canonical
from auriga.core.presentation.lean_compact import (ENCODING, LeanCompact, UNCHANGED_PREFIX, encode_tables, expand_tables,
                                   integral_numbers, is_observation, restore_document)
from auriga.core.presentation.lean_projection import _image_labels, project_request, summarize_text

ARMS = ("left", "right")


def pose(x):
    return {"frame_id": "env_origin", "position_m": [x, -0.1326, 1.0686],
            "quaternion_xyzw": [-0.3267, 0.6272, 0.3265, 0.6271], "source_kind": "measurement",
            "tcp_config_id": "arx-x5-link6-v1"}


def derived(index):
    return {"ee_angular_acceleration_rad_s2": [-0.2173, 0.0833, -0.0007 * index],
            "ee_angular_velocity_rad_s": [0.0008, -0.0003, 0.0], "ee_frame_id": "env_origin",
            "ee_linear_acceleration_m_s2": [0.0161, 0.042, -0.1618], "ee_linear_velocity_m_s": [-0.0001, -0.0002, 0.0008],
            "interval_s": 0.04, "interval_sim_steps": [1760 + index, 1770 + index],
            "jaw_acceleration_m_s2": [0.0008, -0.0006], "jaw_velocity_m_s": [-0.0, 0.0],
            "joint_acceleration_rad_s2": [-0.0002, 0.4052, -0.3954, 0.227, 0.001, 0.0004 * index],
            "joint_acceleration_source": "measured_joint_velocity_difference",
            "source_kind": "finite_difference_at_measurement_boundaries"}


def measured_samples():
    samples = []
    for index in range(6):
        arms = {}
        for side, arm in enumerate(ARMS):
            record = {"jaw_command_normalized": 0.3544 + side, "jaw_goal_normalized": 0.0,
                      "measured_ee_pose": pose(-0.36 + 0.01 * index + side), "phase": "measured",
                      "q_rad": [0.1947, 1.7832 + index / 100, 1.7253, -1.5133, 0.0, 0.8056],
                      "qdot_rad_s": [0.001, -0.2912, 0.6963, -0.6431, -0.0023, -0.0035 * index]}
            if index:
                record["derived_motion"] = derived(index)
            arms[arm] = record
        samples.append({"arms": arms, "elapsed_time_s": 0.64 * index, "native_step": 16 * index,
                        "sim_step": 1130 + 160 * index})
    return samples


def planned_samples():
    samples = [{"arms": {arm: {key: value for key, value in record.items() if key != "derived_motion"}
                         for arm, record in measured_samples()[0]["arms"].items()},
                "execution_state": "initial_measurement", "native_step": 0}]
    for index in range(1, 6):
        samples.append({"arms": {arm: {"jaw_goal_normalized": 1.0, "phase": "move",
                                       "q_rad": [-0.364, 1.9276, 1.7582 + index / 10, -1.4003, 0.0, -0.364]}
                                 for arm in ARMS},
                        "execution_state": "completed_command", "native_step": 16 * index})
    return samples


def stages():
    result = {}
    for index, arm in enumerate(("left", "right", "left")):
        result[f"{arm}_move{index}"] = {
            "arm": arm, "completed_steps": 16, "dispatch_attempted_steps": 16,
            "endpoint_error": {"orientation_error_rad": 0.0006, "position_error_m": 0.0002 * index},
            "grasp_contact_feedback": {"arms": {arm: {
                "actual_contact_position_m": [-0.3102, 0.0649, 0.8998 + index], "approach_shortfall_m": -0.0001,
                "available": True, "contact_depth_m": 0.0, "contact_region": "pad_center", "offset_m": 0.1501,
                "orientation_error_rad": 0.0006, "position_error_m": 0.0003,
                "position_error_vector_m": [-0.0002, -0.0001, -0.0002], "requested_contact_position_m": [-0.31, 0.065, 0.9]}},
                "frame_id": "env_origin", "source_kind": "derived_from_terminal_measured_link6",
                "target_scope": "requested_final_pose_not_completion_claim"},
            "kind": "move", "planned_steps": 16, "status": "completed"}
    result["close"] = {"arm": "left", "completed_steps": 0, "kind": "gripper", "planned_steps": 0, "status": "completed"}
    result["check"] = {"arm": None, "completed_steps": 0, "kind": "observe", "planned_steps": 0, "status": "completed"}
    return result


def camera_documents(step, wrist_x=-0.2996):
    calibration = {"cameras": {name: {"K": [[288.1325, 0.0, 320.0], [0.0, 336.4643, 240.0], [0.0, 0.0, 1.0]],
                                      "calibration_source": "native rendered Camera pinhole parameters",
                                      "distortion": [0.0] * 5, "image_size": [640, 480]}
                               for name in ("cam_head", "cam_left_wrist", "cam_right_wrist")},
                   "profile_id": "robodojo-arx-x5-pinhole-v1", "reference_frame_id": "env_origin"}
    poses = {"cameras": {"cam_head": {"T_world_camera": [[1.0, 0.0, 0.0, 0.0], [0.0, -0.866, 0.5, -0.41],
                                                         [0.0, -0.5, -0.866, 1.308], [0.0, 0.0, 0.0, 1.0]],
                                      "frame_id": "head", "source_kind": "simulator_camera_transform"},
                         "cam_left_wrist": {"T_world_camera": [[1.0, 0.0001, -0.0003, wrist_x], [0.0003, -0.5, 0.866, -0.2675],
                                                               [0.0, -0.866, -0.5, 0.9724], [0.0, 0.0, 0.0, 1.0]],
                                            "arm": "left", "frame_id": "left", "source_kind": "robot_kinematics"},
                         "cam_right_wrist": {"T_world_camera": [[1.0, 0.0001, -0.0003, 0.3004], [0.0003, -0.5, 0.866, -0.2675],
                                                                [0.0, -0.866, -0.5, 0.9724], [0.0, 0.0, 0.0, 1.0]],
                                             "arm": "right", "frame_id": "right", "source_kind": "robot_kinematics"}},
             "reference_frame_id": "env_origin", "sim_step": step}
    return calibration, poses


def observation(observation_id, step, wrist_x=-0.2996):
    calibration, poses = camera_documents(step, wrist_x)
    cameras = [{"calibration_available": True, "camera_id": name, "capture_time": "t" + str(step), "clock_domain": None,
                "control_dt_s": 0.004, "frame_id": "raw-" + name, "height": 480, "pixel_transform": "identity",
                "sim_step": step, "width": 640} for name in ("cam_head", "cam_left_wrist", "cam_right_wrist")]
    arms = {arm: {"ee_pose": pose(0.3 if arm == "right" else -0.3),
                  "grasp_pose": {"angles_deg": [90.0, -0.0, 0.0173], "contact_depth_m": 0.0,
                                 "contact_point_meaning": "center_between_fingers_at_distal_15mm_pad_center",
                                 "convention": "robot_move_grasp", "position_m": [-0.2996, -0.2022, 0.9215 + step / 1e4]},
                  "gripper": {"joint_names": ["joint7", "joint8"], "position_m": [0.044, 0.044]},
                  "joint_names": [f"joint{i}" for i in range(1, 7)], "q_rad": [0.0, -0.0, 0.0, 0.0, -0.0, step / 1e4]}
            for arm in ARMS}
    return {"bundle": {"cameras": cameras, "observation_id": observation_id, "sim_step": step,
                       "task_instruction": "Arrange the numbers from left to right.", "world_kind": "observed"},
            "camera_calibration": calibration, "camera_pose_sample": poses,
            "geometry_delivery": {"mode": "current_camera_geometry", "omitted": "Reconstructive mount matrices omitted."},
            "observation_is_current": True,
            "robot_state": {"arms": arms, "frame_id": "env_origin", "sim_step": step}}


def execute_value(action_id="a1"):
    return {"action_id": action_id, "receipt": {"status": "completed", "result_observation_id": "obs_9"},
            "stages": stages(),
            "trajectory_feedback": {
                "final_measured_jaws": {"arms": {"left": {"joint_names": ["joint7", "joint8"], "position_m": [0.0255, 0.0258]}}},
                "joint_names": {arm: [f"joint{i}" for i in range(1, 7)] for arm in ARMS},
                "limitations": ["No object/contact truth or replanning."],
                "measured": {"available": True, "label": "measured_action", "samples": measured_samples(),
                             "measurement_timing": {"derivatives": "Finite differences over recorded boundary intervals.",
                                                    "physics_dt_s": 0.004, "source_kind": "recorded_sim_step",
                                                    "status": "available", "step_domain": "adapter_control_step"},
                             "timing": "Recorded measurement boundary index.", "total_samples": 65},
                "planned": {"available": True, "samples": planned_samples(), "timing": "Stored native command rows."}}}


def reply(*values, images=1):
    items = []
    for value in values:
        items.append(dict(type="inputText", text=canonical(value)))
        items.extend(dict(type="inputImage", imageUrl="data:image/png;base64,AAAA") for _ in range(images))
    return dict(success=True, contentItems=items)


def texts(response):
    return [json.loads(item["text"]) for item in response["contentItems"] if item["type"] == "inputText"]


def numbers(value, out=None):
    out = collections.Counter() if out is None else out
    if isinstance(value, bool) or value is None:
        return out
    if isinstance(value, (int, float)):
        out[float(value) + 0.0] += 1
    elif isinstance(value, dict):
        for item in value.values():
            numbers(item, out)
    elif isinstance(value, list):
        for item in value:
            numbers(item, out)
    return out


class Reader:
    """What a reader of the context can rebuild: tables and camera documents."""

    def __init__(self):
        self.full = {}  # observation id -> camera documents given in full

    def read(self, value):
        value = expand_tables(value)
        value.pop("encoding", None)
        self._restore(value)
        return value

    def _restore(self, value):
        if isinstance(value, dict):
            if is_observation(value):
                documents = {key: value.get(key) for key in ("camera_calibration", "camera_pose_sample")}
                if all(isinstance(document, dict) and "unchanged_since" not in document for document in documents.values()):
                    self.full.setdefault(value["bundle"]["observation_id"], copy.deepcopy(documents))
                else:
                    reference = self.full[value["unchanged_since"]]
                    for key, document in documents.items():
                        value[key] = restore_document(reference[key], document)
            for item in value.values():
                self._restore(item)
        elif isinstance(value, list):
            for item in value:
                self._restore(item)


class TableEncodingTest(unittest.TestCase):
    def test_samples_stages_and_cameras_round_trip_exactly(self):
        value = execute_value()
        value["observation"] = observation("obs_2", 0)
        encoded = encode_tables(copy.deepcopy(value))
        measured = encoded["trajectory_feedback"]["measured"]["samples"]
        self.assertEqual(measured["table"], "per_sample_arm")
        self.assertEqual(len(measured["rows"]), 12)
        columns = measured["columns"]
        for column in ("native_step", "sim_step", "elapsed_time_s", "arm", "phase", "q_rad", "qdot_rad_s",
                       "measured_ee_pose.position_m", "derived_motion.joint_acceleration_rad_s2", "derived_motion.interval_s"):
            self.assertIn(column, columns)
        self.assertEqual(measured["const"]["measured_ee_pose.frame_id"], "env_origin")
        self.assertEqual(measured["const"]["derived_motion.joint_acceleration_source"], "measured_joint_velocity_difference")
        self.assertNotIn("frame_id", canonical(measured["rows"]))
        self.assertEqual(encoded["trajectory_feedback"]["planned"]["samples"]["table"], "per_sample_arm")
        table = encoded["stages"]
        self.assertEqual(table["table"], "per_key")
        self.assertIn("grasp_contact_feedback.arms.@arm.offset_m", table["columns"])
        self.assertIn("arm", table.get("nulls", []))
        self.assertEqual(encoded["observation"]["bundle"]["cameras"]["table"], "per_item")
        self.assertEqual(canonical(expand_tables(encoded)), canonical(value))
        self.assertLess(len(canonical(encoded)), len(canonical(value)) * 0.7)

    def test_unencodable_values_stay_unchanged(self):
        # A column that is null in one row and absent in another cannot be a table.
        samples = [{"arms": {"left": {"q": None}}, "native_step": 0}, {"arms": {"left": {}}, "native_step": 1},
                   {"arms": {"left": {"q": 1.5}}, "native_step": 2}]
        value = {"samples": samples, "items": [{"a.b": 1, "c": 2}, {"a.b": 3, "c": 4}, {"a.b": 5, "c": 6}]}
        self.assertEqual(canonical(encode_tables(copy.deepcopy(value))), canonical(value))

    def test_integral_numbers_keep_values(self):
        value = {"a": [1.0, -0.0, 0.25, 3], "b": {"c": 2.0, "d": True}}
        self.assertEqual(canonical(integral_numbers(value)), '{"a":[1,0,0.25,3],"b":{"c":2,"d":true}}')


class ContextEncodingTest(unittest.TestCase):
    def sequence(self, compact):
        observe = reply(observation("obs_1", 0), images=3)
        execute = reply(execute_value("a1"), observation("obs_2", 160, wrist_x=-0.25), images=1)
        execute2 = reply(execute_value("a2"), observation("obs_3", 320, wrist_x=-0.2), images=1)
        return [compact.apply("robot_observe", {}, observe, epoch="e1", thread="t"),
                compact.apply("robot_action", {"mode": "execute"}, execute, epoch="e1", thread="t"),
                compact.apply("robot_action", {"mode": "execute"}, execute2, epoch="e1", thread="t")], \
               [observe, execute, execute2]

    def test_every_number_is_recoverable(self):
        outputs, inputs = self.sequence(LeanCompact(anchor_period=8))
        reader = Reader()
        for output, original in zip(outputs, inputs):
            self.assertEqual(len(output["contentItems"]), len(original["contentItems"]))
            for encoded, value in zip(texts(output), texts(original)):
                self.assertEqual(numbers(reader.read(encoded)), numbers(value))
            # Images are untouched and stay in place.
            self.assertEqual([item for item in output["contentItems"] if item["type"] == "inputImage"],
                             [item for item in original["contentItems"] if item["type"] == "inputImage"])

    def test_camera_metadata_full_once_then_references(self):
        outputs, _ = self.sequence(LeanCompact(anchor_period=8))
        first = texts(outputs[0])[0]
        self.assertIsInstance(first["camera_calibration"], dict)
        self.assertIsInstance(first["camera_pose_sample"], dict)
        later = texts(outputs[1])[1]
        self.assertEqual(later["camera_calibration"], UNCHANGED_PREFIX + "obs_1")
        poses = later["camera_pose_sample"]
        self.assertEqual(poses["unchanged_since"], "obs_1")
        self.assertEqual(poses["cameras"]["cam_head"], "unchanged")
        self.assertEqual(poses["cameras"]["cam_right_wrist"], "unchanged")
        self.assertEqual(set(poses["cameras"]["cam_left_wrist"]), {"T_world_camera"})
        self.assertEqual(poses["sim_step"], 160)
        self.assertEqual(later["unchanged_since"], "obs_1")
        self.assertNotIn("task_instruction", later["bundle"])
        self.assertNotIn("convention", later["robot_state"]["arms"]["left"]["grasp_pose"])
        self.assertIn("position_m", later["robot_state"]["arms"]["left"]["grasp_pose"])
        # Camera identity stays in every observation (image labels depend on it).
        self.assertEqual(expand_tables(later)["bundle"]["cameras"][1]["camera_id"], "cam_left_wrist")

    def test_new_context_gives_full_metadata_again(self):
        compact = LeanCompact(anchor_period=8)
        self.sequence(compact)
        output = compact.apply("robot_action", {}, reply(execute_value("a3"), observation("obs_4", 480)),
                               epoch="e2", thread="t")
        later = texts(output)[1]
        self.assertIsInstance(later["camera_calibration"], dict)
        self.assertIn("task_instruction", later["bundle"])
        timing = texts(output)[0]["trajectory_feedback"]["measured"]["measurement_timing"]
        self.assertIn("derivatives", timing)

    def test_reference_outside_an_anchor_is_renewed_after_its_block_closes(self):
        compact = LeanCompact(anchor_period=8)
        observe = lambda name, step: texts(compact.apply("robot_observe", {}, reply(observation(name, step)),
                                                        epoch="e", thread="t"))[0]
        compact.apply("geometry_query", {}, reply({"status": "ok"}), epoch="e", thread="t")  # call 0 (anchor)
        self.assertIsInstance(observe("obs_1", 0)["camera_calibration"], dict)  # call 1: full, reference
        self.assertEqual(observe("obs_2", 10)["camera_calibration"], UNCHANGED_PREFIX + "obs_1")  # call 2
        for _ in range(5):  # calls 3..7
            compact.apply("geometry_query", {}, reply({"status": "ok"}), epoch="e", thread="t")
        # Call 8 opens the next block: call 1 is summarised, so the metadata is given in full again.
        renewed = observe("obs_3", 20)
        self.assertIsInstance(renewed["camera_calibration"], dict)
        self.assertIn("task_instruction", renewed["bundle"])
        self.assertEqual(observe("obs_4", 30)["camera_calibration"], UNCHANGED_PREFIX + "obs_3")
        for _ in range(14):  # calls 10..23
            compact.apply("geometry_query", {}, reply({"status": "ok"}), epoch="e", thread="t")
        # Call 8 is an anchor; its copy is never summarised and stays the reference.
        self.assertEqual(observe("obs_5", 40)["camera_calibration"], UNCHANGED_PREFIX + "obs_3")
        trailing = LeanCompact(anchor_period=0)
        trailing.apply("robot_observe", {}, reply(observation("obs_1", 0)), epoch="e", thread="t")
        late = texts(trailing.apply("robot_observe", {}, reply(observation("obs_2", 10)), epoch="e", thread="t"))[0]
        self.assertIsInstance(late["camera_calibration"], dict)
        self.assertEqual(late["bundle"]["cameras"]["table"], "per_item")

    def test_full_detail_reply_is_unchanged_and_not_a_reference(self):
        compact = LeanCompact(anchor_period=8)
        full = reply(observation("obs_1", 0))
        self.assertIs(compact.apply("robot_observe", {"detail": "full"}, full, epoch="e", thread="t"), full)
        later = texts(compact.apply("robot_observe", {}, reply(observation("obs_2", 10)), epoch="e", thread="t"))[0]
        self.assertIsInstance(later["camera_calibration"], dict)

    def test_measurement_timing_prose_once_per_context(self):
        outputs, _ = self.sequence(LeanCompact(anchor_period=8))
        first = texts(outputs[1])[0]["trajectory_feedback"]["measured"]["measurement_timing"]
        second = texts(outputs[2])[0]["trajectory_feedback"]["measured"]["measurement_timing"]
        self.assertIn("derivatives", first)
        self.assertEqual(second, {"physics_dt_s": 0.004, "status": "available"})
        self.assertNotIn("joint_names", texts(outputs[2])[0]["trajectory_feedback"])

    def test_one_encoding_line_per_changed_reply(self):
        self.assertLessEqual(len(ENCODING), 120)
        outputs, _ = self.sequence(LeanCompact(anchor_period=8))
        for output in outputs[1:]:
            self.assertEqual(sum(1 for value in texts(output) if value.get("encoding") == ENCODING), 1)

    def test_deterministic(self):
        first, _ = self.sequence(LeanCompact(anchor_period=8))
        second, _ = self.sequence(LeanCompact(anchor_period=8))
        self.assertEqual(canonical(first), canonical(second))

    def test_switch_off_and_exempt_replies_are_unchanged(self):
        self.assertFalse(FeaturesConfig().lean_compact)
        compact = LeanCompact(anchor_period=8)
        for tool, arguments, response in (
                ("geometry_query", {}, reply(execute_value())),
                ("history_view_observation", {"detail": "full"}, reply(observation("obs_1", 0))),
                ("robot_observe", {"detail": "full"}, reply(observation("obs_1", 0))),
                ("program_run", {}, dict(success=True, contentItems=[dict(type="inputText", text="aborted by user")]))):
            self.assertIs(compact.apply(tool, arguments, response, epoch="e", thread="t"), response)

    def test_failed_reply_is_also_encoded_losslessly(self):
        value = execute_value()
        value["stages"]["left_move0"]["status"] = "failed"
        value["failure_feedback"] = {"failed_stage_id": "left_move0", "candidates": [{"seed": 1, "score": 0.5}] * 3}
        response = dict(reply(value), success=False)
        output = LeanCompact(anchor_period=8).apply("robot_action", {}, response, epoch="e", thread="t")
        self.assertFalse(output["success"])
        encoded = texts(output)[0]
        self.assertEqual(encoded["stages"]["table"], "per_key")
        self.assertEqual(numbers(Reader().read(encoded)), numbers(value))


class ProjectionCompatibilityTest(unittest.TestCase):
    def test_summaries_and_image_labels_read_encoded_replies(self):
        value = execute_value()
        value["stages"]["right_move1"]["status"] = "failed"
        compact = LeanCompact(anchor_period=8)
        compact.apply("robot_observe", {}, reply(observation("obs_1", 0)), epoch="e", thread="t")
        output = compact.apply("robot_action", {}, reply(value, observation("obs_2", 10), images=3), epoch="e", thread="t")
        action_text = output["contentItems"][0]["text"]
        self.assertIn('"table":"per_key"', action_text)
        self.assertIn("stage_issues", summarize_text("robot_action", action_text, {"mode": "execute"}))
        self.assertIn('"right_move1"', summarize_text("robot_action", action_text, {"mode": "execute"}))
        parts = [dict(item, type="input_text" if item["type"] == "inputText" else "input_image")
                 for item in output["contentItems"]]
        labels = _image_labels(parts)
        self.assertEqual(labels[-3:], ["obs_2/cam_head", "obs_2/cam_left_wrist", "obs_2/cam_right_wrist"])


def responses_output(response):
    """Codex/Responses form of a harness reply (what the request projection sees)."""
    parts = []
    for item in response["contentItems"]:
        if item["type"] == "inputText":
            parts.append({"type": "input_text", "text": item["text"]})
        else:
            parts.append({"type": "input_image", "image_url": item["imageUrl"]})
    return parts


class Episode:
    """Responses input of one thread: harness tool replies plus Codex built-in exec_command outputs."""

    def __init__(self, compact, pin):
        self.compact, self.pin, self.items = compact, pin, []

    def _call(self, name, arguments, output):
        call_id = f"call_{len(self.items) // 2}"
        self.items += [{"type": "function_call", "name": name, "arguments": json.dumps(arguments), "call_id": call_id},
                       {"type": "function_call_output", "call_id": call_id, "output": output}]
        return call_id

    def builtin(self):
        return self._call("exec_command", {"cmd": "ls"}, "Process exited with code 0\nOutput:\nREADME.md")

    def tool(self, name, response, arguments=None):
        call_id = f"call_{len(self.items) // 2}"
        extra = dict(call_id=call_id) if self.pin else {}
        output = self.compact.apply(name, arguments or {}, response, epoch="e", thread="t", **extra)
        self._call(name, arguments or {}, responses_output(output))
        return call_id, output

    def request(self, block=8):
        keep = self.compact.pinned_calls() if self.pin else ()
        return project_request({"input": self.items}, 1, block, keep=keep)["input"]


class ReferencePinTest(unittest.TestCase):
    """The reference copy must stay in full in every request that cites it (lossless context)."""

    def episode(self, pin):
        episode = Episode(LeanCompact(anchor_period=8), pin)
        ok = lambda: episode.tool("geometry_query", reply({"status": "ok"}))
        ok()  # harness call 0, request step 1
        episode.builtin()  # steps 2, 3: Codex built-in tools never reach the harness
        episode.builtin()
        for _ in range(7):  # harness calls 1..7, steps 4..10
            ok()
        reference, _ = episode.tool("robot_observe", reply(observation("obs_1", 0)))  # harness call 8, step 11
        for _ in range(7):  # harness calls 9..15, steps 12..18
            ok()
        _, later = episode.tool("robot_observe", reply(observation("obs_2", 10)))  # harness call 16, step 19
        return episode, reference, later

    def test_builtin_tool_outputs_no_longer_hide_the_reference(self):
        # Before the fix: the encoder counts harness calls only (call 8 = anchor), while
        # the projection numbers every tool output (step 11, not an anchor of 1, 9, 17).
        episode, reference, later = self.episode(pin=False)
        self.assertEqual(texts(later)[0]["camera_calibration"], UNCHANGED_PREFIX + "obs_1")
        projected = {item["call_id"]: item for item in episode.request() if item["type"] == "function_call_output"}
        self.assertIn("[summary robot_observe]", canonical(projected[reference]["output"]))
        self.assertNotIn("camera_calibration", canonical(projected[reference]["output"]))
        # After the fix: the reference reply is pinned by call_id and sent unchanged.
        episode, reference, later = self.episode(pin=True)
        self.assertEqual(episode.compact.pinned_calls(), frozenset({reference}))
        self.assertEqual(texts(later)[0]["camera_calibration"], UNCHANGED_PREFIX + "obs_1")
        original = {item["call_id"]: item for item in episode.items if item["type"] == "function_call_output"}
        request = episode.request()
        projected = {item["call_id"]: item for item in request if item["type"] == "function_call_output"}
        self.assertEqual(canonical(projected[reference]), canonical(original[reference]))
        # A reader of the projected request rebuilds obs_2 exactly.
        reader = Reader()
        for item in request:
            if item["type"] == "function_call_output" and isinstance(item["output"], list):
                for part in item["output"]:
                    if part["type"] == "input_text" and part["text"].startswith("{"):
                        reader.read(json.loads(part["text"]))
        rebuilt = reader.read(texts(later)[0])
        self.assertEqual(numbers(rebuilt["camera_calibration"]), numbers(observation("obs_2", 10)["camera_calibration"]))
        self.assertEqual(numbers(rebuilt["camera_pose_sample"]), numbers(observation("obs_2", 10)["camera_pose_sample"]))

    def test_pinned_reference_outlives_blocks_and_parallel_or_builtin_steps(self):
        episode = Episode(LeanCompact(anchor_period=8), pin=True)
        episode.builtin()
        reference, _ = episode.tool("robot_observe", reply(observation("obs_1", 0)))
        for index in range(30):
            if index % 4 == 0:
                episode.builtin()
            episode.tool("geometry_query", reply({"status": "ok"}))
            # every request so far keeps the reference reply unchanged
            projected = {item["call_id"]: item for item in episode.request() if item["type"] == "function_call_output"}
            original = {item["call_id"]: item for item in episode.items if item["type"] == "function_call_output"}
            self.assertEqual(canonical(projected[reference]), canonical(original[reference]))
        _, late = episode.tool("robot_observe", reply(observation("obs_9", 90)))
        self.assertEqual(texts(late)[0]["camera_calibration"], UNCHANGED_PREFIX + "obs_1")
        self.assertEqual(episode.compact.pinned_calls(), frozenset({reference}))

    def test_once_strings_outside_the_reference_reply_stay_within_their_reply(self):
        compact = LeanCompact(anchor_period=8)
        apply = lambda tool, response, call: compact.apply(tool, {}, response, epoch="e", thread="t", call_id=call)
        apply("robot_observe", reply(observation("obs_1", 0)), "c0")
        first = texts(apply("robot_action", reply(execute_value("a1"), observation("obs_2", 160)), "c1"))
        second = texts(apply("robot_action", reply(execute_value("a2"), observation("obs_3", 320)), "c2"))
        self.assertEqual(compact.pinned_calls(), frozenset({"c0"}))
        # measurement prose is first given in an action reply (not pinned): given again in full
        for value in (first[0], second[0]):
            self.assertIn("derivatives", value["trajectory_feedback"]["measured"]["measurement_timing"])
            self.assertIn("limitations", value["trajectory_feedback"])
        # strings first given in the pinned reference reply are given once per context
        self.assertNotIn("omitted", second[1]["geometry_delivery"])
        self.assertEqual(second[1]["camera_calibration"], UNCHANGED_PREFIX + "obs_1")

    def test_history_view_observation_reuses_the_reference(self):
        compact = LeanCompact(anchor_period=8)
        compact.apply("robot_observe", {}, reply(observation("obs_1", 0)), epoch="e", thread="t", call_id="c0")
        view = compact.apply("history_view_observation", {"observation_id": "obs_2"},
                             reply(observation("obs_2", 10)), epoch="e", thread="t", call_id="c1")
        value = texts(view)[0]
        self.assertEqual(value["camera_calibration"], UNCHANGED_PREFIX + "obs_1")
        self.assertEqual(value["unchanged_since"], "obs_1")
        reader = Reader()
        reader.read(texts(reply(observation("obs_1", 0)))[0])
        self.assertEqual(numbers(reader.read(value)), numbers(observation("obs_2", 10)))
        full = reply(observation("obs_2", 10))
        self.assertIs(compact.apply("history_view_observation", {"observation_id": "obs_2", "detail": "full"},
                                    full, epoch="e", thread="t", call_id="c2"), full)

    def test_pins_survive_a_new_context_and_clear_with_the_thread(self):
        compact = LeanCompact(anchor_period=8)
        compact.apply("robot_observe", {}, reply(observation("obs_1", 0)), epoch="e1", thread="t", call_id="c0")
        renewed = compact.apply("robot_observe", {}, reply(observation("obs_2", 10)), epoch="e2", thread="t", call_id="c1")
        self.assertIsInstance(texts(renewed)[0]["camera_calibration"], dict)
        self.assertEqual(compact.pinned_calls(), frozenset({"c0", "c1"}))
        compact.apply("robot_observe", {}, reply(observation("obs_3", 20)), epoch="e2", thread="t2", call_id="c2")
        self.assertEqual(compact.pinned_calls(), frozenset({"c2"}))
        # trailing projection: no context reference, nothing pinned
        trailing = LeanCompact(anchor_period=0)
        trailing.apply("robot_observe", {}, reply(observation("obs_1", 0)), epoch="e", thread="t", call_id="c0")
        self.assertEqual(trailing.pinned_calls(), frozenset())


if __name__ == "__main__":
    unittest.main()
