"""
Helper to load a Lambda's ``handler.py`` under a unique module name.

Several lambdas (``budget-usage-tracker``, ``pricing-refresh`` …) each ship a
top-level module literally named ``handler``. The tests used to do::

    sys.path.insert(0, ".../lambda/<name>")
    from handler import some_func

That breaks when more than one handler is exercised in the same pytest
process: the first ``import handler`` populates ``sys.modules["handler"]`` and
every later ``from handler import …`` returns that cached module regardless of
the ``sys.path`` change — so the wrong lambda's functions get imported (or the
import fails with ``ImportError`` / ``KeyError`` when the symbol differs).

``load_handler`` sidesteps the collision by loading each handler from its
explicit file path under a stable, unique module name (``lambda_<name>_handler``)
via importlib. The module is registered in ``sys.modules`` under that unique
name so ``unittest.mock.patch`` can still target it (e.g.
``patch("lambda_pricing_refresh_handler.get_db_connection")``).
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

# modules/gateway/lambda
_LAMBDA_ROOT = Path(__file__).resolve().parent.parent.parent / "lambda"
# The shared dir is on path for every handler (db, pricing_fallback live there).
# Inserted at import time so tests can ``from pricing_fallback import …`` at
# module level without each needing its own sys.path dance.
_SHARED = str(_LAMBDA_ROOT / "shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)


def handler_module_name(lambda_dir: str) -> str:
    """Unique, stable module name for a lambda's handler (e.g. ``lambda_pricing_refresh_handler``)."""
    return f"lambda_{lambda_dir.replace('-', '_')}_handler"


def load_handler(lambda_dir: str) -> ModuleType:
    """Load ``lambda/<lambda_dir>/handler.py`` under a unique module name.

    Idempotent: returns the cached module on repeat calls so ``patch`` targets
    stay stable across tests.
    """
    mod_name = handler_module_name(lambda_dir)
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    if _SHARED not in sys.path:
        sys.path.insert(0, _SHARED)

    handler_path = _LAMBDA_ROOT / lambda_dir / "handler.py"
    spec = importlib.util.spec_from_file_location(mod_name, handler_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load handler from {handler_path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so any self-referential imports resolve.
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module
