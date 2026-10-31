"""Experiment heartbeats and a passive recorder for non-Codex producers."""

from copy import deepcopy
import os
from pathlib import Path
import threading

from ...core.contracts import canonical, new_id
from ..library.experiments import write_runtime, finish_run
from ...runtime.harness import Harness
from ...core.observation_policy import ACTOR_PROFILE, actor_policy, require_actor_observation
from ...core.storage.store import Store


class ExperimentProgress:
    def __init__(self, directory, **fields):
        self.directory = Path(directory)
        self.fields = dict(status="starting", source="recorded_stream", pid=os.getpid(), **fields)
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.update()
        self.thread.start()

    def update(self, **fields):
        with self.lock:
            self.fields.update(fields)
            write_runtime(self.directory, self.fields)

    def _heartbeat(self):
        while not self.done.wait(2):
            self.update()

    def finish(self, status, summary=None):
        self.done.set()
        self.thread.join()
        return finish_run(self.directory, status=status, summary=summary)


class ReferenceRecorder:
    """Only persists supplied observations/receipts; never invokes the actuator."""

    def __init__(self, directory, kind="reference", observation_profile=None):
        self.identity = dict(run_id=Path(directory).name, episode_id=new_id("ep"),
                             env_instance_id=new_id("reference-env"), env_generation=1)
        self.store = Store(directory, self.identity)
        self.requested_profile = observation_profile
        self.profile_bound = False
        selected = observation_profile or ACTOR_PROFILE
        actor_policy(selected)
        self.harness = Harness(self.store, None, observation_profile=selected, motion_review=False)
        self.kind = kind
        self.progress = ExperimentProgress(directory, kind=kind)
        self.action_id = None

    def start(self, capabilities):
        selected = self.requested_profile or capabilities.get("observation_profile", ACTOR_PROFILE)
        require_actor_observation(capabilities, selected)
        if self.profile_bound and selected != self.harness.observation_profile:
            raise ValueError("recording_observation_profile_changed")
        self.harness.observation_profile = selected
        self.profile_bound = True
        self.harness.capabilities = capabilities
        policy = dict(observation_profile=selected, actor_information_policy=actor_policy(selected),
                      motion_review=False, execution_role="passive_recording_of_external_producer")
        self.store.set_state("runtime", "recording_policy", policy)
        self.store.set_state("runtime", "capabilities", capabilities)
        self.store.set_state("runtime", "identity", self.identity)
        self.store.event("run.started", {"actor_runtime": "roboprobe_simple_api" if self.kind == "reference" else self.kind, "native_terminal": False, **policy})
        self.progress.update(status="running", phase="observing", capabilities=capabilities, **policy)

    def observe(self, raw, historical=False):
        bundle = self.harness.publish_observation(raw, {"action_id": self.action_id} if self.action_id else {},
                                                  self.action_id, make_current=not historical)
        self.store.set_state("runtime", "reference_observation", {"raw_observation_id": raw["raw_observation_id"],
                             "observation_id": bundle["observation_id"]})
        return bundle

    def request(self, action):
        self.action_id = new_id("reference-action")
        request = {**deepcopy(action), "schema_version": "0.1", **self.identity,
                   "action_id": self.action_id, "based_on_observation_id": self.harness.latest["observation_id"]}
        self.store.save("action_request", self.action_id, request)
        self.store.event("action.accepted", {"dispatch_source": "reference_api"}, {"action_id": self.action_id})
        self.progress.update(phase="executing", active_action=self.action_id)

    def execute(self, backend, action, *, progress_callback=None, cancel_flag=None):
        """Record one existing dispatch, preserving callbacks, return value and exceptions."""
        self.request(action)
        def progress(value):
            if progress_callback: progress_callback(value)
            self.motion_progress(value)
        try:
            result = backend.execute(action, progress_callback=progress, cancel_flag=cancel_flag)
        except Exception as error:
            status = "unknown"
            value = dict(status=status, reason="backend_exception", error=f"{type(error).__name__}: {error}")
            self.store.save("action_receipt", self.action_id, value)
            self.store.event("action." + status, value, {"action_id": self.action_id})
            self.action_id = None
            self.progress.update(phase="execution_error", active_action=None)
            raise
        self.receipt(result)
        return result

    def motion_progress(self, value):
        value = dict(value)
        self.harness._native_progress(value.pop("episode_terminal", None))
        raw = value.pop("observation", None)
        if raw:
            value["observation_id"] = self.observe(raw)["observation_id"]
        self.store.event("action.progress", value, {"action_id": self.action_id})

    def receipt(self, raw_receipt):
        receipt = deepcopy(raw_receipt)
        self.harness._native_progress(receipt.pop("episode_terminal", None))
        before, terminal = receipt.pop("before_observation", None), receipt.pop("terminal_observation", None)
        if before:
            receipt["execution_start_observation_id"] = self.observe(before, historical=True)["observation_id"]
        for key in ("planned_trace", "dispatched_trace", "actual_trace", "before_state"):
            value = receipt.pop(key, None)
            receipt[key + "_ref"] = self.store.put_artifact(canonical(value).encode(), key, "application/json") if value is not None else None
        receipt["result_observation_id"] = self.observe(terminal)["observation_id"] if terminal else None
        self.store.save("action_receipt", self.action_id, receipt)
        self.store.event("action." + receipt.get("status", "unknown"), receipt, {"action_id": self.action_id})
        self.action_id = None
        self.progress.update(phase="model_feedback", active_action=None)

    def record_results(self, report):
        """Publish results before a producer resets to another physical generation."""
        evaluation = report.get("evaluation")
        if evaluation is not None:
            ref = self.store.put_artifact(canonical({**self.identity, "result": evaluation}).encode(),
                                          "native_evaluation", "application/json", "observer")
            self.store.event("observer.evaluated", {"result_ref": ref}, source="private_evaluator")
        videos = deepcopy(report.get("videos", {}))
        for video in videos.get("videos", []):
            video["video_ref"] = self.store.put_artifact(Path(video["video_path"]).read_bytes(), "native_video", "video/mp4", "observer")
        if videos.get("videos"):
            self.store.event("observer.video_exported", {**self.identity, **videos}, source="private_evaluator")
    def finish(self, report, status=None, *, record_results=True):
        if record_results: self.record_results(report)
        evaluation = report.get("evaluation")
        status = status or ("failed" if report.get("errors") or report.get("error") or report.get("passed") is False else "completed")
        self.store.event("run." + status, {"termination_reason": report.get("termination_reason")})
        summary = {key: report.get(key) for key in ("episode_verified", "loop_verified", "action_count", "llm_calls", "termination_reason", "data_origin")}
        receipts = self.store.records("action_receipt")
        summary.update(action_count=len(self.store.records("action_request")), completed_actions=sum(item.get("status") == "completed" for item in receipts),
                       failed_actions=sum(item.get("status") == "failed" for item in receipts), rejected_actions=sum(item.get("status") == "rejected" for item in receipts))
        if "passed" in report:
            summary["validation_passed"] = report["passed"]
        summary.update({key: (evaluation or {}).get(key) for key in ("task_success", "normalized_score", "valid_episode")})
        summary["evaluation_episode_index"] = (evaluation or {}).get("episode_index")
        self.progress.finish(status, summary)

    def close(self):
        if not self.progress.done.is_set():
            self.progress.finish("failed", {"reason": "reference_producer_exited_without_report"})
        self.store.close()
