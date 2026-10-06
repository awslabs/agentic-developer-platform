"""Bounded protected domain roles cannot inherit authority after IAM recreation."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from src.auth.agent_registry import AgentRegistryService


@pytest.fixture(params=["api-producer", "domain-worker"])
def protected_role(request):
    arn = f"arn:aws:iam::123456789012:role/adp-dev-superplane-{request.param}"
    role_id = "AROA11111111111111111"
    item = {
        "agent_id": {"S": "registry-id"},
        "role_arn": {"S": arn},
        "iam_role_id": {"S": role_id},
        "owner": {"S": "webhook-terraform-domain-operations-v1"},
        "status": {"S": "active"},
        "scope": {"S": "internal"},
        "credential_scopes": {"SS": ["domain:operation-producer" if request.param == "api-producer" else "domain:operation-executor"]},
    }
    service = AgentRegistryService(table_name="registry")
    service._dynamodb = MagicMock()
    service._dynamodb.get_item.return_value = {"Item": item}
    service._dynamodb.query.return_value = {"Items": [item]}
    service._iam = MagicMock()
    service._iam.get_role.return_value = {"Role": {"Arn": arn, "RoleId": role_id}}
    return service, arn, item


def test_domain_identity_is_verified_on_each_cached_and_current_authority_read(protected_role):
    service, arn, _ = protected_role
    assert service.get_agent_by_role_arn(arn) is not None
    assert service.get_agent_by_role_arn(arn) is not None
    assert service.get_current_agent("registry-id", arn) is not None
    assert service._iam.get_role.call_count == 3
    assert service._dynamodb.query.call_count == 1
    assert all(call.kwargs["ConsistentRead"] for call in service._dynamodb.get_item.call_args_list)
    service._iam.get_role.assert_called_with(RoleName=arn.rsplit("/", 1)[1])


@pytest.mark.parametrize("path", ["initial", "cached", "current"])
def test_recreated_role_is_refused_before_authority_context_is_returned(protected_role, path):
    service, arn, _ = protected_role
    if path == "cached":
        assert service.get_agent_by_role_arn(arn) is not None
    service._iam.get_role.return_value["Role"]["RoleId"] = "AROA22222222222222222"
    result = service.get_current_agent("registry-id", arn) if path == "current" else service.get_agent_by_role_arn(arn)
    assert result is None


@pytest.mark.parametrize("error", ["NoSuchEntity", "AccessDenied", "ServiceFailure"])
def test_missing_role_or_unverifiable_lookup_never_falls_back_to_cached_authority(protected_role, error):
    service, arn, _ = protected_role
    assert service.get_agent_by_role_arn(arn) is not None
    service._iam.get_role.side_effect = ClientError({"Error": {"Code": error}}, "GetRole")
    assert service.get_agent_by_role_arn(arn) is None
    assert service.get_current_agent("registry-id", arn) is None


def test_missing_role_id_cannot_be_bypassed_by_changing_owner_label(protected_role):
    service, arn, item = protected_role
    del item["iam_role_id"]
    item["owner"] = {"S": "different-owner"}
    assert service.get_agent_by_role_arn(arn) is None
    service._iam.get_role.assert_not_called()


def test_paid_request_authority_boundary_rejects_recreated_role(protected_role, monkeypatch):
    from src.internal.domain_operation_runtime import current_registry

    service, arn, item = protected_role
    monkeypatch.setattr("src.auth.agent_registry.get_agent_registry_service", lambda: service)
    request = SimpleNamespace(
        state=SimpleNamespace(token_context=SimpleNamespace(auth_source="iam", agent_registry_id="registry-id")),
        headers={"X-Caller-Identity": arn},
    )
    scope = item["credential_scopes"]["SS"][0]
    assert current_registry(request, scope) == "registry-id"
    service._iam.get_role.return_value["Role"]["RoleId"] = "AROA22222222222222222"
    with pytest.raises(HTTPException) as failure:
        current_registry(request, scope)
    assert failure.value.status_code == 403


def test_ordinary_agent_lookup_does_not_acquire_iam_dependency():
    service = AgentRegistryService(table_name="registry")
    service._dynamodb = MagicMock()
    service._iam = MagicMock()
    arn = "arn:aws:iam::123456789012:role/customer-agent"
    service._dynamodb.get_item.return_value = {"Item": {"agent_id": {"S": "customer"}, "role_arn": {"S": arn}, "status": {"S": "active"}}}
    assert service.get_current_agent("customer", arn) is not None
    service._iam.get_role.assert_not_called()
