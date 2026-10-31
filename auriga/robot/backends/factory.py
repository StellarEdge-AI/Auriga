"""Explicit environment construction; injected backends use the same Harness port."""
from types import MappingProxyType


def _robodojo(**kwargs):
    from .robodojo.robodojo import RobotBackend
    return RobotBackend(**kwargs)


ENVIRONMENT_BACKENDS = MappingProxyType({"robodojo": _robodojo})


def create_backend(*, config, **kwargs):
    """Resolve an environment first; its adapter validates the robot binding."""
    try:
        factory = ENVIRONMENT_BACKENDS[config.backend.environment]
    except KeyError:
        raise ValueError(f"unsupported_environment: {config.backend.environment}") from None
    return factory(config=config, **kwargs)
