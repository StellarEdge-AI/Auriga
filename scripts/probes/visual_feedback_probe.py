#!/usr/bin/env python3
"""Offline RGB comparison: deterministic tracker and opt-in bounded model calls."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auriga.cortex.session.codex import CodexSession
from auriga.cortex.session.codex_evidence_agent import CodexEvidenceAgent
from auriga.cortex.session.model_proxy import DEFAULT_MAX_RETRIES, RETRY_POLICY
from auriga.core.storage.store import Store
from auriga.perception.visual_feedback import compare_observations

MODELS = ("gpt-6-astra", "gpt-6.1-sol")
ANSWER_SCHEMA = dict(type="object", additionalProperties=False, properties=dict(
    visibility_after=dict(type="string", enum=["visible", "partly_visible", "not_visible", "uncertain"]),
    motion=dict(type="string", enum=["displaced", "roughly_stationary", "uncertain"]),
    after_center_px=dict(anyOf=[dict(type="array", items=dict(type="number"), minItems=2, maxItems=2), dict(type="null")]),
    retained_with_gripper=dict(type="string", enum=["visually_supported", "not_visually_supported", "uncertain"]),
    evidence=dict(type="string")), required=["visibility_after", "motion", "after_center_px", "retained_with_gripper", "evidence"])


def model_request(request_path, output):
    """One fresh official Codex evidence agent, with the harness retry policy."""
    output = Path(output)
    payload = json.loads(Path(request_path).read_text())
    body = json.dumps(payload, allow_nan=False).encode()
    receipt = dict(requested_model=payload["model"], backend="official_codex_app_server_sdk",
                   request_sha256=hashlib.sha256(body).hexdigest(), status="started", max_completed_calls=1,
                   max_retries=DEFAULT_MAX_RETRIES, retry_policy=RETRY_POLICY)
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2))
    started = time.monotonic()
    store = Store(output / "agent-run", dict(run_id="visual-probe", episode_id="offline",
                  env_instance_id="saved-images", env_generation=1))
    session = CodexSession(store.root, store, lambda _: {}, model=payload["model"], effort="high")
    try:
        # Bootstrap the existing runtime without starting an actor turn. Only
        # the independent evidence agent below makes an inference request.
        session.start([], "Offline visual probe bootstrap; no actor turn is started.")
        session.model_proxy.max_requests = 1
        result = CodexEvidenceAgent(session, model=payload["model"]).review_evidence(
            input_items=payload["input"], output_schema=payload["outputSchema"], request_id="offline-comparison")
        (output / "response.json").write_text(json.dumps(result, indent=2))
        receipt.update(served_model=result.get("model"), usage=result.get("usage"),
                       thread_id=result.get("thread_id"), turn_id=result.get("turn_id"),
                       context_policy=result.get("context_policy"))
        if result.get("model") != payload["model"]:
            raise ValueError("served_model_mismatch_or_unknown")
        if result.get("status") != "completed":
            raise ValueError("provider_response_not_completed")
        answer = result["answer"]
        import jsonschema
        jsonschema.validate(answer, ANSWER_SCHEMA)
        receipt.update(status="completed", answer=answer)
    except Exception as error:
        receipt.update(status="failed", error_type=type(error).__name__, reason=str(error) if isinstance(error, ValueError) else None)
    finally:
        if session.model_proxy:
            receipt.update(completed_calls=session.model_proxy.request_count,
                           http_attempt_count=session.model_proxy.attempt_count)
        session.close()
        store.close()
        receipt["wall_s"] = time.monotonic() - started
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", nargs="*", choices=MODELS, default=[])
    parser.add_argument("--request", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.request:
        model_request(args.request, args.output)
        return
    cases = json.loads(args.cases.read_text())
    if not isinstance(cases, list) or not 1 <= len(cases) <= 3 or len(set(args.models)) != len(args.models):
        raise ValueError("probe_accepts_one_to_three_cases_and_distinct_models")
    args.output.mkdir(parents=True, exist_ok=False)
    from benchmark_queue import experiment_environment
    environment, settings = experiment_environment()
    report = dict(scope="offline_visual_comparison_no_robot_actions", models=args.models,
                  max_requests=len(cases)*len(args.models), backend="official_codex_app_server_sdk",
                  hard_deadline_s_per_request=None, child_process_deadline_s=None,
                  max_retries=DEFAULT_MAX_RETRIES, retry_policy=RETRY_POLICY,
                  experimental_settings=settings, cases=[])
    for index, case in enumerate(cases):
        run = Path(case["run_dir"])
        manifest = json.loads((run / "manifest.json").read_text())
        store = Store(run, manifest, read_only=True)
        try:
            request = {key: case[key] for key in ("before_observation_id", "after_observation_id", "camera_id", "roi")}
            if "reference_roi" in case:
                request["reference_roi"] = case["reference_roi"]
            comparison = compare_observations(store, manifest, **request)
            item = dict(case=case, deterministic=comparison, model_results={})
            directory = args.output / f"case-{index:02}"
            directory.mkdir()
            contents = [dict(type="text", text=(
                "Compare the BEFORE and AFTER original RGB images. The caller-selected target is " + case["target"] + ". "
                "Its BEFORE region in original-image pixels [x,y,width,height] is " + str(case["roi"]) + ". "
                "Report only visible evidence. Camera/occlusion/background can confuse matching. "
                "Use uncertain/null if the same target cannot be identified; do not infer metric height, contact, hidden state or task success. "
                "Retained-with-gripper means visual evidence only. Return one concise JSON object matching the output schema."))]
            for label, source in zip(("BEFORE", "AFTER"), comparison["sources"]):
                artifact, raw = store.resolve_artifact(source["rgb_ref"], allowed_scopes=("actor",))
                contents.extend([dict(type="text", text=label), dict(type="image", url=
                    f"data:{artifact['mime_type']};base64," + base64.b64encode(raw).decode())])
                (directory / (label.lower()+".png")).write_bytes(raw)
            (directory / "deterministic.json").write_text(json.dumps(comparison, indent=2))
            for model in args.models:
                child = directory / model
                child.mkdir()
                payload = dict(model=model, effort="high", input=contents, outputSchema=ANSWER_SCHEMA)
                request_path = child / "request.json"
                request_path.write_text(json.dumps(payload))
                process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--request", str(request_path), "--output", str(child)],
                                         env=environment, capture_output=True)
                receipt = json.loads((child / "receipt.json").read_text()) if (child / "receipt.json").exists() else dict(status="child_failed_before_request_receipt")
                receipt["process_returncode"] = process.returncode
                item["model_results"][model] = receipt
                print(index, model, receipt["status"], flush=True)
            report["cases"].append(item)
            (args.output / "report.json").write_text(json.dumps(report, indent=2))
        finally:
            store.close()
    print(str(args.output / "report.json"))


if __name__ == "__main__":
    main()
