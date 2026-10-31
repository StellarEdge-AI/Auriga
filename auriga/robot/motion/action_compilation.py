"""Select backend-owned pure compilers without assuming every robot is ARX."""
from ..model.embodiments import ARX_X5


def uses_arx_actions(capabilities):
    return (capabilities.get("tcp_config_id") == ARX_X5.tcp_config_id
            and capabilities.get("robot_model_id", ARX_X5.robot_model_id) == ARX_X5.robot_model_id)


def _compiler(backend, name, capabilities):
    compiler = getattr(backend, name, None)
    if callable(compiler):
        return compiler
    # Older X5 integrations identify their calibration but predate compiler hooks.
    if compiler is None and uses_arx_actions(capabilities):
        from . import arx_actions
        return getattr(arx_actions, name)
    raise ValueError("action_compiler_unavailable:" + name)


def compile_action(backend, kind, args, identity, capabilities):
    return _compiler(backend, "compile_action", capabilities)(kind, args, identity, capabilities)


def validate_backend_action(backend, action, capabilities):
    validator = getattr(backend, "validate_action", None)
    if callable(validator):
        validator(action)
    elif not uses_arx_actions(capabilities):
        raise ValueError("action_validator_unavailable")
