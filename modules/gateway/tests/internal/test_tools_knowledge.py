"""Offline private mediator wire tests use local RSA/Ed25519 test keys."""

import asyncio
import base64
import hashlib
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from botocore.exceptions import ClientError
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.agentauth import chat_crew_run
from src.agentauth import routes as agent_routes
from src.auth import caller_provenance
from src.crew import host_tools
from src.crew import proxy as crew_proxy
from src.crew.targets import CrewLeaseFencedError, CrewOwner
from src.internal import tools_knowledge as mediator
from tests.agentauth.test_knowledge_service import PUBLIC_KEYS, SIGNING_ENV
from tests.internal.test_tools_dynamic_catalogue import ROLE, TOOLS, deployed  # noqa: F401

FACADE = "arn:aws:iam::123456789012:role/fixture-facade"
RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PSS = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size)
OWNER = "00000000-0000-4000-8000-000000000001"
PRINCIPAL = {"user_id": "fixture-human", "tenant_id": "fixture-tenant", "team_id": "fixture-team"}
HEADERS = {
    "x-caller-identity": ROLE.replace(":iam:", ":sts:").replace(":role/", ":assumed-role/") + "/agentcore",
    "x-adp-edge-provenance": "fixture-edge-proof",
    "x-adp-knowledge-edge-provenance": "fixture-private-proof",
}


def assertion(operation, arguments, **overrides):
    claims = {
        "caller_role_arn": FACADE,
        "principal": PRINCIPAL,
        "root_human_id": PRINCIPAL["user_id"],
        "run_id": None,
        "workload_id": None,
        "run_expires_at": None,
        "receipt_id": "fixture-receipt",
        "issued_at": 999,
        "expires_at": 1029,
        "method": "tools/call",
        "tool": "knowledge___" + operation,
        "rpc_id": "request-one",
        "arguments_sha256": hashlib.sha256(mediator.canonical(arguments)).hexdigest(),
        **overrides,
    }
    signature = RSA.sign(mediator.canonical(claims), PSS, hashes.SHA256())

    def b64(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    return b64(mediator.canonical({"claims": claims, "signature": b64(signature)}))


def packet(operation="search", arguments=None, **claims):
    args = {"query": "own-sentinel"} if arguments is None else arguments
    return {
        "jsonrpc": "2.0",
        "id": "request-one",
        "method": "tools/call",
        "params": {
            "name": operation,
            "arguments": {**args, "adp_assertion": assertion(operation, args, **claims)},
        },
    }


@pytest.fixture
async def bridge(deployed, monkeypatch):  # noqa: F811
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "agent-context"))
    from door import auth

    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", FACADE)
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", "fixture-rsa-key")
    monkeypatch.setenv("ADP_TOOLS_KNOWLEDGE_EDGE_PROVENANCE", "fixture-private-proof")
    monkeypatch.setattr(
        caller_provenance, "get_settings", lambda: SimpleNamespace(trust_apigw_headers=True, apigw_provenance_secret="fixture-edge-proof")
    )
    checks, forwards, identity_reads = [], [], []
    original_client = mediator.boto3.client

    def verify(**kwargs):
        checks.append(kwargs)
        assert kwargs["KeyId"] == "fixture-rsa-key"
        assert kwargs["SigningAlgorithm"] == "RSASSA_PSS_SHA_256" and kwargs["MessageType"] == "RAW"
        try:
            RSA.public_key().verify(kwargs["Signature"], kwargs["Message"], PSS, hashes.SHA256())
            return {"SignatureValid": True}
        except InvalidSignature:
            return {"SignatureValid": False}

    monkeypatch.setattr(
        mediator.boto3, "client", lambda name, **kwargs: SimpleNamespace(verify=verify) if name == "kms" else original_client(name, **kwargs)
    )
    runtime = SimpleNamespace(
        env={"ADP_DOOR_SERVICE_URL": "http://context-mcp.agent-context.svc.cluster.local:5100", **SIGNING_ENV},
        store=SimpleNamespace(table="fixture-authority", client=deployed.store),
    )
    monkeypatch.setattr(agent_routes, "get_agent_runtime", lambda: runtime)
    policy = {
        "status": "active",
        "allowed_tools": ["knowledge.search"],
        "consented_tools": ["knowledge.search"],
        "tool_max_cost_usd": {"knowledge.search": "0.01"},
    }
    monkeypatch.setattr(mediator, "ToolsPolicyStore", lambda **kwargs: SimpleNamespace(get=lambda **kwargs: policy))
    monkeypatch.setattr(mediator, "effective_entitlement", AsyncMock(return_value=(True, None)))

    @asynccontextmanager
    async def identity(principal):
        identity_reads.append(principal)
        yield (
            {"x-owner-sub": OWNER, "x-asset-owner-id": PRINCIPAL["user_id"], "x-github-login": "fixture-human"},
            SimpleNamespace(expire_all=lambda: None),
        )

    monkeypatch.setattr(mediator, "locked_human_identity", identity)

    async def forward(method, url, headers, body):
        forwards.append((method, url, headers, body))
        message = json.loads(body)
        if "x-adp-door-catalogue" in headers:
            auth.verify_catalogue(headers["x-adp-door-catalogue"], PUBLIC_KEYS, method=method, path="/mcp/", body=body)
            result = {"tools": TOOLS}
        else:
            claims = auth.verify_identity(headers["x-adp-door-identity"], PUBLIC_KEYS, method=method, path="/mcp/", body=body)
            assert claims["owner_sub"] == OWNER and claims["tenant_id"] == PRINCIPAL["tenant_id"]
            assert claims["asset_owner_id"] == PRINCIPAL["user_id"]
            assert claims["github_login"] == "fixture-human"
            assert "adp_assertion" not in message["params"]["arguments"]
            result = {"content": [{"type": "text", "text": "own-sentinel citation:fixture/repo:7"}]}
        return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result})

    monkeypatch.setattr(mediator, "forward_door", forward)
    app = FastAPI()
    app.include_router(mediator.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
        yield SimpleNamespace(
            client=client, deployed=deployed, runtime=runtime, policy=policy, forwards=forwards, checks=checks, identities=identity_reads
        )


async def test_real_edge_proof_and_signature_forward_only_current_owned_identity(bridge):
    result = await bridge.client.post(
        mediator.PATH, json=packet(), headers={**HEADERS, "x-owner-sub": "foreign", "x-github-login": "foreign", "x-tenant-id": "foreign"}
    )
    assert result.status_code == 200
    assert result.json()["result"]["content"] == [{"type": "text", "text": "own-sentinel citation:fixture/repo:7"}]
    assert "foreign-sentinel" not in result.text
    assert bridge.identities == [PRINCIPAL] and len(bridge.checks) == 1 and len(bridge.forwards) == 1
    assert set(bridge.forwards[0][2]) == {"x-adp-door-identity", "content-type", "accept"}


@pytest.mark.parametrize("refusal", [None, "before", "after"])
async def test_product_authority_checked_before_forward_and_before_release(bridge, monkeypatch, refusal):
    from src.agentauth import product_knowledge

    product_id = "00000000-0000-0000-0000-000000000001"
    calls = []

    async def authorize(db, **kwargs):
        calls.append(kwargs)
        if (refusal == "before" and len(calls) == 1) or (refusal == "after" and len(calls) == 2):
            raise HTTPException(404, "Product not found")

    monkeypatch.setattr(product_knowledge, "authorize_product", authorize)
    response = await bridge.client.post(
        mediator.PATH, headers={**HEADERS, "x-product-id": "forged"}, json=packet(arguments={"query": "info", "product_id": product_id})
    )
    assert response.status_code == (200 if refusal is None else 404)
    assert len(calls) == (1 if refusal == "before" else 2)
    assert all(call == {"user_id": PRINCIPAL["user_id"], "tenant_id": PRINCIPAL["tenant_id"], "product_id": product_id} for call in calls)
    assert len(bridge.forwards) == (0 if refusal == "before" else 1)
    if refusal:
        assert "own-sentinel" not in response.text
    else:
        from tests.agentauth.test_knowledge_service import verify_identity

        method, _, headers, body = bridge.forwards[0]
        claims = verify_identity(headers["x-adp-door-identity"], PUBLIC_KEYS, method=method, path="/mcp/", body=body)
        assert claims["product_id"] == product_id


async def test_personal_search_uses_existing_signed_human_identity(bridge):
    response = await bridge.client.post(mediator.PATH, headers=HEADERS, json=packet(arguments={"query": "notes", "scope": "personal"}))
    assert response.status_code == 200
    assert json.loads(bridge.forwards[0][3])["params"]["arguments"]["scope"] == "personal"


@pytest.fixture
def host_run(monkeypatch):
    run_id, instance, expires_at = "fixture-host-run", "fixture-instance", 1200
    row = {
        "run_id": run_id,
        "status": "active",
        "kind": "crew",
        "executor_scope": "session",
        "target_kind": "ec2-instance",
        "host_role_arn": "fixture-guest-role",
        "tenant_id": PRINCIPAL["tenant_id"],
        "user_id": PRINCIPAL["user_id"],
        "root_human_id": PRINCIPAL["user_id"],
        "lease_expires_at": expires_at,
        "lease_generation": 3,
        "session_id": "fixture-box",
        "target_ref": instance,
    }
    lease = SimpleNamespace(
        target_kind="ec2-instance",
        tenant_id=PRINCIPAL["tenant_id"],
        user_id=PRINCIPAL["user_id"],
        expires_at=expires_at,
        generation=3,
        target_ref="fixture-box",
    )
    target = SimpleNamespace(lease=lease, host_run_id=run_id, host_instance_ref=instance)
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=target))
    monkeypatch.setattr(chat_crew_run, "current_crew_store", lambda: SimpleNamespace(read_run=lambda requested: row if requested == run_id else None))
    monkeypatch.setattr(crew_proxy, "get_target_resolver", lambda: resolver)
    return SimpleNamespace(
        row=row,
        target=target,
        resolver=resolver,
        claims={"run_id": run_id, "workload_id": instance, "run_expires_at": expires_at, "host_generation": 3},
    )


async def test_signed_active_host_run_matches_owner_instance_and_current_generation(bridge, host_run):
    response = await bridge.client.post(mediator.PATH, json=packet(**host_run.claims), headers=HEADERS)
    assert response.status_code == 200
    assert len(bridge.forwards) == 1 and bridge.identities == [PRINCIPAL]
    host_run.resolver.resolve.assert_awaited_once()


async def relay_host_claims(host_run, monkeypatch):
    monkeypatch.setattr(host_tools.model_token, "_secret", lambda: b"fixture-host-secret")
    owner = CrewOwner(PRINCIPAL["tenant_id"], PRINCIPAL["user_id"])
    token = host_tools.mint_host_tools_assertion(owner, host_run.target, host_run.claims["run_id"], now=1000)
    request = Request(
        {
            "type": "http",
            "headers": [
                (host_tools.ASSERTION_HEADER.encode(), token.encode()),
                (b"x-agent-runid", host_run.claims["run_id"].encode()),
            ],
        }
    )
    identity = await host_tools.authenticate_crew_host(request)
    assert identity.tenant_id == PRINCIPAL["tenant_id"] and identity.user_id == PRINCIPAL["user_id"]
    signed = {
        "run_id": identity.run_id,
        "workload_id": identity.instance_id,
        "run_expires_at": identity.expires_at,
        "host_generation": identity.generation,
    }
    return signed


async def test_relay_minted_host_identity_reaches_signed_mediator_with_fake_kms(bridge, host_run, monkeypatch):
    signed = await relay_host_claims(host_run, monkeypatch)
    response = await bridge.client.post(mediator.PATH, json=packet(**signed), headers=HEADERS)
    assert response.status_code == 200 and len(bridge.forwards) == 1
    assert response.json()["result"]["content"][0]["text"] == "own-sentinel citation:fixture/repo:7"
    assert len(bridge.checks) == 1 and host_run.resolver.resolve.await_count == 2


@pytest.mark.parametrize("change", ["foreign_run", "missing_run"])
async def test_relay_minted_host_identity_cannot_authorize_foreign_or_missing_run(bridge, host_run, monkeypatch, change):
    signed = await relay_host_claims(host_run, monkeypatch)
    signed["run_id"] = "foreign-run" if change == "foreign_run" else None
    response = await bridge.client.post(mediator.PATH, json=packet(**signed), headers=HEADERS)
    assert response.status_code == 403 and response.json() == {"detail": "knowledge assertion refused"}
    assert not bridge.identities and not bridge.forwards and not bridge.deployed.writes
    assert len(bridge.checks) == (1 if change == "foreign_run" else 0)


@pytest.mark.parametrize(
    "change",
    [
        "inactive",
        "wrong_owner",
        "wrong_tenant",
        "wrong_role",
        "wrong_kind",
        "wrong_generation",
        "wrong_box",
        "wrong_instance",
        "wrong_run",
        "expired_lease",
        "fenced",
        "wrong_expiry",
        "foreign_run",
        "wrong_signed_generation",
        "missing_signed_generation",
    ],
)
async def test_signed_host_run_without_current_owned_unfenced_lease_is_refused(bridge, host_run, change):
    claims = dict(host_run.claims)
    if change == "inactive":
        host_run.row["status"] = "provisioning"
    elif change == "wrong_owner":
        host_run.row["root_human_id"] = "foreign-human"
    elif change == "wrong_tenant":
        host_run.row["tenant_id"] = "foreign-tenant"
    elif change == "wrong_role":
        host_run.row["host_role_arn"] = None
    elif change == "wrong_kind":
        host_run.row["target_kind"] = "eks-pod"
    elif change == "wrong_generation":
        host_run.target.lease.generation = 4
    elif change == "wrong_box":
        host_run.target.lease.target_ref = "foreign-box"
    elif change == "wrong_instance":
        host_run.target.host_instance_ref = "foreign-instance"
    elif change == "wrong_run":
        host_run.target.host_run_id = "foreign-run"
    elif change == "expired_lease":
        host_run.target.lease.expires_at = 1000
    elif change == "fenced":
        host_run.resolver.resolve.side_effect = CrewLeaseFencedError("fenced")
    elif change == "wrong_signed_generation":
        claims["host_generation"] = 4
    elif change == "missing_signed_generation":
        del claims["host_generation"]
    elif change == "wrong_expiry":
        claims["run_expires_at"] = 1201
    else:
        claims["run_id"] = "foreign-run"
    response = await bridge.client.post(mediator.PATH, json=packet(**claims), headers=HEADERS)
    assert response.status_code == 403 and response.json() == {"detail": "knowledge assertion refused"}
    assert not bridge.forwards and not bridge.identities and not bridge.deployed.writes


@pytest.mark.parametrize(
    "fault,stage,failed_check",
    [
        ("missing", "assertion_presence", None),
        ("malformed", "envelope", None),
        ("rpc_id", "request_binding", "upstream_rpc_id_valid"),
        ("rpc_type", "request_binding", "upstream_rpc_id_valid"),
        ("arguments", "request_binding", "arguments_match"),
        ("signature", "signature", None),
        ("expired", "freshness", None),
        ("run", "run_binding", None),
    ],
)
async def test_assertion_denial_diagnostics_expose_only_fixed_checks(bridge, caplog, fault, stage, failed_check):
    body = packet(arguments={"query": "private-query-sentinel"})
    arguments = body["params"]["arguments"]
    original_token = arguments["adp_assertion"]
    if fault == "missing":
        del arguments["adp_assertion"]
    elif fault == "malformed":
        arguments["adp_assertion"] = "private-invalid-token!"
    elif fault == "rpc_id":
        arguments["adp_assertion"] = assertion("search", {"query": "private-query-sentinel"}, rpc_id=None)
    elif fault == "rpc_type":
        arguments["adp_assertion"] = assertion("search", {"query": "private-query-sentinel"}, rpc_id=True)
    elif fault == "arguments":
        arguments["query"] = "private-changed-query"
    elif fault == "signature":
        envelope = json.loads(base64.urlsafe_b64decode(original_token + "=" * (-len(original_token) % 4)))
        envelope["claims"]["receipt_id"] = "private-forged-receipt"
        arguments["adp_assertion"] = base64.urlsafe_b64encode(mediator.canonical(envelope)).decode().rstrip("=")
    elif fault == "expired":
        arguments["adp_assertion"] = assertion("search", {"query": "private-query-sentinel"}, issued_at=960, expires_at=990)
    else:
        arguments["adp_assertion"] = assertion("search", {"query": "private-query-sentinel"}, run_id="private-run")

    response = await bridge.client.post(mediator.PATH, json=body, headers=HEADERS)
    assert response.status_code == 403 and response.json() == {"detail": "knowledge assertion refused"}
    assert not bridge.forwards and not bridge.identities and not bridge.deployed.writes
    records = [r for r in caplog.records if getattr(r, "event", None) == "private_tool_assertion_refused"]
    assert len(records) == 1 and records[0].stage == stage
    checks = getattr(records[0], "binding_checks", {})
    assert set(checks) <= {
        "upstream_rpc_id_valid",
        "arguments_match",
        "tool_matches",
        "caller_role_matches",
        "principal_bound",
        "verification_configured",
    }
    assert all(type(value) is bool for value in checks.values())
    if failed_check:
        assert checks[failed_check] is False
    serialized = json.dumps(records[0].__dict__, default=str)
    assert "private-" not in serialized and original_token not in serialized
    assert all(value not in serialized for value in PRINCIPAL.values())


@pytest.mark.parametrize(
    "metadata", [None, {}, {"progressToken": "progress-one", "principal": {"user_id": "foreign"}, "arguments": {"query": "foreign"}}]
)
async def test_mcp_metadata_preserves_signed_arguments_and_owned_identity(bridge, metadata):
    body = packet()
    body["params"]["_meta"] = metadata
    response = await bridge.client.post(mediator.PATH, json=body, headers=HEADERS)
    assert response.status_code == 200
    assert bridge.identities == [PRINCIPAL] and len(bridge.checks) == 1
    forwarded = json.loads(bridge.forwards[0][3])
    assert forwarded["params"] == {"name": "search", "arguments": {"query": "own-sentinel"}}
    assert "foreign" not in response.text


@pytest.mark.parametrize("fault", ["changed_arguments", "assertion_only_in_metadata", "unsigned_parameter"])
async def test_metadata_cannot_replace_signed_authority_or_admit_extra_parameters(bridge, fault):
    body = packet()
    body["params"]["_meta"] = {"progressToken": "progress-one", "adp_assertion": body["params"]["arguments"]["adp_assertion"]}
    if fault == "changed_arguments":
        body["params"]["arguments"]["query"] = "foreign"
    elif fault == "assertion_only_in_metadata":
        del body["params"]["arguments"]["adp_assertion"]
    else:
        body["params"]["principal"] = PRINCIPAL
    response = await bridge.client.post(mediator.PATH, json=body, headers=HEADERS)
    assert response.status_code == (400 if fault == "unsigned_parameter" else 403)
    assert not bridge.forwards and not bridge.identities and not bridge.deployed.writes


async def test_valid_assertion_claims_once_and_denies_replay_before_door(bridge):
    body = packet()
    first = await bridge.client.post(mediator.PATH, json=body, headers=HEADERS)
    assert first.status_code == 200 and "own-sentinel citation:fixture/repo:7" in first.text
    record = bridge.deployed.writes[0]
    assert record["ConditionExpression"] == "attribute_not_exists(pk)"
    assert record["Item"]["pk"]["S"].startswith("TOOLS_KNOWLEDGE_REPLAY#")
    assert "own-sentinel" not in json.dumps(record)
    assert record["Item"]["knowledge_replay_ttl"] == {"N": "1089"}
    second = await bridge.client.post(mediator.PATH, json=body, headers=HEADERS)
    assert second.status_code == 403 and "already used" in second.text
    assert len(bridge.forwards) == len(bridge.deployed.writes) == 1


async def test_concurrent_replay_claims_once_even_when_relay_ids_and_metadata_differ(bridge):
    body = packet()
    requests = [{**body, "id": f"relay-{i}", "params": {**body["params"], "_meta": {"progressToken": i}}} for i in range(4)]
    responses = await asyncio.gather(*(bridge.client.post(mediator.PATH, json=request, headers=HEADERS) for request in requests))
    assert sorted(response.status_code for response in responses) == [200, 403, 403, 403]
    assert len(bridge.checks) == 4
    assert len(bridge.forwards) == len(bridge.deployed.writes) == 1
    assert "own-sentinel citation:fixture/repo:7" in next(response.text for response in responses if response.status_code == 200)
    assert all("own-sentinel" not in response.text for response in responses if response.status_code == 403)


@pytest.mark.parametrize("change", ("forged_tenant", "expired", "not_yet_valid"))
async def test_forged_or_stale_assertion_never_claims_or_retrieves(bridge, change):
    body = packet()
    arguments = body["params"]["arguments"]
    if change == "forged_tenant":
        token = arguments["adp_assertion"]
        envelope = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        envelope["claims"]["principal"]["tenant_id"] = "foreign-tenant"
        arguments["adp_assertion"] = base64.urlsafe_b64encode(mediator.canonical(envelope)).decode().rstrip("=")
    elif change == "expired":
        arguments["adp_assertion"] = assertion("search", {"query": "own-sentinel"}, issued_at=960, expires_at=990)
    else:
        arguments["adp_assertion"] = assertion("search", {"query": "own-sentinel"}, issued_at=1001, expires_at=1031)
    response = await bridge.client.post(mediator.PATH, json=body, headers=HEADERS)
    assert response.status_code == 403 and "own-sentinel" not in response.text
    assert len(bridge.checks) == (1 if change == "forged_tenant" else 0)
    assert not bridge.forwards and not bridge.identities and not bridge.deployed.writes


async def test_replay_store_failure_fails_closed_before_door(bridge, monkeypatch):
    def unavailable(**kwargs):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "fixture"}}, "PutItem")

    monkeypatch.setattr(bridge.runtime.store.client, "put_item", unavailable)
    result = await bridge.client.post(mediator.PATH, json=packet(), headers=HEADERS)
    assert result.status_code == 503 and "replay store unavailable" in result.text
    assert not bridge.forwards and not bridge.deployed.writes


@pytest.mark.parametrize(
    "namespace,operation,arguments",
    [
        ("github", "get_user", {}),
        ("jira", "get_issue", {"cloudId": "site", "issueIdOrKey": "EXAMPLE-1"}),
        ("slack", "get_user", {"user": "U00000000"}),
    ],
)
@pytest.mark.asyncio
async def test_vendor_private_assertion_has_its_own_signed_namespace(bridge, monkeypatch, namespace, operation, arguments):
    monkeypatch.setattr(mediator.time, "time", lambda: 1000)
    token = assertion(
        operation,
        arguments,
        tool=namespace + "___" + operation,
        rpc_id="request-one",
        connection_id="00000000-0000-4000-8000-000000000001",
    )
    claims = await mediator.verify_facade(token, operation, arguments, "request-one", namespace=namespace)
    assert claims["tool"] == namespace + "___" + operation
    with pytest.raises(HTTPException):
        await mediator.verify_facade(token, operation, arguments, "request-one", namespace="knowledge")
    relayed = await mediator.verify_facade(token, operation, arguments, "other-request", namespace=namespace)
    assert relayed == claims and relayed["rpc_id"] == "request-one"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-caller-identity": HEADERS["x-caller-identity"]},
        {**HEADERS, "x-adp-edge-provenance": "forged"},
        {**HEADERS, "x-adp-knowledge-edge-provenance": "forged"},
        {**HEADERS, "x-caller-identity": "arn:aws:sts::123456789012:assumed-role/other/pod"},
    ],
)
async def test_direct_or_wrong_role_cannot_mint_any_door_token(bridge, headers):
    response = await bridge.client.post(mediator.PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers=headers)
    assert response.status_code == 403
    assert not bridge.forwards and not bridge.checks and not bridge.deployed.writes


@pytest.mark.parametrize("change", ["tool", "arguments", "principal", "expired", "run", "signature"])
async def test_unbound_facade_assertion_is_denied_before_identity_or_door(bridge, change):
    body = packet()
    args = body["params"]["arguments"]
    if change == "tool":
        body["params"]["name"] = "browse"
    elif change == "arguments":
        args["query"] = "foreign-sentinel"
    elif change == "principal":
        args["adp_assertion"] = assertion("search", {"query": "own-sentinel"}, root_human_id="other")
    elif change == "expired":
        args["adp_assertion"] = assertion("search", {"query": "own-sentinel"}, issued_at=960, expires_at=990)
    elif change == "run":
        args["adp_assertion"] = assertion("search", {"query": "own-sentinel"}, run_id="unverifiable-run")
    else:
        token = args["adp_assertion"]
        envelope = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        envelope["claims"]["principal"] = {**PRINCIPAL, "tenant_id": "foreign"}
        args["adp_assertion"] = base64.urlsafe_b64encode(mediator.canonical(envelope)).decode().rstrip("=")
    response = await bridge.client.post(mediator.PATH, json=body, headers=HEADERS)
    assert response.status_code == 403
    assert not bridge.forwards and not bridge.identities


@pytest.mark.parametrize("change", ["no_run", "no_workload", "no_expiry", "no_generation", "foreign_run", "native_with_host_generation"])
async def test_incomplete_or_foreign_signed_host_binding_is_refused_before_door(bridge, host_run, change):
    claims = dict(host_run.claims)
    if change == "no_run":
        claims["run_id"] = None
    elif change == "no_workload":
        claims["workload_id"] = None
    elif change == "no_expiry":
        claims["run_expires_at"] = None
    elif change == "no_generation":
        claims["host_generation"] = None
    elif change == "foreign_run":
        claims["run_id"] = "foreign-run"
    else:
        claims["native_test"] = {"operation_id": "fixture", "execution_id": "fixture", "request_id": "fixture", "request_sha256": "fixture"}
    response = await bridge.client.post(mediator.PATH, json=packet(**claims), headers=HEADERS)
    assert response.status_code == 403 and response.json() == {"detail": "knowledge assertion refused"}
    assert not bridge.identities and not bridge.forwards and not bridge.deployed.writes
    assert len(bridge.checks) == (1 if change == "foreign_run" else 0)


@pytest.mark.parametrize("signed_id", [None, True, 1.0, "", "x" * 257, [], {}])
async def test_signed_ingress_request_id_must_be_present_and_valid_before_retrieval(bridge, signed_id):
    body = packet(rpc_id=signed_id)
    body["id"] = "relay-request"
    response = await bridge.client.post(mediator.PATH, json=body, headers=HEADERS)
    assert response.status_code == 403
    assert not bridge.forwards and not bridge.identities and not bridge.checks


@pytest.mark.parametrize(
    "request_id",
    [True, 1.0, None, [], {}, "", "x" * 257],
)
async def test_relay_request_id_must_still_be_a_valid_protocol_id(bridge, request_id):
    body = packet()
    body["id"] = request_id
    response = await bridge.client.post(mediator.PATH, json=body, headers=HEADERS)
    assert response.status_code == (403 if request_id == "" else 400)
    assert not bridge.forwards and not bridge.identities and not bridge.checks and not bridge.deployed.writes


@pytest.mark.parametrize("signed_id,request_id", [("ingress", "relay"), (1, 2), ("1", 1), (1, "1"), (0, 0)])
async def test_relay_id_can_differ_while_signed_ingress_authority_is_preserved(bridge, signed_id, request_id):
    body = packet(rpc_id=signed_id)
    body["id"] = request_id
    response = await bridge.client.post(mediator.PATH, json=body, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["id"] == request_id
    assert json.loads(bridge.forwards[0][3])["id"] == request_id
    assert json.loads(bridge.checks[0]["Message"])["rpc_id"] == signed_id
    assert response.json()["result"]["content"] == [{"type": "text", "text": "own-sentinel citation:fixture/repo:7"}]
    assert len(bridge.forwards) == len(bridge.identities) == len(bridge.deployed.writes) == 1


async def test_changing_signed_ingress_id_still_fails_signature_before_retrieval(bridge):
    body = packet()
    body["id"] = "relay-request"
    token = body["params"]["arguments"]["adp_assertion"]
    envelope = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    envelope["claims"]["rpc_id"] = "tampered-ingress"
    body["params"]["arguments"]["adp_assertion"] = base64.urlsafe_b64encode(mediator.canonical(envelope)).decode().rstrip("=")
    response = await bridge.client.post(mediator.PATH, json=body, headers=HEADERS)
    assert response.status_code == 403
    assert len(bridge.checks) == 1 and not bridge.forwards and not bridge.identities and not bridge.deployed.writes


async def test_interceptor_binds_ingress_then_mediator_accepts_only_one_relay(bridge, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "tools" / "interceptor"))
    from tools_interceptor.handler import handler

    body = packet()
    token = body["params"]["arguments"]["adp_assertion"]
    ingress = {
        **body,
        "params": {"name": "knowledge___search", "arguments": {"query": "own-sentinel"}},
    }
    event = {
        "mcp": {
            "gatewayRequest": {
                "body": ingress,
                "headers": {"x-adp-assertion": token, "x-adp-tenant": PRINCIPAL["tenant_id"]},
            }
        }
    }
    transformed = handler(event, None)["mcp"]["transformedGatewayRequest"]["body"]
    assert transformed["id"] == "request-one"
    assert transformed["params"]["arguments"]["adp_assertion"] == token
    relayed = json.loads(json.dumps(transformed))
    relayed["id"] = "target-request-one"
    relayed["params"]["name"] = "search"
    response = await bridge.client.post(mediator.PATH, json=relayed, headers=HEADERS)
    assert response.status_code == 200 and response.json()["id"] == "target-request-one"

    ingress["id"] = "forged-ingress"
    denied = handler(event, None)["mcp"]["transformedGatewayResponse"]
    assert denied["statusCode"] == 403 and denied["body"]["error"]["message"] == "tool_mismatch"

    relayed["id"] = "target-request-two"
    replay = await bridge.client.post(mediator.PATH, json=relayed, headers=HEADERS)
    assert replay.status_code == 403 and "already used" in replay.text
    assert len(bridge.forwards) == len(bridge.deployed.writes) == 1


@pytest.mark.parametrize("failure", ("http_error", "rpc_error", "tool_error", "backend_error", "empty_result", "missing_result"))
async def test_private_backend_failure_cannot_be_reported_as_success(bridge, monkeypatch, failure):
    async def unavailable(method, url, headers, body):
        request_id = json.loads(body)["id"]
        if failure == "http_error":
            return JSONResponse(
                {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": "false-success"}]}}, status_code=503
            )
        if failure == "rpc_error":
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32001, "message": "private backend unavailable"}})
        if failure == "empty_result":
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"content": []}})
        if failure == "missing_result":
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})
        code = "knowledge_backend_unavailable" if failure == "backend_error" else "knowledge_index_not_ready"
        detail = {"error": "Search unavailable" if failure == "backend_error" else "Index not ready", "code": code}
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": json.dumps(detail)}]},
            }
        )

    monkeypatch.setattr(mediator, "forward_door", unavailable)
    response = await bridge.client.post(mediator.PATH, json=packet(), headers=HEADERS)
    if failure in {"tool_error", "backend_error"}:
        assert response.status_code == 200
        assert response.json()["result"]["isError"] is True
        assert ("knowledge_backend_unavailable" if failure == "backend_error" else "knowledge_index_not_ready") in response.text
    else:
        assert response.status_code == 503
        assert "false-success" not in response.text and "private backend" not in response.text
    assert len(bridge.identities) == 1


@pytest.mark.parametrize(
    "operation,args",
    [("remember", {"session_id": "fixture"}), ("experience", {"action": "save"}), ("search", {"scope": "docs"}), ("search", {"scope": "memory"})],
)
async def test_missing_paid_or_write_admission_never_reaches_backend(bridge, operation, args):
    response = await bridge.client.post(mediator.PATH, json=packet(operation, args), headers=HEADERS)
    assert response.status_code == 200 and response.json()["result"]["isError"] is True
    assert "knowledge_admission_unavailable" in response.text
    assert not bridge.forwards and not bridge.identities


@pytest.mark.parametrize(
    "operation,arguments,admitted",
    [
        ("search", {"query": "own-sentinel", "scope": "code"}, True),
        ("understand", {"target": "fixture/repo::Sentinel"}, True),
        ("impact", {"target": "fixture/repo::Sentinel"}, True),
        *[("browse", {"action": action, "uri": "/"}, True) for action in ("ls", "tree", "read", "info")],
        *[("secure", {"action": action, "repo": "fixture/repo", "cve": "CVE-2026-1234"}, True) for action in ("identify", "plan", "verify")],
        ("remember", {"session_id": "owned-session", "messages": []}, False),
        *[
            ("experience", {"action": action, "persona": "developer", "query": "own-sentinel"}, False)
            for action in ("save", "recall", "list_syntheses")
        ],
    ],
)
async def test_each_released_verb_action_is_mediated_or_explicitly_not_ready(bridge, operation, arguments, admitted):
    permission = "knowledge." + operation
    bridge.policy["allowed_tools"].append(permission)
    bridge.policy["consented_tools"].append(permission)
    bridge.policy["tool_max_cost_usd"][permission] = "0.01"
    response = await bridge.client.post(mediator.PATH, json=packet(operation, arguments), headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["jsonrpc"] == "2.0"
    if admitted:
        assert response.json()["result"]["content"] == [{"type": "text", "text": "own-sentinel citation:fixture/repo:7"}]
        assert len(bridge.forwards) == 1 and len(bridge.identities) == 1
        forwarded = json.loads(bridge.forwards[0][3])
        assert forwarded["params"] == {"name": operation, "arguments": arguments}
        assert set(bridge.forwards[0][2]) == {"x-adp-door-identity", "content-type", "accept"}
    else:
        assert response.json()["result"]["isError"] is True
        assert "knowledge_admission_unavailable" in response.text
        assert not bridge.forwards and not bridge.identities


@pytest.mark.parametrize("operation", ("remember", "experience"))
async def test_unadmitted_actions_never_spend_or_write_without_durable_replay(bridge, operation):
    arguments = (
        {"session_id": "owned-session", "messages": []}
        if operation == "remember"
        else {"action": "save", "persona": "developer", "content": "private fixture"}
    )
    response = await bridge.client.post(mediator.PATH, json=packet(operation, arguments), headers=HEADERS)
    assert response.status_code == 200 and response.json()["result"]["isError"] is True
    assert not bridge.forwards and not bridge.identities and not bridge.deployed.writes


@pytest.mark.parametrize("params", [{}, {"_meta": {"progressToken": "catalogue-one"}}])
async def test_metadata_publishes_actual_schema_without_human_or_policy_grant(bridge, params):
    response = await bridge.client.post(mediator.PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["result"]["tools"][0]["name"] == "search"
    assert "adp_assertion" in response.json()["result"]["tools"][0]["inputSchema"]["properties"]
    assert not bridge.identities and not bridge.checks and len(bridge.deployed.writes) == 1
    document = json.loads(bridge.deployed.writes[0]["Item"]["document"]["S"])
    assert document["tools"] == TOOLS and "adp_assertion" not in json.dumps(document)
    assert json.loads(bridge.forwards[0][3])["params"] == {}


async def test_seven_verb_native_listing_is_published_without_hidden_transport_argument(bridge, monkeypatch):
    from door import auth

    names = ("search", "understand", "impact", "browse", "remember", "experience", "secure")
    native = [
        {
            "name": name,
            "description": name,
            "inputSchema": {
                "type": "object",
                "properties": ({"action": {"type": "string", "enum": ["ls", "tree", "read", "info"]}} if name == "browse" else {}),
            },
        }
        for name in names
    ]
    assert {tool["name"] for tool in native} == {"search", "understand", "impact", "browse", "remember", "experience", "secure"}

    async def forward(method, url, headers, body):
        assert set(headers) == {"x-adp-door-catalogue", "content-type", "accept"}
        auth.verify_catalogue(headers["x-adp-door-catalogue"], PUBLIC_KEYS, method=method, path="/mcp/", body=body)
        packet = json.loads(body)
        return JSONResponse({"jsonrpc": "2.0", "id": packet["id"], "result": {"tools": native}})

    monkeypatch.setattr(mediator, "forward_door", forward)
    response = await bridge.client.post(mediator.PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers=HEADERS)
    assert response.status_code == 200
    listed = response.json()["result"]["tools"]
    assert {tool["name"] for tool in listed} == {tool["name"] for tool in native}
    assert all("adp_assertion" in tool["inputSchema"]["properties"] for tool in listed)
    assert listed[3]["inputSchema"]["properties"]["action"]["type"] == "string"
    record = json.loads(bridge.deployed.writes[0]["Item"]["document"]["S"])
    assert record["tools"] == native
    assert "adp_assertion" not in json.dumps(record)
    assert not bridge.identities and not bridge.checks


@pytest.mark.parametrize("status", ["CREATING", "SYNCHRONIZING"])
async def test_metadata_bootstrap_cannot_publish_callable_provenance(bridge, monkeypatch, status):
    bridge.deployed.target["status"] = status
    monkeypatch.delenv("ADP_TOOLS_KNOWLEDGE_TARGET_ID")
    response = await bridge.client.post(mediator.PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers=HEADERS)
    assert response.status_code == 200 and not bridge.deployed.writes
    denied = await bridge.client.post(mediator.PATH, json=packet(), headers=HEADERS)
    assert denied.status_code == 503 and not bridge.identities


@pytest.mark.parametrize("change", ["consent", "entitlement", "expiry", "target"])
async def test_revocation_during_query_discards_private_response(bridge, monkeypatch, change):
    forward = mediator.forward_door

    async def changed(*args):
        response = await forward(*args)
        if change == "consent":
            # #8387: a revoked consent is an explicit owner decision (default mode would re-grant it).
            bridge.policy["consented_tools"] = []
            bridge.policy["consent_mode"] = "explicit"
        elif change == "entitlement":
            mediator.effective_entitlement.return_value = (False, None)
        elif change == "expiry":
            monkeypatch.setattr(mediator.time, "time", lambda: 1030)
        else:
            bridge.deployed.target["updatedAt"] = "changed"
        return response

    monkeypatch.setattr(mediator, "forward_door", changed)
    response = await bridge.client.post(mediator.PATH, json=packet(), headers=HEADERS)
    assert response.status_code in {403, 503}
    assert "own-sentinel" not in response.text and len(bridge.forwards) == 1


@pytest.mark.parametrize(
    "change,workspace_link",
    [
        (change, linked)
        for linked in (False, True)
        for change in (None, "inactive", "revoked", "foreign_tenant", "foreign_team", "shadow", "worker", "bad_subject", "bad_login")
    ]
    + [("no_login_account", True)]
    + [
        (change, True) for change in ("missing_link", "unproven_link", "ambiguous_link", "foreign_link_tenant", "foreign_link_user", "wrong_provider")
    ],
)
async def test_human_identity_is_derived_from_real_canonical_rows(tmp_path, monkeypatch, change, workspace_link):
    from datetime import UTC, datetime

    from fastapi import HTTPException
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.organization import TeamMembership, User
    from src.shared.models.vault import UserIdentity

    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "identity.sqlite"))
    tables = [User.__table__, TenantMembership.__table__, TeamMembership.__table__, UserIdentity.__table__]
    async with engine.begin() as connection:
        for table in tables:
            await connection.run_sync(table.create)
        user = {
            "id": PRINCIPAL["user_id"],
            "org_id": PRINCIPAL["tenant_id"],
            "team_id": PRINCIPAL["team_id"],
            "email": "fixture@example.test",
            "user_kind": "worker" if change == "worker" else "human",
            "is_shadow": change == "shadow",
            "cognito_sub": None if workspace_link else ("bad" if change == "bad_subject" else OWNER),
        }
        membership = {
            "user_id": user["id"],
            "tenant_id": user["org_id"],
            "is_active": change != "inactive",
            "revoked_at": datetime.now(UTC) if change == "revoked" else None,
        }
        team = {"user_id": user["id"], "org_id": user["org_id"], "team_id": "other" if change == "foreign_team" else user["team_id"]}
        if change == "foreign_tenant":
            membership["tenant_id"] = "foreign"
        await connection.execute(User.__table__.insert().values(**user))
        await connection.execute(TenantMembership.__table__.insert().values(**membership))
        await connection.execute(TeamMembership.__table__.insert().values(**team))
        if workspace_link and change != "no_login_account":
            await connection.execute(
                User.__table__.insert().values(
                    id="fixture-login-account",
                    org_id="login-home",
                    team_id="login-team",
                    email="login@example.test",
                    cognito_sub=OWNER,
                    user_kind="human",
                    is_shadow=False,
                )
            )
        if workspace_link and change != "missing_link":
            await connection.execute(
                UserIdentity.__table__.insert().values(
                    user_id="foreign" if change == "foreign_link_user" else user["id"],
                    org_id="foreign" if change == "foreign_link_tenant" else user["org_id"],
                    team_id=user["team_id"],
                    provider="directory" if change == "wrong_provider" else "cognito",
                    provider_user_id="bad" if change == "bad_subject" else OWNER,
                    verification_method="self_asserted" if change == "unproven_link" else "org_placement",
                )
            )
            if change == "ambiguous_link":
                await connection.execute(
                    UserIdentity.__table__.insert().values(
                        user_id=user["id"],
                        org_id=user["org_id"],
                        team_id=user["team_id"],
                        provider="cognito",
                        provider_user_id="00000000-0000-4000-8000-000000000002",
                        verification_method="org_placement",
                    )
                )
        await connection.execute(
            UserIdentity.__table__.insert().values(
                user_id=user["id"],
                org_id=user["org_id"],
                team_id=user["team_id"],
                provider="github",
                provider_user_id="fixture-gh",
                provider_username="bad/name" if change == "bad_login" else "Fixture-Human",
                verification_method="fixture-offline",
                is_primary=True,
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(mediator, "get_session_factory", lambda: factory)
    try:
        if change in (None, "no_login_account"):
            async with mediator.locked_human_identity(PRINCIPAL) as (identity, db):
                assert identity == {
                    "x-owner-sub": OWNER,
                    "x-asset-owner-id": OWNER
                    if change == "no_login_account"
                    else ("fixture-login-account" if workspace_link else PRINCIPAL["user_id"]),
                    "x-tenant-id": PRINCIPAL["tenant_id"],
                    "x-github-login": "fixture-human",
                }
        else:
            with pytest.raises(HTTPException) as failure:
                async with mediator.locked_human_identity(PRINCIPAL):
                    pytest.fail("invalid canonical identity was admitted")
            assert failure.value.status_code == 403
    finally:
        await engine.dispose()


async def test_unscoped_docs_explains_supported_semantic_scopes(bridge):
    response = await bridge.client.post(mediator.PATH, json=packet("search", {"query": "setup", "scope": "docs"}), headers=HEADERS)
    assert response.json()["result"]["isError"] is True
    assert "scope='personal'" in response.text and "product_id" in response.text
    assert not bridge.forwards


def test_native_code_admission_cannot_be_used_for_product_semantic_search():
    from src.agentauth.native_test_facade_contract import knowledge_read_permitted

    assert knowledge_read_permitted("knowledge___search", {"query": "source", "scope": "code"})
    assert not knowledge_read_permitted("knowledge___search", {"query": "source", "product_id": "00000000-0000-0000-0000-000000000001"})
    assert not knowledge_read_permitted("knowledge___search", {"query": "source", "scope": "personal"})
