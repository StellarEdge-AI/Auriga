"""Verbatim frozen official task sections, never runtime evaluator state."""

import hashlib
import json
from pathlib import Path


CATALOG_PATH = Path(__file__).with_name("robodojo_task_rules.json")


def actor_task_rules_text(record):
    """Remove the historical citation heading, including when resuming a run."""
    text = (record or {}).get("text")
    if not text:
        return None
    heading, separator, body = text.partition("\n")
    if heading.startswith("Official RoboDojo public task rules ("):
        return "RoboDojo task rules:" + separator + body
    return text


def public_task_rules(task):
    data = CATALOG_PATH.read_bytes()
    catalog = json.loads(data)
    entry = catalog["tasks"].get(task)
    record = dict(task=task, version=catalog["version"],
                  catalog_sha256=hashlib.sha256(data).hexdigest(),
                  status="documented" if entry else "unavailable", text=None)
    if entry is None:
        return record
    rules = entry["public_rules"]
    if (not isinstance(rules, list) or not rules
            or any(not isinstance(rule, str) or not rule.strip() for rule in rules)
            or not entry["source_url"].startswith("https://robodojo-benchmark.com/")):
        raise ValueError("invalid_public_task_rules_catalog:" + task)
    record.update(source_url=entry["source_url"], public_rules=rules,
                  text="\n\n".join(rules))
    return record
