"""Bounded protected domain roles cannot inherit authority after IAM recreation."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.awsrequest import AWSResponse
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


@pytest.mark.parametrize(
    "registry_id",
    [
        "00000000-0000-0000-0000-000000008243",
        "00000000-0000-0000-0000-000008243002",
    ],
)
def test_product_evaluator_requires_current_role_even_if_registry_owner_changes(registry_id):
    arn = "arn:aws:iam::123456789012:role/adp-cli-uplift-eval-instance"
    role_id = "AROA11111111111111111"
    item = {
        "agent_id": {"S": registry_id},
        "role_arn": {"S": arn},
        "iam_role_id": {"S": role_id},
        "owner": {"S": "different-owner"},
        "status": {"S": "active"},
        "scope": {"S": "internal"},
        "credential_scopes": {"SS": ["internal:product-fixture:retire"]},
    }
    service = AgentRegistryService(table_name="registry")
    service._dynamodb = MagicMock()
    service._dynamodb.get_item.return_value = {"Item": item}
    service._dynamodb.query.return_value = {"Items": [item]}
    service._iam = MagicMock()
    service._iam.get_role.return_value = {"Role": {"Arn": arn, "RoleId": role_id}}
    assert service.get_agent_by_role_arn(arn) is not None
    service._iam.get_role.return_value["Role"]["RoleId"] = "AROA22222222222222222"
    assert service.get_agent_by_role_arn(arn) is None
    del item["iam_role_id"]
    assert service.get_current_agent(item["agent_id"]["S"], arn) is None


def test_ordinary_agent_lookup_does_not_acquire_iam_dependency():
    service = AgentRegistryService(table_name="registry")
    service._dynamodb = MagicMock()
    service._iam = MagicMock()
    arn = "arn:aws:iam::123456789012:role/customer-agent"
    service._dynamodb.get_item.return_value = {"Item": {"agent_id": {"S": "customer"}, "role_arn": {"S": arn}, "status": {"S": "active"}}}
    assert service.get_current_agent("customer", arn) is not None
    service._iam.get_role.assert_not_called()


@pytest.mark.parametrize("outcome", ["service-failure", "connection-timeout", "access-denied", "recreated", "persistent-failure"])
def test_real_iam_transport_retries_only_transient_reads(protected_role, monkeypatch, outcome):
    import boto3
    from botocore.exceptions import ConnectTimeoutError

    service, arn, item = protected_role
    session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    calls = []
    delays = []

    class Body:
        def __init__(self, body):
            self.body = body.encode()

        def stream(self, **_kwargs):
            yield self.body

    def send(request):
        calls.append(request)
        if len(calls) == 1 and outcome == "connection-timeout":
            raise ConnectTimeoutError(endpoint_url=request.url)
        if outcome == "access-denied" or outcome == "persistent-failure" or (outcome == "service-failure" and len(calls) == 1):
            denied = outcome == "access-denied"
            code = "AccessDenied" if denied else "ServiceFailure"
            return AWSResponse(
                request.url,
                403 if denied else 503,
                {},
                Body(f"<ErrorResponse><Error><Type>Sender</Type><Code>{code}</Code><Message>unavailable</Message></Error></ErrorResponse>"),
            )
        role_id = "AROA22222222222222222" if outcome == "recreated" else item["iam_role_id"]["S"]
        return AWSResponse(
            request.url,
            200,
            {},
            Body(f"<GetRoleResponse><GetRoleResult><Role><Arn>{arn}</Arn><RoleId>{role_id}</RoleId></Role></GetRoleResult></GetRoleResponse>"),
        )

    def client(service_name, **kwargs):
        assert service_name == "iam"
        iam = session.client(service_name, **kwargs)
        monkeypatch.setattr(iam._endpoint.http_session, "send", send)
        return iam

    service._iam = None
    monkeypatch.setattr("src.auth.agent_registry.boto3.client", client)
    monkeypatch.setattr("botocore.endpoint.time.sleep", delays.append)
    result = service.get_current_agent("registry-id", arn)
    assert (result is not None) == (outcome in {"service-failure", "connection-timeout"})
    assert len(calls) == (1 if outcome in {"access-denied", "recreated"} else 2)
    assert len(delays) == len(calls) - 1
