"""Bind queued attempts to source bytes, including non-Git frozen checkouts."""

import hashlib
from pathlib import Path

from auriga.core import legacy_names as legacy
from auriga.core.contracts import digest


# Frozen checkouts made before the rename (0.1.x) and before the package move (0.0.x)
# keep their original entry points and environment variable prefix.
CHECKOUT_LAYOUTS = (
    dict(gateway="auriga/apps/gateway/cli.py", config="auriga/core/config.py", module="auriga.apps.gateway",
         package="auriga", env_prefix="AURIGA_"),
    dict(gateway=legacy.PACKAGE + "/apps/gateway/cli.py", config=legacy.PACKAGE + "/core/config.py",
         module=legacy.PACKAGE + ".apps.gateway", package=legacy.PACKAGE, env_prefix=legacy.ENV_PREFIX),
    dict(gateway=legacy.PACKAGE + "/gateway.py", config=legacy.PACKAGE + "/config.py",
         module=legacy.PACKAGE + ".gateway", package=legacy.PACKAGE, env_prefix=legacy.ENV_PREFIX),
)


def checkout_layout(root):
    """Return the entry-point layout of a checkout, or None when it has no gateway."""
    for layout in CHECKOUT_LAYOUTS:
        if (Path(root) / layout["gateway"]).is_file():
            return layout
    return None


def checkout_environment(environment, layout):
    """A frozen checkout reads its own variable prefix; mirror the queue's AURIGA_* settings into it."""
    prefix = layout["env_prefix"]
    if prefix == "AURIGA_":
        return environment
    return {**environment, **{prefix + key[len("AURIGA_"):]: value
                              for key, value in environment.items() if key.startswith("AURIGA_")}}


def source_binding(root):
    root = Path(root).resolve()
    files = {}
    for directory in ("auriga", legacy.PACKAGE, "scripts", "configs", "third_party", "benchmarks"):
        if (root / directory).is_symlink():
            raise ValueError("source_binding_symlink_denied:" + str(root / directory))
        for path in sorted((root / directory).rglob("*")):
            if "__pycache__" in path.parts:
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("source_binding_symlink_denied:" + str(path))
            if path.is_file():
                with path.open("rb") as stream:
                    files[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    for name in ("pyproject.toml", "requirements.lock.txt", "environment.yml"):
        path = root / name
        if path.exists():
            if path.is_symlink():
                raise ValueError("source_binding_symlink_denied:" + str(path))
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not any(layout["gateway"] in files for layout in CHECKOUT_LAYOUTS):
        raise ValueError("source_binding_gateway_missing:" + str(root))
    return {"version": 1, "files": files, "sha256": digest(files)}


def verify_source(root, expected):
    actual = source_binding(root)
    if actual != expected:
        raise ValueError("queue_source_changed:" + str(root))
    return actual["sha256"]


def bind_sources(cases, expected=None):
    roots = {str(Path(case["cwd"]).resolve()) for case in cases}
    if expected is not None:
        if not isinstance(expected, dict) or not roots <= expected.keys():
            raise ValueError("queue_source_binding_missing")
        for root in roots:
            verify_source(root, expected[root])
        return expected
    return {root: source_binding(root) for root in sorted(roots)}
