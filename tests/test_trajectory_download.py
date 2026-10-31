"""Library downloads remain run scoped, immutable, and behind the origin guard."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web

from auriga.apps.library.experiments import allocate_run, atomic_json, catalog_id
from auriga.apps.library.service import Library


class TrajectoryDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runs = [allocate_run(self.root, category="synthetic", task="download", run_id=name)
                     for name in ("first", "second")]
        for run in self.runs:
            with sqlite3.connect(run / "events.sqlite") as database:
                database.execute("CREATE TABLE fixture (value TEXT)")
            for name, value in (("evaluation.json", {}), ("effective_config.json", {}),
                                ("runtime.json", {"status": "completed"})):
                atomic_json(run / name, value)
            (run / "blobs").mkdir()
            (run / "blobs" / "sample").write_bytes(b"original")
        self.library = Library(self.root, public_origin="https://example.test")
        self.runner = web.AppRunner(self.library.app(), shutdown_timeout=.2)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.client = ClientSession()
        self.identifiers = [catalog_id(run.relative_to(self.root).as_posix()) for run in self.runs]
        self.calls = []

        def export(source, target):
            self.calls.append(source)
            time.sleep(.03)  # Concurrent requests must share one export.
            target.write_bytes(b"PK-fixture-" + source.name.encode() + (source / "blobs" / "sample").read_bytes())
            return dict(path=str(target), sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                        byte_size=target.stat().st_size, source_run_id=source.name)

        self.export = export
        self.module = patch.dict("sys.modules", {"auriga.apps.replay.trajectory_bundle": SimpleNamespace(export_bundle=export)})
        self.module.start()

    async def asyncTearDown(self):
        self.module.stop()
        await self.client.close()
        await self.runner.cleanup()
        self.temporary.cleanup()

    def endpoint(self, index=0):
        return self.url + f"/v1/experiments/{self.identifiers[index]}/trajectory-bundle"

    async def read_download(self, index=0, **kwargs):
        async with self.client.get(self.endpoint(index), **kwargs) as response:
            return response.status, dict(response.headers), await response.read()

    async def test_download_cache_range_scope_and_source_preservation(self):
        original = {str(path): path.read_bytes() for run in self.runs for path in run.rglob("*") if path.is_file()}
        replies = await asyncio.gather(self.read_download(), self.read_download())
        status, headers, body = replies[0]
        self.assertEqual(status, 200)
        self.assertEqual(body, replies[1][2])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn("attachment; filename=", headers["Content-Disposition"])
        self.assertEqual(headers["X-SHA256"], hashlib.sha256(body).hexdigest())
        async with self.client.head(self.endpoint()) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(int(response.headers["Content-Length"]), len(body))
            self.assertEqual(await response.read(), b"")
        status, headers, partial = await self.read_download(headers={"Range": "bytes=0-3"})
        self.assertEqual((status, partial), (206, body[:4]))
        self.assertEqual(headers["Content-Range"], f"bytes 0-3/{len(body)}")
        self.assertNotEqual((await self.read_download(1))[2], body)
        self.assertEqual(original, {path: Path(path).read_bytes() for path in original})
        self.assertEqual(len(list((self.root / "service" / "trajectory-bundles").glob("*.zip"))), 2)
        (self.runs[0] / "blobs" / "sample").write_bytes(b"modified")
        changed = await self.read_download()
        self.assertNotEqual(changed[2], body)
        self.assertEqual(len(self.calls), 3)

    async def test_unfinished_imported_missing_and_invalid_sources_are_rejected(self):
        entry = self.library.catalog.get(self.identifiers[0])
        self.assertEqual(self.library.trajectory_download(entry)["status"], "available")
        for status in ("running", "unknown", "starting"):
            atomic_json(self.runs[0] / "runtime.json", {"status": status})
            self.assertEqual((await self.read_download())[0], 409)
        atomic_json(self.runs[0] / "runtime.json", {"status": "completed"})
        atomic_json(self.runs[0] / "import-inventory.json", {"replay_database": "replay/events.sqlite"})
        self.assertEqual((await self.read_download())[0], 409)
        (self.runs[0] / "import-inventory.json").unlink()
        (self.runs[0] / "effective_config.json").unlink()
        self.assertEqual((await self.read_download())[0], 409)
        self.assertEqual(self.calls, [])
        atomic_json(self.runs[0] / "effective_config.json", {})
        with patch.dict("sys.modules", {"auriga.apps.replay.trajectory_bundle": SimpleNamespace(
                export_bundle=lambda *_: (_ for _ in ()).throw(ValueError("dispatched_trace_has_gaps")))}):
            status, _, body = await self.read_download()
        self.assertEqual(status, 400)
        self.assertIn("dispatched_trace_has_gaps", json.loads(body)["error"])

    async def test_source_change_during_export_and_cache_path_escape_fail_closed(self):
        def changing_export(source, target):
            result = self.export(source, target)
            (source / "blobs" / "sample").write_bytes(b"changed during export")
            return result

        with patch.dict("sys.modules", {"auriga.apps.replay.trajectory_bundle": SimpleNamespace(export_bundle=changing_export)}):
            status, _, body = await self.read_download()
        self.assertEqual(status, 400)
        self.assertIn("source_changed", json.loads(body)["error"])
        cache = self.root / "service" / "trajectory-bundles"
        self.assertEqual(list(cache.glob("*.zip")), [])
        cache.rmdir()
        with tempfile.TemporaryDirectory() as outside:
            cache.symlink_to(outside, target_is_directory=True)
            status, _, body = await self.read_download()
            self.assertEqual(status, 400)
            self.assertIn("path_outside_run", json.loads(body)["error"])
            self.assertEqual(list(Path(outside).iterdir()), [])

    async def test_origin_guard_unknown_id_and_write_rejection(self):
        for headers in ({"Origin": "https://foreign.test"}, {"Host": "foreign.test"}):
            self.assertEqual((await self.read_download(headers=headers))[0], 403)
        self.assertEqual((await self.read_download(headers={"Origin": "https://example.test"}))[0], 200)
        async with self.client.get(self.url + "/v1/experiments/exp-" + "f" * 32 + "/trajectory-bundle") as response:
            self.assertEqual(response.status, 404)
        async with self.client.post(self.endpoint()) as response:
            self.assertEqual(response.status, 405)


if __name__ == "__main__":
    unittest.main()
