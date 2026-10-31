"""Robot control identities and explicit environment asset bindings.

Descriptions are independent of simulator installation paths. A binding selects
the assets for one supported robot/environment combination; it adds no support
for that robot to another environment or action adapter.
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RobotDescription:
    robot_model_id: str
    frame_id: str
    tcp_config_id: str


@dataclass(frozen=True)
class RoboDojoRobotBinding:
    """Paths are relative to RoboDojo root, except layout_directory (Assets root)."""

    robot: RobotDescription
    environment_config_path: Path
    urdf_path: Path
    robot_config_path: Path
    curobo_config_path: Path
    layout_directory: Path


ARX_X5 = RobotDescription(
    robot_model_id="arx-x5", frame_id="env_origin", tcp_config_id="arx-x5-link6-v1",
)

_ROBODOJO_ROBOTS = {
    ARX_X5.robot_model_id: RoboDojoRobotBinding(
        robot=ARX_X5,
        environment_config_path=Path("env_cfg/arx_x5.yml"),
        urdf_path=Path("Assets/Robots/x5/X5A.urdf"),
        robot_config_path=Path("Assets/Robots/x5/robot_config.yml"),
        curobo_config_path=Path("Assets/Robots/x5/curobo.yml"),
        layout_directory=Path("Eval_Layout/RoboDojo/arx_x5"),
    ),
}


def get_robodojo_robot(robot_model_id: str = ARX_X5.robot_model_id) -> RoboDojoRobotBinding:
    """Select an explicitly supported binding; never substitute another robot."""
    try:
        return _ROBODOJO_ROBOTS[robot_model_id]
    except (KeyError, TypeError):
        raise ValueError(f"unsupported_robodojo_robot: {robot_model_id!r}") from None
