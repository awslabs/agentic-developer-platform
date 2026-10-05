"""Provider proof must survive SQL -> DDB -> webhook authority unchanged."""

import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import select

from src.admin.identity.identities_service import IdentitiesService
from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.admin.identity.schemas import IdentityCreateRequest, UserCreateRequest, UserIdentityInput
from src.admin.identity.user_identity_index import UserIdentityIndexClient
from src.admin.identity.users_service import UsersService
from src.admin.identity_index import IdentityIndexClient
from src.shared.identity.providers import SUPPORTED_PROVIDERS
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserIdentity

NON_GITHUB_PROVIDERS = sorted(SUPPORTED_PROVIDERS - {"github"})


@pytest.fixture
async def seeded_db(db_session):
    db_session.add_all(
        [
            Organization(id="org-acme", name="Acme", aws_accounts=[], settings={}),
            Department(id="dept-eng", org_id="org-acme", name="Eng"),
            Team(id="team-eng", org_id="org-acme", department_id="dept-eng", name="Eng"),
            User(id="user-local", org_id="org-acme", team_id="team-eng", email="local@example.test", role="member"),
        ]
    )
    await db_session.commit()
    return db_session


@pytest.fixture
def projection(monkeypatch):
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_WRITE", "true")
    monkeypatch.setenv("IDENTITY_INDEX_TABLE", "inert-old")
    monkeypatch.setenv("USER_IDENTITY_INDEX_TABLE", "inert-new")
    monkeypatch.setenv("RESOLVE_CANONICAL_VIA_GATEWAY", "true")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        for name, pk, sk in [("inert-old", "identity_type", "identity_value"), ("inert-new", "provider", "provider_user_id")]:
            client.create_table(
                TableName=name,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}, {"AttributeName": sk, "AttributeType": "S"}],
            )
        client.put_item(
            TableName="inert-old",
            Item={"identity_type": {"S": "github_installation_id"}, "identity_value": {"S": "123456"}, "org_id": {"S": "org-acme"}},
        )
        writer = IdentityIndexWriter(
            client=IdentityIndexClient("inert-old", client), user_identity_client=UserIdentityIndexClient("inert-new", client)
        )
        yield writer, client


@pytest.fixture
def webhook(monkeypatch, projection):
    """Load this checkout's Lambda modules without leaking their caches to other tests."""
    saved = {name: module for name, module in sys.modules.items() if name == "common" or name.startswith("common.")}
    for name in saved:
        del sys.modules[name]
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "agent-factory/webhook-ingress/lambda"))
    try:
        resolver = importlib.import_module("common.identity_resolver")
        authority = importlib.import_module("common.agent_authority")
        with (
            patch(
                "common.gateway_client.resolve_installation_by_id",
                return_value={"state": "resolved", "tenant_id": "org-acme", "revocation_checked": True},
            ),
            patch("common.gateway_client.resolve_user_state", return_value={"state": "error", "reason": "inert-unavailable"}),
        ):
            yield resolver, authority
    finally:
        for name in list(sys.modules):
            if name == "common" or name.startswith("common."):
                del sys.modules[name]
        sys.modules.update(saved)


def _legacy(client):
    return client.get_item(TableName="inert-old", Key={"identity_type": {"S": "github_user"}, "identity_value": {"S": "999"}}).get("Item")


def _v2(client, provider):
    return client.get_item(TableName="inert-new", Key={"provider": {"S": provider}, "provider_user_id": {"S": "999"}}).get("Item")


def _human_event(webhook):
    resolver, authority = webhook
    resolved, reason = resolver.resolve(123456, 999)
    if resolved is None:
        assert reason == "unknown_user"
        return None
    assert reason == "ok"
    return authority.VerifiedHumanEvent.from_verified_webhook(
        body=b"{}", event_type="issue_comment", resolved=resolved, sender={"type": "User"}, tenant_id="org-acme", repo="inert/repo"
    )


@pytest.mark.parametrize("v2_read", [False, True])
@pytest.mark.parametrize("provider", sorted(SUPPORTED_PROVIDERS))
async def test_admin_identity_proof_only_authorizes_its_provider(seeded_db, projection, webhook, monkeypatch, provider, v2_read):
    """A gateway outage must never turn another provider's manual proof into GitHub authority."""
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", str(v2_read).lower())
    writer, client = projection
    response = await IdentitiesService(seeded_db, writer).add_identity("user-local", IdentityCreateRequest(provider=provider, provider_user_id="999"))
    row = await seeded_db.scalar(select(UserIdentity).where(UserIdentity.id == response.id))
    assert (row.provider, row.verification_method) == (provider, "admin_attested")
    assert row.verified_at is not None

    event = _human_event(webhook)
    if provider == "github":
        assert event.human_id == "user-local"
        assert _legacy(client)["verification_method"] == {"S": "admin_attested"}
    else:
        assert event is None, f"{provider} proof granted GitHub authority to {event.human_id}"
        assert _legacy(client) is None
        assert _v2(client, "github") is None
    assert _v2(client, provider)["user_id"] == {"S": row.user_id}
    assert _v2(client, provider)["verification_method"] == {"S": row.verification_method}


@pytest.fixture
def cognito_sync():
    sync = AsyncMock()
    sync.create_user_and_invite.return_value = {"Username": "inert-login", "Attributes": [{"Name": "sub", "Value": "inert-sub"}]}
    return sync


@pytest.mark.parametrize("provider", sorted(SUPPORTED_PROVIDERS - {"cognito"}))
async def test_create_user_sync_preserves_provider_and_proof(seeded_db, projection, webhook, monkeypatch, cognito_sync, provider):
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "true")
    writer, client = projection
    user = await UsersService(seeded_db, cognito_sync=cognito_sync, identity_writer=writer).create_user(
        "org-acme",
        UserCreateRequest(
            email="new@example.com",
            team_id="team-eng",
            send_invite=False,
            identities=[UserIdentityInput(provider=provider, provider_user_id="999")],
        ),
    )
    row = await seeded_db.scalar(select(UserIdentity).where(UserIdentity.user_id == user.id, UserIdentity.provider == provider))
    assert (row.provider, row.verification_method) == (provider, "admin_attested")
    assert row.verified_at is not None
    event = _human_event(webhook)
    if provider == "github":
        assert event.human_id == user.id
        assert _v2(client, provider)["member_org_ids"] == {"L": [{"S": "org-acme"}]}
    else:
        assert event is None, f"{provider} proof granted GitHub authority to {event.human_id}"
        assert _legacy(client) is None
        assert _v2(client, "github") is None
    assert _v2(client, provider)["user_id"] == {"S": row.user_id}
    assert _v2(client, provider)["verification_method"] == {"S": row.verification_method}


@pytest.mark.parametrize("provider", NON_GITHUB_PROVIDERS)
@pytest.mark.parametrize("memberships", [None, ["org-other"]])
async def test_other_provider_mutations_preserve_same_id_github_rows(projection, provider, memberships):
    writer, client = projection
    await writer.put_user_identity("999", "user-github", "org-acme", member_org_ids=["org-acme"], verification_method="oauth")
    before = (_legacy(client), _v2(client, "github"))

    assert await writer.put_user_identity(
        "999", "user-other", "org-other", provider=provider, member_org_ids=memberships, verification_method="admin_manual"
    )
    assert (_legacy(client), _v2(client, "github")) == before
    assert _v2(client, provider)["user_id"] == {"S": "user-other"}
    assert await writer.update_user_membership_orgs("999", ["org-other", "org-extra"], provider=provider)
    assert (_legacy(client), _v2(client, "github")) == before
    assert _v2(client, provider)["member_org_ids"] == {"L": [{"S": "org-other"}, {"S": "org-extra"}]}
    assert await writer.delete_user_identity("999", provider=provider)
    assert _v2(client, provider) is None
    assert (_legacy(client), _v2(client, "github")) == before


@pytest.mark.parametrize("provider", ["github", "slack", "cognito"])
async def test_identity_deletion_uses_sql_provider(seeded_db, projection, provider):
    writer, client = projection
    service = IdentitiesService(seeded_db, writer)
    created = await service.add_identity("user-local", IdentityCreateRequest(provider=provider, provider_user_id="999"))
    if provider != "github":
        await writer.put_user_identity("999", "user-github", "org-acme", verification_method="oauth")
    before = (_legacy(client), _v2(client, "github"))

    assert await service.delete_identity("user-local", created.id)
    assert await seeded_db.scalar(select(UserIdentity).where(UserIdentity.id == created.id)) is None
    assert _v2(client, provider) is None
    if provider == "github":
        assert _legacy(client) is None
    else:
        assert (_legacy(client), _v2(client, "github")) == before


async def test_user_deletion_cleans_each_provider_without_deleting_github(seeded_db, projection, cognito_sync):
    writer, client = projection
    service = IdentitiesService(seeded_db, writer)
    for provider in ("slack", "teams"):
        await service.add_identity("user-local", IdentityCreateRequest(provider=provider, provider_user_id="999"))
    await writer.put_user_identity("999", "user-github", "org-acme", verification_method="oauth")
    before = (_legacy(client), _v2(client, "github"))

    assert await UsersService(seeded_db, cognito_sync=cognito_sync, identity_writer=writer).delete_user("org-acme", "user-local")
    assert await seeded_db.get(User, "user-local") is None
    assert _v2(client, "slack") is None
    assert _v2(client, "teams") is None
    assert (_legacy(client), _v2(client, "github")) == before


async def _mutate(writer, operation, provider):
    if operation == "membership":
        return await writer.update_user_membership_orgs("999", ["org-other"], provider=provider)
    if operation == "delete":
        return await writer.delete_user_identity("999", provider=provider)
    return await writer.put_user_identity(
        "999",
        "user-other",
        "org-other",
        provider=provider,
        member_org_ids=["org-other"] if operation == "put" else None,
        verification_method="admin_manual",
    )


@pytest.mark.parametrize("operation", ["put", "core", "membership", "delete"])
@pytest.mark.parametrize("provider", ["slack", "cognito"])
async def test_v2_disabled_other_provider_operations_leave_both_tables_unchanged(projection, monkeypatch, operation, provider):
    writer, client = projection
    await writer.put_user_identity("999", "user-github", "org-acme", verification_method="oauth")
    await writer.put_user_identity("999", "user-other", "org-acme", provider=provider, verification_method="oauth")
    before = (_legacy(client), _v2(client, "github"), _v2(client, provider))
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_WRITE", "false")
    assert await _mutate(writer, operation, provider)
    assert (_legacy(client), _v2(client, "github"), _v2(client, provider)) == before


@pytest.mark.parametrize("operation", ["put", "core", "membership", "delete"])
@pytest.mark.parametrize("provider", ["github", "slack", "cognito"])
@pytest.mark.parametrize("raises", [False, True])
async def test_v2_failure_is_reported_when_no_legacy_write_exists(monkeypatch, operation, provider, raises):
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_WRITE", "true")
    old = AsyncMock(spec=IdentityIndexClient)
    new = AsyncMock(spec=UserIdentityIndexClient)
    for name in ("put_identity", "update_user_identity_core", "update_membership_orgs", "delete_identity"):
        getattr(old, name).return_value = True
    for name in ("put_user_identity", "update_user_core_attrs", "update_membership_orgs", "delete_user_identity"):
        method = getattr(new, name)
        method.return_value = False
        if raises:
            method.side_effect = RuntimeError("inert DDB failure")
    writer = IdentityIndexWriter(client=old, user_identity_client=new)
    assert await _mutate(writer, operation, provider) is (provider == "github")
    if provider != "github":
        assert old.mock_calls == []


@pytest.mark.parametrize("operation", ["put", "core", "membership", "delete"])
@pytest.mark.parametrize("provider", ["unsupported", "github_app_register"])
async def test_invalid_provider_cannot_mutate_any_projection(monkeypatch, operation, provider):
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_WRITE", "true")
    old = AsyncMock(spec=IdentityIndexClient)
    new = AsyncMock(spec=UserIdentityIndexClient)
    with pytest.raises(ValueError, match="Unsupported provider"):
        await _mutate(IdentityIndexWriter(client=old, user_identity_client=new), operation, provider)
    assert old.mock_calls == []
    assert new.mock_calls == []


async def test_legacy_bulk_delete_defaults_to_github(projection):
    writer, client = projection
    await writer.put_user_identity("999", "user-github", "org-acme", verification_method="oauth")
    await writer.put_user_identity("999", "user-other", "org-acme", provider="slack", verification_method="oauth")
    slack = _v2(client, "slack")
    await writer.delete_all_user_identities(["999"])
    assert _legacy(client) is None
    assert _v2(client, "github") is None
    assert _v2(client, "slack") == slack
