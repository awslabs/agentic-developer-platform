"""Real API/schema/authority tests for reviewed Bedrock lifecycle writes."""

from unittest.mock import AsyncMock

from sqlalchemy import select

# Preserve the PG primitive before the SQLite harness replaces only that lock.
from src.admin.bedrock_routing.revisions import serialize_writes as postgres_serialize
from src.shared.models.bedrock_routing import BedrockAccountMapping
from tests.admin.bedrock_routing.conftest import (
    ACME_DEST,
    MEMBER_ID,
    ORG_ID,
    PERSONAL_DEST,
    PLATFORM_ADMIN_ID,
    client_for,
    member_context,
    platform_admin_context,
)


async def test_lock_uses_shared_transaction_advisory_key():
    db = AsyncMock()
    await postgres_serialize(db)
    sql, parameters = db.execute.call_args.args
    assert "pg_advisory_xact_lock" in str(sql)
    assert parameters == {"key": 56330001}


async def test_mapping_revision_refuses_newer_admin_rule(session, seeded, probe_ok):
    path = f"/admin/bedrock-routing/mappings/user:{MEMBER_ID}"
    async with client_for(session, platform_admin_context()) as client:
        initial = await client.put(path, json={"destination_id": ACME_DEST}, params={"expected_revision": "absent"})
        assert initial.status_code == 200
        revision = initial.json()["revision"]
        stale = await client.put(path, json={"destination_id": PERSONAL_DEST}, params={"expected_revision": "absent"})
        assert stale.status_code == 409
        assert stale.json()["detail"]["reason"] == "stale_revision"
        deleted = await client.delete(path, params={"expected_revision": "f" * 64})
        assert deleted.status_code == 409
        page = await client.get("/admin/bedrock-routing/mappings", params={"scope": f"user:{MEMBER_ID}", "page": 1, "page_size": 1})
    assert page.json()["items"][0]["revision"] == revision
    assert page.json()["items"][0]["destination_id"] == ACME_DEST
    assert page.json()["has_more"] is False


async def test_destination_revision_refuses_billing_substitution(session, seeded, probe_ok):
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(
            f"/admin/bedrock-routing/mappings/user:{MEMBER_ID}",
            params={"expected_revision": "absent"},
            json={"destination_id": ACME_DEST, "expected_destination_revision": "f" * 64},
        )
    assert response.status_code == 409
    assert list((await session.scalars(select(BedrockAccountMapping))).all()) == []


async def test_legacy_and_reviewed_writers_share_lock(session, seeded, probe_ok, routing_transaction_lock):
    async with client_for(session, platform_admin_context()) as client:
        result = await client.put(f"/admin/bedrock-routing/mappings/user:{MEMBER_ID}", json={"destination_id": ACME_DEST})
        assert result.status_code == 200
    routing_transaction_lock.assert_awaited_once_with(session)


async def test_stale_self_reset_cannot_remove_admin_rule(session, seeded, probe_ok):
    # Read the ordinary caller, then an administrator takes its single user rung.
    from tests.admin.bedrock_routing.test_self_selection import _client as self_client_for

    async with self_client_for(session, member_context()) as client:
        before = (await client.get("/me/bedrock-routing/selection")).json()
    async with client_for(session, platform_admin_context()) as client:
        changed = await client.put(f"/admin/bedrock-routing/mappings/user:{MEMBER_ID}", json={"destination_id": ACME_DEST})
        assert changed.status_code == 200
    async with self_client_for(session, member_context()) as client:
        response = await client.delete("/me/bedrock-routing/selection", params={"expected_revision": before["revision"]})
    assert response.status_code == 409
    mapping = await session.scalar(select(BedrockAccountMapping))
    assert mapping.authored_by_user_id == PLATFORM_ADMIN_ID
    assert mapping.destination_id == ACME_DEST


async def test_admin_mapping_list_never_returns_another_scope(session, seeded, probe_ok):
    async with client_for(session, platform_admin_context()) as client:
        await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})
        result = await client.get("/admin/bedrock-routing/mappings", params={"scope": f"user:{MEMBER_ID}", "page": 1})
    assert result.status_code == 200
    assert result.json()["items"] == []


async def test_link_targets_exact_existing_destination_and_preserves_connection(session, seeded, probe_ok):
    from src.shared.models.bedrock_routing import BedrockDestinationRegistry
    from src.shared.models.vault import UserCredential

    credential = await session.get(UserCredential, "cred-personal")
    original = dict(credential.scopes)
    async with client_for(session, platform_admin_context()) as client:
        rows = (await client.get("/admin/bedrock-routing/destinations")).json()
        before = next(row for row in rows if row["id"] == PERSONAL_DEST)
        assert before["connection_id"] is None
        assert before["source_connection_id"] == "cred-personal"
        response = await client.post(
            "/admin/bedrock-routing/connection-links",
            params={"expected_revision": before["revision"]},
            json={"source": "shared_connection", "credential_id": "cred-personal", "link_to_org_id": ORG_ID, "destination_id": PERSONAL_DEST},
        )
        assert response.status_code == 201
        linked = response.json()["destination"]
        assert linked["id"] == PERSONAL_DEST
        assert linked["connection_id"] == "cred-personal"
        stale = await client.delete(f"/admin/bedrock-routing/connection-links/{PERSONAL_DEST}", params={"expected_revision": before["revision"]})
        assert stale.status_code == 409
        removed = await client.delete(f"/admin/bedrock-routing/connection-links/{PERSONAL_DEST}", params={"expected_revision": linked["revision"]})
        assert removed.status_code == 204
    assert await session.get(BedrockDestinationRegistry, PERSONAL_DEST) is None
    credential = await session.get(UserCredential, "cred-personal")
    assert credential.scopes == original


async def test_link_refuses_mismatched_destination_before_probing(session, seeded, probe_ok):
    async with client_for(session, platform_admin_context()) as client:
        rows = (await client.get("/admin/bedrock-routing/destinations")).json()
        before = next(row for row in rows if row["id"] == ACME_DEST)
        response = await client.post(
            "/admin/bedrock-routing/connection-links",
            params={"expected_revision": before["revision"]},
            json={"source": "shared_connection", "credential_id": "cred-personal", "link_to_org_id": ORG_ID, "destination_id": ACME_DEST},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "connection_destination_mismatch"
    probe_ok.assert_not_awaited()


async def test_self_account_precondition_refuses_changed_account(session, seeded, probe_ok):
    from tests.admin.bedrock_routing.test_self_selection import _client

    async with _client(session, member_context()) as client:
        before = (await client.get("/me/bedrock-routing/selection")).json()
        response = await client.put(
            "/me/bedrock-routing/selection",
            params={"expected_revision": before["revision"]},
            json={"credential_id": "cred-personal", "expected_account_id": "999999999999"},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "connection_account_changed"
    assert list((await session.scalars(select(BedrockAccountMapping))).all()) == []
