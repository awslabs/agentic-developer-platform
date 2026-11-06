"""Offline protected Knowledge catalogue and fixed read-scope contracts."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.agentauth.native_test_enrollment import AllInApproval
from src.agentauth.native_test_facade_contract import NativeFacadeCall
from src.agentauth.native_test_knowledge_runtime import NativeKnowledgeCatalogue, NativeKnowledgeCatalogueBinding
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical
from src.internal import tools_dynamic_catalogue as dynamic
from tests.internal.test_tools_dynamic_catalogue import TOOLS, deployed  # noqa: F401


@pytest.fixture
def catalogue(deployed, monkeypatch):  # noqa: F811
    target_id = "abcdefghij"
    deployed.target["targetId"] = target_id
    gateway_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/fixture-gateway"
    deployed.gateway["gatewayArn"] = deployed.target["gatewayArn"] = gateway_arn
    monkeypatch.setenv("ADP_TOOLS_KNOWLEDGE_TARGET_ID", target_id)
    binding = dynamic.target_binding("fixture-gateway", deployed.target, deployed.gateway)
    dynamic.publish_catalogue(binding=binding, tools=TOOLS, client=deployed.store, table_name="fixture-authority", now=1000)
    pinned = NativeKnowledgeCatalogueBinding(
        gateway_arn=gateway_arn,
        gateway_role_arn=deployed.gateway["roleArn"],
        target_id=target_id,
        catalogue_table="fixture-authority",
        target_binding_sha256=hashlib.sha256(dynamic.canonical(binding)).hexdigest(),
        tools_sha256=hashlib.sha256(dynamic.canonical(TOOLS)).hexdigest(),
    )
    approval = AllInApproval.model_construct(tools=["knowledge.search"], effects=[SimpleNamespace(source_sha256="d" * 64)])
    return SimpleNamespace(source=NativeKnowledgeCatalogue(pinned, "d" * 64), approval=approval, deployed=deployed)


def test_current_private_catalogue_is_pinned_and_returns_actual_schema(catalogue):
    assert catalogue.source.read(catalogue.approval) == {
        "tools": [{"name": "knowledge.search"}],
        "facade_tools": [{**TOOLS[0], "name": "knowledge___search"}],
    }


@pytest.mark.parametrize(
    "fault", ["target", "role", "gateway", "status", "source", "schema", "issuer", "expired", "future", "ttl", "release", "write"]
)
def test_catalogue_drift_cannot_become_authority(catalogue, monkeypatch, fault):
    c = catalogue
    if fault == "target":
        c.deployed.target["targetId"] = "foreign-id"
    elif fault == "role":
        c.deployed.gateway["roleArn"] += "-foreign"
    elif fault == "gateway":
        c.deployed.gateway["gatewayArn"] += "-foreign"
    elif fault == "status":
        c.deployed.target["status"] = "DELETING"
    elif fault == "source":
        monkeypatch.setenv("ADP_TOOLS_KNOWLEDGE_SOURCE_REVISION", "b" * 40)
    elif fault == "release":
        c.approval.effects[0].source_sha256 = "e" * 64
    elif fault == "write":
        c.approval.tools.append("knowledge.remember")
    else:
        item = next(iter(c.deployed.items.values()))
        record = json.loads(item["document"]["S"])
        if fault == "schema":
            record["tools"][0]["inputSchema"]["properties"]["query"]["type"] = "number"
        elif fault == "issuer":
            record["issuer"] = "untrusted-fixture"
        elif fault == "expired":
            record["expires_at"] = 1000
        elif fault == "future":
            record["observed_at"] = 1001
        else:
            record["expires_at"] = 1000 + dynamic.KNOWLEDGE_TTL_SECONDS + 1  # one second past the knowledge lifetime (#8631)
        item["document"]["S"] = json.dumps(record)
    with pytest.raises(AdmissionRefusedError):
        c.source.read(c.approval)


@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("search", {"query": "private", "scope": "code"}),
        ("understand", {"project_id": "owned"}),
        ("impact", {"project_id": "owned"}),
        ("browse", {"action": "read"}),
        ("secure", {"action": "verify"}),
    ],
)
def test_protected_read_digest_binds_all_arguments(operation, arguments):
    body = {
        "schema_version": "1.0",
        "action": "call",
        "tool": "knowledge___" + operation,
        "arguments": arguments,
        "request_id": str(uuid4()),
        "execution_id": None,
        "ttl_seconds": None,
    }
    original = NativeFacadeCall.parse(canonical(body).encode())
    changed = deepcopy(body)
    changed["arguments"]["project_id"] = "foreign"
    assert original.approved_digest() != NativeFacadeCall.parse(canonical(changed).encode()).approved_digest()
    changed = {**body, "request_id": str(uuid4())}
    assert original.approved_digest() == NativeFacadeCall.parse(canonical(changed).encode()).approved_digest()


@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("remember", {}),
        ("experience", {"action": "recall"}),
        ("search", {"scope": "semantic"}),
        ("browse", {"action": "write"}),
        ("secure", {"action": "apply"}),
        ("search", {"user_id": "other"}),
    ],
)
def test_write_semantic_and_authority_arguments_are_refused(operation, arguments):
    body = {
        "schema_version": "1.0",
        "action": "call",
        "tool": "knowledge___" + operation,
        "arguments": arguments,
        "request_id": str(uuid4()),
        "execution_id": None,
        "ttl_seconds": None,
    }
    with pytest.raises(AdmissionRefusedError):
        NativeFacadeCall.parse(canonical(body).encode())
