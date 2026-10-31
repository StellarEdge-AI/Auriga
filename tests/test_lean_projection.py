"""Cache-preserving lean request projection tests."""
import base64
import json
import struct
import unittest
import zlib

from auriga.cortex.context.actor_instructions import instructions as actor_instructions
from auriga.core.presentation.lean_projection import TRAILING_HEADER, image_size, project_request, summarize_text


def png_url(width, height, shade=0):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes([shade, shade, shade]) * width for _ in range(height))
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(data).decode()


def observation(observation_id, step):
    return {"bundle": {"observation_id": observation_id, "sim_step": step,
                       "cameras": [{"camera_id": "cam_head"}, {"camera_id": "cam_left_wrist"}]},
            "robot_state": {"arms": {"left": {"grasp_pose": {"position_m": [0.123456, -0.2, 0.9],
                                                            "angles_deg": [0.0, 0.0, 90.0]},
                                              "gripper": {"position_m": [0.03, 0.03]}}}},
            "current_action_budget": {"native_steps_remaining": 900 - step, "native_steps_limit": 1100},
            "camera_calibration": {"cameras": {"cam_head": {"fx": 288.13253784}}},
            "observation_budget_semantics": "long repeated prose"}


def text(value):
    return {"type": "input_text", "text": json.dumps(value)}


def image(width, height, shade=0):
    return {"type": "input_image", "image_url": png_url(width, height, shade), "detail": "high"}


STEPS = [
    ("robot_observe", {}, [text(observation("obs_2", 0)), image(8, 6, 1), image(8, 6, 2)]),
    ("robot_action", {"mode": "execute", "plan_id": "p1"},
     [text({"status": "completed", "plan_id": "p1", "action_id": "a2",
            "receipt": {"status": "completed", "executed_native_steps": 87, "result_observation_id": "obs_3",
                        "pending_stage_ids": []},
            "action_budget": {"native_steps_remaining": 1013, "native_steps_limit": 1100},
            "trajectory_feedback": {"final_measured_jaws": {"arms": {"left": {"position_m": [0.0288, 0.0291]}}},
                                    "measured": {"samples": [[0.1] * 50] * 20}}}),
      image(24, 6, 3), text(observation("obs_3", 870)), image(8, 6, 4), image(8, 6, 5)]),
    ("program_run", {"source": "print(1)"},
     [text({"job_id": "job_1", "status": "succeeded", "returncode": 0, "calls": 0,
            "stdout_text": "dollLeft [-0.4145 -0.1761 0.78]\n", "result_semantics": "x" * 300})]),
]


def history(count):
    items = [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]},
             {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]}]
    for index, (tool, arguments, output) in enumerate(STEPS[:count]):
        call_id = f"call_{index}"
        items += [{"type": "reasoning", "encrypted_content": f"enc{index}"},
                  {"type": "function_call", "name": tool, "arguments": json.dumps(arguments), "call_id": call_id},
                  {"type": "function_call_output", "call_id": call_id, "output": output}]
    return items


def body(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class LeanProjectionTests(unittest.TestCase):
    def project(self, count, k=1):
        return project_request({"model": "m", "input": history(count), "tools": []}, k)

    def test_next_request_extends_previous_without_trailing_block(self):
        previous = None
        for count in (1, 2, 3):
            request = self.project(count)
            items = request["input"]
            self.assertEqual(items[-1]["role"], "user")
            self.assertEqual(items[-1]["content"][0]["text"], TRAILING_HEADER)
            if previous is not None:
                kept = previous[:-1]
                self.assertEqual(body(items[:len(kept)]), body(kept))
            previous = items

    def test_history_images_are_placeholders_and_trailing_holds_only_latest_k(self):
        items = self.project(2)["input"]
        history_items, trailing = items[:-1], items[-1]
        self.assertNotIn("data:image", body(history_items))
        placeholders = [part["text"] for item in history_items if item.get("type") == "function_call_output"
                        for part in item["output"] if "pixels only in the latest tool feedback" in part.get("text", "")]
        self.assertEqual(len(placeholders), 5)
        self.assertIn("obs_2/cam_head 8x6", placeholders[0])
        self.assertIn("obs_3/cam_left_wrist 8x6", placeholders[4])
        latest_images = [part["image_url"] for part in STEPS[1][2] if part["type"] == "input_image"]
        trailing_images = [part["image_url"] for part in trailing["content"] if part["type"] == "input_image"]
        self.assertEqual(trailing_images, latest_images)
        full_texts = [part["text"] for part in STEPS[1][2] if part["type"] == "input_text"]
        trailing_texts = [part["text"] for part in trailing["content"] if part["type"] == "input_text"]
        for full in full_texts:
            self.assertIn(full, trailing_texts)
        self.assertNotIn(STEPS[0][2][0]["text"], trailing_texts)
        two = self.project(2, k=2)["input"][-1]
        images = [part["image_url"] for part in two["content"] if part["type"] == "input_image"]
        self.assertEqual(images, [STEPS[0][2][1]["image_url"], STEPS[0][2][2]["image_url"]] + latest_images)
        self.assertNotIn(STEPS[0][2][0]["text"], [part.get("text") for part in two["content"]])

    def test_text_only_latest_result_trails_full_text_and_summaries_are_deterministic(self):
        first, second = self.project(3), self.project(3)
        self.assertEqual(body(first), body(second))
        trailing = first["input"][-1]
        self.assertFalse([part for part in trailing["content"] if part["type"] == "input_image"])
        self.assertIn(STEPS[2][2][0]["text"], [part.get("text") for part in trailing["content"]])
        output = [item for item in first["input"] if item.get("call_id") == "call_2" and item["type"] == "function_call_output"][0]
        summary = output["output"][0]["text"]
        self.assertTrue(summary.startswith("[summary program_run] "))
        self.assertIn("stdout=dollLeft", summary)
        self.assertNotIn("result_semantics", summary)
        self.assertEqual(summary, summarize_text("program_run", STEPS[2][2][0]["text"], {}))
        action = [item for item in first["input"] if item.get("call_id") == "call_1" and item["type"] == "function_call_output"][0]
        self.assertLessEqual(len(action["output"][0]["text"]), 560)
        self.assertIn('"final_jaw_m":{"left":0.029', action["output"][0]["text"])
        self.assertTrue(action["output"][2]["text"].startswith("[summary observation] "))

    def test_other_tools_compaction_and_string_outputs_pass_through(self):
        items = [{"type": "compaction", "encrypted_content": "summary"},
                 {"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": "s1"},
                 {"type": "function_call_output", "call_id": "s1", "output": "ls output"},
                 {"type": "function_call", "name": "task_plan", "arguments": "{}", "call_id": "t1"},
                 {"type": "function_call_output", "call_id": "t1", "output": [text({"plan_id": "P1"})]}]
        request = {"input": items}
        self.assertEqual(body(project_request(request, 1)), body(request))
        items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "compact now"}]})
        self.assertEqual(body(project_request({"input": items}, 1)), body({"input": items}))
        geometry = [{"type": "function_call", "name": "geometry_query", "arguments": "{}", "call_id": "g1"},
                    {"type": "function_call_output", "call_id": "g1",
                     "output": json.dumps({"query_type": "triangulate", "status": "ok",
                                           "value": {"points_m": [[0.123456, 0.2, 0.3]]}})},
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "compact now"}]}]
        projected = project_request({"input": geometry}, 1)["input"]
        self.assertTrue(projected[1]["output"].startswith("[summary geometry_query] "))
        self.assertIn("0.1235", projected[1]["output"])
        # The trailing block sits right after the last tool output, before later items.
        self.assertEqual(projected[2]["content"][0]["text"], TRAILING_HEADER)
        self.assertEqual(projected[3]["content"][0]["text"], "compact now")

    def test_block_mode_appends_within_block_and_anchor_survives_rewrite(self):
        def items(count):
            result = history(0)
            for index in range(count):
                tool, arguments, output = STEPS[index % len(STEPS)]
                call_id = f"call_{index}"
                result += [{"type": "reasoning", "encrypted_content": f"enc{index}"},
                           {"type": "function_call", "name": tool, "arguments": json.dumps(arguments), "call_id": call_id},
                           {"type": "function_call_output", "call_id": call_id, "output": output}]
            return result
        block = 3
        projected = {count: project_request({"model": "m", "input": items(count)}, 1, block)["input"]
                     for count in range(1, 11)}
        for count, request in projected.items():
            self.assertNotIn(TRAILING_HEADER, body(request))
            self.assertEqual(len(request), len(items(count)))
            outputs = [item for item in request if item["type"] == "function_call_output"]
            start = block * ((count - 1) // block)
            for number, (item, original) in enumerate(zip(outputs, [i for i in items(count) if i["type"] == "function_call_output"]), 1):
                if number > start or (number - 1) % block == 0:
                    self.assertEqual(body(item), body(original))  # open block or anchor: unchanged
                elif STEPS[(number - 1) % len(STEPS)][0] != "program_run":
                    self.assertNotIn("data:image", body(item))
                    self.assertIn("[summary ", body(item))
        for count in range(1, 10):
            previous, current = projected[count], projected[count + 1]
            if count % block:
                self.assertEqual(body(current[:len(previous)]), body(previous))  # pure extension
            else:
                self.assertNotEqual(body(current[:len(previous)]), body(previous))  # one rewrite per block
                anchor = projected[count - block + 1]
                self.assertEqual(body(current[:len(anchor)]), body(anchor))
        self.assertEqual(body(project_request({"input": items(7)}, 1, block)),
                         body(project_request({"input": items(7)}, 1, block)))
        placeholder = [part["text"] for item in projected[7] if item["type"] == "function_call_output"
                       for part in item["output"] if part.get("text", "").startswith("[image ")]
        self.assertTrue(placeholder and "pixels omitted from older history" in placeholder[0])

    def test_image_size_reads_png_header(self):
        self.assertEqual(image_size(png_url(13, 7)), (13, 7))
        self.assertIsNone(image_size("https://example.invalid/a.png"))


    def test_operation_memory_instruction_omitted_when_tool_disabled(self):
        with_mem = actor_instructions("obs", "handoff", operation_memory=True)
        without_mem = actor_instructions("obs", "handoff", operation_memory=False)
        self.assertIn("operation_memory separates current-task records", with_mem)
        self.assertNotIn("operation_memory separates current-task records", without_mem)
        self.assertIn("Read the public Harness implementation under /harness.", without_mem)


class LeanCodexSessionTests(unittest.TestCase):
    """Native config and request hook; skipped where the pinned Codex SDK is absent."""

    def session(self, features):
        try:
            from auriga.cortex.session.codex import CodexSession
        except ImportError as error:
            self.skipTest(str(error))
        import tempfile
        from pathlib import Path
        from auriga.core.config import Config
        from auriga.core.storage.store import Store
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "source").mkdir()
        (root / "source" / "config.toml").write_text(
            'model_provider = "fixture"\n[model_providers.fixture]\nbase_url = "https://fixture.invalid/v1"\n')
        store = Store(root / "run", dict(run_id="lean", episode_id="episode", env_instance_id="environment",
                                         env_generation=1))
        self.addCleanup(store.close)
        config = Config.from_dict({"model": {"codex_home": str(root / "source")}, "features": features})
        session = CodexSession(store.root, store, lambda _: {}, config=config)
        self.addCleanup(session.close)
        return session

    def prepared(self, session):
        import subprocess
        import tomllib
        from unittest import mock
        relay = mock.MagicMock(base_url="http://127.0.0.1:54321")
        relay.start.return_value = relay
        with mock.patch("auriga.cortex.session.codex.ModelRequestProxy", return_value=relay):
            session._prepare()
        config = tomllib.loads((session.codex_home / "config.toml").read_text())
        result = subprocess.run(session.command[:-5] + [str(session.cli_executable), "features", "list"],
                                env=session.env, cwd=session.actor_dir, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return config, result.stdout

    def test_baseline_keeps_native_features_and_request_shape(self):
        session = self.session({"sandbox_enabled": False})
        self.assertFalse(session.lean_projection)
        config, _ = self.prepared(session)
        self.assertTrue(config["features"]["multi_agent"])
        self.assertTrue(config["features"]["multi_agent_v2"])
        self.assertNotIn("skills", config)
        catalog = json.loads(open(config["model_catalog_json"]).read())
        self.assertIn("v2", [model["multi_agent_version"] for model in catalog["models"]])
        request = {"input": history(2)}
        self.assertNotIn(TRAILING_HEADER, body(session._present_model_request(request)))

    def test_lean_projection_modes_preserve_native_features(self):
        session = self.session({"sandbox_enabled": False, "lean_projection": True})
        config, _ = self.prepared(session)
        self.assertTrue(config["features"]["multi_agent"])
        self.assertTrue(config["features"]["multi_agent_v2"])
        self.assertNotIn("skills", config)
        # Default block mode: no trailing message, the open block is sent unchanged.
        projected = session._present_model_request({"input": history(2)})
        self.assertNotIn(TRAILING_HEADER, body(projected))
        self.assertEqual(body(projected["input"]), body(session._present_model_request({"input": history(2)})["input"]))
        self.assertEqual(sum("data:image" in body(item) for item in projected["input"]), 2)
        trailing = self.session({"sandbox_enabled": False, "lean_projection": True,
                                 "lean_projection_block": 0})
        projected = trailing._present_model_request({"input": history(2)})
        self.assertEqual(projected["input"][-1]["content"][0]["text"], TRAILING_HEADER)


if __name__ == "__main__":
    unittest.main()
