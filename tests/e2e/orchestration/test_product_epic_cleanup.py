"""Interrupted fixture cleanup and Product-repository deletion, offline."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration import run as runner
from tests.e2e.orchestration.config import load_config
from tests.e2e.orchestration.fixtures import (
    FixtureError,
    FixtureRequest,
    cleanup,
    provision,
)
from tests.e2e.orchestration.inventory import (
    CREATED,
    DELETED,
    RECONCILE_FAILED,
    Inventory,
)
from tests.e2e.orchestration.scenarios import cleanup as native
from tests.e2e.orchestration.scenarios.providers import IssueProvider
from tests.e2e.orchestration.scenarios.delivery_resources import (
    DeliveryResourceProvider,
    branch_name,
)
from tests.e2e.orchestration.test_fixtures import FakeProvider
from tests.e2e.orchestration.test_product_epic_config import PRODUCT_EPIC
from tests.e2e.orchestration.test_product_epic_wiring import ProviderTransport

QUALIFICATION_ID = "q-0123456789abcdef"


@pytest.fixture
def owned_run(write_config, monkeypatch):
    config = load_config(write_config({"product_epic": PRODUCT_EPIC}))
    provider = FakeProvider("qualification-issue")
    monkeypatch.setattr(
        runner,
        "verified_target",
        lambda _: SimpleNamespace(verified=True, to_json=dict),
    )
    monkeypatch.setattr(runner, "load_providers", lambda _: {provider.kind: provider})
    inventory = Inventory.create(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    request = FixtureRequest("story-1", provider.kind, QUALIFICATION_ID + "/story-1")
    return config, inventory, provider, request


def test_cleanup_adopts_lost_issue_create_and_verifies_provider_ownership(owned_run):
    config, inventory, provider, request = owned_run
    provider.fail_create_after_resource_exists = True
    with pytest.raises(FixtureError, match="retained for resume"):
        provision(inventory, config, provider, request)
    inventory.start_attempt("product-epic-delivery")
    outcome = runner.cleanup_qualification(config, QUALIFICATION_ID)
    assert outcome.status == runner.STATUS_PASS
    assert outcome.report["reconciled"] == ["story-1"]
    assert outcome.report["attempt_history"] == [
        {"scenario": "product-epic-delivery", "status": "started"}
    ]
    assert outcome.report["unresolved_inventory"] == []
    assert provider.create_calls == [request.intended_identity]
    assert provider.delete_calls == ["qualification-issue-1"]
    assert provider.resources == {}
    assert (
        Inventory.load(config.artifact_directory, QUALIFICATION_ID, config.environment)
        .get("story-1")
        .state
        == DELETED
    )
    assert (
        runner.cleanup_qualification(config, QUALIFICATION_ID).report["deleted"] == []
    )
    assert provider.delete_calls == ["qualification-issue-1"]


def test_cleanup_after_failed_report_retains_attempt_and_deletes_owned_fixture(
    owned_run,
):
    config, inventory, provider, request = owned_run
    provision(inventory, config, provider, request)
    inventory.start_attempt("product-epic-delivery")
    inventory.finish_attempt("failed")
    outcome = runner.cleanup_qualification(config, QUALIFICATION_ID)
    assert outcome.status == runner.STATUS_PASS
    assert outcome.report["deleted"] == ["story-1"]
    assert outcome.report["attempt_history"] == [
        {"scenario": "product-epic-delivery", "status": "failed"}
    ]
    assert outcome.report["unresolved_inventory"] == []
    assert not provider.resources


def test_product_issue_cleanup_recovers_lost_publication_and_verifies_closure(
    write_config, monkeypatch
):
    config = load_config(write_config({"product_epic": PRODUCT_EPIC}))
    transport = ProviderTransport(config)
    provider = IssueProvider(transport)
    inventory = Inventory.create(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    request = FixtureRequest("story-1", provider.kind, QUALIFICATION_ID + "/story-1")
    original_request = transport.request
    original_get = transport.get
    readbacks = []

    def request_transport(method, path, **kwargs):
        if method == "POST" and path.endswith("/issues"):
            original_request(method, path, **kwargs)
            raise OSError("lost issue acknowledgement")
        if method == "PATCH" and path.endswith("/issues/10"):
            transport.issues[path]["state"] = "closed"
            return 200, transport.issues[path]
        return original_request(method, path, **kwargs)

    def read_transport(path, **kwargs):
        readbacks.append(path)
        if path.startswith("/orchestration/flows?q="):
            return {"flows": [], "total": 0}
        return original_get(path, **kwargs)

    transport.request = request_transport
    transport.get = read_transport
    with pytest.raises(FixtureError, match="retained for resume"):
        provision(inventory, config, provider, request)
    monkeypatch.setattr(
        runner,
        "verified_target",
        lambda _: SimpleNamespace(verified=True, to_json=dict),
    )
    monkeypatch.setattr(runner, "load_providers", lambda _: {provider.kind: provider})
    result = runner.cleanup_qualification(config, QUALIFICATION_ID)
    issue_path = f"/repos/{config.story_repository(1)}/issues/10"
    assert result.status == runner.STATUS_PASS
    assert result.report["reconciled"] == ["story-1"]
    assert result.report["retained_audit"][0][0] == "story-1"
    assert result.report["unresolved_inventory"] == []
    assert transport.issues[issue_path]["state"] == "closed"
    assert readbacks.count(issue_path) >= 3
    assert (
        sum(
            method == "POST" and path.endswith("/issues")
            for method, path in transport.calls
        )
        == 1
    )


def test_product_branch_cleanup_uses_verified_story_repository_and_readback(
    write_config, monkeypatch
):
    config = load_config(write_config({"product_epic": PRODUCT_EPIC}))
    config = replace(config, secret_refs={"github": "env:Q2_TEST_TOKEN"})
    repository = config.story_repository(2)
    branch = branch_name(QUALIFICATION_ID, 2)
    pr = {
        "number": 10,
        "state": "closed",
        "head": {
            "ref": branch,
            "sha": "a" * 40,
            "repo": {
                "full_name": repository,
                "id": config.product_epic.stories[1].repository_id,
            },
        },
        "base": {
            "repo": {
                "full_name": repository,
                "id": config.product_epic.stories[1].repository_id,
            }
        },
    }
    graph = {
        "slug": QUALIFICATION_ID,
        "nodes": [
            {
                "kind": "story",
                "node_ref": "second",
                "repository_identity": repository,
                "state": "passed",
                "bound_pull_request": {"pr_number": 10, "repo": repository},
                "execution_history": {
                    "history_complete": True,
                    "runs": [{"invocation_id": "owned-worker"}],
                },
            }
        ],
    }
    client = Mock(config=config)

    def read(path, **kwargs):
        if path == "/orchestration/flows/flow-1":
            return graph
        if path == "/me/agent-invocations/owned-worker":
            return {"repo": repository, "liveness": "exited"}
        if path == f"/repos/{repository}/pulls/10":
            return pr
        raise AssertionError("unexpected provider read")

    client.get.side_effect = read
    client.request.side_effect = [
        (200, {"object": {"sha": "a" * 40}}),
        (404, {}),
    ]
    git = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(native.subprocess, "run", git)
    monkeypatch.setattr(native, "resolve_secret_ref", lambda _: "offline-secret")
    provider = DeliveryResourceProvider(client, "qualification-branch")
    descriptor = provider.descriptor(pr, QUALIFICATION_ID, "flow-1")
    inventory = Inventory.create(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    inventory.record_planned(
        fixture_id="story-2-branch",
        kind=provider.kind,
        intended_identity=QUALIFICATION_ID + "/story-2-branch",
        ownership_tags=config.ownership_tags(QUALIFICATION_ID),
    )
    inventory.mark_created("story-2-branch", descriptor)
    result = cleanup(inventory, config, {provider.kind: provider})
    assert result.clean and result.deleted == ("story-2-branch",)
    assert inventory.get("story-2-branch").state == DELETED
    read_path = f"/repos/{repository}/git/ref/heads/{branch}"
    assert client.request.call_count == 2
    assert all(
        call.args[:2] == ("GET", read_path) for call in client.request.call_args_list
    )
    assert git.call_args.args[0][-2] == f"https://github.com/{repository}.git"
    assert "offline-secret" not in str(git.call_args.args[0])


def test_cleanup_keeps_foreign_resource_and_resumes_after_ownership_restored(owned_run):
    config, inventory, provider, request = owned_run
    owned_id = provision(inventory, config, provider, request).observed_resource_id
    other_request = FixtureRequest(
        "story-2", provider.kind, QUALIFICATION_ID + "/story-2"
    )
    foreign_id = provision(
        inventory, config, provider, other_request
    ).observed_resource_id
    provider.resources[foreign_id]["adp:qualification-id"] = "q-foreign12345678"
    inventory.start_attempt("product-epic-delivery")
    inventory.finish_attempt("failed")

    result = runner.cleanup_qualification(config, QUALIFICATION_ID)
    assert result.status == runner.STATUS_INCOMPLETE
    assert result.report["deleted"] == ["story-1"]
    assert result.report["unresolved_inventory"] == [
        {"fixture_id": "story-2", "state": CREATED}
    ]
    assert result.report["attempt_history"] == [
        {"scenario": "product-epic-delivery", "status": "failed"}
    ]
    assert provider.delete_calls == [owned_id]
    assert foreign_id in provider.resources
    assert "ownership tag mismatch" in result.report["refused"][0]["reason"]
    assert (
        runner.cleanup_qualification(config, QUALIFICATION_ID).status
        == runner.STATUS_INCOMPLETE
    )
    assert provider.delete_calls == [owned_id]

    provider.resources[foreign_id] = config.ownership_tags(QUALIFICATION_ID)
    resolved = runner.cleanup_qualification(config, QUALIFICATION_ID)
    assert resolved.status == runner.STATUS_PASS
    assert resolved.report["deleted"] == ["story-2"]
    assert resolved.report["unresolved_inventory"] == []
    assert provider.delete_calls == [owned_id, foreign_id]


def test_real_product_issue_provider_refuses_foreign_tags_before_closure(
    write_config, monkeypatch
):
    config = load_config(write_config({"product_epic": PRODUCT_EPIC}))
    transport = ProviderTransport(config)
    provider = IssueProvider(transport)
    inventory = Inventory.create(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    record = provision(
        inventory,
        config,
        provider,
        FixtureRequest("story-1", provider.kind, QUALIFICATION_ID + "/story-1"),
    )
    issue_path = provider.issue_path(record.observed_resource_id)
    original_body = transport.issues[issue_path]["body"]
    transport.issues[issue_path]["body"] = original_body.replace(
        f'"adp:qualification-id": "{QUALIFICATION_ID}"',
        '"adp:qualification-id": "q-foreign12345678"',
    )
    assert transport.issues[issue_path]["body"] != original_body
    monkeypatch.setattr(
        runner,
        "verified_target",
        lambda _: SimpleNamespace(verified=True, to_json=dict),
    )
    monkeypatch.setattr(runner, "load_providers", lambda _: {provider.kind: provider})
    result = runner.cleanup_qualification(config, QUALIFICATION_ID)
    assert result.status == runner.STATUS_INCOMPLETE
    assert result.report["unresolved_inventory"] == [
        {"fixture_id": "story-1", "state": CREATED}
    ]
    assert result.report["deleted"] == []
    assert "ownership tag mismatch" in result.report["refused"][0]["reason"]
    assert not any(method == "PATCH" for method, _ in transport.calls)
    assert (
        Inventory.load(config.artifact_directory, QUALIFICATION_ID, config.environment)
        .get("story-1")
        .state
        == CREATED
    )


def test_planned_worker_branch_without_verified_pr_stays_incomplete(
    owned_run, monkeypatch
):
    config, inventory, _, _ = owned_run
    client = Mock(config=config)
    client.pages.return_value = []
    provider = DeliveryResourceProvider(client, "qualification-branch")
    monkeypatch.setattr(runner, "load_providers", lambda _: {provider.kind: provider})
    inventory.record_planned(
        fixture_id="story-2-branch",
        kind=provider.kind,
        intended_identity=QUALIFICATION_ID + "/story-2-branch",
        ownership_tags=config.ownership_tags(QUALIFICATION_ID),
    )
    inventory.start_attempt("product-epic-delivery")

    for _ in range(2):
        result = runner.cleanup_qualification(config, QUALIFICATION_ID)
        assert result.status == runner.STATUS_INCOMPLETE
        assert result.report["deleted"] == []
        assert result.report["unresolved_inventory"] == [
            {"fixture_id": "story-2-branch", "state": RECONCILE_FAILED}
        ]
        assert (
            "worker-created PR/branch absent or ambiguous"
            in result.report["refused"][0]["reason"]
        )
        assert result.report["attempt_history"] == [
            {"scenario": "product-epic-delivery", "status": "started"}
        ]
        assert (
            Inventory.load(
                config.artifact_directory, QUALIFICATION_ID, config.environment
            )
            .get("story-2-branch")
            .state
            == RECONCILE_FAILED
        )
    client.pages.assert_called_with(
        f"/repos/{config.story_repository(2)}/pulls?state=all"
    )
    client.request.assert_not_called()
    client.get.assert_not_called()


def test_product_branch_wrong_target_is_refused_before_provider_or_git(
    write_config, monkeypatch
):
    config = load_config(write_config({"product_epic": PRODUCT_EPIC}))
    client = Mock(config=config)
    git = Mock()
    monkeypatch.setattr(native.subprocess, "run", git)
    with pytest.raises(native.Unsupported, match="repository differs"):
        native.delete_branch(
            client,
            branch_name(QUALIFICATION_ID, 2),
            "a" * 40,
            repository=config.story_repository(1),
        )
    client.request.assert_not_called()
    git.assert_not_called()
