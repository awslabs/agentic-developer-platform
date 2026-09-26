"""Exact cap routing and uncertain acknowledgements never replay writes."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("budget_cli", Path(__file__).parents[2] / "cli/adp-budget.py")
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)
REV = "2026-09-25T12:00:00+00:00"


def args(action, *extra):
    return budget.parser().parse_args(["admin", "budget", action, "--org", "tenant", "--team", "team-a", "--period", "daily", *extra])


def cap(**values):
    return dict(
        org_id="tenant",
        entity_type="team",
        entity_id="team-a",
        period_type="daily",
        budget_amount_usd="1.00",
        enforcement_mode="hard",
        updated_at=REV,
        **values,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(budget.common, "ensure_can_mutate", Mock())
    return Mock()


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "0.001", "100000000.00"])
def test_bad_amount_is_refused(value):
    with pytest.raises(budget.common.CliError):
        budget.amount(value)


@pytest.mark.parametrize("period", ["daily", "weekly", "monthly"])
def test_own_period_echo(client, period):
    client.request.return_value = {"period": {"period_type": period}, "lines": []}
    result = budget.execute(budget.parser().parse_args(["budget", "me", "--period", period]), client)
    assert result["status"] == "ok"
    assert client.request.call_args.args == ("GET", "/me/budget?period_type=" + period)
    client.request.return_value["period"]["period_type"] = "wrong"
    with pytest.raises(budget.common.CliError):
        budget.execute(budget.parser().parse_args(["budget", "me", "--period", period]), client)


def test_missing_cap_is_unavailable_not_zero(client):
    client.request.return_value = None
    result = budget.execute(args("show"), client)
    assert result["status"] == "unavailable"
    assert result["detail"]["configuration"] is None


def test_actual_zero_cap_is_readable(client):
    row = cap()
    row["budget_amount_usd"] = "0.00"
    client.request.return_value = row
    assert budget.execute(args("show"), client)["status"] == "ok"


@pytest.mark.parametrize(
    "field,value",
    [("org_id", "foreign"), ("entity_id", "other"), ("period_type", "monthly"), ("updated_at", "not-a-revision"), ("budget_amount_usd", "NaN")],
)
def test_scope_and_schema_mismatch(client, field, value):
    row = cap()
    row[field] = value
    client.request.return_value = row
    with pytest.raises(budget.common.CliError):
        budget.execute(args("show"), client)


def test_dry_run_reads_only(client):
    client.request.return_value = cap()
    result = budget.execute(args("set", "--amount-usd", "2.00", "--mode", "soft", "--dry-run"), client)
    assert result["status"] == "dry_run"
    assert client.request.call_count == 1
    budget.common.ensure_can_mutate.assert_not_called()


def test_create_and_readback(client):
    client.request.side_effect = [None, cap(), cap()]
    result = budget.execute(args("set", "--amount-usd", "1", "--mode", "hard", "--expect-absent", "--yes"), client)
    assert result["status"] == "ok"
    assert [c.args[0] for c in client.request.call_args_list] == ["GET", "PUT", "GET"]
    assert client.request.call_args_list[1].args[1] == "/admin/organizations/tenant/budget/team/team-a/daily"
    assert client.request.call_args_list[1].args[2]["budget_amount_usd"] == "1.00"


@pytest.mark.parametrize("ack", [None, {}, OSError("lost"), budget.common.CliError("lost", "unknown_mutation_outcome", 4)])
def test_uncertain_ack_does_not_claim_success_or_replay(client, ack):
    client.request.side_effect = [None, ack, cap()]
    result = budget.execute(args("set", "--amount-usd", "1", "--mode", "hard", "--expect-absent", "--yes"), client)
    assert result["status"] == "pending"
    assert result["detail"]["outcome"] == "unknown"
    assert [c.args[0] for c in client.request.call_args_list] == ["GET", "PUT", "GET"]


def test_delete_exact_period_revision_preserves_others(client):
    client.request.side_effect = [cap(), {}, None]
    result = budget.execute(args("delete", "--expected-revision", REV, "--yes"), client)
    assert result["status"] == "ok"
    assert "/team/team-a/daily/revision?expected_revision=" in client.request.call_args_list[1].args[1]


def test_existing_cap_needs_explicit_revision(client):
    client.request.return_value = cap()
    with pytest.raises(budget.common.CliError):
        budget.execute(args("set", "--amount-usd", "1", "--mode", "hard", "--yes"), client)
    assert client.request.call_count == 1


def test_pinned_token(monkeypatch):
    api = Mock()
    token = Mock(return_value="human-token")
    monkeypatch.setattr(budget.common, "Api", Mock(return_value=api))
    monkeypatch.setattr(budget.common, "access_token", token)
    client = budget.Client()
    client.request("GET", "/one")
    client.request("GET", "/two")
    token.assert_called_once()
    assert all(call.kwargs == {"token": "human-token", "timeout": 30} for call in api.request.call_args_list)


def test_personal_and_cloud_ledgers_require_selection():
    parser = budget.parser()
    words = ["admin", "budget", "show", "--org", "tenant", "--user", "person", "--period", "daily"]
    with pytest.raises(budget.common.CliError):
        budget.target(parser.parse_args(words))
    assert budget.target(parser.parse_args([*words, "--usage", "personal"])) == ("user", "person")
    assert budget.target(parser.parse_args([*words, "--usage", "cloud-agents"])) == ("root_user", "person")


@pytest.mark.parametrize("money", ["NaN", 0.0, None])
def test_status_refuses_malformed_money(client, money):
    client.request.side_effect = [
        cap(),
        {
            "period_type": "daily",
            "budget_amount_usd": money,
            "current_spend_usd": "0.123456",
            "remaining_budget_usd": "0.876544",
            "period_start": "2026-09-25",
            "period_end": "2026-09-25",
            "enforcement_mode": "hard",
        },
    ]
    with pytest.raises(budget.common.CliError):
        budget.execute(args("status"), client)


def test_status_preserves_subcent_spend_and_negative_remaining(client):
    client.request.side_effect = [
        cap(),
        {
            "period_type": "daily",
            "budget_amount_usd": "1.00",
            "current_spend_usd": "1.123456",
            "remaining_budget_usd": "-0.123456",
            "period_start": "2026-09-25",
            "period_end": "2026-09-25",
            "enforcement_mode": "soft",
        },
    ]
    result = budget.execute(args("status"), client)
    assert result["detail"]["remaining_budget_usd"] == "-0.123456"
    assert result["detail"]["selection"]["canonical_entity_id"] == "team-a"


@pytest.mark.parametrize("kind,usage", [("user", "personal"), ("root_user", "cloud-agents")])
@pytest.mark.parametrize("mismatch", ["ack", "readback", "create_readback"])
def test_person_alias_canonical_identity_cannot_change_during_write(client, kind, usage, mismatch):
    row = cap()
    row.update(entity_type=kind, entity_id="canonical-human")
    other = {**row, "entity_id": "different-human"}
    before = None if mismatch == "create_readback" else row
    client.request.side_effect = [before, other if mismatch == "ack" else row, other if "readback" in mismatch else row]
    precondition = ["--expect-absent"] if before is None else ["--expected-revision", REV]
    selected = budget.parser().parse_args(
        [
            "admin",
            "budget",
            "set",
            "--org",
            "tenant",
            "--user",
            "login-alias",
            "--usage",
            usage,
            "--period",
            "daily",
            "--amount-usd",
            "1.00",
            "--mode",
            "hard",
            "--yes",
            *precondition,
        ]
    )
    result = budget.execute(selected, client)
    assert result["status"] == "pending"
    assert result["detail"]["outcome"] == "unknown"
    assert result["detail"]["selection"]["canonical_entity_id"] == "canonical-human"
    assert [call.args[0] for call in client.request.call_args_list] == ["GET", "PUT", "GET"]


@pytest.mark.parametrize("usage", ["personal", "cloud-agents"])
def test_person_alias_allows_consistent_canonical_write(client, usage):
    row = cap()
    row.update(entity_type="user" if usage == "personal" else "root_user", entity_id="canonical-human")
    client.request.side_effect = [None, row, row]
    selected = budget.parser().parse_args(
        [
            "admin",
            "budget",
            "set",
            "--org",
            "tenant",
            "--user",
            "login-alias",
            "--usage",
            usage,
            "--period",
            "daily",
            "--amount-usd",
            "1.00",
            "--mode",
            "hard",
            "--yes",
            "--expect-absent",
        ]
    )
    result = budget.execute(selected, client)
    assert result["status"] == "ok"
    assert result["detail"]["selection"]["canonical_entity_id"] == "canonical-human"


@pytest.mark.parametrize("ack", [None, {"unexpected": "body"}, []])
def test_malformed_delete_ack_remains_unknown_even_when_cap_is_absent(client, ack):
    client.request.side_effect = [cap(), ack, None]
    result = budget.execute(args("delete", "--expected-revision", REV, "--yes"), client)
    assert result["status"] == "pending"
    assert result["detail"]["outcome"] == "unknown"
    assert [call.args[0] for call in client.request.call_args_list] == ["GET", "DELETE", "GET"]
