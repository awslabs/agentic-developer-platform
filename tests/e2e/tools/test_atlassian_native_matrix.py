"""Controlled native process and gateway boundaries; never live model/provider calls."""

import json
import base64
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from urllib.parse import urlsplit
from unittest.mock import create_autospec

import pytest

from tests.e2e.tools import atlassian_client_matrix as matrix
from tests.e2e.tools import atlassian_native_matrix as native
from tests.e2e.tools import client_matrix, runner
from tests.e2e.tools.native_client_transport import NativeClientLaunch
from modules.tools.platform.scripts.native_test_session import ApprovedReference

SOURCE = "a" * 40
IMAGE = "example.invalid/image@sha256:" + "b" * 64


class Reader(matrix.IndependentProviderReader):
    def __init__(self, owner, cells):
        self.owner_id, self.cells = owner, cells
        self.effects, self.calls = set(), []

    def mutation_snapshot(self, *, tool, marker, site_id, provider_id):
        cell = self.cells[tool]
        expected = cell["expected"]
        assert (marker, site_id, provider_id) == (
            expected["marker"],
            expected["site_id"],
            expected["provider_id"],
        )
        before = (
            {"status_id": "before"}
            if tool == "transition_jira_issue"
            else {"summary": marker + " before"}
        )
        events, resource = {}, before
        if tool in self.effects:
            change = cell["mutation_expected"]
            if tool == "comment_jira_issue":
                event = {"author_id": expected["account_id"], "body": change["body"]}
            else:
                key = "status_id" if tool == "transition_jira_issue" else "summary"
                event = {
                    "author_id": expected["account_id"],
                    "from": before[key],
                    "to": change[key],
                }
                resource = change
            events = {"event-one": event}
        snapshot = {
            "version": 1,
            "binding": {
                "site_id": site_id,
                "issue_id": provider_id,
                "account_id": expected["account_id"],
                "operation": tool,
                "marker": marker,
            },
            "resource": resource,
            "events": events,
        }
        return {**snapshot, "sha256": matrix.digest(snapshot)}

    def readback(self, **fields):
        self.calls.append(fields)
        tool = fields["tool"]
        result = dict(self.cells[tool]["expected"])
        assert fields["operation_id"] == result["operation_id"]
        if tool in matrix.WRITES and tool not in self.effects:
            result.update(effect_count=0)
            if tool == "create_jira_issue":
                result["content_sha256"] = None
        elif tool == "create_jira_issue":
            result["provider_id"] = "created-one"
        return result


class GeneratedJiraProvider(matrix.IndependentProviderReader):
    def __init__(self, owner, cells):
        super().__init__(
            SimpleNamespace(
                with_access_token=lambda owner, observe: observe("controlled-token")
            ),
            owner,
        )
        self.cells, self.effects, self.calls = cells, set(), []

    def _get(self, path, token):
        self.calls.append(path)
        create = self.cells["create_jira_issue"]
        account = create["expected"]["account_id"]
        if path == "/oauth/token/accessible-resources":
            return [
                {
                    "id": create["expected"]["site_id"],
                    "scopes": ["read:jira-work", "read:confluence-content.all"],
                }
            ]
        if path == "/me":
            return {"account_id": account}
        if "/search/jql?" in path:
            return {
                "issues": [{"id": "created-one"}]
                if "create_jira_issue" in self.effects
                else [],
                "isLast": True,
            }
        if "/wiki/" in path:
            resource_id = urlsplit(path).path.rsplit("/", 1)[1]
            cell = next(
                cell
                for cell in self.cells.values()
                if cell["expected"]["provider_id"] == resource_id
            )
            projection = cell["arguments"]["projection"]
            return {
                "id": resource_id,
                "title": projection["title"],
                "body": {"storage": {"value": projection["body"]}},
            }
        assert "/issue/created-one" in path
        if "/comment?" in path:
            rows = (
                [
                    {
                        "id": "comment-one",
                        "author": {"accountId": account},
                        "body": self.cells["comment_jira_issue"]["mutation_expected"][
                            "body"
                        ],
                    }
                ]
                if "comment_jira_issue" in self.effects
                else []
            )
            return {"startAt": 0, "total": len(rows), "comments": rows}
        if "/changelog?" in path:
            rows = []
            for tool, field, suffix, before, after in (
                (
                    "update_jira_issue",
                    "summary",
                    "String",
                    create["arguments"]["projection"]["summary"],
                    self.cells["update_jira_issue"]["mutation_expected"]["summary"],
                ),
                ("transition_jira_issue", "status", "", "before", "after"),
            ):
                if tool in self.effects:
                    rows.append(
                        {
                            "id": tool,
                            "author": {"accountId": account},
                            "items": [
                                {
                                    "field": field,
                                    "from" + suffix: before,
                                    "to" + suffix: after,
                                }
                            ],
                        }
                    )
            return {"startAt": 0, "total": len(rows), "values": rows}
        projection = create["arguments"]["projection"]
        summary = (
            self.cells["update_jira_issue"]["mutation_expected"]["summary"]
            if "update_jira_issue" in self.effects
            else projection["summary"]
        )
        return {
            "id": "created-one",
            "fields": {
                "summary": summary,
                "description": projection["description"],
                "creator": {"accountId": account},
                "status": {
                    "id": "after"
                    if "transition_jira_issue" in self.effects
                    else "before"
                },
            },
        }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    plans, fixtures = {}, {}
    state = {
        "admissions": [],
        "exchanges": [],
        "dispatches": [],
        "closes": [],
        "deny": False,
        "uncertain": None,
        "bad_result": False,
        "close_failure": False,
        "wrong_owner": False,
        "originals": {},
    }
    for index, client in enumerate(sorted(matrix.CLIENTS)):
        directory = root / client
        directory.mkdir(mode=0o700)
        cells = {}
        for number, tool in enumerate(sorted(matrix.READS | matrix.WRITES)):
            marker = f"adp-8454-{index:01x}{number:015x}"
            projection = (
                {"summary": marker, "description": None}
                if "jira" in tool
                else {"title": marker, "body": marker}
            )
            if tool == "update_jira_issue":
                projection = {"summary": marker + " after"}
            if tool == "comment_jira_issue":
                projection = {
                    "body": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": marker}],
                            }
                        ],
                    }
                }
            if tool == "transition_jira_issue":
                projection = {"status_id": "after"}
            expected = {
                "source": "direct-atlassian-provider",
                "run_id": "run-one",
                "client": client,
                "tool": tool,
                "operation_id": client + "-" + tool,
                "marker": marker,
                "site_id": "site-" + client,
                "provider_id": None
                if tool == "create_jira_issue"
                else str(number + 100),
                "account_id": "account-" + client,
                "effect_count": int(tool in matrix.WRITES),
                "content_sha256": matrix.digest(projection),
            }
            cells[tool] = {
                "expected": expected,
                "arguments": {"cloudId": expected["site_id"], "projection": projection},
                "projection_paths": {key: "/projection/" + key for key in projection}
                if tool in matrix.WRITES
                else {},
                "mutation_expected": projection
                if tool in matrix.WRITES - {"create_jira_issue"}
                else None,
            }
        ledger = matrix.ClientOperationLedger(
            directory / "operations.json", run_id="run-one"
        )
        fixtures[client] = {
            "owner_id": "owner-" + client,
            "ledger": ledger,
            "reader": Reader("owner-" + client, cells),
        }
        plans[client] = {
            "approval_file": str(directory / "native-approval.json"),
            "login_file": str(directory / "native-login.token"),
            "model": "controlled-model",
            "bounds": {
                "max_model_requests": 2,
                "max_input_tokens": 1000,
                "max_output_tokens": 1000,
                "max_request_bytes": 65536,
                "max_response_bytes": 262144,
            },
            "cells": cells,
        }

    def exchange(environment):
        state["exchanges"].append(environment["ADP_TOOLS_SMOKE_APPROVAL_FILE"])
        if state["deny"]:
            raise ValueError("Denied by original native admission")
        client = Path(environment["ADP_TOOLS_SMOKE_APPROVAL_FILE"]).parent.name
        state["admissions"].append(client)
        reference = ApprovedReference(
            str(uuid4()),
            "tenant",
            str(uuid4()),
            "offline",
            "https://gateway.example.test",
            SOURCE,
            SOURCE,
            "42",
            "1",
        )
        token = (
            "e30."
            + base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "sub": "owner-" + client,
                        "custom:org_id": "tenant",
                        "exp": int(time.time()) + 3600,
                    }
                ).encode()
            )
            .decode()
            .rstrip("=")
            + ".offline"
        )
        return native.NativeSession(
            reference,
            token,
            "adpt1." + str(uuid4()) + "." + "s" * 43,
            SimpleNamespace(),
        )

    def status(session):
        payload = json.loads(
            base64.urlsafe_b64decode(session.token.split(".")[1] + "==")
        )
        return {
            "principal": {
                "user_id": "foreign" if state["wrong_owner"] else payload["sub"],
                "tenant_id": "tenant",
            },
            "run_started_at": int(time.time()),
            "run_id": session.reference.run_id,
            "funding_status": "held",
            "cap_usd": "20",
        }

    @contextmanager
    def install(*args, recovery_root, **kwargs):
        home = recovery_root / "home"
        yield (
            home / ".adp/bin/adp",
            {"HOME": str(home), "client": recovery_root.name},
            {"adp": "c" * 64, "adp-mcp.py": "d" * 64},
        )

    @contextmanager
    def launch(installed, environ, session, transport_file, *, client, model):
        assert type(session) is native.NativeSession
        assert isinstance(installed, Path) and isinstance(environ, dict)
        environment = {**environ, "ADP_NATIVE_TEST_TRANSPORT_FILE": str(transport_file)}
        yield (
            NativeClientLaunch(
                client,
                str(installed),
                str(transport_file),
                model,
                ("claude" if client == "claude-code" else "codex",),
                "e" * 64,
            ),
            environment,
        )

    class Connection:
        def __init__(self, command, *, environ):
            self.client = environ["client"]
            self.environ = environ

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, params=None):
            if method == "tools/list":
                return {
                    "tools": [
                        {
                            "name": "atlassian___" + tool,
                            "inputSchema": {
                                "properties": {key: {} for key in cell["arguments"]},
                                "required": list(cell["arguments"]),
                            },
                        }
                        for tool, cell in plans[self.client]["cells"].items()
                    ]
                }
            if method == "tools/call" and params["name"] == "adp___operation_status":
                operation_ref = params["arguments"]["operation_ref"]
                payload, receipt = state["originals"][operation_ref]
                transport = native_transport.NativeTransport(
                    self.environ["ADP_NATIVE_TEST_TRANSPORT_FILE"]
                )
                attempt = transport.begin(
                    "POST",
                    "https://gateway.example.test/api/agent/tools/operations/recover",
                    json.dumps(payload).encode(),
                )
                if state.get("status_failure"):
                    return {"isError": True, "content": []}
                attempt.complete(200, json.dumps(receipt).encode())
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {**receipt, "operation_ref": operation_ref}
                            ),
                        }
                    ]
                }
            assert method == "tools/call" and params["name"] == "adp___execution_close"
            state["closes"].append(self.client)
            if state["close_failure"]:
                raise ValueError("Close response lost")
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "execution_id": state["originals"][
                                    params["arguments"]["operation_ref"]
                                ][1]["execution_id"],
                                "operation_ref": params["arguments"]["operation_ref"],
                                "state": "closed",
                                "cleanup_status": "confirmed",
                            }
                        ),
                    }
                ]
            }

    def process(command, *, environ, input_data=None, **kwargs):
        if "--version" in command:
            return SimpleNamespace(returncode=0, stdout=b"controlled-client-1")
        prompt = input_data.decode()
        tool = (
            prompt.split("MCP operation ", 1)[1]
            .split(" once", 1)[0]
            .removeprefix("atlassian___")
        )
        client = environ["client"]
        fixture = fixtures[client]
        cell = deepcopy(fixture["reader"].cells[tool])
        cell["arguments"] = json.loads(
            prompt.split("using this JSON as arguments: ", 1)[1].removesuffix(
                ". Do not invoke any other tool."
            )
        )
        assert client in state["admissions"]
        assert (
            fixture["ledger"].state()["operations"][cell["expected"]["operation_id"]][
                "status"
            ]
            == "pending"
        )
        state["dispatches"].append((client, tool))
        transport = native_transport.NativeTransport(
            environ["ADP_NATIVE_TEST_TRANSPORT_FILE"]
        )
        request_id = str(uuid4())
        payload = {
            "tool": "atlassian___" + tool,
            "arguments": cell["arguments"],
            "execution_id": None,
            "request_id": request_id,
        }
        if state.get("reordered_arguments"):
            payload["arguments"] = dict(reversed(list(cell["arguments"].items())))
        attempt = transport.begin(
            "POST",
            "https://gateway.example.test/api/agent/tools/call",
            json.dumps(payload).encode(),
        )
        fixture["reader"].effects.add(tool)
        projection = (fixture["reader"].cells[tool]["arguments"]["projection"]
                      if tool in matrix.READS else cell["arguments"]["projection"])
        resource = {"id": cell["expected"]["provider_id"] or "created-one"}
        if tool in matrix.READS:
            if "jira" in tool:
                resource["fields"] = projection
            else:
                resource.update(
                    title=projection["title"],
                    body={"storage": {"value": projection["body"]}},
                )
        if state["bad_result"]:
            resource["id"] = "foreign"
        if tool.startswith("search_"):
            resource = {"issues" if tool == "search_jira" else "results": [resource]}
        state["execution_id"] = str(uuid4())
        receipt = {
            "request_id": request_id,
            "execution_id": state["execution_id"],
            "receipt_id": "tools-test-effect:"
            + matrix.digest(
                {
                    "tenant_id": "tenant",
                    "run_id": transport.reference["run_id"],
                    "operation_id": attempt.entry["operation_id"],
                }
            ),
            "root_human_id": fixture["owner_id"],
            "status": "confirmed",
            "result_status": "success",
            "result": {"structuredContent": resource},
        }
        if state.get("rpc_envelope"):
            receipt["result"] = {"jsonrpc": "2.0", "id": request_id, "result": receipt["result"]}
        state["originals"][request_id] = (payload, deepcopy(receipt))
        if state["uncertain"] == (client, tool):
            raise TimeoutError("Lost native response")
        attempt.complete(200, json.dumps(receipt).encode())
        receipt["operation_ref"] = request_id
        if state.get("receipt_tamper"):
            if state["receipt_tamper"] == "result":
                receipt["result"] = {"structuredContent": {"id": "injected"}}
            else:
                receipt[state["receipt_tamper"]] = str(uuid4())
        if state.get("journal_tamper"):
            with transport.journal() as journal:
                entry = journal["attempts"][-1]
                if state["journal_tamper"] == "missing":
                    journal["attempts"].pop()
                elif state["journal_tamper"] == "duplicate":
                    journal["attempts"].append(deepcopy(entry))
                elif state["journal_tamper"] == "request":
                    entry["request_value_sha256"] = "a" * 64
                else:
                    entry["status"] = "attempted"
        packet = {"content": [{"type": "text", "text": json.dumps(receipt)}]}
        usage = {"input_tokens": 10, "output_tokens": 10}
        if client == "claude-code":
            events = [
                {
                    "type": "assistant",
                    "message": {
                        "usage": usage,
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call",
                                "name": "mcp__adp-tools__atlassian___" + tool,
                                "input": cell["arguments"],
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call",
                                "content": packet["content"],
                            }
                        ]
                    },
                },
            ]
        else:
            events = [
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "id": "call",
                        "server": "adp-tools",
                        "tool": "atlassian___" + tool,
                        "arguments": cell["arguments"],
                        "result": packet,
                    },
                },
                {"type": "turn.completed", "usage": usage},
            ]
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(event) for event in events).encode(),
        )

    monkeypatch.setattr(native.NativeSession, "exchange", exchange)
    monkeypatch.setattr(native.NativeSession, "status", status)
    monkeypatch.setattr(native, "installed_clients", install)
    monkeypatch.setattr(native, "resume_installed_clients", install)
    monkeypatch.setattr(
        native,
        "native_client_transport",
        create_autospec(native.native_client_transport, side_effect=launch),
    )
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[3] / "modules/gateway/cli")
    )
    import adp_native_test as native_transport

    monkeypatch.setattr(client_matrix, "run_bounded", process)
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    return plans, fixtures, state, Connection


def evaluate(tmp_path, runtime):
    plans, fixtures, state, connection = runtime
    passed = runner.evaluate(
        "SCN-8454-02",
        tmp_path / "evidence",
        "https://gateway.example.test",
        SOURCE,
        IMAGE,
        IMAGE,
        command=["unused"],
        live=True,
        client_factory=connection,
        atlassian_client_fixtures=fixtures,
        atlassian_native_plans=plans,
    )
    return passed, json.loads((tmp_path / "evidence/evidence.json").read_text())


@pytest.mark.parametrize("client", sorted(matrix.CLIENTS))
def test_restart_recovers_original_receipt_without_new_admission_or_write(
    runtime, client
):
    plans, fixtures, state, connection = runtime
    state["uncertain"] = (client, "create_jira_issue")
    first = native.NativeMatrix(
        plans,
        fixtures,
        gateway_url="https://gateway.example.test",
        gateway_revision=SOURCE,
        client_factory=connection,
    )
    assert first.execute(client, "create_jira_issue")["status"] == "unknown"
    first.close()
    original = deepcopy(fixtures[client]["ledger"].state())
    restarted = native.NativeMatrix(
        plans,
        fixtures,
        gateway_url="https://gateway.example.test",
        gateway_revision=SOURCE,
        client_factory=connection,
    )
    result = restarted.execute(client, "create_jira_issue")
    assert result["status"] == "pass"
    assert result["cleanup"]["execution"] == "confirmed"
    assert state["dispatches"] == [(client, "create_jira_issue")]
    assert len(state["exchanges"]) == 1
    operation_id = plans[client]["cells"]["create_jira_issue"]["expected"][
        "operation_id"
    ]
    row = fixtures[client]["ledger"].state()["operations"][operation_id]
    assert (
        row["native_binding"] == original["operations"][operation_id]["native_binding"]
    )
    assert row["status"] == "confirmed" and row["provider_id"] == "created-one"
    next_cell = restarted.execute(client, "get_jira_issue")
    assert next_cell["status"] == "pass"
    assert state["dispatches"] == [
        (client, "create_jira_issue"),
        (client, "get_jira_issue"),
    ]
    assert len(state["exchanges"]) == 1
    assert type(restarted.clients[client][3]) is NativeClientLaunch
    restarted.close()


@pytest.mark.parametrize("client", sorted(matrix.CLIENTS))
def test_original_receipt_unavailable_stays_unknown_then_recovers(runtime, client):
    plans, fixtures, state, connection = runtime
    tool = "create_jira_issue"
    state["uncertain"] = (client, tool)
    executor = native.NativeMatrix(
        plans,
        fixtures,
        gateway_url="https://gateway.example.test",
        gateway_revision=SOURCE,
        client_factory=connection,
    )
    assert executor.execute(client, tool)["status"] == "unknown"
    state["status_failure"] = True
    assert executor.execute(client, tool)["status"] == "unknown"
    state["status_failure"] = False
    assert executor.execute(client, tool)["status"] == "pass"
    assert state["dispatches"] == [(client, tool)]
    assert executor.execute(client, "get_jira_issue")["status"] == "pass"
    assert state["dispatches"] == [(client, tool), (client, "get_jira_issue")]
    assert len(state["exchanges"]) == 1
    executor.close()


def test_confirmed_result_can_resume_failed_execution_close(runtime):
    plans, fixtures, state, connection = runtime
    state["close_failure"] = True
    executor = native.NativeMatrix(
        plans,
        fixtures,
        gateway_url="https://gateway.example.test",
        gateway_revision=SOURCE,
        client_factory=connection,
    )
    assert executor.execute("codex", "create_jira_issue")["status"] == "unknown"
    state["close_failure"] = False
    assert executor.execute("codex", "create_jira_issue")["status"] == "pass"
    assert state["dispatches"] == [("codex", "create_jira_issue")]
    executor.close()


def generated_jira_plan(runtime, client):
    plans, fixtures, state, connection = runtime
    cells = plans[client]["cells"]
    sentinel = " ".join(
        cells[tool]["expected"]["marker"] for tool in sorted(matrix.WRITES)
    )
    for tool in (
        "create_jira_issue",
        "get_jira_issue",
        "search_jira",
        "update_jira_issue",
    ):
        projection = cells[tool]["arguments"]["projection"]
        projection["summary"] = sentinel + (
            " updated" if tool == "update_jira_issue" else ""
        )
        cells[tool]["expected"]["content_sha256"] = matrix.digest(projection)
    for tool in native.JIRA_DEPENDENTS:
        cell = cells[tool]
        cell["expected"]["provider_id"] = native.CREATED_JIRA_ID
        field = "jql" if tool == "search_jira" else "issueIdOrKey"
        cell["arguments"][field] = (
            'id = "' + native.CREATED_JIRA_ID + '"'
            if field == "jql"
            else native.CREATED_JIRA_ID
        )
    observer_cells = deepcopy(cells)
    for tool in native.JIRA_DEPENDENTS:
        observer_cells[tool]["expected"]["provider_id"] = "created-one"
    fixtures[client]["reader"] = GeneratedJiraProvider(
        fixtures[client]["owner_id"], observer_cells
    )
    return plans, fixtures, state, connection


@pytest.mark.parametrize("client", sorted(matrix.CLIENTS))
def test_generated_jira_dependencies_resume_from_original_confirmed_create(
    runtime, client
):
    plans, fixtures, state, connection = generated_jira_plan(runtime, client)
    cells = plans[client]["cells"]
    original_plan = deepcopy(plans)
    executor = native.NativeMatrix(
        plans,
        fixtures,
        gateway_url="https://gateway.example.test",
        gateway_revision=SOURCE,
        client_factory=connection,
    )
    assert executor.execute(client, "get_jira_issue")["status"] == "blocked"
    assert not state["exchanges"] and not state["dispatches"]
    state["uncertain"] = (client, "create_jira_issue")
    assert executor.execute(client, "create_jira_issue")["status"] == "unknown"
    state["status_failure"] = True
    assert executor.execute(client, "create_jira_issue")["status"] == "unknown"
    executor.close()
    state["status_failure"] = False
    restarted = native.NativeMatrix(
        plans,
        fixtures,
        gateway_url="https://gateway.example.test",
        gateway_revision=SOURCE,
        client_factory=connection,
    )
    assert restarted.execute(client, "create_jira_issue")["status"] == "pass"
    for tool in matrix.OPERATION_ORDER[1:]:
        assert restarted.execute(client, tool)["status"] == "pass"
    assert state["dispatches"] == [(client, tool) for tool in matrix.OPERATION_ORDER]
    assert len(state["exchanges"]) == 1
    assert plans == original_plan
    assert any("/changelog?" in path for path in fixtures[client]["reader"].calls)
    assert any("/comment?" in path for path in fixtures[client]["reader"].calls)
    operations = fixtures[client]["ledger"].state()["operations"]
    for tool in native.JIRA_DEPENDENTS:
        record = operations[cells[tool]["expected"]["operation_id"]]
        arguments = native.dependent_arguments(
            tool, cells[tool]["arguments"], "created-one"
        )
        assert record["expected"]["provider_id"] == "created-one"
        assert record["native_binding"]["arguments_sha256"] == matrix.digest(arguments)
    restarted.close()


@pytest.mark.parametrize(
    "changed", ["create-marker", "update-marker", "length", "read-sentinel", "partial"]
)
def test_generated_jira_plan_rejects_unobservable_fixture_before_admission(
    runtime, changed
):
    plans, fixtures, state, connection = generated_jira_plan(runtime, "codex")
    cells = plans["codex"]["cells"]
    if changed in {"create-marker", "update-marker", "length"}:
        tool = (
            "update_jira_issue" if changed == "update-marker" else "create_jira_issue"
        )
        projection = cells[tool]["arguments"]["projection"]
        projection["summary"] = (
            projection["summary"] + "x" * 256
            if changed == "length"
            else cells[tool]["expected"]["marker"]
        )
        cells[tool]["expected"]["content_sha256"] = matrix.digest(projection)
    elif changed == "read-sentinel":
        cells["get_jira_issue"]["expected"]["content_sha256"] = "a" * 64
    else:
        cells["get_jira_issue"]["expected"]["provider_id"] = "predeclared"
    with pytest.raises(ValueError, match="Jira"):
        native.NativeMatrix(
            plans,
            fixtures,
            gateway_url="https://gateway.example.test",
            gateway_revision=SOURCE,
            client_factory=connection,
        )
    assert not state["exchanges"] and not state["dispatches"]


@pytest.mark.parametrize("client", sorted(matrix.CLIENTS))
def test_completed_generated_sequence_resumes_after_its_own_update(runtime, client):
    plans, fixtures, state, connection = generated_jira_plan(runtime, client)
    for _ in range(2):
        executor = native.NativeMatrix(
            plans,
            fixtures,
            gateway_url="https://gateway.example.test",
            gateway_revision=SOURCE,
            client_factory=connection,
        )
        for tool in matrix.OPERATION_ORDER:
            result = executor.execute(client, tool)
            assert result["status"] == "pass", tool
        executor.close()
    assert state["dispatches"] == [(client, tool) for tool in matrix.OPERATION_ORDER]
    assert len(state["exchanges"]) == 1


@pytest.mark.parametrize(
    "changed",
    [
        "description",
        "history",
        "missing-resource",
        "missing-observation",
        "update-receipt",
    ],
)
def test_completed_generated_sequence_does_not_trust_stored_passes(
    runtime, monkeypatch, changed
):
    plans, fixtures, state, connection = generated_jira_plan(runtime, "codex")
    executor = native.NativeMatrix(
        plans,
        fixtures,
        gateway_url="https://gateway.example.test",
        gateway_revision=SOURCE,
        client_factory=connection,
    )
    for tool in matrix.OPERATION_ORDER:
        assert executor.execute("codex", tool)["status"] == "pass"
    executor.close()
    ledger, reader = fixtures["codex"]["ledger"], fixtures["codex"]["reader"]
    if changed == "missing-observation":
        with ledger._locked():
            record = ledger._read()
            record["operations"][
                plans["codex"]["cells"]["create_jira_issue"]["expected"]["operation_id"]
            ].pop("provider_observation")
            ledger._write(record)
    elif changed == "update-receipt":
        operation_id = plans["codex"]["cells"]["update_jira_issue"]["expected"][
            "operation_id"
        ]
        path = ledger.path.parent / (
            "native-result-" + matrix.digest(operation_id) + ".json"
        )
        retained = json.loads(path.read_text())
        retained["private_receipt"]["request_id"] = str(uuid4())
        path.write_text(json.dumps(retained))
    else:
        original_get = reader._get

        def changed_get(path, token):
            if changed == "missing-resource" and "/issue/" in path:
                raise ValueError("Independent resource is absent")
            result = original_get(path, token)
            if changed == "description" and "fields" in result:
                result["fields"]["description"] = "unrelated change"
            if changed == "history" and "/changelog?" in path:
                result["values"].append(
                    {**result["values"][0], "id": "unrelated-change"}
                )
                result["total"] += 1
            return result

        monkeypatch.setattr(reader, "_get", changed_get)
    restarted = native.NativeMatrix(
        plans,
        fixtures,
        gateway_url="https://gateway.example.test",
        gateway_revision=SOURCE,
        client_factory=connection,
    )
    assert restarted.execute("codex", "create_jira_issue")["status"] == "unknown"
    assert state["dispatches"] == [("codex", tool) for tool in matrix.OPERATION_ORDER]
    assert len(state["exchanges"]) == 1
    restarted.close()


@pytest.mark.parametrize(
    "changed", ["owner_id", "gateway_revision", "transport_sha256", "missing"]
)
def test_recovery_refuses_missing_or_foreign_original_runtime(runtime, changed):
    plans, fixtures, state, connection = runtime
    state["uncertain"] = ("codex", "create_jira_issue")
    executor = native.NativeMatrix(
        plans,
        fixtures,
        gateway_url="https://gateway.example.test",
        gateway_revision=SOURCE,
        client_factory=connection,
    )
    assert executor.execute("codex", "create_jira_issue")["status"] == "unknown"
    path = fixtures["codex"]["ledger"].path.parent / "native-runtime.json"
    if changed == "missing":
        path.unlink()
    else:
        metadata = json.loads(path.read_text())
        metadata[changed] = "foreign"
        path.write_text(json.dumps(metadata))
    assert executor.execute("codex", "create_jira_issue")["status"] == "unknown"
    assert state["dispatches"] == [("codex", "create_jira_issue")]
    assert len(state["exchanges"]) == 1
    executor.close()


def test_all_sixteen_cells_dispatch_after_admission_and_reservation(tmp_path, runtime):
    passed, report = evaluate(tmp_path, runtime)
    _, fixtures, state, _ = runtime
    assert len(state["dispatches"]) == 16 and len(state["closes"]) == 16
    for client in matrix.CLIENTS:
        assert [tool for owner, tool in state["dispatches"] if owner == client] == list(
            matrix.OPERATION_ORDER
        )
    assert set(state["admissions"]) == matrix.CLIENTS
    assert all(row["status"] == "pass" for row in report["scenarios"])
    assert all(
        row["client_evidence"]["provider_source"] == "direct-atlassian-provider"
        for row in report["scenarios"]
    )
    assert all(
        row["observations"]["operation_id"] and row["evidence_sha256"]
        for row in report["scenarios"]
    )
    assert all(len(fixture["reader"].calls) >= 16 for fixture in fixtures.values())
    assert all(
        row["status"] == "confirmed"
        for fixture in fixtures.values()
        for row in fixture["ledger"].state()["operations"].values()
    )
    for fixture in fixtures.values():
        for operation_id, operation in fixture["ledger"].state()["operations"].items():
            receipt = operation["native_receipt"]
            assert receipt["operation_ref"] != operation_id
            assert receipt["source"] == "native-transport-journal"
            assert matrix.valid_native_receipt(receipt, operation["native_binding"])
    assert (
        not passed and not report["live_attestation"] and report["cleanup_incomplete"]
    )
    assert "owner-codex" not in json.dumps(
        report
    ) and "private_receipt" not in json.dumps(report)


@pytest.mark.parametrize("failure", ["deny", "wrong_owner"])
def test_missing_or_foreign_admission_never_dispatches(tmp_path, runtime, failure):
    runtime[2][failure] = True
    passed, report = evaluate(tmp_path, runtime)
    assert not passed and {row["status"] for row in report["scenarios"]} == {"blocked"}
    assert runtime[2]["dispatches"] == []
    assert len(runtime[2]["exchanges"]) == 2
    assert all(
        fixture["ledger"].state()["operations"] == {} for fixture in runtime[1].values()
    )


def test_uncertain_dispatch_remains_unknown_without_replay(tmp_path, runtime):
    runtime[2]["uncertain"] = ("codex", "create_jira_issue")
    _, first = evaluate(tmp_path, runtime)
    row = next(
        row
        for row in first["scenarios"]
        if (row["client"], row["tool"]) == runtime[2]["uncertain"]
    )
    assert row["status"] == "unknown"
    before = list(runtime[2]["dispatches"])
    _, second = evaluate(tmp_path, runtime)
    assert runtime[2]["dispatches"][: len(before)] == before
    assert len(runtime[2]["dispatches"]) == 16
    assert runtime[2]["dispatches"].count(("codex", "create_jira_issue")) == 1
    recovered = next(
        row
        for row in second["scenarios"]
        if (row["client"], row["tool"]) == runtime[2]["uncertain"]
    )
    assert recovered["status"] == "pass"
    assert all(row["status"] == "pass" for row in second["scenarios"])
    closes = list(runtime[2]["closes"])
    _, third = evaluate(tmp_path, runtime)
    assert len(runtime[2]["dispatches"]) == 16
    assert runtime[2]["closes"] == closes
    assert all(row["status"] == "pass" for row in third["scenarios"])


def test_cleanup_loss_is_not_a_success(tmp_path, runtime):
    runtime[2]["close_failure"] = True
    passed, report = evaluate(tmp_path, runtime)
    assert not passed and not report["live_attestation"]
    assert len(runtime[2]["dispatches"]) == 16
    assert {row["status"] for row in report["scenarios"]} == {"unknown"}


def test_native_argument_key_order_does_not_change_operation_identity(
    tmp_path, runtime
):
    runtime[2]["reordered_arguments"] = True
    _, report = evaluate(tmp_path, runtime)
    assert {row["status"] for row in report["scenarios"]} == {"pass"}


def test_changed_client_result_cannot_confirm_readback(tmp_path, runtime):
    runtime[2]["bad_result"] = True
    _, report = evaluate(tmp_path, runtime)
    assert all(
        row["status"] in {"unknown", "blocked"}
        for row in report["scenarios"]
        if row["tool"] in matrix.READS | {"create_jira_issue"}
    )


def test_foreign_target_is_refused_before_admission(tmp_path, runtime):
    plans = deepcopy(runtime[0])
    plans["codex"]["cells"]["get_jira_issue"]["arguments"]["cloudId"] = "foreign"
    with pytest.raises(ValueError, match="sentinel"):
        native.NativeMatrix(
            plans,
            runtime[1],
            gateway_url="https://gateway.example.test",
            gateway_revision=SOURCE,
            client_factory=runtime[3],
        )
    assert runtime[2]["admissions"] == []


@pytest.mark.parametrize(
    "field", ["operation_ref", "request_id", "receipt_id", "execution_id", "result"]
)
def test_actual_receipt_correlation_cannot_be_substituted(tmp_path, runtime, field):
    runtime[2]["receipt_tamper"] = field
    _, report = evaluate(tmp_path, runtime)
    assert {row["status"] for row in report["scenarios"]} == {"unknown", "blocked"}
    assert all(
        row["status"] == "pending"
        for fixture in runtime[1].values()
        for row in fixture["ledger"].state()["operations"].values()
    )


@pytest.mark.parametrize("change", ["missing", "duplicate", "request", "unconfirmed"])
def test_transport_journal_must_confirm_exactly_the_original_request(
    tmp_path, runtime, change
):
    runtime[2]["journal_tamper"] = change
    _, report = evaluate(tmp_path, runtime)
    assert {row["status"] for row in report["scenarios"]} == {"unknown", "blocked"}


def test_cross_client_operation_and_marker_reuse_is_refused(runtime):
    plans, fixtures, state, connection = runtime
    plans["codex"]["cells"]["get_jira_issue"]["expected"]["operation_id"] = plans[
        "claude-code"
    ]["cells"]["get_jira_issue"]["expected"]["operation_id"]
    with pytest.raises(ValueError, match="globally distinct"):
        native.NativeMatrix(
            plans,
            fixtures,
            gateway_url="https://gateway.example.test",
            gateway_revision=SOURCE,
            client_factory=connection,
        )
    assert not state["admissions"]


@pytest.fixture
def protected_plans(runtime, monkeypatch):
    from tests.e2e.tools import atlassian_client_binding as binding

    plans, fixtures, _, _ = runtime
    raw = deepcopy(plans)
    for client, plan in raw.items():
        plan.pop("approval_file")
        plan.pop("login_file")
        plan["approval"] = {
            "version": 1,
            "run_id": str(uuid4()),
            "tenant_id": "tenant",
            "approval_revision": str(uuid4()),
            "deployment_id": "offline",
            "gateway_origin": "https://gateway.example.test",
            "source_revision": SOURCE,
            "definition_revision": SOURCE,
            "github_run_id": "42",
            "github_run_attempt": "1",
        }
    for key, value in {
        "ADP_SOURCE_REVISION": SOURCE,
        "ADP_DEFINITION_REVISION": SOURCE,
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "ADP_TOOLS_SMOKE_GATEWAY_URL": "https://gateway.example.test",
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
    }.items():
        monkeypatch.setenv(key, value)
    root = fixtures["codex"]["ledger"].path.parent.parent
    tokens = {
        client: "fixture." + client.replace("-", "") + ".signature"
        for client in matrix.CLIENTS
    }
    return binding, root, raw, tokens, fixtures


def test_protected_native_plan_retains_private_separate_original_authority(
    protected_plans,
):
    binding, root, raw, tokens, fixtures = protected_plans
    path = binding.bind_native_plans(
        root, json.dumps(raw), json.dumps(tokens), fixtures
    )
    plans = native.load_native_plans(path)
    assert set(plans) == matrix.CLIENTS
    for client, plan in plans.items():
        assert Path(plan["login_file"]).read_text() == tokens[client]
        assert Path(plan["login_file"]).stat().st_mode & 0o077 == 0
        assert Path(plan["approval_file"]).stat().st_mode & 0o077 == 0
        assert tokens[client] not in path.read_text()
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "fault",
    [
        "same_login",
        "same_run",
        "foreign_revision",
        "extra_field",
        "missing_cell",
        "foreign_site",
    ],
)
def test_protected_native_plan_rejects_incomplete_or_foreign_authority(
    protected_plans, fault
):
    binding, root, raw, tokens, fixtures = protected_plans
    if fault == "same_login":
        tokens["codex"] = tokens["claude-code"]
    elif fault == "same_run":
        raw["codex"]["approval"]["run_id"] = raw["claude-code"]["approval"]["run_id"]
    elif fault == "foreign_revision":
        raw["codex"]["approval"]["source_revision"] = "b" * 40
    elif fault == "extra_field":
        raw["codex"]["command"] = ["untrusted"]
    elif fault == "missing_cell":
        del raw["codex"]["cells"]["get_jira_issue"]
    else:
        raw["codex"]["cells"]["get_jira_issue"]["arguments"]["cloudId"] = "foreign-site"
    with pytest.raises(ValueError):
        binding.bind_native_plans(root, json.dumps(raw), json.dumps(tokens), fixtures)
    assert not (root / "native-plans.json").exists()
