"""Run with: python -m unittest discover -s tests -p test_scene.py -v."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from auriga.core.contracts import digest, validate_action
from auriga.perception.scene.store import SceneStore


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "contracts" / "fixtures"


class SceneCheck(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SceneStore(self.directory.name)
        self.fixture = json.loads((FIXTURE_ROOT / "scene_plane_box.json").read_text())
        self.identity = self.fixture["observations"][0]
        for observation in self.fixture["observations"]:
            self.store.import_observation(observation)
        self.scene = self.store.apply_patch(self.fixture["patches"][0])

    def test_analytic_fixture_and_unknown_space(self):
        fixture = deepcopy(self.fixture)
        claimed = fixture.pop("fixture_digest")
        self.assertEqual(claimed, digest(fixture))
        for artifact in fixture["artifacts"]:
            content = (FIXTURE_ROOT / artifact["storage_key"]).read_bytes()
            self.assertEqual(artifact["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(artifact["byte_size"], len(content))
        results = [self.store.query(query, 1, query) for query in fixture["queries"]]
        self.assertEqual([item["status"] for item in results], fixture["expected"]["query_statuses"])
        self.assertEqual(list(results[0]["value"]["point_m"]), fixture["expected"]["ray_point_m"])
        self.assertAlmostEqual(results[1]["value"]["distance_m"], fixture["expected"]["box_distance_m"])
        self.assertTrue(all(item["world_kind"] == "internal" and item["data_origin"] == "synthetic_fixture" for item in results))
        self.assertIsNone(results[2]["value"])
        self.assertTrue(results[2]["unknown_regions"])
        parallel = dict(kind="ray_surface", entity_id="table", origin_m=[0, 0, 1], direction=[1, 0, 0])
        self.assertEqual(self.store.query(self.identity, 1, parallel)["status"], "unknown")
        box = dict(kind="ray_surface", entity_id="box", origin_m=[0.3, 0.3, 1], direction=[0, 0, -2])
        self.assertAlmostEqual(self.store.query(self.identity, 1, box)["value"]["point_m"][2], 0.2)

    def test_snapshot_stays_frozen_and_cas_conflicts(self):
        before = self.store.snapshot(self.identity, 1, "obs-scene-0")
        frozen_digest = digest(before)
        patch = deepcopy(self.fixture["patches"][0])
        patch["base_scene_version"] = 1
        patch["observation_ids"] = ["obs-scene-1"]
        patch["operations"] = [patch["operations"][1]]
        patch["operations"][0]["geometry"] = dict(kind="box", min_m=[0.6, 0.2, 0], max_m=[0.8, 0.4, 0.2])
        self.store.apply_patch(patch)
        after = self.store.snapshot(self.identity, 1, "obs-scene-0")
        self.assertEqual(digest(after), frozen_digest)
        self.assertEqual(after["scene"]["entities"]["box"]["geometry"]["min_m"][0], 0.2)
        self.assertEqual(self.store.head(self.identity)["scene_version"], 2)
        with self.assertRaisesRegex(ValueError, "scene_version_conflict"):
            self.store.apply_patch(patch)
        changed = deepcopy(self.identity)
        changed["sim_step"] = 2
        with self.assertRaisesRegex(ValueError, "immutable_content_conflict"):
            self.store.import_observation(changed)
        reloaded = SceneStore(self.directory.name)
        self.assertEqual(reloaded.version(self.identity, 1), self.scene)

    def test_lost_entities_and_bad_queries(self):
        patch = deepcopy(self.fixture["patches"][0])
        patch["base_scene_version"] = 1
        patch["operations"] = [dict(op="mark_lost", entity_id="box", source_kind="observation_inference", reason="Occluded; current location unknown.")]
        self.store.apply_patch(patch)
        self.assertEqual(self.store.query(self.identity, 2, dict(kind="distance", entity_id="box", point_m=[0, 0, 0]))["status"], "unknown")
        bad = dict(kind="ray_surface", entity_id="table", origin_m=[0, 0, 1], direction=[0, 0, 0])
        self.assertEqual(self.store.query(self.identity, 1, bad)["status"], "invalid_input")
        wrong_frame = dict(kind="distance", entity_id="box", point_m=[0, 0, 0], frame_id="camera_optical")
        self.assertEqual(self.store.query(self.identity, 1, wrong_frame)["status"], "invalid_input")
        self.assertEqual(self.store.query(self.identity, 1, dict(kind="preview_ee"))["reason"], "unsupported_capability")

    def test_entity_evidence_subset_owns_its_timestamp(self):
        patch = deepcopy(self.fixture["patches"][0])
        patch.update(base_scene_version=1, observation_ids=["obs-scene-0", "obs-scene-1"])
        patch["operations"] = [operation for operation in patch["operations"] if operation["op"] == "upsert_geometry"]
        for operation in patch["operations"]:
            operation["observation_ids"] = ["obs-scene-0" if operation["entity_id"] == "box" else "obs-scene-1"]
        scene = self.store.apply_patch(patch)
        for name, entity in scene["entities"].items():
            observation_id = "obs-scene-0" if name == "box" else "obs-scene-1"
            observation = self.store.observation(self.identity, observation_id)
            self.assertEqual(entity["observation_ids"], [observation_id])
            self.assertEqual(entity["last_observation_id"], observation_id)
            for field in ("step_domain", "clock_domain"):
                self.assertEqual(entity[field], observation[field])
            self.assertEqual(entity["last_sim_step"], observation["sim_step"])

    def test_newer_patch_observation_cannot_revive_entity_from_stale_evidence(self):
        patch = deepcopy(self.fixture["patches"][0])
        patch.update(base_scene_version=1, observation_ids=["obs-scene-1"], operations=[dict(
            op="mark_dirty", entity_id="box", source_kind="observation_inference", reason="Object moved")])
        self.store.apply_patch(patch)
        operation = deepcopy(next(item for item in self.fixture["patches"][0]["operations"]
            if item.get("entity_id") == "box"))
        operation.update(observation_ids=["obs-scene-0"], source_kind="observation_inference", assumptions=[])
        patch.update(base_scene_version=2, observation_ids=["obs-scene-0", "obs-scene-1"], operations=[operation])
        with self.assertRaisesRegex(ValueError, "late_observation_cannot_overwrite_entity"):
            self.store.apply_patch(patch)
        self.assertEqual(self.store.head(self.identity)["scene_version"], 2)
        self.assertEqual(self.store.query(self.identity, 2, dict(kind="entity_pose", entity_id="box"))["status"], "unknown")

    def test_entity_evidence_requires_its_own_comparable_dated_clock(self):
        for change, reason in ((dict(sim_step=None), "undated_observation_cannot_overwrite_entity"),
                (dict(clock_domain="other-clock"), "incomparable_entity_clock"),
                (dict(step_domain="physics_step"), "incomparable_entity_clock")):
            with self.subTest(change=change):
                observation = dict(self.fixture["observations"][0], **change,
                    observation_id="different-" + next(iter(change)))
                self.store.import_observation(observation)
                patch = deepcopy(self.fixture["patches"][0])
                operation = next(item for item in patch["operations"] if item.get("entity_id") == "box")
                operation["observation_ids"] = [observation["observation_id"]]
                patch.update(base_scene_version=1, observation_ids=[observation["observation_id"], "obs-scene-1"],
                    operations=[operation])
                with self.assertRaisesRegex(ValueError, reason):
                    self.store.apply_patch(patch)
                if observation["sim_step"] is not None:
                    operation["observation_ids"].append("obs-scene-1")
                    with self.assertRaisesRegex(ValueError, "incomparable_entity_clock"):
                        self.store.apply_patch(patch)
        self.assertEqual(self.store.head(self.identity)["scene_version"], 1)

    def test_internal_results_have_no_observed_execution_permission(self):
        result = self.store.rollout(dict(snapshot_id="fixture-snapshot", world_kind="internal"))
        self.assertEqual(result["status"], "unsupported")
        self.assertFalse(result["executed"])
        with self.assertRaisesRegex(ValueError, "no observed-world execution permission"):
            self.store.rollout(dict(world_kind="observed"))
        with self.assertRaisesRegex(ValueError, "no observed-world execution permission"):
            self.store.rollout(dict(submit_to_observed=True))
        # The shared gateway validator rejects an internal output before motion validation.
        candidate = self.store.query(self.identity, 1, self.fixture["queries"][0])
        with self.assertRaisesRegex(ValueError, "internal_action_cannot_execute_observed_world"):
            validate_action(candidate, self.identity, "obs-scene-0", {})
        capabilities = self.store.capabilities()
        self.assertFalse(capabilities["worker_ready"])
        self.assertEqual(capabilities["physics"], "unsupported")


if __name__ == "__main__":
    unittest.main()
