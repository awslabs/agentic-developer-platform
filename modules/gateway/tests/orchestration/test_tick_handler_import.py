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


def test_work_claims_imports_without_token_secret():
    """The dispatch pass lazily imports work claims on its first live launch."""
    result = _import_in_scrubbed_subprocess("from src.orchestration.work_claims import claim_work")
    assert result.returncode == 0, f"work_claims dragged web auth into the tick Lambda:\n{result.stderr}"


def test_dispatch_policy_resolves_without_web_session_secret():
    """Importing the handler passes even when a lazy policy lookup loads web auth.

    Exercise the policy lookup itself for human and service principals, in the
    Lambda environment that exposed the production failure.
    """
    result = _import_in_scrubbed_subprocess("""
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from src.agentauth.model_policy import _resolve_active_allowlist_policy
from src.shared.config import get_settings

async def check():
    for kind in ('human', 'service_account'):
        db = SimpleNamespace(
            scalar=AsyncMock(return_value=SimpleNamespace(id='user-a', team_id='', status='active')),
            scalars=AsyncMock(return_value=[]),
        )
        result = await _resolve_active_allowlist_policy(
            db, tenant_id='aws-e', principal_kind=kind, principal_id='user-a',
            expires_at=datetime.now(UTC) + timedelta(minutes=1), settings=get_settings(),
        )
        assert result.context.org_id == 'aws-e'
        assert result.service_policy_unavailable_reason is None
    assert 'src.auth.middleware' not in sys.modules
    assert 'src.admin.persona_models.catalogue_routes' not in sys.modules

asyncio.run(check())
""")
    assert result.returncode == 0, f"Engine policy resolution loaded web auth:\n{result.stderr}"


def test_admin_lazy_exports_still_resolve():
    """The app-facing surface of `src.admin` must survive the lazy rewrite."""
    import src.admin as admin

    assert admin.AdminService.__name__ == "AdminService"
    assert admin.AccessControl.__name__ == "AccessControl"
    # FastAPI auto-discovery and both router aliases.
    assert admin.router is admin.admin_router
    assert admin.health_router is not None


def test_proxy_lazy_exports_still_resolve():
    import src.proxy as proxy
    from src.proxy.routes import router
    from src.proxy.service import ProxyService

    assert proxy.router is router
    assert proxy.ProxyService is ProxyService


def test_admin_unknown_attribute_raises():
    import src.admin as admin

    try:
        admin.definitely_not_an_export
    except AttributeError:
        pass
    else:
        raise AssertionError("expected AttributeError for unknown export")


def test_activity_lazy_router_exports_still_resolve():
    import src.activity as activity

    assert activity.router is activity.activity_router
