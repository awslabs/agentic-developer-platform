"""Fail-closed coverage for the managed database URL."""

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest
from app.config import (
    DatabaseURLMissing,
    Settings,
    require_database_url,
    settings,
)

API_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = API_ROOT / "app"


def test_settings_has_no_database_credential_fallback(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings(_env_file=None).database_url == ""


@pytest.mark.parametrize("value", ["", "   \n\t"])
def test_missing_or_blank_database_url_is_refused(monkeypatch, value: str) -> None:
    monkeypatch.setattr(settings, "database_url", value)
    with pytest.raises(DatabaseURLMissing, match="DATABASE_URL"):
        require_database_url()


def test_database_refusal_does_not_echo_rejected_contents(monkeypatch) -> None:
    rejected = " \t\n "
    monkeypatch.setattr(settings, "database_url", rejected)
    with pytest.raises(DatabaseURLMissing) as exc_info:
        require_database_url()
    assert rejected not in str(exc_info.value)


def test_startup_refuses_a_missing_database_url(monkeypatch) -> None:
    import anyio
    from app.main import app, lifespan

    monkeypatch.setattr(settings, "database_url", "")

    async def enter_lifespan() -> None:
        async with lifespan(app):
            pytest.fail("startup completed without DATABASE_URL")

    with pytest.raises(DatabaseURLMissing):
        anyio.run(enter_lifespan)


def test_alembic_configuration_has_no_embedded_database_url() -> None:
    contents = (API_ROOT / "alembic.ini").read_text(encoding="utf-8")
    assert "sqlalchemy.url =\n" in contents


@pytest.mark.parametrize(
    "probe",
    [
        "import app.database",
        (
            "from alembic import command; from alembic.config import Config; "
            "command.upgrade(Config('alembic.ini'), 'head', sql=True)"
        ),
    ],
)
def test_engine_and_migrations_fail_closed_in_a_clean_process(probe: str) -> None:
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=API_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "DATABASE_URL is not set" in result.stderr


def test_database_url_has_one_guarded_reader() -> None:
    resolver_source = inspect.getsource(require_database_url)
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            code = line.split("#", 1)[0]
            if "settings.database_url" not in code:
                continue
            if code.strip() in resolver_source:
                continue
            offenders.append(f"{path.relative_to(APP_ROOT)}:{number}")
    assert not offenders, (
        "database users must call require_database_url() so blank values fail closed: "
        f"{offenders}"
    )
