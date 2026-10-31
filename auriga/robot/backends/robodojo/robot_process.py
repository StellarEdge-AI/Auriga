"""Own one RoboDojo JSONL worker, cancellation channel, and exit evidence.

This module has no action compiler, task configuration, or Isaac imports.
"""
import json
import os
from pathlib import Path
import re
import select
import subprocess
import threading
import time
import uuid

from .worker_protocol import WIRE_PREFIX

GPU_OOM_FAILURE = "robot_gpu_out_of_memory: native GPU allocation failed; robot status unknown; do not replay action"
NATIVE_GPU_OOM_SIGNATURES = ("ERROR_OUT_OF_DEVICE_MEMORY", "Out of GPU memory allocating resource",
                             "CUDA error: out of memory", "CUDA out of memory", "CUDA_ERROR_OUT_OF_MEMORY",
                             "torch.OutOfMemoryError:", "torch.cuda.OutOfMemoryError:")
# Worker replies prefix the exception type; only the type, never quoted message text, is evidence.
GPU_OOM_REPLY = re.compile(r"^(?:backend_error: )?(?:torch\.)?(?:cuda\.)?OutOfMemoryError: ")


def native_gpu_oom_signature(text):
    for signature in NATIVE_GPU_OOM_SIGNATURES:
        if signature in text:
            return signature
    if "PxgCudaDeviceMemoryAllocator failed to allocate memory" in text and "Result = 2" in text:
        return "PxgCudaDeviceMemoryAllocator allocation failed; Result = 2"
    return None


def gpu_oom_reply(message):
    """A planner/torch OOM is caught by the worker and returned as an error or receipt reason."""
    reply = message.get("result")
    reason = reply.get("reason") if isinstance(reply, dict) else None
    return any(isinstance(text, str) and GPU_OOM_REPLY.match(text) for text in (message.get("error"), reason))


class RobotProcess:
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir).resolve()
        self.process = None
        self.pending = {}
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.operation_lock = threading.Lock()
        self.failure = None
        self.capabilities = None
        self.reader = None
        self.log_start = 0
        self._process_generation = 0
        self.last_activity_at = time.monotonic()

    def start_process(self, command, *, cwd, environment):
        if self.process is not None:
            return self.capabilities
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log = (self.run_dir / "robot-worker.log").open("a", buffering=1)
        self.log_start = self.log.tell()
        self.startup_forced_termination = None
        try:
            self.process = subprocess.Popen(command, cwd=cwd, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, bufsize=1)
        except Exception:
            self.log.close()
            raise
        self._process_generation += 1
        self.close_report = None
        # Capture identity while this Popen still owns the live child. Never
        # infer its start time later from a potentially reused PID during cleanup.
        try:
            fields = Path(f"/proc/{self.process.pid}/stat").read_text().rsplit(")", 1)[1].split()
            self.process_start_ticks = int(fields[19])
        except (OSError, ValueError, IndexError):
            self.process_start_ticks = None
        self.reader = threading.Thread(target=self._read,
            args=(self.process, self.log, self._process_generation), daemon=True)
        self.reader.start()
        self.capabilities = self._request("ready", timeout=600)
        return self.capabilities

    def _read(self, process=None, log=None, generation=None):
        process = self.process if process is None else process
        log = self.log if log is None else log
        generation = self._process_generation if generation is None else generation
        def owns_generation():
            return self.process is process and self._process_generation == generation
        progress_seen = {}
        for line in process.stdout:
            if not owns_generation():
                return
            if not line.startswith(WIRE_PREFIX):
                log.write(line)
                continue
            try:
                message = json.loads(line[len(WIRE_PREFIX):])
            except ValueError:
                log.write(line)
                continue
            with self.lock:
                entry = self.pending.get(message.get("id"))
            if entry is None:
                continue
            if "progress" in message:
                progress = message["progress"]
                if isinstance(progress, dict):
                    # Only worker execution milestones count, never log/heartbeat traffic.
                    milestone = {key: progress[key] for key in (
                        "phase", "waypoint_index", "executed_steps", "sim_step", "arm_context")
                        if key in progress}
                    if milestone and milestone != progress_seen.get(message.get("id")):
                        progress_seen[message.get("id")] = milestone
                        self.last_activity_at = time.monotonic()
                if entry[2]:
                    try:
                        entry[2](message["progress"])
                    except Exception as error:
                        log.write(f"progress callback failed: {error}\n")
            else:
                self.last_activity_at = time.monotonic()
                progress_seen.pop(message.get("id"), None)
                entry[1].update(message)
                entry[0].set()
        if owns_generation():
            self._check_native_failure()
            self._fail(f"robot worker disconnected (exit={process.poll()}); inspect {self.run_dir / 'robot-worker.log'}")

    def _fail(self, reason):
        with self.lock:
            self.failure = self.failure or reason
            for event, result, _ in self.pending.values():
                if not event.is_set():
                    result.setdefault("error", self.failure)
                    event.set()

    def _check_native_failure(self):
        # Keep native stderr file-backed: Kit descendants can inherit it beyond
        # worker exit. A pipe reader would need separate shutdown machinery.
        try:
            with (self.run_dir / "robot-worker.log").open("rb") as stream:
                end = stream.seek(0, os.SEEK_END)
                stream.seek(max(self.log_start, end - 1024 * 1024))
                lines = stream.read(1024 * 1024).decode("utf-8", errors="replace").splitlines()
        except OSError:
            return  # Keep the IPC error/deadline if diagnostics are unavailable.
        for line in lines:
            if native_gpu_oom_signature(line):
                # Never publish simulator log text or asset paths.
                self._fail(GPU_OOM_FAILURE)
                return

    def _send(self, message, deadline):
        data = memoryview((json.dumps(message, allow_nan=False) + "\n").encode())
        acquired = False
        try:
            acquired = self.write_lock.acquire(timeout=max(0, deadline - time.monotonic()))
            if not acquired:
                raise TimeoutError("robot status unknown: worker request write deadline; do not replay action")
            descriptor = self.process.stdin.fileno()
            os.set_blocking(descriptor, False)
            while data:
                try:
                    data = data[os.write(descriptor, data):]
                except BlockingIOError:
                    pass
                if data:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([], [descriptor], [], remaining)[1]:
                        raise TimeoutError("robot status unknown: worker request write deadline; do not replay action")
        except (TimeoutError, OSError) as error:
            # A partial JSON frame cannot safely accept another request or cancellation.
            self._fail(str(error))
            raise
        finally:
            if acquired:
                self.write_lock.release()

    def _request(self, operation, payload=None, timeout=180, progress=None, cancel=None):
        deadline = time.monotonic() + timeout
        request_id = str(uuid.uuid4())
        done, result = threading.Event(), {}
        with self.lock:
            if self.failure:
                raise RuntimeError(self.failure)
            self.pending[request_id] = (done, result, progress)
        try:
            self._send({"id": request_id, "op": operation, "payload": payload}, deadline)
            cancellation_sent = False
            next_log_check = time.monotonic() + 1
            while not done.wait(min(0.1, max(0, deadline - time.monotonic()))):
                if time.monotonic() >= next_log_check:
                    self._check_native_failure()
                    next_log_check = time.monotonic() + 1
                if cancel is not None and cancel.is_set() and not cancellation_sent:
                    self._send({"id": str(uuid.uuid4()), "op": "cancel", "target": request_id}, deadline)
                    cancellation_sent = True
                if time.monotonic() >= deadline:
                    if not cancellation_sent:
                        try:
                            self._send({"id": str(uuid.uuid4()), "op": "cancel", "target": request_id}, deadline)
                        except (TimeoutError, OSError):
                            pass
                    raise TimeoutError("robot status unknown: worker response deadline; do not replay action")
            self._check_native_failure()
            if gpu_oom_reply(result):
                self._fail(GPU_OOM_FAILURE)
            if "error" in result or (self.failure or "").startswith("robot_gpu_out_of_memory"):
                raise RuntimeError(self.failure or result["error"])
            return result["result"]
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def close(self):
        if self.process is None:
            return {"native_close_acknowledged": False, "forced_termination": None,
                    "process_exit_confirmed": True, "reader_exit_confirmed": True, "not_started": True, "errors": []}
        previous = getattr(self, "close_report", None)
        if (previous and previous.get("pid") == self.process.pid and previous["process_exit_confirmed"]
                and previous.get("reader_exit_confirmed") is True):
            return previous
        report = self.close_report = {"pid": self.process.pid,
            "start_ticks": getattr(self, "process_start_ticks", None), "native_close_acknowledged": False,
            "environment_close_completed": False, "application_close_started": False,
            "application_close_completed": False, "application_fast_shutdown": None,
            "forced_termination": getattr(self, "startup_forced_termination", None),
            "process_exit_confirmed": False, "reader_exit_confirmed": False, "errors": []}

        def close_progress(value):
            if value.get("phase") == "environment_closed":
                report["environment_close_completed"] = True
            elif value.get("phase") == "application_close_started":
                report["application_close_started"] = True
                report["application_fast_shutdown"] = value.get("fast_shutdown")
            elif value.get("phase") == "application_closed":
                report["application_close_completed"] = True

        try:
            if self.process.poll() is None:
                if self.failure:
                    raise RuntimeError(self.failure)
                result = self._request("close", timeout=180, progress=close_progress)
                report["native_close_acknowledged"] = result.get("closed") is True
                if not report["native_close_acknowledged"]:
                    raise RuntimeError("native_close_acknowledgement_missing")
                report.update(environment_close_completed=True, application_close_started=True,
                              application_close_completed=True)
            self.process.wait(timeout=30)
        except (RuntimeError, TimeoutError, subprocess.TimeoutExpired, OSError) as error:
            report["errors"].append(f"{type(error).__name__}: {error}")
            # A dead IPC link cannot confirm robot status. Termination only owns this worker.
            try:
                # EOF can precede waitpid visibility during a normal native exit.
                # Give that exit a short grace; do not label it a forced kill just
                # because the final IPC acknowledgement never arrived.
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    if self.process.poll() is None:
                        self.process.terminate()
                        report["forced_termination"] = "SIGTERM"
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        if self.process.poll() is None:
                            self.process.kill()
                            report["forced_termination"] = "SIGKILL"
                        self.process.wait(timeout=10)
                    except (subprocess.TimeoutExpired, OSError) as error:
                        report["errors"].append(f"{type(error).__name__}: {error}")
                except OSError as error:
                    report["errors"].append(f"{type(error).__name__}: {error}")
            except OSError as error:
                report["errors"].append(f"{type(error).__name__}: {error}")
        finally:
            report["process_exit_confirmed"] = self.process.poll() is not None
            report["returncode"] = self.process.returncode
            report["exit_mode"] = ("unconfirmed" if not report["process_exit_confirmed"] else
                "after_forced_signal" if report["forced_termination"] else
                "after_native_close_ack" if report["native_close_acknowledged"] else "self_exit_without_close_ack")
            # Closing pipes with an unconfirmed writer can itself block. Retain
            # them for the supervisor's forced-exit path in that exceptional case.
            if report["process_exit_confirmed"]:
                if self.reader is not None:
                    self.reader.join(timeout=5)
                report["reader_exit_confirmed"] = self.reader is None or not self.reader.is_alive()
                if report["reader_exit_confirmed"]:
                    for stream in (self.process.stdin, self.process.stdout):
                        try: stream.close()
                        except OSError as error: report["errors"].append(f"{type(error).__name__}: {error}")
                    try: self.log.close()
                    except OSError as error: report["errors"].append(f"{type(error).__name__}: {error}")
                else:
                    report["errors"].append("robot_reader_stop_unconfirmed")
        return report
