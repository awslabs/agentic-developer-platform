"""Offline exact native catalogue identity checks; no deployed bindings."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import ValidationError

from src.agentauth import native_test_catalogue as catalogue_module
from src.agentauth.native_test_catalogue import NativeSearchCatalogue, NativeSearchCatalogueBinding
from src.agentauth.native_test_enrollment import AllInApproval
from src.agentauth.native_test_stateless_runtime import NativeStatelessBinding, stateless_coverage
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest


def offline_catalogue():
    configuration = {
        "mcp": {
            "lambda": {
                "lambdaArn": "arn:aws:lambda:us-east-1:123456789012:function:offline-shared",
                "toolSchema": {"inlinePayload": [{"name": "search", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}]},
            }
        }
    }
    binding = NativeSearchCatalogueBinding(
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-0123456789",
        gateway_role_arn="arn:aws:iam::123456789012:role/offline-gateway",
        target_id="abcdefghij",
        lambda_arn=configuration["mcp"]["lambda"]["lambdaArn"],
        target_configuration_sha256=digest(configuration),
    )
    gateway = {
        "gatewayArn": binding.gateway_arn,
        "gatewayId": binding.gateway_id,
        "name": "adp-tools",
        "status": "READY",
        "roleArn": binding.gateway_role_arn,
        "protocolType": "MCP",
        "authorizerType": "AWS_IAM",
    }
    target = {
        "gatewayArn": binding.gateway_arn,
        "targetId": binding.target_id,
        "name": "websearch",
        "status": "READY",
        "targetConfiguration": configuration,
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    }
    return binding, gateway, target


def install_control(monkeypatch, binding, gateway, target, *, fail=None):
    calls = []

    def read(name, expected, reply, arguments):
        assert arguments == expected
        calls.append(name)
        if fail and name == fail[0]:
            raise fail[1]
        return deepcopy(reply)

    def client(service, *, region_name, config):
        assert service == "bedrock-agentcore-control"
        assert region_name == binding.region
        assert config.retries == {"total_max_attempts": 1}
        assert (config.connect_timeout, config.read_timeout) == (2, 3)
        # No paginator, list or Lambda method exists on this test transport.
        return SimpleNamespace(
            get_gateway=lambda **kw: read("gateway", {"gatewayIdentifier": binding.gateway_id}, gateway, kw),
            get_gateway_target=lambda **kw: read("target", {"gatewayIdentifier": binding.gateway_id, "targetId": binding.target_id}, target, kw),
        )

    monkeypatch.setattr(catalogue_module, "boto3", SimpleNamespace(client=client))
    return calls


def offline_approval():
    # Only the catalogue boundary is under test here. The integration tests use
    # actual enrollment, SQL membership, Dynamo policy and protected contracts.
    return AllInApproval.model_construct(
        tools=["websearch.search"],
        allowed_models=[],
        allowed_routes=[],
        effects=[SimpleNamespace(action=action, tool="websearch.search", source_sha256="d" * 64) for action in ("setup", "list", "call")],
    )


def test_two_single_attempt_reads_on_every_check_without_requiring_lambda_version(monkeypatch):
    binding, gateway, target = offline_catalogue()
    calls = install_control(monkeypatch, binding, gateway, target)
    reader = NativeSearchCatalogue(binding, "d" * 64)
    for _ in range(2):
        assert reader.read(offline_approval()) == {"tools": [{"name": "websearch.search"}]}
    assert calls == ["gateway", "target", "gateway", "target"]


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("gateway", "gatewayArn", "arn:aws:bedrock-agentcore:us-east-1:999999999999:gateway/adp-tools-0123456789"),
        ("gateway", "gatewayId", "foreign-0123456789"),
        ("gateway", "name", "foreign"),
        ("gateway", "status", "UPDATING"),
        ("gateway", "roleArn", "arn:aws:iam::123456789012:role/foreign"),
        ("gateway", "authorizerType", "NONE"),
        ("gateway", "protocolType", "HTTP"),
        ("gateway", "gatewayArn", None),
        ("target", "gatewayArn", "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/foreign-0123456789"),
        ("target", "targetId", "foreign123"),
        ("target", "name", "browser"),
        ("target", "status", "FAILED"),
        ("target", "targetConfiguration", {}),
        ("target", "targetConfiguration", {"mcp": {"openApiSchema": {"inlinePayload": "changed provider"}}}),
        ("target", "credentialProviderConfigurations", [{"credentialProviderType": "OAUTH"}]),
    ],
)
def test_drift_or_missing_metadata_refuses_without_retry(monkeypatch, section, field, value):
    binding, gateway, target = offline_catalogue()
    row = gateway if section == "gateway" else target
    row[field] = value
    calls = install_control(monkeypatch, binding, gateway, target)
    with pytest.raises(AdmissionRefusedError, match="catalogue_unavailable"):
        NativeSearchCatalogue(binding, "d" * 64).read(offline_approval())
    assert calls == (["gateway"] if section == "gateway" else ["gateway", "target"])


@pytest.mark.parametrize("fault", ["provider_arn", "schema", "missing_search", "duplicate_search", "unexpected_source"])
def test_provider_and_schema_are_exactly_bound(monkeypatch, fault):
    binding, gateway, target = offline_catalogue()
    config = target["targetConfiguration"]
    provider = config["mcp"]["lambda"]
    entries = provider["toolSchema"]["inlinePayload"]
    if fault == "provider_arn":
        provider["lambdaArn"] += "-foreign"
    elif fault == "schema":
        entries[0]["inputSchema"]["properties"]["query"]["type"] = "integer"
    elif fault == "missing_search":
        entries.clear()
    elif fault == "duplicate_search":
        entries.append(deepcopy(entries[0]))
    else:
        config["mcp"]["openApiSchema"] = {"inlinePayload": "unexpected source"}
    if fault in {"provider_arn", "missing_search", "duplicate_search", "unexpected_source"}:
        # A digest of malformed metadata cannot supply Search authority either.
        binding = binding.model_copy(update={"target_configuration_sha256": digest(config)})
    calls = install_control(monkeypatch, binding, gateway, target)
    with pytest.raises(AdmissionRefusedError, match="catalogue_unavailable"):
        NativeSearchCatalogue(binding, "d" * 64).read(offline_approval())
    assert calls == ["gateway", "target"]


@pytest.mark.parametrize("name", ["gateway", "target"])
@pytest.mark.parametrize("error", ["missing", "denied", "throttled", "transport"])
def test_api_failure_has_no_retry_or_fallback(monkeypatch, name, error):
    binding, gateway, target = offline_catalogue()
    codes = {"missing": "ResourceNotFoundException", "denied": "AccessDeniedException", "throttled": "ThrottlingException"}
    failure = (
        EndpointConnectionError(endpoint_url="https://offline.invalid")
        if error == "transport"
        else ClientError({"Error": {"Code": codes[error]}}, name)
    )
    calls = install_control(monkeypatch, binding, gateway, target, fail=(name, failure))
    with pytest.raises(AdmissionRefusedError, match="catalogue_unavailable"):
        NativeSearchCatalogue(binding, "d" * 64).read(offline_approval())
    assert calls == (["gateway"] if name == "gateway" else ["gateway", "target"])


@pytest.mark.parametrize("fault", ["release", "model", "route", "tool", "effect"])
def test_non_stateless_or_changed_release_scope_never_reads_control(monkeypatch, fault):
    binding, gateway, target = offline_catalogue()
    calls = install_control(monkeypatch, binding, gateway, target)
    approval = offline_approval()
    if fault == "release":
        approval.effects[0].source_sha256 = "e" * 64
    elif fault == "model":
        approval.allowed_models.append("offline-model")
    elif fault == "route":
        approval.allowed_routes.append("/v1/messages")
    elif fault == "tool":
        approval.tools.append("browser.start")
    else:
        approval.effects.pop()
    with pytest.raises(AdmissionRefusedError, match="catalogue_scope_refused"):
        NativeSearchCatalogue(binding, "d" * 64).read(approval)
    assert calls == []


def test_binding_requires_same_account_region_and_new_contract():
    catalogue, _, _ = offline_catalogue()
    for field, value in (
        ("gateway_role_arn", "arn:aws:iam::999999999999:role/foreign"),
        ("lambda_arn", "arn:aws:lambda:eu-west-1:123456789012:function:foreign"),
        ("lambda_arn", "arn:aws:lambda:us-east-1:999999999999:function:foreign"),
    ):
        with pytest.raises(ValidationError):
            NativeSearchCatalogueBinding.model_validate({**catalogue.model_dump(), field: value})
    values = dict(
        schema_version="2.0",
        deployment_id="offline-gateway",
        release_sha256="d" * 64,
        authority_table="offline-authority",
        execution_table="offline-executions",
        facade_role_arn="arn:aws:iam::123456789012:role/offline-facade",
        reconciler_role_arn="arn:aws:iam::123456789012:role/offline-reconciler",
        catalogue=catalogue,
    )
    binding = NativeStatelessBinding(**values)
    for changed in ({**values, "schema_version": "1.0"}, {k: v for k, v in values.items() if k != "catalogue"}):
        with pytest.raises(ValidationError):
            NativeStatelessBinding(**changed)
    for field, value in (("target_id", "0123456789"), ("target_configuration_sha256", "e" * 64)):
        changed = binding.model_copy(update={"catalogue": catalogue.model_copy(update={field: value})})
        assert stateless_coverage(changed) != stateless_coverage(binding)
