"""CPU-only robot selection and deployment-binding compatibility checks."""
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
import unittest

from auriga.robot.model.arx_contract import FRAME_ID, TCP_CONFIG_ID
from auriga.robot.model.embodiments import ARX_X5, RobotDescription, get_robodojo_robot


class EmbodimentTests(unittest.TestCase):
    def test_default_preserves_arx_control_and_deployment_paths(self):
        binding = get_robodojo_robot()
        self.assertIs(binding, get_robodojo_robot("arx-x5"))
        self.assertIs(binding.robot, ARX_X5)
        self.assertEqual(ARX_X5.frame_id, FRAME_ID)
        self.assertEqual(ARX_X5.tcp_config_id, TCP_CONFIG_ID)
        for field, path in {
            "environment_config_path": "env_cfg/arx_x5.yml",
            "urdf_path": "Assets/Robots/x5/X5A.urdf",
            "robot_config_path": "Assets/Robots/x5/robot_config.yml",
            "curobo_config_path": "Assets/Robots/x5/curobo.yml",
            "layout_directory": "Eval_Layout/RoboDojo/arx_x5",
        }.items():
            self.assertEqual(getattr(binding, field), Path(path))

    def test_unknown_or_malformed_selection_never_falls_back(self):
        for selection in ("unknown", "ARX-X5", "", None, [], {}):
            with self.subTest(selection=selection), self.assertRaisesRegex(
                ValueError, "unsupported_robodojo_robot",
            ):
                get_robodojo_robot(selection)

    def test_robot_description_has_no_environment_assets_and_binding_is_immutable(self):
        self.assertEqual({field.name for field in fields(RobotDescription)},
                         {"robot_model_id", "frame_id", "tcp_config_id"})
        with self.assertRaises(FrozenInstanceError):
            ARX_X5.tcp_config_id = "another-tcp"
        with self.assertRaises(FrozenInstanceError):
            get_robodojo_robot().urdf_path = Path("another.urdf")


if __name__ == "__main__":
    unittest.main()
