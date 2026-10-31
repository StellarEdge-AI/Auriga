"""Public scene schemas describe accepted requests and actual versioned replies."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from auriga.perception.scene.scene_contracts import (SCHEMAS, GET_CONTRACTS, POST_CONTRACTS,
    GEOMETRY_KINDS, openapi, validate_request, validate_response)
from auriga.core.tool_errors import ToolInputError
from geometry_fixture import IDENTITY


class SceneContractsCheck(unittest.TestCase):
    def test_all_routes_publish_real_schemas_and_resolvable_references(self):
        document = openapi()
        for name, schema in SCHEMAS.items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)
        def check_refs(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    self.assertTrue(value["$ref"].startswith("#/components/schemas/"))
                    self.assertIn(value["$ref"].split("/")[-1], document["components"]["schemas"])
                for part in value.values(): check_refs(part)
            elif isinstance(value, list):
                for part in value: check_refs(part)
        check_refs(document)
        for route in GET_CONTRACTS:
            self.assertIn("content", document["paths"]["/v1/" + route]["get"]["responses"]["200"])
        for route in POST_CONTRACTS:
            operation = document["paths"]["/v1/" + route]["post"]
            self.assertIn("schema", operation["requestBody"]["content"]["application/json"])
        self.assertIn("scene_version", SCHEMAS["SceneQuery"]["oneOf"][0]["required"])
        self.assertIn("Snapshot", document["components"]["schemas"])

    def test_actual_geometry_fields_and_mutually_exclusive_requests(self):
        base = dict(IDENTITY, camera_id="head", observation_id="obs")
        values = dict(fk={}, camera_pose={}, pixel_ray=dict(pixel=[2, 3]),
            pixel_plane=dict(pixel=[2, 3], plane_origin_m=[0, 0, 1], plane_normal=[0, 0, 1],
                             plane_origin_alt_m=[0, 0, 2]),
            ray_surface=dict(pixel=[2, 3], scene_version=1, entity_id="table"),
            project_points=dict(points_m=[[0, 0, 1]]), project_geometry=dict(scene_version=1),
            preview_ee=dict(action=dict(frame_id="env_origin", mode="absolute", path=dict(
                waypoints=[dict(left=dict(position_m=[0, 0, 1], quaternion_xyzw=[0, 0, 0, 1]))]))),
            sparse_track={}, triangulate={}, build_scene=dict(base_scene_version=0),
            alignment=dict(scene_version=1))
        self.assertEqual(set(values) | {"help"}, set(GEOMETRY_KINDS))
        for kind, fields in values.items():
            with self.subTest(kind=kind):
                validate_request("geometry/query", dict(base, kind=kind, **fields))
        validate_request("geometry/query", dict(IDENTITY, kind="help"))
        for changes in (dict(pixel=[True, 3]), dict(pixel=[float("nan"), 2]),
                        dict(pixel=[2, 3], typo=True), dict(pixel=[2, 3], observation_ids=["obs"])):
            with self.subTest(changes=changes), self.assertRaises(ToolInputError):
                validate_request("geometry/query", dict(base, kind="pixel_ray", **changes))
        with self.assertRaises(ToolInputError) as error:
            validate_request("scene/query", dict(IDENTITY, kind="distance", entity_id="box", point_m=[0, 0, 0]))
        self.assertEqual(error.exception.field_path, "/scene_version")

    def test_fixture_requests_and_historical_response_extensions(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / "contracts/fixtures/scene_plane_box.json").read_text())
        for observation in fixture["observations"]:
            validate_request("observations", observation)
        for patch in fixture["patches"]:
            validate_request("scene/patches", patch)
        for query in fixture["queries"]:
            validate_request("scene/query", query)
        # Existing snapshot/cancel callers omit schema_version; published schema
        # explicitly permits that omission rather than inventing a new client rule.
        validate_request("snapshots", dict(IDENTITY, scene_version=1, anchor_observation_id="obs"))
        validate_request("jobs/{identifier}/cancel", dict(IDENTITY, reason="test"))
        head = dict(IDENTITY, scene_version=0, world_kind="internal", future_evidence={"exact": .123456789})
        original = deepcopy(head)
        validate_response("scene/head", head)
        self.assertEqual(head, original)


if __name__ == "__main__":
    unittest.main()
