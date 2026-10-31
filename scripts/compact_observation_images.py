#!/usr/bin/env python3
"""Replace RGB blobs in an explicitly selected, ended run with labelled video frames."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auriga.core.storage.image_archive import compact_images
from auriga.apps.library.experiments import atomic_json
from auriga.core.storage.store import Store


def compact_run(root):
    root = Path(root).resolve()
    if not (root / "events.sqlite").is_file():
        raise ValueError("image_compaction_requires_existing_store")
    runtime = json.loads((root / "runtime.json").read_text())
    manifest = json.loads((root / "manifest.json").read_text())
    if (runtime.get("status") not in ("completed", "stopped", "failed") or not runtime.get("finished_at")
            or runtime.get("active_action") is not None):
        raise ValueError("image_compaction_requires_finished_inactive_run")
    if runtime.get("pid"):
        try:
            os.kill(runtime["pid"], 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError("image_compaction_requires_exited_process")
    with (root / "gateway.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if json.loads((root / "runtime.json").read_text()) != runtime:
            raise ValueError("image_compaction_runtime_changed")
        store = Store(root, manifest)
        try:
            result = compact_images(store)
            atomic_json(root / "image-archive.json", result)
            return result
        finally:
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--apply", action="store_true", help="Remove verified RGB originals; reconstructions are lossy.")
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required: video reconstructions cannot reproduce original JPEG bytes")
    print(json.dumps(compact_run(args.run_dir), indent=2))
