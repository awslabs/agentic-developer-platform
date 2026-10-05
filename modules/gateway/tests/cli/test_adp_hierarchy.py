"""Hierarchy CLI contract and uncertain acknowledgement handling."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("hierarchy_cli", Path(__file__).parents[2] / "cli/adp-hierarchy.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
REV = "a" * 64


def snapshot(kind="team", key="team"):
    return {
        "org_id": "org",
        "kind": kind,
        "id": key,
        "revision": REV,
        "resource": {"id": key, "name": "Old"},
        "dependent_tables": [],
        "delete_permitted": True,
        "cascade_supported": False,
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(cli.common, "ensure_can_mutate", Mock())
    return Mock()


def args(*words):
    return cli.parser().parse_args(words)


def test_create_org_uses_only_canonical_full_bundle_route(client):
    client.request.side_effect = [{"id": "org"}, snapshot("org", "org")]
    result = cli.execute(args("org", "create", "--id", "org", "--name", "Org", "--yes"), client)
    assert result["status"] == "ok"
    assert client.request.call_args_list[0].args == ("POST", "/api/admin/identity/organizations", {"id": "org", "name": "Org"})


def test_dry_run_never_writes_or_checks_mutation_permission(client):
    client.request.return_value = snapshot()
    result = cli.execute(args("team", "update", "--org", "org", "--id", "team", "--name", "New", "--dry-run"), client)
    assert result["status"] == "dry_run"
    assert client.request.call_count == 1
    cli.common.ensure_can_mutate.assert_not_called()


def test_patch_omits_unselected_fields(client):
    updated = snapshot()
    updated["resource"]["name"] = "New"
    client.request.side_effect = [snapshot(), updated, updated]
    result = cli.execute(args("team", "update", "--org", "org", "--id", "team", "--name", "New", "--expected-revision", REV, "--yes"), client)
    assert result["status"] == "ok"
    assert client.request.call_args_list[1].args[2] == {"expected_revision": REV, "patch": {"name": "New"}}


@pytest.mark.parametrize("ack", [None, {}, OSError("lost")])
def test_uncertain_write_remains_pending_no_replay(client, ack):
    client.request.side_effect = [snapshot(), ack, snapshot()]
    result = cli.execute(args("team", "update", "--org", "org", "--id", "team", "--name", "New", "--expected-revision", REV, "--yes"), client)
    assert result["status"] == "pending"
    assert sum(c.args[0] == "PATCH" for c in client.request.call_args_list) == 1


def test_foreign_readback_refused(client):
    row = snapshot()
    row["org_id"] = "foreign"
    client.request.return_value = row
    with pytest.raises(cli.common.CliError):
        cli.execute(args("team", "show", "--org", "org", "--id", "team"), client)


def test_delete_dependencies_refused_without_write(client):
    row = snapshot()
    row["dependent_tables"] = ["users"]
    client.request.return_value = row
    with pytest.raises(cli.common.CliError):
        cli.execute(args("team", "delete", "--org", "org", "--id", "team", "--expected-revision", REV, "--yes"), client)
    assert client.request.call_count == 1


def test_team_member_add_changes_only_one_membership(client):
    row = snapshot("member", "user")
    row["resource"]["teams"] = [{"team_id": "team", "role": "member", "is_primary": False}]
    client.request.side_effect = [row, row, row]
    result = cli.execute(
        args("team", "members", "add", "--org", "org", "--team", "team", "--user", "user", "--expected-revision", REV, "--yes"), client
    )
    assert result["status"] == "ok"
    body = client.request.call_args_list[1].args[2]
    assert body["patch"] == {"team_add": {"team_id": "team", "role": "member", "is_primary": False}}
    assert "/hierarchy/member/user" in client.request.call_args_list[1].args[1]


def test_new_user_creation_explicitly_disables_invite(client):
    client.request.side_effect = [{"id": "user", "org_id": "org"}, snapshot("member", "user")]
    cli.execute(args("member", "add", "--org", "org", "--new-user", "--email", "new@example.test", "--yes"), client)
    body = client.request.call_args_list[0].args[2]
    assert body["send_invite"] is False
    assert not {"password", "temporary_password", "cognito_identity"}.intersection(body)


def test_primary_remove_uses_membership_adapter_never_identity_delete(client):
    row = snapshot("member", "user")
    row["resource"]["membership_status"] = "revoked"
    client.request.side_effect = [row, row, row]
    result = cli.execute(args("member", "remove", "--org", "org", "--user", "user", "--expected-revision", REV, "--yes"), client)
    assert result["status"] == "ok"
    assert "/hierarchy/member/user?" in client.request.call_args_list[1].args[1]
