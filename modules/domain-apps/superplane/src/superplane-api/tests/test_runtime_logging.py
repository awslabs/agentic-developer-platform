"""The production lifespan must protect configured outputs, including server logs."""

import io
import logging
from unittest.mock import AsyncMock

import pytest
from uvicorn.logging import AccessFormatter

from app import main


@pytest.mark.parametrize(
    "logger_name",
    ["app.services.provider_connections", "uvicorn.error", "uvicorn.access"],
)
async def test_lifespan_scrubs_real_outputs_after_handler_replacement(
    monkeypatch, logger_name
):
    for reconciler in (main.vault_sync_reconciler, main.workspace_reconciler):
        monkeypatch.setattr(reconciler, "start", AsyncMock())
        monkeypatch.setattr(reconciler, "stop", AsyncMock())
    # Alembic's fileConfig in earlier migration tests disables existing loggers.
    # Explicitly model the server's enabled output instead of inheriting that state.
    source = logging.getLogger(logger_name)
    monkeypatch.setattr(source, "disabled", False)
    monkeypatch.setattr(source, "level", logging.ERROR)
    secret = "AKIAIOSFODNN7EXAMPLE"
    for _ in range(2):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(
            logging.Formatter("%(message)s %(payload)s", defaults={"payload": ""})
        )
        output = logging.getLogger(
            "uvicorn.access"
            if logger_name == "uvicorn.access"
            else "uvicorn"
            if logger_name.startswith("uvicorn")
            else ""
        )
        monkeypatch.setattr(output, "handlers", [handler])
        monkeypatch.setattr(output, "propagate", False)
        async with main.app.router.lifespan_context(main.app):
            try:
                raise ValueError(secret)
            except ValueError:
                logging.getLogger(logger_name).error(
                    "credential=%s",
                    secret,
                    extra={"payload": {"detail": secret}},
                    exc_info=True,
                )
        emitted = stream.getvalue()
        assert emitted
        assert secret not in emitted
        assert "REDACTED" in emitted


async def test_lifespan_preserves_uvicorn_access_formatter_arguments(
    monkeypatch, capsys
):
    for reconciler in (main.vault_sync_reconciler, main.workspace_reconciler):
        monkeypatch.setattr(reconciler, "start", AsyncMock())
        monkeypatch.setattr(reconciler, "stop", AsyncMock())
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        AccessFormatter("%(request_line)s %(status_code)s", use_colors=False)
    )
    source = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(source, "handlers", [handler])
    monkeypatch.setattr(source, "disabled", False)
    monkeypatch.setattr(source, "level", logging.INFO)
    monkeypatch.setattr(source, "propagate", False)
    secret = "AKIAIOSFODNN7EXAMPLE"
    async with main.app.router.lifespan_context(main.app):
        source.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:1234",
            "GET",
            f"/?credential={secret}",
            "1.1",
            200,
        )
    emitted = stream.getvalue()
    assert secret not in emitted
    assert "GET [REDACTED] HTTP/1.1 200 OK" in emitted
    assert capsys.readouterr().err == ""
