# Adaptive camera feedback

`images.feedback_policy` is `all` by default. The opt-in `adaptive` policy changes which already archived original RGB views accompany ordinary completed action and template feedback. Capture and storage retain all cameras. This is a deterministic CPU comparison and does not call a model or advance simulation.

Initial delivery and delivery after compaction/session replacement, explicit `robot_observe`, history requests, waits, failed or unknown action results, observation checkpoints, and key handoff verification use full views. A supported-transfer template context or airborne receiver template marks a key handoff. Ordinary successful templates are eligible for deduplication. A requested history camera or crop still follows the explicit request. Unknown context falls back to inclusion. Both direct action and outer template feedback use the same selector; program-internal captures never advance the model baseline.

All three cameras, including head and active wrists, are compared with their last original view delivered to the model. Arm participation is telemetry, not an inclusion or exclusion rule. A stationary arm's view is included when its pixels change; an active arm's unchanged view can be omitted. If every view is below the thresholds, the response contains measured state and retrieval metadata with no repeated RGB. Trajectory overlays have a separate delivery policy: fully recorded routine completed actions return a scalar summary and exact artifact reference; diagnostic actions retain detailed paths and overlays. An overlay never replaces the original-image baseline. See [tool output contract](tool-output-contract.md).

## Delivery baselines and caller contract

`auriga.perception.view_selection.select_observation_views(bundle, images, last_delivered, ...)` accepts archived observation metadata, camera-ID-to-original-bytes mappings, and camera-ID-to-baseline mappings. It returns JSON-safe `selected_camera_ids`, `skipped_camera_ids`, ordered per-camera `decisions`, thresholds and fallback reasons. Each skipped decision retains camera ID, current observation ID, comparison baseline observation ID, reason, and a complete `history_view_observation` tool call with arguments.

`baseline_record(bundle, camera_id, rgb_bytes)` constructs an in-memory record containing the original encoded bytes, original image provenance, dimensions, calibration ID/reference, observation ID and run/episode/environment identity. This record contains bytes and is separate from JSON delivery metadata. The caller must update its map **only when that full original view is actually delivered to the model**. Captures, saved images, skipped images, program-only tool responses and cropped/resized/overlay views do not advance it. Reset baselines after model-context loss, compaction that discards those images, session replacement or other changes that invalidate the claim that the model still has the reference view.

The Harness also invalidates baselines synchronously on the actor thread’s native `contextCompaction` started/completed events. Local window compaction may use ordinary `/responses`; watching only `/responses/compact` is insufficient. The proxy endpoint hook remains a separate fallback. Background-thread compaction does not reset the actor baseline.

Selection never changes that mapping. Successive small changes therefore accumulate against the last delivered image until a threshold is reached; skipping a capture cannot move the reference forward. A full original camera requested through history can become the new reference after model delivery. Cropped history cannot.

The caller supplies `receipt_status`, `receipt_reason` and `context`. Context `post_action` or `template`, status `completed`, and absent/empty reason qualify. An outer template is marked completed for this purpose only after its expected phases succeed and its terminal observation is current; this is not a claim of object retention. Use a different context or `force_full=True, full_reason="handoff_verification"` for full feedback. `active_arms` remains optional diagnostic metadata. Unknown context/status or an exceptional reason conservatively retains all views.

The caller also validates synchronized camera calibration and supplies `calibration_valid={camera_id: True/False}`. Profile availability alone is insufficient. Only explicit `True` permits comparison. Missing/changed calibration references, unknown or changed environment identity, missing provenance, transformed views, changed dimensions, unavailable images or decoding failures include the affected view. Matching encoded bytes do not bypass decoding, so identical corrupt inputs still include.

## Change thresholds

Every threshold is exposed as a module constant, can be overridden through the helper's `settings` mapping, and is returned in result metadata. Invalid overrides force full delivery. Current defaults are deliberately conservative:

| Measurement | Include the view when |
|---|---|
| Mean absolute RGB difference on a `96 × 72` BOX thumbnail, channel scale `0–255` | `>= 1.0` |
| Fraction of thumbnail pixels whose largest RGB channel difference is `> 12.0` | `>= 0.001` |
| Largest channel difference at original image resolution | `>= 32.0` |

Any one trigger is sufficient. The original-resolution peak catches a very small high-contrast change that thumbnail averaging could otherwise hide. The comparison uses decoded archived RGB before transport encoding; it does not change the image resolution or coordinates delivered for selected cameras. This is a visual-change heuristic: a subthreshold change can still be task-relevant, and inclusion can also be caused by rendering/encoding variation. No semantic equivalence or task-performance guarantee follows from these thresholds.

## Measurement and validation limits

Record selected/skipped image counts and original bytes separately from encoded transport bytes. A same-encoder estimate for omitted transport bytes is a counterfactual delivery-size metric. Neither quantity is image-token attribution. Model requests also contain history, tool schemas and text; provider cached-input tokens are another separate quantity.

Existing `reuse_exact_images` removes only exact duplicate typed image parts within one model request and keeps a reference to an earlier image in that same request. It does not supply this policy's cross-observation baseline. The image URL cache saves encoding CPU; archive sharing saves storage. Neither alone reduces the number of views delivered to the model.

Unit checks cover accumulated change against the last delivered baseline, tiny local changes, all-camera comparison and changed active/jaw wrist delivery, failure/context overrides, camera ordering, retrieval metadata and conservative fallback. They establish delivery-selection behavior only. A matched full-feature ablation with full versus adaptive views must assess native task result, native steps, model usage and encoded delivery volume before claiming maintained task performance or overall savings. An offline replay can estimate omitted images/bytes in a frozen trajectory, but cannot predict the actor's changed decisions or establish causal episode savings.
