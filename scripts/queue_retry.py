"""Compatibility entry point; implementation lives in auriga.apps.evaluation.retry."""
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auriga.apps.evaluation import retry as _implementation

def __getattr__(name):
    return getattr(_implementation, name)


if __name__ == "__main__":
    pass  # Historical helper module has no command-line operation.
else:
    # Preserve module identity for existing importers and instrumentation.
    sys.modules[__name__] = _implementation
