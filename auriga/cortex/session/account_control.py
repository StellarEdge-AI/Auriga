"""One read-only ChatGPT account-discovery route for the isolated native TUI.

The model relay remains separate. Neither account responses nor credentials are
logged, and no account request can be used to reach a model or arbitrary URL.
"""

import http.client
from http.client import HTTPSConnection
import json
from pathlib import Path
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .model_proxy import upstream_auth_headers_copy


ACCOUNT_HOST = "chatgpt.com"
ACCOUNT_PATH = "/backend-api/wham/accounts/check"
ACCOUNT_BASE_URL = "https://chatgpt.com/backend-api"


def _local_certificate(directory, ca_path):
    """Keep signing/server keys private; Codex receives only the public CA."""
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    ca_path = Path(ca_path)
    ca_path.parent.mkdir(parents=True, exist_ok=True)
    ca_key, server_key = directory / "ca.key", directory / "server.key"
    request, certificate = directory / "server.csr", directory / "server.pem"
    extensions = directory / "server.ext"
    extensions.write_text("basicConstraints=critical,CA:FALSE\n"
                          "keyUsage=critical,digitalSignature,keyEncipherment\n"
                          "extendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1\n")
    commands = [
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", "/CN=Auriga run account relay CA", "-keyout", str(ca_key), "-out", str(ca_path),
         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign"],
        ["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
         "-subj", "/CN=Auriga local account relay", "-keyout", str(server_key), "-out", str(request)],
        ["openssl", "x509", "-req", "-in", str(request), "-CA", str(ca_path),
         "-CAkey", str(ca_key), "-set_serial", "1", "-days", "2", "-out", str(certificate),
         "-extfile", str(extensions)],
    ]
    for command in commands:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    for path in directory.iterdir():
        path.chmod(0o600)
    ca_path.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, server_key)
    return context


class AccountControlRelay:
    def __init__(self, directory, ca_path, *, timeout_s=10, max_response_bytes=1024 * 1024,
                 upstream_auth_headers=None):
        self._upstream_auth_headers = upstream_auth_headers_copy(upstream_auth_headers)
        self.directory, self.ca_path = Path(directory), Path(ca_path)
        self.timeout_s, self.max_response_bytes = timeout_s, max_response_bytes
        self.server = self.thread = self.base_url = None
        self.closed = False
        self.request_count = 0
        self.last_status = None
        self._slots = threading.BoundedSemaphore(4)
        self._lock = threading.Lock()

    def _get_account(self, authorization):
        # http.client does not follow redirects or honor outbound proxy variables.
        connection = HTTPSConnection(ACCOUNT_HOST, timeout=self.timeout_s)
        started = time.monotonic()
        try:
            headers = {
                "Authorization": authorization, "Accept": "application/json",
                "Accept-Encoding": "identity", "User-Agent": "auriga-native-account-relay/1",
            }
            if self._upstream_auth_headers is not None:
                headers.update(self._upstream_auth_headers)
            connection.request("GET", ACCOUNT_PATH, headers=headers)
            response = connection.getresponse()
            with self._lock:
                self.last_status = response.status
            if response.status != 200:
                # Preserve authentication failure semantics without returning an
                # upstream redirect, diagnostic body, cookie or arbitrary header.
                return (response.status if 400 <= response.status < 500 else 502), None
            if response.getheader("Content-Encoding", "identity") != "identity":
                return 502, None
            raw = bytearray()
            while True:
                remaining = self.timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError()
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, self.max_response_bytes + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > self.max_response_bytes:
                    return 502, None
            if response.length not in (None, 0):
                return 502, None
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("accounts"), (list, dict)):
                return 502, None
            return 200, bytes(raw)
        finally:
            connection.close()

    def start(self):
        if self.server is not None or self.closed:
            raise ValueError("account_control_already_started_or_closed")
        context = _local_certificate(self.directory, self.ca_path)
        owner = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

            def finish_request(self, request, client_address):
                # Handshake inside the worker so a slow peer cannot block accept.
                request.settimeout(owner.timeout_s)
                try:
                    with context.wrap_socket(request, server_side=True) as secured:
                        super().finish_request(secured, client_address)
                except (OSError, ssl.SSLError):
                    pass

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def reply(self, status, body=None):
                length = self.headers.get("Content-Length", "0")
                if status >= 400 and length.isdigit() and 0 < int(length) <= 16384:
                    # Consume only a small rejected body so TLS close does not
                    # discard the error response with an unread-data TCP reset.
                    self.rfile.read(int(length))
                body = body if body is not None else b'{"error":"account_control_request_failed"}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                self.wfile.write(body)

            def do_GET(self):
                if self.path != ACCOUNT_PATH:
                    return self.reply(404)
                if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                    return self.reply(400)
                authorization = (self.headers.get("Authorization", "") if owner._upstream_auth_headers is None
                                 else owner._upstream_auth_headers.get("Authorization", ""))
                if not authorization.startswith("Bearer ") or not 7 < len(authorization) <= 16384:
                    return self.reply(401)
                if owner.closed or not owner._slots.acquire(blocking=False):
                    return self.reply(503)
                try:
                    with owner._lock:
                        owner.request_count += 1
                    try:
                        status, body = owner._get_account(authorization)
                    except (OSError, ValueError, http.client.HTTPException):
                        status, body = 502, None
                    self.reply(status, body)
                finally:
                    owner._slots.release()

            def do_POST(self): return self.reply(405)
            def do_PUT(self): return self.reply(405)
            def do_DELETE(self): return self.reply(405)
            def do_PATCH(self): return self.reply(405)
            def do_CONNECT(self): return self.reply(405)
            def do_HEAD(self): return self.reply(405)
            def do_OPTIONS(self): return self.reply(405)

        self.server = Server(("127.0.0.1", 0), Handler)
        self.base_url = f"https://127.0.0.1:{self.server.server_port}/backend-api"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.closed = True
        if self.server:
            if self.thread and self.thread.is_alive():
                self.server.shutdown()
            self.server.server_close()
        if self.thread and self.thread.ident is not None:
            self.thread.join(1)

    def __enter__(self): return self.start()
    def __exit__(self, *_): self.close()
