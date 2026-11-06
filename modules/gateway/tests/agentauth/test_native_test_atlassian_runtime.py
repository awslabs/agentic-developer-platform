"""Complete protected catalogue and exact argument contracts, offline."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.agentauth.native_test_atlassian_runtime import NativeAtlassianCatalogue, NativeAtlassianCatalogueBinding
from src.agentauth.native_test_enrollment import AllInApproval
from src.agentauth.native_test_facade_contract import ATLASSIAN_CALLS, NativeFacadeCall
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical, digest
from src.internal import tools_dynamic_catalogue as dynamic
from tests.internal.test_tools_dynamic_catalogue import deployed  # noqa: F401


@pytest.fixture
def catalogue(deployed, monkeypatch):  # noqa: F811
    target = deployed.target
    target["name"], target["targetId"] = "atlassian", "abcdefghij"
    arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/fixture-gateway"
    deployed.gateway["gatewayArn"] = target["gatewayArn"] = arn
    endpoint = "https://fixture.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/atlassian/mcp"
    target["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] = endpoint
    for key, value in {
        "MEDIATOR_ENABLED": "true",
        "SOURCE_REVISION": "a" * 40,
        "MEDIATOR_URL": endpoint,
        "GATEWAY_ID": "fixture-gateway",
        "TARGET_ID": "abcdefghij",
        "WRITES_ENABLED": "true",
    }.items():
        monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_" + key, value)
    tools = [
        {
            "name": tool.split("___")[1],
            "description": tool,
            "inputSchema": {"type": "object", "properties": {"cloudId": {"type": "string"}}, "required": ["cloudId"]},
        }
        for tool in sorted(ATLASSIAN_CALLS)
    ]
    schema_digest = hashlib.sha256(dynamic.canonical(tools)).hexdigest()
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_SHA256", schema_digest)
    actual = dynamic.target_binding("fixture-gateway", target, deployed.gateway)
    dynamic.publish_catalogue(binding=actual, tools=tools, client=deployed.store, table_name="fixture-authority", now=1000)
    binding = NativeAtlassianCatalogueBinding(
        gateway_arn=arn,
        gateway_role_arn=deployed.gateway["roleArn"],
        target_id="abcdefghij",
        catalogue_table="fixture-authority",
        target_binding_sha256=digest(actual),
        tools_sha256=schema_digest,
    )
    approval = AllInApproval.model_construct(tools=["atlassian.search_jira"], effects=[SimpleNamespace(source_sha256="d" * 64)])
    return SimpleNamespace(source=NativeAtlassianCatalogue(binding, "d" * 64), approval=approval, deployed=deployed, tools=tools)


def test_complete_pinned_catalogue_only_exposes_approved_operations(catalogue):
    result = catalogue.source.read(catalogue.approval)
    assert result["tools"] == [{"name": "atlassian.search_jira"}]
    assert [tool["name"] for tool in result["facade_tools"]] == ["atlassian___search_jira"]


@pytest.mark.parametrize("drift", ["target", "role", "source", "schema", "issuer", "expired", "tool", "release", "table"])
def test_incomplete_or_changed_catalogue_never_establishes_authority(catalogue, monkeypatch, drift):
    c = catalogue
    if drift == "target":
        c.deployed.target["name"] = "knowledge"
    elif drift == "role":
        c.deployed.gateway["roleArn"] += "-other"
    elif drift in {"source", "table"}:
        monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_SOURCE_REVISION" if drift == "source" else "AGENT_AUTHORITY_TABLE", "b" * 40)
    elif drift == "tool":
        c.approval = c.approval.model_copy(update={"tools": ["knowledge.search"]})
    elif drift == "release":
        c.approval.effects[0].source_sha256 = "e" * 64
    else:
        item = next(iter(c.deployed.items.values()))
        record = json.loads(item["document"]["S"])
        if drift == "schema":
            record["tools"][0]["inputSchema"]["properties"]["cloudId"]["type"] = "number"
        elif drift == "issuer":
            record["issuer"] = "untrusted"
        else:
            record["expires_at"] = 999
        item["document"]["S"] = json.dumps(record)
    with pytest.raises(AdmissionRefusedError):
        c.source.read(c.approval)


@pytest.mark.parametrize("tool", sorted(ATLASSIAN_CALLS))
def test_all_eight_operations_pin_every_argument_and_refuse_execution_extension(tool):
    value = {
        "schema_version": "1.0",
        "action": "call",
        "tool": tool,
        "arguments": {"cloudId": "owned", "summary": "☃"},
        "request_id": str(uuid4()),
        "execution_id": None,
        "ttl_seconds": None,
    }
    call = NativeFacadeCall.parse(canonical(value).encode())
    changed = deepcopy(value)
    changed["arguments"]["cloudId"] = "another"
    assert NativeFacadeCall.parse(canonical(changed).encode()).approved_digest() != call.approved_digest()
    for change in ({"execution_id": str(uuid4())}, {"arguments": {"cloudId": "owned", "adp_assertion": "forged"}}, {"arguments": {}}):
        with pytest.raises(AdmissionRefusedError):
            NativeFacadeCall.parse(canonical({**value, **change}).encode())
