"""Tool handlers. Registration binds these functions; dispatch has no name switch."""
import json

from ...core.tool_output import ToolOutput, ToolResponse, resolve_detail


def program_display(runtime, result, detail, correlation):
    if correlation.get('program_id') or not correlation.get('actor_log_delivery'):
        return result
    epoch = runtime._model_context_epoch()
    projected = runtime._program_output_delivery.project(result, epoch=epoch,
        call_id=correlation.get('tool_call_id'), detail=detail)
    return projected if runtime._model_context_epoch() == epoch else result


def program_content(runtime, job, result, detail, correlation):
    """Forward broker-owned images with their original labels, never stdout media."""
    visual_parts = {}
    errors = []
    for feedback in result.get('visual_feedback', []):
        try:
            info, raw = runtime.store.resolve_artifact(feedback['reply_ref'], allowed_scopes=('actor',))
            if info.get('kind') != 'program_visual_reply':
                raise ValueError('program_visual_artifact_kind_mismatch')
            parts = json.loads(raw)['contentItems']
        except Exception as error:
            feedback['delivery'] = dict(status='unavailable', reason=str(error))
            errors.append(dict(source='supplementary', call_id=feedback['call_id'], reason=str(error)))
            continue
        visual_parts[feedback['call_id']] = ToolOutput(dict(program_visual_feedback=feedback,
            current_observation_id_at_delivery=(runtime.latest or {}).get('observation_id'),
            semantics='Saved program tool reply at this call; preview/historical images are not a new current observation. '
                'Original currentness flags and budgets are as of the source call, not delivery time.'
            )).content_parts()
        visual_parts[feedback['call_id']] += parts
    extra = []
    if job.get('status') == 'awaiting_review':
        try:
            extra += [item for item in runtime.programs.review_content(job['job_id'])
                        if item.get('type') == 'inputImage']
        except Exception as error:
            errors.append(dict(source='pending_review', reason=str(error)))
    if job.get('final_observation_id'):
        try:
            bundle = runtime.store.read('observation', job['final_observation_id'])
            extra += runtime.observation_content(bundle,
                correlation, structured=True)
        except Exception as error:
            errors.append(dict(source='final_observation', reason=str(error)))
        else:
            try:
                from ...perception.external.obs3d import action_attachments, attachments
                recent = (job.get('action_summary') or {}).get('recent_actions') or []
                if recent:
                    before_obs = next(
                        (item.get('based_on_observation_id') for item in recent
                         if isinstance(item, dict) and item.get('based_on_observation_id')),
                        None,
                    )
                    extra += action_attachments(runtime, bundle, correlation, before_observation_id=before_obs)
                else:
                    extra += attachments(runtime, bundle, correlation, reply='observe')
            except Exception:
                pass
    if errors:
        # A display failure must not erase already recorded action/process facts.
        result['visual_feedback_errors'] = errors
        runtime._program_output_delivery.invalidate()
    # Preparing final-frame content can reset the image context. Project afterwards.
    result = program_display(runtime, result, detail, correlation)
    content = ToolOutput(result, projection='owner_record').content_parts()
    for feedback in result.get('visual_feedback', []):
        if not feedback.get('delivery'):
            content += visual_parts[feedback['call_id']]
    return content + extra


def run_program(runtime, name, args, correlation):
    if not runtime.programs: raise ValueError("program_runner_not_started")
    detail = resolve_detail(args.get("detail"), program=bool(correlation.get("program_id")))
    args = {key: value for key, value in args.items() if key != "detail"}
    from ..programs.program_result import program_result
    job = runtime.programs.run(args, correlation)
    result = program_result(runtime.store, job, detail=detail)
    content = program_content(runtime, job, result, detail, correlation)
    return ToolResponse(content, data=job, kind="program", data_projection="owner_record")
