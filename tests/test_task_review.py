"""Read-only scoring provenance, native/normalized separation and observer API."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from auriga.core.config import Config
from auriga.apps.library.experiments import allocate_run, atomic_json, catalog_id
from auriga.apps.gateway.workbench import Workbench
from auriga.apps.library.service import Library
from auriga.apps.library.task_review import TaskReviewService


TASK_SOURCE = '''
raise RuntimeError("task review must never import task code")
class Fixture:
    def get_score(self):
        self.reward_manager.score([["first"], ["second"], ["last"]], [15, 40, 100], score_mode="transition")
    def run_reward(self):
        self.reward_manager.check(["complete", "arms returned"])
    def gen_instruction(self, env_idx):
        templates = ["Official fixture instruction."]
        return templates
'''
VERSION = {"commit": "commit-one", "diff_sha256": "diff-one"}


def install_fixture(root, task="fixture", source=TASK_SOURCE):
    directory = root / "task/RoboDojo"
    (directory / "tasks").mkdir(parents=True, exist_ok=True)
    (directory / "config").mkdir(parents=True, exist_ok=True)
    (directory / "tasks" / (task + ".py")).write_text(source)
    (directory / "config" / (task + ".yml")).write_text("Rigid: []\n")


def fixture_entry(root, *, task="fixture", summary=None, evaluation=None):
    run = allocate_run(root, category="synthetic", task=task, run_id="task-review")
    manifest = dict(task=task, capabilities=dict(versions=dict(robodojo_commit=VERSION["commit"],
                                                             robodojo_local_diff_sha256=VERSION["diff_sha256"])))
    atomic_json(run / "manifest.json", manifest)
    if summary is not None:
        atomic_json(run / "summary.json", summary)
    if evaluation is not None:
        atomic_json(run / "evaluation.json", dict(result=evaluation))
    metadata = json.loads((run / "experiment.json").read_text())
    return dict(path=run, metadata=metadata, item=dict(task=task, id=catalog_id(run.relative_to(root)),
                                                     status="completed", summary=summary or {}))


class TaskReviewChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "official"
        install_fixture(self.source)
        self.version = patch("auriga.apps.library.task_review.repository_version", return_value=VERSION)
        self.version.start()
        self.addCleanup(self.version.stop)
        self.service = TaskReviewService(self.source)

    def entry(self, **kwargs):
        return fixture_entry(self.base / "runs", **kwargs)

    def test_scored_success_keeps_native_scale_and_unknown_breakdown(self):
        entry = self.entry(summary=dict(scored_episode=True, primary_outcome="scored_success", task_success=True,
                                        normalized_score=1, native_process_score=100, native_reward=1),
                           evaluation=dict(task="fixture", task_success=True, normalized_score=1, native_process_score=100))
        review = self.service.get(entry)
        self.assertEqual(review["description"]["text"], "Official fixture instruction.")
        self.assertEqual([item["score"] for item in review["scoring"]["criteria"]], [15, 40, 100])
        self.assertEqual(review["scoring"]["rule_source"]["status"], "matched")
        self.assertEqual(review["result"]["status"], "scored")
        self.assertEqual(review["result"]["normalized_score"], 1)
        self.assertEqual(review["result"]["native_process_score"], 100)
        self.assertTrue(all(item["recorded_result"] is None and item["evidence_status"] == "not_recorded"
                            for item in review["scoring"]["criteria"]))
        self.assertEqual(review["result"]["breakdown_status"], "not_recorded")
        self.assertTrue(review["read_only"])
        self.assertEqual(review["audience"], "observer_only")

    def test_infrastructure_exclusion_cannot_be_overwritten_by_evaluator_zero(self):
        entry = self.entry(summary=dict(scored_episode=False, primary_outcome="unscored_infrastructure", normalized_score=None,
                                        task_success=None, infrastructure_error="transport failed",
                                        partial_native_result=dict(normalized_score=0, native_process_score=0)),
                           evaluation=dict(task="fixture", task_success=False, normalized_score=0, native_process_score=0, native_reward=0))
        review = self.service.get(entry)
        self.assertEqual(review["result"]["status"], "unscored")
        self.assertIsNone(review["result"]["normalized_score"])
        self.assertIsNone(review["result"]["task_success"])
        self.assertEqual(review["result"]["partial_native_result"]["normalized_score"], 0)
        self.assertEqual(review["result"]["evaluation_normalized_score"], 0)
        self.assertIn("transport failed", " ".join(review["result"]["explanation"]))

    def test_legacy_infrastructure_error_also_remains_unscored(self):
        entry = self.entry(summary=dict(infrastructure_error="worker unavailable", normalized_score=0, task_success=False),
                           evaluation=dict(task="fixture", native_process_score=0, normalized_score=0))
        result = self.service.get(entry)["result"]
        self.assertEqual(result["status"], "unscored")
        self.assertIsNone(result["normalized_score"])

    def test_scored_failure_explains_division_and_evaluator_only_stays_partial(self):
        entry = self.entry(summary=dict(scored_episode=True, primary_outcome="scored_failure", task_success=False,
                                        normalized_score=.4, native_process_score=40, native_reward=0))
        result = self.service.get(entry)["result"]
        self.assertEqual(result["status"], "scored")
        self.assertIn("40 ÷ 100 = 0.4", " ".join(result["explanation"]))
        (entry["path"] / "summary.json").unlink()
        entry["item"]["summary"] = {}
        atomic_json(entry["path"] / "evaluation.json", dict(result=dict(task="fixture", task_success=False, native_process_score=40, normalized_score=.4)))
        result = self.service.get(entry)["result"]
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["scored_episode"])

    def test_version_mismatch_and_missing_versions_never_claim_historical_rules(self):
        entry = self.entry()
        manifest = json.loads((entry["path"] / "manifest.json").read_text())
        manifest["capabilities"]["versions"]["robodojo_commit"] = "old"
        atomic_json(entry["path"] / "manifest.json", manifest)
        provenance = self.service.get(entry)["scoring"]["rule_source"]
        self.assertEqual(provenance["status"], "current_source_only")
        self.assertEqual(provenance["recorded"]["commit"], "old")
        atomic_json(entry["path"] / "manifest.json", dict(task="fixture"))
        self.assertEqual(self.service.get(entry)["scoring"]["rule_source"]["status"], "current_source_only")

    def test_recorded_instruction_beats_dynamic_source_template_and_cache_invalidates(self):
        entry = self.entry()
        database = entry["path"] / "events.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE records (kind TEXT, body TEXT)")
            connection.execute("INSERT INTO records VALUES (?,?)", ("observation", json.dumps(dict(task_instruction="Put actual pens into the left basket."))))
        review = self.service.get(entry)
        self.assertEqual(review["description"], dict(text="Put actual pens into the left basket.", source_kind="recorded_observation", status="recorded"))
        review["scoring"]["criteria"].clear()
        self.assertEqual(len(self.service.get(entry)["scoring"]["criteria"]), 3)
        atomic_json(entry["path"] / "summary.json", dict(scored_episode=True, task_success=False, normalized_score=.15, native_process_score=15))
        self.assertEqual(self.service.get(entry)["result"]["normalized_score"], .15)

    def test_variable_gradients_keep_variants_separate(self):
        source = '''
class Task:
    def get_score(self):
        score_lists = {4: [5, 15, 30, 100], 5: [5, 15, 25, 40, 100]}
        self.rm.score_single_env(env_idx, stages, score_lists[n], score_mode="transition")
    def gen_instruction(self, env_idx):
        templates = [f"Arrange {count} digits."]
        return templates
'''
        install_fixture(self.source, "arrange_largest_number", source)
        entry = self.entry(task="arrange_largest_number")
        review = self.service.get(entry)
        self.assertEqual(review["description"]["text"], "Arrange {count} digits.")
        criteria = review["scoring"]["criteria"]
        self.assertEqual([item["score"] for item in criteria if item["variant"] == "4 个数字"], [5, 15, 30, 100])
        self.assertEqual([item["score"] for item in criteria if item["variant"] == "5 个数字"], [5, 15, 25, 40, 100])
        self.assertFalse(review["scoring"]["description_reviewed"])

    def test_binary_and_unknown_tasks_do_not_invent_process_points(self):
        install_fixture(self.source, "binary", "class Binary:\n def run_reward(self):\n  self.rm.check(['complete'])\n")
        entry = self.entry(task="binary", summary=dict(scored_episode=True, task_success=False, normalized_score=0, native_process_score=None))
        review = self.service.get(entry)
        self.assertEqual(review["scoring"]["mode"], "binary_success")
        self.assertTrue(all(item["score"] is None for item in review["scoring"]["criteria"]))
        atomic_json(entry["path"] / "manifest.json", dict(task="unknown_task"))
        review = self.service.get(entry)
        self.assertEqual(review["scoring"]["rule_source"]["status"], "unavailable")
        self.assertEqual(review["scoring"]["criteria"], [])

    def test_foreign_task_evaluation_is_not_assigned_to_this_run(self):
        entry = self.entry(evaluation=dict(task="different", task_success=True, normalized_score=1, native_process_score=100))
        result = self.service.get(entry)["result"]
        self.assertIsNone(result["normalized_score"])
        self.assertIsNone(result["native_process_score"])
        self.assertIn("不一致", result["explanation"][0])


class TaskReviewHTTPChecks(unittest.IsolatedAsyncioTestCase):
    async def test_standalone_current_run_review_refresh_and_bounded_evidence_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            install_fixture(base / "official")
            root = base / "standalone"
            root.mkdir()
            atomic_json(root / "manifest.json", dict(task="fixture"))
            # Exercise real HTTP routing without starting a robot, session or writable Store.
            workbench = Workbench.__new__(Workbench)
            workbench.root, workbench.task, workbench.status = root, "fixture", "running"
            workbench.identity = dict(run_id=root.name)
            workbench.config = Config().with_overrides({"library": {"task_source_root": str(base / "official")}})
            with patch("auriga.apps.library.task_review.repository_version", return_value=VERSION), \
                    patch("auriga.apps.library.experiments.Catalog.scan", side_effect=AssertionError("standalone must not scan")):
                async with TestClient(TestServer(workbench.app())) as client:
                    response = await client.get("/v1/task-review")
                    self.assertEqual(response.status, 200)
                    self.assertEqual((await response.json())["result"]["status"], "pending")
                    atomic_json(root / "summary.json", dict(scored_episode=True, task_success=False, normalized_score=.4, native_process_score=40))
                    atomic_json(root / "evaluation.json", dict(result=dict(task="fixture", native_process_score=40, normalized_score=.4)))
                    before = {path.name: path.read_bytes() for path in root.iterdir()}
                    workbench.status = "replay"
                    review = await (await client.get("/v1/task-review")).json()
                    self.assertEqual(review["result"]["status"], "scored")
                    self.assertEqual(review["result"]["normalized_score"], .4)
                    self.assertEqual(review["audience"], "observer_only")
                    urls = [source["url"] for source in review["sources"] if source.get("url")]
                    self.assertEqual(len(urls), 2)
                    for url in urls:
                        response = await client.get(url)
                        self.assertEqual(response.status, 200)
                        raw = await response.read()
                        self.assertEqual(response.headers["X-Evidence-SHA256"], hashlib.sha256(raw).hexdigest())
                        self.assertIn(raw, before.values())
                        self.assertEqual((await client.head(url)).status, 200)
                        response = await client.get(url, headers={"Range": "bytes=0-15"})
                        self.assertEqual(response.status, 206)
                        self.assertEqual(await response.read(), raw[:16])
                        self.assertEqual((await client.post(url, json={})).status, 405)
                    self.assertEqual((await client.head("/v1/task-review")).status, 200)
                    self.assertEqual((await client.post("/v1/task-review", json={})).status, 405)
                    self.assertEqual((await client.get("/v1/task-review", headers={"X-Auriga-Run-ID": "foreign"})).status, 404)
                    prefix = f"/v1/experiments/{catalog_id('.')}/files/"
                    for name in ("manifest.json", "private.json", "../evaluation.json"):
                        self.assertEqual((await client.get(prefix + hashlib.sha256(name.encode()).hexdigest()[:32])).status, 404)
                    self.assertEqual((await client.get(urls[0].replace(catalog_id('.'), catalog_id('foreign')))).status, 404)
                    self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})
                    (root / "evaluation.json").unlink()
                    (root / "evaluation.json").symlink_to(root / "manifest.json")
                    self.assertEqual((await client.get(prefix + hashlib.sha256(b"evaluation.json").hexdigest()[:32])).status, 404)

    async def test_detail_and_readonly_run_api_share_review_and_safe_evidence_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            install_fixture(base / "official")
            entry = fixture_entry(base / "library", summary=dict(scored_episode=False, primary_outcome="unscored_infrastructure", normalized_score=None),
                                  evaluation=dict(task="fixture", native_process_score=0, native_reward=0, normalized_score=0))
            with patch("auriga.apps.library.task_review.repository_version", return_value=VERSION):
                library = Library(base / "library", task_source_root=base / "official")
                runner = web.AppRunner(library.app(), shutdown_timeout=.2)
                await runner.setup()
                try:
                    site = web.TCPSite(runner, "127.0.0.1", 0)
                    await site.start()
                    origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
                    identifier = entry["item"]["id"]
                    route = f"/runs/{identifier}/v1/task-review"
                    async with ClientSession() as client:
                        async with client.get(origin + route) as response:
                            self.assertEqual(response.status, 200)
                            review = await response.json()
                        async with client.get(origin + f"/v1/experiments/{identifier}") as response:
                            detail = await response.json()
                        self.assertEqual(detail["task_review"], review)
                        self.assertEqual(review["result"]["status"], "unscored")
                        for source in review["sources"]:
                            if source.get("url"):
                                self.assertTrue(source["url"].startswith(f"/v1/experiments/{identifier}/files/"))
                                async with client.get(origin + source["url"]) as response:
                                    self.assertEqual(response.status, 200)
                        async with client.post(origin + route, json={}) as response:
                            self.assertEqual(response.status, 403)
                        async with client.head(origin + route) as response:
                            self.assertEqual(response.status, 200)
                finally:
                    await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
