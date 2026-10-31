"""Portable replay stays data-only and rejects incomplete or tampered packages."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import warnings
import zipfile

from auriga.core.config import BackendConfig, Config
from auriga.apps.replay.trajectory_bundle import FORMAT, VERSION_HASHES, export_bundle, read_bundle


def evidence():
    versions = {key: "a" * 64 for key in VERSION_HASHES} | dict(robodojo_commit="b" * 40,
        python="3.11.16", submodules="c" * 40 + " third_party/IsaacLab (heads/main)",
        packages=dict(isaaclab="0.54.3", isaacsim="5.1.0.0", torch="2.7.0+cu128"), private_note="SECRET")
    state = dict(arms={side: dict(q_rad=[0.] * 6, qdot_rad_s=[0.] * 6,
        ee_pose=dict(position_m=[0.] * 3, quaternion_xyzw=[0., 0., 0., 1.], private_note="SECRET"),
        gripper=dict(position_m=[0.] * 2, goal_normalized=1., private_note="SECRET"), private_note="SECRET")
        for side in ("left", "right")}, private_note="SECRET")
    commands = {f"{side}_{part}_joint_state": dict(position=[0.] * length, velocity=0)
        for side in ("left", "right") for part, length in (("arm", 6), ("ee", 2))}
    return dict(evaluation=dict(task="classify_objects", seed=0, layout_id=3, versions=versions,
        sim_step=20, evaluator_actions=2, task_success=True, normalized_score=1., valid_episode=True,
        native_reward=1., native_process_score=100., evaluation_path="/private/SECRET/evaluation.json",
        episode_terminal=dict(ended=True, success=True, reason="native_success", native_action_count=2,
                              native_step_limit=1100, private_note="SECRET")),
        config=dict(features=dict(observation_profile="rgb_proprio_calibrated_v1"),
                    backend=dict(root="/private/SECRET", python="/private/SECRET/python"),
                    model=dict(api_key="SECRET"), images=dict(private_note="SECRET")),
        observations=[dict(instruction="SECRET")],
        samples=[dict(sim_step=step, commands=deepcopy(commands), private_note="SECRET") for step in range(1, 21)],
        states={step: deepcopy(state) for step in (0, 10, 20)}, physics_dt_s=.004, ticks_per_action=10)


class TrajectoryBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "evaluation.json").write_text(json.dumps(dict(run_id="run-fixture")))
        self.archive = evidence()
        loader = patch("auriga.apps.replay.hd_replay.load_archive", return_value=self.archive)
        loader.start()
        self.addCleanup(loader.stop)
        local = Config(backend=BackendConfig(root="/local/robodojo", python="/local/python"))
        settings = patch("auriga.core.config.Config.load", return_value=local)
        settings.start()
        self.addCleanup(settings.stop)
        self.bundle = self.root / "trajectory.zip"

    def exported(self):
        export_bundle(self.source, self.bundle)
        with zipfile.ZipFile(self.bundle) as zipped:
            return {name: zipped.read(name) for name in zipped.namelist()}

    def rewritten(self, payloads):
        path = self.root / "modified.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            for name, data in payloads.items():
                zipped.writestr(name, data)
        return path

    @staticmethod
    def update_payload(payloads, name, rows):
        data = b"".join((json.dumps(row) + "\n").encode() for row in rows)
        payloads[name] = data
        manifest = json.loads(payloads["manifest.json"])
        manifest["members"][name] = dict(byte_size=len(data), sha256=hashlib.sha256(data).hexdigest())
        payloads["manifest.json"] = json.dumps(manifest).encode()

    def test_roundtrip_privacy_and_local_backend(self):
        metadata = export_bundle(self.source, self.bundle)
        imported = read_bundle(self.bundle)
        self.assertEqual(metadata["format"], FORMAT)
        self.assertEqual(metadata["byte_size"], self.bundle.stat().st_size)
        self.assertEqual(metadata["source_run_id"], "run-fixture")
        self.assertEqual(imported["source_file_sha256"], metadata["sha256"])
        self.assertTrue(imported["bundle"])
        self.assertEqual(imported["config"]["backend"], dict(environment="robodojo", robot_model_id="arx-x5",
                                                           root="/local/robodojo", python="/local/python"))
        self.assertEqual(imported["observations"], [])
        self.assertEqual([row["sim_step"] for row in imported["samples"]], list(range(1, 21)))
        self.assertEqual(set(imported["states"]), {0, 10, 20})
        self.assertEqual(imported["samples"][9]["commands"], self.archive["samples"][9]["commands"])
        with zipfile.ZipFile(self.bundle) as zipped:
            self.assertEqual(set(zipped.namelist()), {"manifest.json", "commands.jsonl", "measured_states.jsonl"})
            data = b"\n".join(zipped.read(name) for name in zipped.namelist())
            for forbidden in (b"SECRET", b"evaluation_path", b"backend", b"api_key", b"observations"):
                self.assertNotIn(forbidden, data)

    def test_bundle_downloaded_before_the_rename_is_still_read(self):
        payloads = self.exported()
        manifest = json.loads(payloads["manifest.json"])
        manifest["format"] = "daedalus.robodojo_trajectory.v1"
        payloads["manifest.json"] = json.dumps(manifest).encode()
        self.assertEqual([row["sim_step"] for row in read_bundle(self.rewritten(payloads))["samples"]], list(range(1, 21)))
        manifest["format"] = "other.trajectory.v1"
        payloads["manifest.json"] = json.dumps(manifest).encode()
        with self.assertRaisesRegex(ValueError, "unsupported_trajectory_bundle_format"):
            read_bundle(self.rewritten(payloads))

    def test_existing_target_is_never_replaced_and_failed_export_leaves_no_partial(self):
        self.bundle.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            export_bundle(self.source, self.bundle)
        self.assertEqual(self.bundle.read_bytes(), b"original")
        self.bundle.unlink()
        with patch.object(zipfile.ZipFile, "writestr", side_effect=OSError("disk full")), self.assertRaises(OSError):
            export_bundle(self.source, self.bundle)
        self.assertFalse(self.bundle.exists())
        self.assertEqual(list(self.root.glob(".trajectory-*")), [])

    def test_rejects_traversal_unknown_and_duplicate_members(self):
        payloads = self.exported()
        for member in ("../escape", "/tmp/escape", "extra.py"):
            with self.subTest(member=member), self.assertRaisesRegex(ValueError, "members"):
                read_bundle(self.rewritten(payloads | {member: b"malicious"}))
        duplicate = self.rewritten(payloads)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate, "a") as zipped:
                zipped.writestr("commands.jsonl", payloads["commands.jsonl"])
        with self.assertRaisesRegex(ValueError, "members"):
            read_bundle(duplicate)
        self.assertFalse((self.root / "escape").exists())

    def test_rejects_symlink_members(self):
        payloads = self.exported()
        path = self.root / "symlink.zip"
        with zipfile.ZipFile(path, "w") as zipped:
            for name, data in payloads.items():
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16 if name == "commands.jsonl" else 0
                zipped.writestr(info, data)
        with self.assertRaisesRegex(ValueError, "member_type"):
            read_bundle(path)

    def test_rejects_hash_tampering_and_oversize_before_decompression(self):
        payloads = self.exported()
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            read_bundle(self.rewritten(payloads | {"commands.jsonl": payloads["commands.jsonl"] + b"\n"}))
        limit = sum(map(len, payloads.values())) - 1
        self.assertLess(self.bundle.stat().st_size, limit)
        with patch("auriga.apps.replay.trajectory_bundle.MAX_UNCOMPRESSED_BYTES", limit), \
                patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("must check size first")), \
                self.assertRaisesRegex(ValueError, "too_large"):
            read_bundle(self.bundle)

    def test_rejects_missing_versions_unsafe_task_and_bad_clock(self):
        payloads = self.exported()
        for mutate in (lambda m: m["evaluation"]["versions"].pop("adapter_source_sha256"),
                       lambda m: m["evaluation"].update(task="../../payload"),
                       lambda m: m.update(ticks_per_action=3)):
            with self.subTest(mutate=mutate):
                modified = deepcopy(payloads)
                manifest = json.loads(modified["manifest.json"])
                mutate(manifest)
                modified["manifest.json"] = json.dumps(manifest).encode()
                with self.assertRaises(ValueError):
                    read_bundle(self.rewritten(modified))

    def test_rejects_gapped_commands_duplicate_states_and_nonfinite_measurements(self):
        payloads = self.exported()
        for kind in ("gap", "duplicate", "nan"):
            with self.subTest(kind=kind):
                modified = deepcopy(payloads)
                name = "commands.jsonl" if kind == "gap" else "measured_states.jsonl"
                rows = [json.loads(line) for line in modified[name].splitlines()]
                if kind == "gap":
                    rows.pop(5)
                elif kind == "duplicate":
                    rows.append(rows[-1])
                else:
                    rows[0]["state"]["arms"]["left"]["q_rad"][0] = float("nan")
                self.update_payload(modified, name, rows)
                with self.assertRaises(ValueError):
                    read_bundle(self.rewritten(modified))


if __name__ == "__main__":
    unittest.main()
