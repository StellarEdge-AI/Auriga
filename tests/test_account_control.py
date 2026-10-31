"""Offline account relay and actual native Codex bootstrap; no model/GPU."""

import base64
from datetime import datetime, timezone
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import selectors
import ssl
import subprocess
import tempfile
import threading
import time
import tomllib
import unittest
from unittest import mock

from auriga.cortex.session.account_control import AccountControlRelay, ACCOUNT_HOST, ACCOUNT_PATH
from auriga.cortex.session.codex import CodexSession
from auriga.core.storage.store import Store


HTTPSConnection = http.client.HTTPSConnection
ACCOUNT_ID = "00000000-0000-4000-8000-000000000001"


class AccountControlTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.requests = []
        self.response_status, self.response_headers = 200, {}
        self.response_body = json.dumps({"accounts": [{"id": ACCOUNT_ID,
            "workspace_backend_origin": "NO_CONSTRAINT", "account_routing_override": "NO_CONSTRAINT"}]}).encode()
        self.response_delay = 0
        owner = self

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                owner.requests.append((self.path, dict(self.headers)))
                time.sleep(owner.response_delay)
                self.send_response(owner.response_status)
                self.send_header("Content-Length", str(len(owner.response_body)))
                for key, value in owner.response_headers.items(): self.send_header(key, value)
                self.end_headers()
                try: self.wfile.write(owner.response_body)
                except (BrokenPipeError, ConnectionResetError): pass

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.daemon_threads = True
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever,
            kwargs={"poll_interval": .02}, daemon=True)
        self.upstream_thread.start()

        def connection(host, *, timeout):
            self.assertEqual(host, ACCOUNT_HOST, "the request cannot choose its upstream")
            return http.client.HTTPConnection("127.0.0.1", self.upstream.server_port, timeout=timeout)
        self.connector = mock.patch("auriga.cortex.session.account_control.HTTPSConnection", side_effect=connection)
        self.connector.start()
        self.relay = AccountControlRelay(self.root / "private", self.root / "public-ca.pem").start()
        self.context = ssl.create_default_context(cafile=self.relay.ca_path)

    def tearDown(self):
        self.relay.close()
        self.connector.stop()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(1)
        self.temporary.cleanup()

    def request(self, path=ACCOUNT_PATH, method="GET", headers=None, body=None):
        connection = HTTPSConnection("127.0.0.1", self.relay.server.server_port,
                                     timeout=3, context=self.context)
        try:
            connection.request(method, path, body=body,
                               headers={"Authorization": "Bearer fixture-not-a-credential", **(headers or {})})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_exact_upstream_response_with_verified_local_tls_and_minimal_headers(self):
        status, body, headers = self.request(headers={"Cookie": "must-not-forward", "X-Target": "https://other.invalid"})
        self.assertEqual((status, body), (200, self.response_body))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.requests[0][0], ACCOUNT_PATH)
        self.assertEqual(self.requests[0][1]["Authorization"], "Bearer fixture-not-a-credential")
        self.assertNotIn("Cookie", self.requests[0][1])
        self.assertNotIn("X-Target", self.requests[0][1])
        self.assertEqual(self.relay.request_count, 1)
        self.assertEqual(self.relay.last_status, 200)

    def test_rejects_other_destinations_paths_methods_and_request_bodies(self):
        paths = ["/responses", "/responses/compact", "/backend-api/wham/usage",
                 ACCOUNT_PATH + "?next=https://other.invalid", ACCOUNT_PATH + "/",
                 "/backend-api/wham/../wham/accounts/check", "/backend-api/wham/accounts/%63heck",
                 "https://other.invalid" + ACCOUNT_PATH]
        for path in paths:
            with self.subTest(path=path): self.assertEqual(self.request(path)[0], 404)
        for method in ["POST", "PUT", "PATCH", "DELETE", "CONNECT", "OPTIONS"]:
            with self.subTest(method=method): self.assertEqual(self.request(method=method)[0], 405)
        self.assertEqual(self.request(body=b"unexpected-body")[0], 400)
        self.assertEqual(self.request(headers={"Authorization": ""})[0], 401)
        self.assertFalse(self.requests)
        self.assertEqual(self.relay.request_count, 0)

    def test_host_credentials_override_client_and_allow_no_client_authorization(self):
        host = {'authorization': 'Bearer fixture-host-secret', 'chatgpt-account-id': 'fixture-host-account'}
        self.relay.close()
        self.relay = AccountControlRelay(self.root / 'private', self.root / 'public-ca.pem',
                                         upstream_auth_headers=host).start()
        self.context = ssl.create_default_context(cafile=self.relay.ca_path)
        host['authorization'] = 'Bearer changed-after-construction'
        for supplied in ({'Authorization': ''}, {'Authorization': 'not-bearer'},
                         {'aUtHoRiZaTiOn': 'Bearer fixture-client-secret',
                          'cHaTgPt-AcCoUnT-Id': 'fixture-client-account'}):
            status, body, _ = self.request(headers=supplied)
            self.assertEqual((status, body), (200, self.response_body))
            sent = {key.lower(): value for key, value in self.requests[-1][1].items()}
            self.assertEqual(sent['authorization'], 'Bearer fixture-host-secret')
            self.assertEqual(sent['chatgpt-account-id'], 'fixture-host-account')
            self.assertNotIn(b'fixture-host-secret', body)
        self.assertEqual(self.relay.request_count, 3)

    def test_empty_host_auth_never_falls_back_to_client_credentials(self):
        self.relay.close()
        self.relay = AccountControlRelay(self.root / 'private', self.root / 'public-ca.pem',
                                         upstream_auth_headers={}).start()
        self.context = ssl.create_default_context(cafile=self.relay.ca_path)
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.requests, [])

    def test_invalid_host_auth_validation_does_not_expose_secret(self):
        with self.assertRaises(ValueError) as raised:
            AccountControlRelay(self.root / 'unused', self.root / 'unused.pem',
                                upstream_auth_headers={'Authorization': 'fixture-secret\nInjected: yes'})
        self.assertEqual(str(raised.exception), 'invalid_upstream_auth_headers')

    def test_redirects_errors_oversized_and_slow_responses_are_bounded(self):
        self.response_status = 302
        self.response_headers = {"Location": "https://other.invalid/credential-sink"}
        self.response_body = b"fixture-private-upstream-detail"
        status, body, headers = self.request()
        self.assertEqual(status, 502)
        self.assertNotIn("Location", headers)
        self.assertNotIn(b"fixture-private", body)
        self.assertEqual(len(self.requests), 1, "a redirect is never followed")
        self.response_status = 401
        self.assertEqual(self.request()[0], 401, "Codex retains its native unauthorized semantics")
        self.response_status, self.response_headers = 200, {}
        self.relay.max_response_bytes = 32
        self.response_body = b'{"accounts":[],"padding":"' + b"x" * 100 + b'"}'
        self.assertEqual(self.request()[0], 502)
        self.relay.max_response_bytes = 1024
        self.response_body, self.response_delay = b'{"accounts":[]}', .25
        self.relay.timeout_s = .05
        started = time.monotonic()
        self.assertEqual(self.request()[0], 502)
        self.assertLess(time.monotonic() - started, .5)

    def profile(self):
        profile = self.root / "profile"
        profile.mkdir()
        (profile / "config.toml").write_text('model_provider = "openai"\n')
        claims = {"email": "fixture@example.invalid", "https://api.openai.com/auth": {
            "chatgpt_account_id": ACCOUNT_ID, "chatgpt_user_id": "fixture-user", "chatgpt_plan_type": "plus"}}
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        token = encode({"alg": "none"}) + "." + encode(claims) + ".synthetic-signature"
        (profile / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
            "id_token": token, "access_token": "fixture-not-a-credential", "refresh_token": "fixture-refresh",
            "account_id": ACCOUNT_ID}, "last_refresh": datetime.now(timezone.utc).isoformat()}))
        return profile

    def test_actual_sandbox_codex_uses_placeholder_and_host_relay_keeps_chatgpt_credentials(self):
        profile = self.profile()
        store = Store(self.root / "run", dict(run_id="account-fixture", episode_id="none",
                                             env_instance_id="none", env_generation=0))
        from auriga.core.config import Config
        session = CodexSession(store.root, store, lambda params: None,
                               config=Config.from_dict({"model": {"codex_home": str(profile)}}))
        process = selector = None
        try:
            # Authentication is frozen at construction; a later host environment
            # change cannot switch the selected provider or credentials.
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "unused-home")}): session._prepare()
            config = tomllib.loads((session.codex_home / "config.toml").read_text())
            self.assertFalse((session.private / "isolation.json").exists())
            self.assertTrue(config["model_providers"][config["model_provider"]]["base_url"].startswith("http://127.0.0.1:"))
            self.assertEqual(config["chatgpt_base_url"], session.account_control.base_url)
            self.assertEqual(session.env["CODEX_CA_CERTIFICATE"], str(session.account_control.ca_path))
            self.assertEqual(session.env["CODEX_HOME"], str(session.codex_home))
            self.assertEqual(config["sandbox_mode"], "danger-full-access")
            self.assertEqual(config["web_search"], "disabled")
            for feature in ("shell_tool", "unified_exec"):
                self.assertTrue(config["features"][feature], feature)
            self.assertFalse(config["features"]["network_proxy"])
            self.assertEqual(json.loads((session.codex_home / 'auth.json').read_text()),
                             {'OPENAI_API_KEY': 'auriga-local-relay'})
            stderr_path = session.private / "fixture-stderr.log"
            with stderr_path.open("wb") as stderr:
                # Match CodexSession.start with the fixture's frozen environment.
                process = subprocess.Popen(session.command[:-1] + ["stdio://"], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=stderr, env=session.env, cwd=session.actor_dir)
                session.process = process
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                pending = b""

                def send(value):
                    process.stdin.write((json.dumps(value) + "\n").encode()); process.stdin.flush()

                def receive(identifier):
                    nonlocal pending
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        self.assertTrue(selector.select(deadline - time.monotonic()), "native RPC timed out")
                        chunk = os.read(process.stdout.fileno(), 65536)
                        self.assertTrue(chunk, "native app-server exited: " +
                                        stderr_path.read_text(errors="replace"))
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            value = json.loads(line)
                            if value.get("id") == identifier: return value
                    self.fail("native RPC did not respond")

                send({"id": 1, "method": "initialize", "params": {"clientInfo": {
                    "name": "offline-account-fixture", "title": "Offline account fixture", "version": "1"},
                    "capabilities": {"experimentalApi": True}}})
                self.assertIn("result", receive(1)); send({"method": "initialized"})
                send({"id": 2, "method": "account/read", "params": {"refreshToken": False}})
                account = receive(2)
                self.assertIn("result", account)
                self.assertEqual(account["result"]["account"]["type"], "apiKey")
                self.assertEqual(session.model_proxy.request_count, 0)
                self.assertEqual(session.model_proxy.attempt_count, 0)
                self.assertEqual(session.account_control.request_count, 0)
                connection = HTTPSConnection('127.0.0.1', session.account_control.server.server_port,
                    timeout=3, context=ssl.create_default_context(cafile=session.account_control.ca_path))
                try:
                    connection.request('GET', ACCOUNT_PATH)
                    response = connection.getresponse()
                    self.assertEqual((response.status, response.read()), (200, self.response_body))
                finally:
                    connection.close()
                sent = {key.lower(): value for key, value in self.requests[-1][1].items()}
                self.assertEqual(sent['authorization'], 'Bearer fixture-not-a-credential')
                self.assertEqual(sent['chatgpt-account-id'], ACCOUNT_ID)
        finally:
            if selector: selector.close()
            if process:
                process.stdin.close(); process.stdout.close()
            session.close()
            store.close()


if __name__ == "__main__": unittest.main()
