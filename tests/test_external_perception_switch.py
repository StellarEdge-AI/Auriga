"""Tests for the one-click external_perception master switch and dual-mode isolation."""
import unittest
from types import SimpleNamespace

from auriga.perception.external import object_table, obs3d, pose_skill, scene3d, vggt_height
from auriga.core.config import Config, FeaturesConfig
from auriga.core.contracts import canonical
from auriga.runtime.tools.builtins import create_registry
from auriga.runtime.tools.definitions import public_definitions


def _catalog_runtime(config):
    return SimpleNamespace(
        disabled_tools=(),
        observation_profile="rgb_proprio_calibrated_v1",
        motion_review=True,
        enable_wait=True,
        vision_compare=True,
        visual_reviewer=True,
        action_policy=None,
        capabilities=None,
        programs=True,
        config=config,
        features=config.features,
    )


class ExternalPerceptionSwitchTest(unittest.TestCase):
    def test_default_enables_external_perception_stack(self):
        cfg = Config()
        self.assertTrue(cfg.features.external_perception)
        for flag in ("vggt_height", "object_table", "scene3d", "object_table_observe", "scene3d_observe", "held_perception"):
            self.assertTrue(getattr(cfg.features, flag), flag)
        self.assertFalse(cfg.features.pose_skill)
        self.assertEqual(cfg.features.vggt_height_mode, "optional")
        self.assertEqual(vggt_height.mode(cfg), "optional")
        self.assertTrue(object_table.enabled(cfg))
        self.assertTrue(scene3d.enabled(cfg))
        self.assertEqual(obs3d.flags(cfg), (True, True))
        self.assertEqual(scene3d.workspace_modules(cfg), ("scene3d_helpers.py",))
        rt = _catalog_runtime(cfg)
        catalog = {item["name"]: item for item in create_registry(runtime=rt).catalog(rt, role="actor")}
        self.assertNotIn("pose_plan", catalog)
        self.assertIn("object_table", catalog["geometry_query"]["description"])
        self.assertIn("scene3d", catalog["geometry_query"]["description"])
        self.assertIn("VGGT", catalog["robot_observe"]["description"])
        dev_prompt = obs3d.developer_instructions(cfg.features)
        self.assertIn("## 3D Perception (VGGT + SAM)", dev_prompt)
        self.assertIn("object_table", dev_prompt)
        self.assertIn("scene3d_helpers", dev_prompt)

    def test_explicit_false_restores_native_standalone_mode(self):
        cfg = Config().with_overrides({"features": {"external_perception": False}})
        self.assertFalse(cfg.features.external_perception)
        for flag in ("vggt_height", "object_table", "scene3d", "object_table_observe", "scene3d_observe", "held_perception", "pose_skill"):
            self.assertFalse(getattr(cfg.features, flag), flag)
        self.assertEqual(cfg.features.vggt_height_mode, "optional")
        self.assertEqual(obs3d.developer_instructions(cfg.features), "")
        rt = _catalog_runtime(cfg)
        self.assertEqual(
            canonical(create_registry(runtime=rt).catalog(rt, role="actor")),
            canonical(create_registry().catalog(rt, role="actor")),
        )

    def test_direct_dataclass_construction_and_reversible_toggle(self):
        feat = FeaturesConfig(external_perception=True)
        self.assertTrue(feat.vggt_height)
        self.assertTrue(feat.object_table)
        self.assertTrue(feat.scene3d)
        self.assertTrue(feat.object_table_observe)
        self.assertTrue(feat.scene3d_observe)
        self.assertTrue(feat.held_perception)
        self.assertFalse(feat.pose_skill)
        self.assertEqual(feat.vggt_height_mode, "optional")

        feat_off = FeaturesConfig(external_perception=False)
        for flag in ("vggt_height", "object_table", "scene3d", "object_table_observe", "scene3d_observe", "held_perception", "pose_skill"):
            self.assertFalse(getattr(feat_off, flag), flag)

        cfg_on = Config.from_dict({"features": {"external_perception": True}})
        cfg_off = cfg_on.with_overrides({"features": {"external_perception": False}})
        self.assertFalse(cfg_off.features.external_perception)
        for flag in ("vggt_height", "object_table", "scene3d", "object_table_observe", "scene3d_observe", "held_perception", "pose_skill"):
            self.assertFalse(getattr(cfg_off.features, flag), flag)
        self.assertEqual(cfg_off.features.vggt_height_mode, "optional")

    def test_master_switch_respects_explicit_sub_switch_override(self):
        cfg = Config.from_dict({"features": {"external_perception": True, "pose_skill": False, "vggt_height_mode": "default"}})
        self.assertTrue(cfg.features.external_perception)
        self.assertTrue(cfg.features.object_table)
        self.assertTrue(cfg.features.object_table_observe)
        self.assertFalse(cfg.features.pose_skill)
        self.assertEqual(cfg.features.vggt_height_mode, "default")


if __name__ == "__main__":
    unittest.main()
