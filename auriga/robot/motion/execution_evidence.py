"""Per-action execution accounting, without planner, simulator, or I/O access."""


class ExecutionEvidence:
    def __init__(self, receipt, segments):
        self.receipt, self.segments = receipt, segments

    def attempted(self, command_phase, arm_context):
        jaws = tuple(self.receipt.get('gripper_execution', {}).values()) if command_phase == 'gripper' else ()
        for jaw in jaws:
            jaw['dispatch_attempted_steps'] += 1
        attempted = []
        if arm_context is not None and self.segments:
            for side, context in arm_context.items():
                index = context.get('segment_index', context['waypoint_index'])
                if index is not None and context['phase'] != 'hold':
                    own = self.segments[-1]['arms'][side]['segments'][index]
                    own['attempted_steps'] = own.get('attempted_steps', 0) + 1
                    attempted.append(own)
        return (*jaws, *attempted)

    @staticmethod
    def failed(attempted):
        for record in attempted:
            record['execution_uncertain'] = True

    def completed(self, command_phase, arm_context):
        receipt, segments = self.receipt, self.segments
        receipt['executed_steps'] += 1
        if segments:
            segment = segments[-1]
            segment['completed_native_steps'] = segment.get('completed_native_steps', 0) + 1
            key = 'completed_' + command_phase + '_steps'
            segment[key] = segment.get(key, 0) + 1
            if arm_context is not None:
                # One successful native call can advance different phases and
                # waypoint indices on the two arms. Guards run after this count.
                for side, context in arm_context.items():
                    arm_trace = segment['arms'][side]
                    own_key = 'completed_' + context['phase'] + '_steps'
                    arm_trace[own_key] = arm_trace.get(own_key, 0) + 1
                    index = context.get('segment_index', context['waypoint_index'])
                    if index is not None:
                        own = arm_trace['segments'][index]
                        own[own_key] = own.get(own_key, 0) + 1
            elif segment.get('arm') == 'both':
                for arm_trace in segment['arms'].values():
                    own_count = arm_trace['planned_' + command_phase + '_steps']
                    arm_trace[key] = min(segment[key], own_count)
                    arm_trace['completed_' + command_phase + '_hold_steps'] = max(0, segment[key] - own_count)
        if command_phase == 'gripper':
            for jaw in receipt.get('gripper_execution', {}).values():
                jaw['completed_steps'] += 1
                jaw['started'] = True
                jaw['command_complete'] = jaw['completed_steps'] == jaw['planned_steps']

    def sample(self, control_step, state):
        self.receipt['actual_trace'].append(dict(sim_step=control_step, state=state))

    def finish(self, *, start_control_step, control_step, start_native_count, native_count,
               episode_terminal, dispatched_trace):
        receipt, segments = self.receipt, self.segments
        receipt['planned_trace'] = segments[0] if len(segments) == 1 else {'segments': segments}
        receipt['execution_interval'] = dict(start_sim_step=start_control_step, end_sim_step=control_step,
                                             step_domain='adapter_control_step')
        receipt['executed_control_steps'] = control_step - start_control_step
        receipt['completed_native_steps'] = receipt['executed_steps']
        receipt['executed_native_steps'] = native_count - start_native_count
        receipt['executed_steps'] = receipt['executed_native_steps']
        receipt['step_semantics'] = {
            'executed_steps': 'official native action count delta; includes a partially executed faulting call',
            'completed_native_steps': 'native take_action calls that returned successfully',
            'executed_control_steps': 'physics steps within official action interpolation'}
        receipt['episode_terminal'] = episode_terminal
        receipt['dispatched_trace'] = dict(scope='official interpolated actuator commands', samples=dispatched_trace)
