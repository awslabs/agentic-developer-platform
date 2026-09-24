"""Offline actual removal -> DDB projection -> strict webhook policy review."""

import importlib
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.admin.identity.user_identity_index import UserIdentityIndexClient
from src.admin.identity_index import IdentityIndexClient
from src.admin.service import AdminService
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity


@pytest.mark.parametrize("foreign_method", ["channel_placement", "oauth"])
async def test_removing_shadow_cannot_turn_an_unproven_sibling_into_strict_policy_membership(db_session, monkeypatch, foreign_method):
    db = db_session
    for org_id in ("home", "removed", "foreign"):
        db.add(Organization(id=org_id, name=org_id))
        await db.flush()
        db.add(User(id=f"user-{org_id}", org_id=org_id, team_id="", email=f"{org_id}@example.test"))
        await db.flush()
        db.add_all(
            [
                UserIdentity(
                    user_id=f"user-{org_id}",
                    org_id=org_id,
                    team_id="",
                    provider="github",
                    provider_user_id="123",
                    verification_method={"home": "oauth", "removed": "channel_placement", "foreign": foreign_method}[org_id],
                ),
                TenantMembership(user_id=f"user-{org_id}", tenant_id=org_id, role="member"),
            ]
        )
    await db.commit()

    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_WRITE", "true")
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "true")
    monkeypatch.setenv("RESOLVE_CANONICAL_VIA_GATEWAY", "true")
    monkeypatch.setenv("IDENTITY_INDEX_TABLE", "review-removal-old")
    monkeypatch.setenv("USER_IDENTITY_INDEX_TABLE", "review-removal-new")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "agent-factory/webhook-ingress/lambda"))

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        for name, pk, sk in [
            ("review-removal-old", "identity_type", "identity_value"),
            ("review-removal-new", "provider", "provider_user_id"),
        ]:
            client.create_table(
                TableName=name,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}, {"AttributeName": sk, "AttributeType": "S"}],
            )
        writer = IdentityIndexWriter(
            client=IdentityIndexClient("review-removal-old", client),
            user_identity_client=UserIdentityIndexClient("review-removal-new", client),
        )
        assert await writer.put_user_identity(
            provider_user_id="123",
            user_id="user-home",
            org_id="home",
            verification_method="oauth",
            member_org_ids=["home"],
        )
        client.put_item(
            TableName="review-removal-old",
            Item={
                "identity_type": {"S": "github_installation_id"},
                "identity_value": {"S": "456"},
                "org_id": {"S": "foreign"},
                "trigger_policy": {"S": "home_tenant_only"},
            },
        )

        from common import agent_authority, identity_resolver

        importlib.reload(identity_resolver)
        importlib.reload(agent_authority)
        with (
            patch(
                "common.gateway_client.resolve_installation_by_id",
                return_value={"state": "resolved", "tenant_id": "foreign", "revocation_checked": True},
            ),
            patch("common.gateway_client.resolve_user_state", return_value={"state": "error", "reason": "offline-unavailable"}),
        ):
            before, reason = identity_resolver.resolve(456, 123)
            assert before is None and reason == "cross_tenant_denied"
            assert await AdminService(db).remove_user("removed", "user-removed", identity_writer=writer)

            projected = client.get_item(TableName="review-removal-new", Key={"provider": {"S": "github"}, "provider_user_id": {"S": "123"}})["Item"]
            # The removal leaves genuine identity proof intact, as it should.
            assert projected["user_id"]["S"] == "user-home"
            assert projected["verification_method"]["S"] == "oauth"
            after, reason = identity_resolver.resolve(456, 123)
            authority = None
            if after is not None:
                authority = agent_authority.VerifiedHumanEvent.from_verified_webhook(
                    body=b"{}",
                    event_type="issue_comment",
                    resolved=after,
                    sender={"type": "User"},
                    tenant_id="foreign",
                    repo="foreign/example",
                )
            print(
                f"surviving_foreign_method={foreign_method} member_org_ids={projected['member_org_ids']} "
                f"reason={reason} authority={authority is not None}"
            )
            if foreign_method == "oauth":
                assert reason == "ok" and authority is not None
            else:
                assert after is None and reason == "cross_tenant_denied", "unproven surviving identity manufactured strict-policy membership"
