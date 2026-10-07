"""Actual client wire + maintained Gateway auth, with offline edge/registry fixtures.

This does not run AWS API Gateway. The fixture verifies SigV4 over the received
bytes before adding edge-owned headers. Gateway provenance/auth functions and
TokenContext are loaded from maintained source; unrelated Gateway integrations
are not installed in the API test environment. No copied authentication logic.
"""

import ast
from datetime import UTC, datetime, timedelta
import importlib.util
import json
import logging
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace

import botocore.auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials, RefreshableCredentials
from fastapi import HTTPException
import httpx
import pytest
from starlette.requests import Request
from superplane_contracts.connections import CredentialReference

from app.adapters.adp_vault_client import AdpVaultClient, EVIDENCE_PATH

REPO = Path(__file__).resolve().parents[6]
GATEWAY = REPO / "modules/gateway/src"
ENDPOINT = "https://abcdefghij.execute-api.us-east-1.amazonaws.com/dev"
CALLER = "arn:aws:sts::123456789012:assumed-role/api-producer/session"
ROLE = "arn:aws:iam::123456789012:role/api-producer"
PROOF = "offline-edge-owned-provenance-never-sent-by-client"


def _module(monkeypatch, name, **values):
    result = ModuleType(name)
    result.__dict__.update(values)
    monkeypatch.setitem(sys.modules, name, result)
    return result


def _load(monkeypatch, name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _functions(module, path, names):
    """Compile original named functions; avoid unrelated middleware dependencies."""
    parsed = ast.parse(path.read_text(), filename=str(path))
    selected = [
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {node.name for node in selected} == set(names)
    future = ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0
    )
    tree = ast.fix_missing_locations(
        ast.Module(body=[future, *selected], type_ignores=[])
    )
    exec(compile(tree, str(path), "exec"), module.__dict__)


@pytest.fixture
def gateway(monkeypatch):
    settings = SimpleNamespace(trust_apigw_headers=True, apigw_provenance_secret=PROOF)
    _module(monkeypatch, "src.shared.config", get_settings=lambda: settings)
    _module(
        monkeypatch,
        "src.shared.metrics",
        emit_caller_provenance_rejected=lambda **kw: None,
    )
    schema = _load(
        monkeypatch, "src.shared.schemas.auth", GATEWAY / "shared/schemas/auth.py"
    )
    provenance = _load(
        monkeypatch, "src.auth.caller_provenance", GATEWAY / "auth/caller_provenance.py"
    )
    entry = {
        "agent_id": "registered-api",
        "agent_name": "api-producer",
        "org_id": "tenant",
        "team_id": "",
        "scope": "internal",
    }
    registry_reads = []

    def lookup(role):
        registry_reads.append(role)
        assert role == ROLE
        return entry

    registry = _module(
        monkeypatch,
        "src.auth.agent_registry",
        re=re,
        logger=logging.getLogger(__name__),
        datetime=datetime,
        UTC=UTC,
        timedelta=timedelta,
        TokenContext=schema.TokenContext,
        get_agent_registry_service=lambda: SimpleNamespace(
            get_agent_by_role_arn=lookup
        ),
    )
    _functions(
        registry,
        GATEWAY / "auth/agent_registry.py",
        {"parse_assumed_role_arn", "agent_entry_to_token_context"},
    )
    middleware = _module(
        monkeypatch,
        "src.auth.middleware",
        logger=logging.getLogger(__name__),
        verified_caller_identity=provenance.verified_caller_identity,
        get_settings=lambda: settings,
        API_GATEWAY_HEADER_ORG_ID="X-Agent-OrgId",
    )
    _functions(
        middleware,
        GATEWAY / "auth/middleware.py",
        {"extract_iam_identity_from_headers"},
    )
    # The evidence endpoint is not a material broker endpoint. Read maintained
    # route membership rather than treating a future broker addition as absent.
    authority_tree = ast.parse(
        (GATEWAY / "internal/credential_authorization.py").read_text()
    )
    assignment = next(
        node
        for node in authority_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "BROKER_CAPABILITIES"
            for t in node.targets
        )
    )
    paths = frozenset(ast.literal_eval(assignment.value.args[0])) | {
        "/internal/v1/github-installation-token"
    }
    assert EVIDENCE_PATH not in paths

    async def unrelated_broker(*args):
        raise AssertionError("Evidence unexpectedly entered material delivery")

    _module(
        monkeypatch,
        "src.agentauth.broker_identity",
        BROKER_PATHS=paths,
        verify_broker_worker=unrelated_broker,
    )
    auth = _load(
        monkeypatch, "src.internal.auth_deps", GATEWAY / "internal/auth_deps.py"
    )
    return SimpleNamespace(
        auth=auth, settings=settings, entry=entry, reads=registry_reads
    )


def _verify_wire(request, credentials):
    """Offline edge fixture: reject a signature if any actual wire byte changes."""
    assert request.method == "POST" and str(request.url) == ENDPOINT + EVIDENCE_PATH
    assert not {
        "x-internal-api-key",
        "x-caller-identity",
        "x-adp-edge-provenance",
    } & set(request.headers)
    authorization = request.headers["Authorization"]
    match = re.fullmatch(
        r"AWS4-HMAC-SHA256 Credential=([^/]+)/([^,]+), SignedHeaders=([^,]+), Signature=([0-9a-f]{64})",
        authorization,
    )
    assert match and match[1] == credentials.access_key
    assert match[2].endswith("/us-east-1/execute-api/aws4_request")
    signed = {key: request.headers[key] for key in match[3].split(";")}
    assert signed["x-amz-security-token"] == credentials.token
    wire = AWSRequest(
        method=request.method,
        url=str(request.url),
        data=request.content,
        headers=signed,
    )
    wire.context["timestamp"] = request.headers["X-Amz-Date"]
    signer = botocore.auth.SigV4Auth(credentials, "execute-api", "us-east-1")
    expected = signer.signature(
        signer.string_to_sign(wire, signer.canonical_request(wire)), wire
    )
    assert expected == match[4]
    return json.loads(request.content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,accepted",
    [
        ("verified", True),
        ("direct", False),
        ("direct-shared-key", False),
        ("forged-caller", False),
        ("bad-provenance", False),
        ("untrusted-edge", False),
        ("wrong-registry-scope", False),
        ("tampered-body", False),
    ],
)
async def test_actual_client_requires_verified_edge_and_registered_internal_iam(
    gateway, mode, accepted
):
    credentials = Credentials(
        "OFFLINEACCESS",
        "offline-signing-secret",
        "offline-session-token",
        method="assume-role-with-web-identity",
    )
    observed = []

    async def edge(request):
        observed.append(request)
        if mode == "tampered-body":
            altered = httpx.Request(
                request.method,
                request.url,
                headers=request.headers,
                content=request.content + b" ",
            )
            with pytest.raises(AssertionError):
                _verify_wire(altered, credentials)
            return httpx.Response(403)
        body = _verify_wire(request, credentials)
        headers = dict(request.headers)
        if mode not in {"direct", "direct-shared-key"}:
            headers["x-caller-identity"] = CALLER
            if mode != "forged-caller":
                headers["x-adp-edge-provenance"] = (
                    "incorrect" if mode == "bad-provenance" else PROOF
                )
        if mode == "direct-shared-key":
            headers["x-internal-api-key"] = "legacy-transport-key"
        if mode == "untrusted-edge":
            gateway.settings.trust_apigw_headers = False
        if mode == "wrong-registry-scope":
            gateway.entry["scope"] = "organization"
        incoming = Request(
            {
                "type": "http",
                "method": "POST",
                "path": EVIDENCE_PATH,
                "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
            }
        )
        try:
            await gateway.auth.verify_internal_or_irsa(incoming)
        except HTTPException as error:
            return httpx.Response(error.status_code, json={"detail": "refused"})
        assert incoming.state.token_context.agent_registry_id == "registered-api"
        return httpx.Response(
            200,
            json={
                **body,
                "owner_principal": "human",
                "delegated_to_workspaces": [body["workspace_id"]],
                "expires_at": "2099-01-01T00:00:00Z",
                "attested_report_digest": None,
            },
        )

    client = AdpVaultClient(
        base_url=ENDPOINT,
        region="us-east-1",
        session=SimpleNamespace(get_credentials=lambda: credentials),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(edge), follow_redirects=False, trust_env=False
        ),
    )
    result = await client.read(
        org_id="org",
        workspace_id="workspace",
        reference=CredentialReference("cred-id", "aws", 'quote" and café'),
        principal="human",
        report_digest=None,
    )
    assert (result is not None) is accepted
    assert len(observed) == 1
    if accepted:
        assert gateway.reads == [ROLE]
        assert result.reference.label == 'quote" and café'
    elif mode != "wrong-registry-scope":
        assert gateway.reads == []


@pytest.mark.asyncio
async def test_real_refreshable_credentials_are_refrozen_for_each_wire_request():
    generations = []

    def refresh():
        number = len(generations) + 1
        value = {
            "access_key": f"OFFLINEACCESS{number}",
            "secret_key": f"offline-secret{number}",
            "token": f"offline-token{number}",
            "expiry_time": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
        generations.append(value)
        return value

    credentials = RefreshableCredentials.create_from_metadata(
        metadata=refresh(),
        refresh_using=refresh,
        method="assume-role-with-web-identity",
    )
    access_keys = []

    async def edge(request):
        frozen = credentials.get_frozen_credentials()
        _verify_wire(request, frozen)
        access_keys.append(frozen.access_key)
        return httpx.Response(403)

    client = AdpVaultClient(
        base_url=ENDPOINT,
        region="us-east-1",
        session=SimpleNamespace(get_credentials=lambda: credentials),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(edge)),
    )
    payload = dict(
        org_id="org",
        workspace_id="workspace",
        reference=CredentialReference("cred-id", "aws", "label"),
        principal="human",
        report_digest=None,
    )
    assert await client.read(**payload) is None
    credentials._expiry_time = datetime.now(UTC) - timedelta(seconds=1)
    assert await client.read(**payload) is None
    assert access_keys == ["OFFLINEACCESS1", "OFFLINEACCESS2"]
