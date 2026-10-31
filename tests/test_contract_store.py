import copy
import tempfile
import unittest

from auriga.core.contracts import validate_action, validate_observation
from auriga.core.storage.store import Store


class ContractStoreCheck(unittest.TestCase):
    def test_shared_observation_profiles_are_typed_without_granting_actor_permission(self):
        from auriga.core.observation_policy import actor_policy
        observation = dict(schema_version="0.1", run_id="run", episode_id="ep", env_instance_id="env",
            env_generation=1, world_kind="observed", observation_id="obs", robot_state_ref="artifact:state", cameras=[])
        validate_observation(observation)  # Missing profile retains the historical RGB default.
        for profile in ("mistyped_profile", None, 1, True, [], {}):
            with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, "unsupported_observation_profile"):
                validate_observation(dict(observation, observation_profile=profile))
        synthetic = dict(observation, observation_profile="rgb_proprio_calibrated_synthetic_v1",
                         calibration_id="fixture", data_origin="synthetic_fixture")
        validate_observation(synthetic)
        with self.assertRaisesRegex(ValueError, "actor_requires_supported_observation_profile"):
            actor_policy(synthetic["observation_profile"])

    def test_immutable_artifacts_events_and_generation(self):
        identity = dict(run_id="check", episode_id="ep", env_instance_id="env", env_generation=1)
        capabilities = dict(frame_id="env_origin", tcp_config_id="tcp", path_kinds=["backend_free"])
        action = dict(schema_version="0.1", **identity, action_id="a", based_on_observation_id="o",
                      frame_id="env_origin", tcp_config_id="tcp", mode="absolute", timeout_s=10,
                      path=dict(kind="backend_free", waypoints=[dict(left=dict(position_m=[0, 0, 1], quaternion_xyzw=[0, 0, 0, 1]))]))
        validate_action(action, identity, "o", capabilities)
        for key, value in [("env_generation", 2), ("world_kind", "internal"), ("based_on_observation_id", "old")]:
            bad = copy.deepcopy(action); bad[key] = value
            with self.assertRaises(ValueError): validate_action(bad, identity, "o", capabilities)
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, identity)
            ref = store.put_artifact(b"rgb", "rgb", "image/png")
            self.assertEqual(store.artifact(ref)[1].read_bytes(), b"rgb")
            self.assertEqual(store.resolve_artifact(ref)[1], b"rgb")
            private_ref = store.put_artifact(b"score", "evaluation", "application/json", "evaluator")
            with self.assertRaisesRegex(ValueError, "access_scope"):
                store.resolve_artifact(private_ref)
            store.set_state("runtime", "identity", identity)
            store.save("request", "a", action); store.save("request", "a", action)
            with self.assertRaises(ValueError): store.save("request", "a", dict(action, timeout_s=11))
            store.event("action.requested", {}, {"action_id": "a"})
            store.event("action.completed", {}, {"action_id": "a"})
            self.assertEqual([e["server_seq"] for e in store.events(1)], [2])
            store.close()
            restored = Store(root, identity)
            self.assertEqual(restored.read("request", "a"), action)
            self.assertEqual(len(restored.events()), 2)
            self.assertEqual(restored.read_state("runtime", "identity"), identity)
            restored.artifact(ref)[1].write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "digest"):
                restored.resolve_artifact(ref)
            restored.close()


if __name__ == "__main__":
    unittest.main()
