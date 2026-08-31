"""Regression tests for the tick Lambda import cascade (#4527).

The orchestration-tick Lambda imports `src.orchestration.tick_handler` in an
environment that has no web-session secret (``BG_TOKEN_SECRET_KEY``) — and
must never need one. It once crashed at every invocation because importing
any `src.admin` submodule eagerly executed `src.admin.__init__` →
`routes.py` → `src.auth.middleware`, whose module-level ``AuthService()``
raises without the secret. `src/admin/__init__.py` now resolves its exports
lazily (PEP 562); these tests pin both sides of that contract.

The import tests run in a subprocess with a scrubbed environment because
`tests/conftest.py` sets ``BG_TOKEN_SECRET_KEY`` process-wide, which would
mask the regression in-process.
"""

import os
import subprocess
import sys
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]

# Env vars that let the auth stack initialize; the whole point is that the
# tick handler must import WITHOUT them.
_SECRET_VARS = ("BG_TOKEN_SECRET_KEY", "JWT_SECRET_KEY")


def _import_in_scrubbed_subprocess(statement: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _SECRET_VARS}
    env["AWS_REGION"] = env.get("AWS_REGION", "us-east-1")
    return subprocess.run(
        [sys.executable, "-c", statement],
        cwd=GATEWAY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_tick_handler_imports_without_token_secret():
    """The Lambda entrypoint must import with no web-session secret (#4527)."""
    result = _import_in_scrubbed_subprocess("import src.orchestration.tick_handler")
    assert result.returncode == 0, f"tick_handler failed to import without BG_TOKEN_SECRET_KEY — the admin import cascade is back:\n{result.stderr}"


def test_admin_submodule_imports_without_token_secret():
    """Importing one admin submodule must not drag in the auth stack."""
    result = _import_in_scrubbed_subprocess("from src.admin.access_control import AccessControl")
    assert result.returncode == 0, f"src.admin.access_control failed to import without BG_TOKEN_SECRET_KEY:\n{result.stderr}"


def test_admin_lazy_exports_still_resolve():
    """The app-facing surface of `src.admin` must survive the lazy rewrite."""
    import src.admin as admin

    assert admin.AdminService.__name__ == "AdminService"
    assert admin.AccessControl.__name__ == "AccessControl"
    # FastAPI auto-discovery and both router aliases.
    assert admin.router is admin.admin_router
    assert admin.health_router is not None


def test_admin_unknown_attribute_raises():
    import src.admin as admin

    try:
        admin.definitely_not_an_export
    except AttributeError:
        pass
    else:
        raise AssertionError("expected AttributeError for unknown export")
