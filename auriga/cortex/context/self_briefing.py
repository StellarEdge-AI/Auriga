"""Optional self-briefing before the first actor turn (``features.self_briefing``).

One extra call to the actor's own model, in a fresh ephemeral helper thread on
the episode's counted model transport, reads only what the actor will receive
at startup: the delivered task text (native instruction, after any configured
public override), the public task rules text and the initial camera views. It
returns short operating notes that are appended once to the initial task text,
so they stay with that text through native context compaction.

The wording below is task-independent: it names no task, carries no
per-task text and no evaluator values. A failed call is skipped silently for
the actor and recorded for audit.
"""
import threading
import time

from ...core.contracts import canonical, digest, new_id
from ...core.storage.images import image_url, store_image_settings


VERSION = "self_briefing_v1"
MAX_WORDS = 150
MAX_CHARS = 2000
TIMEOUT_S = 300
SOURCE = "self_briefing"

BASE_INSTRUCTIONS = (
    "You prepare a short operating brief for a robot manipulation agent that acts after you. "
    "Your complete context is the supplied task text, public task rules and initial camera images. "
    "Do not access files, use tools, retrieve memories, request other context, or make robot actions. "
    "Return the requested structured JSON."
)
DEVELOPER_INSTRUCTIONS = "Use only the supplied task text, public rules and images."
PROMPT = (
    "Before the robot acts, write concise operating notes for this episode, at most "
    f"{MAX_WORDS} words, based only on the task text, the public task rules and the initial camera images below. "
    "Cover: (1) the order of the key steps; (2) the contacts, collisions, slips or drops most likely to go wrong "
    "in this scene and how to avoid them; (3) how to check from camera images and measured robot state that the "
    "completion condition is met before finishing. Write only points that hold for this scene and say when the "
    "images leave something uncertain. Do not restate the task text, do not give numeric coordinates, and do not "
    "assume hidden simulator or evaluator state. Return one JSON object with the field notes."
)
ANSWER_SCHEMA = dict(type="object", additionalProperties=False,
                     properties=dict(notes=dict(type="string")), required=["notes"])
SECTION_HEADING = (
    "Operating notes (written by you before acting, from the task text, public rules and initial views; "
    "provisional judgment, not verified world state; the task requirements and fresh evidence govern):"
)


def section(notes):
    return "\n" + SECTION_HEADING + "\n" + notes


def _initial_images(store, observation):
    settings = store_image_settings(store)
    items, sources = [], []
    for camera in (observation or {}).get("cameras") or []:
        info, data = store.resolve_image(camera["rgb_ref"], allowed_scopes=("actor",))
        url, encoding = image_url(data, settings)
        items.extend([dict(type="text", text="Initial view: " + str(camera["camera_id"])),
                      dict(type="image", url=url)])
        sources.append(dict(camera_id=camera["camera_id"], rgb_ref=camera["rgb_ref"],
                            source_sha256=info["sha256"], sha256=encoding.get("sha256")))
    return items, sources


def _clean(notes):
    if not isinstance(notes, str):
        raise ValueError("self_briefing_notes_not_text")
    text = "\n".join(line.rstrip() for line in notes.strip().splitlines()).strip()
    if not text:
        raise ValueError("self_briefing_notes_empty")
    truncated = len(text) > MAX_CHARS
    return (text[:MAX_CHARS].rstrip() if truncated else text), truncated


def generate(agent, store, observation, task_text, *, timeout_s=TIMEOUT_S):
    """Return an audit record; status 'applied' only with usable notes.

    ``agent`` is a CodexEvidenceAgent-compatible object (review_evidence).
    Never raises: any error yields status 'failed' and no notes.
    """
    request_id = new_id("self-briefing")
    started = time.monotonic()
    record = dict(version=VERSION, enabled=True, status="failed", request_id=request_id,
                  model=getattr(agent, "model", None), source=SOURCE, notes=None, usage=None,
                  base_instructions=BASE_INSTRUCTIONS, developer_instructions=DEVELOPER_INSTRUCTIONS,
                  briefing_prompt=PROMPT, task_text=task_text, task_text_sha256=None, images=[], elapsed_s=None,
                  error_type=None, truncated=False, placement="initial_task_text_after_public_rules")
    cancel = threading.Event()
    timer = threading.Timer(timeout_s, cancel.set)
    timer.daemon = True
    try:
        record["task_text_sha256"] = digest(task_text)
        images, sources = _initial_images(store, observation)
        record["images"] = sources
        input_items = [dict(type="text", text=PROMPT), dict(type="text", text="Task text:\n" + task_text), *images]
        request = dict(model=record["model"], input=input_items, outputSchema=ANSWER_SCHEMA, source=SOURCE)
        record["request_ref"] = store.put_artifact(canonical(request).encode(), "self_briefing_request",
                                                   "application/json", "observer")
        store.event("self_briefing.started", {key: record[key] for key in ("version", "request_id", "model", "images")},
                    source="private_model_transport")
        timer.start()
        result = agent.review_evidence(input_items=input_items, output_schema=ANSWER_SCHEMA,
                                       request_id=request_id, cancel_event=cancel)
        record["response_ref"] = store.put_artifact(canonical(result).encode(), "self_briefing_response",
                                                    "application/json", "observer")
        record.update(usage=result.get("usage"), thread_id=result.get("thread_id"), turn_id=result.get("turn_id"))
        if result.get("status") != "completed" or result.get("model") != record["model"]:
            raise ValueError("self_briefing_unverified_or_incomplete_response")
        notes, truncated = _clean((result.get("answer") or {}).get("notes"))
        record.update(status="applied", notes=notes, truncated=truncated,
                      word_count=len(notes.split()))
    except Exception as error:  # Self-briefing is optional; the episode proceeds without it.
        record.update(status="failed", notes=None, error_type=type(error).__name__,
                      cancelled=cancel.is_set())
    finally:
        timer.cancel()
        record["elapsed_s"] = round(time.monotonic() - started, 3)
        try:
            store.event("self_briefing." + record["status"], {key: value for key, value in record.items()
                        if key not in ("base_instructions", "developer_instructions", "briefing_prompt", "task_text")},
                        source="private_model_transport")
        except Exception:
            pass
    return record
