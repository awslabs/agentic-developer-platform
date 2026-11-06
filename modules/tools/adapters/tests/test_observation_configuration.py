"""Operator preparation uses live readback, never caller-authored schema pins."""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from tool_adapters.atlassian import READ_TOOLS, WRITE_TOOLS
from tool_adapters.atlassian_observe import PROTOCOL_VERSION, SITES_TOOL, SUBJECT_TOOL

SCRIPT = Path(__file__).resolve().parents[1] / "observation_configuration.py"
spec = importlib.util.spec_from_file_location("observation_configuration", SCRIPT)
prepare_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_module)
ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/observer"


def request(*, reads=False, writes=False):
    return {
        "release": {
            "contract_version": "1.0",
            "source_revision": "a" * 40,
            "image_digest": "sha256:" + "b" * 64,
            "group": "atlassian-foundation",
            "group_generation": 2,
            "backend_generation": 3,
        },
        "bindings": {
            "core_authorize_url": "https://core.example/internal/v1/tool-adapters/authorize",
            "core_role_arn": f"arn:aws:iam::{ACCOUNT}:role/core",
            "assertion_key_arn": f"arn:aws:kms:us-east-1:{ACCOUNT}:key/12345678-1234-1234-1234-123456789012",
            "edge_provenance_sha256": "c" * 64,
            "identity_workload": "adp-dev-atlassian-mcp-oauth-user",
            "identity_provider": "adp-dev-atlassian-mcp-user",
            "config_revision": "d" * 64,
            "gateway_role_arn": f"arn:aws:iam::{ACCOUNT}:role/gateway",
        },
        "observer_role_arn": ROLE,
        "owner_id": "owned-member",
        "expected_provider_user_id": "provider-member",
        "site_id": "owned-site",
        "enable_reads": reads,
        "enable_writes": writes,
    }


def catalogue():
    names = [
        SUBJECT_TOOL,
        SITES_TOOL,
        *(name for name, _ in READ_TOOLS.values()),
        *(name for name, _ in WRITE_TOOLS.values()),
    ]
    return [{"name": name, "inputSchema": {"type": "object", "properties": {}}} for name in names]


class Transport:
    def __init__(self, *, deadline):
        self.tools = catalogue()
        self.subject = "provider-member"
        self.scopes = [*prepare_module.READ_SCOPES, "write:jira:agent-interface"]
        self.site = "owned-site"
        self.next_cursor = None

    def send(self, method, params, token):
        assert token == "private-access-token"
        if method == "initialize":
            return {"result": {"protocolVersion": PROTOCOL_VERSION}}
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {"result": {"tools": copy.deepcopy(self.tools), "nextCursor": self.next_cursor}}
        if params["name"] == SUBJECT_TOOL:
            content = {"accountId": self.subject}
        else:
            content = {"resources": [{"id": self.site, "scopes": self.scopes}]}
        return {"result": {"structuredContent": content, "isError": False}}


def clients():
    sts, identity = Mock(), Mock()
    sts.get_caller_identity.return_value = {
        "Account": ACCOUNT,
        "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/observer/ci-run",
    }
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "private-workload-token"}
    identity.get_resource_oauth2_token.return_value = {"accessToken": "private-access-token"}
    return sts, identity


@pytest.mark.parametrize("reads,writes", [(False, False), (True, False), (True, True)])
def test_live_catalogue_and_expected_subject_site_generate_bound_configuration(reads, writes):
    sts, identity = clients()
    expected = request(reads=reads, writes=writes)
    result = prepare_module.prepare(expected, sts_client=sts, identity_client=identity, transport_factory=Transport)
    assert result.mode == "observation"
    assert result.release.model_dump() == expected["release"]
    settings = result.observation
    assert set(settings.read_schemas or {}) == (set(READ_TOOLS) if reads else set())
    assert set(settings.write_schemas or {}) == (set(WRITE_TOOLS) if writes else set())
    assert settings.write_enabled is writes
    payload = result.model_dump_json()
    for secret in ("private-access-token", "private-workload-token", "provider-member", "owned-member", "owned-site"):
        assert secret not in payload
    kwargs = identity.get_resource_oauth2_token.call_args.kwargs
    assert kwargs["resources"] == [prepare_module.RESOURCE]
    assert kwargs["oauth2Flow"] == "USER_FEDERATION"
    assert "forceAuthentication" not in kwargs
    assert ("write:jira:agent-interface" in kwargs["scopes"]) is writes
    identity.get_workload_access_token_for_user_id.assert_called_once_with(
        workloadName=expected["bindings"]["identity_workload"],
        userId=expected["owner_id"],
    )


@pytest.mark.parametrize("change", ["account", "role", "identity_namespace", "core_account", "writes_without_reads"])
def test_wrong_authority_is_rejected_before_token_retrieval(change):
    sts, identity = clients()
    data = request()
    if change == "account":
        sts.get_caller_identity.return_value["Account"] = "999999999999"
    elif change == "role":
        sts.get_caller_identity.return_value["Arn"] = f"arn:aws:sts::{ACCOUNT}:assumed-role/other/ci-run"
    elif change == "identity_namespace":
        data["bindings"]["identity_provider"] = "other-atlassian-mcp-user"
    elif change == "core_account":
        data["bindings"]["core_role_arn"] = "arn:aws:iam::999999999999:role/core"
    else:
        data["enable_writes"] = True
    with pytest.raises(ValueError):
        prepare_module.prepare(data, sts_client=sts, identity_client=identity, transport_factory=Transport)
    assert not identity.mock_calls


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"authorizationUrl": "private-url"},
        {"sessionStatus": "IN_PROGRESS", "accessToken": "private-access-token"},
        {"accessToken": "invalid token"},
    ],
)
def test_missing_or_incomplete_grant_never_reaches_provider(result):
    sts, identity = clients()
    identity.get_resource_oauth2_token.return_value = result
    transport = Mock()
    with pytest.raises(ValueError):
        prepare_module.prepare(request(), sts_client=sts, identity_client=identity, transport_factory=transport)
    transport.assert_not_called()


@pytest.mark.parametrize(
    "change", ["duplicate", "missing", "bad_schema", "cursor_loop", "owner", "site", "scope", "drift"]
)
def test_unverified_provider_data_never_generates_configuration(change):
    sts, identity = clients()
    creations = 0

    def factory(**kwargs):
        nonlocal creations
        creations += 1
        transport = Transport(**kwargs)
        if change == "duplicate":
            transport.tools.append(transport.tools[0])
        elif change == "missing":
            transport.tools = [tool for tool in transport.tools if tool["name"] != READ_TOOLS["get_jira_issue"][0]]
        elif change == "bad_schema":
            transport.tools[0]["inputSchema"] = {"type": "string"}
        elif change == "cursor_loop":
            transport.next_cursor = "same"
        elif change == "owner":
            transport.subject = "someone-else"
        elif change == "site":
            transport.site = "different-site"
        elif change == "scope":
            transport.scopes = []
        elif change == "drift" and creations > 1:
            transport.tools[0]["inputSchema"]["properties"]["changed"] = {"type": "string"}
        return transport

    with pytest.raises(ValueError):
        prepare_module.prepare(request(reads=True), sts_client=sts, identity_client=identity, transport_factory=factory)


def test_request_requires_private_bounded_unique_document(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request()))
    path.chmod(0o600)
    assert prepare_module.read_request(path).owner_id == "owned-member"
    path.chmod(0o644)
    with pytest.raises(ValueError):
        prepare_module.read_request(path)
    path.chmod(0o600)
    path.write_text('{"owner_id":"one","owner_id":"two"}')
    with pytest.raises(ValueError, match="duplicate"):
        prepare_module.read_request(path)
    path.write_text(" " * 16385)
    with pytest.raises(ValueError):
        prepare_module.read_request(path)


def test_configuration_pins_cannot_be_supplied_by_request():
    data = request()
    data["bindings"]["subject_schema_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        prepare_module.Preparation.model_validate(data)


def test_jira_only_grant_can_prepare_reads_without_claiming_confluence_access():
    sts, identity = clients()

    def factory(**kwargs):
        transport = Transport(**kwargs)
        transport.scopes = ["read:jira:agent-interface", "search:jira:agent-interface"]
        return transport

    result = prepare_module.prepare(
        request(reads=True), sts_client=sts, identity_client=identity, transport_factory=factory
    )
    assert result.mode == "observation" and not result.observation.write_enabled
    with pytest.raises(ValueError, match="provider_owner_or_site_mismatch"):
        prepare_module.prepare(
            request(reads=True, writes=True), sts_client=sts, identity_client=identity, transport_factory=factory
        )


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("profile", [False, True])
def test_operator_command_writes_private_configuration_only_after_provider_success(
    tmp_path, monkeypatch, capsys, failed, profile
):
    import boto3

    tmp_path.chmod(0o700)
    source = tmp_path / "request.json"
    source.write_text(json.dumps(request()))
    source.chmod(0o600)
    output = tmp_path / "observation.json"
    sts, identity = clients()
    if failed:
        identity.get_resource_oauth2_token.side_effect = RuntimeError("provider private-token-response")
    session = Mock()

    def client(service, *, config):
        assert config.ignore_configured_endpoint_urls is True
        return {"sts": sts, "bedrock-agentcore": identity}[service]

    session.client.side_effect = client
    monkeypatch.setattr(boto3, "Session", lambda **_: session)
    original = prepare_module.prepare
    monkeypatch.setattr(prepare_module, "prepare", lambda data, **kw: original(data, transport_factory=Transport, **kw))
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--request", str(source), "--output", str(output)] + (["--profile"] if profile else []),
    )
    if failed:
        with pytest.raises(SystemExit) as error:
            prepare_module.main()
        assert str(error.value) == "adapter_observation_configuration_unavailable"
        assert not output.exists()
        assert "private" not in capsys.readouterr().out
    else:
        prepare_module.main()
        assert output.stat().st_mode & 0o777 == 0o600
        result = json.loads(output.read_text())
        assert result["mode"] == "observation"
        if profile:
            assert result["source_revision"] == "a" * 40 and "release" not in result
        calls = list(identity.mock_calls)
        with pytest.raises(SystemExit):
            prepare_module.main()
        assert identity.mock_calls == calls
