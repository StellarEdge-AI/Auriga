"""Legacy compiler regression adapter, never installed by an actor gateway.

Old fixed skill/template compilers remain backend compatibility code. Their CPU
fixtures exercise those algorithms, receipts and cancellation using this local
runner; the production ProgramRunner still rejects every legacy motion name.
This class is deliberately outside auriga and has no invocation/schema flag.
"""
import threading

from auriga.runtime.programs.runner import ProgramRunner, ProgramServices
from auriga.runtime.programs.program_review import LEGACY_ACTION_TOOLS
from auriga.core.observation_policy import LEGACY_ACTOR_PROFILE


def program_services(harness):
    """Bind minimal synthetic hosts; real Harness fixtures use the real port."""
    if hasattr(harness, 'program_services'):
        return harness.program_services()
    from auriga.runtime.tools.builtins import create_registry
    registry = create_registry()
    lock = getattr(harness, 'lock', threading.RLock())

    def active(identifier, *, cancel=False):
        with lock:
            owns_action = (getattr(harness, 'active_context', None) or {}).get('program_id') == identifier
            if owns_action and cancel:
                harness.cancel.set()
            return owns_action

    def invoke(name, arguments, correlation):
        registry.validate(name, arguments, role='program')
        return harness.dispatch(name, arguments, correlation)

    return ProgramServices(store=harness.store,
        validate_tool=lambda name: registry.spec(name, role='program'),
        validate_arguments=lambda name, arguments: registry.validate(name, arguments, role='program'),
        invoke=invoke, effect=lambda name, arguments: registry.spec(name, role='program').effect_for(arguments),
        observation=lambda: dict(observation_id=(getattr(harness, 'latest', None) or {}).get('observation_id'),
            observation_profile=getattr(harness, 'observation_profile', LEGACY_ACTOR_PROFILE)),
        action_active=active, cancel_action=lambda identifier: active(identifier, cancel=True))


class LegacyProgramRunner(ProgramRunner):
    def __init__(self, harness):
        self._legacy_dispatch = lambda name, arguments, correlation: harness.dispatch(name, arguments, correlation)
        super().__init__(program_services(harness))

    def _validate_tool(self, name):
        if name in LEGACY_ACTION_TOOLS | {'task_plan'}:
            return
        super()._validate_tool(name)

    def _requires_review(self, entry, tool, arguments):
        if tool in LEGACY_ACTION_TOOLS | {'robot_wait'}:
            return False
        return super()._requires_review(entry, tool, arguments)

    def _validate_arguments(self, name, arguments):
        if name in LEGACY_ACTION_TOOLS | {'task_plan'}:
            return
        super()._validate_arguments(name, arguments)

    def _invoke(self, name, arguments, correlation):
        if name in LEGACY_ACTION_TOOLS | {'task_plan'}:
            return self._legacy_dispatch(name, arguments, correlation)
        return super()._invoke(name, arguments, correlation)

    def _tool_effect(self, name, arguments):
        if name in LEGACY_ACTION_TOOLS:
            return 'legacy'
        if name == 'task_plan':
            return 'record'
        return super()._tool_effect(name, arguments)
