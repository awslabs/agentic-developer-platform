"""Single-installation routing repair: attestation, transaction and audit boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from pydantic import ValidationError

from src.admin.audit_operation import Operation, current_operation
from src.admin.identity_index import IdentityIndexClient
from src.admin.installations.resolver import OwnerState
from src.admin.org_connections import routes, service
from src.admin.org_connections.schemas import ReconcileRoutingRequest
from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap


def test_repair_request_refuses_caller_selected_destination():
    with pytest.raises(ValidationError):
        ReconcileRoutingRequest(expected_projection_org_id="historical", desired_org_id="attacker")


@pytest.mark.asyncio
async def test_attested_canonical_binding_repairs_only_forward_projection(db_session, monkeypatch):
    db_session.add(
        Organization(
            id="canonical",
            name="Canonical",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=["42"],
            github_org_id="98765",
        )
    )
    db_session.add(
        ChannelTenantMap(
            provider="github",
            provider_scope_id="98765",
            installation_id="42",
            org_id="canonical",
            install_metadata={"account_id": "98765"},
        )
    )
    await db_session.commit()

    github = MagicMock()
    github.get_installation = AsyncMock(return_value={"account": {"id": 98765}})
    github.aclose = AsyncMock()
    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("1", "key"))
    monkeypatch.setattr("src.admin.connections.github_client.GitHubAppClient", lambda *args: github)
    projection = Projection(extra={"trigger_policy": {"S": "deny"}})
    monkeypatch.setattr(
        "src.admin.identity_index.IdentityIndexClient", lambda: IdentityIndexClient(table_name="test-only", dynamodb_client=projection)
    )

    assert await service.reconcile_routing("canonical", 42, "historical", db_session) == ("repaired", "historical")
    github.get_installation.assert_awaited_once_with(42)
    assert projection.item["org_id"] == {"S": "canonical"}
    assert projection.item["trigger_policy"] == {"S": "deny"}
    binding = await db_session.get(Organization, "canonical")
    assert binding.github_installation_ids == ["42"]
    assert binding.github_org_id == "98765"


class Projection:
    def __init__(self, owner="historical", *, revoked=False, extra=None):
        self.item = (
            {"identity_type": {"S": "github_installation_id"}, "identity_value": {"S": "42"}, "org_id": {"S": owner}, **(extra or {})}
            if owner
            else None
        )
        self.revoked = revoked
        self.before_transaction = None
        self.calls = []

    def get_item(self, **kwargs):
        assert kwargs["ConsistentRead"] is True
        return {"Item": self.item.copy()} if self.item else {}

    def transact_write_items(self, **kwargs):
        self.calls.append(kwargs)
        if self.before_transaction:
            self.before_transaction()
        guard, operation = kwargs["TransactItems"]
        assert guard["ConditionCheck"]["Key"]["identity_type"]["S"] == "github_installation_revoked"
        assert guard["ConditionCheck"]["ConditionExpression"] == "attribute_not_exists(identity_type)"
        effect = operation.get("Update") or operation.get("ConditionCheck")
        assert effect["ConditionExpression"] == "attribute_exists(identity_type) AND org_id = :observed"
        if self.revoked or not self.item or self.item["org_id"] != effect["ExpressionAttributeValues"][":observed"]:
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException", "Message": "refused"},
                    "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
                },
                "TransactWriteItems",
            )
        if "Update" in operation:
            assert effect["UpdateExpression"] == "SET org_id = :canonical, updated_at = :now"
            self.item["org_id"] = effect["ExpressionAttributeValues"][":canonical"]
            self.item["updated_at"] = effect["ExpressionAttributeValues"][":now"]


@pytest.mark.asyncio
async def test_repair_preserves_policy_and_replay_checks_revocation():
    projection = Projection(extra={"trigger_policy": {"S": "deny"}, "min_author_association": {"S": "OWNER"}})
    index = IdentityIndexClient(table_name="test-only", dynamodb_client=projection)
    assert await index.reconcile_installation_routing(42, "historical", "canonical") == ("repaired", "historical")
    assert projection.item["org_id"] == {"S": "canonical"}
    assert projection.item["trigger_policy"] == {"S": "deny"}
    assert projection.item["min_author_association"] == {"S": "OWNER"}
    assert await index.reconcile_installation_routing(42, "historical", "canonical") == ("already_consistent", "canonical")
    assert "Update" not in projection.calls[-1]["TransactItems"][1]
    projection.revoked = True
    assert await index.reconcile_installation_routing(42, "historical", "canonical") == ("concurrent_change_or_revocation", "canonical")


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["revoked", "new_owner", "missing"])
async def test_transaction_rejects_racing_changes(event):
    projection = Projection()
    if event == "revoked":
        projection.before_transaction = lambda: setattr(projection, "revoked", True)
    elif event == "new_owner":
        projection.before_transaction = lambda: projection.item.update(org_id={"S": "other"})
    else:
        projection.before_transaction = lambda: setattr(projection, "item", None)
    index = IdentityIndexClient(table_name="test-only", dynamodb_client=projection)
    assert (await index.reconcile_installation_routing(42, "historical", "canonical"))[0] == "concurrent_change_or_revocation"
    assert not projection.item or projection.item.get("org_id") != {"S": "canonical"}


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,outcome", [(None, "missing_projection"), ("unknown", "stale_expectation")])
async def test_missing_or_unexpected_row_refused_without_transaction(owner, outcome):
    projection = Projection(owner=owner)
    index = IdentityIndexClient(table_name="test-only", dynamodb_client=projection)
    assert (await index.reconcile_installation_routing(42, "historical", "canonical"))[0] == outcome
    assert projection.calls == []


@pytest.mark.asyncio
async def test_unreadable_row_is_not_treated_as_missing():
    projection = Projection()
    projection.get_item = MagicMock(side_effect=ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetItem"))
    with pytest.raises(ClientError):
        await IdentityIndexClient(table_name="test-only", dynamodb_client=projection).reconcile_installation_routing(42, "historical", "canonical")


@pytest.mark.asyncio
async def test_audit_effect_boundary_starts_only_at_transaction():
    operation = Operation("reconcile_github_routing", "/admin/organizations/{org_id}/connections/github/{installation_id}/reconcile-routing")
    token = current_operation.set(operation)
    try:
        missing = Projection(owner=None)
        assert (
            await IdentityIndexClient(table_name="test-only", dynamodb_client=missing).reconcile_installation_routing(42, "historical", "canonical")
        )[0] == "missing_projection"
        assert operation.effects_started is False
        assert (
            await IdentityIndexClient(table_name="test-only", dynamodb_client=Projection()).reconcile_installation_routing(
                42, "historical", "canonical"
            )
        )[0] == "repaired"
        assert operation.effects_started is True
    finally:
        current_operation.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,tenant,reason",
    [
        (OwnerState.REVOKED, None, "installation_revoked"),
        (OwnerState.RESOLVED, "other", "canonical_owner_differs_from_target"),
        (OwnerState.UNATTESTABLE, None, "ownership_unattestable"),
        (OwnerState.AMBIGUOUS, None, "ownership_ambiguous"),
    ],
)
async def test_service_refuses_without_projection_write(monkeypatch, state, tenant, reason):
    client = MagicMock()
    client.aclose = AsyncMock()
    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("1", "key"))
    monkeypatch.setattr("src.admin.connections.github_client.GitHubAppClient", lambda *args: client)
    resolve = AsyncMock(return_value=(SimpleNamespace(tenant_id=tenant, attested=True) if tenant else None, state))
    monkeypatch.setattr("src.admin.installations.resolver.resolve_installation_owner", resolve)
    index = MagicMock()
    monkeypatch.setattr("src.admin.identity_index.IdentityIndexClient", index)
    with pytest.raises(service.RoutingReconciliationRefusedError, match=reason) as error:
        await service.reconcile_routing("canonical", 42, "historical", MagicMock())
    if reason == "canonical_owner_differs_from_target":
        assert error.value.authoritative_org_id == "other"
    resolve.assert_awaited_once()
    assert resolve.await_args.kwargs["attest"] is True
    client.aclose.assert_awaited_once()
    index.assert_not_called()


@pytest.mark.asyncio
async def test_service_refuses_github_unavailable(monkeypatch):
    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("", ""))
    with pytest.raises(service.RoutingReconciliationRefusedError, match="github_app_credentials_unavailable"):
        await service.reconcile_routing("canonical", 42, "historical", MagicMock())

    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("1", "key"))
    client = MagicMock(aclose=AsyncMock())
    monkeypatch.setattr("src.admin.connections.github_client.GitHubAppClient", lambda *args: client)
    monkeypatch.setattr("src.admin.installations.resolver.resolve_installation_owner", AsyncMock(side_effect=PermissionError("GitHub denied")))
    with pytest.raises(service.RoutingReconciliationRefusedError, match="github_attestation_unavailable"):
        await service.reconcile_routing("canonical", 42, "historical", MagicMock())
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_audits_repair_and_refusal(monkeypatch):
    monkeypatch.setattr(routes, "_require_platform_admin", MagicMock())
    actor = SimpleNamespace(user_id="admin", org_id="platform")
    audit = AsyncMock()
    monkeypatch.setattr(routes, "write_admin_audit", audit)
    monkeypatch.setattr(routes, "reconcile_routing", AsyncMock(return_value=("repaired", "historical")))
    request = ReconcileRoutingRequest(expected_projection_org_id="historical")
    response = await routes.reconcile_github_routing("canonical", 42, request, MagicMock(), actor)
    assert response.outcome == "repaired"
    assert audit.await_args.kwargs["extra"] == {
        "expected_projection_org_id": "historical",
        "observed_projection_org_id": "historical",
        "authoritative_org_id": "canonical",
    }

    operation = Operation("reconcile_github_routing", "/admin/organizations/{org_id}/connections/github/{installation_id}/reconcile-routing")
    token = current_operation.set(operation)
    try:
        monkeypatch.setattr(
            routes,
            "reconcile_routing",
            AsyncMock(
                side_effect=service.RoutingReconciliationRefusedError("stale_expectation", observed_org_id="other", authoritative_org_id="canonical")
            ),
        )
        with pytest.raises(HTTPException) as error:
            await routes.reconcile_github_routing("canonical", 42, request, MagicMock(), actor)
        assert error.value.status_code == 409
        assert operation.refusal["observed_projection_org_id"] == "other"
        assert operation.refusal["authoritative_org_id"] == "canonical"
        assert audit.await_count == 1
    finally:
        current_operation.reset(token)


@pytest.mark.asyncio
async def test_route_stages_durable_audit_receipt_for_replay(monkeypatch):
    monkeypatch.setattr(routes, "_require_platform_admin", MagicMock())
    monkeypatch.setattr(routes, "reconcile_routing", AsyncMock(return_value=("already_consistent", "canonical")))
    actor = SimpleNamespace(user_id="admin", org_id="platform")
    operation = Operation("reconcile_github_routing", "/admin/organizations/{org_id}/connections/github/{installation_id}/reconcile-routing")
    operation.actor = actor
    operation.target_org = "canonical"
    token = current_operation.set(operation)
    try:
        response = await routes.reconcile_github_routing(
            "canonical", 42, ReconcileRoutingRequest(expected_projection_org_id="historical"), MagicMock(), actor
        )
        assert response.outcome == "already_consistent"
        assert operation.terminal["outcome"] == "already_consistent"
        assert operation.terminal["target_id"] == "42"
        assert operation.terminal["org_id"] == "canonical"
        assert operation.terminal["extra"] == {
            "expected_projection_org_id": "historical",
            "observed_projection_org_id": "canonical",
            "authoritative_org_id": "canonical",
        }
    finally:
        current_operation.reset(token)
