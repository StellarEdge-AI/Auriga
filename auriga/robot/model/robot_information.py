"""Packaged robot knowledge, separate from live capabilities and observations.

Add a robot by registering a data directory with ``physical.json``,
``briefing.txt``, ``geometry_model.zip`` and optional ``hardware.json`` / ``geometry.png``. Optional
CAD components are listed in ``resources.json`` with matching ``cad/<id>.STEP``
files. The default answer includes the full briefing, original URDF text and
supplemental physical parameters absent from the URDF. The Harness attaches
its readable file reference and current calibration. Detailed physical records,
hardware comparisons and illustrations are requested separately.
"""

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
from types import MappingProxyType
import zipfile


DEFAULT_ROBOT_ID = "arx-x5"
ROBOT_INFORMATION_SECTIONS = (
    "default", "kinematics", "geometry", "dynamics", "control", "hardware", "all",
)
_DATA_ROOT = Path(__file__).with_name("robot_information_data")
# Canonical identities are deliberate: an unregistered robot never gets X5 data.
ROBOT_INFORMATION_REGISTRY = MappingProxyType({"arx-x5": "arx_x5"})


@lru_cache(maxsize=None)
def _package(robot_id: str) -> dict:
    directory = _DATA_ROOT / ROBOT_INFORMATION_REGISTRY[robot_id]
    reference = json.loads((directory / "physical.json").read_text(encoding="utf-8"))
    return {
        "briefing": (directory / "briefing.txt").read_text(encoding="utf-8").strip(),
        "reference": reference,
    }


def _compact_reference(reference: dict) -> dict:
    """Supplement the inline URDF without repeating its physical records.

    The URDF supplies the joint tree, origins, axes, declared limits, link
    inertials and mesh placements. Keep only interface conventions, contact
    offsets and effective control settings here; native meshes stay in files.
    This projection does not round or infer any physical values.
    """
    kinematics = reference["kinematics"]
    control = reference["control"]
    pose = kinematics["pose_interface"]
    selected = {
        "identity": {key: reference["identity"][key] for key in (
            "physical_model", "native_action_dimensions")},
        "kinematics": {
            "frame_convention": kinematics["frame_convention"],
            "pose_interface": {key: pose[key] for key in (
                "pose_units", "tool_axis_link6", "grasp_contact_offsets_m")},
        },
        "geometry": reference["geometry"]["native_model"],
        "control": {
            "configured_actuators_and_physics": reference["dynamics"]["configured_actuators_and_physics"],
            "timing": {key: control["default_harness"][key] for key in (
                "physics_dt_s", "native_control_hz", "default_max_command_delta_per_joint_rad",
                "speed_scale_range", "gripper_normalized_step_limit", "eef_gripper_dwell_step")},
            "gripper": {key: control["gripper"][key] for key in (
                "normalized_command_range", "native_joint_target_m")},
            "preview_gripper_target_m": control["preview_gripper_mapping"]["preview_command_to_joint_m"],
        },
    }
    return deepcopy(selected)


@lru_cache(maxsize=None)
def _urdf(robot_id: str) -> str:
    """Read the registered assembly verbatim, preserving numeric text/newlines."""
    model = _package(robot_id)["reference"]["geometry"]["native_model"]
    path = robot_resource_path(robot_id, model["resource_id"])
    if path is None:
        raise ValueError("robot_geometry_archive_unavailable")
    with zipfile.ZipFile(path) as archive:
        return archive.read(model["assembly_member"]).decode("utf-8")


def robot_information_text(record: dict) -> str:
    """Render startup data once, with literal XML outside the compact JSON."""
    from ...core.contracts import canonical
    supplementary = {key: value for key, value in record.items() if key != "urdf"}
    text = "Robot physical information\n" + canonical(supplementary)
    if "urdf" in record:
        text += "\n\nRobot URDF\n" + record["urdf"]
    return text


@lru_cache(maxsize=None)
def _hardware(robot_id: str) -> dict | None:
    path = _DATA_ROOT / ROBOT_INFORMATION_REGISTRY[robot_id] / "hardware.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def robot_information(robot_id: str = DEFAULT_ROBOT_ID, section: str = "default") -> dict:
    """Return an independent, model-facing robot knowledge record.

    ``default`` supplies the complete briefing, verbatim URDF, a native mesh
    resource ID and supplemental physical parameters. ``all`` supplies the full numerical records and
    optional hardware comparisons. Named sections select detailed records,
    without adding the briefing.
    Unknown identities and sections return explicit unavailable records.
    No simulation, network access, or source workspace is required.
    """
    record = {"robot_id": robot_id, "available": False, "section": section}
    if not isinstance(robot_id, str) or robot_id not in ROBOT_INFORMATION_REGISTRY:
        return dict(record, error="robot_information_unavailable",
                    message="No physical information is registered for this robot.",
                    available_robot_ids=list(ROBOT_INFORMATION_REGISTRY))
    if not isinstance(section, str) or section not in ROBOT_INFORMATION_SECTIONS:
        return dict(record, error="robot_information_section_unavailable",
                    message="The requested robot information section is unavailable.",
                    available_sections=list(ROBOT_INFORMATION_SECTIONS))

    package = _package(robot_id)
    if section == "default":
        record.update(briefing=package["briefing"], urdf=_urdf(robot_id),
                      reference=_compact_reference(package["reference"]))
    elif section == "all":
        record.update(deepcopy(package))
        hardware = _hardware(robot_id)
        if hardware is not None:
            record["reference"]["hardware"] = deepcopy(hardware)
    else:
        data = _hardware(robot_id) if section == "hardware" else package["reference"].get(section)
        if data is None:
            return dict(record, error="robot_information_section_unavailable",
                        message="This section has no registered information for this robot.")
        record["reference"] = {section: deepcopy(data)}
    record["available"] = True
    return record


def robot_geometry_path(robot_id: str = DEFAULT_ROBOT_ID) -> Path | None:
    """Get an internal illustration path; do not include it in model text.

    The caller may return the image through its usual image tool result.
    It depicts static geometry and never substitutes for a live observation.
    """
    if not isinstance(robot_id, str) or robot_id not in ROBOT_INFORMATION_REGISTRY:
        return None
    path = _DATA_ROOT / ROBOT_INFORMATION_REGISTRY[robot_id] / "geometry.png"
    return path if path.is_file() else None


@lru_cache(maxsize=None)
def _resources(robot_id: str) -> list[dict]:
    path = _DATA_ROOT / ROBOT_INFORMATION_REGISTRY[robot_id] / "resources.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


def robot_resources(robot_id: str = DEFAULT_ROBOT_ID) -> dict:
    """List optional component resources without exposing storage locations."""
    if not isinstance(robot_id, str) or robot_id not in ROBOT_INFORMATION_REGISTRY:
        return dict(robot_id=robot_id, available=False, resources=[],
                    error="robot_information_unavailable")
    resources = _resources(robot_id)
    return dict(robot_id=robot_id, available=bool(resources), resources=deepcopy(resources))


def robot_resource_path(robot_id: str = DEFAULT_ROBOT_ID, resource_id: str = "") -> Path | None:
    """Resolve a registered component internally for the caller's artifact API.

    The resource ID must be listed explicitly in this robot's package.  Never
    return this path as model-facing text; export a normal artifact instead.
    """
    if not isinstance(robot_id, str) or robot_id not in ROBOT_INFORMATION_REGISTRY:
        return None
    if not isinstance(resource_id, str) or resource_id not in {
        entry["resource_id"] for entry in _resources(robot_id)
    }:
        return None
    entry = next(entry for entry in _resources(robot_id) if entry["resource_id"] == resource_id)
    directory = _DATA_ROOT / ROBOT_INFORMATION_REGISTRY[robot_id]
    if entry["format"].startswith("STEP"):
        path = directory / "cad" / f"{resource_id}.STEP"
    elif entry["format"] == "ZIP":
        path = directory / f"{resource_id}.zip"
    else:
        return None
    return path if path.is_file() else None
