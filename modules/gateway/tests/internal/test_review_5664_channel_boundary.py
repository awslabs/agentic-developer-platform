"""Offline review: an explicit tenant must also constrain channel placement."""

import asyncio

import pytest
from sqlalchemy import select

from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity
from tests.internal.test_resolve_user_trust import (  # noqa: F401
    _resolve,
    client,
    db,
    engine,
)


def test_mapped_channel_cannot_provision_in_a_different_requested_tenant(client, db):  # noqa: F811
    # The fixture maps T-mapped exclusively to org-a. The caller pins org-b.
    response = _resolve(
        client,
        provider="slack",
        provider_user_id="U-foreign-boundary",
        channel_context="T-mapped",
        org_id="org-b",
    )

    async def read_created():
        identities = (await db.scalars(select(UserIdentity))).all()
        users = (await db.scalars(select(User).where(User.is_shadow.is_(True)))).all()
        return len(identities), len(users)

    created = asyncio.get_event_loop().run_until_complete(read_created())
    print(f"requested_tenant=org-b response={response.status_code}:{response.json()} created={created}")
    assert response.status_code == 403, "channel fallback crossed the request's explicit tenant boundary"
    assert response.json()["detail"]["error"] == "channel_tenant_mismatch"
    assert created == (0, 0)


@pytest.mark.parametrize("org_id", [None, "org-a"])
def test_matching_or_unscoped_channel_placement_stays_idempotent(client, db, org_id):  # noqa: F811
    body = {"provider": "slack", "provider_user_id": "U-local-boundary", "channel_context": "T-mapped", "org_id": org_id}
    first = _resolve(client, **body)
    second = _resolve(client, **body)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["user_id"] == second.json()["user_id"]
    assert first.json()["org_id"] == second.json()["org_id"] == "org-a"
    assert first.json()["verification_method"] == second.json()["verification_method"] == "channel_placement"

    async def read_created():
        return list((await db.scalars(select(UserIdentity))).all())

    identities = asyncio.get_event_loop().run_until_complete(read_created())
    assert len(identities) == 1
    assert identities[0].verified_at is None
