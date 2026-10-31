"""Large log endings and fixed-skill outcomes survive model presentation."""

from copy import deepcopy
import base64
import io
import json
from pathlib import Path
import random
import tempfile
import unittest

from PIL import Image

from auriga.runtime.programs.program_result import program_result
from auriga.core.storage.store import Store


class ProgramResultTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name), {})
        self.addCleanup(self.store.close)

    def job(self, stdout, stderr=b"", **extra):
        return dict(job_id="program-test", status="failed", returncode=1, stop_confirmed=False,
                    calls=3, timeout_s=120, timeout_semantics="work_wall_time_excluding_own_sol_requests",
                    model_wait_s=10, finished_at="2026-10-05T00:00:00Z",
                    stdout_ref=self.store.put_artifact(stdout, "program_stdout", "text/plain"),
                    stderr_ref=self.store.put_artifact(stderr, "program_stderr", "text/plain"),
                    stdout_text=stdout[:8192].decode(errors="replace"), stdout_truncated=len(stdout)>8192,
                    stderr_text=stderr[:8192].decode(errors="replace"), stderr_truncated=len(stderr)>8192,
                    **extra)

    def test_arbitrary_stdout_stays_text_and_retains_terminal_result(self):
        raw = b"start\n" + b"x"*40000 + b'\n{"status":"completed","grasp":true}'
        errors = b"Traceback\n" + b"line\n"*7000 + b"RuntimeError: action stop unconfirmed"
        job = self.job(raw, errors, source_sha256="source", input_digest="input", correlation=dict(program_id="p"))
        before = deepcopy(job)
        result = program_result(self.store, job)
        self.assertIn('"grasp":true', result["stdout_text"])
        self.assertFalse(result["stdout_truncated"])
        self.assertTrue(result["stderr_text"].endswith("action stop unconfirmed"))
        self.assertNotIn("skill_output", result)
        for key in ("status", "returncode", "stop_confirmed", "calls", "model_wait_s", "timeout_semantics"):
            self.assertEqual(result[key], job[key])
        self.assertEqual(result["stdout_excerpt"]["byte_size"], len(raw))
        self.assertEqual(result["full_result"], dict(tool="program_status", job_id=job["job_id"], detail="full"))
        self.assertEqual(job, before)
        full = program_result(self.store, job, detail="full")
        self.assertEqual(full["stdout_text"].encode(), raw)
        self.assertEqual(full["stderr_text"].encode(), errors)
        self.assertFalse(full["stdout_truncated"])
        self.assertEqual(full["correlation"], job["correlation"])

    def test_default_preserves_middle_diagnostics_and_all_text_in_both_streams(self):
        for size in (10240, 32768, 32769, 100000):
            raw = b"x" * size + "\n中间唯一诊断: stop unconfirmed\n".encode() + b"y" * size
            result = program_result(self.store, self.job(raw, raw))
            for name in ("stdout", "stderr"):
                with self.subTest(size=size, stream=name):
                    excerpt = result[name + "_excerpt"]
                    self.assertEqual(excerpt["byte_size"], len(raw))
                    self.assertFalse(result[name + "_truncated"])
                    self.assertEqual(result[name + "_text"].encode(), raw)
                    self.assertEqual(excerpt["byte_ranges"], [[0, len(raw)]])
                    self.assertEqual(excerpt["omitted_bytes"], 0)

    def test_long_image_data_urls_leave_diagnostics_visible_and_full_exact(self):
        image = Image.frombytes("RGB", (128, 128), random.Random(0).randbytes(128 * 128 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        url = b"data:image/png;base64," + base64.b64encode(buffer.getvalue())
        self.assertGreater(len(url), 10240)
        before = "失败诊断: status=stopped executed_native_steps=4\n".encode()
        after = "\nRuntimeError: 停止未确认; observation unavailable\n".encode()
        raw = before + url + after
        job = self.job(raw, raw)
        original_job = deepcopy(job)
        result = program_result(self.store, job)
        for name in ("stdout", "stderr"):
            with self.subTest(stream=name):
                text = result[name + "_text"]
                self.assertTrue(text.startswith(before.decode()))
                self.assertTrue(text.endswith(after.decode()))
                self.assertIn("inline image/png data URL omitted", text)
                self.assertNotIn(url[100:200].decode(), text)
                self.assertTrue(result[name + "_truncated"])
                excerpt = result[name + "_excerpt"]
                self.assertEqual(excerpt["presentation"], "inline_image_data_urls_omitted")
                self.assertEqual(excerpt["byte_ranges_source"], "presentation_utf8")
                self.assertEqual(excerpt["byte_size"], len(text.encode()))
                self.assertEqual(excerpt["byte_ranges"], [[0, len(text.encode())]])
                self.assertEqual(excerpt["omitted_bytes"], 0)
                self.assertEqual(excerpt["original_byte_size"], len(raw))
                self.assertEqual(excerpt["removed_media_count"], 1)
                self.assertEqual(excerpt["removed_media_bytes"], len(url))
                self.assertEqual(excerpt["removed_media_byte_ranges"], [[len(before), len(before) + len(url)]])
                self.assertEqual(excerpt["removed_media_byte_ranges_source"], "original_log_bytes")
                self.assertTrue(excerpt["source_complete"])
                self.assertEqual(self.store.resolve_artifact(job[name + "_ref"])[1], raw)
        self.assertEqual(job, original_job)
        full = program_result(self.store, job, detail="full")
        self.assertEqual(full["stdout_text"].encode(), raw)
        self.assertEqual(full["stderr_text"].encode(), raw)
        self.assertFalse(full["stdout_truncated"])

    def test_nonstandard_or_incomplete_media_and_nonimage_urls_stay_opaque(self):
        for payload in (b"AAA", b"AAAA=", b"____", b"AAAA@BBBB", b"AAAA\\/BBBB"):
            raw = b'before "data:image/png;base64,' + payload + b'" after: status=failed'
            with self.subTest(payload=payload):
                result = program_result(self.store, self.job(raw))
                self.assertEqual(result["stdout_text"].encode(), raw)
                self.assertNotIn("presentation", result["stdout_excerpt"])
                self.assertFalse(result["stdout_truncated"])
        raw = b'data:application/octet-stream;base64,AAAA'
        result = program_result(self.store, self.job(raw))
        self.assertEqual(result["stdout_text"].encode(), raw)
        raw = b'metadata:image/png;base64,AAAA'
        result = program_result(self.store, self.job(raw))
        self.assertEqual(result["stdout_text"].encode(), raw)
        incomplete = dict(stdout_text="data:image/png;base64,AAAA", stdout_truncated=True)
        result = program_result(self.store, incomplete)
        self.assertEqual(result["stdout_text"], incomplete["stdout_text"])
        self.assertNotIn("presentation", result["stdout_excerpt"])
        self.assertFalse(result["stdout_excerpt"]["source_complete"])

    def test_multibyte_plain_output_keeps_original_byte_ranges(self):
        raw = ("汉" * 12000 + "\n错误: stop unconfirmed").encode()
        result = program_result(self.store, self.job(raw))
        excerpt = result["stdout_excerpt"]
        self.assertEqual(excerpt["byte_size"], len(raw))
        self.assertEqual(excerpt["byte_ranges"], [[0, len(raw)]])
        self.assertEqual(result["stdout_text"].encode(), raw)
        self.assertNotIn("byte_ranges_source", excerpt)
        self.assertTrue(result["stdout_text"].endswith("错误: stop unconfirmed"))
        self.assertEqual(program_result(self.store, self.job(raw), detail="full")["stdout_text"].encode(), raw)

    def test_media_ranges_stay_auditable_with_explicit_internal_excerpt_budget(self):
        prefix = "前段诊断: ".encode() + b"a" * 20000 + b"\n"
        url = b"data:image/png;base64," + base64.b64encode(b"x" * 30000)
        suffix = b"\n" + b"b" * 20000 + "\n末段诊断: stop unconfirmed".encode()
        raw = prefix + url + suffix
        result = program_result(self.store, self.job(raw), max_output_bytes=32768)
        excerpt = result["stdout_excerpt"]
        size = excerpt["byte_size"]
        self.assertGreater(size, 32768)
        self.assertEqual(excerpt["byte_ranges"], [[0, 16384], [size - 16384, size]])
        self.assertEqual(excerpt["omitted_bytes"], size - 32768)
        self.assertEqual(excerpt["byte_ranges_source"], "presentation_utf8")
        self.assertEqual(excerpt["removed_media_byte_ranges"], [[len(prefix), len(prefix) + len(url)]])
        self.assertEqual(excerpt["original_byte_size"], len(raw))
        self.assertTrue(result["stdout_text"].startswith("前段诊断: "))
        self.assertTrue(result["stdout_text"].endswith("末段诊断: stop unconfirmed"))
        self.assertEqual(program_result(self.store, self.job(raw), detail="full")["stdout_text"].encode(), raw)

    def test_plain_json_arrays_and_unknown_diagnostics_are_not_filtered(self):
        value = dict(status="unknown", failure_feedback=dict(witness=[.123456789] * 400),
            new_diagnostic=dict(reason="execution_uncertain", state=[0, 1, 2]),
            contentItems=[dict(type="inputImage", imageUrl="ordinary opaque value")])
        raw = json.dumps(value).encode()
        self.assertLess(len(raw), 10240)
        result = program_result(self.store, self.job(raw))
        self.assertEqual(result["stdout_text"].encode(), raw)
        self.assertNotIn("skill_output", result)
        self.assertNotIn("presentation", result["stdout_excerpt"])

    def test_output_scope_and_kind_remain_checked(self):
        for kind, scope, error in (("program_stdout", "private", "artifact_access_scope_denied"),
                                   ("other", "actor", "program_output_artifact_kind_mismatch")):
            ref = self.store.put_artifact(b"opaque output", kind, "text/plain", scope)
            with self.subTest(kind=kind, scope=scope), self.assertRaisesRegex(ValueError, error):
                program_result(self.store, dict(stdout_ref=ref))

    def test_known_nested_evidence_omits_metadata_in_summary_and_full(self):
        receipt = dict(schema_version='0.1', run_id='fixture', env_generation=1, status='unknown',
            stop_confirmed=False, executed_native_steps=None, plan_revision=5,
            failure_feedback=dict(schema_version='diagnostic-v1', reason='stop unconfirmed',
                                  error_m=0.123456789012345),
            stage_results={'version': dict(status='partial', revision=3)},
            diagnostic=dict(version='business-version', revision=4))
        expected = {key: value for key, value in receipt.items()
                    if key not in {'schema_version', 'run_id', 'env_generation'}}
        expected['failure_feedback'] = {key: value for key, value in receipt['failure_feedback'].items()
                                        if key != 'schema_version'}
        job = self.job(b'complete middle diagnostics',
            action_summary=dict(recent_actions=[receipt] * 10),
            pending_review=dict(review_id='review', preview=receipt))
        original = deepcopy(job)
        for detail in ('summary', 'full'):
            shown = program_result(self.store, job, detail=detail)
            self.assertEqual(shown['action_summary']['recent_actions'], [expected] * 10)
            self.assertEqual(shown['pending_review']['preview'], expected)
            self.assertEqual(shown['stdout_text'], 'complete middle diagnostics')
        self.assertEqual(job, original)


if __name__ == "__main__":
    unittest.main()
