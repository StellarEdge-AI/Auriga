"""The actor sees published Harness source, never an entire checkout."""
from pathlib import Path
import tempfile
import unittest

from auriga.cortex.session.actor_source import SOURCE_FILES, snapshot_actor_source


class ActorSourceTest(unittest.TestCase):
    def test_snapshot_is_exact_and_rejects_unpublished_files_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "snapshot"
            snapshot_actor_source(destination)
            self.assertEqual({path.relative_to(destination).as_posix() for path in destination.rglob("*")
                              if path.is_file()}, {"auriga/" + name for name in SOURCE_FILES})
            path = destination / "auriga" / "robot/backends/robodojo/robot_worker.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("unpublished backend")
            with self.assertRaisesRegex(ValueError, "unpublished_files"):
                snapshot_actor_source(destination)
            path.unlink()
            path = destination / "auriga" / "runtime/harness.py"
            path.unlink()
            target = Path(directory) / "host-file"
            target.write_text("host sentinel")
            path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "unpublished_files"):
                snapshot_actor_source(destination)
            self.assertEqual(target.read_text(), "host sentinel")
