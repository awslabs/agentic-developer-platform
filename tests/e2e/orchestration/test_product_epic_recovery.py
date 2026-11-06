"""Offline publication and native-parent recovery through the existing runner."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration import run as runner
from tests.e2e.orchestration.fixtures import FixtureError, FixtureRequest, provision
from tests.e2e.orchestration.inventory import CREATED, RECONCILE_FAILED, Inventory
from tests.e2e.orchestration.scenarios.providers import (
    IssueProvider,
    ensure_native_parent,
)

pytest_plugins = ("tests.e2e.orchestration.test_product_epic_wiring",)

QUALIFICATION_ID = "q-0123456789abcdef"


def setup_recovery(wired, monkeypatch):
    config, transport = wired
    inventory = Inventory.create(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    provider = IssueProvider(transport)
    monkeypatch.setattr(
        runner,
        "verified_target",
        lambda config: type(
            "Target", (), {"verified": True, "to_json": lambda self: {}}
        )(),
    )
    monkeypatch.setattr(
        runner, "load_providers", lambda config: {provider.kind: provider}
    )
    request = FixtureRequest("story-1", provider.kind, f"{QUALIFICATION_ID}/story-1")
    return config, transport, inventory, provider, request


@pytest.mark.parametrize("lost_response", ["issue", "before_link", "after_link"])
def test_resume_adopts_issue_and_reconciles_native_parent_without_duplicate(
    wired, monkeypatch, lost_response
):
    config, transport, inventory, provider, request = setup_recovery(wired, monkeypatch)
    original_request = transport.request
    failed = False

    def dropped_response(method, path, **kwargs):
        nonlocal failed
        target = (
            path.endswith("/issues")
            if lost_response == "issue"
            else path.endswith("/sub_issues")
        )
        if not failed and method == "POST" and target:
            failed = True
            if lost_response == "before_link":
                raise OSError("lost link response")
            original_request(method, path, **kwargs)
            raise OSError("lost provider acknowledgement")
        return original_request(method, path, **kwargs)

    transport.request = dropped_response
    if lost_response == "issue":
        with pytest.raises(FixtureError):
            provision(inventory, config, provider, request)
    else:
        record = provision(inventory, config, provider, request)
        with pytest.raises(OSError):
            ensure_native_parent(inventory, config, provider, record)
    retained = Inventory.load(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    )
    assert len(transport.issues) == 1
    assert retained.get("story-1").state in {"planned", RECONCILE_FAILED}

    outcome = runner.resume_qualification(config, QUALIFICATION_ID)
    assert outcome.status == runner.STATUS_READY, outcome.report
    assert outcome.report["native_parent_verified"] == ["story-1"]
    confirmed = Inventory.load(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    ).get("story-1")
    assert confirmed.state == CREATED
    assert confirmed.native_parent_verified
    assert confirmed.native_link_attempts == (1 if lost_response == "issue" else 2)
    assert len(transport.issues) == 1
    assert (
        transport.parents[
            provider.issue_path(confirmed.observed_resource_id) + "/parent"
        ]
        == 500
    )
    assert (
        sum(
            method == "POST" and path.endswith("/issues")
            for method, path in transport.calls
        )
        == 1
    )
    assert (
        sum(
            method == "POST" and path.endswith("/sub_issues")
            for method, path in transport.calls
        )
        == 1
    )


def test_resume_refuses_foreign_parent_and_preserves_owned_issue(wired, monkeypatch):
    config, transport, inventory, provider, request = setup_recovery(wired, monkeypatch)
    record = provision(inventory, config, provider, request)
    transport.parents[provider.issue_path(record.observed_resource_id) + "/parent"] = (
        999
    )
    outcome = runner.resume_qualification(config, QUALIFICATION_ID)
    assert outcome.status == runner.STATUS_FAILED
    assert outcome.report["reconcile_failed"][0]["fixture_id"] == "story-1"
    assert outcome.report["native_parent_verified"] == []
    assert len(transport.issues) == 1
    assert not any(path.endswith("/sub_issues") for _, path in transport.calls)


def test_exhausted_link_budget_can_verify_lost_ack_but_never_post_again(
    wired, monkeypatch
):
    config, transport, inventory, provider, request = setup_recovery(wired, monkeypatch)
    config = replace(config, bounds={**config.bounds, "max_runs": 1})
    record = provision(inventory, config, provider, request)
    original_request = transport.request

    def lost_ack(method, path, **kwargs):
        result = original_request(method, path, **kwargs)
        if method == "POST" and path.endswith("/sub_issues"):
            raise OSError("lost acknowledgement")
        return result

    transport.request = lost_ack
    with pytest.raises(OSError):
        ensure_native_parent(inventory, config, provider, record)
    assert (
        runner.resume_qualification(config, QUALIFICATION_ID).status
        == runner.STATUS_READY
    )
    assert sum(path.endswith("/sub_issues") for _, path in transport.calls) == 1
    transport.parents.clear()
    inventory.mark_reconcile_failed("story-1", "recheck")
    result = runner.resume_qualification(config, QUALIFICATION_ID)
    assert result.status == runner.STATUS_FAILED
    assert "retry bound" in result.report["reconcile_failed"][0]["detail"]
    assert sum(path.endswith("/sub_issues") for _, path in transport.calls) == 1


def test_resume_does_not_claim_ready_when_issue_is_not_found(wired, monkeypatch):
    config, transport, inventory, provider, request = setup_recovery(wired, monkeypatch)
    inventory.record_planned(
        fixture_id=request.fixture_id,
        kind=request.kind,
        intended_identity=request.intended_identity,
        ownership_tags=config.ownership_tags(QUALIFICATION_ID),
        idempotency_token=f"{QUALIFICATION_ID}-story-1",
    )
    outcome = runner.resume_qualification(config, QUALIFICATION_ID)
    assert outcome.status == runner.STATUS_INCOMPLETE
    assert outcome.report["unresolved"] == ["story-1"]
    assert not transport.issues
    assert not any(method == "POST" for method, _ in transport.calls)


@pytest.mark.parametrize(
    "boundary", ["publication", "dispatch", "review", "report", "evidence"]
)
def test_lifecycle_failure_retains_attempt_for_resume(wired, monkeypatch, boundary):
    config, _, inventory, _, _ = setup_recovery(wired, monkeypatch)
    inventory.path.unlink()
    monkeypatch.setattr(runner, "new_qualification_id", lambda: QUALIFICATION_ID)
    monkeypatch.setattr(
        runner,
        "load_scenario_adapters",
        lambda _: {
            "product-epic-delivery": SimpleNamespace(
                execute=Mock(side_effect=OSError(boundary))
            )
        },
    )
    result = runner.run(config)
    assert result.status == runner.STATUS_FAILED
    assert result.report["attempt_history"] == [
        {"scenario": "product-epic-delivery", "status": "failed"}
    ]
    resumed = runner.resume_qualification(config, QUALIFICATION_ID)
    assert resumed.status == runner.STATUS_READY
    assert resumed.report["attempt_history"] == result.report["attempt_history"]


def test_interruption_keeps_started_attempt(wired, monkeypatch):
    config, _, inventory, _, _ = setup_recovery(wired, monkeypatch)
    inventory.path.unlink()
    monkeypatch.setattr(runner, "new_qualification_id", lambda: QUALIFICATION_ID)
    monkeypatch.setattr(
        runner,
        "load_scenario_adapters",
        lambda _: {
            "product-epic-delivery": SimpleNamespace(
                execute=Mock(side_effect=KeyboardInterrupt())
            )
        },
    )
    with pytest.raises(KeyboardInterrupt):
        runner.run(config)
    assert Inventory.load(
        config.artifact_directory, QUALIFICATION_ID, config.environment
    ).attempts == [{"scenario": "product-epic-delivery", "status": "started"}]
