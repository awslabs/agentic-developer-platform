"""Real matrix, ledgers, Identity readers and cleanup against modeled external APIs."""

import json
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tool_adapters.atlassian_observe import (
    SITES_TOOL,
    SUBJECT_TOOL,
    collect_observation,
)
from tool_adapters.atlassian import ReadMCPTransport
from tool_adapters.atlassian_observe import MCPTransport, ProviderResponseError
from tests.e2e.tools import atlassian_fixture_provider as cleanup
from tests.e2e.tools import atlassian_readback_grant as grant
from tests.e2e.tools import test_atlassian_native_matrix as suite
from tests.e2e.tools.test_atlassian_readback_grant import configuration


runtime = suite.runtime


def schema(arguments):
    return {
        "type": "object",
        "properties": {
            key: {"type": "integer" if key in {"startAt", "maxResults"} else "string"}
            for key in arguments
        },
        "required": list(arguments),
    }


@pytest.fixture
def lifecycle(runtime, monkeypatch):
    plans, fixtures, state, connection = runtime
    state["rpc_envelope"] = True
    control = {
        "deleted": set(),
        "deletions": [],
        "missing": None,
        "lose_ack": False,
        "sdk": [],
        "unrelated": {"issue-other": "unchanged"},
    }
    configs = {}
    for client in suite.matrix.CLIENTS:
        owner = fixtures[client]["owner_id"]
        readback = configuration("account-" + client)
        readback["site_ids"] = ["site-" + client]
        configs[client] = readback
        delete = {
            "cloudId": {"fixture": "site_id"},
            "issueIdOrKey": {"fixture": "provider_id"},
        }
        projects = {"cloudId": {"fixture": "site_id"}}
        config = {
            "enabled": True,
            "tenant_id": "tenant",
            "site_id": "site-" + client,
            "project_key": "FIX",
            "delete_call": {
                "schema_sha256": suite.matrix.digest(schema(delete)),
                "arguments": delete,
            },
            "projects_call": {
                "schema_sha256": suite.matrix.digest(schema(projects)),
                "arguments": projects,
            },
        }
        fixtures[client]["cleanup_provider"] = cleanup.JiraFixtureProvider(
            config, readback=readback, owner_id=owner, run_id="run-one"
        )
        plans[client]["cells"]["create_jira_issue"]["arguments"]["projectKey"] = "FIX"
        for tool in suite.native.JIRA_DEPENDENTS:
            field = "jql" if tool == "search_jira" else "issueIdOrKey"
            plans[client]["cells"][tool]["arguments"][field] = (
                'id = "' + suite.native.CREATED_JIRA_ID + '"'
                if field == "jql"
                else suite.native.CREATED_JIRA_ID
            )

    class Transport:
        def __init__(self, **kwargs):
            self.deadline = kwargs["deadline"]
            self.session_id, self.request_id = None, 0

        def send(self, method, params, token):
            client = token.removeprefix("access-owner-")
            config = configs[client]
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
                    tools.append(
                        {
                            "name": name,
                            "inputSchema": schema(
                                config["calls"][operation]["arguments"]
                            ),
                        }
                    )
                for operation, name in (
                    ("delete_call", "deleteJiraIssue"),
                    ("projects_call", "listJiraProjects"),
                ):
                    if control["missing"] != name:
                        definition = schema(
                            fixtures[client]["cleanup_provider"].config[operation][
                                "arguments"
                            ]
                        )
                        if (
                            control["missing"] == "schema"
                            and operation == "delete_call"
                        ):
                            definition["description"] = "changed"
                        tools.append({"name": name, "inputSchema": definition})
                return {"result": {"tools": tools}}
            name, arguments = params["name"], params["arguments"]
            reader = fixtures[client]["reader"]
            if name == SUBJECT_TOOL:
                value = {
                    "account_id": "foreign"
                    if control["missing"] == "subject"
                    else "account-" + client
                }
            elif name == SITES_TOOL:
                scopes = list(grant.CONSENT_SCOPES)
                if control["missing"] != "scope":
                    scopes.append(cleanup.DELETE_SCOPE)
                value = [{"id": "site-" + client, "scopes": scopes}]
            elif name == "listJiraProjects":
                value = {
                    "isLast": True,
                    "values": []
                    if control["missing"] == "project"
                    else [{"key": "FIX"}],
                }
            elif name == "searchJiraIssuesUsingJql":
                present = (
                    "create_jira_issue" in reader.effects
                    and client not in control["deleted"]
                    and not control.get("search_missing")
                )
                value = {
                    "isLast": True,
                    "issues": [{"id": "created-one"}] if present else [],
                }
            elif name == "getJiraIssue":
                control["direct_reads"] = control.get("direct_reads", 0) + 1
                if "direct_error" in control:
                    return {"error": control["direct_error"]}
                if client in control["deleted"]:
                    return {
                        "error": {
                            "code": -32000,
                            "data": {"status": 404, "issueIdOrKey": arguments["id"]},
                        }
                    }
                value = reader._get("/ex/jira/site/rest/api/3/issue/created-one", None)
                value["fields"]["project"] = {"key": "FIX"}
                if control.get("foreign_creator"):
                    value["fields"]["creator"] = {"accountId": "unrelated-owner"}
            elif name == "deleteJiraIssue":
                assert arguments == {
                    "cloudId": "site-" + client,
                    "issueIdOrKey": "created-one",
                }
                control["deletions"].append(client)
                control["deleted"].add(client)
                if control["lose_ack"]:
                    control["lose_ack"] = False
                    raise ValueError("Delete succeeded but acknowledgment was lost")
                value = {"deleted": True}
            else:
                raise AssertionError(name)
            return {
                "result": {"content": [{"type": "text", "text": json.dumps(value)}]}
            }

    def sdk(config, role, service, operation, *, input_value):
        control["sdk"].append((operation, deepcopy(input_value)))
        assert role == "adapter"
        if operation == "get-workload-access-token-for-user-id":
            return {"workloadAccessToken": "workload-" + input_value["userId"]}
        assert input_value["scopes"] == [*grant.CONSENT_SCOPES, cleanup.DELETE_SCOPE]
        assert input_value["resourceCredentialProviderName"] == config["provider_name"]
        assert input_value["resources"] == [cleanup.RESOURCE]
        return {
            "accessToken": "access-"
            + input_value["workloadIdentityToken"].removeprefix("workload-")
        }

    monkeypatch.setattr(cleanup, "_aws", sdk)
    monkeypatch.setattr(grant, "ReadMCPTransport", Transport)
    monkeypatch.setattr(cleanup, "WriteMCPTransport", Transport)
    monkeypatch.setattr(
        cleanup,
        "collect_observation",
        lambda token, settings: collect_observation(
            token, settings, transport_factory=Transport
        ),
    )

    def new_executor():
        return suite.native.NativeMatrix(
            plans,
            fixtures,
            gateway_url="https://gateway.example.test",
            gateway_revision=suite.SOURCE,
            client_factory=connection,
        )

    executor = new_executor()
    for client in suite.matrix.CLIENTS:
        cells = deepcopy(executor.plans[client]["cells"])
        for tool in suite.native.JIRA_DEPENDENTS:
            cells[tool]["expected"]["provider_id"] = "created-one"
        for tool in ("get_jira_issue", "search_jira"):
            cells[tool]["arguments"]["projection"] = deepcopy(
                cells["create_jira_issue"]["arguments"]["projection"]
            )
        fixtures[client]["reader"] = suite.GeneratedJiraProvider(
            fixtures[client]["owner_id"], cells
        )
    return executor, new_executor, control, runtime


@pytest.mark.parametrize("client", sorted(suite.matrix.CLIENTS))
def test_owned_fixture_cleanup_and_post_cleanup_restart(lifecycle, client):
    executor, restart, control, runtime = lifecycle
    _, fixtures, state, _ = runtime
    for tool in suite.matrix.OPERATION_ORDER:
        assert executor.execute(client, tool)["status"] == "pass", tool
    assert executor.finalize(client)["status"] == "confirmed"
    executor.close()
    resumed = restart()
    for tool in suite.matrix.OPERATION_ORDER:
        result = resumed.execute(client, tool)
        assert result["status"] == "pass", tool
        assert result["cleanup"]["provider_fixtures"] == "confirmed"
    assert len(state["dispatches"]) == 8 and len(state["exchanges"]) == 1
    assert control["deletions"] == [client] and control["unrelated"] == {
        "issue-other": "unchanged"
    }
    assert resumed.fixture_ledgers[client].state()["cleanup_status"] == "confirmed"
    for path in fixtures[client]["ledger"].path.parent.glob("*.json"):
        assert "access-owner-" not in path.read_text()
    resumed.close()


def test_failed_read_cleans_owned_fixture_without_passing_unattempted_cells(lifecycle):
    executor, restart, control, runtime = lifecycle
    state = runtime[2]
    assert executor.execute("codex", "create_jira_issue")["status"] == "pass"
    state["bad_result"] = True
    assert executor.execute("codex", "get_jira_issue")["status"] == "unknown"
    assert executor.finalize("codex")["status"] == "confirmed"
    executor.close()
    resumed = restart()
    assert resumed.execute("codex", "get_jira_issue")["status"] == "blocked"
    assert resumed.execute("codex", "update_jira_issue")["status"] == "blocked"
    assert len(state["dispatches"]) == 2 and control["deletions"] == ["codex"]
    resumed.close()


def test_unknown_write_prevents_cleanup_until_original_receipt_recovers(lifecycle):
    executor, _, control, runtime = lifecycle
    runtime[2]["uncertain"] = ("codex", "create_jira_issue")
    assert executor.execute("codex", "create_jira_issue")["status"] == "unknown"
    assert executor.finalize("codex")["status"] == "pending"
    assert control["deletions"] == []
    assert executor.execute("codex", "create_jira_issue")["status"] == "pass"
    assert executor.finalize("codex")["status"] == "confirmed"
    assert runtime[2]["dispatches"] == [("codex", "create_jira_issue")]
    executor.close()


def test_lost_delete_response_reconciles_absence_without_another_delete(lifecycle):
    executor, restart, control, runtime = lifecycle
    for tool in suite.matrix.OPERATION_ORDER:
        assert executor.execute("codex", tool)["status"] == "pass"
    control["lose_ack"] = True
    assert executor.finalize("codex")["status"] == "pending"
    executor.close()
    resumed = restart()
    assert resumed.execute("codex", "create_jira_issue")["status"] == "pass"
    assert control["deletions"] == ["codex"] and len(runtime[2]["dispatches"]) == 8
    resumed.close()


@pytest.mark.parametrize(
    "missing", ["scope", "deleteJiraIssue", "schema", "subject", "project"]
)
def test_missing_real_cleanup_capability_prevents_creation(lifecycle, missing):
    executor, _, control, runtime = lifecycle
    control["missing"] = missing
    assert executor.execute("codex", "create_jira_issue")["status"] == "blocked"
    assert not runtime[2]["dispatches"] and not runtime[2]["exchanges"]
    assert control["deletions"] == []
    executor.close()


def test_shared_runner_qualifies_cleanup_for_both_clients(tmp_path, lifecycle):
    executor, _, control, runtime = lifecycle
    executor.close()
    passed, report = suite.evaluate(tmp_path, runtime)
    assert passed is True
    assert report["live_attestation"] is True
    assert len(runtime[2]["dispatches"]) == 16
    assert set(control["deletions"]) == suite.matrix.CLIENTS
    assert all(
        row["cleanup"]["provider_fixtures"] == "confirmed"
        for row in report["scenarios"]
    )


def test_search_miss_does_not_hide_a_known_live_issue(lifecycle):
    executor, _, control, _ = lifecycle
    assert executor.execute("codex", "create_jira_issue")["status"] == "pass"
    before = control["direct_reads"]
    control["search_missing"] = True
    assert executor.finalize("codex")["status"] == "pending"
    assert control["direct_reads"] > before
    assert control["deletions"] == []
    assert executor.fixture_ledgers["codex"].state()["cleanup_status"] == "pending"
    executor.close()


@pytest.mark.parametrize(
    "error",
    [
        {"code": -32000, "data": {"status": 403, "issueIdOrKey": "created-one"}},
        {"code": -32000, "data": {"status": 404, "issueIdOrKey": "another-issue"}},
        {"code": -32000, "message": "not found or unauthorized"},
    ],
)
def test_unconfirmed_direct_absence_cannot_complete_cleanup(lifecycle, error):
    executor, _, control, _ = lifecycle
    assert executor.execute("codex", "create_jira_issue")["status"] == "pass"
    control.update(search_missing=True, direct_error=error)
    assert executor.finalize("codex")["status"] == "pending"
    assert control["deletions"] == []
    executor.close()


@pytest.mark.parametrize("fault", ["foreign_creator", "revoked_scope"])
def test_cleanup_rechecks_ownership_and_consent(lifecycle, fault):
    executor, _, control, _ = lifecycle
    assert executor.execute("codex", "create_jira_issue")["status"] == "pass"
    if fault == "foreign_creator":
        control["foreign_creator"] = True
    else:
        control["missing"] = "scope"
    assert executor.finalize("codex")["status"] == "pending"
    assert control["deletions"] == []
    executor.close()


def test_changed_finalized_evidence_cannot_resume_or_redispatch(lifecycle):
    executor, restart, control, runtime = lifecycle
    assert executor.execute("codex", "create_jira_issue")["status"] == "pass"
    assert executor.finalize("codex")["status"] == "confirmed"
    executor._finalized_path("codex").write_text(
        json.dumps({"version": 1, "proof_sha256": "0" * 64})
    )
    executor.close()
    resumed = restart()
    assert resumed.execute("codex", "create_jira_issue")["status"] == "unknown"
    assert control["deletions"] == ["codex"]
    assert runtime[2]["dispatches"] == [("codex", "create_jira_issue")]
    resumed.close()


def test_ambiguous_delete_response_never_uses_read_retry(monkeypatch):
    arguments = {
        "cloudId": {"fixture": "site_id"},
        "issueIdOrKey": {"fixture": "provider_id"},
    }
    definition = schema(arguments)
    config = {
        "enabled": True,
        "tenant_id": "tenant",
        "site_id": "fixture-site",
        "project_key": "OWNED",
        "delete_call": {
            "schema_sha256": suite.matrix.digest(definition),
            "arguments": arguments,
        },
        "projects_call": {"schema_sha256": "a" * 64, "arguments": {}},
    }
    provider = cleanup.JiraFixtureProvider(
        config, readback=configuration("account"), owner_id="owner", run_id="run"
    )
    session = SimpleNamespace(
        catalogue={"deleteJiraIssue": {"inputSchema": definition}},
        transport=ReadMCPTransport(deadline=time.monotonic() + 30),
        token="modeled-token",
    )
    session.transport.session_id = "original-session"
    session.transport.request_id = 10
    calls = []

    def send(transport, method, params, token):
        assert transport.session_id == "original-session"
        transport.request_id += 1
        calls.append((method, params["name"]))
        raise ProviderResponseError("provider_unavailable")

    monkeypatch.setattr(MCPTransport, "send", send)
    with pytest.raises(ProviderResponseError):
        provider._call(session, "delete_call", "10001")
    assert calls == [("tools/call", "deleteJiraIssue")]
    assert session.transport.request_id == 11
