"""Regression guard: ``run_alembic`` must derive PYTHONPATH from the real layout.

Issue #8453.

``run_alembic`` used to build the subprocess ``PYTHONPATH`` as::

    [GATEWAY_ROOT,
     GATEWAY_ROOT.parent / "L5-services/src",
     GATEWAY_ROOT.parents[1] / "libs/python/adp-common/src"]

Both sibling entries were unconditional and ``parents[1]`` was indexed blindly.
The packaged qualification image mounts the gateway tree at ``/app``, which has
exactly one parent, so ``parents[1]`` raised ``IndexError`` and every real
PostgreSQL migration test crashed before Alembic was ever invoked — the failure
that blocked tool-adapters qualification. ``Path("/app")`` is therefore used
literally below: it reproduces the old crash and needs no filesystem setup,
because the packaged branch must not consult the filesystem at all.

The sibling source directories exist only in a ``modules/gateway`` repository
checkout; the packaged image installs those dependencies instead. These cases pin
the layout detection in both directions, cover a partial checkout, and assert the
invocation itself (``sys.executable -m alembic``, cwd) plus the fact that no
inherited production database/AWS configuration reaches the subprocess.

``subprocess.run`` is mocked: this file is about the command and environment that
``run_alembic`` constructs, so it must neither need a server nor skip.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.migrations import conftest_postgres

FIXTURE_URL = "postgresql://fixture_user:fixture_pw@127.0.0.1:55432/t0123456789abcdef"
EXPECTED_ASYNC_URL = "postgresql+asyncpg://fixture_user:fixture_pw@127.0.0.1:55432/t0123456789abcdef"
# Exactly what run_alembic is allowed to hand to the subprocess.
EXPECTED_ENV_KEYS = {"LD_LIBRARY_PATH", "PATH", "BG_DATABASE_URL", "BG_RDS_IAM_AUTH", "PYTHONPATH"}


@pytest.fixture
def invoke(monkeypatch):
    """Call ``run_alembic`` with ``GATEWAY_ROOT`` pinned; return the captured call."""

    def _invoke(gateway_root: Path, *args: str) -> dict:
        calls: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, **kwargs})
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(conftest_postgres, "GATEWAY_ROOT", gateway_root)
        monkeypatch.setattr(conftest_postgres.subprocess, "run", fake_run)

        # A production-shaped ambient environment that must not be inherited,
        # beside the one interpreter variable that must be preserved. The other
        # optional interpreter variables are cleared so the assertion on the
        # exact env key set is independent of the host runner.
        monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/hostedtoolcache/python/lib")
        monkeypatch.setenv("AWS_PROFILE", "production-admin")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        monkeypatch.setenv("BG_DATABASE_URL", "postgresql+asyncpg://prod:prod@prod.rds.amazonaws.com/adp")
        monkeypatch.setenv("BG_RDS_IAM_AUTH", "true")
        monkeypatch.setenv("PYTHONPATH", "/opt/inherited/site-packages")
        for name in ("DYLD_LIBRARY_PATH", "PYTHONHOME", "PYTHONUSERBASE"):
            monkeypatch.delenv(name, raising=False)

        result = conftest_postgres.run_alembic(FIXTURE_URL, *(args or ("upgrade", "head")))

        assert result.returncode == 0
        assert len(calls) == 1, "run_alembic must shell out exactly once"
        return calls[0]

    return _invoke


def _python_path(call: dict) -> list[str]:
    return call["env"]["PYTHONPATH"].split(os.pathsep)


def _checkout(root: Path, *, l5: bool = True, common: bool = True) -> Path:
    """Build a ``modules/gateway`` checkout, optionally missing sibling sources."""
    gateway_root = root / "modules/gateway"
    gateway_root.mkdir(parents=True)
    if l5:
        (root / "modules/L5-services/src").mkdir(parents=True)
    if common:
        (root / "libs/python/adp-common/src").mkdir(parents=True)
    return gateway_root


def test_packaged_app_root_uses_only_app_and_drops_production_env(invoke):
    """Case 1: packaged /app — only /app on PYTHONPATH, fixture DB, no AWS leak."""
    app = Path("/app")

    call = invoke(app)

    assert _python_path(call) == [str(app)]
    assert call["cmd"] == [sys.executable, "-m", "alembic", "upgrade", "head"]
    assert call["cwd"] == app
    assert call["capture_output"] is True
    assert call["text"] is True
    assert call["check"] is False

    env = call["env"]
    # Fixture database, never the inherited production one, and IAM auth off.
    assert env["BG_DATABASE_URL"] == EXPECTED_ASYNC_URL
    assert "prod.rds.amazonaws.com" not in env["BG_DATABASE_URL"]
    assert env["BG_RDS_IAM_AUTH"] == "false"
    # Interpreter configuration preserved so the subprocess keeps its packages.
    assert env["LD_LIBRARY_PATH"] == "/opt/hostedtoolcache/python/lib"
    # No inherited AWS credentials/profile and no inherited PYTHONPATH.
    assert "AWS_PROFILE" not in env
    assert "AWS_DEFAULT_REGION" not in env
    assert "/opt/inherited/site-packages" not in env["PYTHONPATH"]
    assert set(env) == EXPECTED_ENV_KEYS


def test_repository_checkout_includes_sibling_sources_in_order(tmp_path, invoke):
    """Case 2: genuine modules/gateway checkout — both siblings, in order."""
    gateway_root = _checkout(tmp_path)

    call = invoke(gateway_root, "downgrade", "-1")

    assert _python_path(call) == [
        str(gateway_root),
        str(tmp_path / "modules/L5-services/src"),
        str(tmp_path / "libs/python/adp-common/src"),
    ]
    assert call["cmd"] == [sys.executable, "-m", "alembic", "downgrade", "-1"]
    assert call["cwd"] == gateway_root
    assert set(call["env"]) == EXPECTED_ENV_KEYS


def test_partial_checkout_omits_absent_sibling_sources(tmp_path, invoke):
    """Case 3: a partial checkout contributes only the directories that exist."""
    without_common = _checkout(tmp_path / "no-common", common=False)

    call = invoke(without_common)

    assert _python_path(call) == [
        str(without_common),
        str(tmp_path / "no-common/modules/L5-services/src"),
    ]
    assert "adp-common" not in call["env"]["PYTHONPATH"]

    bare = _checkout(tmp_path / "bare", l5=False, common=False)

    call = invoke(bare)

    assert _python_path(call) == [str(bare)]


def test_runtime_app_ignores_unrelated_sibling_directories(tmp_path, invoke):
    """Case 4: a runtime /app layout ignores look-alike siblings that do exist."""
    app = tmp_path / "runtime/app"
    app.mkdir(parents=True)
    # Same relative shape the checkout branch would pick up, but this is not a
    # modules/gateway tree, so the installed dependencies must be used instead.
    (tmp_path / "runtime/L5-services/src").mkdir(parents=True)
    (tmp_path / "libs/python/adp-common/src").mkdir(parents=True)

    call = invoke(app)

    assert _python_path(call) == [str(app)]
    assert str(tmp_path / "runtime/L5-services/src") not in call["env"]["PYTHONPATH"]
    assert "adp-common" not in call["env"]["PYTHONPATH"]
    assert call["cwd"] == app
