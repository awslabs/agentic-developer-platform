"""Controlled MCP/Identity boundaries, not live provider qualification."""

import copy
import json

import pytest
from tool_adapters.atlassian_observe import (
    SITES_TOOL,
    SUBJECT_TOOL,
    collect_observation,
)

from tests.e2e.tools import atlassian_readback_grant as grant
from tests.e2e.tools import atlassian_client_binding as binding
from tests.e2e.tools import runner
from tests.e2e.tools.atlassian_client_binding import bind_clients
from tests.e2e.tools.atlassian_client_matrix import (
    CLIENTS,
    digest,
    load_isolated_fixtures,
)

MARKER = "adp-8454-" + "a" * 16


def configuration(owner):
    calls = {}
    for operation in grant.TOOLS:
        arguments = {"cloudId": {"fixture": "site_id"}}
        if operation == "search":
            arguments.update(
                jql={"fixture": "jql"}, maxResults={"fixture": "max_results"}
            )
        else:
            arguments["id"] = {"fixture": "provider_id"}
        if operation in {"comments", "changelog"}:
            arguments.update(
                startAt={"fixture": "start_at"}, maxResults={"fixture": "max_results"}
            )
        schema = {
            "type": "object",
            "properties": {
                key: {
                    "type": "integer" if key in {"startAt", "maxResults"} else "string"
                }
                for key in arguments
            },
            "required": list(arguments),
        }
        calls[operation] = {"schema_sha256": digest(schema), "arguments": arguments}
    return {
        "account": "123456789012",
        "region": "us-east-1",
        "environment": "dev",
        "adapter_role_arn": "arn:aws:iam::123456789012:role/adp-dev-atlassian-mcp-observer-adapter",
        "adapter_label": "oidc-adapter",
        "workload_name": "adp-tools-dev-atlassian-mcp-oauth-user",
        "provider_name": "adp-tools-dev-atlassian-mcp-user",
        "provider_account_id": owner,
        "site_ids": ["fixture-site"],
        "subject_schema_sha256": digest({}),
        "sites_schema_sha256": digest({}),
        "calls": calls,
    }


@pytest.fixture
def provider(monkeypatch):
    state = {
        "calls": [],
        "sdk": [],
        "missing": None,
        "duplicate": False,
        "subject": None,
        "scopes": list(grant.CONSENT_SCOPES),
    }

    class Transport:
        def __init__(self, **kwargs):
            pass

        def send(self, method, params, token):
            state["calls"].append((method, copy.deepcopy(params)))
            if method == "initialize":
                return {"result": {"protocolVersion": "2025-03-26"}}
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                tools = [
                    {"name": name, "inputSchema": {}}
                    for name in (SUBJECT_TOOL, SITES_TOOL)
                ]
                for operation, name in grant.TOOLS.items():
                    arguments = configuration("owner")["calls"][operation]["arguments"]
                    schema = {
                        "type": "object",
                        "properties": {
                            key: {
                                "type": "integer"
                                if key in {"startAt", "maxResults"}
                                else "string"
                            }
                            for key in arguments
                        },
                        "required": list(arguments),
                    }
                    if name != state["missing"]:
                        tools.append({"name": name, "inputSchema": schema})
                return {"result": {"tools": tools}}
            name, payload = params["name"], params["arguments"]
            if name == SUBJECT_TOOL:
                value = {
                    "account_id": state["subject"] or token.removeprefix("access-")
                }
            elif name == SITES_TOOL:
                value = [{"id": "fixture-site", "scopes": state["scopes"]}]
            elif name == "getJiraIssue":
                value = {
                    "id": payload["id"],
                    "fields": {"summary": MARKER, "description": None},
                }
            elif name == "getConfluenceContent":
                value = {
                    "id": payload["id"],
                    "title": MARKER,
                    "body": {"storage": {"value": MARKER}},
                }
            elif name == "searchJiraIssuesUsingJql":
                value = {"issues": [], "isLast": True}
            elif name == "listJiraIssueComments":
                offset = payload["startAt"]
                value = {
                    "startAt": offset,
                    "total": 2,
                    "comments": [
                        {
                            "id": "1" if state["duplicate"] else str(offset + 1),
                            "author": {"accountId": token.removeprefix("access-")},
                            "body": {"type": "doc", "version": 1, "content": []},
                        }
                    ],
                }
            else:
                raise AssertionError("unexpected provider operation")
            return {
                "result": {"content": [{"type": "text", "text": json.dumps(value)}]}
            }

    def sdk(config, role, service, operation, *, input_value):
        state["sdk"].append((role, service, operation, input_value))
        assert role == "adapter"
        if operation == "get-workload-access-token-for-user-id":
            return {"workloadAccessToken": "workload-" + input_value["userId"]}
        assert input_value["scopes"] == list(grant.CONSENT_SCOPES)
        assert input_value["resources"] == ["https://mcp.atlassian.com/v2/mcp"]
        assert input_value["resourceCredentialProviderName"] == config["provider_name"]
        return {
            "accessToken": "access-"
            + input_value["workloadIdentityToken"].removeprefix("workload-")
        }

    monkeypatch.setattr(grant, "_aws", sdk)
    monkeypatch.setattr(grant, "ReadMCPTransport", Transport)
    monkeypatch.setattr(
        grant,
        "collect_observation",
        lambda token, settings: collect_observation(
            token, settings, transport_factory=Transport
        ),
    )
    return state


def manifest():
    return {
        "run_id": "run-one",
        "owners": {client: "owner-" + client for client in CLIENTS},
        "readback": {client: configuration("owner-" + client) for client in CLIENTS},
    }


def test_protected_manifest_constructs_real_readers_and_observes_each_owner(
    tmp_path, provider
):
    source = manifest()
    path, aws_config = bind_clients(
        json.dumps(source),
        directory=tmp_path,
        account="123456789012",
        region="us-east-1",
        reader_role=source["readback"]["codex"]["adapter_role_arn"],
    )
    fixtures = load_isolated_fixtures(path)
    assert "oidc-adapter" in aws_config.read_text()
    assert aws_config.stat().st_mode & 0o077 == 0
    for client, fixture in fixtures.items():
        reader = fixture["reader"]
        assert isinstance(reader, grant.BoundProviderReader)
        snapshot = reader.readback(
            run_id="run-one",
            client=client,
            tool="get_jira_issue",
            operation_id=client,
            marker=MARKER,
            site_id="fixture-site",
            provider_id="101",
        )
        assert snapshot["account_id"] == source["owners"][client]
        assert snapshot["content_sha256"] == digest(
            {"summary": MARKER, "description": None}
        )
        assert snapshot["effect_count"] == 0
        fixture["ledger"].reserve(
            client=client,
            tool="get_jira_issue",
            operation_id=client,
            marker=MARKER,
            provider_id="101",
            site_id="fixture-site",
            expected=snapshot,
            observer=reader,
        )
        assert "access-" not in fixture["ledger"].path.read_text()
    assert not any(
        "REST" in str(call) or "read:jira-work" in str(call) for call in provider["sdk"]
    )


@pytest.mark.parametrize(
    "damage", ["owner", "site", "scope", "schema", "missing", "foreign-call"]
)
def test_reader_rejects_foreign_or_changed_provider_before_sentinel_read(
    provider, damage
):
    config = configuration("owner")
    if damage == "owner":
        provider["subject"] = "foreign-owner"
    elif damage == "site":
        config["site_ids"] = ["foreign-site"]
    elif damage == "scope":
        provider["scopes"] = ["read:jira-work"]
    elif damage == "schema":
        config["calls"]["issue"]["schema_sha256"] = "0" * 64
    elif damage == "missing":
        provider["missing"] = "listJiraIssueChangelogs"
    lifecycle = grant.IdentityReadbackGrant(config, "owner")
    with pytest.raises(ValueError):
        if damage == "foreign-call":
            lifecycle.with_access_token(
                "another", lambda token: pytest.fail("foreign invocation")
            )
        else:
            lifecycle.reader()
    assert not any(
        params.get("name") == "getJiraIssue"
        for method, params in provider["calls"]
        if method == "tools/call"
    )


def test_paginated_mcp_comments_reuse_the_mutation_oracle(provider):
    reader = grant.IdentityReadbackGrant(configuration("owner"), "owner").reader()
    snapshot = reader.mutation_snapshot(
        tool="comment_jira_issue",
        marker=MARKER,
        site_id="fixture-site",
        provider_id="101",
    )
    assert set(snapshot["events"]) == {"1", "2"}
    assert [
        params["arguments"]["startAt"]
        for method, params in provider["calls"]
        if method == "tools/call" and params["name"] == "listJiraIssueComments"
    ] == [0, 1]
    provider["duplicate"] = True
    with pytest.raises(ValueError, match="repeats"):
        reader.mutation_snapshot(
            tool="comment_jira_issue",
            marker=MARKER,
            site_id="fixture-site",
            provider_id="101",
        )


@pytest.mark.parametrize(
    "damage", ["role", "subject", "owner", "account", "extra-field"]
)
def test_binding_rejects_foreign_authority_before_creating_files(tmp_path, damage):
    source = manifest()
    config = source["readback"]["codex"]
    role = config["adapter_role_arn"]
    if damage == "role":
        config["adapter_role_arn"] = role.replace("observer-adapter", "observer-reader")
    elif damage == "subject":
        config["provider_account_id"] = source["readback"]["claude-code"][
            "provider_account_id"
        ]
    elif damage == "owner":
        source["owners"]["codex"] = source["owners"]["claude-code"]
    elif damage == "account":
        config["account"] = "000000000000"
    else:
        config["access_token"] = "must-not-be-persisted"
    with pytest.raises(ValueError):
        bind_clients(
            json.dumps(source),
            directory=tmp_path,
            account="123456789012",
            region="us-east-1",
            reader_role=role,
        )
    assert list(tmp_path.iterdir()) == []


def test_production_reader_refuses_hardcoded_foreign_site_before_dispatch(provider):
    config = configuration("owner")
    config["calls"]["issue"]["arguments"]["cloudId"] = "foreign-site"
    with pytest.raises(ValueError, match="binding"):
        reader = grant.IdentityReadbackGrant(config, "owner").reader()
        reader.readback(
            run_id="run-one",
            client="codex",
            tool="get_jira_issue",
            operation_id="read-one",
            marker=MARKER,
            site_id="fixture-site",
            provider_id="101",
        )
    assert not any(
        params.get("name") == "getJiraIssue"
        for method, params in provider["calls"]
        if method == "tools/call"
    )


@pytest.mark.parametrize(
    "operation,field",
    [
        ("issue", "id"),
        ("page", "cloudId"),
        ("page", "id"),
        ("search", "jql"),
        ("search", "maxResults"),
        ("comments", "startAt"),
        ("comments", "maxResults"),
        ("changelog", "id"),
        ("changelog", "startAt"),
    ],
)
def test_each_query_target_and_page_uses_the_requested_fixture(
    provider, operation, field
):
    config = configuration("owner")
    config["calls"][operation]["arguments"][field] = "stale-constant"
    with pytest.raises(ValueError, match="binding"):
        grant.IdentityReadbackGrant(config, "owner").reader()
    assert provider["sdk"] == [] and provider["calls"] == []


@pytest.mark.parametrize(
    "damage", ["swapped", "nested", "extra-target", "extra-site", "alternate-id"]
)
def test_substitutions_cannot_be_placed_in_unrelated_arguments(provider, damage):
    config = configuration("owner")
    template = config["calls"]["issue"]["arguments"]
    if damage == "swapped":
        template["cloudId"], template["id"] = template["id"], template["cloudId"]
    elif damage == "nested":
        template["cloudId"] = {"unused": {"fixture": "site_id"}}
    elif damage == "extra-target":
        template["url"] = "https://foreign.example.test"
    elif damage == "extra-site":
        template["unused"] = {"fixture": "site_id"}
    else:
        template["issueIdOrKey"] = {"fixture": "provider_id"}
    with pytest.raises(ValueError, match="binding"):
        grant.IdentityReadbackGrant(config, "owner").reader()
    assert provider["sdk"] == [] and provider["calls"] == []


def test_session_rechecks_binding_before_each_actual_read(provider):
    config = configuration("owner")
    lifecycle = grant.IdentityReadbackGrant(config, "owner")

    def observe(session):
        config["calls"]["issue"]["arguments"]["cloudId"] = "foreign-site"
        return session.get("/ex/jira/fixture-site/rest/api/3/issue/101")

    with pytest.raises(ValueError, match="binding"):
        lifecycle.with_access_token("owner", observe)
    assert not any(
        params.get("name") == "getJiraIssue"
        for method, params in provider["calls"]
        if method == "tools/call"
    )


def test_search_sends_the_exact_requested_marker_query_and_limit(provider):
    lifecycle = grant.IdentityReadbackGrant(configuration("owner"), "owner")
    result = lifecycle.with_access_token(
        "owner",
        lambda session: session.get(
            "/ex/jira/fixture-site/rest/api/3/search/jql?jql="
            + MARKER
            + "&maxResults=2"
        ),
    )
    assert result == {"issues": [], "isLast": True}
    calls = [
        params["arguments"]
        for method, params in provider["calls"]
        if method == "tools/call" and params["name"] == "searchJiraIssuesUsingJql"
    ]
    assert calls == [{"cloudId": "fixture-site", "jql": MARKER, "maxResults": 2}]


def test_protected_entrypoint_receives_qualified_readers(
    tmp_path, provider, monkeypatch
):
    source = manifest()
    environment = tmp_path / "github-env"
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_JSON", json.dumps(source))
    monkeypatch.setenv(
        "ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN",
        source["readback"]["codex"]["adapter_role_arn"],
    )
    monkeypatch.setenv("EXPECTED_ACCOUNT", "123456789012")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_ENV", str(environment))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    binding.main()
    for line in environment.read_text().splitlines():
        key, value = line.split("=", 1)
        monkeypatch.setenv(key, value)
    observed = []
    monkeypatch.setattr(
        runner, "evaluate", lambda *args, **kwargs: observed.append(kwargs) or False
    )
    assert (
        runner.main(
            [
                "--scenario-filter",
                "SCN-8454-02",
                "--output",
                str(tmp_path / "evidence"),
                "--gateway-url",
                "https://gateway.example.test",
                "--gateway-revision",
                "a" * 40,
                "--gateway-image",
                "example.invalid/gateway@sha256:" + "b" * 64,
                "--facade-image",
                "example.invalid/facade@sha256:" + "c" * 64,
            ]
        )
        == 1
    )
    fixtures = observed[0]["atlassian_client_fixtures"]
    assert set(fixtures) == CLIENTS
    assert all(
        isinstance(fixture["reader"], grant.BoundProviderReader)
        and fixture["reader"].owner_id == fixture["owner_id"]
        for fixture in fixtures.values()
    )
    assert "access-" not in environment.read_text()


def test_binding_cleans_private_files_when_qualification_fails(
    tmp_path, provider, monkeypatch
):
    source = manifest()
    provider["subject"] = "foreign-owner"
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_JSON", json.dumps(source))
    monkeypatch.setenv(
        "ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN",
        source["readback"]["codex"]["adapter_role_arn"],
    )
    monkeypatch.setenv("EXPECTED_ACCOUNT", "123456789012")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_ENV", str(tmp_path / "github-env"))
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE", "/prior/private-config")
    with pytest.raises(SystemExit, match="unavailable"):
        binding.main()
    assert list(tmp_path.iterdir()) == []
    assert (
        binding.os.environ["ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE"]
        == "/prior/private-config"
    )


def test_missing_binding_never_constructs_an_unauthenticated_production_reader(
    tmp_path,
):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    path = root / "clients.json"
    path.write_text(json.dumps({"run_id": "run-one", "owners": manifest()["owners"]}))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="independently admitted"):
        load_isolated_fixtures(path)
