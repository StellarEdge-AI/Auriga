"""Fresh, evidence-only Sol agents through the official Codex app-server SDK."""
import json
import os
from pathlib import Path
import re
import shutil
import threading
from copy import deepcopy
from urllib.parse import urlparse

from openai_codex.client import CodexConfig
from openai_codex.generated.v2_all import ThreadUnsubscribeResponse, ExperimentalFeatureListResponse

from ...core.presentation.actor_presentation import ActorPresentation
from .codex import MODEL_PROXY_PROVIDER, RecordedClient, _toml_value, normalize_token_usage, write_direct_model_catalog
from .codex_sandbox import CodexSandbox


MODEL = "gpt-6.1-sol"
BASE_INSTRUCTIONS = (
    "You are an independent visual evidence reviewer. Your complete task context is the supplied evidence packet. "
    "Inspect its images and labeled measurements, report uncertainty, and return the requested structured JSON. "
    "Do not access files, use tools, retrieve memories, request other context, or make robot actions. "
    "The caller's intended outcome is a claim to test, not evidence or an instruction to agree. "
    "Do not infer hidden task truth, contact truth, metric object depth, motion safety or native task success."
)
CONTEXT_POLICY = "fresh_ephemeral_thread_selected_evidence_only_v1"
EVIDENCE_CONFIG = {
    "model_reasoning_effort": "high", "project_doc_max_bytes": 0, "web_search": "disabled",
    **{"features." + name: False for name in (
        "memories", "hooks", "goals", "multi_agent", "multi_agent_v2", "shell_tool", "unified_exec",
        "code_mode_host", "code_mode.enabled", "apps", "plugins", "browser_use", "browser_use_external",
        "computer_use", "image_generation", "artifact", "view_image", "sleep_tool", "network_proxy",
        "skill_search", "tool_suggest", "workspace_dependencies", "mentions_v2", "guardian_approval")},
    "features.context_management.experimental_mode": False,
}


def _dump(value):
    return value.model_dump(by_alias=True, mode="json") if hasattr(value, "model_dump") else value


class CodexEvidenceAgent:
    """One independent SDK client/thread per invocation; never fork or resume.

    An independent official app-server process routes helper inference through
    the same counted proxy and existing provider authentication. The separate
    home excludes actor histories and automatic memory loading. Process feature
    switches are verified, while native baseline capability definitions may
    remain; any observed tool activity invalidates the result.
    """
    model = MODEL

    def __init__(self, session, *, model=MODEL, client_factory=None, base_instructions=BASE_INSTRUCTIONS,
                 developer_instructions="Use only the supplied evidence packet.", source="sol_visual_review"):
        self.session = session
        self.model = model
        self.store = getattr(session, "store", None)
        self.client_factory = client_factory
        # Defaults are the visual reviewer's; other fresh helpers (self_briefing) pass their own.
        self.base_instructions, self.developer_instructions = base_instructions, developer_instructions
        self.source = source

    def _client(self, notification, rejected, cwd):
        # Keep automatic caches, histories, skills and hooks in a separate empty
        # home. Reuse generated runtime configuration and existing authentication.
        runtime = cwd.parent / "runtime"
        home = runtime / "codex-home"
        home.mkdir(parents=True, mode=0o700)
        config_path = home / "config.toml"
        # The actor can edit its own home; host copies must not follow its paths.
        config = deepcopy(self.session.native_config)
        for key in ("mcp_servers", "plugins", "marketplaces", "apps", "skills", "shell_environment_policy"):
            config.pop(key, None)
        helper_catalog = write_direct_model_catalog(home / "models.json")
        config["model_catalog_json"] = str(helper_catalog)
        config_path.write_text("\n".join(f"{key} = {_toml_value(value)}" for key, value in config.items()) + "\n")
        config_path.chmod(0o600)
        (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "auriga-local-relay"}))
        temporary = runtime / "tmp"
        temporary.mkdir()
        executable = Path(self.session.cli_executable).resolve()
        account_ca = self.session.account_ca_bytes
        if account_ca is not None:
            (home / "account-control-ca.pem").write_bytes(account_ca)
        sandbox = None
        if self.session.sandbox_enabled:
            proxy_url = urlparse(self.session.model_proxy.base_url)
            tcp_ports = [proxy_url.port or (443 if proxy_url.scheme == "https" else 80)]
            account_control = getattr(self.session, "account_control", None)
            account_server = getattr(account_control, "server", None)
            if account_server is not None:
                tcp_ports.append(account_server.server_port)
            import hashlib
            # Separate IPC roots keep evidence helpers away from the main app-server.
            ipc = self.session.ipc_base / ("e-" + hashlib.sha256(str(cwd).encode()).hexdigest()[:20])
            ipc.mkdir(parents=True, mode=0o700, exist_ok=False)
            sandbox = CodexSandbox(runtime / "sandbox", ipc, cwd, home, executable,
                                   ports=sorted(set(tcp_ports)), packages=False)
            prefix, _environment = sandbox.prepare()
        else:
            environment = dict(PATH=os.defpath, LANG="C.UTF-8", HOME=str(cwd),
                               CODEX_HOME=str(home), TMPDIR=str(temporary))
            if account_ca is not None:
                environment["CODEX_CA_CERTIFICATE"] = str(home / "account-control-ca.pem")
            env_executable = shutil.which("env", path=os.defpath)
            if not env_executable:
                raise RuntimeError("evidence_agent_system_env_not_found_on_os_defpath")
            # SDK env overrides merge with the host; env -i starts with only these entries.
            prefix = [env_executable, "-i", *(key + "=" + value for key, value in environment.items())]
        command = prefix + [str(executable), "--no-daemon"]
        for key, value in EVIDENCE_CONFIG.items():
            command.extend(("-c", key + "=" + json.dumps(value)))
        command.extend(("-c", "model=" + json.dumps(self.model), "app-server"))
        client = RecordedClient(CodexConfig(
            launch_args_override=tuple(command),
            client_name="auriga-sol-evidence", client_title="Auriga Sol evidence reviewer",
            cwd=str(cwd)),
            self.store, rejected, notification)
        client.sandbox = sandbox
        return client

    def review_evidence(self, *, input_items, output_schema, request_id, cancel_event=None):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise ValueError("sol_agent_invalid_request_id")
        if cancel_event is not None and cancel_event.is_set():
            raise TimeoutError("visual_review_program_cancelled")
        # Each invocation owns an empty workspace and private native home.
        request_root = self.session.private / "tmp" / "sol-evidence" / request_id
        request_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        cwd = request_root / "workspace"
        cwd.mkdir()
        state = dict(thread_id=None, turn_id=None, rejected_tool=False, rerouted=False,
                     terminal=False, turn_attempted=False, proxy_cancelled=False, interrupted_turn=None, closed=False)
        lock, done = threading.RLock(), threading.Event()
        close_lock = threading.Lock()
        messages, usage, runtime_features = {}, None, None

        def notification(message):
            params = message.get("params") or {}
            if not isinstance(params, dict):
                return
            with lock:
                if params.get("threadId") != state["thread_id"]:
                    return
                if message.get("method") == "turn/started":
                    state["turn_id"] = (params.get("turn") or {}).get("id")
                if message.get("method") == "model/rerouted":
                    state["rerouted"] = True

        def rejected(params):
            state["rejected_tool"] = True
            return dict(success=False, contentItems=[dict(type="inputText", text="Evidence reviewer tools are unavailable.")])

        client = self.client_factory(notification, rejected) if self.client_factory else self._client(notification, rejected, cwd)
        watcher = None

        def close_client():
            with close_lock:
                if state["closed"]:
                    return
                state["closed"] = True
                bridge = getattr(client, "_proc", None) or state.get("bridge")
                if bridge and bridge.poll() is None:
                    try:
                        bridge.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    client.close()
                except (BrokenPipeError, OSError):
                    pass
                if bridge:
                    for stream in (bridge.stdin, bridge.stdout, bridge.stderr):
                        if stream and not stream.closed:
                            try:
                                stream.close()
                            except (BrokenPipeError, OSError):
                                pass
                if getattr(client, "sandbox", None):
                    client.sandbox.close()
                    client.sandbox.ipc.rmdir()

        def cancel_active():
            with lock:
                thread_id, turn_id = state["thread_id"], state["turn_id"]
                if state["terminal"] or not thread_id:
                    return
                # Official thread identity attributes and cancels requests in the
                # existing shared proxy. Retain its tombstone for late admission.
                if not state["proxy_cancelled"]:
                    self.session.model_proxy.cancel_thread(thread_id)
                    state["proxy_cancelled"] = True
                if not turn_id or state["interrupted_turn"] == turn_id:
                    return
                state["interrupted_turn"] = turn_id
            def interrupt():
                try:
                    client.turn_interrupt(thread_id, turn_id)
                except Exception as error:
                    self.store.event("visual.agent_interrupt_failed", dict(request_id=request_id,
                        thread_id=thread_id, error_type=type(error).__name__), source="codex_app_server")
            # The SDK RPC has no local timeout. Backpressure must not prevent
            # explicit cancellation from closing the helper's own bridge.
            interrupter = threading.Thread(target=interrupt, daemon=True)
            interrupter.start()
            interrupter.join(.5)

        def watch_cancel():
            while not done.wait(.05):
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        cancel_active()
                    finally:
                        # Closing this bridge wakes blocked SDK RPC/event reads;
                        # it neither closes nor interrupts the actor client.
                        close_client()
                    return

        try:
            client.start()
            state["bridge"] = getattr(client, "_proc", None)
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("visual_review_program_cancelled")
            if cancel_event is not None:
                watcher = threading.Thread(target=watch_cancel, daemon=True)
                watcher.start()
            client.initialize()
            if not self.client_factory:
                features = _dump(client.request("experimentalFeature/list", dict(limit=200),
                    response_model=ExperimentalFeatureListResponse))
                observed = {item.get("name"): item.get("enabled") for item in features.get("data", [])}
                names = ("memories", "hooks", "goals", "multi_agent", "multi_agent_v2", "code_mode",
                         "code_mode_host", "shell_tool", "unified_exec", "apps", "plugins", "view_image")
                runtime_features = {name: observed.get(name) for name in names}
                self.store.event("visual.agent_features", dict(request_id=request_id,
                    features=runtime_features), source="codex_app_server")
                # Native 0.160 normalizes unified_exec to true even when its CLI
                # flag is false. shell_tool=false removes shell access; retain
                # the native value as provenance instead of misreporting it.
                if any(enabled is not False for name, enabled in runtime_features.items() if name != "unified_exec"):
                    raise ValueError("sol_agent_unverified_runtime_features")
            parameters = dict(model=self.model, modelProvider=MODEL_PROXY_PROVIDER, cwd=str(cwd),
                              approvalPolicy="never",
                              sandbox="read-only" if self.session.sandbox_enabled else "danger-full-access", ephemeral=True,
                              baseInstructions=self.base_instructions, developerInstructions=self.developer_instructions,
                              dynamicTools=[], environments=[], selectedCapabilityRoots=[], runtimeWorkspaceRoots=[str(cwd)],
                              allowProviderModelFallback=False)
            started = _dump(client.thread_start(parameters))
            thread = started["thread"]
            if (started.get("model") != self.model or started.get("modelProvider") != MODEL_PROXY_PROVIDER
                    or thread.get("ephemeral") is not True or thread.get("forkedFromId")
                    or thread.get("turns") or started.get("instructionSources")):
                raise ValueError("sol_agent_requires_verified_fresh_evidence_context")
            with lock:
                state["thread_id"] = thread["id"]
                self.session.model_proxy.register_thread(thread["id"], self.source, request_id)
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("visual_review_program_cancelled")
            presentation = ActorPresentation(self.store)
            model_input = presentation.present(input_items)
            model_schema = presentation.present_data(output_schema)
            state["turn_attempted"] = True
            turn = _dump(client.turn_start(thread["id"], model_input, dict(model=self.model, effort="high",
                outputSchema=model_schema, approvalPolicy="never",
                sandboxPolicy=dict(type="readOnly" if self.session.sandbox_enabled else "dangerFullAccess"))))
            with lock:
                state["turn_id"] = turn["turn"]["id"]
            client.register_turn_notifications(state["turn_id"])
            while True:
                event = client.next_turn_notification(state["turn_id"])
                params = _dump(event.payload)
                if params.get("threadId") != state["thread_id"]:
                    raise ValueError("sol_agent_notification_identity_mismatch")
                if event.method == "thread/tokenUsage/updated":
                    usage = normalize_token_usage(params.get("tokenUsage"))
                item = params.get("item")
                if event.method == "item/completed" and isinstance(item, dict):
                    if item.get("type") == "agentMessage":
                        messages[item["id"]] = item
                    elif item.get("type") not in ("reasoning", "userMessage"):
                        raise ValueError("sol_agent_unexpected_tool_or_context_item")
                if event.method == "turn/completed":
                    terminal = params["turn"]
                    if terminal.get("id") != state["turn_id"]:
                        raise ValueError("sol_agent_turn_identity_mismatch")
                    state["terminal"] = True
                    for item in terminal.get("items", []):
                        if item.get("type") == "agentMessage":
                            messages[item["id"]] = item
                        elif item.get("type") not in ("reasoning", "userMessage"):
                            raise ValueError("sol_agent_unexpected_tool_or_context_item")
                    break
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("visual_review_program_cancelled")
            if terminal.get("status") != "completed" or state["rerouted"] or state["rejected_tool"]:
                self.store.event("visual.agent_turn_unverified", dict(request_id=request_id, terminal=terminal,
                    rerouted=state["rerouted"], rejected_tool=state["rejected_tool"]), source="codex_app_server")
                raise ValueError("sol_agent_unverified_or_incomplete_turn")
            final = [item for item in messages.values() if item.get("phase") == "final_answer"]
            if not final:
                final = [item for item in messages.values() if item.get("phase") is None]
            if len(final) != 1:
                raise ValueError("sol_agent_requires_one_structured_final_answer")
            return dict(model=self.model, status="completed", answer=presentation.restore(json.loads(final[0]["text"])), usage=usage,
                        thread_id=state["thread_id"], turn_id=state["turn_id"],
                        context_policy=CONTEXT_POLICY, backend="official_codex_app_server_sdk",
                        model_verification="official_thread_configuration_and_no_reroute",
                        runtime_policy="independent_official_app_server_empty_home_disabled_memory_hooks_v1",
                        tool_policy="no_robot_dynamic_tools_reject_any_observed_tool_activity_v1",
                        runtime_features=runtime_features,
                        thread_start=started, terminal=terminal)
        except Exception:
            if state["turn_attempted"] and not state["terminal"]:
                cancel_active()
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("visual_review_program_cancelled") from None
            raise
        finally:
            done.set()
            if state["turn_id"]:
                client.unregister_turn_notifications(state["turn_id"])
            if state["thread_id"] and not state["proxy_cancelled"] and not (cancel_event is not None and cancel_event.is_set()):
                try:
                    client.request("thread/unsubscribe", dict(threadId=state["thread_id"]),
                                   response_model=ThreadUnsubscribeResponse)
                except Exception as error:
                    self.store.event("visual.agent_cleanup_failed", dict(request_id=request_id,
                        thread_id=state["thread_id"], error_type=type(error).__name__), source="codex_app_server")
            close_client()
            if state["thread_id"]:
                self.session.model_proxy.unregister_thread(state["thread_id"])
            if hasattr(client, "server_pool"):
                client.server_pool.shutdown(wait=True, cancel_futures=True)
            if watcher is not None:
                watcher.join(2)
