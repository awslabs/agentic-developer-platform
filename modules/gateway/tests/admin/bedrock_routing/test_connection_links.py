"""Existing connections can grant Bedrock use without transferring vault ownership."""

from copy import deepcopy

import pytest
from sqlalchemy import delete, func, select

from src.admin.bedrock_routing import service
from src.proxy.bedrock_routing import BedrockRoutingResolver
from src.proxy.bedrock_signing import BedrockDestinationSigner
from src.shared.models.bedrock_routing import BedrockConnectionGrant, BedrockDestinationRegistry
from src.shared.models.vault import UserCredential

from .conftest import (
    MEMBER_ID,
    ORG_ID,
    OTHER_ORG_ID,
    OTHER_TEAM_ID,
    PERSONAL_ACCOUNT,
    PERSONAL_DEST,
    PLATFORM_ADMIN_ID,
    client_for,
    context_for,
    fake_secrets,
    platform_admin_context,
)

BASE = "/admin/bedrock-routing"
BODY = {"source": "shared_connection", "credential_id": "cred-personal", "link_to_org_id": OTHER_ORG_ID}


def snapshot(credential):
    return deepcopy({key: getattr(credential, key) for key in ("org_id", "user_id", "team_id", "secret_arn", "scopes", "label")})


async def test_inventory_metadata_only(session, seeded, probe_ok):
    secrets = fake_secrets()
    async with client_for(session, platform_admin_context(), secrets) as client:
        response = await client.get(f"{BASE}/connections")
    assert response.status_code == 200
    personal = next(row for row in response.json() if row["credential_id"] == "cred-personal")
    assert personal["owner_scope"] == "user"
    assert personal["selectable"] is True
    assert personal["account_id"] == PERSONAL_ACCOUNT
    assert personal["org_id"] == ORG_ID
    assert "secret_arn" not in response.text and "external_id" not in response.text and "role_arn" not in response.text
    assert not secrets.mock_calls
    probe_ok.assert_not_awaited()


@pytest.mark.parametrize("scope", ["org", "team"])
async def test_cross_org_link_routes_and_preserves_original_connection(session, seeded, probe_ok, scope):
    credential = await session.get(UserCredential, "cred-personal")
    original = snapshot(credential)
    secrets = fake_secrets()
    async with client_for(session, platform_admin_context(), secrets) as client:
        linked = await client.post(f"{BASE}/connection-links", json=BODY)
        assert linked.status_code == 201, linked.text
        destination = linked.json()["destination"]
        assert destination["owner_org_id"] == OTHER_ORG_ID
        assert destination["connection_id"] == credential.id
        assert destination["usable_for_routing"] is True
        assert destination["id"] != PERSONAL_DEST
        scope_path = f"org:{OTHER_ORG_ID}" if scope == "org" else f"team:{OTHER_ORG_ID}:{OTHER_TEAM_ID}"
        response = await client.put(f"{BASE}/mappings/{scope_path}", json={"destination_id": destination["id"]})
        assert response.status_code == 200, response.text
        wrong_org = await client.put(f"{BASE}/mappings/org:{ORG_ID}", json={"destination_id": destination["id"]})
        assert wrong_org.status_code == 422
        assert wrong_org.json()["detail"]["reason"] == "account_unlinked"
        listed = (await client.get(f"{BASE}/destinations", params={"org_id": OTHER_ORG_ID})).json()
        assert next(row for row in listed if row["id"] == destination["id"])["connection_id"] == credential.id
    await session.refresh(credential)
    assert snapshot(credential) == original
    assert probe_ok.await_count == 2
    secrets.create_secret.assert_not_called()
    secrets.delete_secret.assert_not_called()
    grant = await session.get(BedrockConnectionGrant, destination["id"])
    assert grant.created_by_user_id == PLATFORM_ADMIN_ID
    target = await BedrockRoutingResolver().resolve(session, context_for("foreign", org_id=OTHER_ORG_ID, team_id=OTHER_TEAM_ID), user_id="foreign")
    assert target.account_id == PERSONAL_ACCOUNT
    assert target.rung == scope
    # Real secret resolution reads the source owner's secret, despite different orgs.
    signer = BedrockDestinationSigner(secrets_manager=secrets)
    registry = await session.get(BedrockDestinationRegistry, destination["id"])
    assert await signer._resolve_external_id(session, registry) == "ext-4745"
    secrets.get_secret.assert_called_with(original["secret_arn"])


@pytest.mark.parametrize("reason", ["role_user_pinned_needs_v2_template", "role_missing_bedrock_permission", "assume_role_failed"])
async def test_probe_failure_saves_no_link(session, seeded, probe_ok, reason):
    probe_ok.return_value = (False, reason)
    credential = await session.get(UserCredential, "cred-personal")
    original = snapshot(credential)
    before = await session.scalar(select(func.count()).select_from(BedrockDestinationRegistry))
    async with client_for(session, platform_admin_context()) as client:
        response = await client.post(f"{BASE}/connection-links", json=BODY)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == reason
    assert "AWS account administrator" in response.json()["detail"]["message"]
    assert await session.scalar(select(func.count()).select_from(BedrockConnectionGrant)) == 0
    assert await session.scalar(select(func.count()).select_from(BedrockDestinationRegistry)) == before
    await session.refresh(credential)
    assert snapshot(credential) == original


@pytest.mark.parametrize("change", ["missing", "pending", "not_aws", "missing_role", "missing_org"])
async def test_invalid_source_or_target_refused_before_probe(session, seeded, probe_ok, change):
    body = BODY.copy()
    credential = await session.get(UserCredential, "cred-personal")
    if change == "missing":
        body["credential_id"] = "missing"
    elif change == "missing_org":
        body["link_to_org_id"] = "missing"
    elif change == "not_aws":
        credential.service = "github"
    elif change == "pending":
        credential.scopes = {**credential.scopes, "status": "pending"}
    else:
        credential.scopes = {**credential.scopes, "role_arn": None}
    await session.commit()
    async with client_for(session, platform_admin_context()) as client:
        response = await client.post(f"{BASE}/connection-links", json=body)
    assert response.status_code == 422
    probe_ok.assert_not_awaited()
    assert await session.scalar(select(func.count()).select_from(BedrockConnectionGrant)) == 0


async def test_retries_are_idempotent_but_reprobe_and_do_not_reassign_org(session, seeded, probe_ok):
    async with client_for(session, platform_admin_context()) as client:
        first = (await client.post(f"{BASE}/connection-links", json=BODY)).json()["destination"]
        retry = await client.post(f"{BASE}/connection-links", json=BODY)
        assert retry.status_code == 201
        assert retry.json()["destination"]["id"] == first["id"]
        other = (await client.post(f"{BASE}/connection-links", json={**BODY, "link_to_org_id": ORG_ID})).json()["destination"]
        assert other["id"] != first["id"]
        probe_ok.return_value = (False, "assume_role_failed")
        failed = await client.post(f"{BASE}/connection-links", json=BODY)
        assert failed.status_code == 422
    assert await session.scalar(select(func.count()).select_from(BedrockConnectionGrant)) == 2
    existing = await session.get(BedrockDestinationRegistry, first["id"])
    assert existing.owner_org_id == OTHER_ORG_ID
    assert existing.is_usable_for_routing  # Failed link retry preserves prior saved state.
    assert probe_ok.await_count == 4


async def test_self_selection_never_reuses_explicit_link(session, seeded, probe_ok):
    # Remove preexisting personal destination so the lookup has to choose carefully.
    await session.execute(delete(BedrockDestinationRegistry).where(BedrockDestinationRegistry.id == PERSONAL_DEST))
    await session.commit()
    async with client_for(session, platform_admin_context()) as client:
        for org in (ORG_ID, OTHER_ORG_ID):
            response = await client.post(f"{BASE}/connection-links", json={**BODY, "link_to_org_id": org})
            assert response.status_code == 201
    credential = await session.get(UserCredential, "cred-personal")
    personal = await service.find_or_create_destination_for_credential(session, credential, actor_id=MEMBER_ID)
    assert personal.owner_org_id == ORG_ID
    assert await session.get(BedrockConnectionGrant, personal.id) is None


async def test_unlink_refuses_used_destination_and_preserves_source(session, seeded, probe_ok):
    credential = await session.get(UserCredential, "cred-personal")
    original = snapshot(credential)
    secrets = fake_secrets()
    async with client_for(session, platform_admin_context(), secrets) as client:
        destination = (await client.post(f"{BASE}/connection-links", json=BODY)).json()["destination"]
        dest_id = destination["id"]
        path = f"{BASE}/mappings/org:{OTHER_ORG_ID}"
        assert (await client.put(path, json={"destination_id": dest_id})).status_code == 200
        assert (await client.delete(f"{BASE}/connection-links/{dest_id}")).status_code == 409
        assert (await client.delete(path)).status_code == 204
        assert (await client.delete(f"{BASE}/connection-links/{dest_id}")).status_code == 204
        assert (await client.delete(f"{BASE}/connection-links/{PERSONAL_DEST}")).status_code == 404
        assert (await client.delete(f"{BASE}/connection-links/{dest_id}")).status_code == 404
    session.expire_all()
    assert await session.get(BedrockDestinationRegistry, dest_id) is None
    assert await session.get(BedrockConnectionGrant, dest_id) is None
    assert snapshot(await session.get(UserCredential, "cred-personal")) == original
    secrets.delete_secret.assert_not_called()


async def test_deleted_source_fails_closed_and_link_can_still_be_removed(session, seeded, probe_ok):
    from src.proxy.bedrock_routing_errors import BedrockAccountUnavailableError

    async with client_for(session, platform_admin_context()) as client:
        destination = (await client.post(f"{BASE}/connection-links", json=BODY)).json()["destination"]
        await session.execute(delete(UserCredential).where(UserCredential.id == "cred-personal"))
        await session.commit()
        registry = await session.get(BedrockDestinationRegistry, destination["id"])
        with pytest.raises(BedrockAccountUnavailableError):
            await BedrockDestinationSigner()._resolve_external_id(session, registry)
        assert (await client.delete(f"{BASE}/connection-links/{destination['id']}")).status_code == 204


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/connections", None),
        ("POST", "/connection-links", BODY),
        ("DELETE", "/connection-links/missing", None),
    ],
)
async def test_unauthenticated_requests_are_denied(session, seeded, probe_ok, method, path, body):
    async with client_for(session, None) as client:
        response = await client.request(method, BASE + path, json=body)
    assert response.status_code in (401, 403)
    probe_ok.assert_not_awaited()


async def test_relink_rejects_changed_source_role_and_reverify_keeps_link_metadata(session, seeded, probe_ok):
    async with client_for(session, platform_admin_context()) as client:
        destination = (await client.post(f"{BASE}/connection-links", json=BODY)).json()["destination"]
        verified = await client.post(f"{BASE}/destinations/{destination['id']}/verify")
        assert verified.status_code == 200
        assert verified.json()["destination"]["connection_id"] == "cred-personal"
        credential = await session.get(UserCredential, "cred-personal")
        credential.scopes = {**credential.scopes, "role_arn": "arn:aws:iam::333333332210:role/changed"}
        await session.commit()
        probe_ok.reset_mock()
        retry = await client.post(f"{BASE}/connection-links", json=BODY)
        assert retry.status_code == 409
        probe_ok.assert_not_awaited()


async def test_grant_does_not_authorize_other_destinations_for_same_personal_credential(session, seeded, probe_ok):
    async with client_for(session, platform_admin_context()) as client:
        linked = await client.post(f"{BASE}/connection-links", json={**BODY, "link_to_org_id": ORG_ID})
        assert linked.status_code == 201
        probe_ok.reset_mock()
        legacy = await client.put(f"{BASE}/mappings/org:{ORG_ID}", json={"destination_id": PERSONAL_DEST})
        assert legacy.status_code == 422
        assert legacy.json()["detail"]["reason"] == "personal_credential_for_shared_scope"
        probe_ok.assert_not_awaited()
