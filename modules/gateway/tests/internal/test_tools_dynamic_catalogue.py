"""Synthetic control-plane fixtures prove provenance and metadata-only sync."""

import copy
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from src.internal import tools_catalogue
from src.internal import tools_dynamic_catalogue as dynamic

ROLE = "arn:aws:iam::123456789012:role/fixture-tools"
GATEWAY_ID = "fixture-gateway"
TARGET_ID = "fixture-target"
ENDPOINT = "https://fixture.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/knowledge/mcp"
TOOLS = [
    {
        "name": "search",
        "description": "Fixture search",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]


@pytest.fixture
def deployed(monkeypatch):
    for name, value in {
        "ADP_TOOLS_KNOWLEDGE_MEDIATOR_ENABLED": "true",
        "ADP_TOOLS_KNOWLEDGE_SOURCE_REVISION": "a" * 40,
        "ADP_TOOLS_KNOWLEDGE_MEDIATOR_URL": ENDPOINT,
        "ADP_TOOLS_KNOWLEDGE_GATEWAY_ID": GATEWAY_ID,
        "ADP_TOOLS_KNOWLEDGE_TARGET_ID": TARGET_ID,
        "ADP_TOOLS_GATEWAY_ROLE_ARN": ROLE,
        "AWS_REGION": "us-east-1",
        "AGENT_AUTHORITY_TABLE": "fixture-authority",
    }.items():
        monkeypatch.setenv(name, value)
    gateway = {
        "gatewayId": GATEWAY_ID,
        "gatewayArn": "fixture-gateway-arn",
        "roleArn": ROLE,
        "status": "READY",
        "authorizerType": "AWS_IAM",
        "interceptorConfigurations": [{"fixture": "interceptor"}],
        "updatedAt": "2026-01-01T00:00:00Z",
    }
    target = {
        "targetId": TARGET_ID,
        "gatewayArn": gateway["gatewayArn"],
        "name": "knowledge",
        "status": "READY",
        "updatedAt": "2026-01-01T00:00:00Z",
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": ENDPOINT, "listingMode": "DYNAMIC"}}},
        "privateEndpoint": {"managedVpcResource": {"vpcIdentifier": "vpc-00000000"}},
        "credentialProviderConfigurations": [
            {
                "credentialProviderType": "GATEWAY_IAM_ROLE",
                "credentialProvider": {
                    "iamCredentialProvider": {"service": "execute-api"},
                },
            }
        ],
    }
    writes = []
    items = {}
    lock = threading.Lock()

    def key(values):
        return values["pk"]["S"], values["sk"]["S"]

    def put(**kwargs):
        with lock:
            if kwargs.get("ConditionExpression") == "attribute_not_exists(pk)" and key(kwargs["Item"]) in items:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "fixture replay"}},
                    "PutItem",
                )
            writes.append(kwargs)
            items[key(kwargs["Item"])] = copy.deepcopy(kwargs["Item"])

    def transact(**kwargs):
        operations = [entry["Put"] for entry in kwargs["TransactItems"]]
        with lock:
            for operation in operations:
                current = items.get(key(operation["Item"]))
                if (
                    current
                    and "ConditionExpression" in operation
                    and int(current["observed_at"]["N"]) > int(operation["ExpressionAttributeValues"][":now"]["N"])
                ):
                    raise ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "stale publication"}}, "TransactWriteItems")
            for operation in operations:
                writes.append(operation)
                items[key(operation["Item"])] = copy.deepcopy(operation["Item"])

    store = SimpleNamespace(put_item=put, transact_write_items=transact, get_item=lambda **kwargs: {"Item": items.get(key(kwargs["Key"]))})
    control = SimpleNamespace(
        get_gateway=lambda **kwargs: copy.deepcopy(gateway),
        get_gateway_target=lambda **kwargs: copy.deepcopy(target),
        get_paginator=lambda operation: SimpleNamespace(
            paginate=lambda **kwargs: [
                {
                    "items": [
                        {"name": "adp-tools", "gatewayId": GATEWAY_ID}
                        if operation == "list_gateways"
                        else {"name": "knowledge", "targetId": TARGET_ID, "status": target["status"]}
                    ]
                }
            ]
        ),
    )
    monkeypatch.setattr(dynamic.boto3, "client", lambda name, **kwargs: store if name == "dynamodb" else control)
    monkeypatch.setattr(dynamic.time, "time", lambda: 1000)
    return SimpleNamespace(gateway=gateway, target=target, store=store, control=control, writes=writes, items=items)


def publish(deployed):
    binding = dynamic.target_binding(GATEWAY_ID, deployed.target, deployed.gateway)
    dynamic.publish_catalogue(binding=binding, tools=TOOLS, client=deployed.store, table_name="fixture-authority", now=1000)


def test_knowledge_tools_stay_listed_after_five_minutes_but_expire_at_the_bound(deployed, monkeypatch):
    from src.internal.tools_catalogue import gateway_catalogue

    publish(deployed)
    monkeypatch.setattr(dynamic.time, "time", lambda: 1301)
    tools_catalogue.invalidate_catalogue_cache()
    assert [tool["name"] for tool in gateway_catalogue()["tools"]] == ["knowledge.search"]
    monkeypatch.setattr(dynamic.time, "time", lambda: 1000 + dynamic.KNOWLEDGE_TTL_SECONDS)
    tools_catalogue.invalidate_catalogue_cache()
    result = gateway_catalogue()
    assert result["tools"] == []
    assert result["connectors"][0]["discovery_status"] == "unavailable"


def test_seven_knowledge_tools_survive_five_idle_minutes_without_refresh_probes(deployed, monkeypatch):
    names = {"search", "understand", "impact", "browse", "secure", "remember", "experience"}
    tools = [{"name": name, "description": "Offline knowledge fixture", "inputSchema": {"type": "object"}} for name in sorted(names)]
    binding = dynamic.target_binding(GATEWAY_ID, deployed.target, deployed.gateway)
    dynamic.publish_catalogue(binding=binding, tools=tools, client=deployed.store, table_name="fixture-authority", now=1000)
    clock = {"wall": 1000, "elapsed": 0}
    monkeypatch.setattr(dynamic.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(tools_catalogue.time, "monotonic", lambda: clock["elapsed"])
    tools_catalogue.invalidate_catalogue_cache()
    try:
        initial = tools_catalogue.gateway_catalogue()
        assert {tool["name"] for tool in initial["tools"]} == {"knowledge." + name for name in names}
        clock.update(wall=1301, elapsed=301)
        after_idle = tools_catalogue.gateway_catalogue()
        assert {tool["name"] for tool in after_idle["tools"]} == {"knowledge." + name for name in names}
        assert after_idle["connectors"][0]["discovery_status"] == "ready"
    finally:
        tools_catalogue.invalidate_catalogue_cache()


def test_stale_knowledge_provenance_recovers_after_producer_refresh_without_hiding_static_tools(deployed, monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace

    from src.internal import tools_catalogue
    from src.internal import tools_dynamic_catalogue as dynamic

    publish(deployed)
    static = {
        "name": "websearch",
        "targetConfiguration": {
            "mcp": {
                "lambda": {
                    "toolSchema": {
                        "inlinePayload": [
                            {"name": "search", "description": "Offline search", "inputSchema": {"type": "object"}},
                        ]
                    }
                }
            }
        },
    }
    original_paginator = deployed.control.get_paginator
    monkeypatch.setattr(
        deployed.control,
        "get_paginator",
        lambda operation: (
            SimpleNamespace(
                paginate=lambda **kwargs: [
                    {
                        "items": [
                            {"targetId": TARGET_ID, "status": "READY"},
                            {"targetId": "static-target", "status": "READY"},
                        ]
                    }
                ]
            )
            if operation == "list_gateway_targets"
            else original_paginator(operation)
        ),
    )
    monkeypatch.setattr(
        deployed.control,
        "get_gateway_target",
        lambda **kwargs: (deepcopy(static) if kwargs["targetId"] == "static-target" else deepcopy(deployed.target)),
    )
    clock = {"wall": 1000, "elapsed": 0}
    monkeypatch.setattr(dynamic.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(tools_catalogue.time, "monotonic", lambda: clock["elapsed"])
    tools_catalogue.invalidate_catalogue_cache()
    try:
        assert {tool["name"] for tool in tools_catalogue.gateway_catalogue()["tools"]} == {"knowledge.search", "websearch.search"}
        clock.update(wall=1301, elapsed=301)
        assert {tool["name"] for tool in tools_catalogue.gateway_catalogue()["tools"]} == {"knowledge.search", "websearch.search"}
        clock.update(wall=1601, elapsed=601)
        stale = tools_catalogue.gateway_catalogue()
        assert [tool["name"] for tool in stale["tools"]] == ["websearch.search"]
        assert next(entry for entry in stale["connectors"] if entry["name"] == "knowledge")["discovery_reason"] == (
            "dynamic_catalogue_provenance_unavailable"
        )
        dynamic.publish_catalogue(
            binding=dynamic.target_binding(GATEWAY_ID, deployed.target, deployed.gateway),
            tools=[{"name": "search", "description": "Fixture search", "inputSchema": {"type": "object"}}],
            client=deployed.store,
            table_name="fixture-authority",
            now=1601,
        )
        clock.update(wall=1722, elapsed=722)
        refreshed = tools_catalogue.gateway_catalogue()
        assert {tool["name"] for tool in refreshed["tools"]} == {"knowledge.search", "websearch.search"}
        assert next(entry for entry in refreshed["connectors"] if entry["name"] == "knowledge")["discovery_status"] == "ready"
    finally:
        tools_catalogue.invalidate_catalogue_cache()


def test_other_private_connectors_keep_shorter_provenance_lifetime(deployed, monkeypatch):
    endpoint = ENDPOINT.replace("/knowledge/mcp", "/github/mcp")
    for name, value in {
        "ADP_TOOLS_GITHUB_MEDIATOR_ENABLED": "true",
        "ADP_TOOLS_GITHUB_SOURCE_REVISION": "a" * 40,
        "ADP_TOOLS_GITHUB_MEDIATOR_URL": endpoint,
        "ADP_TOOLS_GITHUB_GATEWAY_ID": GATEWAY_ID,
        "ADP_TOOLS_GITHUB_TARGET_ID": TARGET_ID,
    }.items():
        monkeypatch.setenv(name, value)
    deployed.target["name"] = "github"
    deployed.target["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] = endpoint
    binding = dynamic.target_binding(GATEWAY_ID, deployed.target, deployed.gateway)
    dynamic.publish_catalogue(binding=binding, tools=TOOLS, client=deployed.store, table_name="fixture-authority", now=1000)
    monkeypatch.setattr(dynamic.time, "time", lambda: 1000 + dynamic.TTL_SECONDS)
    assert dynamic.read_catalogue(GATEWAY_ID, deployed.target, deployed.gateway) is None


def test_only_fresh_authentic_producer_metadata_becomes_callable(deployed):
    assert dynamic.read_catalogue(GATEWAY_ID, deployed.target, deployed.gateway) is None
    publish(deployed)
    assert dynamic.read_catalogue(GATEWAY_ID, deployed.target, deployed.gateway) == TOOLS
    assert len(deployed.writes) == 1
    item = deployed.writes[0]["Item"]
    assert item["pk"]["S"].startswith("TOOLS_DYNAMIC_CATALOGUE#")
    assert not any(field in item for field in ("allowed_tools", "consented_tools", "tool_max_cost_usd", "tenant_id", "owner_id"))


@pytest.mark.parametrize("namespace", ["github", "jira", "slack", "atlassian"])
def test_vendor_uses_its_own_pinned_private_target_and_catalogue(deployed, monkeypatch, namespace):
    endpoint = ENDPOINT.replace("/knowledge/mcp", f"/{namespace}/mcp")
    for name, value in {
        f"ADP_TOOLS_{namespace.upper()}_MEDIATOR_ENABLED": "true",
        f"ADP_TOOLS_{namespace.upper()}_SOURCE_REVISION": "a" * 40,
        f"ADP_TOOLS_{namespace.upper()}_MEDIATOR_URL": endpoint,
        f"ADP_TOOLS_{namespace.upper()}_GATEWAY_ID": GATEWAY_ID,
        f"ADP_TOOLS_{namespace.upper()}_TARGET_ID": TARGET_ID,
    }.items():
        monkeypatch.setenv(name, value)
    deployed.target["name"] = namespace
    deployed.target["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] = endpoint
    assert dynamic.read_catalogue(GATEWAY_ID, deployed.target, deployed.gateway) is None
    binding = dynamic.target_binding(GATEWAY_ID, deployed.target, deployed.gateway)
    tools = TOOLS
    if namespace == "atlassian":
        tools = [
            {"name": name, "description": name, "inputSchema": TOOLS[0]["inputSchema"]}
            for name in ("search_jira", "get_jira_issue", "search_confluence", "get_confluence_page")
        ]
        monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_READS_SHA256", hashlib.sha256(dynamic.canonical(tools)).hexdigest())
    dynamic.publish_catalogue(binding=binding, tools=tools, client=deployed.store, table_name="fixture-authority", now=1000)
    assert dynamic.read_catalogue(GATEWAY_ID, deployed.target, deployed.gateway) == tools
    monkeypatch.setenv(f"ADP_TOOLS_{namespace.upper()}_TARGET_ID", "other-target")
    assert dynamic.read_catalogue(GATEWAY_ID, deployed.target, deployed.gateway) is None


def test_atlassian_write_catalogue_requires_exact_eight_tool_digest(deployed, monkeypatch):
    endpoint = ENDPOINT.replace("/knowledge/mcp", "/atlassian/mcp")
    for name, value in {
        "ADP_TOOLS_ATLASSIAN_MEDIATOR_ENABLED": "true",
        "ADP_TOOLS_ATLASSIAN_WRITES_ENABLED": "true",
        "ADP_TOOLS_ATLASSIAN_SOURCE_REVISION": "a" * 40,
        "ADP_TOOLS_ATLASSIAN_MEDIATOR_URL": endpoint,
        "ADP_TOOLS_ATLASSIAN_GATEWAY_ID": GATEWAY_ID,
        "ADP_TOOLS_ATLASSIAN_TARGET_ID": TARGET_ID,
    }.items():
        monkeypatch.setenv(name, value)
    deployed.target["name"] = "atlassian"
    deployed.target["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] = endpoint
    binding = dynamic.target_binding(GATEWAY_ID, deployed.target, deployed.gateway)
    names = (
        "search_jira",
        "get_jira_issue",
        "search_confluence",
        "get_confluence_page",
        "create_jira_issue",
        "update_jira_issue",
        "comment_jira_issue",
        "transition_jira_issue",
    )
    tools = [{"name": name, "description": name, "inputSchema": TOOLS[0]["inputSchema"]} for name in names]
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_SHA256", hashlib.sha256(dynamic.canonical(tools)).hexdigest())
    dynamic.publish_catalogue(binding=binding, tools=tools, client=deployed.store, table_name="fixture-authority", now=1000)
    assert dynamic.read_catalogue(GATEWAY_ID, deployed.target, deployed.gateway) == tools
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_SHA256", "0" * 64)
    assert dynamic.read_catalogue(GATEWAY_ID, deployed.target, deployed.gateway) is None


def invalidate_provenance(deployed, monkeypatch, change):
    if change == "expired":
        monkeypatch.setattr(dynamic.time, "time", lambda: 1000 + dynamic.KNOWLEDGE_TTL_SECONDS)
    elif change == "future":
        monkeypatch.setattr(dynamic.time, "time", lambda: 999)
    elif change == "source":
        monkeypatch.setenv("ADP_TOOLS_KNOWLEDGE_SOURCE_REVISION", "b" * 40)
    elif change == "endpoint":
        deployed.target["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] += "/changed"
    elif change == "target":
        deployed.target["targetId"] = "other-target"
    elif change == "target_update":
        deployed.target["updatedAt"] = "changed"
    elif change == "gateway_update":
        deployed.gateway["updatedAt"] = "changed"
    elif change == "gateway_config":
        deployed.gateway["interceptorConfigurations"] = [{"different": "interceptor"}]
    elif change == "removed":
        deployed.target["status"] = "DELETING"
    elif change == "static":
        deployed.target["targetConfiguration"]["mcp"]["mcpServer"]["listingMode"] = "DEFAULT"
    else:
        item = next(iter(deployed.items.values()))
        document = json.loads(item["document"]["S"])
        document["tools_sha256" if change == "hash" else "issuer"] = "forged"
        item["document"]["S"] = json.dumps(document)


@pytest.mark.parametrize(
    "change",
    ["expired", "future", "source", "endpoint", "target", "target_update", "gateway_update", "gateway_config", "removed", "static", "hash", "issuer"],
)
def test_changed_or_stale_provenance_never_produces_tools(deployed, monkeypatch, change):
    publish(deployed)
    invalidate_provenance(deployed, monkeypatch, change)
    assert dynamic.read_catalogue(GATEWAY_ID, deployed.target, deployed.gateway) is None


@pytest.mark.parametrize("status", ["CREATING", "UPDATING", "SYNCHRONIZING"])
def test_bootstrap_has_no_callable_authority(deployed, monkeypatch, status):
    deployed.target["status"] = status
    monkeypatch.delenv("ADP_TOOLS_KNOWLEDGE_TARGET_ID")
    binding = dynamic.target_binding(GATEWAY_ID, deployed.target, deployed.gateway, metadata_only=True)
    assert binding["target_status"] == status
    with pytest.raises(ValueError):
        dynamic.target_binding(GATEWAY_ID, deployed.target, deployed.gateway)


@pytest.mark.parametrize("change", ["duplicate", "authority", "foreign_name", "not_object", "oversized"])
def test_malformed_metadata_cannot_be_published(deployed, change):
    tools = copy.deepcopy(TOOLS)
    if change == "duplicate":
        tools *= 2
    elif change == "authority":
        tools[0]["inputSchema"]["properties"]["tenant_id"] = {"type": "string"}
    elif change == "foreign_name":
        tools[0]["name"] = "browser___start"
    elif change == "not_object":
        tools[0]["inputSchema"]["type"] = "string"
    else:
        tools[0]["description"] = "a" * dynamic.MAX_BYTES
    with pytest.raises(ValueError):
        dynamic.publish_catalogue(binding={}, tools=tools, client=deployed.store, table_name="fixture-authority")
    assert deployed.writes == []


def test_gateway_consumer_uses_producer_and_removes_stale_target(deployed):
    from src.internal.tools_catalogue import gateway_catalogue

    publish(deployed)
    result = gateway_catalogue()
    assert [entry["name"] for entry in result["tools"]] == ["knowledge.search"]
    assert result["connectors"][0]["discovery_status"] == "ready"
    deployed.target["status"] = "DELETING"
    tools_catalogue.invalidate_catalogue_cache()  # a tools deploy; the cache is at most CATALOGUE_TTL_SECONDS stale
    assert gateway_catalogue() == {"tools": [], "connectors": []}


def test_removed_target_drops_stored_schema_without_hiding_engine(deployed, monkeypatch):
    from src.internal.tools_catalogue import gateway_catalogue

    publish(deployed)
    original_paginator = deployed.control.get_paginator
    original_target = deployed.control.get_gateway_target
    present = [True]
    core = {
        "name": "websearch",
        "targetConfiguration": {
            "mcp": {
                "lambda": {"toolSchema": {"inlinePayload": [{"name": "search", "description": "Engine search", "inputSchema": {"type": "object"}}]}}
            }
        },
    }

    def paginator(operation):
        if operation != "list_gateway_targets":
            return original_paginator(operation)
        items = [{"targetId": "fixture-core", "status": "READY"}]
        if present[0]:
            items.append({"targetId": TARGET_ID, "status": "READY"})
        return SimpleNamespace(paginate=lambda **kwargs: [{"items": items}])

    def target(**kwargs):
        if kwargs["targetId"] == "fixture-core":
            return copy.deepcopy(core)
        return original_target(**kwargs)

    monkeypatch.setattr(deployed.control, "get_paginator", paginator)
    monkeypatch.setattr(deployed.control, "get_gateway_target", target)
    before = gateway_catalogue()
    assert [tool["name"] for tool in before["tools"]] == ["knowledge.search", "websearch.search"]
    assert next(connector for connector in before["connectors"] if connector["name"] == "knowledge")["discovery_status"] == "ready"

    present[0] = False
    tools_catalogue.invalidate_catalogue_cache()
    after = gateway_catalogue()
    assert [tool["name"] for tool in after["tools"]] == ["websearch.search"]


@pytest.mark.parametrize("system_context", [False, True])
def test_atlassian_named_reads_reach_real_gateway_catalogue_only_with_current_provenance(deployed, monkeypatch, system_context):
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_SYSTEM_ENABLED", str(system_context).lower())
    namespace = "atlassian"
    endpoint = ENDPOINT.replace("/knowledge/mcp", "/atlassian/mcp")
    for name, value in {
        "ADP_TOOLS_ATLASSIAN_MEDIATOR_ENABLED": "true",
        "ADP_TOOLS_ATLASSIAN_SOURCE_REVISION": "a" * 40,
        "ADP_TOOLS_ATLASSIAN_MEDIATOR_URL": endpoint,
        "ADP_TOOLS_ATLASSIAN_GATEWAY_ID": GATEWAY_ID,
        "ADP_TOOLS_ATLASSIAN_TARGET_ID": TARGET_ID,
    }.items():
        monkeypatch.setenv(name, value)
    deployed.target["name"] = namespace
    deployed.target["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] = endpoint
    original_paginator = deployed.control.get_paginator
    deployed.control.get_paginator = (
        lambda operation: SimpleNamespace(paginate=lambda **kwargs: [{"items": [{"name": namespace, "targetId": TARGET_ID, "status": "READY"}]}])
        if operation == "list_gateway_targets"
        else original_paginator(operation)
    )
    monkeypatch.delenv("ADP_TOOLS_ATLASSIAN_TARGET_ID")
    expected_names = {"search_jira", "get_jira_issue", "search_confluence", "get_confluence_page"}
    if system_context:
        expected_names.add("get_project_context")
    schemas = [
        {
            "name": name,
            "description": "fixture " + name,
            "inputSchema": {"type": "object", "properties": {"cloudId": {"type": "string"}}, "required": ["cloudId"]},
        }
        for name in sorted(expected_names)
    ]
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.internal import tool_adapter_authority as authority
    from src.internal.auth_deps import verify_internal_or_irsa

    release = {
        "contract_version": "1.0",
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "group": "tool-connectors",
        "group_generation": 1,
        "backend_generation": 1,
    }
    role = "arn:aws:iam::123456789012:role/fixture-adapter"
    monkeypatch.setenv("ADP_TOOL_ADAPTER_ADAPTER_ROLE_ARN", role)
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_READS_SHA256", hashlib.sha256(dynamic.canonical(schemas)).hexdigest())
    monkeypatch.setattr(authority, "observation_settings", lambda: (None, None, None, release))
    monkeypatch.setattr(authority, "verified_caller_identity", lambda request: role)
    monkeypatch.setattr(authority, "get_settings", lambda: SimpleNamespace(atlassian_identity_compatibility_verified=True))
    app = FastAPI()
    app.include_router(authority.router)

    async def iam(request: Request):
        request.state.token_context = SimpleNamespace(
            auth_source="iam",
            user_id="adapter",
            scope="internal",
            credential_scopes={"tools:adapter:observe"},
        )

    app.dependency_overrides[verify_internal_or_irsa] = iam
    with TestClient(app) as client:
        request = {"schema_version": "1.0", "release": release, "tools": schemas}
        assert client.post("/internal/v1/tool-adapters/catalogue", json=request).status_code == 200
        bad = {**request, "tools": schemas[:-1]}
        assert client.post("/internal/v1/tool-adapters/catalogue", json=bad).status_code == (503 if system_context else 422)
        oversized = [dict(tool) for tool in schemas]
        oversized[0]["description"] = "x" * (dynamic.MAX_BYTES + 1)
        rejected = client.post("/internal/v1/tool-adapters/catalogue", json={**request, "tools": oversized})
        assert rejected.status_code == 503
        assert rejected.json()["detail"]["error"] == "catalogue_incomplete"
        assert len(deployed.writes) == 1
        oversized_definition = copy.deepcopy(schemas)
        oversized_definition[0]["inputSchema"]["properties"]["cloudId"]["description"] = "x" * (dynamic.MAX_BYTES + 1)
        refused = client.post("/internal/v1/tool-adapters/catalogue", json={**request, "tools": oversized_definition})
        assert refused.status_code == 503 and refused.json()["detail"]["error"] == "catalogue_incomplete"
        assert len(deployed.writes) == 1
        large = copy.deepcopy(schemas)
        for tool in large:
            tool["inputSchema"]["properties"]["cloudId"]["description"] = "s" * 180_000
        monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_READS_SHA256", hashlib.sha256(dynamic.canonical(large)).hexdigest())
        assert client.post("/internal/v1/tool-adapters/catalogue", json={**request, "tools": large}).status_code == 200
        assert len(deployed.writes) == len(schemas) + 2
        monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_TARGET_ID", TARGET_ID)
        tools_catalogue.invalidate_catalogue_cache()
        assert len(tools_catalogue.gateway_catalogue()["tools"]) == len(schemas)
        monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_READS_SHA256", hashlib.sha256(dynamic.canonical(schemas)).hexdigest())
        assert client.post("/internal/v1/tool-adapters/catalogue", json=request).status_code == 200
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_TARGET_ID", TARGET_ID)
    tools_catalogue.invalidate_catalogue_cache()
    try:
        result = tools_catalogue.gateway_catalogue()
        assert {tool["name"] for tool in result["tools"]} == {"atlassian." + name for name in expected_names}
        assert all(tool["connector"] == "atlassian" and tool["inputSchema"]["required"] == ["cloudId"] for tool in result["tools"])
        binding = dynamic.target_binding(GATEWAY_ID, deployed.target, deployed.gateway)
        partial = schemas[:-1]
        dynamic.publish_catalogue(binding=binding, tools=partial, client=deployed.store, table_name="fixture-authority", now=1000)
        for expected in (schemas, partial):
            monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_READS_SHA256", hashlib.sha256(dynamic.canonical(expected)).hexdigest())
            tools_catalogue.invalidate_catalogue_cache()
            incomplete = tools_catalogue.gateway_catalogue()
            assert incomplete["tools"] == []
            assert next(entry for entry in incomplete["connectors"] if entry["name"] == "atlassian")["discovery_status"] == "unavailable"
        monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_READS_SHA256", hashlib.sha256(dynamic.canonical(schemas)).hexdigest())
        dynamic.publish_catalogue(binding=binding, tools=schemas, client=deployed.store, table_name="fixture-authority", now=1000)
        tools_catalogue.invalidate_catalogue_cache()
        assert len(tools_catalogue.gateway_catalogue()["tools"]) == len(schemas)
        deployed.target["updatedAt"] = "2026-01-02T00:00:00Z"
        tools_catalogue.invalidate_catalogue_cache()
        assert tools_catalogue.gateway_catalogue()["tools"] == []
    finally:
        tools_catalogue.invalidate_catalogue_cache()


@pytest.fixture
def mixed_catalogue(deployed, monkeypatch):
    """Use the actual catalogue reader and producer with static and dynamic targets."""
    original_paginator = deployed.control.get_paginator
    original_target = deployed.control.get_gateway_target
    calls = []
    core = {
        "targetId": "fixture-core",
        "name": "websearch",
        "targetConfiguration": {
            "mcp": {
                "lambda": {"toolSchema": {"inlinePayload": [{"name": "search", "description": "Static search", "inputSchema": {"type": "object"}}]}}
            }
        },
    }

    def paginator(operation):
        calls.append(operation)
        if operation != "list_gateway_targets":
            return original_paginator(operation)
        return SimpleNamespace(
            paginate=lambda **kwargs: [{"items": [{"targetId": TARGET_ID, "status": "READY"}, {"targetId": "fixture-core", "status": "READY"}]}]
        )

    def target(**kwargs):
        calls.append(kwargs["targetId"])
        return copy.deepcopy(core) if kwargs["targetId"] == "fixture-core" else original_target(**kwargs)

    monkeypatch.setattr(deployed.control, "get_paginator", paginator)
    monkeypatch.setattr(deployed.control, "get_gateway_target", target)
    monkeypatch.setattr(tools_catalogue.time, "monotonic", lambda: 0)
    return SimpleNamespace(deployed=deployed, calls=calls)


def test_cached_misses_expiry_and_producer_refresh_are_observed_by_each_replica(mixed_catalogue, monkeypatch):
    """Two process caches must recover from the shared producer without cache invalidation."""
    caches = [{}, {}]

    def read_each():
        results = []
        for cache in caches:
            monkeypatch.setattr(tools_catalogue, "_catalogue_cache", cache)
            results.append(tools_catalogue.gateway_catalogue())
        return results

    def assert_names(results, names):
        assert [[tool["name"] for tool in result["tools"]] for result in results] == [names, names]
        assert all(set(result) == {"tools", "connectors"} for result in results)

    assert_names(read_each(), ["websearch.search"])
    publish(mixed_catalogue.deployed)
    ready = read_each()
    assert_names(ready, ["knowledge.search", "websearch.search"])
    assert all(result["connectors"][0]["discovery_status"] == "ready" for result in ready)
    ready[0]["tools"][0]["inputSchema"]["properties"].clear()
    assert read_each()[0]["tools"][0]["inputSchema"]["properties"] == {"query": {"type": "string"}}

    expired = 1000 + dynamic.KNOWLEDGE_TTL_SECONDS
    monkeypatch.setattr(dynamic.time, "time", lambda: expired)
    assert_names(read_each(), ["websearch.search"])
    deployed = mixed_catalogue.deployed
    dynamic.publish_catalogue(
        binding=dynamic.target_binding(GATEWAY_ID, deployed.target, deployed.gateway),
        tools=TOOLS,
        client=deployed.store,
        table_name="fixture-authority",
        now=expired,
    )
    assert_names(read_each(), ["knowledge.search", "websearch.search"])
    assert mixed_catalogue.calls.count("fixture-core") == 2
    assert mixed_catalogue.calls.count("list_gateways") == 2
    assert mixed_catalogue.calls.count("list_gateway_targets") == 2


@pytest.mark.parametrize(
    "change",
    ["expired", "future", "source", "endpoint", "target", "target_update", "gateway_update", "gateway_config", "removed", "static", "hash", "issuer"],
)
def test_process_cache_cannot_bypass_current_dynamic_provenance(mixed_catalogue, monkeypatch, change):
    deployed = mixed_catalogue.deployed
    publish(deployed)
    assert [tool["name"] for tool in tools_catalogue.gateway_catalogue()["tools"]] == ["knowledge.search", "websearch.search"]
    invalidate_provenance(deployed, monkeypatch, change)
    result = tools_catalogue.gateway_catalogue()
    assert [tool["name"] for tool in result["tools"]] == ["websearch.search"]
    assert result["connectors"][0]["discovery_status"] == "unavailable"
    assert result["connectors"][0]["discovery_reason"] == "dynamic_catalogue_provenance_unavailable"


@pytest.mark.parametrize("operation", ["get_gateway", "get_gateway_target", "get_item"])
def test_failed_dynamic_read_removes_cached_tools_and_recovers_without_losing_static_tools(mixed_catalogue, monkeypatch, operation):
    deployed = mixed_catalogue.deployed
    publish(deployed)
    assert len(tools_catalogue.gateway_catalogue()["tools"]) == 2
    client = deployed.store if operation == "get_item" else deployed.control
    original = getattr(client, operation)

    def unavailable(**kwargs):
        raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "fixture unavailable"}}, operation)

    monkeypatch.setattr(client, operation, unavailable)
    failed = tools_catalogue.gateway_catalogue()
    assert [tool["name"] for tool in failed["tools"]] == ["websearch.search"]
    assert failed["connectors"][0]["discovery_status"] == "unavailable"
    monkeypatch.setattr(client, operation, original)
    recovered = tools_catalogue.gateway_catalogue()
    assert [tool["name"] for tool in recovered["tools"]] == ["knowledge.search", "websearch.search"]
    assert mixed_catalogue.calls.count("fixture-core") == 1


@pytest.mark.parametrize("definition_size", [0, 200_000, 260_000])
def test_atlassian_current_provenance_with_static_tool_and_no_cache_invalidation(mixed_catalogue, monkeypatch, definition_size):
    deployed = mixed_catalogue.deployed
    endpoint = ENDPOINT.replace("/knowledge/mcp", "/atlassian/mcp")
    for key, value in {
        "ADP_TOOLS_ATLASSIAN_MEDIATOR_ENABLED": "true",
        "ADP_TOOLS_ATLASSIAN_SOURCE_REVISION": "a" * 40,
        "ADP_TOOLS_ATLASSIAN_MEDIATOR_URL": endpoint,
        "ADP_TOOLS_ATLASSIAN_GATEWAY_ID": GATEWAY_ID,
        "ADP_TOOLS_ATLASSIAN_TARGET_ID": TARGET_ID,
    }.items():
        monkeypatch.setenv(key, value)
    deployed.target["name"] = "atlassian"
    deployed.target["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"] = endpoint
    names = ("search_jira", "get_jira_issue", "search_confluence", "get_confluence_page")
    schema = {"type": "object", "properties": {"cloudId": {"type": "string", "description": "s" * definition_size}}}
    schemas = [{"name": name, "description": name, "inputSchema": schema} for name in names]
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_READS_SHA256", hashlib.sha256(dynamic.canonical(schemas)).hexdigest())
    binding = dynamic.target_binding(GATEWAY_ID, deployed.target, deployed.gateway)

    def catalogue():
        return tools_catalogue.gateway_catalogue()

    def assert_names(expected):
        result = catalogue()
        assert {tool["name"] for tool in result["tools"]} == set(expected)
        status = next(connector for connector in result["connectors"] if connector["name"] == "atlassian")
        assert status["discovery_status"] == ("ready" if len(expected) > 1 else "unavailable")

    assert_names({"websearch.search"})
    dynamic.publish_catalogue(binding=binding, tools=schemas, client=deployed.store, table_name="fixture-authority", now=1000)
    ready = {"websearch.search", *("atlassian." + name for name in names)}
    assert_names(ready)
    if definition_size:
        root = deployed.items[("TOOLS_DYNAMIC_CATALOGUE#" + GATEWAY_ID, "TARGET#" + TARGET_ID)]
        assert json.loads(root["document"]["S"])["version"] == "2"
        assert len(deployed.writes) == 5
        assert all(len(item["Item"]["document"]["S"].encode()) < 400_000 for item in deployed.writes)
        original_root = copy.deepcopy(root)
        malformed = json.loads(root["document"]["S"])
        malformed["tool_order"] = [names[0]] * 1000
        root["document"]["S"] = json.dumps(malformed)
        assert_names({"websearch.search"})
        deployed.items[("TOOLS_DYNAMIC_CATALOGUE#" + GATEWAY_ID, "TARGET#" + TARGET_ID)] = original_root
        assert_names(ready)
        child_key = ("TOOLS_DYNAMIC_CATALOGUE#" + GATEWAY_ID, "TARGET#" + TARGET_ID + "#SCHEMA#" + names[0])
        child = deployed.items.pop(child_key)
        assert_names({"websearch.search"})
        deployed.items[child_key] = copy.deepcopy(child)
        assert_names(ready)
        changed = json.loads(deployed.items[child_key]["document"]["S"])
        changed["description"] = "changed"
        deployed.items[child_key]["document"]["S"] = json.dumps(changed)
        assert_names({"websearch.search"})
        deployed.items[child_key] = child
        assert_names(ready)
        original_read = deployed.store.get_item

        def failed_child_read(**kwargs):
            if kwargs["Key"]["sk"]["S"].endswith("#SCHEMA#" + names[0]):
                raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "fixture unavailable"}}, "GetItem")
            return original_read(**kwargs)

        monkeypatch.setattr(deployed.store, "get_item", failed_child_read)
        assert_names({"websearch.search"})
        monkeypatch.setattr(deployed.store, "get_item", original_read)
        assert_names(ready)
        original_write = deployed.store.transact_write_items
        before = len(deployed.writes)

        def failed_write(**kwargs):
            raise ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "fixture refused"}}, "TransactWriteItems")

        monkeypatch.setattr(deployed.store, "transact_write_items", failed_write)
        with pytest.raises(ClientError):
            dynamic.publish_catalogue(binding=binding, tools=schemas, client=deployed.store, table_name="fixture-authority", now=1000)
        assert len(deployed.writes) == before
        assert_names(ready)
        monkeypatch.setattr(deployed.store, "transact_write_items", original_write)
    monkeypatch.setattr(dynamic.time, "time", lambda: 1000 + dynamic.TTL_SECONDS)
    assert_names({"websearch.search"})
    dynamic.publish_catalogue(binding=binding, tools=schemas, client=deployed.store, table_name="fixture-authority", now=1000 + dynamic.TTL_SECONDS)
    assert_names(ready)
    deployed.target["updatedAt"] = "2026-01-02T00:00:00Z"
    assert_names({"websearch.search"})
    binding = dynamic.target_binding(GATEWAY_ID, deployed.target, deployed.gateway)
    dynamic.publish_catalogue(binding=binding, tools=schemas, client=deployed.store, table_name="fixture-authority", now=1000 + dynamic.TTL_SECONDS)
    assert_names(ready)
    assert mixed_catalogue.calls.count("fixture-core") == 1
    assert mixed_catalogue.calls.count("list_gateways") == 1
