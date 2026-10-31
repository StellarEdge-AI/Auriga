"""Robot packages preserve physical distinctions without research metadata."""

from copy import deepcopy
from collections import Counter
import io
import json
import re
import struct
import unittest
import xml.etree.ElementTree as ET
import zipfile

from PIL import Image

from auriga.robot.model.robot_information import (
    ROBOT_INFORMATION_REGISTRY,
    ROBOT_INFORMATION_SECTIONS,
    robot_geometry_path,
    robot_information,
    robot_information_text,
    robot_resource_path,
    robot_resources,
)


def objects(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)


class RobotInformationTests(unittest.TestCase):
    def test_full_reference_retains_complete_numerical_body(self):
        result = robot_information(section="all")
        self.assertTrue(result["available"])
        self.assertEqual(result["robot_id"], "arx-x5")
        self.assertGreater(len(result["briefing"]), 6000)
        for phrase in ("modified", "current RoboDojo episode calibration", "0.1501", "0.1226", "0.1576",
                       "qcmd7=qcmd8", "0.807 mm", "88.807 mm", "25 Hz", "0.05 rad",
                       "1.25 rad/s", "retained grasp", "measured state"):
            self.assertIn(phrase, result["briefing"])
        ref = result["reference"]
        self.assertEqual(set(ref), {"identity", "kinematics", "geometry", "dynamics",
                                    "control", "limitations", "hardware"})
        self.assertEqual(ref["identity"]["native_action_dimensions"], 14)
        joints = {j["name"]: j for j in ref["kinematics"]["joints"]}
        self.assertEqual(len(joints), 10)
        self.assertEqual(sum(j["type"] == "revolute" for j in joints.values()), 6)
        self.assertEqual(sum(j["type"] == "prismatic" for j in joints.values()), 2)
        self.assertEqual(joints["joint3"]["origin"],
                         {"xyz_m": [-.264, 0., 0.], "rpy_rad": [3.1416, 0., 0.]})
        self.assertEqual(joints["joint4"]["origin"]["xyz_m"], [.245, 0., -.056])
        self.assertEqual(joints["joint6"]["declared_limits"]["lower"], -3.14)
        self.assertEqual(joints["joint8"]["axis"], [0., -1., 0.])
        self.assertEqual(ref["kinematics"]["pose_interface"]["grasp_contact_offsets_m"],
                         {"pad_center": .1501, "finger_middle": .1226, "fingertip": .1576})

        dynamics = ref["dynamics"]
        links = {link["name"]: link["inertial"] for link in dynamics["declared_links"]}
        self.assertEqual(len(links), 11)
        self.assertIsNone(links["camera"])
        self.assertIsNone(links["camera_base"])
        self.assertAlmostEqual(sum(link["mass_kg"] for link in links.values() if link), 3.454278)
        self.assertEqual(links["link3"]["inertia_kg_m2"],
                         dict(ixx=.00036, ixy=-.00003, ixz=-.00039,
                              iyy=.00423, iyz=.00001, izz=.0042))
        for link in links.values():
            if link is not None:
                self.assertEqual(set(link["inertia_kg_m2"]), {"ixx", "ixy", "ixz", "iyy", "iyz", "izz"})
                self.assertEqual(len(link["origin"]["xyz_m"]), 3)
        self.assertEqual(len(dynamics["authored_rigid_bodies"]), 11)
        self.assertEqual(len(dynamics["authored_joints"]), 10)
        self.assertEqual(len(dynamics["authored_rigid_bodies"]["camera"]["diagonal_inertia_kg_m2"]), 3)

    def test_geometry_retains_mesh_bounds_gap_and_all_planner_spheres(self):
        geometry = robot_information(section="geometry")["reference"]["geometry"]
        links = {link["name"]: link for link in geometry["links"]}
        self.assertEqual(len(links), 11)
        self.assertAlmostEqual(links["link2"]["visual_and_collision"][0]["mesh_local_min_m"][0], -.292)
        gap = geometry["distal_flat_pad_gap"]
        self.assertAlmostEqual(gap["gap_q7_q8_zero_m"], .000807165976524353)
        self.assertAlmostEqual(gap["gap_q7_q8_0_044_m"], .08880716597652435)
        self.assertIn("actual physical joint positions", gap["q7_q8_units"])
        self.assertEqual(geometry["collision_approximations"]["link3"]["approximation"], "convexDecomposition")
        self.assertTrue(geometry["collision_approximations"]["camera"]["enabled"])
        spheres = geometry["planning_collision"]["collision_spheres"]
        self.assertEqual({k: len(v) for k, v in spheres.items()},
                         dict(base_link=3, camera=3, camera_base=6, link1=13,
                              link2=12, link3=13, link4=4, link5=3, link6=8))
        self.assertAlmostEqual(spheres["base_link"][0]["radius"], .043082091957330704)
        self.assertEqual(set(geometry["finger_geometry"]), {"link7", "link8"})

    def test_default_and_authored_settings_are_not_conflated_with_measurements(self):
        ref = robot_information(section="all")["reference"]
        dynamics = ref["dynamics"]
        authored = dynamics["authored_articulation_defaults"]
        configured = dynamics["configured_actuators_and_physics"]
        self.assertFalse(authored["enabled_self_collisions"])
        self.assertTrue(configured["enabled_self_collisions"])
        self.assertEqual(authored["solver_position_iteration_count"], 32)
        self.assertEqual(configured["solver_position_iteration_count"], 8)
        self.assertEqual(configured["arm"]["velocity_limit_sim"], 5.)
        self.assertEqual(configured["arm"]["stiffness"], 4400.)
        self.assertEqual(configured["gripper"]["damping"], 100.)
        self.assertTrue(configured["disable_gravity"])
        control = ref["control"]
        self.assertEqual(control["default_harness"]["physics_dt_s"], .004)
        self.assertEqual(control["default_harness"]["native_control_hz"], 25.)
        self.assertEqual(control["default_harness"]["default_resampled_command_rate_bound_rad_s"], 1.25)
        self.assertEqual(control["gripper"]["native_target_endpoints_m"], [-.01, .044])
        preview = control["preview_gripper_mapping"]
        self.assertNotEqual(preview["preview_command_to_joint_m"], preview["native_command_to_joint_m"])
        self.assertIn("measured", preview["interpretation"])
        self.assertEqual(control["default_spawn_joint_positions"]["joint7"], .044)
        self.assertAlmostEqual(control["planner_joint_space"]["default_joint_position"][-1], .022)

    def test_every_section_is_free_of_provenance_and_file_metadata(self):
        forbidden = re.compile(r"(?:^|_)(?:source|provenance|sha\d+|hash|fingerprint|commit|repository|"
                               r"filename|file_path|prim_path|schema_version|format_version|"
                               r"observed_date|observed_utc|triangle_count|vertex_count|bytes|"
                               r"archived_file|saved_as|url)", re.I)
        for robot_id in ROBOT_INFORMATION_REGISTRY:
            for section in ROBOT_INFORMATION_SECTIONS:
                with self.subTest(robot_id=robot_id, section=section):
                    result = robot_information(robot_id, section)
                    members = {obj["assembly_member"] for obj in objects(result)
                               if isinstance(obj, dict) and "assembly_member" in obj}
                    for obj in objects(result):
                        if isinstance(obj, dict):
                            for key in obj:
                                self.assertIsNone(forbidden.search(key), key)
                        elif isinstance(obj, str):
                            self.assertNotRegex(obj, r"(?i)\b[0-9a-f]{32,}\b|https?://|/home/|/tmp/|/X5A/")
                            if obj not in members and obj != result.get("urdf"):
                                self.assertNotRegex(obj, r"(?i)\b\w+\.(?:py|json|txt|urdf|usd|stl|yml|yaml|pdf|step)\b")
                    self.assertNotRegex(json.dumps(result), r"(?i)\bsha256\b|\bfingerprint\b|\bprovenance\b")

    def test_unknown_robot_and_section_are_explicitly_unavailable(self):
        for robot_id in ("unknown", "x5", "arx-x5-2025", "../arx_x5", "", None, []):
            with self.subTest(robot_id=robot_id):
                result = robot_information(robot_id)
                self.assertFalse(result["available"])
                self.assertEqual(result["error"], "robot_information_unavailable")
                self.assertNotIn("reference", result)
                self.assertNotIn("briefing", result)
                self.assertIsNone(robot_geometry_path(robot_id))
        for section in ("missing", "../physical", None, []):
            with self.subTest(section=section):
                result = robot_information(section=section)
                self.assertFalse(result["available"])
                self.assertNotIn("reference", result)

    def test_partial_sections_all_and_deep_copies(self):
        default = robot_information()
        full = robot_information(section="all")
        for section in ("kinematics", "geometry", "dynamics", "control"):
            partial = robot_information(section=section)
            self.assertEqual(set(partial["reference"]), {section})
            self.assertEqual(partial["reference"][section], full["reference"][section])
            self.assertNotIn("briefing", partial)
        self.assertEqual(full["briefing"], default["briefing"])
        self.assertEqual(full["reference"]["hardware"],
                         robot_information(section="hardware")["reference"]["hardware"])
        self.assertNotIn("hardware", default["reference"])
        baseline = deepcopy(full)
        full["reference"]["kinematics"]["joints"][0]["axis"][0] = 123
        full["reference"]["geometry"]["planning_collision"]["collision_spheres"]["link1"].clear()
        full["reference"]["hardware"]["product_families"].clear()
        self.assertEqual(robot_information(section="all"), baseline)
        self.assertEqual(robot_information(), default)

    def test_default_keeps_briefing_native_geometry_and_one_physical_representation(self):
        default = robot_information()
        full = robot_information(section="all")
        self.assertEqual(default["briefing"], full["briefing"])
        compact = default["reference"]
        detailed = full["reference"]
        self.assertEqual(set(compact), {"identity", "kinematics", "geometry", "control"})
        self.assertEqual(compact["geometry"], detailed["geometry"]["native_model"])
        self.assertEqual(compact["geometry"]["resource_id"], "geometry_model")
        self.assertNotIn("joints", compact["kinematics"])
        with zipfile.ZipFile(robot_resource_path(resource_id="geometry_model")) as archive:
            self.assertEqual(default["urdf"].encode("utf-8"), archive.read("X5A.urdf"))
        self.assertEqual(len(ET.fromstring(default["urdf"]).findall("joint")), 10)
        control = compact["control"]
        self.assertEqual(control["configured_actuators_and_physics"],
                         detailed["dynamics"]["configured_actuators_and_physics"])
        self.assertEqual(control["gripper"]["native_joint_target_m"],
                         detailed["control"]["gripper"]["native_joint_target_m"])
        self.assertEqual(compact["kinematics"]["pose_interface"]["grasp_contact_offsets_m"],
                         dict(pad_center=.1501, finger_middle=.1226, fingertip=.1576))
        # Alternate unit/precision representations and raw numerical inventories
        # must not creep back into the initial JSON. They are available on demand.
        forbidden = {"links", "joint_units", "declared_position_limits", "declared_limits",
                     "axis", "origin", "inertial", "visual_and_collision",
                     "authored_joints", "authored_rigid_bodies", "declared_links", "planning_collision",
                     "native_frame_matrices", "finger_geometry", "pad_center_offset_m", "tool_offset_m",
                     "native_gripper_command", "native_target_endpoints_m", "limit_units", "default_mounts"}
        for obj in objects(compact):
            if isinstance(obj, dict):
                self.assertFalse(set(obj) & forbidden)
        compact["geometry"].clear()
        compact["kinematics"]["pose_interface"]["grasp_contact_offsets_m"]["pad_center"] = 999
        control["gripper"].clear()
        self.assertEqual(robot_information()["reference"]["geometry"], detailed["geometry"]["native_model"])
        self.assertEqual(robot_information()["reference"]["kinematics"]["pose_interface"]["grasp_contact_offsets_m"]["pad_center"], .1501)

    def test_startup_text_delivers_verbatim_urdf_once_without_mutation(self):
        record = robot_information()
        original = deepcopy(record)
        text = robot_information_text(record)
        metadata, urdf = text.removeprefix("Robot physical information\n").split("\n\nRobot URDF\n", 1)
        self.assertEqual(urdf, record["urdf"])
        self.assertEqual(text.count(record["urdf"]), 1)
        self.assertNotIn("urdf", json.loads(metadata))
        self.assertEqual(json.loads(metadata), {k: v for k, v in record.items() if k != "urdf"})
        self.assertEqual(record, original)
        unknown = robot_information("unregistered")
        self.assertNotIn("Robot URDF", robot_information_text(unknown))
        self.assertNotIn("urdf", unknown)

    def test_geometry_illustration_is_packaged_without_metadata(self):
        path = robot_geometry_path()
        self.assertIsNotNone(path)
        self.assertEqual(path.parent.name, "arx_x5")
        with Image.open(path) as illustration:
            self.assertEqual(illustration.format, "PNG")
            self.assertGreaterEqual(illustration.width, 1000)
            self.assertEqual(illustration.info, {})

    def test_optional_component_resources_have_no_model_facing_paths(self):
        catalog = robot_resources()
        self.assertTrue(catalog["available"])
        expected_solids = dict(base_step=1, end_step=3, gripper_1_step=1, gripper_2_step=1)
        self.assertEqual({entry["resource_id"] for entry in catalog["resources"]}, set(expected_solids) | {"geometry_model"})
        for entry in catalog["resources"]:
            self.assertEqual(set(entry), {"resource_id", "name", "format", "description"})
            if entry["resource_id"] == "geometry_model":
                self.assertEqual(entry["format"], "ZIP")
                continue
            self.assertIn("not a complete arm", entry["description"])
            self.assertIn("do not represent the modified simulated fingers", entry["description"])
            path = robot_resource_path(resource_id=entry["resource_id"])
            self.assertIsNotNone(path)
            data = path.read_bytes()
            self.assertTrue(data.startswith(b"ISO-10303-21;"))
            self.assertTrue(data.rstrip().endswith(b"END-ISO-10303-21;"))
            self.assertEqual(data.count(b"MANIFOLD_SOLID_BREP"), expected_solids[entry["resource_id"]])
            # A truncated or damaged transfer must not leave dangling entity references.
            defined = set(re.findall(rb"#(\d+)\s*=", data))
            referenced = set(re.findall(rb"#(\d+)", data))
            self.assertEqual(defined, referenced)
        encoded = json.dumps(catalog)
        self.assertNotRegex(encoded, r"(?i)https?://|/home/|sha256|\b[0-9a-f]{32,}\b|\.STEP")
        catalog["resources"][0]["description"] = "changed"
        self.assertNotEqual(robot_resources(), catalog)
        for robot_id in ("unknown", "x5", None, []):
            self.assertFalse(robot_resources(robot_id)["available"])
            self.assertIsNone(robot_resource_path(robot_id, "base_step"))
        for resource_id in ("unknown", "../../physical", "base_step.STEP", None, []):
            self.assertIsNone(robot_resource_path(resource_id=resource_id))

    def test_cad_documentation_is_removed_without_losing_geometry_or_units(self):
        # Recorded physical entity counts also detect incomplete component transfers.
        kinds = ("CARTESIAN_POINT", "DIRECTION", "EDGE_CURVE", "ADVANCED_FACE",
                 "VERTEX_POINT", "CLOSED_SHELL", "MANIFOLD_SOLID_BREP")
        expected = {
            "base_step": (3446, (597, 646, 289, 102, 188, 1, 1)),
            "end_step": (11789, (2266, 2173, 938, 384, 591, 3, 3)),
            "gripper_1_step": (1573, (268, 271, 130, 51, 86, 1, 1)),
            "gripper_2_step": (1573, (268, 271, 130, 51, 86, 1, 1)),
        }
        documentary = {
            "APPROVAL", "APPROVAL_DATE_TIME", "APPROVAL_PERSON_ORGANIZATION",
            "APPROVAL_ROLE", "APPROVAL_STATUS", "CALENDAR_DATE", "CC_DESIGN_APPROVAL",
            "CC_DESIGN_DATE_AND_TIME_ASSIGNMENT", "CC_DESIGN_PERSON_AND_ORGANIZATION_ASSIGNMENT",
            "CC_DESIGN_SECURITY_CLASSIFICATION", "COORDINATED_UNIVERSAL_TIME_OFFSET",
            "DATE_AND_TIME", "DATE_TIME_ROLE", "LOCAL_TIME", "ORGANIZATION", "PERSON",
            "PERSON_AND_ORGANIZATION", "PERSON_AND_ORGANIZATION_ROLE",
            "SECURITY_CLASSIFICATION", "SECURITY_CLASSIFICATION_LEVEL",
        }
        for resource_id, (total, physical_counts) in expected.items():
            with self.subTest(resource_id=resource_id):
                text = robot_resource_path(resource_id=resource_id).read_text(encoding="ascii")
                self.assertIn("FILE_NAME ('', '', (''), (''), '', '', '');", text)
                self.assertIn("FILE_SCHEMA (( 'CONFIG_CONTROL_DESIGN' ));", text)
                self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}T|https?://|[A-Z]:\\|/home/|/tmp/")
                entities = re.findall(r"(?m)^#\d+\s*=", text)
                counts = Counter(re.findall(r"(?m)^#\d+\s*=\s*([A-Z_0-9]+)", text))
                self.assertEqual(len(entities), total)
                self.assertEqual(tuple(counts[kind] for kind in kinds), physical_counts)
                self.assertFalse(set(counts) & documentary)
                self.assertEqual(text.count("SI_UNIT ( .MILLI., .METRE. )"), 1)
                self.assertEqual(text.count("SI_UNIT ( $, .RADIAN. )"), 1)
                self.assertEqual(text.count("SI_UNIT ( $, .STERADIAN. )"), 1)
                labels = set(re.findall(r"'(?:[^']|'')*'", text))
                allowed = {
                    "''", "'NONE'", "'1'", "'STEP AP203'", "'CONFIG_CONTROL_DESIGN'",
                    "'config_control_design'", "'international standard'", "'WORKASPACE'",
                    "'configuration controlled 3d designs of mechanical parts and assemblies'",
                    "'design'", "'detail'", "'detailed design'", "'distance_accuracy_value'",
                    "'mechanical'", "'solid_1'", "'solid_2'", "'solid_3'",
                    "'" + resource_id.removesuffix("_step") + "'",
                }
                self.assertFalse(labels - allowed, labels - allowed)

    def test_native_geometry_archive_contains_every_assembly_mesh(self):
        path = robot_resource_path(resource_id="geometry_model")
        self.assertIsNotNone(path)
        self.assertLess(path.stat().st_size, 16*1024*1024)
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            urdf = archive.read("X5A.urdf")
            self.assertNotIn(b"<!--", urdf)
            root = ET.fromstring(urdf)
            links = {link.get("name") for link in root.findall("link")}
            self.assertEqual(links, {"base_link", "camera", "camera_base"} | {f"link{i}" for i in range(1, 9)})
            self.assertEqual(len(root.findall("joint")), 10)
            meshes = {node.get("filename") for node in root.findall(".//mesh")}
            self.assertEqual(set(archive.namelist()), meshes | {"X5A.urdf"})
            for member in meshes:
                self.assertTrue(member.startswith("meshes/") and ".." not in member)
                data = archive.read(member)
                if member.endswith(".STL"):
                    self.assertEqual(data[:80], b"\0"*80)
                    count = struct.unpack_from("<I", data, 80)[0]
                    self.assertEqual(len(data), 84 + 50*count)
                    self.assertGreater(count, 0)
                else:
                    magic, version, length, json_size, kind = struct.unpack_from("<5I", data)
                    self.assertEqual((magic, version, length, kind), (0x46546c67, 2, len(data), 0x4e4f534a))
                    model = json.loads(data[20:20+json_size])
                    self.assertNotIn("generator", model["asset"])
                    self.assertNotIn("copyright", model["asset"])
                    self.assertFalse(any("uri" in buffer for buffer in model["buffers"]))
            joints = {joint.get("name"): joint for joint in root.findall("joint")}
            self.assertEqual(float(joints["joint3"].find("origin").get("xyz").split()[0]), -.264)
            self.assertEqual(float(joints["joint7"].find("limit").get("upper")), .044)


if __name__ == "__main__":
    unittest.main()
