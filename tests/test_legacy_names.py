"""Data and checkouts created before the Daedalus -> Auriga rename stay usable."""
from pathlib import Path
import tempfile
import unittest

from auriga.apps.evaluation import queue
from auriga.apps.evaluation.provenance import CHECKOUT_LAYOUTS, checkout_environment, checkout_layout
from auriga.apps.evaluation.retry import recovered_command


class LegacyNamesTest(unittest.TestCase):
    def test_frozen_checkouts_receive_the_queue_settings_under_their_own_prefix(self):
        environment = {"AURIGA_GRASP_MEMORY_DIR": "/memory/case", "AURIGA_PRICE_CARD": "/cards/a.json",
                       "DAEDALUS_GRASP_MEMORY_DIR": "/memory/stale", "PATH": "/bin"}
        with tempfile.TemporaryDirectory() as directory:
            for gateway, prefix in (("auriga/apps/gateway/cli.py", None), ("daedalus/apps/gateway/cli.py", "DAEDALUS_"),
                                    ("daedalus/gateway.py", "DAEDALUS_")):
                with self.subTest(gateway=gateway):
                    checkout = Path(directory) / gateway.replace("/", "-")
                    (checkout / gateway).parent.mkdir(parents=True)
                    (checkout / gateway).write_text("")
                    launched = checkout_environment(environment, checkout_layout(checkout))
                    if prefix is None:
                        self.assertEqual(launched, environment)
                    else:
                        self.assertEqual(launched["DAEDALUS_GRASP_MEMORY_DIR"], "/memory/case")
                        self.assertEqual(launched["DAEDALUS_PRICE_CARD"], "/cards/a.json")
                        self.assertEqual(launched["AURIGA_GRASP_MEMORY_DIR"], "/memory/case")

    def test_manifest_config_under_the_legacy_key_is_used(self):
        config = queue.Config.from_dict(queue.Config().to_dict()).to_dict()
        manifest = queue.current_manifest({"cases": [], "daedalus_config": config})
        self.assertEqual(manifest, {"cases": [], "auriga_config": config})
        self.assertEqual(queue.queue_config({"daedalus_config": config}).to_dict(), config)
        current = {"auriga_config": config, "daedalus_config": {}}
        self.assertIs(queue.current_manifest(current), current)

    def test_oom_recovery_replays_commands_of_every_checkout_layout(self):
        for layout in CHECKOUT_LAYOUTS:
            with self.subTest(module=layout["module"]):
                entry = {"case": {"run_dir": "/runs/retry"},
                         "retry_command_template": ["/usr/bin/python", "-u", "-m", layout["module"], "--benchmark",
                                                    "--run-dir", "/runs/original", "--gpu", "1"]}
                self.assertEqual(recovered_command(entry, 3)[-4:], ["--run-dir", "/runs/retry", "--gpu", "3"])
        entry["retry_command_template"][3] = "other.gateway"
        with self.assertRaisesRegex(ValueError, "fixed-budget benchmark gateway command"):
            recovered_command(entry, 3)


if __name__ == "__main__":
    unittest.main()
