import base64
import hashlib
import hmac
import json
from pathlib import Path

import boto3
import pytest
from tools_interceptor.handler import MAX_AUDIT_UTF8_BYTES, _audit, _encoded, handler

FIXTURES = Path(__file__).parent / "fixtures"
KEY = b"unit-test-signing-key"
ROLE = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
GATEWAY_ROLE = "arn:aws:iam::123456789012:role/adp-dev-role-gateway-service"
KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


@pytest.fixture
def kms(monkeypatch):
    class FakeKMS:
        def verify(self, **parameters):
            assert parameters["KeyId"] == KEY_ARN
            assert parameters["MessageType"] == "RAW"
            assert parameters["SigningAlgorithm"] == "RSASSA_PSS_SHA_256"
            expected = hmac.digest(KEY, parameters["Message"], "sha256")
            return {"SignatureValid": hmac.compare_digest(parameters["Signature"], expected)}

    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", KEY_ARN)
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", ROLE)
    def client(service, *, config):
        assert config.retries == {"total_max_attempts": 1}
        return FakeKMS() if service == "kms" else None

    monkeypatch.setattr(boto3, "client", client)


def signed_event(fixture, *, claims_update=None, body_update=None, tenant="tenant-1", now=1000):
    event = json.loads((FIXTURES / fixture).read_text())
    request = event["mcp"]["gatewayRequest"]
    body = request["body"]
    if body_update:
        body.update(body_update)
    method = body["method"]
    args = body.get("params", {}) if method == "tools/list" else body.get("params", {}).get("arguments", {})
    claims = {
        "caller_role_arn": ROLE,
        "principal": {"user_id": "user-1", "tenant_id": "tenant-1", "team_id": "team-1"},
        "method": method,
        "tool": body.get("params", {}).get("name", method),
        "arguments_sha256": hashlib.sha256(_encoded(args)).hexdigest(),
        "receipt_id": "receipt-1", "issued_at": now - 1, "expires_at": now + 30,
    }
    if method == "tools/call" and claims["tool"].startswith(("knowledge___", "github___", "jira___", "atlassian___")):
        claims["rpc_id"] = body["id"]
    if claims_update:
        claims.update(claims_update)
    envelope = {"claims": claims, "signature": _b64(hmac.digest(KEY, _encoded(claims), "sha256"))}
    request["headers"] = {"x-adp-assertion": _b64(_encoded(envelope)), "x-adp-tenant": tenant}
    return event


@pytest.mark.parametrize("operation,arguments", [
    ("list", {"service": "fixture"}),
    ("proxy_request", {"service": "fixture", "method": "GET", "url": "https://fixture.example.invalid/status"}),
])
def test_broker_openapi_request_preserves_signed_header_without_identity_selectors(kms, monkeypatch, operation, arguments):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", claims_update={"root_human_id": "user-1"})
    request = event["mcp"]["gatewayRequest"]
    request["body"]["method"] = "tools/call"
    request["body"]["params"] = {"name": f"credentials___{operation}", "arguments": arguments}
    claims = json.loads(base64.urlsafe_b64decode(request["headers"]["x-adp-assertion"] + "=="))["claims"]
    claims["tool"] = f"credentials___{operation}"
    claims["method"] = "tools/call"
    claims["arguments_sha256"] = hashlib.sha256(_encoded(arguments)).hexdigest()
    request["headers"]["x-adp-assertion"] = _b64(_encoded({"claims": claims, "signature": _b64(hmac.digest(KEY, _encoded(claims), "sha256"))}))
    result = handler(event, None)
    assert result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"] == arguments
    assert request["headers"]["x-adp-assertion"]
    assert not {"user_id", "agent_id", "task_id"} & set(arguments)


def test_audit_retains_original_receipt_and_method_as_json(kms, monkeypatch, caplog):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    handler(signed_event("response.json"), None)
    records = [json.loads(record.getMessage()) for record in caplog.records]
    assert records == [{"event": "response_allowed", "receipt_id": "receipt-1", "method": "tools/call"}]


@pytest.mark.parametrize("text", ["🧪" * 1000, "\x00\n\\\"" * 1000, "\ud800" * 1000])
def test_audit_caps_encoded_json_without_splitting_unicode_or_losing_correlation(text, caplog):
    _audit("response_allowed", receipt_id=text, method=text, reason=text, status=10**5000,
           error_type=text, body_type=text, streaming=True, body_key_count=9000, response_key_count=4)
    message = caplog.records[-1].getMessage()
    assert len(message.encode("utf-8")) <= MAX_AUDIT_UTF8_BYTES
    document = json.loads(message)
    assert document["event"] == "response_allowed"
    assert document["receipt_sha256"] == hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    assert document["truncated"] is True
    assert document["streaming"] is True
    assert document["body_key_count"] == 9000
    assert {"receipt_id", "method", "reason", "error_type", "body_type"} <= document.keys()


def test_malformed_response_emits_two_bounded_records_without_dynamic_keys(kms, monkeypatch, caplog):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("response.json")
    private_key = "private-response-key-" + "🧪" * 10000
    event["mcp"]["gatewayResponse"]["body"] = {private_key: "never-log-this-payload"}
    event["mcp"]["gatewayResponse"]["statusCode"] = 10**5000
    result = handler(event, None)
    assert result["mcp"]["transformedGatewayResponse"]["statusCode"] == 403
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    documents = [json.loads(message) for message in messages]
    assert [row["event"] for row in documents] == ["refused_response", "denied"]
    assert documents[0]["body_key_count"] == 1
    assert all(len(message.encode("utf-8")) <= MAX_AUDIT_UTF8_BYTES for message in messages)
    assert all("private-response-key" not in message and "never-log-this-payload" not in message for message in messages)


def test_json_escape_expansion_retains_digest_when_short_receipt_is_truncated(caplog):
    receipt = "\x00" * 100
    _audit("response_allowed", receipt_id=receipt, method="tools/call")
    message = caplog.records[-1].getMessage()
    record = json.loads(message)
    assert len(message.encode("utf-8")) <= MAX_AUDIT_UTF8_BYTES
    assert record["receipt_id"] != receipt
    assert record["receipt_sha256"] == hashlib.sha256(receipt.encode()).hexdigest()
    assert record["truncated"] is True


def test_signed_call_without_arguments_never_logs_allow_before_refusal(kms, monkeypatch, caplog):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", body_update={
        "method": "tools/call", "params": {"name": "code-interpreter___start"},
    })
    result = handler(event, None)
    assert result["mcp"]["transformedGatewayResponse"]["statusCode"] == 403
    messages = [record.getMessage() for record in caplog.records]
    assert [json.loads(message)["event"] for message in messages] == ["refused_request", "denied"]
    assert all(len(message.encode()) <= MAX_AUDIT_UTF8_BYTES for message in messages)


@pytest.mark.parametrize("fixture", ["request.json", "response.json"])
def test_documented_gateway_payload_denied_without_authority(fixture):
    event = json.loads((FIXTURES / fixture).read_text())
    result = handler(event, None)

    assert result["interceptorOutputVersion"] == "1.0"
    assert result["mcp"]["transformedGatewayResponse"]["body"]["error"]["message"] == "untrusted_assertion"
    assert "transformedGatewayRequest" not in result["mcp"]


@pytest.mark.parametrize("event", [{}, {"mcp": {"gatewayRequest": {"body": "not JSON"}}}])
def test_malformed_payload_cannot_forward(event):
    result = handler(event, None)
    assert result["mcp"]["transformedGatewayResponse"]["statusCode"] == 403
    assert "transformedGatewayRequest" not in result["mcp"]


@pytest.mark.parametrize("fixture", ["request.json", "response.json"])
def test_authorized_request_and_receipt_response(kms, monkeypatch, fixture):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event(fixture)
    result = handler(event, None)
    if fixture == "request.json":
        assert result["mcp"] == {"transformedGatewayRequest": {"body": event["mcp"]["gatewayRequest"]["body"]}}
        assert "transformedGatewayResponse" not in result["mcp"]
    else:
        response = result["mcp"]["transformedGatewayResponse"]
        assert response["statusCode"] == 200
        assert response["body"]["result"] == {"tools": [], "_meta": {"adp_receipt_id": "receipt-1"}}
        assert "transformedGatewayRequest" not in result["mcp"]


def test_valid_call_bound_to_arguments_and_receipt(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("response.json", body_update={"params": {"name": "example_tool", "arguments": {"q": "one"}}})
    assert handler(event, None)["mcp"]["transformedGatewayResponse"]["body"]["result"]["_meta"]["adp_receipt_id"] == "receipt-1"
    event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"]["q"] = "two"
    assert_denied(event, "tool_mismatch")


def assert_denied(event, reason):
    result = handler(event, None)["mcp"]
    assert result["transformedGatewayResponse"]["statusCode"] == 403
    assert result["transformedGatewayResponse"]["body"]["error"]["message"] == reason
    assert "transformedGatewayRequest" not in result


@pytest.mark.parametrize("tenant,claims_update,body_update,reason", [
    ("other-tenant", None, None, "tenant_mismatch"),
    ("tenant-1", {"principal": {"user_id": "user-1", "tenant_id": "other-tenant", "team_id": "team-1"}}, None, "tenant_mismatch"),
    ("tenant-1", {"tool": "other_tool"}, None, "tool_mismatch"),
    ("tenant-1", None, {"params": {"name": "other_tool"}}, "tool_mismatch"),
])
def test_signed_mismatches_never_forward(kms, monkeypatch, tenant, claims_update, body_update, reason):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("response.json", tenant=tenant, claims_update=claims_update)
    if body_update:
        event["mcp"]["gatewayRequest"]["body"].update(body_update)
    assert_denied(event, reason)


@pytest.mark.parametrize("claims_update", [
    {"caller_role_arn": "arn:aws:iam::123456789012:role/other"},
    {"caller_role_arn": GATEWAY_ROLE},
    {"issued_at": 900, "expires_at": 930},
    {"issued_at": 1000, "expires_at": 1100},
    {"receipt_id": ""},
])
def test_untrusted_claims_never_forward(kms, monkeypatch, claims_update):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    assert_denied(signed_event("request.json", claims_update=claims_update), "untrusted_assertion")


def test_tampered_signature_and_spoofed_role_header_never_forward(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json")
    headers = event["mcp"]["gatewayRequest"]["headers"]
    headers["x-adp-caller-role"] = ROLE
    envelope = json.loads(base64.urlsafe_b64decode(headers["x-adp-assertion"] + "=="))
    envelope["claims"]["principal"]["tenant_id"] = "other-tenant"
    headers["x-adp-assertion"] = _b64(_encoded(envelope))
    assert_denied(event, "untrusted_assertion")


def test_forged_facade_claim_cannot_substitute_for_signature(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", claims_update={"caller_role_arn": GATEWAY_ROLE})
    headers = event["mcp"]["gatewayRequest"]["headers"]
    envelope = json.loads(base64.urlsafe_b64decode(headers["x-adp-assertion"] + "=="))
    envelope["claims"]["caller_role_arn"] = ROLE
    headers["x-adp-assertion"] = _b64(_encoded(envelope))
    headers["x-adp-caller-role"] = ROLE
    assert_denied(event, "untrusted_assertion")


def test_legacy_gateway_role_setting_does_not_authorize_facade_claim(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    monkeypatch.delenv("ADP_TOOLS_FACADE_ROLE_ARN")
    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", ROLE)
    assert_denied(signed_event("request.json"), "untrusted_assertion")


def test_list_arguments_are_bound_to_signature(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", body_update={"params": {"cursor": "first"}})
    assert "transformedGatewayRequest" in handler(event, None)["mcp"]
    event["mcp"]["gatewayRequest"]["body"]["params"]["cursor"] = "second"
    assert_denied(event, "tool_mismatch")


@pytest.mark.parametrize("mutation,reason", [
    (lambda event: event["mcp"]["gatewayRequest"]["headers"].update({"x-adp-tenant": "other-tenant"}), "tenant_mismatch"),
    (lambda event: event["mcp"]["gatewayRequest"]["body"].update({"method": "tools/list"}), "tool_mismatch"),
    (lambda event: event["mcp"]["gatewayRequest"]["body"]["params"].update({"name": "other_tool"}), "tool_mismatch"),
    (lambda event: event["mcp"]["gatewayRequest"]["body"]["params"].update({"arguments": {"q": "different"}}), "tool_mismatch"),
])
def test_post_signature_mismatch_never_forwards(kms, monkeypatch, mutation, reason):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("response.json")
    mutation(event)
    assert_denied(event, reason)


@pytest.mark.parametrize("bad_header", ["not-base64!", "", "e30", None])
def test_invalid_assertion_header_never_forwards(kms, monkeypatch, bad_header):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json")
    event["mcp"]["gatewayRequest"]["headers"]["x-adp-assertion"] = bad_header
    assert_denied(event, "untrusted_assertion")


def test_duplicate_case_variant_assertion_header_never_forwards(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json")
    event["mcp"]["gatewayRequest"]["headers"]["X-Adp-Assertion"] = "forged"
    assert_denied(event, "untrusted_assertion")


def test_kms_failure_denies_without_lambda_exception(kms, monkeypatch):
    from botocore.exceptions import ClientError

    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    class FailedKMS:
        def verify(self, **parameters):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "unavailable"}}, "Verify")

    monkeypatch.setattr(boto3, "client", lambda service: FailedKMS())
    assert_denied(signed_event("request.json"), "untrusted_assertion")


def test_role_spoof_without_signed_claim_never_forwards(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = json.loads((FIXTURES / "request.json").read_text())
    event["mcp"]["gatewayRequest"]["headers"]["x-adp-caller-role"] = ROLE
    assert_denied(event, "untrusted_assertion")


@pytest.mark.parametrize("provider_data", [{"retryable": True}, "provider-detail", None])
def test_authorized_provider_error_keeps_error_and_receipt(kms, monkeypatch, provider_data):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("response.json")
    event["mcp"]["gatewayResponse"] = {
        "statusCode": 500,
        "body": {"jsonrpc": "2.0", "id": 2, "error": {
            "code": -32001, "message": "Provider failed", "data": provider_data,
        }},
    }
    response = handler(event, None)["mcp"]["transformedGatewayResponse"]
    assert response["statusCode"] == 500
    assert response["body"]["error"]["code"] == -32001
    assert response["body"]["error"]["message"] == "Provider failed"
    assert response["body"]["error"]["data"]["adp_receipt_id"] == "receipt-1"
    if isinstance(provider_data, dict):
        assert response["body"]["error"]["data"]["retryable"] is True
    else:
        assert response["body"]["error"]["data"]["provider_data"] == provider_data


@pytest.mark.parametrize("body", [{"error": "forbidden"}, "forbidden", {"result": {"tools": []}}])
def test_knowledge_denial_from_target_is_mcp_error_not_success(kms, monkeypatch, body):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("response.json", body_update={"params": {
        "name": "knowledge___search", "arguments": {"query": "private-repo"},
    }})
    event["mcp"]["gatewayResponse"] = {"statusCode": 403, "body": body}
    response = handler(event, None)["mcp"]["transformedGatewayResponse"]
    assert response["statusCode"] == 403
    assert response["body"] == {"jsonrpc": "2.0", "id": 2, "error": {
        "code": -32003, "message": "knowledge_access_denied",
        "data": {"adp_receipt_id": "receipt-1"},
    }}


def test_knowledge_mcp_error_preserves_provider_code_and_status(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("response.json", body_update={"params": {
        "name": "knowledge___search", "arguments": {"query": "private-repo"},
    }})
    event["mcp"]["gatewayResponse"] = {"statusCode": 403, "body": {
        "jsonrpc": "2.0", "id": 2, "error": {"code": -32003, "message": "forbidden"},
    }}
    response = handler(event, None)["mcp"]["transformedGatewayResponse"]
    assert response["statusCode"] == 403
    assert response["body"]["error"] == {
        "code": -32003, "message": "forbidden", "data": {"adp_receipt_id": "receipt-1"},
    }
    event["mcp"]["gatewayRequest"]["headers"]["x-adp-tenant"] = "other-tenant"
    assert_denied(event, "tenant_mismatch")


def test_nonknowledge_nonmcp_403_still_fails_closed(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("response.json")
    event["mcp"]["gatewayResponse"] = {"statusCode": 403, "body": {"error": "forbidden"}}
    assert_denied(event, "untrusted_assertion")


def test_authorized_streaming_frame_keeps_payload_and_receipt(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("response.json")
    event["mcp"]["gatewayResponse"] = {
        "isStreamingResponse": True,
        "body": {"jsonrpc": "2.0", "id": "elicit-1", "method": "elicitation/create", "params": {
            "message": "Confirm action", "requestedSchema": {},
        }},
    }
    response = handler(event, None)["mcp"]["transformedGatewayResponse"]
    # Only the documented output fields may be returned; the gateway rejects an
    # echoed isStreamingResponse as an invalid interceptor response.
    assert set(response) == {"body"}
    assert response["body"]["method"] == "elicitation/create"
    assert response["body"]["params"]["message"] == "Confirm action"
    assert response["body"]["params"]["_meta"]["adp_receipt_id"] == "receipt-1"


def test_response_interception_keeps_signed_mismatch_denial(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("response.json", tenant="other-tenant")
    event["mcp"]["gatewayResponse"]["body"] = {
        "jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "tenant_mismatch"},
    }
    assert_denied(event, "tenant_mismatch")


def test_legacy_jira_scope_without_identity_is_not_forwarded(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", claims_update={"connection_scopes": ["read:jira-work"]},
                         body_update={"method": "tools/call", "params": {
                             "name": "jira___get_issue", "arguments": {
                                 "cloudId": "example-cloud", "issueIdOrKey": "TEST-123",
                             },
                         }})
    request_result = handler(event, None)["mcp"]
    assert "transformedGatewayRequest" not in request_result
    assert request_result["transformedGatewayResponse"]["body"]["error"]["message"] == "scope_not_granted"



def test_jira_requires_signed_account_and_resource_observations(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    binding = {
        "connection_id": "00000000-0000-4000-8000-000000000001",
        "revision": "00000000-0000-4000-8000-000000000002",
        "account_id": "fixture-account",
        "scopes_sha256": "a" * 64,
        "credential_sha256": "b" * 64,
        "resources_sha256": "c" * 64,
    }
    arguments = {"cloudId": "00000000-0000-4000-8000-000000000003", "issueIdOrKey": "EXAMPLE-1"}
    claims = {"connection_scopes": ["read:jira-work", "read:me", "offline_access"],
              "connection_id": binding["connection_id"], "jira_connection": binding}
    body = {"method": "tools/call", "params": {"name": "jira___get_issue", "arguments": arguments}}
    event = signed_event("request.json", claims_update=claims, body_update=body)
    forwarded = handler(event, None)["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert {key: value for key, value in forwarded.items() if key != "adp_assertion"} == arguments
    for fault in ({"connection_id": "other-owner"}, {"jira_connection": None},
                  {"jira_connection": {key: value for key, value in binding.items() if key != "resources_sha256"}},
                  {"jira_connection": {**binding, "resources_sha256": "unverified"}}):
        assert_denied(signed_event("request.json", claims_update={**claims, **fault}, body_update=body), "connection_binding_required")

def test_github_requires_signed_observed_binding_before_private_dispatch(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    binding = {
        "connection_id": "00000000-0000-4000-8000-000000000001",
        "revision": "00000000-0000-4000-8000-000000000002",
        "account_id": "213",
        "scopes_sha256": "a" * 64,
        "credential_sha256": "b" * 64,
    }
    args = {}
    event = signed_event(
        "request.json", claims_update={
            "connection_scopes": ["read:user"], "connection_id": binding["connection_id"],
            "github_connection": binding,
        }, body_update={"method": "tools/call", "params": {"name": "github___get_user", "arguments": args}},
    )
    forwarded = handler(event, None)["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert set(forwarded) == {"adp_assertion"}
    assert forwarded["adp_assertion"] == event["mcp"]["gatewayRequest"]["headers"]["x-adp-assertion"]
    mismatched = signed_event(
        "request.json", claims_update={
            "connection_scopes": ["read:user"], "connection_id": "other-connection", "github_connection": binding,
        }, body_update={"method": "tools/call", "params": {"name": "github___get_user", "arguments": {}}},
    )
    assert_denied(mismatched, "connection_binding_required")
    legacy_pat = signed_event(
        "request.json", claims_update={
            "connection_scopes": ["read:user"], "connection_id": "legacy-pat", "github_connection": None,
        }, body_update={"method": "tools/call", "params": {"name": "github___get_user", "arguments": {}}},
    )
    assert_denied(legacy_pat, "connection_binding_required")


def test_slack_write_forwards_only_signed_owned_binding_and_exact_arguments(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    binding = {
        "connection_id": "00000000-0000-4000-8000-000000000001",
        "revision": "00000000-0000-4000-8000-000000000002",
        "account_id": "T00000001:U00000001",
        "scopes_sha256": "a" * 64,
        "credential_sha256": "b" * 64,
    }
    arguments = {"channel": "C00000001", "text": "ADP isolated fixture message. [run:7d787d28-5c0e-4c86-b56f-fc13bb332b00]",
                 "run_marker": "7d787d28-5c0e-4c86-b56f-fc13bb332b00"}
    bound = {"connection_scopes": ["users:read", "chat:write"], "connection_id": binding["connection_id"],
             "slack_connection": binding, "rpc_id": 1}
    event = signed_event("request.json", claims_update=bound,
                         body_update={"method": "tools/call", "params": {"name": "slack___post_message", "arguments": arguments}})
    forwarded = handler(event, None)["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert {key: value for key, value in forwarded.items() if key != "adp_assertion"} == arguments
    assert forwarded["adp_assertion"] == event["mcp"]["gatewayRequest"]["headers"]["x-adp-assertion"]
    for changed in ({"connection_id": "other-owner"}, {"slack_connection": None}, {"connection_scopes": ["users:read"]}):
        denied = signed_event("request.json", claims_update={**bound, **changed},
                              body_update={"method": "tools/call", "params": {"name": "slack___post_message", "arguments": arguments}})
        result = handler(denied, None)["mcp"]
        assert "transformedGatewayRequest" not in result
        assert result["transformedGatewayResponse"]["statusCode"] == 403


@pytest.mark.parametrize("tool,scopes,expected", [
    ("jira___get_issue", ["read:jira-work"], False),
    ("jira___get_issue", [], False),
    ("jira___get_issue", ["users:read"], False),
    ("slack___get_user", ["users:read"], True),
    ("slack___post_message", ["users:read"], False),
    ("slack___post_message", [], False),
    ("slack___post_message", ["chat:write"], True),
    ("slack___unlisted", ["chat:write"], False),
])
def test_vendor_scope_is_checked_before_forwarding(kms, monkeypatch, tool, scopes, expected):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", claims_update={"connection_scopes": scopes, "rpc_id": 1},
                         body_update={"method": "tools/call", "params": {"name": tool, "arguments": {}}})
    result = handler(event, None)["mcp"]
    assert result["transformedGatewayResponse"]["body"]["error"]["message"] == (
        "connection_binding_required" if expected and tool.startswith("slack___") else
        "user_bound_target_unavailable" if expected else "scope_not_granted"
    )
    assert "transformedGatewayRequest" not in result


def test_forged_vendor_scopes_never_forward(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", claims_update={"connection_scopes": ["users:read"]},
                         body_update={"method": "tools/call", "params": {
                             "name": "slack___post_message", "arguments": {"channel": "C1", "text": "hi"},
                         }})
    headers = event["mcp"]["gatewayRequest"]["headers"]
    envelope = json.loads(base64.urlsafe_b64decode(headers["x-adp-assertion"] + "=="))
    envelope["claims"]["connection_scopes"] = ["chat:write"]
    headers["x-adp-assertion"] = _b64(_encoded(envelope))
    assert_denied(event, "untrusted_assertion")


def test_unscoped_slack_post_never_invokes_gateway_provider(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", claims_update={"connection_scopes": ["users:read"], "rpc_id": 1},
                         body_update={"method": "tools/call", "params": {
                             "name": "slack___post_message", "arguments": {"channel": "C123", "text": "hello"},
                         }})
    provider_calls = []

    def invoke_provider(forwarded_request):
        provider_calls.append(forwarded_request)
        return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

    request_result = handler(event, None)["mcp"]
    if "transformedGatewayRequest" in request_result:
        invoke_provider(request_result["transformedGatewayRequest"]["body"])
    assert provider_calls == []
    response = request_result["transformedGatewayResponse"]
    assert response["statusCode"] == 403
    assert response["body"]["error"]["message"] == "scope_not_granted"
    assert response["body"]["id"] == 1


@pytest.mark.parametrize("tenant,forwarded", [("tenant-1", True), ("other-tenant", False)])
def test_local_gateway_emulator_only_invokes_provider_after_request_allow(kms, monkeypatch, tenant, forwarded):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", tenant=tenant)
    request_result = handler(event, None)["mcp"]
    provider_calls = []
    if "transformedGatewayRequest" in request_result:
        provider_calls.append(request_result["transformedGatewayRequest"]["body"])
        response_event = {"interceptorInputVersion": "1.0", "mcp": {
            **event["mcp"],
            "gatewayResponse": {"statusCode": 200, "body": {
                "jsonrpc": "2.0", "id": 1, "result": {"tools": []},
            }},
        }}
        response_result = handler(response_event, None)["mcp"]["transformedGatewayResponse"]
        assert response_result["body"]["result"]["_meta"]["adp_receipt_id"] == "receipt-1"
    else:
        assert request_result["transformedGatewayResponse"]["body"]["error"]["message"] == "tenant_mismatch"
    assert bool(provider_calls) is forwarded


def test_signed_search_assertion_reaches_lambda_without_accepting_caller_override(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", body_update={"method": "tools/call", "params": {
        "name": "websearch___search", "arguments": {"query": "example"},
    }})
    original = event["mcp"]["gatewayRequest"]["body"]
    forwarded = handler(event, None)["mcp"]["transformedGatewayRequest"]["body"]
    assert forwarded["params"]["arguments"] == {
        "query": "example", "adp_assertion": event["mcp"]["gatewayRequest"]["headers"]["x-adp-assertion"],
    }
    assert original["params"]["arguments"] == {"query": "example"}
    event["mcp"]["gatewayRequest"]["body"] = forwarded
    event["mcp"]["gatewayResponse"] = {"statusCode": 200, "body": {
        "jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": "found"}]},
    }}
    result = handler(event, None)["mcp"]["transformedGatewayResponse"]
    assert result["body"]["result"]["_meta"]["adp_receipt_id"] == "receipt-1"
    forged = signed_event("request.json", body_update={"method": "tools/call", "params": {
        "name": "websearch___search", "arguments": {"query": "example", "adp_assertion": "forged"},
    }})
    assert_denied(forged, "tool_mismatch")


def test_request_phase_with_null_gateway_response_is_forwarded(kms, monkeypatch):
    # Live gateways deliver REQUEST events with "gatewayResponse": null; that is
    # not a response to transform, and treating it as one denied every call.
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json")
    event["mcp"]["gatewayResponse"] = None
    result = handler(event, None)["mcp"]
    assert result == {"transformedGatewayRequest": {"body": event["mcp"]["gatewayRequest"]["body"]}}


def test_streaming_envelope_without_body_passes_through(kms, monkeypatch):
    # Live gateways with response streaming deliver a first RESPONSE event that
    # has statusCode/headers but no body; the frames follow in later events.
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("response.json")
    event["mcp"]["gatewayResponse"] = {
        "statusCode": 200, "headers": {"Mcp-Session-Id": "s-1"}, "isStreamingResponse": True,
        "httpMethod": "POST", "path": "/mcp",
    }
    response = handler(event, None)["mcp"]["transformedGatewayResponse"]
    assert response == {"statusCode": 200, "headers": {"Mcp-Session-Id": "s-1"}}
    event["mcp"]["gatewayResponse"] = {"isStreamingResponse": True, "body": {
        "jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "found"}]},
    }}
    frame = handler(event, None)["mcp"]["transformedGatewayResponse"]
    assert set(frame) == {"body"}
    assert frame["body"]["result"]["_meta"]["adp_receipt_id"] == "receipt-1"


def test_knowledge_transport_injects_only_verified_argument_assertion(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", body_update={"method": "tools/call", "params": {
        "name": "knowledge___search", "arguments": {"query": "fixture-sentinel"},
    }})
    output = handler(event, None)["mcp"]["transformedGatewayRequest"]
    assert set(output) == {"body"}
    arguments = output["body"]["params"]["arguments"]
    assert arguments == {"query": "fixture-sentinel", "adp_assertion": event["mcp"]["gatewayRequest"]["headers"]["x-adp-assertion"]}
    event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"]["adp_assertion"] = "caller-forged"
    assert_denied(event, "tool_mismatch")


def test_knowledge_rpc_id_is_bound_before_forwarding(kms, monkeypatch):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", body_update={"method": "tools/call", "params": {
        "name": "knowledge___search", "arguments": {"query": "fixture-sentinel"},
    }})
    assert handler(event, None)["mcp"]["transformedGatewayRequest"]["body"]["id"] == 1
    event["mcp"]["gatewayRequest"]["body"]["id"] = 2
    assert_denied(event, "tool_mismatch")


@pytest.mark.parametrize(
    "signed_id,request_id",
    [(1, True), (1, 1.0), (True, 1), (1.0, 1), ("1", 1), (1, "1")],
)
def test_knowledge_rpc_id_type_must_match_before_forwarding(kms, monkeypatch, signed_id, request_id):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    event = signed_event("request.json", body_update={"method": "tools/call", "id": request_id, "params": {
        "name": "knowledge___search", "arguments": {"query": "fixture-sentinel"},
    }}, claims_update={"rpc_id": signed_id})
    assert_denied(event, "tool_mismatch")


@pytest.mark.parametrize("tool", ["atlassian___get_jira_issue", "atlassian___get_project_context"])
def test_atlassian_read_interception_binds_scope_site_connection_and_receipt(kms, monkeypatch, tool):
    monkeypatch.setattr("tools_interceptor.handler.time.time", lambda: 1000)
    arguments = {"cloudId": "site-1"}
    if tool == "atlassian___get_project_context":
        arguments = {"product_id": "product", "project_id": "project", "view": "sources"}
    release = {"group_generation": 1, "backend_generation": 1}
    connection = {"id": "connection-1", "revision": "revision-1", "site_id": "site-1"}
    claims = {
        "root_human_id": "user-1", "connection_id": "connection-1", "connection": connection,
        "adapter_release": release, "connection_scopes": ["read:jira:agent-interface"],
        "binding": {"operation_id": "1", "receipt_id": "receipt-1",
                    "payload_sha256": hashlib.sha256(_encoded(arguments)).hexdigest(), "release": release},
    }
    packet = {"method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    event = signed_event("request.json", body_update=packet, claims_update=claims)
    forwarded = handler(event, None)["mcp"]["transformedGatewayRequest"]["body"]
    assert forwarded["params"]["arguments"] == {
        **arguments, "adp_assertion": event["mcp"]["gatewayRequest"]["headers"]["x-adp-assertion"]
    }
    assert_denied(signed_event("request.json", body_update=packet,
                              claims_update={**claims, "connection_scopes": []}), "scope_not_granted")
    assert_denied(signed_event("request.json", body_update=packet,
                              claims_update={**claims, "connection": {**connection, "site_id": ""}}), "connection_binding_required")
    assert_denied(signed_event("request.json", body_update=packet,
                              claims_update={**claims, "binding": {**claims["binding"], "receipt_id": "other"}}),
                  "connection_binding_required")
