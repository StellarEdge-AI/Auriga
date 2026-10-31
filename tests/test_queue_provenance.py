"""No GPU: immutable source bindings cover changed, added and removed modules."""
from pathlib import Path
import tempfile
import unittest

from auriga.apps.evaluation.provenance import bind_sources, checkout_layout, verify_source


class SourceBindingTests(unittest.TestCase):
    def test_launch_binding_detects_runtime_changes_but_ignores_generated_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "auriga"
            (package / "apps/gateway").mkdir(parents=True)
            gateway = package / "apps/gateway/cli.py"
            gateway.write_text("original = True\n")
            binding = bind_sources([{"cwd": directory}])
            expected = binding[directory]
            cache = package / "__pycache__"
            cache.mkdir()
            (cache / "cli.pyc").write_bytes(b"cache")
            self.assertEqual(verify_source(root, expected), expected["sha256"])
            for change in ("edit", "add", "remove"):
                with self.subTest(change=change):
                    extra = package / "extra.py"
                    if change == "edit":
                        gateway.write_text("original = False\n")
                    elif change == "add":
                        extra.write_text("new = True\n")
                    else:
                        gateway.unlink()
                    with self.assertRaisesRegex(ValueError, "queue_source_changed|gateway_missing"):
                        bind_sources([{"cwd": directory}], binding)
                    gateway.write_text("original = True\n")
                    extra.unlink(missing_ok=True)

    def test_symlink_and_missing_binding_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "auriga/apps/gateway").mkdir(parents=True)
            (root / "outside.py").write_text("pass\n")
            (root / "auriga/apps/gateway/cli.py").symlink_to(root / "outside.py")
            with self.assertRaisesRegex(ValueError, "symlink_denied"):
                bind_sources([{"cwd": directory}])
            with self.assertRaisesRegex(ValueError, "binding_missing"):
                bind_sources([{"cwd": directory}], {})

    def test_package_and_frozen_checkout_layouts_are_both_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            for gateway, module in (("auriga/apps/gateway/cli.py", "auriga.apps.gateway"),
                                    ("daedalus/apps/gateway/cli.py", "daedalus.apps.gateway"),
                                    ("daedalus/gateway.py", "daedalus.gateway")):
                with self.subTest(module=module):
                    checkout = Path(directory).resolve() / module
                    (checkout / gateway).parent.mkdir(parents=True)
                    (checkout / gateway).write_text("pass\n")
                    self.assertEqual(checkout_layout(checkout)["module"], module)
                    self.assertIn(gateway, bind_sources([{"cwd": str(checkout)}])[str(checkout)]["files"])
            self.assertIsNone(checkout_layout(directory))
