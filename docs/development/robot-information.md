# Unified Robot Information Interface

By default, `robot_information` returns the active robot's **complete physical briefing, verbatim URDF text, supplementary parameter JSON, directly readable native geometry file references, and camera/base calibration from the latest published observation in the current episode**. The JSON payload no longer expands verbose prose descriptions of link shapes or sends static diagram images by default. Instead, the raw URDF is delivered directly to the model, and full surface geometry is accessible through the mesh files it references.

Reading robot information never triggers a new sensor observation, moves the robot, advances simulation time, or makes network requests. The currently registered model ID is `arx-x5`; querying an unknown robot ID explicitly returns unavailable rather than falling back to X5. Static documentation availability and `calibration.available` are evaluated independently.

## Launch Injection and Prompt Ordering

Once the robot backend, runtime capabilities, and initial observation are established at startup, the gateway invokes the same default reader used by the `robot_information` tool. `developerInstructions` begins with the full physical briefing, the supplementary parameter JSON (including geometry file references and current-episode calibration), and a standalone verbatim URDF block, followed by the standard Harness instructions; the user task prompt is sent next, with Task Notes appended at the end of the task body (`task_notes_source` is archived only and never sent to the model).

The URDF is extracted verbatim from the registered ZIP archive's assembly entry point—preserving exact numeric literals, whitespace, and line breaks—and placed once into its own text block at launch rather than being JSON-escaped and duplicated inside the JSON payload. When the `robot_information` tool is called with default arguments, its `urdf` field returns that exact same string for programmatic parsing.

Binary ZIP contents are never base64-encoded into the prompt; instead, `reference.geometry.resource_ref` provides an artifact handle that the model can read directly via `client.artifact` inside `program_run`. Launch injection adds no extra model turns and fabricates no synthetic tool calls. Resuming a session with a compatible tool catalog re-injects the robot information without re-sending the initial user prompt; if an older thread fails with `resume_tool_registry_conflict` due to catalog changes, start a fresh run. Replay mode remains strictly read-only.

## Default Payload and On-Demand Sections

Because kinematic chains, fixed transforms, local joint axes, nominal limits, per-link inertias, and mesh assembly origins are already defined in the verbatim URDF, the compact `reference` JSON does not duplicate them. Instead, `reference` supplies end-effector interface conventions, contact-reference offsets, gripper command mappings, effective controller parameters, and mesh file references. Raw USD properties, collision spheres, and other detailed tables remain queryable on demand by section, while the full briefing explains motion semantics and physical uncertainties.

| `section` | Returned Content |
| --- | --- |
| `"default"` or omitted | Full `briefing`, verbatim `urdf`, supplementary `reference` parameters, native geometry file reference, and current `calibration` |
| `"calibration"` | Re-reads camera intrinsics/extrinsics, hand-eye transforms, and base transforms from the latest published observation |
| `"kinematics"` | Complete joint and kinematic tables |
| `"geometry"` | Complete geometry records and native model resource descriptors |
| `"dynamics"` | Link masses, centers of mass, inertias, and applicability boundaries |
| `"control"` | Complete controller gains, simulation settings, action clock, and command semantics |
| `"hardware"` | Optional hardware comparisons, conflicting datasheet specs, and unconfirmed items |
| `"all"` | Full briefing plus all detailed static physical sections (read live episode calibration via `"default"` or `"calibration"`) |
| `"resources"` | Manifest of registered file resources with `resource_id`, format, and description |
| `"resource"` | Artifact reference for the specified `resource_id` |
| `"geometry_image"` | Legacy static diagram image, delivered only on demand |

Omitting `robot_id` queries the active run's `robot_model_id`; passing an explicit `robot_id` queries that model's static documentation without switching the active runtime robot, and never attaches the current episode's live calibration to a different `robot_id`. Requesting a non-existent resource returns unavailable, and `resource_id` is valid only when `section="resource"`.

## Native Geometry Files

For ARX X5, the `geometry_model` resource is `geometry_model.zip`, containing `X5A.urdf`, 9 binary STL meshes, and 2 GLB assets. Native geometry for the base, all six arm links, both fingers, camera brackets, and camera housings is preserved intact along with relative directory paths, URDF origins/scales, joint transforms, and GLB node transforms. Asset cleanup strips only non-geometric document metadata without altering vertices, triangle faces, or assembly transforms. Both arms share this structural geometry, while their respective base poses in the world frame come from live episode calibration.

Inside `program_run`, a script can read the ZIP archive directly from the default tool reply:

```python
import io, json, struct, zipfile
import xml.etree.ElementTree as ET
from auriga_client import AurigaClient

client = AurigaClient()
reply = client.call("robot_information")
info = json.loads(next(i["text"] for i in reply["contentItems"] if i["type"] == "inputText"))
model = info["reference"]["geometry"]
with zipfile.ZipFile(io.BytesIO(client.artifact(model["resource_ref"]))) as archive:
    assembly = ET.fromstring(info["urdf"])
    stl = next(n.get("filename") for n in assembly.findall(".//mesh")
               if n.get("filename", "").lower().endswith(".stl"))
    triangle_count = struct.unpack_from("<I", archive.read(stl), 80)[0]
    print({"links": len(assembly.findall("link")), "first_stl_triangles": triangle_count})
```

This snippet illustrates parsing the URDF and reading a binary STL header; reconstructing the full 3D assembly requires applying the URDF and GLB node transforms while respecting each format's unit and axis conventions. Note that visual/nominal meshes do not equal PhysX's runtime convex-hull decomposition and do not certify continuous-time clearance or physical contact.

Optional individual CAD parts remain accessible via `base_step`, `end_step`, `gripper_1_step`, and `gripper_2_step`. Those STEP files represent individual components rather than a full-arm assembly, and nominal vendor finger CAD does not replace the modified fingers present in the native simulation meshes above.

## Episode Camera and Base Calibration

The default `calibration` block reads raw values from the permitted artifacts of the latest published observation, returning `available`, `observation_id`, `sim_step`, coordinate frame and unit conventions, per-camera intrinsics `K`, distortion coefficients, hand-eye transform `T_link_camera`, current-frame `T_world_camera` / `T_world_link`, and per-arm `T_world_base`. Before returning, the reader verifies run, episode, environment, and generation identities as well as step-counter, clock-domain, and camera-frame consistency, translating internal IDs into reversible short observation references and stripping internal run provenance. Each camera returns only the fields that actually apply to it (e.g., fixed vs. wrist-mounted), and missing base transforms are explicitly marked unavailable rather than backfilled from static example mounts; neither the default briefing nor the compact JSON embeds hardcoded installation coordinates.

These values reflect the simulator's active configuration and sampled/derived poses for the current episode rather than empirical real-hardware calibration. Wrist camera world poses for the current frame are composed from that frame's measured link transform and the episode's static hand-eye mount, and the interface passes the published values through directly. Calibration is readable under the default calibrated observation profile (`rgb_proprio_calibrated_v1`); under the legacy uncalibrated profile (`rgb_proprio_v1`), or when data is missing or inconsistent, `available=false` is returned explicitly. Calling `section="calibration"` reads the latest already-published frame without triggering sensor capture, so callers must check the returned `observation_id` and `sim_step` to judge freshness.

This interface never exposes hidden task object states, contact ground truth, or evaluator scores. `robot_capabilities`, live proprioceptive measurements, and active tool contracts remain authoritative, and target poses, planned trajectories, dispatched commands, measured states, and physical object outcomes must always be evaluated separately.

## Output Boundaries and Registering a New Robot

For the complete file checklist, environment/robot decoupling, combination validation, control adaptation, and acceptance testing workflow, see the [Robot Arm and Environment Integration Guide](robot-environment-integration.md). Registering a data package here provides static documentation only and does not imply backend execution support for that robot model.

Model-facing outputs strip SHA/hash/digest fields, document provenance, and repository metadata while preserving full numerical precision, measured/commanded/derived provenance tags, coordinate frames, and short observation/action IDs. Long artifact URIs are mapped to reversible `artifact:ref_N` short references that are resolved transparently when passed back in tool arguments or program source code (unknown aliases are rejected). Raw `Store` records and internal integrity checks remain untouched, and user free text is never rewritten by broad regex scrubbing.

To register static documentation for a new robot, add its `robot_model_id` and `robot_information_data/<directory_name>/` mapping to `ROBOT_INFORMATION_REGISTRY` in [`robot_information.py`](../../auriga/robot/model/robot_information.py), supplying `briefing.txt`, `physical.json`, and the native geometry ZIP archive. Declare the resource ID, assembly entry point, mesh formats, and coordinate conventions under `geometry.native_model` in `physical.json`, and register the ZIP in `resources.json` while preserving relative mesh paths inside the assembly. The default URDF reader extracts `assembly_member` verbatim without parsing and re-serializing XML, and the compact JSON projection selects only supplementary fields that do not duplicate the URDF (avoiding separate prose shape catalogs). `hardware.json`, STEP component files, and static diagram images are optional.

Include the new data directory in package data and verify that it can be read from an installed wheel outside the Git checkout. New robots share the exact same `robot_information` tool rather than introducing model-specific tools. The active `robot_model_id` must match runtime capabilities, and live episode calibration must always come from current observations rather than static data files. After modifying documentation files, load them in a fresh process and verify that unknown IDs do not fall back, file references resolve cleanly, numeric values and assembly transforms survive intact, and missing calibration is reported honestly.

Validating the `robot_information` interface does not start the simulator or measure payload capacity, friction, compliance, stopping latency, or end-to-end task performance.
