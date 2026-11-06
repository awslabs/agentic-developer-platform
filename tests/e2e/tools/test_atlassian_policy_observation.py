"""Exercise real grant/read/probe code with controlled external Identity and MCP."""

import json
from copy import deepcopy

import pytest

from tests.e2e.tools import atlassian_policy_observation as policy
from tests.e2e.tools import atlassian_readback_grant as readback
from tests.e2e.tools.test_atlassian_readback_grant import (
    MARKER,
    configuration,
    provider,
)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    external = provider.__wrapped__(monkeypatch)
    original = readback.ReadMCPTransport.send
    state = {"reply": None, "unavailable": False, "calls": [], "now": 1000}

    def send(self, method, params, token):
        if method == "tools/call" and params["name"] in readback.TOOLS.values():
            state["calls"].append(deepcopy(params))
            if state["unavailable"]:
                raise TimeoutError("private provider exception access-owner-one")
            if state["reply"] is not None:
                return deepcopy(state["reply"])
        return original(self, method, params, token)

    monkeypatch.setattr(readback.ReadMCPTransport, "send", send)
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    grant = readback.IdentityReadbackGrant(configuration("owner-one"), "owner-one")
    path = "/ex/jira/fixture-site/rest/api/3/issue/101"
    expected = policy.digest(
        {"id": "101", "fields": {"summary": MARKER, "description": None}}
    )
    binding = policy.binding_for(
        grant,
        run_id="run-one",
        tenant_id="tenant-one",
        site_id="fixture-site",
        client_sha256="a" * 64,
        configuration_sha256="b" * 64,
        permission="read",
        path=path,
        expected_result_sha256=expected,
    )
    ledger = policy.PolicyObservationLedger(
        root / "policy.json", binding, clock=lambda: state["now"]
    )
    return grant, ledger, path, expected, state, external


def denied(permission="read"):
    return {
        "result": {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": f"Access denied: Your organization admin has not authorized the {permission} permission.",
                }
            ],
        }
    }


def probe(runtime, **kwargs):
    grant, ledger, path, expected, _, _ = runtime
    return policy.observe_read_policy(
        grant,
        ledger,
        path=kwargs.get("path", path),
        expected_result_sha256=kwargs.get("expected", expected),
    )


def test_actual_denial_then_verified_read_survives_restart_without_private_content(
    runtime,
):
    grant, ledger, path, expected, state, external = runtime
    state["reply"] = denied()
    first = probe(runtime)
    assert (
        first["status"] == "policy_denied" and not first["policy_transition_verified"]
    )
    assert external["sdk"] and len(state["calls"]) == 1
    reopened = policy.PolicyObservationLedger(
        ledger.path, ledger.binding, clock=lambda: state["now"]
    )
    state.update(reply=None, now=1001)
    final = policy.observe_read_policy(
        grant, reopened, path=path, expected_result_sha256=expected
    )
    assert final["status"] == "allowed" and final["policy_transition_verified"]
    assert final["allow_sha256"] and final["denial_sha256"]
    assert (
        state["calls"]
        == [
            {
                "name": "getJiraIssue",
                "arguments": {"cloudId": "fixture-site", "id": "101"},
            }
        ]
        * 2
    )
    stored = ledger.path.read_text()
    assert "access-owner-one" not in stored and MARKER not in stored
    assert "actor" not in final and "policy_approval" not in final
    assert ledger.path.stat().st_mode & 0o077 == 0


def test_success_alone_does_not_invent_admin_change(runtime):
    result = probe(runtime)
    assert result["status"] == "allowed" and not result["policy_transition_verified"]
    assert result["denial_sha256"] is None


@pytest.mark.parametrize(
    "reply",
    [
        {"error": {"code": 403, "message": "Forbidden"}},
        {
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "provider unavailable"}],
            }
        },
        {"result": {"isError": False, "content": denied()["result"]["content"]}},
        denied("write"),
        {"status": "ready", "policy_approval": True},
        {"result": {**denied()["result"], "structuredContent": {"status": "allowed"}}},
    ],
)
def test_unqualified_response_cannot_supply_policy_denial_or_approval(runtime, reply):
    runtime[4]["reply"] = reply
    result = probe(runtime)
    assert result["status"] == "unknown" and not result["policy_transition_verified"]
    runtime[4]["reply"] = None
    result = probe(runtime)
    assert result["status"] == "allowed" and not result["policy_transition_verified"]


def test_standard_jsonrpc_documented_denial_is_recognized(runtime):
    runtime[4]["reply"] = {
        "error": {"code": -32000, "message": denied()["result"]["content"][0]["text"]}
    }
    assert probe(runtime)["status"] == "policy_denied"


def test_changed_fixture_result_never_confirms_permission_transition(runtime):
    state = runtime[4]
    state["reply"] = denied()
    probe(runtime)
    state["reply"] = {
        "result": {
            "structuredContent": {"id": "foreign", "fields": {"summary": MARKER}}
        }
    }
    result = probe(runtime)
    assert result["status"] == "unknown" and not result["policy_transition_verified"]


def test_current_denial_supersedes_previous_allow(runtime):
    state = runtime[4]
    state["reply"] = denied()
    probe(runtime)
    state["reply"] = None
    assert probe(runtime)["policy_transition_verified"]
    state["reply"] = denied()
    result = probe(runtime)
    assert (
        result["status"] == "policy_denied" and not result["policy_transition_verified"]
    )


@pytest.mark.parametrize("advance", [-1, 301])
def test_future_or_stale_success_cannot_be_accepted(runtime, advance):
    runtime[4]["reply"] = denied()
    probe(runtime)
    runtime[4]["reply"] = None
    assert probe(runtime)["policy_transition_verified"]
    runtime[4]["now"] += advance
    evidence = runtime[1].evidence()
    assert (
        evidence["status"] == "unknown" and not evidence["policy_transition_verified"]
    )


@pytest.mark.parametrize("field", sorted(policy.BINDING_FIELDS))
def test_changed_binding_cannot_read_or_extend_original_history(runtime, field):
    probe(runtime)
    ledger = runtime[1]
    changed = dict(ledger.binding)
    changed[field] = (
        "c" * 64
        if field.endswith("_sha256")
        else "search"
        if field == "permission"
        else "other"
    )
    other = policy.PolicyObservationLedger(ledger.path, changed)
    with pytest.raises(ValueError, match="binding"):
        other.evidence()


@pytest.mark.parametrize("change", ["path", "expected", "grant"])
def test_changed_probe_or_grant_rejected_before_dispatch(runtime, change):
    if change == "grant":
        runtime[0].config["subject_schema_sha256"] = "f" * 64
    kwargs = {"path": runtime[2].replace("101", "102")} if change == "path" else {}
    if change == "expected":
        kwargs["expected"] = "f" * 64
    with pytest.raises(ValueError, match="binding"):
        probe(runtime, **kwargs)
    assert runtime[4]["calls"] == [] and runtime[5]["sdk"] == []


def test_foreign_authenticated_subject_has_no_policy_tool_dispatch(runtime):
    runtime[5]["subject"] = "foreign-owner"
    assert probe(runtime)["status"] == "unknown"
    assert runtime[4]["calls"] == []


def test_search_cannot_establish_read_permission(runtime):
    grant, ledger, _, _, state, _ = runtime
    path = "/ex/jira/fixture-site/rest/api/3/search/jql?jql=project%3DTEST&maxResults=1"
    expected = policy.digest({"issues": [], "isLast": True})
    binding = policy.binding_for(
        grant,
        run_id="run-one",
        tenant_id="tenant-one",
        site_id="fixture-site",
        client_sha256="a" * 64,
        configuration_sha256="b" * 64,
        permission="read",
        path=path,
        expected_result_sha256=expected,
    )
    ledger = policy.PolicyObservationLedger(ledger.path, binding)
    assert (
        policy.observe_read_policy(
            grant, ledger, path=path, expected_result_sha256=expected
        )["status"]
        == "unknown"
    )
    assert state["calls"] == []


def test_interrupted_read_is_retained_as_unknown_before_fresh_read(runtime):
    ledger = runtime[1]
    original = ledger._begin()
    result = probe(runtime)
    assert result["status"] == "allowed" and not result["policy_transition_verified"]
    events = json.loads(ledger.path.read_text())["events"]
    assert events[0]["id"] == original and events[0]["status"] == "unavailable"
    assert events[0]["response_sha256"] is None
    assert events[1]["status"] == "allowed"


def test_network_failure_cannot_reuse_last_success(runtime):
    assert probe(runtime)["status"] == "allowed"
    runtime[4]["unavailable"] = True
    assert probe(runtime)["status"] == "unknown"
    assert "private provider exception" not in runtime[1].path.read_text()


def test_probe_count_is_bounded_before_more_provider_calls(runtime):
    for _ in range(policy.MAX_EVENTS):
        probe(runtime)
    with pytest.raises(ValueError, match="allowance"):
        probe(runtime)
    assert len(runtime[4]["calls"]) == policy.MAX_EVENTS


@pytest.mark.parametrize("unsafe", ["public", "symlink", "duplicate-json"])
def test_unsafe_or_ambiguous_history_is_rejected(runtime, unsafe):
    probe(runtime)
    path = runtime[1].path
    if unsafe == "public":
        path.chmod(0o644)
    elif unsafe == "symlink":
        actual = path.with_name("actual.json")
        path.rename(actual)
        path.symlink_to(actual)
    else:
        path.write_text('{"version":1,"version":1}')
    with pytest.raises((ValueError, OSError)):
        runtime[1].evidence()
