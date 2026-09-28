"""Non-live cleanup interruption, ownership and atomic deletion contracts."""

from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration.fixtures import (
    FixtureRequest,
    RetainedAudit,
    cleanup,
    provision,
)
from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.scenarios import cleanup as native
from tests.e2e.orchestration.scenarios.http import Unsupported
from tests.e2e.orchestration.test_fixtures import FakeProvider

QID = "q-cleanup0123456789"
BRANCH = "qualification/" + QID + "/story-1"


def test_already_absent_branch_is_idempotent_without_credential(valid_config):
    client = Mock(config=valid_config)
    client.request.return_value = (404, {})
    native.delete_branch(client, BRANCH, "a" * 40)
    assert client.request.call_count == 1


def test_changed_branch_never_reaches_git(valid_config, monkeypatch):
    client = Mock(config=valid_config)
    client.request.return_value = (200, {"object": {"sha": "b" * 40}})
    run = Mock()
    monkeypatch.setattr(native.subprocess, "run", run)
    with pytest.raises(Unsupported, match="head changed"):
        native.delete_branch(client, BRANCH, "a" * 40)
    run.assert_not_called()


def test_atomic_branch_delete_uses_expected_sha_and_no_argv_credential(
    valid_config, monkeypatch
):
    config = replace(valid_config, secret_refs={"github": "env:Q2_TEST_TOKEN"})
    client = Mock(config=config)
    client.request.side_effect = [(200, {"object": {"sha": "a" * 40}}), (404, {})]
    run = Mock(return_value=NS(returncode=0))
    monkeypatch.setattr(native.subprocess, "run", run)
    monkeypatch.setattr(native, "resolve_secret_ref", lambda _: "offline-secret")
    native.delete_branch(client, BRANCH, "a" * 40)
    command = run.call_args.args[0]
    assert f"--force-with-lease=refs/heads/{BRANCH}:{'a' * 40}" in command
    assert command[-1] == ":refs/heads/" + BRANCH
    assert "offline-secret" not in str(command)
    assert run.call_args.kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert client.request.call_count == 2


def test_retained_inert_audit_is_persistent_and_not_deleted_again(valid_config):
    inventory = Inventory.create(
        valid_config.artifact_directory, QID, valid_config.environment
    )
    provider = FakeProvider("qualification-flow")
    provision(
        inventory,
        valid_config,
        provider,
        FixtureRequest("flow", provider.kind, QID + "/flow"),
    )
    provider.delete = Mock(return_value=RetainedAudit("verified inert audit"))
    result = cleanup(inventory, valid_config, {provider.kind: provider})
    assert result.clean and result.deleted == () and result.retained
    assert inventory.get("flow").state == "retained"
    assert cleanup(inventory, valid_config, {provider.kind: provider}).clean
    provider.delete.assert_called_once()


def test_namespace_cleanup_waits_for_children(valid_config):
    valid_config = replace(
        valid_config, bounds={**valid_config.bounds, "max_resources": 6}
    )
    inventory = Inventory.create(
        valid_config.artifact_directory, QID, valid_config.environment
    )
    providers = {}
    for fixture, kind in (
        ("namespace", "qualification-namespace"),
        ("deployment", "qualification-runtime"),
    ):
        provider = FakeProvider(kind)
        providers[kind] = provider
        provision(
            inventory,
            valid_config,
            provider,
            FixtureRequest(fixture, kind, QID + "/" + fixture),
        )
    providers["qualification-runtime"].fail_delete = True
    result = cleanup(inventory, valid_config, providers)
    assert not result.clean
    assert providers["qualification-namespace"].delete_calls == []
    providers["qualification-runtime"].fail_delete = False
    assert cleanup(inventory, valid_config, providers).clean


@pytest.mark.parametrize("attack", ["active", "incomplete", "foreign", "live-worker"])
def test_terminal_flow_cleanup_requires_complete_owned_exited_history(
    valid_config, attack
):
    graph = {
        "slug": QID,
        "nodes": [
            {
                "kind": "story",
                "state": "passed",
                "execution_history": {
                    "history_complete": True,
                    "runs": [{"invocation_id": "owned"}],
                },
            }
        ],
    }
    invocation = {"repo": valid_config.repository, "liveness": "exited"}
    if attack == "active":
        graph["nodes"][0]["state"] = "running"
    elif attack == "incomplete":
        graph["nodes"][0]["execution_history"]["history_complete"] = False
    elif attack == "foreign":
        graph["slug"] = "q-someoneelse0123456789"
    else:
        invocation["liveness"] = "live"
    client = Mock(config=valid_config)
    client.get.side_effect = [graph, invocation]
    with pytest.raises(Unsupported):
        native.terminal_flow(client, "flow", QID)


def test_failed_reconciliation_cannot_disappear_from_cleanup_or_budget(valid_config):
    inventory = Inventory.create(
        valid_config.artifact_directory, QID, valid_config.environment
    )
    provider = FakeProvider("qualification-runtime")
    provision(
        inventory,
        valid_config,
        provider,
        FixtureRequest("deployment", provider.kind, QID + "/deployment"),
    )
    inventory.mark_reconcile_failed("deployment", "lost connection")
    result = cleanup(inventory, valid_config, {provider.kind: provider})
    assert not result.clean and inventory.get("deployment") in inventory.unresolved
    from tests.e2e.orchestration.fixtures import BoundExceededError

    with pytest.raises(BoundExceededError):
        provision(
            inventory,
            valid_config,
            provider,
            FixtureRequest("extra", provider.kind, QID + "/extra"),
        )
