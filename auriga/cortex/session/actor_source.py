"""The readable Harness API implementation, without backends or experiment data."""
from pathlib import Path
import shutil


# Explicit publication keeps new evaluators, reference code and package data private.
SOURCE_FILES = (
    "__init__.py", "runtime/__init__.py", "runtime/programs/__init__.py", "runtime/programs/auriga_client.py",
    "runtime/harness.py", "core/__init__.py", "core/contracts.py", "core/action_validation.py",
    "runtime/actions/__init__.py", "runtime/actions/unified_actions.py", "runtime/actions/plan_continuation.py",
    "robot/__init__.py", "robot/motion/__init__.py", "robot/motion/batch_contract.py",
    "robot/motion/batch_motion.py", "robot/model/__init__.py", "robot/model/arx_contract.py",
    "robot/motion/arx_actions.py", "robot/model/geometry.py", "robot/motion/batch_geometry.py",
    "perception/__init__.py", "perception/scene/__init__.py", "perception/scene/scene_contracts.py",
    "cortex/__init__.py", "cortex/context/__init__.py", "cortex/context/actor_instructions.py",
    "core/presentation/__init__.py", "core/presentation/actor_output.py", "core/presentation/actor_presentation.py",
    "core/presentation/actor_payload.py", "core/tool_output.py", "core/tool_errors.py", "core/observation_policy.py",
    "core/presentation/observation_output.py", "core/presentation/history_output.py", "runtime/programs/runner.py",
    "runtime/programs/program_review.py", "runtime/programs/program_result.py",
    "core/presentation/program_output_delivery.py", "runtime/tools/__init__.py", "runtime/tools/definitions.py",
    "runtime/tools/registry.py", "runtime/tools/context.py", "runtime/tools/handlers.py",
    "runtime/tools/builtins.py", "runtime/tools/actions.py", "runtime/tools/program_tools.py",
)


def snapshot_actor_source(destination):
    """Copy actual files; the sandbox mounts this snapshot read-only at /harness."""
    destination = Path(destination)
    source = Path(__file__).resolve().parents[2]
    allowed = {"auriga/" + name for name in SOURCE_FILES}
    if destination.is_symlink() or (destination.exists() and any(
            path.is_symlink() or (path.is_file() and path.relative_to(destination).as_posix() not in allowed)
            for path in destination.rglob("*"))):
        raise ValueError("actor_source_snapshot_contains_unpublished_files")
    for relative in SOURCE_FILES:
        origin = source / relative
        if origin.is_symlink() or not origin.is_file():
            raise ValueError("actor_source_requires_regular_file: " + relative)
        target = destination / "auriga" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError("actor_source_snapshot_symlink")
        shutil.copyfile(origin, target)
    return destination
