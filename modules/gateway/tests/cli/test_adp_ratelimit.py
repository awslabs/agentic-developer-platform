"""CLI validation and lost acknowledgements preserve target and revision."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("ratelimit_cli", Path(__file__).parents[2] / "cli/adp-ratelimit.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
REV = "2026-09-25T12:00:00+00:00"


def args(action, *flags):
    return cli.parser().parse_args(["admin", "ratelimit", action, "--org", "tenant", "--scope", "team", "--target", "team-a", *flags])


def row():
    return dict(org_id="tenant", entity_type="team", entity_id="team-a", rpm=20, tpm=200, concurrent_requests=2, updated_at=REV)


def snapshot(saved=None):
    return dict(
        org_id="tenant",
        entity_type="team",
        entity_id="team-a",
        requested_target="team-a",
        canonical_user_id=None,
        saved=saved,
        runtime={"tpm": "unavailable_actual_usage_not_reconciled"},
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(cli.common, "ensure_can_mutate", Mock())
    return Mock()


@pytest.mark.parametrize("value", ["0", "-1", "2147483648", "1.5", "nan", "01"])
def test_bad_integer(value):
    with pytest.raises(cli.common.CliError):
        cli.limit(value)


def test_dry_run_clear_does_not_write(client):
    client.request.return_value = snapshot(row())
    result = cli.execute(args("set", "--rpm", "unset", "--dry-run"), client)
    assert result["status"] == "dry_run"
    assert result["detail"]["changes"] == {"rpm": None}
    assert client.request.call_count == 1


def test_create_readback_uses_server_schema(client):
    from src.admin.ratelimit_cli import RateLimitPatch

    client.request.side_effect = [snapshot(), row(), snapshot(row())]
    result = cli.execute(args("set", "--rpm", "20", "--tpm", "200", "--concurrent-requests", "2", "--expect-absent", "--yes"), client)
    assert result["status"] == "ok"
    body = client.request.call_args_list[1].args[2]
    assert RateLimitPatch.model_validate(body).rpm == 20
    assert [c.args[0] for c in client.request.call_args_list] == ["GET", "PUT", "GET"]


def test_lost_response_is_pending_not_replayed(client):
    client.request.side_effect = [snapshot(row()), OSError("lost"), snapshot(row())]
    result = cli.execute(args("set", "--rpm", "21", "--expected-revision", REV, "--yes"), client)
    assert result["status"] == "pending"
    assert [c.args[0] for c in client.request.call_args_list] == ["GET", "PUT", "GET"]


@pytest.mark.parametrize("field,value", [("org_id", "foreign"), ("entity_id", "foreign"), ("runtime", None), ("saved", {})])
def test_malformed_read_refused(client, field, value):
    value_row = snapshot(row())
    value_row[field] = value
    client.request.return_value = value_row
    with pytest.raises(cli.common.CliError):
        cli.execute(args("show"), client)


def test_yes_cannot_approve_unseen_revision(client):
    client.request.return_value = snapshot(row())
    with pytest.raises(cli.common.CliError):
        cli.execute(args("set", "--rpm", "21", "--yes"), client)
    assert client.request.call_count == 1


@pytest.mark.parametrize("line", [{}, {"entity_type": "user", "entity_id": "u", "saved": None, "effective": {}, "sources": {}}])
def test_malformed_own_lines_refused(line):
    value = {
        "org_id": "tenant",
        "lines": [line],
        "runtime": {
            "state": "configured_not_probed",
            "tpm": "unavailable_actual_usage_not_reconciled",
            "worker_convergence": "unknown",
            "backend": "InMemoryBackend",
            "quota_storage": "process_local",
            "defaults": {account: {"rpm": 60, "tpm": 100000, "concurrent_requests": 10} for account in ("human", "service")},
        },
    }
    with pytest.raises(cli.common.CliError):
        cli.own_response(value)


def test_user_mapping_requires_requested_target(client):
    value = snapshot(row())
    value.update(entity_type="user", entity_id="login-sub", canonical_user_id="canonical", requested_target="different", saved=None)
    client.request.return_value = value
    arguments = cli.parser().parse_args(["admin", "ratelimit", "show", "--org", "tenant", "--scope", "user", "--target", "canonical"])
    with pytest.raises(cli.common.CliError):
        cli.execute(arguments, client)
