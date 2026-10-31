"""Declare runtime facts on process-free startup fixtures."""
from auriga.runtime.tools.builtins import create_registry


def install_registry(runtime, config):
    defaults = dict(config=config, disabled_tools=frozenset(config.features.disabled_tools),
        observation_profile=config.features.observation_profile, capabilities=None,
        motion_review=config.features.motion_review, enable_wait=config.features.enable_wait,
        vision_compare=config.features.vision_compare, visual_reviewer=None,
        action_policy=None, programs=None)
    for name, value in defaults.items():
        if not hasattr(runtime, name):
            setattr(runtime, name, value)
    runtime.tool_registry = create_registry(runtime=runtime)
    return runtime
