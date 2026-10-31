"""Export only permitted robot joint geometry from an explicitly named URDF.

No mesh, material, task state or external URI is opened. Output can be an actor artifact.
"""
import hashlib
import math
from pathlib import Path
import xml.etree.ElementTree as ET


def urdf_manifest(path):
    data = Path(path).read_bytes()
    root = ET.fromstring(data)
    joints = []
    for node in root.findall("joint"):
        origin = node.find("origin")
        xyz = [float(x) for x in origin.get("xyz","0 0 0").split()] if origin is not None else [0.,0.,0.]
        r,p,y = [float(x) for x in origin.get("rpy","0 0 0").split()] if origin is not None else [0.,0.,0.]
        cr,sr,cp,sp,cy,sy = math.cos(r),math.sin(r),math.cos(p),math.sin(p),math.cos(y),math.sin(y)
        matrix = [[cy*cp,cy*sp*sr-sy*cr,cy*sp*cr+sy*sr,xyz[0]], [sy*cp,sy*sp*sr+cy*cr,sy*sp*cr-cy*sr,xyz[1]],[-sp,cp*sr,cp*cr,xyz[2]],[0.,0.,0.,1.]]
        axis = node.find("axis")
        joint = {"name":node.attrib["name"],"type":node.attrib["type"],"parent":node.find("parent").attrib["link"],"child":node.find("child").attrib["link"],"T_parent_joint":matrix,"axis":[float(x) for x in axis.get("xyz","1 0 0").split()] if axis is not None else [1.,0.,0.]}
        limit = node.find("limit")
        if limit is not None and "lower" in limit.attrib and "upper" in limit.attrib:
            lower, upper = float(limit.attrib["lower"]), float(limit.attrib["upper"])
            if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
                raise ValueError("robot_model_invalid_joint_limits")
            joint["limits"] = {"lower": lower, "upper": upper}
        mimic = node.find("mimic")
        if mimic is not None: joint["mimic"] = {"joint":mimic.attrib["joint"],"multiplier":float(mimic.get("multiplier",1)),"offset":float(mimic.get("offset",0))}
        joints.append(joint)
    children = {joint["child"] for joint in joints}
    roots = {joint["parent"] for joint in joints}-children
    if len(roots)!=1: raise ValueError("robot_model_requires_single_root")
    base = next(iter(roots))
    ordered, resolved = [], {base}
    while joints:
        ready = [joint for joint in joints if joint["parent"] in resolved]
        if not ready: raise ValueError("robot_model_cycle_or_disconnected_chain")
        ordered.extend(ready)
        for joint in ready: resolved.add(joint["child"]); joints.remove(joint)
    return {"schema_version":"0.1","robot_name":root.get("name"),"base_link":base,"joints":ordered,"source_kind":"permitted_robot_urdf","source_sha256":hashlib.sha256(data).hexdigest(),"source_filename":Path(path).name,"geometry_scope":"joint_frames_and_declared_limits; no task assets or mesh references"}


_COLLISION_MESH_CACHE = {}


def collision_mesh_bounds(path, links=("link7", "link8"), *, include_vertices=False):
    """Local conservative bounds for named robot STL collision meshes only.

    This exports hardware geometry, never simulator task assets. Bounds include
    the mesh scale and collision origin and conservatively enclose the mesh.
    """
    from copy import deepcopy
    import struct
    import numpy as np
    path = Path(path).resolve()
    raw_urdf = path.read_bytes()
    cache_key = (str(path), hashlib.sha256(raw_urdf).hexdigest(), tuple(links), bool(include_vertices))
    if cache_key in _COLLISION_MESH_CACHE:
        return deepcopy(_COLLISION_MESH_CACHE[cache_key])
    root = ET.fromstring(raw_urdf)
    result = {}
    for node in root.findall("link"):
        if node.get("name") not in links:
            continue
        points, hashes = [], []
        for collision in node.findall("collision"):
            mesh = collision.find("geometry/mesh")
            if mesh is None:
                raise ValueError("unsupported_robot_collision_geometry")
            source = (path.parent / mesh.attrib["filename"]).resolve()
            if not source.is_relative_to(path.parent) or source.suffix.lower() != '.stl':
                raise ValueError("robot_collision_mesh_outside_asset_root")
            data = source.read_bytes()
            count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
            if len(data) != 84 + count * 50 or count == 0:
                raise ValueError("robot_collision_mesh_requires_binary_stl")
            vertices = np.array([struct.unpack_from("<9f", data, 84 + i * 50 + 12)
                                 for i in range(count)]).reshape(-1, 3)
            vertices *= np.array([float(v) for v in mesh.get("scale", "1 1 1").split()])
            origin = collision.find("origin")
            if origin is not None:
                r, p, y = [float(v) for v in origin.get("rpy", "0 0 0").split()]
                cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
                rotation = np.array([[cy*cp,cy*sp*sr-sy*cr,cy*sp*cr+sy*sr],
                    [sy*cp,sy*sp*sr+cy*cr,sy*sp*cr-cy*sr],[-sp,cp*sr,cp*cr]])
                vertices = vertices @ rotation.T + np.array([float(v) for v in origin.get("xyz", "0 0 0").split()])
            if not np.isfinite(vertices).all():
                raise ValueError("nonfinite_robot_collision_mesh")
            points.extend(vertices.tolist())
            hashes.append(hashlib.sha256(data).hexdigest())
        if points:
            values = np.array(points)
            result[node.attrib["name"]] = {"min_m": values.min(axis=0).tolist(),
                "max_m": values.max(axis=0).tolist(), "mesh_sha256": hashes,
                "source_kind": "robot_collision_stl_bounds"}
            if include_vertices:
                from scipy.spatial import ConvexHull, QhullError
                values = np.unique(values, axis=0)
                try:
                    values = values[ConvexHull(values).vertices]
                except QhullError:
                    pass  # Keep every vertex for planar/degenerate public meshes.
                result[node.attrib['name']]['convex_vertices_m'] = values.tolist()
    if set(result) != set(links):
        raise ValueError("missing_robot_finger_collision_mesh")
    _COLLISION_MESH_CACHE[cache_key] = deepcopy(result)
    return result
