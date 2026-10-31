"""Synthetic HTTP/WS checks for the authenticated public-origin entry."""
import tempfile
import unittest
from aiohttp import ClientSession, WSServerHandshakeError, web
from auriga.apps.library.experiments import allocate_run, atomic_json, catalog_id
from auriga.apps.library.service import Library


class PublicOriginCheck(unittest.IsolatedAsyncioTestCase):
    async def test_page_and_assets_revalidate_after_deployment(self):
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as assets:
            static = Path(assets)
            (static / "library.html").write_text('<script src="/assets/library.js?v=new"></script>')
            (static / "index.html").write_text('<script src="/app.js?v=new"></script>')
            for name in ("library.js", "app.js"):
                (static / name).write_text("window.synthetic_fixture = true;")
            run = allocate_run(directory, category="synthetic", task="cache-check")
            identifier = catalog_id(run.relative_to(directory).as_posix())
            with patch("auriga.apps.library.service.STATIC", static):
                runner = web.AppRunner(Library(directory).app(), shutdown_timeout=.2)
                await runner.setup()
                try:
                    site = web.TCPSite(runner, "127.0.0.1", 0)
                    await site.start()
                    local = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
                    async with ClientSession() as client:
                        for url in ("/", f"/runs/{identifier}/"):
                            async with client.get(local + url) as response:
                                self.assertEqual(response.status, 200)
                                self.assertEqual(response.headers["Cache-Control"], "no-store")
                                self.assertIn("?v=new", await response.text())
                        for url in ("/assets/library.js?v=new", "/app.js?v=new"):
                            async with client.get(local + url) as response:
                                self.assertEqual(response.status, 200)
                                self.assertEqual(response.headers["Cache-Control"], "no-cache, max-age=0, must-revalidate")
                                etag = response.headers["ETag"]
                            async with client.get(local + url, headers={"If-None-Match": etag}) as response:
                                self.assertEqual(response.status, 304)
                                self.assertIn("must-revalidate", response.headers["Cache-Control"])
                finally:
                    await runner.cleanup()

    async def test_exact_public_origin_validation_http_and_websocket(self):
        origin = "https://library.example.org"
        with tempfile.TemporaryDirectory() as directory:
            for invalid in ("", "http://library.example.org", "https://*.example.org", origin + "/",
                            origin + "/path", origin + "?", origin + "#", origin + ":bad",
                            origin + ":65536", "https://user:password@library.example.org",
                            "https://@library.example.org", origin + "\n", "\t" + origin,
                            "https://library.example.org\\evil", origin + "\x7f"):
                with self.subTest(invalid=repr(invalid)), self.assertRaises(ValueError):
                    Library(directory, public_origin=invalid)
            run = allocate_run(directory, category="synthetic", task="origin-check")
            (run / "terminal.bin").write_bytes(b"synthetic public-origin terminal")
            atomic_json(run / "terminal.json", dict(session_id="synthetic-public-pty", exit_code=0))
            identifier = catalog_id(run.relative_to(directory).as_posix())
            async with ClientSession() as client:
                for configured in (None, origin):
                    library = Library(directory, public_origin=configured)
                    runner = web.AppRunner(library.app(), shutdown_timeout=.2)
                    await runner.setup()
                    try:
                        site = web.TCPSite(runner, "127.0.0.1", 0)
                        await site.start()
                        local = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
                        for headers in ({}, {"Origin": local}, {"Origin": origin},
                                        {"Origin": "https://other.example.org"},
                                        {"Origin": origin, "Host": "library.example.org"}):
                            expected = 200 if not headers or headers.get("Origin") == local or headers == {"Origin": configured} else 403
                            async with client.get(local + "/v1/experiments", headers=headers) as response:
                                self.assertEqual(response.status, expected, (configured, headers))
                        for ws_origin in (local, origin, "https://other.example.org"):
                            url = local + f"/runs/{identifier}/v1/terminal"
                            if ws_origin == local or ws_origin == configured:
                                async with client.ws_connect(url, headers={"Origin": ws_origin}) as ws:
                                    self.assertEqual((await ws.receive_json(timeout=3))["type"], "ready")
                                    self.assertEqual((await ws.receive_json(timeout=3))["type"], "output")
                            else:
                                with self.assertRaises(WSServerHandshakeError) as rejected:
                                    await client.ws_connect(url, headers={"Origin": ws_origin})
                                self.assertEqual(rejected.exception.status, 403)
                    finally:
                        await runner.cleanup()


if __name__ == "__main__": unittest.main()
