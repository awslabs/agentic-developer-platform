"""Review reproduction: installing a shared app must preserve earlier bot routing."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import boto3
import pytest
from fastapi import Request
from moto import mock_aws
from sqlalchemy import select

from src.admin.connections import bot_identity
from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.admin.identity.user_identity_index import UserIdentityIndexClient
from src.admin.identity_index import IdentityIndexClient
from src.shared.models.organization import User


@pytest.mark.parametrize("v2", [False, True])
async def test_second_install_preserves_first_tenant_bot_resolution(db_session, monkeypatch, v2):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "agent-factory/webhook-ingress/lambda"))
    from common import identity_resolver

    installation_owners = {"111": "org-a", "222": "org-b", "333": "org-unrelated"}
    monkeypatch.setattr(
        importlib.import_module("common.gateway_client"),
        "resolve_installation_by_id",
        lambda iid: {"state": "resolved", "tenant_id": installation_owners[str(iid)], "revocation_checked": True},
    )

    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_WRITE", str(v2).lower())
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", str(v2).lower())
    monkeypatch.setenv("RESOLVE_CANONICAL_VIA_GATEWAY", "false")
    old_table = f"review-identity-{uuid4().hex}"
    new_table = f"review-users-{uuid4().hex}"
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        old = ddb.create_table(
            TableName=old_table,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "identity_type", "KeyType": "HASH"}, {"AttributeName": "identity_value", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "identity_type", "AttributeType": "S"},
                {"AttributeName": "identity_value", "AttributeType": "S"},
            ],
        )
        ddb.create_table(
            TableName=new_table,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "provider", "KeyType": "HASH"}, {"AttributeName": "provider_user_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "provider", "AttributeType": "S"}, {"AttributeName": "provider_user_id", "AttributeType": "S"}],
        )
        client = boto3.client("dynamodb", region_name="us-east-1")
        writer = IdentityIndexWriter(
            client=IdentityIndexClient(table_name=old_table, dynamodb_client=client),
            user_identity_client=UserIdentityIndexClient(table_name=new_table, dynamodb_client=client),
        )
        monkeypatch.setattr(bot_identity, "IdentityIndexWriter", lambda: writer)
        monkeypatch.setattr(identity_resolver, "_dynamodb", ddb)
        monkeypatch.setattr(identity_resolver, "_cloudwatch", MagicMock())
        monkeypatch.setattr(identity_resolver, "IDENTITY_INDEX_TABLE", old_table)
        monkeypatch.setattr(identity_resolver, "USER_IDENTITY_INDEX_TABLE", new_table)
        for installation, org in [(111, "org-a"), (222, "org-b"), (333, "org-unrelated")]:
            old.put_item(
                Item={
                    "identity_type": "github_installation_id",
                    "identity_value": str(installation),
                    "org_id": org,
                    "user_provisioning_mode": "strict",
                    "trigger_policy": "home_tenant_only",
                }
            )
        github = MagicMock()
        github.get_bot_user = AsyncMock(return_value={"id": 424242, "login": "shared-platform[bot]", "type": "Bot"})
        await bot_identity.seed_bot_identity(installation_id=111, org_id="org-a", app_slug="shared-platform", github_client=github, db=db_session)
        initial_identity, initial_reason = identity_resolver.resolve(111, 424242)
        assert initial_reason == "ok"
        initial_user_id = initial_identity.user_id

        await bot_identity.seed_bot_identity(installation_id=111, org_id="org-b", app_slug="shared-platform", github_client=github, db=db_session)
        later_identity, later_reason = identity_resolver.resolve(111, 424242)
        users = (await db_session.execute(select(User).where(User.bot_kind == "shared-platform"))).scalars().all()
        current = old.get_item(Key={"identity_type": "github_user", "identity_value": "424242"})["Item"]
        assert later_reason == "ok", "A second tenant installation broke bot resolution for the first tenant"
        assert later_identity.user_id == initial_user_id
        assert len(users) == 1
        assert current["org_id"] == "org-a"
        assert current["member_org_ids"] == ["org-a", "org-b"]
        second, reason = identity_resolver.resolve(222, 424242)
        assert reason == "ok"
        assert second.user_id == initial_user_id
        assert identity_resolver.resolve(333, 424242) == (None, "cross_tenant_denied")

        # Canonical Postgres resolution must agree with both DDB read paths.
        from src.internal.routes import ResolveUserRequest, resolve_user

        request = Request({"type": "http"})
        request.state.token_context = SimpleNamespace(
            auth_source="iam",
            user_id="iam-agent:ingress",
            scope="internal",
            org_id="org-a",
            credential_scopes=["internal:identity:resolve"],
        )
        canonical = await resolve_user(ResolveUserRequest(provider="github", provider_user_id="424242"), request=request, db=db_session, _=None)
        assert canonical.user_id == initial_user_id
        assert canonical.org_id == "org-a"

        # A reinstall must preserve the complete membership set and user ID.
        await bot_identity.seed_bot_identity(installation_id=111, org_id="org-a", app_slug="shared-platform", github_client=github, db=db_session)
        assert identity_resolver.resolve(222, 424242)[0].user_id == initial_user_id
