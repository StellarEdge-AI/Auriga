"""Install the CPU-only experiment library as a lingering systemd user service."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from auriga.core.config import Config


def quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def unit_text(repository, python, root, port, *, scan_interval=1, public_origin=None, task_source_root=None):
    values = (repository, python, root, public_origin, task_source_root)
    if any(char in str(value) for value in values if value is not None for char in "\r\n\x00"):
        raise ValueError("service settings cannot contain control characters")
    options = f" --scan-interval {scan_interval}"
    if public_origin is not None:
        options += " --public-origin " + quote(public_origin)
    if task_source_root is not None:
        options += " --task-source-root " + quote(task_source_root)
    return f"""[Unit]
Description=Auriga experiment library (CPU only)
After=network.target

[Service]
Type=simple
WorkingDirectory={str(repository).replace("%", "%%")}
ExecStart={quote(python)} -u -m auriga.apps.library --root {quote(root)} --port {port}{options}
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
UMask=0077

[Install]
WantedBy=default.target
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Unified Auriga JSON configuration")
    parser.add_argument("--root")
    parser.add_argument("--port", type=int)
    parser.add_argument("--scan-interval", type=float)
    parser.add_argument("--public-origin")
    parser.add_argument("--task-source-root")
    parser.add_argument("--install", action="store_true", help="Write the unit and enable/start this service; default prints the unit")
    args = parser.parse_args()
    overrides = {key: getattr(args, key) for key in ("root", "port", "scan_interval", "public_origin", "task_source_root")
                 if getattr(args, key) is not None}
    config = Config.load(args.config, overrides={"library": overrides})
    for key, value in config.to_dict()["library"].items():
        setattr(args, key, value)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024–65535")
    repository = Path(__file__).resolve().parents[1]
    root = Path(args.root).expanduser().resolve()
    if any(char in str(root) + str(repository) + sys.executable for char in "\r\n\x00"):
        parser.error("service paths cannot contain control characters")
    unit = unit_text(repository, sys.executable, root, args.port, scan_interval=args.scan_interval,
                     public_origin=args.public_origin, task_source_root=args.task_source_root or config.backend.root)
    if not args.install:
        print(unit, end="")
        return
    runtime = Path("/run/user") / str(os.getuid())
    env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime), DBUS_SESSION_BUS_ADDRESS=f"unix:path={runtime}/bus")
    if not (runtime / "bus").exists():
        parser.error("systemd user bus is unavailable")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    service_dir = root / "service"
    service_dir.mkdir(exist_ok=True)
    target = Path.home() / ".config/systemd/user/auriga-library.service"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_text() != unit:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (service_dir / f"auriga-library.service.{timestamp}.bak").write_bytes(target.read_bytes())
    target.write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], env=env, check=True)
    subprocess.run(["systemctl", "--user", "enable", "auriga-library.service"], env=env, check=True)
    subprocess.run(["systemctl", "--user", "restart", "auriga-library.service"], env=env, check=True)
    subprocess.run(["systemctl", "--user", "is-active", "auriga-library.service"], env=env, check=True)
    receipt = {"unit": str(target), "root": str(root), "url": f"http://127.0.0.1:{args.port}",
               "python": sys.executable, "repository": str(repository), "installed_at": datetime.now(timezone.utc).isoformat()}
    (service_dir / "installation.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
