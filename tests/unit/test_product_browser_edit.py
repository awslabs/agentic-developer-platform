"""Offline contract checks for Product edits and denial oracles."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import re
from unittest.mock import AsyncMock

import pytest

from tests.e2e.new_ui.test_product_profile import (
    assert_denied_read,
    assert_revoked_read,
    patch_product,
    product_path,
)


class Response:
    def __init__(self, status, text=""):
        self.status = status
        self.body = text

    def text(self):
        return self.body

    def json(self):
        return json.loads(self.body)


class Request:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def patch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_concurrent_edit_uses_revision_and_unique_receipt():
    request = Request(Response(200))
    path = product_path("owned/product", "owned project")
    assert path == "/products/owned%2Fproduct/projects/owned%20project"
    assert patch_product(request, "https://example.invalid", {"access_token": "fixture-only"}, path, 7, "new") is request.response
    url, options = request.calls[0]
    assert url.endswith(path)
    assert options["headers"]["If-Profile-Revision"] == "7"
    assert re.fullmatch(r"[a-f0-9]{32}", options["headers"]["Idempotency-Key"])
    assert options["data"] == {"purpose": "new"}
    assert options["timeout"] == 30_000


@pytest.mark.parametrize("status,body", [(200, ""), (403, ""), (503, "")])
def test_nonmember_read_must_return_not_found(status, body):
    with pytest.raises(AssertionError, match="did not deny access"):
        assert_denied_read(Response(status, body), ("private-name",))


def test_denial_body_cannot_disclose_saved_fields():
    assert_denied_read(Response(404, '{"detail":"not_found"}'), ("private-name", "private-purpose"))
    with pytest.raises(AssertionError, match="disclosed"):
        assert_denied_read(Response(404, "private-purpose"), ("private-name", "private-purpose"))


def test_browser_denial_matches_real_membership_revocation_guard(monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.admin import membership_revocation
    from src.shared.models.base import Base
    from src.shared.models.organization import Organization, User
    from src.shared.schemas.auth import TokenContext

    monkeypatch.setattr(membership_revocation, "project_member_org_ids", AsyncMock())

    async def exercise():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                session.add(Organization(id="fixture-tenant", name="eval-regression-browser"))
                session.add(User(id="fixture-owner", org_id="fixture-tenant", team_id="", email="owner@example.test", user_kind="human"))
                await session.commit()
                context = TokenContext(user_id="fixture-owner", org_id="fixture-tenant", team_id="", department_id="",
                                       account_type="human", is_admin=False, expires_at=datetime.now(UTC) + timedelta(hours=1))
                await membership_revocation.require_not_revoked_context(context, session)
                await membership_revocation.revoke_membership(session, org_id=context.org_id, user_id=context.user_id)
                with pytest.raises(HTTPException) as denied:
                    await membership_revocation.require_not_revoked_context(context, session)
                response = Response(denied.value.status_code, json.dumps({"detail": denied.value.detail}))
                assert_revoked_read(response, ("private-name", "private-purpose"))
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("status,body", [
    (404, {"detail": {"error": "tenant_membership_revoked"}}),
    (403, {"detail": {"error": "access_denied"}}),
    (200, {"detail": {"error": "tenant_membership_revoked"}}),
    (403, {"detail": {"error": "tenant_membership_revoked", "message": "private-purpose"}}),
])
def test_revoked_denial_requires_canonical_error_and_no_disclosure(status, body):
    with pytest.raises(AssertionError):
        assert_revoked_read(Response(status, json.dumps(body)), ("private-name", "private-purpose"))
