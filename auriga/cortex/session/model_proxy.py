"""Buffered Responses relay: no request deadline, bounded provider-error retries.

Actor, helper and compact completions share the budget. Attempts and unknown usage
are private evidence; no partial model/tool output is released before validation.
"""
import base64
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import queue
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
PRIVATE_HEADERS = {"x-auriga-deadline-unix-ms", "x-auriga-source", "x-auriga-request-id"}
AUTH_HEADERS = {"authorization": "Authorization", "chatgpt-account-id": "ChatGPT-Account-Id"}
CLIENT_AUTH_HEADERS = set(AUTH_HEADERS) | {"cookie", "x-api-key", "api-key", "openai-organization", "openai-project"}
DEFAULT_MAX_RETRIES = 12  # Pinned RoboProbe joint-policy default; excludes the first attempt.
RETRY_POLICY = "bounded_harness_retry_after_explicit_retryable_provider_error"
TRANSPORT_RECOVERY_KEYS = frozenset(("max_rounds", "initial_delay_s", "backoff_multiplier", "max_delay_s",
                                     "max_episode_delay_s", "transport_incomplete"))
MIN_TRIM_IMAGE_LIMIT = 24


def upstream_auth_headers_copy(headers):
    """Freeze host credentials without putting their values in validation errors."""
    if headers is None:
        return None
    if not isinstance(headers, dict):
        raise ValueError("invalid_upstream_auth_headers")
    copied = {}
    for name, value in headers.items():
        key = AUTH_HEADERS.get(name.lower()) if isinstance(name, str) else None
        if (key is None or key in copied or not isinstance(value, str) or not 0 < len(value) <= 16384
                or any(ord(char) < 32 or ord(char) > 126 for char in value)):
            raise ValueError("invalid_upstream_auth_headers")
        copied[key] = value
    return copied


def _terminal_sse_type(block):
    data = b"\n".join(line[5:].lstrip(b" ") for line in block.splitlines() if line.startswith(b"data:"))
    if not data or data == b"[DONE]":
        return None
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    event_type = value.get("type") if isinstance(value, dict) else None
    return event_type if event_type in {"response.completed", "response.failed", "response.incomplete", "error"} else None


def _terminal_sse_block(block):
    return _terminal_sse_type(block) is not None


def _kill(process):
    if hasattr(process, "kill_inprocess"):
        process.kill_inprocess()
        return
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


_DEFAULT_POPEN = subprocess.Popen


class _InProcessAttempt:
    """进程内 HTTP 尝试句柄，保持与子进程句柄兼容的取消和状态查询接口。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.conn = None
        self.killed = False
        self.done = threading.Event()
        self.returncode = None

    def set_connection(self, conn):
        with self.lock:
            self.conn = conn
            if self.killed and conn is not None:
                self._force_close(conn)

    @staticmethod
    def _force_close(conn):
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            conn.close()
        except OSError:
            pass

    def kill_inprocess(self):
        with self.lock:
            self.killed = True
            conn, self.conn = self.conn, None
        if conn is not None:
            self._force_close(conn)

    def poll(self):
        return self.returncode if self.done.is_set() else None

    def wait(self, timeout=None):
        self.done.wait(timeout)
        return self.returncode

    def communicate(self, *_args, **_kwargs):
        self.done.wait(0.2)
        return "", ""


def _http_worker():
    """One HTTP attempt in an independently killable process; stdout is a pipe."""
    payload = json.load(sys.stdin)
    started = time.monotonic()
    def event(kind, **fields):
        print(json.dumps(dict(kind=kind, wall_time=time.time(), elapsed_s=time.monotonic()-started, **fields)), flush=True)
    url = urlsplit(payload["url"])
    connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(url.hostname, url.port, timeout=None)
    body = base64.b64decode(payload["body"])
    phase, incoming, first_byte = "connecting_upstream", 0, None
    response = None
    raw = bytearray()
    try:
        event("model_attempt_phase", phase=phase)
        connection.connect()
        event("model_upstream_connected")
        phase = "sending_upstream_body"
        event("model_attempt_phase", phase=phase)
        connection.request("POST", url.path, body=body, headers=payload["headers"])
        event("model_request_forwarded", body_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest())
        phase = "awaiting_response_headers"
        event("model_attempt_phase", phase=phase)
        response = connection.getresponse()
        event("model_response_headers", http_status=response.status)
        phase = "streaming_response"
        event("model_attempt_phase", phase=phase)
        pending, terminal_seen = b"", False
        while chunk := response.read1(65536):
            event("model_response_bytes_received")
            raw.extend(chunk)
            incoming += len(chunk)
            if first_byte is None:
                first_byte = time.monotonic()-started
                event("model_response_first_byte")
            pending += chunk
            while match := re.search(br"\r?\n\r?\n", pending):
                block, pending = pending[:match.start()], pending[match.end():]
                if _terminal_sse_block(block):
                    terminal_seen = True
                    if pending:
                        del raw[-len(pending):]
                    break
            if terminal_seen:
                # A complete SSE terminal, not HTTP EOF, ends a Responses stream.
                event("model_response_terminal_received")
                break
        if not terminal_seen and response.length not in (None, 0):
            raise http.client.IncompleteRead(b"", response.length)
        result = dict(status=response.status, headers=dict(response.getheaders()), body=base64.b64encode(raw).decode(),
                      terminal_seen=terminal_seen)
    except Exception as error:
        result = dict(error_type=type(error).__name__, phase=phase, body=base64.b64encode(raw).decode(),
                      status=response.status if response is not None else None,
                      headers=dict(response.getheaders()) if response is not None else {})
    finally:
        connection.close()
    print(json.dumps(dict(result=result, incoming_bytes=incoming, first_upstream_byte_s=first_byte)), flush=True)


class ModelRequestProxy:
    def __init__(self, upstream_url, *, max_requests=170, request_timeout_s=None, max_retries=None,
                 on_event=None, on_limit=None, metrics_path=None, image_reuse=False,
                 image_trim=True, request_projection=None, upstream_auth_headers=None, request_context=None,
                 max_inline_images=0, transport_recovery=None, recovery_gate=None):
        self._upstream_auth_headers = upstream_auth_headers_copy(upstream_auth_headers)
        url = urlsplit(upstream_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("model_proxy_requires_http_base_url_without_credentials_or_query")
        if type(max_requests) is not int or max_requests < 1:
            raise ValueError("model_proxy_invalid_request_budget")
        if request_timeout_s is not None:
            raise ValueError("model_proxy_request_deadlines_disabled")
        from ...apps.reference.reference_api import upstream
        self.policy = upstream().joint
        self.max_retries = DEFAULT_MAX_RETRIES if max_retries is None else max_retries
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= DEFAULT_MAX_RETRIES:
            raise ValueError("model_proxy_invalid_retry_budget")
        if type(image_reuse) is not bool:
            raise ValueError("model_proxy_image_reuse_requires_boolean")
        self.image_reuse = image_reuse
        if type(image_trim) is not bool:
            raise ValueError("model_proxy_image_trim_requires_boolean")
        self.image_trim = image_trim
        if type(max_inline_images) is not int or not (max_inline_images == 0 or max_inline_images >= MIN_TRIM_IMAGE_LIMIT):
            raise ValueError("model_proxy_invalid_inline_image_limit")
        self.max_inline_images = max_inline_images
        if transport_recovery is not None:
            if not isinstance(transport_recovery, dict) or transport_recovery.keys() != TRANSPORT_RECOVERY_KEYS:
                raise ValueError("model_proxy_invalid_transport_recovery")
            transport_recovery = dict(transport_recovery) if transport_recovery["max_rounds"] > 0 else None
        if recovery_gate is not None and not callable(recovery_gate):
            raise ValueError("model_proxy_recovery_gate_requires_callable")
        self.transport_recovery, self.recovery_gate = transport_recovery, recovery_gate
        if request_projection is not None and not callable(request_projection):
            raise ValueError("model_proxy_request_projection_requires_callable")
        self.request_projection = request_projection
        if request_context is not None and not callable(request_context):
            raise ValueError("model_proxy_request_context_requires_callable")
        self.request_context = request_context
        self.upstream_url = upstream_url.rstrip("/")
        self.max_requests, self.request_timeout_s = max_requests, None
        self.on_event, self.on_limit = on_event, on_limit
        self.metrics_path = Path(metrics_path) if metrics_path else None
        if self.metrics_path:
            self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
            self.metrics_path.touch(exist_ok=False)
            self.metrics_path.chmod(0o600)
        self.lock = threading.RLock()
        self.request_count = self.attempt_count = self.logical_call_count = self.reserved_calls = 0
        self.transport_id = uuid.uuid4().hex
        self.image_context_epoch = None
        self.limit_notified = False
        self.events, self.active = [], {}
        self.thread_owners, self.cancelled_threads = {}, {}
        self._image_trim_watermarks = {}
        self._image_trim_starts = {}
        self._recovery_rounds = self._recovered_calls = self._recovery_denials = 0
        self._recovery_delay_s = 0.0
        self._wait_started_at = None
        self._wait_total_s = 0.0
        self._last_activity_at = None
        self.closed = False
        self.blocked_error = None
        self.server = self.thread = self.base_url = None
        self.callbacks = queue.Queue()
        self.callback_thread = None
        self._conn_pool = {}
        self._conn_pool_lock = threading.Lock()

    @staticmethod
    def _socket_alive(conn):
        sock = getattr(conn, "sock", None)
        if sock is None:
            return False
        try:
            readable, _, _ = select.select([sock], [], [], 0)
            return not readable
        except (OSError, ValueError):
            return False

    def _acquire_upstream_conn(self, url):
        key = (url.scheme, url.hostname, url.port)
        with self._conn_pool_lock:
            bucket = self._conn_pool.get(key)
            while bucket:
                conn = bucket.pop()
                if self._socket_alive(conn):
                    return conn, True
                try:
                    conn.close()
                except OSError:
                    pass
        connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        return connection_type(url.hostname, url.port, timeout=None), False

    def _release_upstream_conn(self, url, conn, response):
        if conn is None or response is None:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
            return
        if getattr(response, "will_close", True):
            try:
                conn.close()
            except OSError:
                pass
            return
        sock = getattr(conn, "sock", None)
        if not response.isclosed() and sock is not None:
            try:
                sock.settimeout(0.005)
                response.read()
            except Exception:
                pass
            finally:
                try:
                    sock.settimeout(None)
                except OSError:
                    pass
        if not response.isclosed() or not self._socket_alive(conn):
            try:
                conn.close()
            except OSError:
                pass
            return
        key = (url.scheme, url.hostname, url.port)
        with self._conn_pool_lock:
            if self.closed:
                try:
                    conn.close()
                except OSError:
                    pass
                return
            self._conn_pool.setdefault(key, []).append(conn)

    def _close_conn_pool(self):
        with self._conn_pool_lock:
            buckets = list(self._conn_pool.values())
            self._conn_pool.clear()
        for bucket in buckets:
            for conn in bucket:
                try:
                    conn.close()
                except OSError:
                    pass

    def _inprocess_http(self, attempt, identity, url_str, body, headers, started):
        def emit(kind, **fields):
            elapsed = time.monotonic() - started
            if kind == "model_response_bytes_received":
                with self.lock:
                    self._last_activity_at = time.monotonic()
                return
            self._record(kind, **identity, elapsed_s=elapsed, **fields)

        url = urlsplit(url_str)
        req_headers = dict(headers)
        if not any(k.lower() == "connection" for k in req_headers):
            req_headers["Connection"] = "keep-alive"
        phase, incoming, first_byte = "connecting_upstream", 0, None
        response = None
        connection = None
        reused_ok = False
        raw = bytearray()
        try:
            emit("model_attempt_phase", phase=phase)
            if identity.get("attempt_index", 1) > 1:
                self._close_conn_pool()
            connection, was_reused = self._acquire_upstream_conn(url)
            attempt.set_connection(connection)
            if not was_reused:
                connection.connect()
            emit("model_upstream_connected")
            phase = "sending_upstream_body"
            emit("model_attempt_phase", phase=phase)
            forwarded_emitted = False
            awaiting_emitted = False
            try:
                connection.request("POST", url.path, body=body, headers=req_headers)
                emit("model_request_forwarded", body_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest())
                forwarded_emitted = True
                phase = "awaiting_response_headers"
                emit("model_attempt_phase", phase=phase)
                awaiting_emitted = True
                response = connection.getresponse()
            except Exception:
                if not was_reused or attempt.killed:
                    raise
                try:
                    connection.close()
                except OSError:
                    pass
                phase = "connecting_upstream"
                connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
                connection = connection_type(url.hostname, url.port, timeout=None)
                attempt.set_connection(connection)
                connection.connect()
                phase = "sending_upstream_body"
                connection.request("POST", url.path, body=body, headers=req_headers)
                if not forwarded_emitted:
                    emit("model_request_forwarded", body_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest())
                phase = "awaiting_response_headers"
                if not awaiting_emitted:
                    emit("model_attempt_phase", phase=phase)
                response = connection.getresponse()
            emit("model_response_headers", http_status=response.status)
            phase = "streaming_response"
            emit("model_attempt_phase", phase=phase)
            pending, terminal_seen, terminal_type = b"", False, None
            while chunk := response.read1(65536):
                emit("model_response_bytes_received")
                raw.extend(chunk)
                incoming += len(chunk)
                if first_byte is None:
                    first_byte = time.monotonic() - started
                    emit("model_response_first_byte")
                pending += chunk
                while match := re.search(br"\r?\n\r?\n", pending):
                    block, pending = pending[:match.start()], pending[match.end():]
                    if (t_type := _terminal_sse_type(block)) is not None:
                        terminal_seen = True
                        terminal_type = t_type
                        if pending:
                            del raw[-len(pending):]
                        break
                if terminal_seen:
                    emit("model_response_terminal_received")
                    break
            if not terminal_seen and response.length not in (None, 0):
                raise http.client.IncompleteRead(b"", response.length)
            result = dict(status=response.status, headers=dict(response.getheaders()),
                          body=base64.b64encode(raw).decode(), terminal_seen=terminal_seen)
            is_sse = raw.lstrip().startswith((b"event:", b"data:", b":"))
            reused_ok = (not attempt.killed and 200 <= response.status < 400
                         and (terminal_type == "response.completed" if is_sse else not terminal_seen))
        except Exception as error:
            result = dict(error_type=type(error).__name__, phase=phase, body=base64.b64encode(raw).decode(),
                          status=response.status if response is not None else None,
                          headers=dict(response.getheaders()) if response is not None else {})
        finally:
            attempt.set_connection(None)
            if reused_ok:
                self._release_upstream_conn(url, connection, response)
            elif connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
            attempt.returncode = 0
            attempt.done.set()
        return dict(result=result, incoming_bytes=incoming, first_upstream_byte_s=first_byte), phase

    def _record(self, kind, **fields):
        event = {"kind": kind, "wall_time": time.time(), "transport_schema_version": "2", **fields}
        with self.lock:
            if kind in {"model_call_started", "model_request_started", "model_request_body_received",
                        "model_upstream_connected", "model_request_forwarded", "model_response_headers",
                        "model_response_first_byte", "model_response_terminal_received",
                        "model_request_finished", "model_call_completed"}:
                self._last_activity_at = time.monotonic()
            if kind == "model_call_started" and fields.get("endpoint") == "/responses/compact":
                # Synchronous with admission; on_event callbacks run asynchronously.
                self.image_context_epoch = fields.get("logical_call_id") or uuid.uuid4().hex
                thread_id = fields.get("native_thread_id") or ""
                self._image_trim_watermarks.pop(thread_id, None)
                self._image_trim_starts.pop(thread_id, None)
            self.events.append(event)
            if self.metrics_path:
                with self.metrics_path.open("a") as stream:
                    stream.write(json.dumps(event, allow_nan=False) + "\n")
        if self.on_event:
            self.callbacks.put((self.on_event, event))
        return event

    def _callbacks(self):
        while (work := self.callbacks.get()) is not None:
            try:
                work[0](work[1])
            except Exception:
                pass  # Callback failure must not repeat or block a model request.

    @staticmethod
    def _shutdown(connection):
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def _cancel(self, state, reason):
        with self.lock:
            if state["cancel"].is_set() or state["finished"].is_set():
                return
            if reason == "client_disconnected" and state.get("delivered"):
                # The whole response already reached the client; a helper closing its
                # connection before the call is marked finished is not a transport failure.
                return
            state["cancel_reason"] = reason
            state["cancel"].set()
            if reason == "client_disconnected":
                state["provider_explicit_error"] = False
                state["failure_evidence"] = dict(classification="downstream_disconnected",
                                                response_completion="unknown")
                self._block_after_failure(state, "ClientDisconnected")
            _kill(state.get("process"))
            if state.get("reading_client_body"):
                self._shutdown(state["client"])
        self._record("model_call_cancelled", **state["identity"], reason=reason)

    def cancel_pending(self, reason="episode_stopped"):
        with self.lock:
            active = list(self.active.values())
        for state in active:
            self._cancel(state, reason)

    def register_thread(self, thread_id, source, request_id):
        """Attribute an official native thread; only the harness registers owners."""
        if any(not isinstance(value, str) or not value or len(value) > limit or '\n' in value or '\r' in value
               for value, limit in ((thread_id, 256), (source, 128), (request_id, 128))):
            raise ValueError("model_thread_registration_invalid")
        owner = dict(source=source, caller_request_id=request_id)
        with self.lock:
            if thread_id in self.cancelled_threads:
                raise ValueError("model_thread_already_cancelled")
            if thread_id in self.thread_owners and self.thread_owners[thread_id] != owner:
                raise ValueError("model_thread_owner_conflict")
            self.thread_owners[thread_id] = owner
        self._record("model_thread_registered", native_thread_id=thread_id, **owner)

    def unregister_thread(self, thread_id):
        # Cancellation tombstones outlive attribution: turn_start may dispatch
        # its first HTTP request only after the caller has already cancelled.
        with self.lock:
            owner = self.thread_owners.pop(thread_id, None)
        if owner is not None:
            self._record("model_thread_unregistered", native_thread_id=thread_id, **owner)

    def cancel_thread(self, thread_id, reason="helper_turn_interrupted"):
        """Cancel this thread only and reject its future HTTP admissions."""
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("model_thread_cancellation_invalid")
        with self.lock:
            self.cancelled_threads.setdefault(thread_id, reason)
            for state in list(self.active.values()):
                if state["identity"].get("native_thread_id") == thread_id:
                    self._cancel(state, reason)
        self._record("model_thread_cancelled", native_thread_id=thread_id, reason=reason)

    @property
    def transport_blocked(self):
        with self.lock:
            return self.blocked_error is not None

    @property
    def pending_requests(self):
        with self.lock:
            return bool(self.active)

    @property
    def last_activity_at(self):
        """Latest actual request/transport activity on the monotonic clock, or None."""
        with self.lock:
            return self._last_activity_at

    @property
    def model_wait_seconds(self):
        """Union of intervals with any pending logical request, including now."""
        with self.lock:
            return self._wait_total_s + (time.monotonic() - self._wait_started_at
                                         if self._wait_started_at is not None else 0.0)

    @property
    def transport_recovery_summary(self):
        with self.lock:
            return dict(policy=dict(self.transport_recovery) if self.transport_recovery else None,
                        rounds=self._recovery_rounds, recovered_calls=self._recovered_calls,
                        denials=self._recovery_denials, cooldown_s=self._recovery_delay_s)

    def _recovery_delay(self, state, rounds, explicit_retryable):
        """Next cooldown before resending the same body, or None to keep the terminal path.

        A resend never reaches the actor or the robot; the gate refuses it unless no
        action is executing or unconfirmed and the episode still accepts actions.
        """
        policy = self.transport_recovery
        evidence = state.get("failure_evidence") or {}
        # Provider credit exhaustion was transient in practice; it waits like a transport failure but
        # never uses the immediate retries.
        if policy is None or not (explicit_retryable or evidence.get("classification") == "quota_exceeded" or (
                policy["transport_incomplete"] and evidence.get("classification") == "transport_incomplete")):
            return None
        delay = policy["initial_delay_s"]
        for _ in range(rounds):
            delay = min(policy["max_delay_s"], delay * policy["backoff_multiplier"])
        delay = min(delay, policy["max_delay_s"])
        reason = "recovery_rounds_exhausted" if rounds >= policy["max_rounds"] else self._recovery_gate_reason()
        if reason is None:
            with self.lock:
                if self._recovery_delay_s + delay > policy["max_episode_delay_s"]:
                    reason = "episode_recovery_budget_exhausted"
                else:
                    self._recovery_delay_s += delay
                    self._recovery_rounds += 1
        if reason is not None:
            self._deny_recovery(state, reason, rounds)
            return None
        return delay

    def _recovery_gate_reason(self):
        with self.lock:
            if self.closed:
                return "proxy_closed"
            if self.blocked_error is not None:
                return "transport_blocked"
        if self.recovery_gate is None:
            return "recovery_gate_unavailable"
        try:
            reason = self.recovery_gate()
        except Exception as error:
            return "recovery_gate_error:" + type(error).__name__
        return reason if reason is None or isinstance(reason, str) else "recovery_gate_invalid"

    def _deny_recovery(self, state, reason, rounds):
        with self.lock:
            self._recovery_denials += 1
        self._record("model_transport_recovery_denied", **state["identity"], reason=reason,
                     recovery_rounds=rounds, error=state.get("failure_evidence"))

    def _block_after_failure(self, state, error_type):
        if state["cancel"].is_set() and state.get("cancel_reason") != "client_disconnected":
            return
        error = dict(logical_call_id=state["identity"]["logical_call_id"], error_type=error_type,
                     manual_retry_allowed=bool(state.get("provider_explicit_error")),
                     evidence=state.get("failure_evidence"))
        with self.lock:
            if self.blocked_error is not None:
                return
            self.blocked_error = error
        self._record("model_transport_blocked", **state["identity"], error=error,
                     automatic_retries=self.max_retries, manual_retry_allowed=error["manual_retry_allowed"])

    def manual_retry(self):
        """Explicit operator action only; unknown transport outcomes stay blocked."""
        with self.lock:
            if self.active:
                raise ValueError("manual_retry_requires_no_active_model_calls")
            if not self.blocked_error or not self.blocked_error["manual_retry_allowed"]:
                raise ValueError("manual_retry_requires_explicit_provider_error")
            previous = self.blocked_error
            self.blocked_error = None
        self._record("model_manual_retry_authorized", previous_error=previous)

    def _watch_client(self, state):
        while not state["finished"].wait(.05):
            try:
                readable, _, _ = select.select([state["client"]], [], [], 0)
                if readable and not state["client"].recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                    self._cancel(state, "client_disconnected")
                    return
            except (OSError, ValueError):
                self._cancel(state, "client_disconnected")
                return

    @staticmethod
    def _failed_response_error(response):
        """Codex Responses error mapping; never persist the provider message.

        Based on native Responses error categories. Harness policy also retries
        explicit temporary overloads; auth/configuration failures stay terminal.
        """
        error = response.get("error")
        detail = error if isinstance(error, dict) else {}
        valid = isinstance(error, dict) and all(detail.get(k) is None or isinstance(detail[k], str)
                    for k in ("type", "code", "message", "plan_type"))
        reset = detail.get("resets_at")
        valid = valid and (reset is None or type(reset) is int and -(2**63) <= reset < 2**63)
        code = detail.get("code")
        fatal = {"context_length_exceeded": "context_window_exceeded",
                 "insufficient_quota": "quota_exceeded", "credit_balance_exhausted": "quota_exceeded",
                 "organization_spend_limit_exceeded": "quota_exceeded", "project_spend_limit_exceeded": "quota_exceeded",
                 "usage_not_included": "usage_not_included", "cyber_policy": "cyber_policy",
                 "bio_policy": "bio_policy", "misalignment_policy_violation": "misalignment_policy_violation",
                 "invalid_api_key": "authentication_error", "authentication_error": "authentication_error",
                 "permission_denied": "permission_denied", "model_not_found": "configuration_error",
                 "invalid_request_error": "configuration_error", "unsupported_parameter": "configuration_error",
                 "invalid_prompt": "invalid_prompt", "image_compression_failed": "retryable",
                 "server_is_overloaded": "server_overloaded",
                 "server_overloaded": "server_overloaded", "overloaded": "server_overloaded",
                 "content_filter": "refusal", "content_policy": "refusal", "refusal": "refusal",
                 "flex_unavailable": "flex_unavailable"}
        category = ("flex_unavailable" if code == "flex_unavailable" else "stream" if not valid else
                    fatal.get(code, fatal.get(detail.get("type"),
                        "rate_limit" if code in ("rate_limit_exceeded", "slow_down") else "retryable")))
        delay = None
        if category == "rate_limit":
            match = re.search(r"(?i)try again in\s*(\d+(?:\.\d+)?)\s*(s|ms|seconds?)", detail.get("message") or "")
            if match:
                delay = float(match[1]) / (1000 if match[2].lower() == "ms" else 1)
                if not math.isfinite(delay) or delay <= 0:
                    delay = None
        def safe(value):
            return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value) else None
        return dict(code=safe(code), type=safe(detail.get("type")),
                    code_state="unknown" if code is None else "present" if safe(code) is not None else "redacted_or_invalid",
                    classification=category, retryable=category in ("retryable", "stream", "rate_limit", "server_overloaded"),
                    retry_after_s=delay)

    @staticmethod
    def _response_evidence(raw, endpoint):
        """Return authoritative response/usage; never construct output from deltas."""
        terminal = None
        if raw.lstrip().startswith((b"event:", b"data:", b":")):
            for block in raw.decode().replace("\r\n", "\n").split("\n\n"):
                data = "\n".join(line[5:].lstrip(" ") for line in block.splitlines() if line.startswith("data:"))
                if not data or data == "[DONE]":
                    continue
                event = json.loads(data)
                if event.get("type") == "error":
                    error = event.get("error", event)
                    response = dict(status="failed", output=[], error=error)
                    return True, dict(response_id=None, served_model=None, response_status="failed",
                                      usage=None, usage_known=False,
                                      response_error=ModelRequestProxy._failed_response_error(response)), response
                if event.get("type") in ("response.completed", "response.failed", "response.incomplete"):
                    terminal = event
                    break
            if terminal is None:
                raise EOFError("response terminal missing")
            response = terminal.get("response")
            complete = True  # A terminal is transport evidence, not necessarily a usable completion.
        else:
            response = json.loads(raw)
            complete = endpoint == "/responses/compact" or response.get("status") in ("completed", "incomplete", "failed")
        if not isinstance(response, dict) or (response.get("status") != "failed" and not isinstance(response.get("output"), list)):
            raise ValueError("invalid response structure")
        if terminal is not None and terminal["type"] != "response." + str(response.get("status")):
            raise ValueError("response event and status conflict")
        if endpoint != "/responses/compact":
            complete = complete and response.get("status") in ("completed", "incomplete", "failed")
        evidence = dict(response_id=response.get("id"), served_model=response.get("model"),
                        response_status=response.get("status"),
                        usage=response.get("usage"), usage_known=isinstance(response.get("usage"), dict) and bool(response["usage"]))
        if response.get("status") == "failed":
            evidence["response_error"] = ModelRequestProxy._failed_response_error(response)
        return complete, evidence, response

    def _attempt(self, state, body, headers, endpoint, retry_index):
        for key in ("failed_response", "provider_explicit_error", "failure_evidence", "connect_failure",
                    "stream_incomplete"):
            state.pop(key, None)
        with self.lock:
            if state["cancel"].is_set():
                raise self.policy.InfrastructureFailure("model call cancelled")
            self.attempt_count += 1
            number = self.attempt_count
        identity = dict(**state["identity"], request_index=number, attempt_index=retry_index + 1)
        payload = dict(url=self.upstream_url + endpoint, body=base64.b64encode(body).decode(), headers=headers)
        serialized = json.dumps(payload)
        started = time.monotonic()
        process = None
        result, phase = None, "worker_initialization"
        try:
            if subprocess.Popen is _DEFAULT_POPEN:
                process = _InProcessAttempt()
                with self.lock:
                    state["process"] = process
                    if state["cancel"].is_set():
                        _kill(process)
                self._record("model_request_started", **identity, timeout_s=None, max_retries=self.max_retries,
                             deadline_boundary=None, phase="worker_initialization")
                started = time.monotonic()
                result, phase = self._inprocess_http(process, identity, self.upstream_url + endpoint, body, headers, started)
            else:
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--http-worker"],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           text=True, start_new_session=True)
                with self.lock:
                    state["process"] = process
                    if state["cancel"].is_set():
                        _kill(process)
                self._record("model_request_started", **identity, timeout_s=None, max_retries=self.max_retries,
                             deadline_boundary=None, phase="worker_initialization")
                started = time.monotonic()
                process.stdin.write(serialized)
                try:
                    process.stdin.close()
                finally:
                    # A failed flush still closes stdin; communicate must not flush it again.
                    process.stdin = None
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if "result" in event:
                        result = event
                    elif "kind" in event:
                        phase = event.get("phase", phase)
                        kind = event.pop("kind")
                        if kind == "model_response_bytes_received":
                            # Track real upstream bytes without retaining one metric per chunk.
                            with self.lock:
                                self._last_activity_at = time.monotonic()
                            continue
                        self._record(kind, **identity, **event)
                process.wait()
            if state["cancel"].is_set():
                raise self.policy.InfrastructureFailure("model call cancelled")
            if process.returncode != 0 or result is None:
                raise self.policy.InfrastructureFailure("planner HTTP worker failed", retryable=False)
            response = result["result"]
            try:
                _, state["attempt_evidence"], _ = self._response_evidence(base64.b64decode(response.get("body", "")), endpoint)
            except (ValueError, EOFError, TypeError, AttributeError):
                pass
            raw = base64.b64decode(response["body"])
            status = response.get("status")
            if status is not None and status >= 400:
                state["provider_explicit_error"] = True
                error = Exception("provider HTTP failure")
                error.status_code = status
                try:
                    error.body = json.loads(raw)
                except ValueError:
                    error.body = None
                detail = error.body if isinstance(error.body, dict) else {}
                state["attempt_evidence"] = dict(usage=None, usage_known=False,
                    response_error=self._failed_response_error(detail), http_error_status=status)
                state["failure_evidence"] = dict(http_status=status, **state["attempt_evidence"]["response_error"])
                # The pinned classifier only reads these attributes, never raw headers/body into logs.
                from types import SimpleNamespace
                error.response = SimpleNamespace(headers={k.lower(): v for k, v in response["headers"].items()})
                failure = self.policy.classify_openai_error(error)
                if state["attempt_evidence"]["response_error"].get("code") == "image_compression_failed":
                    failure.retryable = True
                raise failure
            if response.get("error_type"):
                phase = response.get("phase", phase)
                # A failure while connecting (DNS resolution, refused or timed-out connect) happens before
                # any request byte reaches the provider, so resending cannot duplicate a generation.
                connect_failure = phase == "connecting_upstream"
                state["failure_evidence"] = dict(
                    classification="transport_connect_failed" if connect_failure else "transport_incomplete",
                    transport_error_type=response["error_type"], phase=phase)
                state["connect_failure"] = connect_failure
                self._record("model_request_transport_error", **identity, error_type=response["error_type"], phase=phase)
                raise self.policy.InfrastructureFailure("model upstream connect failed" if connect_failure
                    else "model upstream transport incomplete", retryable=connect_failure)
            # Nothing is released to the actor before a completed terminal, so a stream that ended
            # without one can be resent like a connect failure.
            def stream_incomplete(message, detail):
                state["failure_evidence"] = dict(classification="stream_incomplete", http_status=status, detail=detail)
                state["stream_incomplete"] = True
                return self.policy.InfrastructureFailure(message, retryable=True)
            try:
                complete, evidence, terminal = self._response_evidence(raw, endpoint)
            except EOFError:
                raise stream_incomplete("model response terminal missing", "terminal_missing") from None
            except (ValueError, TypeError, AttributeError):
                raise stream_incomplete("model response malformed", "malformed") from None
            state["attempt_evidence"] = evidence
            if evidence["response_status"] in ("failed", "incomplete"):
                # Neither failed nor incomplete responses may release tool deltas.
                terminal = {**terminal, "output": []}
                event_type = "response." + evidence["response_status"]
                failed_raw = (("event: " + event_type + "\ndata: " + json.dumps(dict(type=event_type, response=terminal)) + "\n\n").encode()
                              if raw.lstrip().startswith((b"event:", b"data:", b":")) else json.dumps(terminal).encode())
                state["failed_response"] = (failed_raw, response["headers"])
                error = evidence.get("response_error", dict(classification="incomplete"))
                state["failure_evidence"] = dict(http_status=status, **error)
                state["provider_explicit_error"] = evidence["response_status"] == "failed"
                raise self.policy.InfrastructureFailure("model response failed: " + error["classification"],
                    retryable=bool(error.get("retryable")), retry_after_s=error.get("retry_after_s"))
            if not complete:
                raise stream_incomplete("model response ended without completed status", "not_completed")
            return raw, response["headers"]
        finally:
            _kill(process)
            if process is not None:
                process.communicate()
            with self.lock:
                state["process"] = None
            info = result or {}
            response = info.get("result", {})
            self._record("model_request_finished", **identity, http_status=response.get("status"),
                         elapsed_s=time.monotonic()-started, first_upstream_byte_s=info.get("first_upstream_byte_s"),
                         incoming_bytes=info.get("incoming_bytes"), outgoing_bytes=0,
                         transport_complete=bool(result and not response.get("error_type")),
                         timed_out=False, phase=phase,
                         **state.pop("attempt_evidence", dict(usage=None, usage_known=False)))

    @staticmethod
    def _part_observation_labels(parts):
        labels, queue, observation = [], [], None
        for part in parts:
            if not isinstance(part, dict):
                labels.append((None, None))
                continue
            ptype = part.get("type")
            if ptype in ("input_text", "inputText", "output_text", "text"):
                value = None
                try:
                    value = json.loads(part.get("text", ""))
                except (TypeError, ValueError):
                    pass
                bundle = value.get("bundle") if isinstance(value, dict) else None
                if isinstance(bundle, dict) and isinstance(bundle.get("observation_id"), str):
                    observation = bundle["observation_id"]
                    view = value.get("image_view")
                    feedback = value.get("image_feedback")
                    if isinstance(view, dict) and view.get("camera_id"):
                        queue = [view["camera_id"]]
                    elif isinstance(feedback, dict) and isinstance(feedback.get("included_camera_ids"), list):
                        queue = list(feedback["included_camera_ids"])
                    else:
                        queue = [cam.get("camera_id") for cam in bundle.get("cameras", []) if isinstance(cam, dict)]
                elif isinstance(value, dict):
                    observation = (value.get("observation_id")
                                   or (value.get("receipt") or {}).get("result_observation_id")
                                   or (value.get("observation_ids") or [None])[0])
                    overlay = value.get("trajectory_overlay") or value.get("value")
                    cam = overlay.get("camera_id") if isinstance(overlay, dict) else None
                    queue = [cam] if cam else []
                else:
                    queue, observation = [], None
                labels.append((None, None))
            elif ptype == "input_image" and str(part.get("image_url", "")).startswith("data:image/"):
                camera = queue.pop(0) if queue else None
                labels.append((observation, camera))
            else:
                labels.append((None, None))
        return labels

    @classmethod
    def _inline_image_locations(cls, request, *, include_labels=False):
        items = request.get("input") if isinstance(request, dict) else None
        if not isinstance(items, list):
            return []
        locations = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = "content" if item.get("type", "message") == "message" else (
                "output" if item.get("type") in ("function_call_output", "custom_tool_call_output") else None)
            parts = item.get(key) if key else None
            if not isinstance(parts, list):
                continue
            labels = cls._part_observation_labels(parts) if include_labels else None
            for idx, part in enumerate(parts):
                if isinstance(part, dict) and part.get("type") == "input_image" and str(part.get("image_url", "")).startswith("data:image/"):
                    locations.append((parts, idx, labels[idx]) if include_labels else (parts, idx))
        return locations

    @classmethod
    def _step_image_boundaries(cls, request):
        items = request.get("input") if isinstance(request, dict) else None
        if not isinstance(items, list):
            return [], []
        locations, boundaries = [], []
        output_types = ("function_call_output", "custom_tool_call_output")
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            itype = item.get("type", "message")
            key = "content" if itype == "message" else ("output" if itype in output_types else None)
            parts = item.get(key) if key else None
            if isinstance(parts, list):
                for idx, part in enumerate(parts):
                    if isinstance(part, dict) and part.get("type") == "input_image" and str(part.get("image_url", "")).startswith("data:image/"):
                        locations.append((parts, idx))
            next_type = items[i + 1].get("type") if i + 1 < len(items) and isinstance(items[i + 1], dict) else None
            if (itype not in output_types or next_type not in output_types) and locations and (not boundaries or boundaries[-1] != len(locations)):
                boundaries.append(len(locations))
        return locations, boundaries

    @classmethod
    def _trim_image_range(cls, request, start_idx, end_idx):
        if end_idx <= start_idx or end_idx <= 0:
            return request, 0
        from copy import deepcopy
        cloned = deepcopy(request)
        locations = cls._inline_image_locations(cloned, include_labels=True)
        start = max(0, min(start_idx, len(locations)))
        end = max(start, min(end_idx, len(locations)))
        for parts, idx, (obs_id, cam) in locations[start:end]:
            if obs_id and cam:
                text = (f"[Earlier historical observation image omitted (observation_id={obs_id}, camera={cam}) "
                        f"to stay within upstream gateway payload limit; call history_view_observation(observation_id='{obs_id}') "
                        "if you need to inspect this frame.]")
            elif obs_id:
                text = (f"[Earlier historical observation image omitted (observation_id={obs_id}) "
                        f"to stay within upstream gateway payload limit; call history_view_observation(observation_id='{obs_id}') "
                        "if you need to inspect this frame.]")
            else:
                text = "[Earlier historical observation image omitted to stay within upstream gateway payload limit.]"
            parts[idx] = dict(type="input_text", text=text)
        return cloned, end - start

    @classmethod
    def _trim_prefix_images(cls, request, prefix_count):
        return cls._trim_image_range(request, 0, prefix_count)

    @classmethod
    def _trim_oldest_images(cls, body, keep_latest=24):
        try:
            request = json.loads(body)
        except ValueError:
            return body
        total = len(cls._inline_image_locations(request))
        if total <= keep_latest:
            return body
        trimmed, _ = cls._trim_prefix_images(request, total - keep_latest)
        return json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()

    def _prepare_responses_body(self, state, base_request, base_body, *, force_advance=False, retry_index=0):
        identity = state["identity"]
        thread_key = identity.get("native_thread_id") or ""
        locations, step_boundaries = self._step_image_boundaries(base_request)
        total_images = len(locations)
        with self.lock:
            watermark, last_total = self._image_trim_watermarks.get(thread_key, (0, 0))
            start_idx = self._image_trim_starts.get(thread_key, 0)
            if total_images < last_total or watermark > total_images or start_idx > watermark:
                start_idx, watermark = 0, 0
            previous = (start_idx, watermark)
            if force_advance and total_images > 0:
                keep = max(8, 16 - retry_index * 4)
                watermark = min(total_images, max(watermark + 4, total_images - keep))

        def render(s_idx, wm):
            working = base_request
            if wm > s_idx:
                working, _ = self._trim_image_range(working, s_idx, wm)
            image_stats = None
            if self.image_reuse:
                from ...core.presentation.image_reuse import reuse_exact_images
                rewritten, image_stats = reuse_exact_images(working)
                if image_stats["replaced"]:
                    working = rewritten
            if wm <= s_idx and not (image_stats and image_stats["replaced"]):
                encoded = base_body
            else:
                encoded = json.dumps(working, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            return working, encoded, image_stats

        working, encoded, image_stats = render(start_idx, watermark)
        limit = self.max_inline_images
        def image_count(request):
            return len(self._inline_image_locations(request))
        # The provider rejects requests by inline image count, independent of bytes.
        bytes_over = len(encoded) > 4_500_000
        count_over = bool(limit) and image_count(working) > limit
        reason = "body_bytes_and_image_count" if bytes_over and count_over else "image_count" if count_over else "body_bytes"
        if self.image_trim and (bytes_over or count_over) and total_images > 8:
            target_bytes = 2_400_000
            target_images = limit * 2 // 3
            tail_candidates = [b for b in step_boundaries if b <= total_images - 12]
            new_wm = max(watermark, tail_candidates[-1] if tail_candidates else max(0, total_images - 12))
            if watermark == 0 and new_wm > 0:
                need_bytes = len(encoded) - target_bytes if bytes_over else 0
                need_images = image_count(working) - target_images if count_over else 0
                saved, removed, raw_start = 0, 0, new_wm
                for idx in range(new_wm - 1, -1, -1):
                    parts, part_idx = locations[idx]
                    saved += len(parts[part_idx].get("image_url", ""))
                    removed += 1
                    raw_start = idx
                    if saved >= need_bytes and removed >= need_images:
                        break
                prefix_candidates = [b for b in step_boundaries if 0 < b <= raw_start and b < new_wm]
                start_idx = prefix_candidates[-1] if prefix_candidates else 0
            watermark = new_wm
            working, encoded, image_stats = render(start_idx, watermark)
            while ((bytes_over and len(encoded) > target_bytes)
                   or (count_over and image_count(working) > target_images)):
                if start_idx > 0:
                    lower = [b for b in step_boundaries if 0 < b < start_idx]
                    start_idx = lower[-1] if lower else 0
                elif total_images - watermark > 8:
                    watermark = min(total_images - 8, max(watermark + 4, total_images - 8))
                else:
                    break
                working, encoded, image_stats = render(start_idx, watermark)
        with self.lock:
            self._image_trim_watermarks[thread_key] = (watermark, total_images)
            self._image_trim_starts[thread_key] = start_idx
        if (start_idx, watermark) != previous:
            self._record("model_images_trimmed", **identity,
                         reason="provider_rejected" if force_advance else reason,
                         images_total=total_images, images_forwarded=image_count(working),
                         window_start=start_idx, window_end=watermark,
                         previous_window=list(previous), original_body_bytes=len(base_body),
                         forwarded_body_bytes=len(encoded))
        if not force_advance and image_stats and image_stats["replaced"]:
            self._record("model_images_reused", **identity, **image_stats,
                         original_body_bytes=len(base_body), forwarded_body_bytes=len(encoded),
                         forwarded_body_sha256=hashlib.sha256(encoded).hexdigest())
        return working, encoded

    def _complete(self, state, body, headers, endpoint):
        if (self.image_trim and endpoint.rstrip("/").endswith("/responses") and "base_request" not in state
                and len(body) > 4_500_000):
            body = self._trim_oldest_images(body, 16)
        retry_index = rounds = 0
        while True:
            try:
                result = self._attempt(state, body, headers, endpoint, retry_index)
                if rounds:
                    with self.lock:
                        self._recovered_calls += 1
                    self._record("model_transport_recovered", **state["identity"], recovery_rounds=rounds,
                                 attempt_index=retry_index + 1)
                return result
            except self.policy.InfrastructureFailure as failure:
                if state["cancel"].is_set():
                    raise
                image_rejected = (state.get("failure_evidence") or {}).get("code") == "image_compression_failed"
                # Without trimming, a retry would resend the body the provider just rejected.
                explicit_retryable = bool((state.get("provider_explicit_error") or state.get("connect_failure")
                                           or state.get("stream_incomplete"))
                                          and failure.retryable
                                          and not (image_rejected and not self.image_trim))
                recovering = not (explicit_retryable and retry_index < self.max_retries)
                delay = self._recovery_delay(state, rounds, explicit_retryable) if recovering else None
                if recovering and delay is None:
                    return self._terminal_failure(state, failure)
                if image_rejected:
                    if "base_request" in state:
                        _, body = self._prepare_responses_body(
                            state, state["base_request"], state["base_body"],
                            force_advance=True, retry_index=retry_index,
                        )
                    else:
                        body = self._trim_oldest_images(body, max(8, 16 - retry_index * 4))
                if recovering:
                    rounds += 1
                    self._record("model_transport_recovery_scheduled", **state["identity"], recovery_round=rounds,
                                 attempt_index=retry_index + 1, delay_s=delay, http_status=failure.status,
                                 episode_cooldown_s=self.transport_recovery_summary["cooldown_s"],
                                 error=state.get("failure_evidence"))
                else:
                    delay = self.policy._retry_delay_seconds(retry_index, failure)
                    self._record("model_request_retry", **state["identity"], attempt_index=retry_index + 1,
                                 delay_s=delay, http_status=failure.status,
                                 error=state.get("failure_evidence"))
                if state["cancel"].wait(delay):
                    raise self.policy.InfrastructureFailure("model call cancelled") from None
                if recovering and (reason := self._recovery_gate_reason()) is not None:
                    # Actions may have started during the cooldown; keep the original failure.
                    self._deny_recovery(state, reason, rounds)
                    return self._terminal_failure(state, failure)
                retry_index += 1

    def _terminal_failure(self, state, failure):
        if "failed_response" in state:
            self._block_after_failure(state, "ModelResponseFailed")
            state["delivering_failed_response"] = True
            return state["failed_response"]
        raise failure

    def start(self):
        if self.server is not None or self.closed:
            raise ValueError("model_proxy_already_started_or_closed")
        self.callback_thread = threading.Thread(target=self._callbacks, daemon=True)
        self.callback_thread.start()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_): pass

            def do_POST(self):
                if self.path not in ("/responses", "/responses/compact"):
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    self.send_error(400)
                    return
                if not 0 <= length <= 128 * 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                    self.send_error(400, "Content-Length required within model input bound")
                    return
                with owner.lock:
                    native_thread_id = self.headers.get("thread-id", "")
                    if native_thread_id in owner.cancelled_threads:
                        owner._record("model_thread_request_rejected", native_thread_id=native_thread_id,
                                      reason=owner.cancelled_threads[native_thread_id])
                        self.send_error(409, "model thread explicitly cancelled")
                        return
                    if owner.blocked_error is not None:
                        # Blocks SDK reconnect, automatic compaction recovery and
                        # any new logical request until an operator authorizes it.
                        self.send_error(409, "model transport blocked; manual review required")
                        return
                    exhausted = owner.request_count >= owner.max_requests
                    denied = owner.closed or owner.request_count + owner.reserved_calls >= owner.max_requests
                    if not denied:
                        owner.reserved_calls += 1
                        owner.logical_call_count += 1
                        number = owner.logical_call_count
                        attribution = owner.thread_owners.get(native_thread_id, dict(
                            source=self.headers.get("X-Auriga-Source", "actor")[:128],
                            caller_request_id=self.headers.get("X-Auriga-Request-Id", "")[:128]))
                        identity = dict(logical_call_index=number, logical_call_id=f"{owner.transport_id}:{number}",
                                        endpoint=self.path, native_thread_id=native_thread_id, **attribution)
                        state = dict(identity=identity, client=self.connection, process=None,
                                     cancel=threading.Event(), finished=threading.Event(), cancel_reason=None,
                                     reading_client_body=True)
                        if not owner.active:
                            owner._wait_started_at = time.monotonic()
                        owner.active[number] = state
                    notify_limit = exhausted and not owner.limit_notified
                    if notify_limit:
                        owner.limit_notified = True
                if denied:
                    event = owner._record("model_budget_exhausted" if exhausted else "model_budget_reserved",
                                          completed_calls=owner.request_count, reserved_calls=owner.reserved_calls,
                                          max_requests=owner.max_requests)
                    self.send_error(429, "episode model completion budget unavailable")
                    if notify_limit and owner.on_limit:
                        owner.callbacks.put((owner.on_limit, event))
                    return
                started, streaming, delivered, charged = time.monotonic(), False, False, False
                io_lock = threading.Lock()
                heartbeat_stop = threading.Event()
                def heartbeat():
                    while not heartbeat_stop.wait(1):
                        try:
                            with io_lock:
                                # Codex times parsed SSE events, so comments do
                                # not keep its idle watchdog alive. Unknown
                                # event types are ignored without model output.
                                self.wfile.write(b'event: auriga.keepalive\ndata: {"type":"auriga.keepalive"}\n\n')
                                self.wfile.flush()
                        except OSError:
                            owner._cancel(state, "client_disconnected")
                            return
                try:
                    owner._record("model_call_started", **identity, phase="reading_client_body")
                    self.connection.settimeout(None)
                    body = self.rfile.read(length)
                    with owner.lock:
                        state["reading_client_body"] = False
                    if len(body) != length:
                        raise ConnectionError("incomplete client request")
                    owner._record("model_request_body_received", **identity, body_bytes=len(body),
                                  elapsed_s=time.monotonic()-started, body_sha256=hashlib.sha256(body).hexdigest())
                    request = json.loads(body)
                    if owner.request_context is not None:
                        # Snapshot once per logical request; retries reuse the filtered body.
                        request = owner.request_context(request, dict(identity))
                    if owner.request_projection is not None:
                        request = owner.request_projection(request)
                    if owner.request_context is not None or owner.request_projection is not None:
                        body = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
                    if self.path.rstrip("/").endswith("/responses"):
                        state["base_request"] = request
                        state["base_body"] = body
                        request, body = owner._prepare_responses_body(state, request, body)
                    identity["requested_model"] = request.get("model")
                    streaming = request.get("stream") is True
                    self.connection.settimeout(None)
                    if streaming:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        threading.Thread(target=heartbeat, daemon=True).start()
                    threading.Thread(target=owner._watch_client, args=(state,), daemon=True).start()
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS | PRIVATE_HEADERS}
                    headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
                    if owner._upstream_auth_headers is not None:
                        headers = {k: v for k, v in headers.items() if k.lower() not in CLIENT_AUTH_HEADERS}
                        headers.update(owner._upstream_auth_headers)
                    headers["Accept-Encoding"] = "identity"
                    raw, response_headers = owner._complete(state, body, headers, self.path)
                    with owner.lock:
                        if state["cancel"].is_set() or owner.closed:
                            raise owner.policy.InfrastructureFailure("model call cancelled before delivery")
                    heartbeat_stop.set()
                    with io_lock:
                        if not streaming:
                            self.send_response(200)
                            for key, value in response_headers.items():
                                if key.lower() not in HOP_HEADERS:
                                    self.send_header(key, value)
                            self.send_header("Content-Length", str(len(raw)))
                            self.send_header("Connection", "close")
                            self.end_headers()
                        self.wfile.write(raw)
                        self.wfile.flush()
                        delivered = True
                        state["delivered"] = True
                    if state.get("delivering_failed_response"):
                        owner._block_after_failure(state, "ModelResponseFailed")
                        owner._record("model_call_failed", **identity, error_type="ModelResponseFailed",
                                      cancelled=False, completed_call_charged=False, outgoing_bytes=len(raw),
                                      manual_retry_required=True, error=state.get("failure_evidence"))
                    else:
                        with owner.lock:
                            # Keep the reservation until delivery succeeds; usage is
                            # already recorded even when a downstream write fails.
                            owner.request_count += 1
                            owner.reserved_calls -= 1
                            charged = True
                        owner._record("model_call_completed", **identity, completed_calls=owner.request_count,
                                      outgoing_bytes=len(raw))
                except (Exception, owner.policy.InfrastructureFailure) as error:
                    heartbeat_stop.set()
                    owner._block_after_failure(state, type(error).__name__)
                    owner._record("model_call_failed", **identity, error_type=type(error).__name__,
                                  cancelled=state["cancel"].is_set(), completed_call_charged=charged,
                                  manual_retry_required=not state["cancel"].is_set() or state["cancel_reason"] == "client_disconnected",
                                  error=state.get("failure_evidence"))
                    try:
                        with io_lock:
                            if streaming:
                                details = dict(type="upstream_error", code="model_request_failed",
                                    message="Model request failed; provider-error retry policy exhausted or not applicable.",
                                    evidence=state.get("failure_evidence"),
                                    manual_retry_allowed=bool(state.get("provider_explicit_error")))
                                self.wfile.write(("event: error\ndata: " + json.dumps(dict(type="error", error=details)) + "\n\n").encode())
                                self.wfile.flush()
                            else:
                                data = json.dumps(dict(error=dict(code="model_request_failed",
                                    message="Model request failed; provider-error retry policy exhausted or not applicable.",
                                    evidence=state.get("failure_evidence"),
                                    manual_retry_allowed=bool(state.get("provider_explicit_error"))))).encode()
                                self.send_response((state.get("failure_evidence") or {}).get("http_status") or 502)
                                self.send_header("Content-Type", "application/json")
                                self.send_header("Content-Length", str(len(data)))
                                self.send_header("Connection", "close")
                                self.end_headers()
                                self.wfile.write(data)
                                self.wfile.flush()
                    except OSError:
                        pass
                finally:
                    heartbeat_stop.set()
                    state["finished"].set()
                    self.close_connection = True
                    with owner.lock:
                        if not charged:
                            owner.reserved_calls -= 1
                        owner.active.pop(number, None)
                        if not owner.active and owner._wait_started_at is not None:
                            owner._wait_total_s += time.monotonic() - owner._wait_started_at
                            owner._wait_started_at = None
                    owner._record("model_call_finished", **identity, completed_calls=owner.request_count,
                                  attempt_count=owner.attempt_count, delivered=delivered,
                                  elapsed_s=time.monotonic()-started, cancelled=state["cancel"].is_set(),
                                  failed_response_delivered=bool(delivered and state.get("delivering_failed_response")))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()
        return self

    def close(self):
        with self.lock:
            self.closed = True
        self.cancel_pending("proxy_closed")
        with self.lock:
            active = list(self.active.values())
        for state in active:
            self._shutdown(state["client"])
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(1)
        for state in active:
            state["finished"].wait(1)
        self._close_conn_pool()
        self.callbacks.put(None)
        if self.callback_thread and self.callback_thread is not threading.current_thread():
            self.callback_thread.join(1)

    def __enter__(self): return self.start()
    def __exit__(self, *_): self.close()


if __name__ == "__main__":
    _http_worker()
