"""Fixed current-interface robot acceptance, using Harness and no model calls."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time

from auriga.robot.motion.arx_actions import compile_action
from auriga.core.config import Config
from auriga.core.contracts import new_id
from auriga.apps.library.experiments import atomic_json, finish_run
from auriga.runtime.harness import Harness
from auriga.robot.backends.robodojo.robodojo import RobotBackend
from auriga.core.storage.store import Store


def validate_options(displacement_m, jaw_goal):
    if type(displacement_m) not in (int, float) or not math.isfinite(displacement_m) or not 0 < displacement_m <= .02:
        raise ValueError('validation_displacement_m_requires_0_to_0.02')
    if type(jaw_goal) not in (int, float) or not math.isfinite(jaw_goal) or not .5 <= jaw_goal <= 1:
        raise ValueError('validation_jaw_goal_requires_0.5_to_1')


def run_checks(harness, report, *, displacement_m=.005, jaw_goal=.8, save=lambda: None):
    """Use nearby measured poses; check execution evidence, never task success."""
    validate_options(displacement_m, jaw_goal)
    report.setdefault('checks', {})
    report.setdefault('calls', [])

    def check(name, condition, **evidence):
        report['checks'][name] = dict(passed=bool(condition), **evidence)
        save()
        if not condition:
            raise RuntimeError('robot_validation_failed:' + name)

    def state():
        current = harness.current_observation()
        return json.loads(harness.store.resolve_artifact(current['robot_state_ref'])[1])

    def measured_intervals(receipt):
        samples = json.loads(harness.store.resolve_artifact(receipt['actual_trace_ref'])[1])
        return [(first['state']['arms'], second['state']['arms']) for first, second in zip(samples, samples[1:])
            if second['sim_step'] > first['sim_step']]

    def moved(first, second, arm):
        return any(abs(a-b) > 1e-7 for a, b in zip(first[arm]['q_rad'], second[arm]['q_rad']))

    def move(identifier, arm, pose, delta=0., **options):
        position = list(pose['position_m'])
        position[2] += delta
        return dict(id=identifier, kind='move', arm=arm, position_m=position,
            quaternion_xyzw=list(pose['quaternion_xyzw']), speed_scale=.8, **options)

    def call(tool, arguments):
        response = harness.invoke(tool, arguments, dict(thread_id='fixed-validation',
            turn_id='matrix', tool_call_id=new_id('validation-call')), role='actor')
        value = response.data
        report['calls'].append(dict(tool=tool, arguments=deepcopy(arguments), result=value))
        save()
        if response.get('success') is not True:
            raise RuntimeError('robot_validation_tool_error:' + str(value))
        return value

    def preview(name, stages):
        before = harness.current_observation()
        plan = call('robot_action', dict(observation_id=before['observation_id'], stages=stages))
        after = harness.robot.observe()
        check(name + '_preview', plan.get('status') == 'planned' and after['sim_step'] == before['sim_step'],
            plan_id=plan.get('plan_id'), before_sim_step=before['sim_step'], after_sim_step=after['sim_step'])
        return plan

    def execute(name, plan, expected='completed'):
        value = call('robot_action', dict(mode='execute', plan_id=plan['plan_id']))
        action_id = value['action_id']
        receipt = harness.store.read('action_receipt', action_id)
        status = 'stopped' if expected == 'awaiting_observation' else 'completed'
        check(name, receipt.get('status') == status and receipt.get('batch_status') == expected
            and bool(receipt.get('result_observation_id')), action_id=action_id, receipt=receipt)
        return receipt

    initial = state()
    left = initial['arms']['left']['ee_pose']
    plan = preview('single_checkpoint', [move('lift', 'left', left, displacement_m),
        dict(id='inspect', kind='observe', reason='Validate cached-plan checkpoint continuation.'),
        move('return', 'left', left)])
    paused = execute('single_checkpoint_pause', plan, 'awaiting_observation')
    finished = execute('single_checkpoint_resume', plan)
    check('same_cached_plan_resumed', paused['batch_plan_id'] == finished['batch_plan_id']
        and finished['batch_cursor'] > paused['batch_cursor']
        and finished['batch_revision'] > paused['batch_revision'], plan_id=plan['plan_id'])

    current = state()
    left, right = (current['arms'][side]['ee_pose'] for side in ('left', 'right'))
    plan = preview('unequal_dual', [move('left_out', 'left', left, displacement_m),
        move('left_back', 'left', left), move('right_out', 'right', right, displacement_m)])
    result = execute('unequal_dual', plan)
    check('unequal_dual_stage_coverage', {'left_out', 'left_back', 'right_out'} <= set(result['completed_stage_ids']))
    simultaneous = sum(moved(first, second, 'left') and moved(first, second, 'right')
        for first, second in measured_intervals(result))
    check('dual_measured_overlap', simultaneous > 0, simultaneous_native_intervals=simultaneous,
        actual_trace_ref=result['actual_trace_ref'])
    plan = preview('right_return', [move('right_back', 'right', right)])
    execute('right_return', plan)

    current = state()
    left = current['arms']['left']['ee_pose']
    initial_jaw = current['arms']['left']['gripper']['goal_normalized']
    check('jaw_goal_changes_command', abs(jaw_goal - initial_jaw) > .01,
        initial_jaw_goal=initial_jaw, validation_jaw_goal=jaw_goal)
    plan = preview('synchronized_gripper', [move('jaw_during_lift', 'left', left, displacement_m,
        gripper_goal_normalized=jaw_goal, gripper_motion=dict(start_fraction=.25, end_fraction=.75)),
        move('lower', 'left', left), dict(id='restore_jaw', kind='gripper', arm='left', goal_normalized=initial_jaw)])
    result = execute('synchronized_gripper', plan)
    check('synchronized_gripper_stage_coverage', {'jaw_during_lift', 'lower', 'restore_jaw'} <= set(result['completed_stage_ids']))
    lift_steps = result['stage_results']['jaw_during_lift']['planned_steps']
    synchronized = sum(moved(first, second, 'left') and abs(first['left']['gripper']['goal_normalized']
        - second['left']['gripper']['goal_normalized']) > 1e-7
        for first, second in measured_intervals(result)[:lift_steps])
    check('jaw_command_during_measured_motion', synchronized > 0, synchronized_native_intervals=synchronized,
        actual_trace_ref=result['actual_trace_ref'], semantics='jaw command change during measured arm motion; no grasp claim')

    waited = call('robot_wait', dict(observation_id=harness.current_observation()['observation_id'], native_steps=3))
    receipt = waited['receipt']
    check('bounded_wait', receipt.get('status') == 'completed' and receipt.get('executed_native_steps') == 3
        and bool(receipt.get('result_observation_id')), receipt=receipt)

    # Real isolated programs, with a fixed operator decision and no model.
    # Native time must stay paused while a cached action awaits that decision.
    from auriga.runtime.programs.runner import ProgramRunner
    if harness.programs is not None:
        raise ValueError('matrix_requires_no_existing_program_runner')
    runner = harness.programs = ProgramRunner(harness.program_services())
    try:
        def program():
            observation = harness.current_observation()
            pose = state()['arms']['left']['ee_pose']
            stages = [move('program_lift', 'left', pose, displacement_m), move('program_return', 'left', pose)]
            source = ('from auriga_client import AurigaClient\n'
                f'AurigaClient().action({stages!r}, observation_id={observation["observation_id"]!r})\n')
            return call('program_run', dict(source=source, timeout_s=300))

        before = harness.robot.observe()['sim_step']
        pending = program()
        check('program_review_pauses_native_clock', pending['status'] == 'awaiting_review'
            and harness.robot.observe()['sim_step'] == before, job=pending)
        reviewed_plan = pending['pending_review']['plan_id']
        native_plan = harness.store.read('unified_action_plan', reviewed_plan)['native_plan_id']
        approved = call('program_run', dict(operation='resume', job_id=pending['job_id'],
            review_id=pending['pending_review']['review_id'], decision='approve'))
        receipts = [value for value in harness.store.records('action_receipt')
            if value.get('batch_plan_id') == native_plan]
        check('program_approval_executes_reviewed_plan', approved['status'] == 'succeeded'
            and approved.get('stop_confirmed') is True and len(receipts) == 1
            and receipts[0]['status'] == 'completed', job=approved, receipts=receipts)

        before_count = harness.action_count
        before = harness.robot.observe()['sim_step']
        pending = program()
        check('program_cancel_starts_at_review', pending['status'] == 'awaiting_review')
        runner.cancel(pending['job_id'])
        deadline = time.monotonic() + 10
        while pending['job_id'] in runner.snapshot()['active_jobs'] and time.monotonic() < deadline:
            time.sleep(.02)
        cancelled = runner.status(pending['job_id'])
        check('program_cancel_preserves_world', cancelled['status'] == 'cancelled'
            and cancelled.get('stop_confirmed') is True and harness.action_count == before_count
            and harness.robot.observe()['sim_step'] == before, job=cancelled)
    finally:
        snapshot = runner.close()
        if not snapshot['active_jobs'] and not snapshot['unresolved_jobs']:
            harness.programs = None
        check('program_cleanup_confirmed', not snapshot['active_jobs'] and not snapshot['unresolved_jobs'],
            snapshot=snapshot)

    # Deliberately discard only the terminal capture after a real wait. Preserve
    # the completed action and verify stale observations cannot admit another.
    original_execute = harness.robot.execute
    before = harness.current_observation()
    def missing_terminal(action, **kwargs):
        result = original_execute(action, **kwargs)
        result.pop('terminal_observation', None)
        return result
    action = compile_action('wait', dict(action_id=new_id('validation-missing-frame'),
        observation_id=before['observation_id'], native_steps=2, timeout_s=60),
        harness.identity, harness.capabilities)
    harness.robot.execute = missing_terminal
    try:
        completed = harness.execute(action)['receipt']
    finally:
        harness.robot.execute = original_execute
    check('missing_terminal_keeps_execution_fact', completed['status'] == 'completed'
        and completed.get('executed_native_steps') == 2 and completed.get('result_observation_id') is None
        and harness.latest is None, receipt=completed, injected_fault='discard_terminal_observation_after_real_execution')
    before_count = harness.action_count
    rejected = harness.execute({**action, 'action_id': new_id('validation-stale-frame')})['receipt']
    check('missing_terminal_blocks_new_action', rejected['status'] == 'rejected'
        and rejected.get('reason') == 'current_observation_unavailable'
        and harness.action_count == before_count, receipt=rejected)
    fresh = call('robot_observe', {})
    resumed = call('robot_wait', dict(observation_id=fresh['observation_id'], native_steps=2))
    check('fresh_observation_restores_admission', resumed['receipt']['status'] == 'completed'
        and bool(resumed['receipt'].get('result_observation_id')), receipt=resumed['receipt'])

    # Deliver stop from the actual backend progress channel, after confirmed
    # native progress, through the same Harness admission/cancel boundary.
    original_execute = harness.robot.execute
    requested = threading.Event()
    def cancellable(action, progress_callback=None, cancel_flag=None):
        def progress(update):
            if progress_callback:
                progress_callback(update)
            if update.get('executed_steps', 0) >= 1 and not requested.is_set():
                requested.set()
                harness.request_stop()
        return original_execute(action, progress_callback=progress, cancel_flag=cancel_flag)
    harness.robot.execute = cancellable
    try:
        action = compile_action('wait', dict(action_id=new_id('validation-stop'),
            observation_id=harness.current_observation()['observation_id'], native_steps=50, timeout_s=60),
            harness.identity, harness.capabilities)
        stopped = harness.execute(action)['receipt']
    finally:
        harness.robot.execute = original_execute
    check('confirmed_stop', requested.is_set() and stopped.get('status') == 'stopped'
        and stopped.get('reason') == 'cancelled' and stopped.get('stop_confirmation', {}).get('clock') == 'paused'
        and bool(stopped.get('result_observation_id')), receipt=stopped)
    check('stop_closed_admission', harness.accepting is False)


def run_validation(directory, *, gpu, task='stack_bowls', displacement_m=.005, jaw_goal=.8):
    validate_options(displacement_m, jaw_goal)
    directory = Path(directory).resolve()
    config = Config.load().with_overrides({'run': dict(gpu=gpu, task=task), 'features': dict(
        observation_profile='rgb_proprio_calibrated_v1', motion_execution_mode='resampled',
        completion_guidance='none', record_video=True, export_video=True, enable_wait=True)})
    identity = dict(run_id=directory.name, episode_id=new_id('ep'), env_instance_id=new_id('env'), env_generation=1)
    report = dict(schema_version=1, matrix='current', **identity, started_at=datetime.now(timezone.utc).isoformat(),
        data_origin='robodojo_runtime', execution_role='fixed_interface_validation_without_model',
        task_success=None, config=config.to_dict(), checks={}, passed=False)
    def save():
        atomic_json(directory / 'validation-report.json', report)
    store = backend = harness = None
    checks_completed = False
    save()
    try:
        from .provenance import source_binding
        report['source_binding'] = source_binding(Path(__file__).resolve().parents[3])
        store = Store(directory, identity)
        backend = RobotBackend(config=config, run_dir=directory / 'private' / 'robot')
        harness = Harness(store, backend, config=config)
        harness.start()
        report['capabilities'] = harness.capabilities
        run_checks(harness, report, displacement_m=displacement_m, jaw_goal=jaw_goal, save=save)
        videos = backend.save_video()
        report['videos'] = videos
        for video in videos.get('videos', []):
            video['video_ref'] = store.put_artifact(Path(video['video_path']).read_bytes(), 'native_video', 'video/mp4', 'observer')
        checks_completed = True
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        cleanup_errors = report['cleanup_errors'] = []
        if harness is not None:
            try:
                harness.request_stop()
            except Exception as error:
                cleanup_errors.append(dict(step='request_stop', error=f'{type(error).__name__}: {error}'))
        if backend is not None:
            try:
                report['cleanup'] = backend.close()
            except Exception as error:
                cleanup_errors.append(dict(step='robot_close', error=f'{type(error).__name__}: {error}'))
        programs_stopped = True
        if harness is not None and harness.programs is not None:
            programs_stopped = False
            try:
                snapshot = report['program_cleanup'] = harness.programs.close()
                programs_stopped = not snapshot['active_jobs'] and not snapshot['unresolved_jobs']
                if not programs_stopped:
                    cleanup_errors.append(dict(step='program_close', error='program_stop_unconfirmed'))
            except Exception as error:
                cleanup_errors.append(dict(step='program_close', error=f'{type(error).__name__}: {error}'))
        if store is not None and programs_stopped:
            try:
                store.close()
            except Exception as error:
                cleanup_errors.append(dict(step='store_close', error=f'{type(error).__name__}: {error}'))
        elif store is not None:
            report['store_close_deferred'] = 'program_storage_users_stop_unconfirmed'
        cleanup = report.get('cleanup', {})
        report['passed'] = checks_completed and not cleanup_errors and all(cleanup.get(key) is True for key in (
            'process_exit_confirmed', 'reader_exit_confirmed', 'native_close_acknowledged'))
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
        try:
            finish_run(directory, status='completed' if report['passed'] else 'failed', summary=report)
        except Exception as error:
            report['passed'] = False
            cleanup_errors.append(dict(step='finish_run', error=f'{type(error).__name__}: {error}'))
            save()
    return report
