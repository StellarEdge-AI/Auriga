"""Compatibility entry point; implementation lives in auriga.apps.evaluation.report."""
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auriga.apps.evaluation import report as _implementation

def __getattr__(name):
    return getattr(_implementation, name)


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    # Preserve module identity for existing importers and instrumentation.
    sys.modules[__name__] = _implementation
