"""Runtime verification of a tools gateway guarded by the fail-closed interceptor (#7729).

The tools interceptor (TLS-S15) refuses every MCP request that lacks the façade's signed
assertion, so the engine's own SigV4 ``tools/list`` as the connection role can never
succeed by design. The live proof for such a gateway is that it binds exactly the reviewed
interceptor, that the alias serves the reviewed version and code, and that an unasserted
listing is refused by the interceptor itself — not by IAM and not by an absent gateway.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from src.orchestration.review_cycle import CycleBlockedError
from tests.orchestration.test_deployment_runtime_tools import gateway_fixture

INTERCEPTOR = "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-dev-interceptor:live"
DENIAL = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "untrusted_assertion"}}


def guarded_fixture(*, denial=DENIAL, status=403, version="9", code="c" * 43 + "="):
    runtime = gateway_fixture()
    runtime.artifact = runtime.artifact.model_copy(
        update={"interceptor_lambda_arn": INTERCEPTOR, "interceptor_version": version, "interceptor_code_sha256": code}
    )
    runtime.gateway["interceptorConfigurations"] = [
        {
            "interceptor": {"lambda": {"arn": INTERCEPTOR}},
            "interceptionPoints": ["REQUEST", "RESPONSE"],
            "inputConfiguration": {"passRequestHeaders": True},
        }
    ]
    function_arn = INTERCEPTOR.rsplit(":", 1)[0]
    lambda_client = SimpleNamespace(
        get_alias=Mock(return_value={"FunctionVersion": version, "RoutingConfig": {}}),
        get_function=Mock(
            return_value={
                "Configuration": {"FunctionArn": f"{function_arn}:{version}", "CodeSha256": code, "State": "Active", "LastUpdateStatus": "Successful"}
            }
        ),
    )
    runtime.lambda_client = lambda_client
    runtime.scoped.client = Mock(side_effect=lambda name, **_: lambda_client if name == "lambda" else runtime.control)
    runtime.calls.clear()

    def send(request):
        runtime.calls.append(request)
        return httpx.Response(status, json=denial)

    runtime.client = httpx.Client(transport=httpx.MockTransport(send))
    return runtime


def test_interceptor_refusal_is_the_live_proof_for_a_guarded_gateway():
    from src.orchestration.deployment_runtime_tools import verify_agentcore_gateway

    runtime = guarded_fixture()
    with runtime.client:
        observed = verify_agentcore_gateway(runtime.scoped, runtime.artifact, "d" * 64, "release/artifact", client=runtime.client)
    assert observed.tools_list_sha256 == runtime.artifact.tools_list_sha256
    assert observed.tools_list_source == "release_smoke"
    assert observed.interceptor_fail_closed is True
    assert observed.interceptor_lambda_arn == INTERCEPTOR
    assert observed.target_sync_revision == runtime.artifact.target_sync_revision
    # The verifier still probed the data plane as the connection role, once, with SigV4.
    assert len(runtime.calls) == 1 and json.loads(runtime.calls[0].content)["method"] == "tools/list"
    assert "AWS4-HMAC-SHA256" in runtime.calls[0].headers["authorization"]
    runtime.lambda_client.get_alias.assert_called_once_with(FunctionName=INTERCEPTOR.rsplit(":", 1)[0], Name="live")


def test_unguarded_gateway_keeps_the_live_listing_contract():
    from src.orchestration.deployment_runtime_tools import verify_agentcore_gateway

    runtime = gateway_fixture()
    with runtime.client:
        observed = verify_agentcore_gateway(runtime.scoped, runtime.artifact, "d" * 64, "release/artifact", client=runtime.client)
    assert observed.tools_list_source == "live_listing"
    assert observed.interceptor_fail_closed is None and observed.interceptor_lambda_arn is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("interceptor_answers", "deployment_runtime_interceptor_open"),  # the interceptor let an unasserted caller list tools
        ("iam_denial", "deployment_runtime_mismatch"),  # AgentCore's own 403 is not the interceptor's refusal
        ("other_reason", "deployment_runtime_mismatch"),  # a refusal for another reason is not the fail-closed proof
        ("no_interceptor_bound", "deployment_runtime_mismatch"),  # gateway no longer binds the reviewed interceptor
        ("different_interceptor", "deployment_runtime_mismatch"),
        ("response_only", "deployment_runtime_mismatch"),  # interceptor not at REQUEST cannot refuse requests
        ("alias_version", "deployment_runtime_mismatch"),
        ("alias_code", "deployment_runtime_mismatch"),
        ("unreviewed_interceptor_live", "deployment_runtime_mismatch"),  # artifact has no interceptor but the gateway does
    ],
)
def test_guarded_gateway_differences_never_verify(change, reason):
    from src.orchestration.deployment_runtime_tools import verify_agentcore_gateway

    runtime = guarded_fixture()
    if change == "interceptor_answers":
        runtime = guarded_fixture(denial=runtime.listing, status=200)
    elif change == "iam_denial":
        runtime = guarded_fixture(denial={"message": "User is not authorized to perform bedrock-agentcore:InvokeGateway"})
    elif change == "other_reason":
        runtime = guarded_fixture(denial={**DENIAL, "error": {"code": -32000, "message": "tenant_mismatch"}})
    elif change == "no_interceptor_bound":
        runtime.gateway["interceptorConfigurations"] = []
    elif change == "different_interceptor":
        runtime.gateway["interceptorConfigurations"][0]["interceptor"]["lambda"]["arn"] = INTERCEPTOR.replace("dev", "other")
    elif change == "response_only":
        runtime.gateway["interceptorConfigurations"][0]["interceptionPoints"] = ["RESPONSE"]
    elif change == "alias_version":
        runtime.lambda_client.get_alias.return_value = {"FunctionVersion": "8"}
    elif change == "alias_code":
        runtime.lambda_client.get_function.return_value["Configuration"]["CodeSha256"] = "x" * 43 + "="
    else:
        runtime.artifact = runtime.artifact.model_copy(
            update={"interceptor_lambda_arn": None, "interceptor_version": None, "interceptor_code_sha256": None}
        )
    with runtime.client, pytest.raises(CycleBlockedError, match=reason):
        verify_agentcore_gateway(runtime.scoped, runtime.artifact, "d" * 64, "release/artifact", client=runtime.client)


def test_interceptor_denial_shape_is_recognised_only_for_the_interceptor():
    from src.orchestration.deployment_runtime_tools import _interceptor_denial

    assert _interceptor_denial(403, json.dumps(DENIAL).encode()) == "untrusted_assertion"
    assert _interceptor_denial(200, json.dumps(DENIAL).encode()) is None
    assert _interceptor_denial(403, b'{"message": "AccessDenied"}') is None
    assert _interceptor_denial(403, b"not json") is None
    assert _interceptor_denial(403, json.dumps({**DENIAL, "error": {"code": -32600, "message": "untrusted_assertion"}}).encode()) is None
    assert _interceptor_denial(403, json.dumps({**DENIAL, "error": {"code": -32000, "message": "Not A Token!"}}).encode()) is None


def test_runtime_component_records_the_listing_source():
    from src.orchestration.deployment_runtime_contract import RuntimeComponent

    component = RuntimeComponent(
        component="tools-platform",
        actual_revision="b" * 40,
        artifact_hash="d" * 64,
        healthy=True,
        evidence_ref="release/artifact",
        observed_at=datetime.now(UTC),
        gateway_id="example",
        gateway_url="https://example.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        target_ids=["target-1"],
        target_sync_revision="f" * 64,
        tools_list_sha256="e" * 64,
        tools_list_source="release_smoke",
        interceptor_lambda_arn=INTERCEPTOR,
        interceptor_fail_closed=True,
    )
    assert component.tools_list_source == "release_smoke"
    with pytest.raises(ValueError):
        RuntimeComponent.model_validate({**component.model_dump(), "tools_list_source": "guessed"})
