"""Pinned RoboProbe API actor with a disclosed Auriga execution boundary."""

import base64
from copy import deepcopy
from functools import lru_cache
import hashlib
import http.client
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import tomllib
import types
import urllib.error
import urllib.request

import cv2
import numpy as np

from ...core.contracts import canonical, new_id
from ...core.config import Config
from ...core.observation_policy import actor_robot_state, require_actor_observation
from ...robot.backends.robodojo.robodojo import FRAME_ID, TCP_CONFIG_ID, validate_action

ROOT = Path(__file__).resolve().parents[3]
REFERENCE_ROOT = ROOT if (ROOT / "third_party/roboprobe/api-provenance.json").is_file() else Path(__file__).parents[2] / "reference_sources"
UPSTREAM_COMMIT = "9ffacc54f372beef6479d61711bf4945e31ae4ce"
MAX_MODEL_CALLS = 170
MODEL_REQUEST_TIMEOUT_S = None
PUBLIC_EEF_DOCS = """The ARX X5 tool uses a gripping-face center 0.1501 m along link6's local +x axis.
Positions use metres in env_origin. The adapter converts grasp-center targets to link6 poses.
pitch_deg, roll_deg and yaw_deg are extrinsic rotations about world +x, +y and +z, in degrees
from the top-down reference: all zero points the tool's local +x along world -z.
Gripper 0 commands closed and 1 commands open. The reported gripper value is the previous
command, not measured finger position, contact or retained-grasp evidence.
Only one arm can move per request. Unnamed pose dimensions hold their observed values.
The optional gripper command follows arm motion. The backend plans free paths; it does not
guarantee straight Cartesian motion or avoidance of task objects. Infer object geometry from RGB."""


@lru_cache(maxsize=1)
def upstream():
    """Import unchanged vendored modules under their original package names."""
    provenance = json.loads((REFERENCE_ROOT / "third_party/roboprobe/api-provenance.json").read_text())
    source = REFERENCE_ROOT / provenance["vendored_root"]
    if provenance["commit"] != UPSTREAM_COMMIT:
        raise ValueError("reference_source_commit_mismatch")
    for name, info in provenance["files"].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != info["sha256"]:
            raise ValueError("reference_source_digest_mismatch:" + name)
    if "XPolicyLab" in sys.modules:
        raise RuntimeError("reference_requires_its_own_process_namespace")
    package = types.ModuleType("XPolicyLab")
    package.__path__ = [str(source)]
    sys.modules["XPolicyLab"] = package
    prefix = "XPolicyLab.policy.RoboDojo_Agent_L3_Inspect"
    return types.SimpleNamespace(
        joint=importlib.import_module(prefix + ".policy"),
        eef=importlib.import_module(prefix + "_EEF.policy"),
        docs=importlib.import_module(prefix + "_EEF.docs"),
        responses=importlib.import_module("XPolicyLab.utils.openai_responses"),
        provenance=provenance,
    )


class ResponsesClient:
    """No request deadline; bounded retries only for explicit provider errors."""

    def __init__(self, base_url, token, directory, *, model="gpt-6.1-sol", effort="high",
                 timeout_s=MODEL_REQUEST_TIMEOUT_S, max_retries=None):
        if timeout_s is not None:
            raise ValueError("reference_model_request_deadlines_disabled")
        self.base_url, self._token = base_url.rstrip("/"), token
        self.model, self.effort, self.timeout_s = model, effort, timeout_s
        self.runtime_provider = dict(base_url=self.base_url, model=model, effort=effort,
                                     service_tier="default")
        self.directory = Path(directory).resolve()
        self.current_observation_id = None
        self.calls = []
        self.attempt_count = 0
        from ...cortex.session.model_proxy import DEFAULT_MAX_RETRIES
        self._max_retries = DEFAULT_MAX_RETRIES if max_retries is None else max_retries
        if type(self._max_retries) is not int or not 0 <= self._max_retries <= DEFAULT_MAX_RETRIES:
            raise ValueError("reference_invalid_retry_budget")
        self.blocked_error = None
        self._cancel = threading.Event()
        self._process_lock = threading.Lock()
        self._active_process = None

    @property
    def max_retries(self):
        return self._max_retries

    def cancel_pending(self):
        with self._process_lock:
            self._cancel.set()
            if self._active_process is not None and self._active_process.poll() is None:
                try:
                    os.killpg(self._active_process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def manual_retry(self):
        if self._active_process is not None or not self.blocked_error or not self.blocked_error["manual_retry_allowed"]:
            raise ValueError("reference_manual_retry_requires_explicit_provider_error")
        self.blocked_error = None

    @classmethod
    def from_codex_config(cls, directory, *, config=None, codex_home=None, **kwargs):
        """Use the resolved experiment model and only its selected provider/home."""
        resolved = config if config is not None else Config.load()
        home = Path(codex_home if codex_home is not None else resolved.model.codex_home).expanduser().resolve()
        document = tomllib.loads((home / "config.toml").read_text())
        provider_name = document.get("model_provider")
        if not isinstance(provider_name, str) or not provider_name.strip():
            raise ValueError("reference_selected_provider_unavailable")
        providers = document.get("model_providers", {})
        if provider_name != "openai" and provider_name not in providers:
            raise ValueError("reference_selected_provider_unavailable:" + str(provider_name))
        provider = providers.get(provider_name, {})
        if provider.get("wire_api", "responses") != "responses":
            raise ValueError("reference_requires_responses_provider")
        base_url = provider.get("base_url")
        if not base_url and provider_name != "openai":
            raise ValueError("reference_selected_provider_base_url_unavailable:" + str(provider_name))
        key_name = provider.get("env_key")
        token = (os.environ.get(key_name) if key_name else None) or provider.get("experimental_bearer_token")
        if not token:
            auth = json.loads((home / "auth.json").read_text()) if (home / "auth.json").exists() else {}
            token = auth.get("OPENAI_API_KEY")
        if not token:
            raise ValueError("reference_provider_credential_unavailable")
        kwargs.setdefault("model", resolved.model.name)
        kwargs.setdefault("effort", resolved.model.effort)
        client = cls(base_url or "https://api.openai.com/v1", token, directory, **kwargs)
        client.runtime_provider = dict(provider=provider_name, codex_home=str(home),
                                       base_url=base_url or "https://api.openai.com/v1",
                                       model=kwargs["model"], effort=kwargs["effort"], service_tier="default")
        return client

    @staticmethod
    def _stream_response(raw):
        """Keep terminal evidence and complete items; never assemble action deltas."""
        terminal, terminal_event, items = None, None, {}
        for block in raw.decode().replace("\r\n", "\n").split("\n\n"):
            content = "\n".join(line[5:].lstrip(" ") for line in block.splitlines() if line.startswith("data:"))
            if not content or content == "[DONE]":
                continue
            event = json.loads(content)
            kind = event.get("type")
            if kind == "error":
                terminal = dict(status="failed", output=[], error=event.get("error", event))
                terminal_event = "response.failed"
                break
            if kind == "response.output_item.done":
                index, item = event.get("output_index"), event.get("item")
                if type(index) is not int or index < 0 or not isinstance(item, dict):
                    raise ValueError("reference stream completed item is malformed")
                if index in items and items[index] != item:
                    raise ValueError("reference stream completed items conflict")
                items[index] = item
            elif kind in ("response.completed", "response.failed", "response.incomplete"):
                terminal, terminal_event = event["response"], kind
                break
        if terminal is None:
            raise EOFError("reference streaming response lacks authoritative terminal event")
        if not isinstance(terminal, dict) or terminal_event != "response." + str(terminal.get("status")):
            raise ValueError("reference terminal event and response status conflict")
        result = deepcopy(terminal)
        source = "terminal_response.output"
        if not result.get("output") and items:
            if sorted(items) != list(range(len(items))):
                raise ValueError("reference stream completed output indices are incomplete")
            result["output"] = [items[index] for index in sorted(items)]
            source = "response.output_item.done"
        return terminal, result, dict(terminal_event=terminal_event, output_source=source,
                                     completed_output_item_ids=[item.get("id") for item in items.values()])

    def complete(self, messages, tools):
        if self.blocked_error is not None:
            raise upstream().joint.InfrastructureFailure("reference transport blocked; manual review required")
        self._cancel.clear()
        helpers = upstream().responses
        request = dict(model=self.model, input=helpers.chat_messages_to_responses_input(messages),
                       tools=helpers.chat_tools_to_responses_tools(tools), tool_choice="required",
                       parallel_tool_calls=False, reasoning={"effort": self.effort}, service_tier="default",
                       store=False, stream=True)
        body = canonical(request).encode()
        index = len(self.calls) + 1
        directory = self.directory / f"call-{index:03}"
        directory.mkdir(parents=True)
        (directory / "request.json").write_bytes(body)
        images = []
        for message in request["input"]:
            for part in message.get("content", []) if isinstance(message.get("content"), list) else []:
                if part.get("type") == "input_image":
                    uri = part["image_url"]
                    data = base64.b64decode(uri.split(",", 1)[1])
                    images.append({"sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data), "mime_type": uri[5:].split(";", 1)[0]})
        metadata = dict(call_index=index, requested_model=self.model, reasoning_effort=self.effort, service_tier="default",
                        input_observation_id=self.current_observation_id,
                        request_sha256=hashlib.sha256(body).hexdigest(), request_path=str(directory / "request.json"),
                        model_images=images, attempts=[])
        self.calls.append(metadata)
        self._current_call, self._request_body = metadata, body
        started = time.monotonic()
        try:
            for retry_index in range(self.max_retries + 1):
                try:
                    result = self._isolated_complete(messages, tools)
                    break
                except upstream().joint.InfrastructureFailure as failure:
                    if (self._cancel.is_set() or not failure.retryable or retry_index >= self.max_retries
                            or not metadata["attempts"][-1].get("provider_explicit_error")):
                        raise
                    delay = upstream().joint._retry_delay_seconds(retry_index, failure)
                    metadata["attempts"][-1]["retry_delay_s"] = delay
                    if self._cancel.wait(delay):
                        raise upstream().joint.InfrastructureFailure("reference model call cancelled") from None
            metadata["status"] = "received"
            return result
        except (Exception, upstream().joint.InfrastructureFailure):
            metadata["status"] = "failed"
            if not self._cancel.is_set():
                self.blocked_error = dict(call_index=index,
                    manual_retry_allowed=bool(metadata["attempts"] and metadata["attempts"][-1].get("provider_explicit_error")))
            raise
        finally:
            metadata.update(latency_s=time.monotonic() - started, http_attempt_count=len(metadata["attempts"]))
            (directory / "delivery.json").write_text(canonical(metadata))

    def _isolated_complete(self, messages, tools):
        """Wait for one terminal response or explicit cancellation; never run tools."""
        call = self._current_call
        self.attempt_count += 1
        attempt = dict(attempt_index=len(call["attempts"]) + 1, episode_attempt_index=self.attempt_count,
                       request_sha256=call["request_sha256"], input_observation_id=self.current_observation_id,
                       usage=None, usage_known=False)
        call["attempts"].append(attempt)
        directory = Path(call["request_path"]).parent / f"attempt-{attempt['attempt_index']:03}"
        directory.mkdir()
        attempt["directory"] = str(directory)
        payload = dict(base_url=self.base_url, body=self._request_body.decode(), directory=str(directory),
                       model=self.model)
        worker_env = os.environ.copy()
        worker_env["AURIGA_REFERENCE_WORKER_KEY"] = self._token
        serialized = json.dumps(payload)
        started = time.monotonic()
        process = None
        try:
            process = subprocess.Popen([sys.executable, "-m", "auriga.apps.reference.reference_api", "--http-worker"],
                                       cwd=ROOT, env=worker_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
            with self._process_lock:
                self._active_process = process
                if self._cancel.is_set():
                    os.killpg(process.pid, signal.SIGKILL)
            started = time.monotonic()
            stdout, _stderr = process.communicate(serialized)
            if process.returncode != 0:
                attempt["status"] = "worker_failed"
                raise upstream().joint.InfrastructureFailure(
                    f"planner HTTP worker exited with status {process.returncode}", retryable=False)
            try:
                envelope = json.loads(stdout)
                attempt.update(envelope["metadata"])
                failure = envelope.get("failure")
                if failure:
                    if failure["kind"] == "capability":
                        raise upstream().joint.CapabilityFailure(failure["message"])
                    raise upstream().joint.InfrastructureFailure(
                        failure["message"], retryable=failure["retryable"], retry_after_s=failure["retry_after_s"],
                        status=failure["status"], key_unusable=failure["key_unusable"])
                result = envelope["response"]
            except (KeyError, TypeError, ValueError):
                attempt["status"] = "worker_response_unreadable"
                raise upstream().joint.InfrastructureFailure("planner HTTP worker returned an unreadable response", retryable=False) from None
            call.update({key: value for key, value in attempt.items() if key not in
                         {"attempt_index", "episode_attempt_index", "directory", "latency_s"}})
            return result
        except upstream().joint.InfrastructureFailure as failure:
            attempt.update(retryable=failure.retryable, http_status=failure.status or attempt.get("http_status"))
            raise
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.communicate()
            with self._process_lock:
                self._active_process = None
            attempt["latency_s"] = time.monotonic() - started
            (directory / "delivery.json").write_text(canonical(attempt))
            (directory.parent / "delivery.json").write_text(canonical(call))

    @staticmethod
    def _transport_complete(payload, metadata):
        """Single worker attempt. No SDK retries and no policy/tool execution."""
        directory = Path(payload["directory"])
        body = payload["body"].encode()
        joint = upstream().joint
        def record(kind, **fields):
            with (directory / "transport.jsonl").open("a") as stream:
                stream.write(canonical(dict(kind=kind, wall_time=time.time(), **fields)) + "\n")
        try:
            wire = urllib.request.Request(payload["base_url"] + "/responses", data=body, method="POST",
                                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["AURIGA_REFERENCE_WORKER_KEY"]})
            record("model_request_started", phase="opening_response", body_bytes=len(body))
            from ...cortex.session.model_proxy import _terminal_sse_block
            import re
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None
            with urllib.request.build_opener(NoRedirect).open(wire, timeout=None) as response:
                metadata["http_status"] = response.status
                record("model_response_headers", http_status=response.status)
                with (directory / "response.sse").open("wb") as output:
                    pending, terminal_seen = b"", False
                    while chunk := response.read1(65536):
                        output.write(chunk)
                        output.flush()
                        pending += chunk
                        while match := re.search(br"\r?\n\r?\n", pending):
                            block, pending = pending[:match.start()], pending[match.end():]
                            if _terminal_sse_block(block):
                                terminal_seen = True
                                break
                        if terminal_seen:
                            break
                if not terminal_seen and response.length not in (None, 0):
                    raise http.client.IncompleteRead(b"", response.length)
            raw = (directory / "response.sse").read_bytes()
            if raw.lstrip().startswith((b"event:", b"data:", b":")):
                terminal, result, evidence = ResponsesClient._stream_response(raw)
                metadata.update(evidence)
            else:
                raise ValueError("reference provider did not return a terminal SSE stream")
        except urllib.error.HTTPError as error:
            metadata.update(http_status=error.code, status="provider_error", provider_explicit_error=True)
            # Reuse the pinned classification, including Retry-After and gateway -4003.
            # The provider body is used only for classification, never persisted or echoed.
            try:
                error.body = json.loads(error.read())
            except (OSError, http.client.HTTPException, ValueError):
                error.body = None
            error.status_code = error.code
            error.response = types.SimpleNamespace(headers=error.headers)
            failure = joint.classify_openai_error(error)
            error.close()
            raise failure from None
        except (OSError, http.client.HTTPException, EOFError):
            metadata["status"] = "provider_transport_incomplete"
            raise joint.InfrastructureFailure("reference provider transport ended before a complete response", retryable=False) from None
        except ValueError:
            metadata["status"] = "provider_transport_or_json_error"
            raise joint.InfrastructureFailure("reference provider returned malformed response") from None
        (directory / "response.json").write_text(canonical(terminal))
        (directory / "assembled-response.json").write_text(canonical(result))
        metadata.update(served_model=result.get("model"), response_path=str(directory / "response.sse"),
                        terminal_response_path=str(directory / "response.json"),
                        assembled_response_path=str(directory / "assembled-response.json"),
                        response_sha256=hashlib.sha256(raw).hexdigest(), usage=result.get("usage"),
                        usage_known=isinstance(result.get("usage"), dict) and bool(result["usage"]),
                        response_status=result.get("status"))
        if result.get("model") and result["model"] != payload["model"]:
            metadata["status"] = "provider_model_mismatch"
            (directory / "delivery.json").write_text(canonical(metadata))
            raise upstream().joint.InfrastructureFailure("reference provider returned a different model; no fallback")
        if result.get("status") != "completed":
            metadata["status"] = "provider_response_not_completed"
            metadata["provider_explicit_error"] = result.get("status") == "failed"
            (directory / "delivery.json").write_text(canonical(metadata))
            from ...cortex.session.model_proxy import ModelRequestProxy
            error = ModelRequestProxy._failed_response_error(result) if metadata["provider_explicit_error"] else {}
            metadata["response_error"] = error
            raise upstream().joint.InfrastructureFailure("reference provider response not completed",
                retryable=bool(error.get("retryable")), retry_after_s=error.get("retry_after_s"))
        metadata["status"] = "received"
        (directory / "delivery.json").write_text(canonical(metadata))
        converted = upstream().responses.responses_to_chat_completion(result)
        converted["model"] = result.get("model")
        return converted


def _http_worker():
    metadata = {}
    joint = upstream().joint
    try:
        response = ResponsesClient._transport_complete(json.load(sys.stdin), metadata)
        envelope = dict(response=response)
    except joint.CapabilityFailure as failure:
        envelope = dict(failure=dict(kind="capability", message=str(failure)))
    except joint.InfrastructureFailure as failure:
        envelope = dict(failure=dict(kind="infrastructure", message=str(failure), retryable=failure.retryable,
                                    retry_after_s=failure.retry_after_s, status=failure.status,
                                    key_unusable=failure.key_unusable))
    envelope["metadata"] = metadata
    json.dump(envelope, sys.stdout)


def action_spec(capabilities, raw):
    """Use the official deploy's live embodiment limits and declared step sizes."""
    labels, low, high, max_step = [], [], [], []
    control_hz = float(capabilities["native_control_hz"])
    if control_hz != 25:
        raise ValueError("reference_requires_official_25_hz_control")
    for arm in ("left", "right"):
        names = capabilities["arm_joint_names"][arm]
        limits = np.asarray(capabilities["arm_joint_limits_rad"][arm], dtype=float)
        steps = np.asarray(capabilities["arm_max_step_rad"][arm], dtype=float)
        if len(raw["robot_state"]["arms"][arm]["q_rad"]) != 6:
            raise ValueError("reference_requires_six_joint_arx_x5_arm")
        if list(names) != [f"joint{index}" for index in range(1, 7)] or limits.shape != (6, 2) or steps.shape != (6,):
            raise ValueError("reference_invalid_live_action_spec")
        if not np.isfinite(limits).all() or not np.isfinite(steps).all() or (limits[:, 1] <= limits[:, 0]).any() or (steps <= 0).any():
            raise ValueError("reference_invalid_live_action_limits")
        for name, (lower, upper), step in zip(names, limits, steps):
            labels.append(arm + "_" + name)
            low.append(float(lower))
            high.append(float(upper))
            max_step.append(float(step))
        labels.append(arm + "_gripper")
        low.append(0.0); high.append(1.0); max_step.append(float(capabilities["gripper_max_step"]))
    return upstream().joint.RoboDojoActionSpec(labels=tuple(labels), low=np.asarray(low), high=np.asarray(high),
                control_hz=control_hz, max_step=tuple(max_step), docs=PUBLIC_EEF_DOCS)


def make_policy(capabilities, initial, client, *, max_calls=MAX_MODEL_CALLS, action_timeout_s=180):
    original = upstream()

    class BackendPolicy(original.eef.EefAgentPolicy):
        def _give_up_chunk(self, reason, observation):
            chunk = super()._give_up_chunk(reason, observation)
            chunk.actions[0].meta["stop_source"] = "framework"
            if reason == "LLM call budget exhausted":
                chunk.actions[0].meta["stop_reason"] = "llm_call_budget_exhausted"
            return chunk

        def _system_message(self):
            return (
                "Control the RoboDojo ARX X5 using the native task instruction, RGB and robot proprioception. "
                "Choose exactly one tool call per turn and inspect the next observation after motion. "
                "Task score and hidden scene state are unavailable; done records only your judgment. "
                f"Commands execute through the native environment at {self.action_spec.control_hz:g} Hz, "
                "using the live embodiment's declared joint and gripper step limits.\n\n" + PUBLIC_EEF_DOCS
            )

        def prepare(self, observation):
            # Do not enter upstream recipe, task-specific goal, ICL or prior-attempt injection paths.
            if not self._goal_sent:
                self._goal_text = "Goal: " + observation.instruction
                self._messages.append({"role": "user", "content": self._goal_text})
                self._goal_sent = True

        def _axis_bounds(self, label):
            axis = label.partition("_")[2]
            if axis in original.eef.pose.POSITION_AXES:
                # Generic backend link6 envelope, allowing the public tool offset in any direction.
                extent = 2 - original.eef.pose.GRASP_POINT_OFFSET_M
                return -extent, extent
            return (0, 1) if axis == "gripper" else original.eef.pose.ANGLE_BOUNDS[axis]

        def _build_tools(self, spec):
            tools = super()._build_tools(spec)
            tools[0]["function"]["description"] = (
                "Command absolute grasp-center dimensions for one arm; omitted dimensions hold their observed values. "
                "Positions are metres; angles are top-down-relative degrees as described in the system message. "
                "Gripper 0=closed, 1=open, applied after arm motion. No grasp success is implied. "
                f"The whole action has a {action_timeout_s:g}s wall timeout. "
                f"Generic backend envelope and control bounds: {self._bounds_text()}. "
                "A target within this envelope may still be unreachable."
            )
            return tools

        def _handle_motion(self, name, arguments, observation):
            if not isinstance(arguments, dict):
                return self._repair("move_eef arguments must be an object with targets and note.")
            by_arm, error = self._parse_targets(arguments.get("targets"))
            if error:
                return self._repair(error)
            if len(by_arm) != 1:
                return self._repair("Auriga supports one arm per move_eef; split this request into separate calls. Neither arm moved.")
            arm, overrides = next(iter(by_arm.items()))
            axes = {key: value for key, value in overrides.items() if key != "gripper"}
            waypoints = []
            if axes:
                values = self._measured_values(observation, arm)
                if values is None:
                    raise original.joint.InfrastructureFailure("reference measured EE pose unavailable")
                target = original.eef.pose.values_to_pose({**values, **axes})
                self._last_targets = {arm: target}
                waypoints = [{arm: dict(position_m=target[:3].tolist(), quaternion_xyzw=np.r_[target[4:], target[3]].tolist())}]
            action = dict(action_id=new_id("reference-action"), mode="absolute", frame_id=FRAME_ID, tcp_config_id=TCP_CONFIG_ID,
                          limits_profile="robodojo-native-v1", timeout_s=action_timeout_s,
                          path=dict(kind="backend_free" if waypoints else "hold", waypoints=waypoints,
                                    orientation="backend_free", position_tolerance_m=0.005, blend_radius_m=0.0),
                          gripper_events=[dict(arm=arm, phase="after_motion", goal_normalized=overrides["gripper"])] if "gripper" in overrides else [])
            validate_action(action)
            chunk = original.eef.ActionChunk(actions=[original.eef.Action(data=observation.state)],
                        control_hz=self.action_spec.control_hz, meta={"auriga_action": action})
            return original.joint.MotionOutcome(chunk=chunk, tool_result="EE request accepted for backend execution; completion and fresh RGB follow in the next observation.")

        def feedback(self, receipt):
            feedback = {key: receipt[key] for key in ("status", "executed_steps", "executed_control_steps") if key in receipt}
            feedback["reason"] = str(receipt["reason"]).partition(":")[0] if receipt.get("reason") else None
            feedback["waypoint_results"] = [{key: item[key] for key in ("waypoint_index", "position_error_m", "orientation_error_rad", "sim_step") if key in item}
                                            for item in receipt.get("waypoint_results", [])]
            for field, allowed in (("stop_confirmation", ("clock", "actuator_target", "settling_steps")),
                                   ("execution_interval", ("start_sim_step", "end_sim_step", "step_domain"))):
                if field in receipt:
                    feedback[field] = {key: receipt[field][key] for key in allowed if key in receipt[field]}
            text = canonical(feedback)
            self._messages[-1]["content"] = "Actual backend receipt: " + text
            self._transcript[-1].update(tool_result=self._messages[-1]["content"], execution_status=receipt["status"])

    env = {"L3_INSPECT_PLANNER": "astra", "L3_INSPECT_MODEL": client.model,
           "L3_INSPECT_BASE_URL": client.base_url, "L3_INSPECT_API_STYLE": "responses",
           "L3_INSPECT_REASONING_EFFORT": client.effort, "L3_INSPECT_MAX_LLM_CALLS": str(max_calls),
           # The vendored policy parses numeric settings, but uses our injected
           # client for completions. Zero disables its hard boundary; its socket
           # default is never used by ResponsesClient's timeout=None transport.
           "L3_INSPECT_HARD_TIMEOUT_S": "0", "L3_INSPECT_TIMEOUT_S": "60",
           "L3_INSPECT_MAX_RETRIES": str(client.max_retries),
           "L3_INSPECT_KEEP_ALL_IMAGES": "1", "L3_INSPECT_DEPTH": "off", "L3_INSPECT_ALLOW_DONE": "1",
           "L3_INSPECT_USE_RECIPE": "0"}
    return BackendPolicy(planner=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("reference direct planner is disabled")),
                         action_spec=action_spec(capabilities, initial), env=env, client=client)


def observation(raw, policy_step):
    original = upstream()
    require_actor_observation(raw)
    instruction = raw.get("instruction")
    if raw.get("observation_profile") != "rgb_proprio_v1" or not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("reference_requires_native_rgb_proprio_instruction")
    robot_state, state = actor_robot_state(raw["robot_state"]), {}
    for arm in ("left", "right"):
        own = robot_state["arms"][arm]
        measured = own["ee_pose"]
        state[arm + "_arm_joint_state"] = np.asarray(own["q_rad"])
        state[arm + "_ee_joint_state"] = np.asarray([own["gripper"]["command_normalized"]])
        state[arm + "_ee_pose"] = np.asarray(measured["position_m"] + measured["quaternion_xyzw"][3:] + measured["quaternion_xyzw"][:3])
    cameras = raw["cameras"]
    if len(cameras) != 3 or {camera["camera_id"] for camera in cameras} != {"cam_head", "cam_left_wrist", "cam_right_wrist"}:
        raise ValueError("reference_requires_native_rgb_cameras")
    from ...core.storage.images import camera_path
    images = {camera["camera_id"]: cv2.cvtColor(cv2.imread(str(camera_path(camera))), cv2.COLOR_BGR2RGB) for camera in cameras}
    return original.eef.Observation(images=images, state=state, instruction=instruction,
                    step=policy_step, remaining_steps=None, extra={})


def run_episode(backend, client, directory, *, max_calls=MAX_MODEL_CALLS, action_timeout_s=180, recorder=None):
    """One persistent upstream API conversation; evaluator never enters that conversation."""
    from ...cortex.session.model_proxy import RETRY_POLICY
    if type(max_calls) is not int or not 1 <= max_calls <= MAX_MODEL_CALLS:
        raise ValueError("reference_model_call_limit_must_be_1_to_170")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    report = dict(schema_version="0.1", reference_id="roboprobe-eef-api-native-runtime-v4", upstream_commit=UPSTREAM_COMMIT,
                  full_upstream_protocol_reproduction=False, benchmark_result=False, model=client.model, effort=client.effort,
                  runtime_provider=deepcopy(client.runtime_provider),
                  actor_input_policy=dict(observation_profile="rgb_proprio_v1", cameras="original RGB re-encoded by upstream JPEG quality=95",
                                          proprioception="native own-arm joints and link6 pose; previous gripper command, not contact feedback",
                                          instruction="native public task instruction only",
                                          prompt="generic EE interface; no upstream embodiment notes, task recipes, demonstrations or prior attempts",
                                          camera_calibration=False, robot_urdf_model=False, hidden_object_states=False,
                                          observer_score=False),
                  budgets=dict(max_llm_calls=max_calls, model_call_counting="complete() returns an authoritative completed model terminal; failed/incomplete responses are excluded",
                               model_request_timeout_s=client.timeout_s, max_model_retries=client.max_retries,
                               retry_policy=RETRY_POLICY,
                               model_timeout_boundary=None,
                               episode_termination="native EvalEnv terminal state or model stop/call limit",
                               action_timeout_s=action_timeout_s),
                  adaptations=["Streaming Responses HTTP inside an isolated worker; no request deadline; bounded automatic retries only after explicit retryable provider errors; one configured credential; authoritative completed terminal SSE event required; complete output_item.done items supply output only if terminal output is omitted; original terminal and raw stream retained; no AIDP sticky headers or encrypted reasoning replay",
                               "Upstream act/repair/history and pose conversion reused; system/goal/motion description and position envelope replaced with generic public-interface semantics; task recipes, demonstrations and prior-attempt injection bypassed",
                               "Single-arm EEF requests become official 25 Hz EvalEnv.take_action joint commands using live action-spec limits",
                               "Native task step limit and success terminate execution privately; no total wall-time or high-level-action-count cap; remaining budgets are not actor inputs",
                               "done enabled as actor judgment only; final native evaluator remains observer-only"],
                  observations=[], decisions=[], errors=[], api_calls=client.calls)
    manifest = {key: deepcopy(report[key]) for key in ("schema_version", "reference_id", "upstream_commit", "full_upstream_protocol_reproduction",
                "benchmark_result", "model", "effort", "runtime_provider", "actor_input_policy", "budgets", "adaptations")}
    manifest.update(task=backend.task, seed=backend.seed, layout_id=backend.layout_id,
                    data_origin="pending_native_start", actor_runtime="pinned RoboProbe simple API policy; no Codex tool registry",
                    api_provenance_sha256=hashlib.sha256((REFERENCE_ROOT / "third_party/roboprobe/api-provenance.json").read_bytes()).hexdigest())
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False))
    policy = None
    initial_step, raw, executed = None, None, 0
    started = time.monotonic()
    try:
        report["capabilities"] = backend.start()
        if recorder:
            recorder.start(report["capabilities"])
        report["data_origin"] = manifest["data_origin"] = report["capabilities"].get("data_origin", "unknown")
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False))
        initial = backend.observe()
        if initial["observation_profile"] != "rgb_proprio_v1":
            raise ValueError("reference_requires_rgb_proprio_v1")
        if recorder:
            recorder.observe(initial)
        backend.record(enabled=True)
        policy = make_policy(report["capabilities"], initial, client, max_calls=max_calls, action_timeout_s=action_timeout_s)
        initial_step, raw, executed = initial["sim_step"], initial, 0
        while True:
            if policy.calls >= max_calls:
                report["termination_reason"] = "llm_call_budget_exhausted"; break
            report["observations"].append(deepcopy(raw))
            current = observation(raw, len(report["decisions"]))
            client.current_observation_id = raw["raw_observation_id"]
            if recorder:
                recorder.progress.update(phase="model", llm_calls=policy.calls, action_count=executed)
            chunk = policy.act(current)
            decision = dict(policy_step=current.step, input_observation_id=raw["raw_observation_id"], calls=policy.calls)
            report["decisions"].append(decision)
            if chunk.actions[0].meta.get("request_stop"):
                kind = "framework_stop" if chunk.actions[0].meta.get("stop_source") == "framework" else "actor_judgment"
                decision[kind] = dict(chunk.actions[0].meta)
                report["termination_reason"] = chunk.actions[0].meta["stop_reason"]; break
            action = chunk.meta["auriga_action"]
            decision["action"] = action
            executed += 1
            if recorder:
                receipt = recorder.execute(backend, action)
            else:
                receipt = backend.execute(action)
            decision["receipt"] = receipt
            raw = receipt.get("terminal_observation")
            if raw is None or receipt.get("status") in ("unknown", "status_unknown"):
                report["termination_reason"] = "backend_status_or_observation_unknown"; break
            decision["result_observation_id"] = raw["raw_observation_id"]
            terminal = receipt.get("episode_terminal")
            if terminal is None:
                raise ValueError("reference_backend_missing_native_episode_terminal")
            report["episode_terminal"] = deepcopy(terminal)
            if terminal["ended"]:
                report["termination_reason"] = terminal["reason"]
                break
            policy.feedback(receipt)
            (directory / "reference-episode.json").write_text(json.dumps(report, indent=2, allow_nan=False))
        report["action_count"] = executed
        report["http_attempt_count"] = client.attempt_count
        if raw is not None:
            report["final_observation"] = raw
            report["executed_adapter_control_steps"] = raw["sim_step"] - initial_step
    except (Exception, upstream().joint.InfrastructureFailure) as error:
        report["termination_reason"] = "infrastructure_or_policy_failure"
        report["errors"].append(type(error).__name__ + ": " + str(error))
    finally:
        report["action_count"] = executed
        report["http_attempt_count"] = client.attempt_count
        report["executed_native_steps"] = sum(item.get("receipt", {}).get("executed_native_steps", 0) for item in report["decisions"])
        if raw is not None:
            report["final_observation"] = raw
            if initial_step is not None:
                report["executed_adapter_control_steps"] = raw["sim_step"] - initial_step
        if policy is not None:
            report.update(llm_calls=policy.calls, transcript=policy.transcript(), hindsight=policy.hindsight)
            try:
                report["policy_audit"] = policy.audit_config()
            except Exception as error:
                report["errors"].append("policy_audit: " + type(error).__name__ + ": " + str(error))
        for name, operation in (("evaluation", backend.evaluate), ("videos", backend.save_video)):
            try:
                report[name] = operation()
            except Exception as error:
                report["errors"].append(name + ": " + type(error).__name__ + ": " + str(error))
        report["elapsed_wall_time_s"] = time.monotonic() - started
        result_ids = {decision.get("result_observation_id") for decision in report["decisions"]} - {None}
        report["feedback_model_calls"] = sum(call.get("status") == "received" and call.get("input_observation_id") in result_ids
                                             for call in report["api_calls"])
        videos = report.get("videos", {}).get("videos", [])
        report["video_files_verified"] = bool(videos and all(Path(video.get("video_path", "")).is_file() and
                    Path(video["video_path"]).stat().st_size > 0 and video.get("frames", 0) > 0 for video in videos))
        report["rgb_delivery_verified"] = bool(report["api_calls"] and all(len(call.get("model_images", [])) >= 3 for call in report["api_calls"]))
        report["episode_verified"] = bool(report["api_calls"] and report["decisions"] and
                    any(decision.get("receipt", {}).get("terminal_observation") for decision in report["decisions"]) and
                    (report["feedback_model_calls"] > 0 or report.get("episode_terminal", {}).get("ended")) and
                    "evaluation" in report and report["video_files_verified"] and
                    report["rgb_delivery_verified"] and not report["errors"] and
                    report["termination_reason"] != "backend_status_or_observation_unknown")
        report["loop_verified"] = report["episode_verified"] and report["feedback_model_calls"] > 0
        (directory / "reference-episode.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    return report


if __name__ == "__main__" and sys.argv[1:] == ["--http-worker"]:
    _http_worker()
