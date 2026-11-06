"""Direct provider readback and isolated operation reservations; no live acceptance."""

import json
from copy import deepcopy

import pytest

from tests.e2e.tools import atlassian_client_matrix as matrix
from tests.e2e.tools import test_atlassian_mutation_observer as mutation_fixtures


class Identity:
    def with_access_token(self, owner_id, dispatch):
        assert owner_id == "owner-one"
        return dispatch("transient-test-token")


def mutation_fixture(tmp_path, monkeypatch, client, tool):
    ledger, reader, _ = fixture(tmp_path, monkeypatch)
    provider = mutation_fixtures.Provider()

    def get(path, token):
        assert token == "transient-test-token"
        if path == "/oauth/token/accessible-resources":
            return [{"id": "site-one", "scopes": ["read:jira-work"]}]
        if path == "/me":
            return {"account_id": mutation_fixtures.ACCOUNT}
        return provider.get(path)

    monkeypatch.setattr(reader, "_get", get)
    arguments = {**inputs(client, tool, "mutation"), "marker": mutation_fixtures.MARKER}
    change = {
        "update_jira_issue": {"summary": mutation_fixtures.MARKER + " after"},
        "transition_jira_issue": {"status_id": "10001"},
        "comment_jira_issue": {"body": mutation_fixtures.body(mutation_fixtures.MARKER + " a comment")},
    }[tool]
    expected = {"source": "direct-atlassian-provider", "run_id": ledger.run_id, **arguments,
                "account_id": mutation_fixtures.ACCOUNT, "content_sha256": matrix.digest(change), "effect_count": 1}
    return ledger, reader, provider, arguments, change, expected


@pytest.mark.parametrize("client", sorted(matrix.CLIENTS))
@pytest.mark.parametrize("tool", sorted(matrix.WRITES - {"create_jira_issue"}))
def test_mutation_history_is_durable_before_dispatch_and_reused_after_restart(tmp_path, monkeypatch, client, tool):
    ledger, reader, provider, arguments, change, expected = mutation_fixture(tmp_path, monkeypatch, client, tool)
    ledger.reserve(**arguments, expected=expected, observer=reader, mutation_expected=change)
    stored = json.loads(ledger.path.read_text())
    baseline = stored["operations"][arguments["operation_id"]]["mutation_baseline"]
    assert baseline["events"] == {} and provider.rows == []
    assert "transient-test-token" not in ledger.path.read_text()
    restarted = matrix.ClientOperationLedger(ledger.path, run_id=ledger.run_id)
    result, receipt = attest(expected)
    with pytest.raises(ValueError, match="Exactly one new"):
        restarted.confirm(operation_id=arguments["operation_id"], client_result=result, receipt=receipt, observer=reader)
    assert mutation_fixtures.perform(provider, tool) == change
    assert restarted.confirm(operation_id=arguments["operation_id"], client_result=result, receipt=receipt,
                             observer=reader)["effect_count"] == 1
    assert restarted.state()["operations"][arguments["operation_id"]]["mutation_baseline"] == baseline
    assert matrix.observe_cell(restarted, reader, client=client, tool=tool)["effect_count"] == 1
    provider.rows.append({**deepcopy(provider.rows[0]), "id": "201"})
    with pytest.raises(ValueError, match="Exactly one new"):
        matrix.observe_cell(restarted, reader, client=client, tool=tool)


def test_mutation_baseline_rejects_changed_owner_and_torn_journal(tmp_path, monkeypatch):
    ledger, reader, provider, arguments, change, expected = mutation_fixture(tmp_path, monkeypatch, "codex", "update_jira_issue")
    with pytest.raises(ValueError, match="baseline"):
        ledger.reserve(**arguments, expected={**expected, "account_id": "foreign"}, observer=reader, mutation_expected=change)
    assert not ledger.path.exists() and provider.rows == []
    ledger.reserve(**arguments, expected=expected, observer=reader, mutation_expected=change)
    stored = json.loads(ledger.path.read_text())
    stored["operations"][arguments["operation_id"]]["mutation_baseline"]["events"]["foreign"] = {}
    ledger.path.write_text(json.dumps(stored))
    with pytest.raises(ValueError, match="invalid record"):
        ledger.state()


def test_mutation_without_separate_rest_grant_cannot_reserve_or_query_history(tmp_path, monkeypatch):
    ledger, reader, provider, arguments, change, expected = mutation_fixture(tmp_path, monkeypatch, "codex", "update_jira_issue")
    calls = []

    def denied(path, token):
        calls.append(path)
        return [{"id": "site-one", "scopes": ["read:jira:agent-interface"]}]

    monkeypatch.setattr(reader, "_get", denied)
    with pytest.raises(ValueError, match="REST read grant"):
        ledger.reserve(**arguments, expected=expected, observer=reader, mutation_expected=change)
    assert calls == ["/oauth/token/accessible-resources"]
    assert not ledger.path.exists() and provider.rows == []


def test_oversized_mutation_baseline_is_rejected_before_a_reservation_is_returned(tmp_path, monkeypatch):
    ledger, reader, provider, arguments, change, expected = mutation_fixture(tmp_path, monkeypatch, "codex", "update_jira_issue")
    before = reader.mutation_snapshot(tool=arguments["tool"], marker=arguments["marker"], site_id=arguments["site_id"], provider_id="42")
    before["events"] = {"100": {"author_id": mutation_fixtures.ACCOUNT, "from": "x" * (1024 * 1024), "to": provider.summary}}
    before["sha256"] = matrix.digest({key: value for key, value in before.items() if key != "sha256"})
    monkeypatch.setattr(reader, "mutation_snapshot", lambda **kwargs: before)
    with pytest.raises(ValueError, match="storage bound"):
        ledger.reserve(**arguments, expected=expected, observer=reader, mutation_expected=change)
    assert not ledger.path.exists() and provider.rows == []


class Provider:
    content = "before"
    matches = 0
    account = "000000:account-one"
    last_page = True
    rest_scopes = ["read:jira-work", "read:confluence-content.all"]
    creator = None
    generated_key = "UNPREDICTABLE-7"
    generated_timestamp = "server-generated-time"
    calls = None

    def __init__(self):
        self.calls = []

    def __call__(self, host, timeout):
        assert host == "api.atlassian.com" and timeout == 5
        return self

    def request(self, method, path, *, headers):
        assert method == "GET" and headers["Authorization"] == "Bearer transient-test-token"
        self.calls.append(path)

    def getresponse(self):
        class Response:
            status = 200

            def __init__(self, body):
                self.body = body

            def read(self, size):
                return json.dumps(self.body).encode()

        path = self.calls[-1]
        if path == "/oauth/token/accessible-resources":
            body = [{"id": "site-one", "scopes": self.rest_scopes}]
        elif path == "/me":
            body = {"account_id": self.account}
        elif "/search/jql?" in path:
            body = {"issues": [{"id": "42"}] if self.matches else [], "isLast": self.last_page}
        elif path.endswith("/rest/api/3/issue/42"):
            body = {"id": "42", "key": self.generated_key, "updated": self.generated_timestamp,
                    "fields": {"summary": self.content, "description": "fixture-description",
                               "creator": {"accountId": self.creator or self.account}}}
        elif path.endswith("/wiki/api/v2/pages/42?body-format=storage"):
            body = {"id": "42", "title": self.content,
                    "body": {"storage": {"value": "fixture-page-body"}}, "updated": self.generated_timestamp}
        else:
            raise AssertionError(path)
        return Response(body)

    def close(self):
        pass


def fixture(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    provider = Provider()
    monkeypatch.setattr(matrix.http.client, "HTTPSConnection", provider)
    reader = matrix.IndependentProviderReader(Identity(), "owner-one")
    ledger = matrix.ClientOperationLedger(private / "operations.json", run_id="run-one")
    return ledger, reader, provider


def inputs(client, tool, suffix):
    return {"client": client, "tool": tool, "operation_id": "operation-" + suffix,
            "marker": "adp-8454-abcdefghijklmnop" + suffix, "provider_id": "42", "site_id": "site-one"}


def attest(expected):
    result = {key: expected[key] for key in ("provider_id", "site_id", "account_id", "content_sha256")}
    receipt = {"source": "gateway-operation-receipt", "operation_id": expected["operation_id"],
               "client": expected["client"], "status": "committed", "account_id": expected["account_id"],
               "provider_id": expected["provider_id"]}
    return result, receipt


@pytest.mark.parametrize("client,tool", [("claude-code", "get_jira_issue"), ("codex", "get_confluence_page")])
def test_each_client_read_requires_unchanged_direct_provider_sentinel(tmp_path, monkeypatch, client, tool):
    ledger, reader, provider = fixture(tmp_path, monkeypatch)
    arguments = inputs(client, tool, "read")
    expected = reader.readback(run_id="run-one", **arguments)
    ledger.reserve(**arguments, expected=expected, observer=reader)
    restarted = matrix.ClientOperationLedger(ledger.path, run_id="run-one")
    assert restarted.state()["operations"][arguments["operation_id"]]["status"] == "pending"
    result, receipt = attest(expected)
    assert restarted.confirm(operation_id=arguments["operation_id"], client_result=result, receipt=receipt,
                             observer=reader) == {"status": "confirmed", "client": client, "tool": tool,
                                                  "provider_id": "42", "effect_count": 0}
    assert provider.calls and all("transient-test-token" not in path for path in provider.calls)
    assert "/oauth/token/accessible-resources" in provider.calls
    assert "transient-test-token" not in ledger.path.read_text()
    with pytest.raises(ValueError, match="unresolved"):
        restarted.confirm(operation_id=arguments["operation_id"], client_result=result, receipt=receipt, observer=reader)


def test_write_requires_exactly_one_provider_effect_and_admitted_identity(tmp_path, monkeypatch):
    ledger, reader, provider = fixture(tmp_path, monkeypatch)
    arguments = {**inputs("codex", "create_jira_issue", "write"), "provider_id": None}
    expected = {**reader.readback(run_id="run-one", **arguments),
                "content_sha256": matrix.digest({"summary": arguments["marker"] + " after",
                                                 "description": "fixture-description"}),
                "effect_count": 1}
    ledger.reserve(**arguments, expected=expected, observer=reader)
    result, receipt = attest({**expected, "provider_id": "42"})
    with pytest.raises(ValueError, match="provider effect differs"):
        ledger.confirm(operation_id=arguments["operation_id"], client_result=result, receipt=receipt, observer=reader)
    assert ledger.state()["operations"][arguments["operation_id"]]["status"] == "pending"
    provider.content, provider.matches = arguments["marker"] + " after", 1
    provider.generated_key, provider.generated_timestamp = "UNKNOWN-1938", "new-server-time"
    with pytest.raises(ValueError, match="provider effect differs"):
        ledger.confirm(operation_id=arguments["operation_id"], client_result=result,
                       receipt={**receipt, "account_id": "foreign"}, observer=reader)
    assert ledger.confirm(operation_id=arguments["operation_id"], client_result=result, receipt=receipt,
                          observer=reader)["effect_count"] == 1
    assert any("search/jql?" in path for path in provider.calls)


def test_rest_grant_and_provider_author_come_from_provider_not_mcp_scope_claims(tmp_path, monkeypatch):
    ledger, reader, provider = fixture(tmp_path, monkeypatch)
    arguments = {**inputs("codex", "create_jira_issue", "write"), "provider_id": None}
    provider.rest_scopes = ["read:jira:agent-interface"]
    with pytest.raises(ValueError, match="REST read grant"):
        reader.readback(run_id="run-one", **arguments)
    assert provider.calls == ["/oauth/token/accessible-resources"]
    provider.rest_scopes = ["read:jira-work"]
    expected = {**reader.readback(run_id="run-one", **arguments),
                "content_sha256": matrix.digest({"summary": arguments["marker"] + " after",
                                                 "description": "fixture-description"}), "effect_count": 1}
    ledger.reserve(**arguments, expected=expected, observer=reader)
    provider.content, provider.matches = arguments["marker"] + " after", 1
    provider.creator = "foreign-account"
    result, receipt = attest({**expected, "provider_id": "42"})
    with pytest.raises(ValueError, match="owned marker or author"):
        ledger.confirm(operation_id=arguments["operation_id"], client_result=result, receipt=receipt, observer=reader)
    assert ledger.state()["operations"][arguments["operation_id"]]["status"] == "pending"


def test_distinct_clients_and_markers_cannot_share_reservations(tmp_path, monkeypatch):
    ledger, reader, provider = fixture(tmp_path, monkeypatch)
    first = inputs("claude-code", "get_jira_issue", "first")
    expected = reader.readback(run_id="run-one", **first)
    ledger.reserve(**first, expected=expected, observer=reader)
    second = inputs("codex", "get_jira_issue", "second")
    expected_second = reader.readback(run_id="run-one", **second)
    ledger.reserve(**second, expected=expected_second, observer=reader)
    assert len(ledger.state()["operations"]) == 2
    with pytest.raises(ValueError, match="already reserved"):
        duplicate = {**second, "marker": first["marker"]}
        ledger.reserve(**duplicate, expected=reader.readback(run_id="run-one", **duplicate), observer=reader)
    with pytest.raises(ValueError, match="already reserved"):
        ledger.reserve(**first, expected=expected, observer=reader)
    result, receipt = attest(expected)
    with pytest.raises(ValueError, match="provider effect differs"):
        ledger.confirm(operation_id=first["operation_id"], client_result=result,
                       receipt={**receipt, "client": "codex"}, observer=reader)
    assert provider.matches == 0


def test_operation_ledger_rejects_foreign_or_public_state(tmp_path, monkeypatch):
    ledger, reader, _ = fixture(tmp_path, monkeypatch)
    arguments = inputs("codex", "get_jira_issue", "safety")
    ledger.reserve(**arguments, expected=reader.readback(run_id="run-one", **arguments), observer=reader)
    with pytest.raises(ValueError, match="different run"):
        matrix.ClientOperationLedger(ledger.path, run_id="foreign").state()
    ledger.path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        ledger.state()


@pytest.mark.parametrize("tool", ["update_jira_issue", "comment_jira_issue", "transition_jira_issue"])
def test_issue_search_cannot_attest_other_mutations(tmp_path, monkeypatch, tool):
    _, reader, provider = fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="cannot count"):
        reader.readback(run_id="run-one", **inputs("codex", tool, "write"))
    assert provider.calls == []


def test_incomplete_provider_search_and_changed_readback_remain_unverified(tmp_path, monkeypatch):
    ledger, reader, provider = fixture(tmp_path, monkeypatch)
    arguments = inputs("claude-code", "get_jira_issue", "safety")
    expected = reader.readback(run_id="run-one", **arguments)
    ledger.reserve(**arguments, expected=expected, observer=reader)
    provider.content = "changed-by-foreign-owner"
    result, receipt = attest(expected)
    with pytest.raises(ValueError, match="provider effect differs"):
        ledger.confirm(operation_id=arguments["operation_id"], client_result=result, receipt=receipt, observer=reader)
    assert ledger.state()["operations"][arguments["operation_id"]]["status"] == "pending"
    assert ledger.state()["operations"][arguments["operation_id"]]["provider_id"] is None
    provider.last_page = False
    with pytest.raises(ValueError, match="incomplete or ambiguous"):
        reader.readback(run_id="run-one", **inputs("codex", "create_jira_issue", "write"))
