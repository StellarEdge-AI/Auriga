"""Exercise upstream pose arithmetic with real Torch, without launching Isaac.

Set AURIGA_NATIVE_TEST_CUDA=1 and CUDA_VISIBLE_DEVICES to include CUDA checks.
Only the native get_state methods are compiled, avoiding Isaac's import startup.
"""

import json
import os
from pathlib import Path
import subprocess
import unittest

from auriga.core.config import Config


class NativePoseDeviceCheck(unittest.TestCase):
    def test_relative_geometry_and_garment_poses_preserve_values_across_devices(self):
        config = Config.load()
        root = Path(config.backend.root)
        python = Path(config.backend.python)
        if not (root / "env/scene_manager/objects/geometry.py").is_file() or not python.is_file():
            self.skipTest("installed RoboDojo source and Python required; no simulator is launched")
        code = r'''
import ast
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import numpy as np
import torch

root = Path(sys.argv[1])
devices = ["cpu"]
if os.environ.get("AURIGA_NATIVE_TEST_CUDA") == "1":
    assert torch.cuda.is_available(), "CUDA checks requested but CUDA is unavailable"
    devices.append("cuda:0")

# Geometry reads its pose through USD. Stub that read, leaving every native
# tensor conversion and relative-coordinate operation unchanged.
omni = ModuleType("omni")
usd = ModuleType("omni.usd")
omni.usd = usd
sys.modules["omni"] = omni
sys.modules["omni.usd"] = usd
world = np.array([1.5, 2.5, 3.5], dtype=np.float32)
quat = np.array([1., 0., 0., 0.], dtype=np.float32)
usd.get_world_transform_matrix = lambda prim: SimpleNamespace(
    ExtractTranslation=lambda: world,
    ExtractRotationQuat=lambda: SimpleNamespace(
        GetReal=lambda: quat[0], GetImaginary=lambda: quat[1:]))
records = []
for kind in ("geometry", "garment"):
    path = root / "env/scene_manager/objects" / (kind + ".py")
    tree = ast.parse(path.read_text())
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "get_state")
    namespace = dict(torch=torch, np=np)
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    get_state = namespace["get_state"]
    for device in devices:
        for origin_kind in ("numpy", "list", "cpu_tensor", "scalar", "short_tensor") + tuple(devices):
            base = np.array([.25, .5, .75], dtype=np.float32)
            if origin_kind == "numpy": origin = base.copy()
            elif origin_kind == "list": origin = base.tolist()
            elif origin_kind == "scalar": origin = torch.tensor(.25, device="cpu")
            elif origin_kind == "short_tensor": origin = torch.tensor([.25, .5], device="cpu")
            else: origin = torch.tensor(base, device="cpu" if origin_kind == "cpu_tensor" else origin_kind)
            original = origin.clone() if isinstance(origin, torch.Tensor) else np.array(origin).copy()
            obj = SimpleNamespace(prim=object(), env_origin=origin,
                get_world_pose=lambda: (torch.tensor(world, device=device), torch.tensor(quat, device=device)))
            with torch.device(device):
                actual = get_state(obj, is_relative=True)["root_pose"]
                absolute = get_state(obj, is_relative=False)["root_pose"]
            offset = np.asarray(original.cpu() if isinstance(original, torch.Tensor) else original).reshape(-1)
            offset = np.pad(offset, (0, 3 - len(offset)))
            expected = np.concatenate((world - offset, quat))
            np.testing.assert_allclose(actual.cpu().numpy(), expected)
            np.testing.assert_allclose(absolute.cpu().numpy(), np.concatenate((world, quat)))
            assert str(actual.device) == device, (kind, device, actual.device)
            if isinstance(origin, torch.Tensor): torch.testing.assert_close(origin, original)
            else: np.testing.assert_array_equal(origin, original)
            records.append([kind, device, origin_kind])
assert not any(name == "isaacsim" or name.startswith("isaacsim.") for name in sys.modules)
print(json.dumps(records))
'''
        result = subprocess.run([str(python), "-c", code, str(root)], text=True,
            capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = json.loads(result.stdout)
        self.assertEqual({row[0] for row in records}, {"geometry", "garment"})
        expected_devices = {"cpu", "cuda:0"} if os.environ.get("AURIGA_NATIVE_TEST_CUDA") == "1" else {"cpu"}
        self.assertEqual({row[1] for row in records}, expected_devices)


if __name__ == "__main__":
    unittest.main()
