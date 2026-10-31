"""Real HTTP checks for compact library views and private revalidation."""
import copy
import gzip
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

from aiohttp import ClientSession, web

from auriga.apps.library.experiments import atomic_json, catalog_id, register_run
from auriga.apps.library.service import Library


class LoadingCheck(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "benchmark" / "fixture" / "runs" / "first"
        register_run(self.run, category="benchmark", task="pick", run_id="first")
        self.summary = self.run / "summary.json"
        atomic_json(self.summary, dict(normalized_score=.5, task_success=False, scored_episode=True, valid_episode=True))
        atomic_json(self.run.parent.parent / "manifest.json", dict(method="fixture",
            protocol_cases=[dict(task="pick", variant="standard", eval_seed=0, layout_id=0)],
            cases=[dict(run_dir=str(self.run), task="pick", seed=0, layout_id=0, cwd="/fixture")]))
        self.identifier = catalog_id(self.run.relative_to(self.root).as_posix())
        self.library = Library(self.root, scan_interval=3600)
        self.runner = web.AppRunner(self.library.app(), shutdown_timeout=.2)
        await self.runner.setup()
        self.addAsyncCleanup(self.runner.cleanup)
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.client = ClientSession()
        self.addAsyncCleanup(self.client.close)
        self.group_id = self.library.group_snapshot["groups"][0]["id"]

    async def get(self, path, **kwargs):
        async with self.client.get(self.url + path, **kwargs) as response:
            self.assertEqual(response.status, 200, await response.text())
            return await response.json(), response.headers["ETag"]

    def javascript(self, script, value):
        result = subprocess.run(["node", "--input-type=module", "-e", script],
            input=json.dumps(value), text=True, capture_output=True, check=True,
            cwd=Path(__file__).resolve().parents[1])
        return json.loads(result.stdout)

    async def test_summary_retains_exact_formal_scores_denominators_and_lineage(self):
        summaries = [
            {"normalized_score": 0, "task_success": False}, {"normalized_score": 1, "task_success": True},
            {"score": .4}, {"normalized_score": None, "score": .2}, {"normalized_score": False},
            {"normalized_score": "0.5"}, {"normalized_score": 1.1}, {},
            *({"normalized_score": .8, "score_status": status} for status in ("unavailable", "unscored", "invalid")),
            {"normalized_score": .8, "scored_episode": False}, {"normalized_score": .8, "valid_episode": False},
            {"normalized_score": .8, "primary_outcome": "unscored_infrastructure"},
            {"normalized_score": .8, "infrastructure_error": True},
            {"normalized_score": .8, "infrastructure_error": True, "scored_episode": True},
        ]
        snapshot = copy.deepcopy(self.library.group_snapshot)
        group = snapshot["groups"][0]
        group.pop("revision"); group.pop("details_loaded")
        template = copy.deepcopy(group["cases"][0])
        group["cases"] = [{**template, "case_key": str(index), "run": {**template["run"],
            "summary": {**summary, "cleanup_status": "omitted", "termination_reason": "omitted"}}}
            for index, summary in enumerate(summaries)]
        group["cases"].append({**template, "case_key": "missing", "run": None})
        group["case_count"] = len(group["cases"])
        with patch.object(self.library.experiment_groups, "snapshot", return_value=snapshot):
            await self.library.refresh()
        full, full_tag = await self.get("/v1/experiment-groups")
        summary, summary_tag = await self.get("/v1/experiment-groups?view=summary")
        explicit, _ = await self.get("/v1/experiment-groups?view=full")
        detail, _ = await self.get(f"/v1/experiment-groups/{self.group_id}")
        self.assertEqual(detail, full["groups"][0])
        self.assertEqual(explicit["groups"], full["groups"])
        self.assertNotEqual(full_tag, summary_tag)
        compact = summary["groups"][0]
        self.assertFalse(compact["details_loaded"])
        self.assertTrue(detail["details_loaded"])
        self.assertEqual({key: value for key, value in compact.items() if key not in {"cases", "details_loaded"}},
                         {key: value for key, value in detail.items() if key not in {"cases", "details_loaded"}})
        for row in compact["cases"]:
            self.assertEqual(set(row), {"case_key", "run"})
            if row["run"]:
                self.assertEqual(set(row["run"]), {"summary"})
                self.assertNotIn("cleanup_status", row["run"]["summary"])
                self.assertNotIn("termination_reason", row["run"]["summary"])
        scores = self.javascript("""
            import { formalScore } from './web/experiment-metrics.js';
            let input = ''; for await (const chunk of process.stdin) input += chunk;
            console.log(JSON.stringify(JSON.parse(input).map(group => group.cases.map(row =>
                [row.case_key, row.run ? formalScore(row.run.summary) : null, row.run?.summary?.task_success ?? null]))));
        """, [detail, compact])
        self.assertEqual(scores[0], scores[1])
        self.assertEqual([row[1] for row in scores[1]], [0, 1, .4, .2, None, None, None, None,
                                                        None, None, None, None, None, None, None, .8, None])
        for path, status in (("/v1/experiment-groups?view=unknown", 400),
                             ("/v1/experiment-groups/no-such-group", 404)):
            async with self.client.get(self.url + path, headers={"If-None-Match": "*"}) as response:
                self.assertEqual(response.status, status)

    async def test_merged_execution_links_resolve_to_the_same_group(self):
        snapshot = copy.deepcopy(self.library.group_snapshot)
        snapshot["groups"][0]["id_aliases"] = ["group-old-recovery"]
        snapshot["groups"][0]["execution_groups"] = [{"id": self.group_id}, {"id": "group-old-recovery"}]
        with patch.object(self.library.experiment_groups, "snapshot", return_value=snapshot):
            await self.library.refresh()
        current, _ = await self.get(f"/v1/experiment-groups/{self.group_id}")
        previous, previous_tag = await self.get("/v1/experiment-groups/group-old-recovery")
        self.assertEqual(previous, current)
        self.assertEqual(len(current["execution_groups"]), 2)
        async with self.client.get(self.url + "/v1/experiment-groups/group-old-recovery",
                                   headers={"If-None-Match": previous_tag}) as response:
            self.assertEqual(response.status, 304)
        listing, _ = await self.get("/v1/experiment-groups?view=summary")
        self.assertEqual([g["id"] for g in listing["groups"]], [self.group_id])
        self.assertNotIn("execution_groups", listing["groups"][0])
        self.assertEqual(listing["groups"][0]["id_aliases"], ["group-old-recovery"])

    async def test_revalidation_across_refresh_score_changes_and_query_variants(self):
        paths = ["/v1/experiments", "/v1/experiment-groups", "/v1/experiment-groups?view=summary",
                 f"/v1/experiment-groups/{self.group_id}", f"/v1/experiments/{self.identifier}",
                 f"/v1/experiments/{self.identifier}/media"]
        before = {path: await self.get(path) for path in paths}
        await self.library.refresh()
        self.assertNotEqual(before[paths[0]][0]["observed_at"], self.library.refreshed_at)
        for path, (_, etag) in before.items():
            for method in ("GET", "HEAD"):
                async with self.client.request(method, self.url + path, headers={"If-None-Match": etag}) as response:
                    self.assertEqual(response.status, 304, path)
                    self.assertEqual(await response.read(), b"")
                    self.assertEqual(response.headers["ETag"], etag)
                    self.assertEqual(response.headers["Cache-Control"], "private, no-cache")
                    self.assertEqual(response.headers["Vary"], "Accept-Encoding")
            async with self.client.head(self.url + path) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.read(), b"")
                self.assertEqual(response.headers["ETag"], etag)
        _, variant_tag = await self.get("/v1/experiments?limit=50", headers={"If-None-Match": before[paths[0]][1]})
        self.assertNotEqual(variant_tag, before[paths[0]][1])
        for header in ('"no-match", ' + before[paths[0]][1], before[paths[0]][1].removeprefix("W/"), "*"):
            async with self.client.get(self.url + paths[0], headers={"If-None-Match": header}) as response:
                self.assertEqual(response.status, 304)
        for header in ("garbage", before[paths[0]][1].strip('W/"'), '"no-match"', '"*"', 'W/"*"'):
            await self.get(paths[0], headers={"If-None-Match": header})
        atomic_json(self.summary, dict(normalized_score=1, task_success=True, scored_episode=True, valid_episode=True))
        await self.library.refresh()
        for path in paths[:-1]:
            _, tag = await self.get(path, headers={"If-None-Match": before[path][1]})
            self.assertNotEqual(tag, before[path][1])
        # A score-only change does not change the media inventory.
        async with self.client.get(self.url + paths[-1], headers={"If-None-Match": before[paths[-1]][1]}) as response:
            self.assertEqual(response.status, 304)
        _, group_tag = await self.get(paths[3])
        register_run(self.root / "unrelated", category="synthetic", task="other", run_id="other")
        await self.library.refresh()
        async with self.client.get(self.url + paths[3], headers={"If-None-Match": group_tag}) as response:
            self.assertEqual(response.status, 304)

    async def test_media_preserves_camera_aliases_without_reviews_or_full_reports(self):
        for name in ("head.mp4", "recording-a.mp4", "recording-b.mp4", "recording-c.mp4"):
            (self.run / name).write_bytes(b"synthetic local media")
        atomic_json(self.run / "report.json", dict(secret="never emitted", videos=[
            dict(camera_id="head_camera", video_path="/old/recording-a.mp4", diagnostics="never emitted"),
            dict(camera_id="left_wrist_camera", video_path="recording-b.mp4", private="never emitted"),
            dict(camera_id="cam_right_wrist", video_path="recording-c.mp4"), None]))
        atomic_json(self.run / "array-report.json", ["not a camera report"])
        path = f"/v1/experiments/{self.identifier}/media"
        with patch.object(self.library.task_reviews, "get", side_effect=AssertionError("review must not be built")), \
                patch.object(self.library, "detail", side_effect=AssertionError("full detail must not be built")):
            media, etag = await self.get(path)
        self.assertEqual(set(media), {"id", "view_url", "replay_available", "media", "reports"})
        self.assertEqual(len(media["reports"]), 1)
        self.assertEqual(set(media["reports"][0]), {"content"})
        self.assertEqual(set(media["reports"][0]["content"]), {"videos"})
        self.assertNotIn("never emitted", json.dumps(media))
        for descriptor in media["reports"][0]["content"]["videos"]:
            self.assertLessEqual(set(descriptor), {"camera_id", "video_path", "url"})
        cameras = self.javascript("""
            import { cameraVideos } from './web/experiment-media.js';
            globalThis.location = {href: 'http://localhost/', origin: 'http://localhost'};
            let input = ''; for await (const chunk of process.stdin) input += chunk;
            const data = JSON.parse(input); console.log(JSON.stringify(cameraVideos(data.media, data.reports)));
        """, media)
        self.assertEqual({key: value["name"] for key, value in cameras.items()},
                         {"head": "recording-a.mp4", "left": "recording-b.mp4", "right": "recording-c.mp4"})
        (self.run / "extra.mp4").write_bytes(b"new local media")
        _, changed_tag = await self.get(path, headers={"If-None-Match": etag})
        self.assertNotEqual(changed_tag, etag)
        with patch.object(self.library.task_reviews, "get", return_value={"fixture": "full detail retained"}):
            detail, _ = await self.get(f"/v1/experiments/{self.identifier}")
        self.assertEqual(detail["task_review"], {"fixture": "full detail retained"})
        self.assertIn("never emitted", json.dumps(detail["reports"]))

    async def test_conditional_requests_still_validate_origin_host_and_run_paths(self):
        paths = ["/v1/experiments", "/v1/experiment-groups?view=summary",
                 f"/v1/experiment-groups/{self.group_id}", f"/v1/experiments/{self.identifier}",
                 f"/v1/experiments/{self.identifier}/media"]
        for path in paths:
            _, etag = await self.get(path)
            for denied in ({"Origin": "https://other.example"}, {"Host": "other.example"}):
                async with self.client.get(self.url + path, headers={"If-None-Match": etag, **denied}) as response:
                    self.assertEqual(response.status, 403)
        path = paths[-1]
        _, etag = await self.get(path)
        original = self.run.with_name("moved")
        self.run.rename(original)
        self.run.symlink_to(original, target_is_directory=True)
        async with self.client.get(self.url + path, headers={"If-None-Match": etag}) as response:
            self.assertEqual(response.status, 400)
            self.assertIn("symlink_run_directory_denied", await response.text())

    async def test_json_compression_negotiation_and_uncompressed_video_ranges(self):
        path = "/v1/experiment-groups?view=summary"
        tag = None
        async with ClientSession(auto_decompress=False) as client:
            for accepted, expected in (("gzip", "gzip"), ("deflate", "deflate"),
                                       ("gzip;q=0, deflate;q=0.5", "deflate"),
                                       ("deflate;q=0.3, gzip;q=0.8", "gzip"),
                                       ("gzip;q=0, deflate;q=0", None), ("identity", None),
                                       ("gzip;q=0.5, identity;q=1", None), ("br", None), ("*", "gzip")):
                async with client.get(self.url + path, headers={"Accept-Encoding": accepted}) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get("Content-Encoding"), expected)
                    body = await response.read()
                    decoded = gzip.decompress(body) if expected == "gzip" else zlib.decompress(body) if expected == "deflate" else body
                    self.assertFalse(json.loads(decoded)["groups"][0]["details_loaded"])
                    if tag is not None: self.assertEqual(response.headers["ETag"], tag)
                    tag = response.headers["ETag"]
            async with client.get(self.url + path, headers={"Accept-Encoding": "*;q=0"}) as response:
                self.assertEqual(response.status, 406)
            (self.run / "head.mp4").write_bytes(b"synthetic-video-bytes")
            media, _ = await self.get(f"/v1/experiments/{self.identifier}/media")
            async with client.get(self.url + media["media"][0]["url"], headers={"Accept-Encoding": "gzip", "Range": "bytes=0-8"}) as response:
                self.assertEqual(response.status, 206)
                self.assertNotIn("Content-Encoding", response.headers)
                self.assertEqual(await response.read(), b"synthetic")


if __name__ == "__main__": unittest.main()
