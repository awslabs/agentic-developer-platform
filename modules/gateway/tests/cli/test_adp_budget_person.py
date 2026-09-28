"""Person-limit authority, targeting and uncertain acknowledgements."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("person_budget_cli", Path(__file__).parents[2] / "cli/adp-budget.py")
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)
REV = "2026-09-26T12:00:00+00:00"


def args(action="show", family="person-cap", *extra):
    target = ["--person", "github:123"] if family == "person-cap" else ["--scope", "team:org:team"]
    return budget.parser().parse_args(["admin", "budget", family, action, *target, *extra])


def cap(default=False, absent=False):
    identity = (
        dict(scope_type="team", scope_id_org="org", scope_id_team="team")
        if default
        else dict(person_anchor="github:123", source=None if absent else "admin")
    )
    return dict(
        **identity,
        period_type="monthly",
        cap_usd=None if absent else "1.00",
        cap_status="uncapped" if absent else "capped",
        enforcement_mode=None if absent else "hard",
        updated_at=None if absent else REV,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(budget.common, "ensure_can_mutate", Mock())
    return Mock()


@pytest.mark.parametrize("action", ["set", "delete"])
def test_self_refuses_without_request(client, action):
    extra = ["--amount-usd", "1"] if action == "set" else []
    parsed = budget.parser().parse_args(["budget", "person-cap", action, *extra, "--yes"])
    with pytest.raises(budget.common.CliError, match="admin-governed"):
        budget.execute(parsed, client)
    client.request.assert_not_called()


@pytest.mark.parametrize("family", ["person-cap", "person-default"])
def test_dry_run_overrides_yes(client, family):
    client.request.return_value = cap(default=family == "person-default")
    result = budget.execute(args("set", family, "--amount-usd", "2", "--dry-run", "--yes"), client)
    assert result["status"] == "dry_run"
    assert result["detail"]["expected_revision"] == REV
    assert client.request.call_count == 1
    budget.common.ensure_can_mutate.assert_not_called()


@pytest.mark.parametrize(
    "field,bad",
    [
        ("person_anchor", "github:456"),
        ("period_type", "daily"),
        ("cap_usd", "NaN"),
        ("cap_usd", 1),
        ("updated_at", "bad"),
        ("enforcement_mode", "other"),
        ("source", None),
    ],
)
def test_malformed_target_never_writes(client, field, bad):
    row = cap()
    row[field] = bad
    client.request.return_value = row
    with pytest.raises(budget.common.CliError):
        budget.execute(args("set", "person-cap", "--amount-usd", "2", "--yes", "--expected-revision", REV), client)
    assert client.request.call_count == 1


@pytest.mark.parametrize("family", ["person-cap", "person-default"])
def test_create_absent(client, family):
    row = cap(default=family == "person-default")
    client.request.side_effect = [cap(default=family == "person-default", absent=True), row, row]
    assert budget.execute(args("set", family, "--amount-usd", "1", "--yes", "--expected-revision", "absent"), client)["status"] == "ok"
    method, path, body = client.request.call_args_list[1].args
    assert method == "PUT" and path.endswith("expected_revision=absent")
    assert body == {"budget_amount_usd": "1.00"}


def test_stale_never_writes(client):
    client.request.return_value = cap()
    with pytest.raises(budget.common.CliError, match="changed since review"):
        budget.execute(args("delete", "person-cap", "--yes", "--expected-revision", "old"), client)
    assert client.request.call_count == 1


@pytest.mark.parametrize("ack", [None, {}, {"cap_status": "capped"}])
def test_bad_ack_pending_without_replay(client, ack):
    client.request.side_effect = [cap(absent=True), ack, cap()]
    result = budget.execute(args("set", "person-cap", "--amount-usd", "1", "--yes", "--expected-revision", "absent"), client)
    assert result["status"] == "pending"
    assert [c.args[0] for c in client.request.call_args_list] == ["GET", "PUT", "GET"]


def test_delete_readback(client):
    client.request.side_effect = [cap(), {}, cap(absent=True)]
    assert budget.execute(args("delete", "person-cap", "--yes", "--expected-revision", REV), client)["status"] == "ok"


def test_member_report_does_not_invent_headroom(client):
    row = dict(user_id="user", person_anchor="github:123", spend_usd="1.000000", limit_usd="2.00", limit_status="capped", source="admin")
    client.request.return_value = dict(items=[row], total=1, page=1, page_size=20, has_more=False, period_type="monthly", period_start="2026-09-01")
    parsed = budget.parser().parse_args(["admin", "budget", "member-report", "--org", "org"])
    assert budget.execute(parsed, client)["detail"]["cross_org_headroom"] == "unknown"
    client.request.return_value["items"].append(row)
    with pytest.raises(budget.common.CliError):
        budget.execute(parsed, client)
