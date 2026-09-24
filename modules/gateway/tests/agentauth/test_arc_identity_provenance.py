"""ARC human authority requires proven provenance, not only a timestamp."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from src.shared.models.vault import UserIdentity
from tests.agentauth.test_arc_model import arc_context as arc_context_fixture
from tests.agentauth.test_arc_model import decide
from tests.agentauth.test_arc_model import store as store_fixture

arc_context = arc_context_fixture
store = store_fixture


@pytest.mark.parametrize("method", ["self_asserted", "magic_link", "", "unknown_method"])
async def test_unproven_arc_identity_cannot_create_protected_authority(arc_context, db_session, store, method):
    await db_session.execute(update(UserIdentity).values(verification_method=method, verified_at=datetime.now(UTC)))
    await db_session.commit()
    before = store.client.scan(TableName=store.table)["Items"]

    response = await decide(arc_context)

    assert response.status_code == 403, response.text
    assert store.client.scan(TableName=store.table)["Items"] == before


@pytest.mark.parametrize("method", ["oauth", "admin_manual", "magic_link_confirmed"])
async def test_proven_arc_identity_keeps_its_human_authority(arc_context, db_session, store, method):
    await db_session.execute(update(UserIdentity).values(verification_method=method, verified_at=datetime.now(UTC)))
    await db_session.commit()

    response = await decide(arc_context)

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    grant = store.live_grant(invocation_id=result["invocation_id"], tenant_id="tenant", attempt=1, now=datetime.now(UTC))
    assert grant.authority.human_id == "human"


async def test_arc_still_requires_a_verification_timestamp(arc_context, db_session, store):
    await db_session.execute(update(UserIdentity).values(verification_method="oauth", verified_at=None))
    await db_session.commit()
    before = store.client.scan(TableName=store.table)["Items"]

    response = await decide(arc_context)

    assert response.status_code == 403, response.text
    assert store.client.scan(TableName=store.table)["Items"] == before
