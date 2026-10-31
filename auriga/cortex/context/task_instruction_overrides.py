"""Per-task corrections to the task text shown to the actor.

The frozen official documentation (robodojo_task_rules.json) and the native
RoboDojo instruction stay untouched; this layer replaces or appends text only
for tasks listed in task_instruction_overrides.json, and only when
features.task_instruction_overrides is enabled.  Corrections use public
sources only (native instruction text, official documentation, upstream public
issues); they carry no evaluator thresholds and no strategy hints.
"""

import hashlib
import json
from pathlib import Path

from .public_task_rules import actor_task_rules_text


CATALOG_PATH = Path(__file__).with_name("task_instruction_overrides.json")
FIELDS = {"instruction", "replace_sections", "append_sections", "reason", "sources"}


def _heading(section):
    return section.split("\n", 1)[0].strip()


def _validate(task, entry):
    unknown = set(entry) - FIELDS
    if unknown:
        raise ValueError(f"invalid_task_instruction_override:{task}:unknown_fields:{','.join(sorted(unknown))}")
    instruction = entry.get("instruction")
    if instruction is not None and (not isinstance(instruction, str) or not instruction.strip()):
        raise ValueError(f"invalid_task_instruction_override:{task}:instruction")
    replace = entry.get("replace_sections", {})
    if not isinstance(replace, dict) or any(
            not isinstance(key, str) or (value is not None and (not isinstance(value, str) or not value.strip()))
            for key, value in replace.items()):
        raise ValueError(f"invalid_task_instruction_override:{task}:replace_sections")
    append = entry.get("append_sections", [])
    if not isinstance(append, list) or any(
            not isinstance(section, str) or "\n\n" not in section or not _heading(section) for section in append):
        raise ValueError(f"invalid_task_instruction_override:{task}:append_sections")


def task_instruction_override(task, enabled=True):
    """Return the auditable override record for one task (never None)."""
    data = CATALOG_PATH.read_bytes()
    catalog = json.loads(data)
    resolved = catalog.get("aliases", {}).get(task, task)
    entry = catalog["tasks"].get(resolved)
    record = dict(task=task, enabled=bool(enabled), version=catalog["version"],
                  catalog_sha256=hashlib.sha256(data).hexdigest(),
                  status="disabled" if not enabled else "applied" if entry else "not_listed")
    if entry is not None:
        _validate(resolved, entry)
    if enabled and entry is not None:
        record.update(entry_task=resolved, entry=entry)
    return record


def apply_task_instruction_override(instruction, rules, record):
    """Return (instruction, rules_text) as delivered to the actor.

    Without an applied record this is exactly the historical behaviour:
    the native instruction and actor_task_rules_text(rules).
    """
    rules_text = actor_task_rules_text(rules)
    if not record or record.get("status") != "applied":
        return instruction, rules_text
    entry = record["entry"]
    instruction = entry.get("instruction") or instruction
    replace = entry.get("replace_sections", {})
    append = entry.get("append_sections", [])
    if not replace and not append:
        return instruction, rules_text
    sections = list((rules or {}).get("public_rules") or [])
    if replace and not sections:
        raise ValueError("task_instruction_override_requires_public_rules:" + record["task"])
    headings = [_heading(section) for section in sections]
    missing = set(replace) - set(headings)
    if missing:
        raise ValueError("task_instruction_override_unknown_section:" + ",".join(sorted(missing)))
    updated = []
    for heading, section in zip(headings, sections):
        if heading not in replace:
            updated.append(section)
        elif replace[heading] is not None:
            updated.append(heading + "\n\n" + replace[heading].strip())
    updated.extend(section.strip() for section in append)
    text = "\n\n".join(updated)
    if rules_text and rules_text.startswith("RoboDojo task rules:") and sections:
        text = "RoboDojo task rules:\n" + text
    return instruction, text or None
