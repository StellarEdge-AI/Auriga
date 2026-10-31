# Stereo Epipolar Line Guide (`stereo_guide`)

`stereo_guide` marks a caller-selected pixel in one formal RGB camera view and draws its corresponding epipolar line across a second synchronized RGB camera view from the same observation, narrowing the search region for matching the same physical feature across views. It does not recognize objects, match pixels automatically, perform 3D triangulation, move cameras, or capture new observations.

## Enabling the Tool

Set `features.stereo_guide=true` in configuration or pass `--stereo-guide` to the Gateway CLI (`--no-stereo-guide` explicitly disables it). It defaults to `false` and is independent of `robotic_view`. Benchmark queue manifest cases accept a boolean `stereo_guide` field and inherit from the unified configuration when omitted. Session resume and replay honor the archived flag (treating older archives without the field as disabled); see the [Configuration Guide](configuration.md) for full precedence rules.

When disabled, the plugin is neither imported nor wired, no `stereo_guide` tool is registered for actor or program callers, and no tool instructions are added to the prompt. Even when enabled, the tool can still be suppressed via `features.disabled_tools` or `--disabled-tools stereo_guide`. Usage instructions are delivered exclusively through the tool's own `ToolSpec` without adding task-specific prompts.

## Inputs and Outputs

All four input parameters are required:

| Field | Description |
| --- | --- |
| `observation_id` | Formal observation ID captured while the robot is idle; both cameras must belong to this same synchronized observation. |
| `source_camera_id` | Calibrated camera in which the source pixel was selected. |
| `target_camera_id` | Second calibrated camera (sharing the same world reference frame) onto which the epipolar line overlay is projected. |
| `source_pixel` | `[u, v]` pixel coordinates in the uncropped source image (`u` right, `v` down), with finite numbers satisfying `0 <= u < width` and `0 <= v < height`. Coordinates from cropped or rescaled views must not be passed directly without converting back to full-frame coordinates. |

Only zero-distortion pinhole camera models are supported: native `pinhole_no_distortion` or OpenCV pinhole models supported by `GeometryTools` whose distortion coefficients are strictly all zeros. Any other distortion model or non-zero distortion coefficient is rejected rather than implicitly undistorted.

On success, the tool returns a typed response with `kind="stereo_guide"` and `data.status="guided"`, along with the source and target camera IDs, source pixel, normalized line coefficients `target_line=[a, b, c]`, clipped segment endpoints `target_endpoints`, and references to the two rendered overlay images. In the uncropped target image, points on the epipolar line satisfy `a*u + b*v + c = 0` with `sqrt(a*a + b*b) = 1`, and `target_endpoints` gives the intersection of that line with the pixel-center bounding rectangle `[0, width - 1] × [0, height - 1]`.

Two overlay images are returned in `(source_camera, target_camera)` order at full native resolution: a cyan crosshair marks `source_pixel` on the source view, and a thin cyan line marks the epipolar constraint across the target view. Both overlays are stored as actor-readable `geometry_overlay` artifacts without modifying the archived source RGB images.

## From Epipolar Line to 3D Triangulation

The example below shows how the primary actor model chains `stereo_guide` with `geometry_query` triangulation. First, pass `current_observation`, the two camera IDs, and `source_pixel` from the current formal observation; after inspecting the returned target overlay image to visually identify the exact same physical feature along the cyan line, pass its pixel coordinates as `matched_target_pixel`:

```python
guide = stereo_guide(
    observation_id=current_observation,
    source_camera_id=source_camera,
    target_camera_id=target_camera,
    source_pixel=source_pixel,
)

# Submit the pixel pair only after visually confirming the same physical feature in both views.
measurement = geometry_query(request={
    "kind": "triangulate",
    "observation_ids": [current_observation, current_observation],
    "camera_id": source_camera,
    "camera_id_b": target_camera,
    "pixels_a": [source_pixel],
    "pixels_b": [matched_target_pixel],
})
```

Passing `current_observation` twice in `observation_ids` is intentional and specifies two synchronized cameras from the same observation step. Inside `program_run`, callers invoke `AurigaClient.call("stereo_guide", {...})` and `AurigaClient.call("geometry_query", {"request": {...}})` and inspect the typed `kind` and `data` fields, while overlay images are delivered through the standard program image channel rather than printed as base64 strings to `stdout`.

A status of `guided` indicates only that the geometric epipolar constraint was computed: the drawn line is not clipped to positive-depth intervals, and the true physical point may be occluded in the target view. A small point-to-line distance or low reprojection residual indicates geometric consistency with the epipolar plane, not proof that the two pixels correspond to the same physical feature; if visual correspondence is ambiguous, callers should treat the match as unknown.

When `geometry_query(kind="triangulate")` runs, it validates positive depth in both cameras, ray parallax angle (default minimum `0.5°`), and reprojection error (default maximum `2 px`). Accepted points return `status="conditional"` with `value.points_m` already expressed in the common reference frame (`env_origin` natively), indexed by `accepted_indices`—callers must not multiply `points_m` by an extra camera transform. Failed geometric checks return `unknown` and must not be used as motion targets; see the [Tool and Plugin Contract](tool-plugins.md) for full field definitions.

## Implementation and Validation Boundaries

The plugin is implemented in [`auriga/runtime/tools/plugins/stereo_guide.py`](../../auriga/runtime/tools/plugins/stereo_guide.py) and reuses `GeometryTools.camera` to read permitted camera calibrations, forward-kinematics camera poses, and observation clock domains. Given optical-to-world transforms `T_A` and `T_B` in a shared reference frame, let `T_B⁻¹ T_A = [R, t]`; the epipolar line in camera `B` is computed as `K_B⁻ᵀ [t]× R K_A⁻¹ [u, v, 1]ᵀ`, normalized to unit normal length, and clipped to the target image bounds.

The tool raises an error if the source and target cameras are identical, the stereo baseline is less than `1e-6 m`, the source ray is collinear with the baseline, the epipolar line does not intersect the target image in a finite non-zero segment, pixel coordinates or calibrations are invalid, or distortion coefficients are non-zero. Mismatched execution states, observation identities, or observation profile permissions are likewise rejected. The tool validates freshness and renders overlay images inside the Harness lock without invoking models, motion planners, camera captures, or simulation steps, and never clears an `unknown` or `stopped` execution gate.

The Gateway wires the plugin via a narrow `make_stereo_guide_tool(read)` callback so that actor and program callers share a single read-only `ToolSpec`. Regression tests covering epipolar geometry, observation permission gates, source-image preservation, and typed image outputs live in [`tests/test_stereo_guide.py`](../../tests/test_stereo_guide.py). These software checks verify projective geometry and boundary enforcement; they do not solve semantic feature matching, full 6-DoF pose estimation, or end-to-end task performance, which must be evaluated independently in native benchmark runs.
