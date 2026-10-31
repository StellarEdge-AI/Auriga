"""Check native cloth registration and sampling contracts without starting Isaac."""

import ast
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest

from auriga.core.config import Config
from unittest.mock import patch

import numpy as np


class ArrayView:
    def __init__(self, values): self.values = np.asarray(values)
    def detach(self): return self
    def cpu(self): return self
    def numpy(self): return self.values
    def squeeze(self, axis): return ArrayView(self.values.squeeze(axis))


class NativeGarmentRuntimeCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(Config.load().backend.root)
        source = root / "env/scene_manager/objects/garment.py"
        if not source.is_file():
            raise unittest.SkipTest("installed native garment source required; no simulator is launched")
        tree = ast.parse(source.read_text())
        methods = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                   and node.name in ("_get_initial_info", "sample_mesh_vertices")]
        cls.namespace = dict(np=np)
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), cls.namespace)

    def test_cuda_cloth_registers_before_binding_and_reads_live_particles(self):
        events = []
        particle_positions = ArrayView([[[1., 2., 3.], [4., 5., 6.]]])
        sim_view = object()
        def initialize(view):
            self.assertEqual(events, ["register"])
            self.assertIs(view, sim_view)
            events.append("bind")
        def positions():
            self.assertEqual(events, ["register", "bind"])
            events.append("read")
            return particle_positions
        omni, physx = ModuleType("omni"), ModuleType("omni.physx")
        omni.physx = physx
        physx.get_physx_interface = lambda: SimpleNamespace(
            force_load_physics_from_usd=lambda: events.append("register"))
        obj = SimpleNamespace(_device="cuda:0", _cloth_prim_view=SimpleNamespace(
            initialize=initialize, get_world_positions=positions))
        manager = SimpleNamespace(get_physics_sim_view=lambda: sim_view)
        with patch.dict(self.namespace, SimulationManager=manager), patch.dict(
                "sys.modules", {"omni": omni, "omni.physx": physx}):
            self.namespace["_get_initial_info"](obj)
        self.assertEqual(events, ["register", "bind", "read"])
        self.assertIs(obj.initial_points_positions, particle_positions)

    def test_cuda_sampling_returns_native_pose_and_unmodified_live_vertices(self):
        positions = ArrayView([[[1., 2., 3.], [4., 5., 6.]]])
        position, orientation = ArrayView([.2, .3, .4]), ArrayView([.5, .5, .5, .5])
        obj = SimpleNamespace(_device="cuda:0", get_world_pose=lambda: (position, orientation),
            _cloth_prim_view=SimpleNamespace(get_world_positions=lambda: positions))
        world, mesh, pos, quat = self.namespace["sample_mesh_vertices"](obj)
        np.testing.assert_array_equal(world, positions.values[0])
        self.assertIs(mesh, world)
        self.assertIs(pos, position)
        self.assertIs(quat, orientation)


if __name__ == "__main__":
    unittest.main()
