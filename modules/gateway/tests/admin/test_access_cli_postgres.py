"""Concurrent HTTP decisions use the real dedicated PostgreSQL lock."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from src.admin.onboarding import handler
from src.shared.models.base import Base
from tests.admin.test_admin_audit_postgres import audit_postgres  # noqa: F401
from tests.admin.test_onboarding_access_request_scope import OWN_ORG, _client, _context, seeded  # noqa: F401


@pytest.fixture
async def db_engine(audit_postgres):  # noqa: F811
    engine = create_async_engine(audit_postgres, connect_args={"server_settings": {"statement_timeout": "10000"}})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def test_identical_concurrent_decision_has_one_receipt(seeded, monkeypatch):  # noqa: F811
    monkeypatch.setattr(handler, "_determine_role_for_matched_user", AsyncMock(return_value="member"))
    async with _client(seeded, _context("sub-org-admin", OWN_ORG)) as client:
        row = (await client.get("/admin/access-requests/req-own-b/review")).json()
        body = {
            "operation_id": str(uuid4()),
            "expected_revision": row["revision"],
            "expected_role": row["proposed_role"],
            "expected_scope": row["requested_scope"],
            "decision_note": "Concurrent fixture",
        }
        path = "/admin/access-requests/req-own-b/deny/revision"
        first, second = await asyncio.gather(client.post(path, json=body), client.post(path, json=body))
        assert first.status_code == second.status_code == 200, (first.text, second.text)
        assert first.json() == second.json()
        assert first.json()["status"] == "denied"


async def test_postgres_reviewed_revocation_preserves_new_token(seeded, monkeypatch):  # noqa: F811 -- shared fixture
    from tests.admin.test_access_cli_contract import exercise_reviewed_token_mint_race

    await exercise_reviewed_token_mint_race(seeded, monkeypatch)
