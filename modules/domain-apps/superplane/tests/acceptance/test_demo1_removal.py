"""Synthetic removal controls; independent provider readers have separate tests."""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_demo1_cleanup_evidence as fixtures
from test_demo1_browser import identity
from superplane_acceptance import demo1_controls, demo1_removal_provider
from superplane_acceptance.demo1_browser import CreationCheckpoint
from superplane_acceptance.demo1_cleanup import PrivateCleanup
from superplane_acceptance.demo1_evidence import EvidenceError
from superplane_acceptance.demo1_removal import PrivateRemoval, advance_removal

driver, prepared, cleanup, observed = (
    fixtures.driver,
    fixtures.prepared,
    fixtures.cleanup,
    fixtures.observed,
)


@pytest.fixture
def removal(observed, monkeypatch):
    report = observed.cleanup.run()
    driver, original = observed.cleanup.driver, observed.cleanup.original
    original = CreationCheckpoint(**vars(original))
    artifact = report["browser"]["cleanup_preparation"]["artifact"]
    now = datetime.now(UTC)
    state = SimpleNamespace(
        approved=False,
        lose_approval=False,
        lose_reply=False,
        sends=0,
        calls=[],
        change=lambda value: value,
        now=now,
    )

    # Only isolate provider-domain validation; its real reader is tested separately.
    def validate(selected, envelope, checkpoint, baseline):
        assert baseline["fixture"] == "independent-provider-baseline"

    monkeypatch.setattr(demo1_removal_provider, "validate_provider_baseline", validate)
    with PrivateCleanup(
        driver.path / "cleanup.json", driver.selected, driver.envelope, original
    ) as cleanup_store:
        preparation = cleanup_store.load()
        with PrivateRemoval(
            driver.path / "removal.json",
            driver.selected,
            driver.envelope,
            original,
            preparation,
        ) as store:
            state.store = store
            baseline = {
                "fixture": "independent-provider-baseline",
                "source_operation_id": preparation.source_operation_id,
                "started_at": now.isoformat(),
            }

            def ticket():
                return state.change(
                    {
                        "approval_id": identity(90),
                        "workspace_id": original.workspace_id,
                        "requester": driver.selected.requester_id,
                        "approvers": [driver.selected.approver_id],
                        "request": {
                            "contract_version": "v1",
                            **{
                                key: value
                                for key, value in observed.review[
                                    "approval_request"
                                ].items()
                                if key != "workspace_id"
                            },
                        },
                        "plan_digest": observed.review["revision"],
                        "expires_at": (now + timedelta(minutes=15)).isoformat(),
                        "result": "allowed-once" if state.approved else "pending",
                        "revoked": False,
                        "decided_by": driver.selected.approver_id
                        if state.approved
                        else None,
                        "decided_at": now.isoformat() if state.approved else None,
                    }
                )

            def request(method, path, body=None):
                state.calls.append((method, path, copy.deepcopy(body)))
                if method == "POST":
                    assert (
                        path.endswith("/operation-approvals")
                        and body == observed.review["approval_request"]
                    )
                    assert store.load().provider_baseline == baseline
                    if state.lose_approval:
                        raise ConnectionError("private lost approval reply")
                return 200, ticket()

            def control(transport, saved, before_send):
                assert not store.load().submitted
                before_send()
                assert store.load().submitted
                state.sends += 1
                if state.lose_reply:
                    raise ConnectionError("private lost removal reply")
                return 200, {
                    "request_id": saved.request_id,
                    "workspace_id": saved.workspace_id,
                    "phase": "retire-workspace",
                    "retirement_complete": False,
                    "retryable": False,
                    "operation_id": identity(91),
                }

            def recover(transport, saved):
                assert saved.submitted and state.sends == 1
                return {
                    "request_id": saved.request_id,
                    "workspace_id": saved.workspace_id,
                    "provisioning_operation_id": identity(91),
                    "state": "succeeded",
                    "phase": "execution",
                }

            monkeypatch.setattr(demo1_controls, "remove_workspace", control)
            monkeypatch.setattr(demo1_controls, "recover_removal", recover)
            transport = SimpleNamespace(request=request)

            def run():
                return advance_removal(
                    driver.selected,
                    driver.envelope,
                    transport,
                    store,
                    clock=lambda: state.now,
                    review=observed.review,
                    artifact=artifact,
                    provider_baseline=baseline,
                    ready_observed_at=now.isoformat(),
                )

            state.driver, state.original, state.preparation = (
                driver,
                original,
                preparation,
            )
            state.release_locks = lambda: (store.__exit__(), cleanup_store.__exit__())
            state.transport = transport
            state.run, state.selected = run, driver.selected
            yield state


def test_pending_approval_submits_once_and_preserves_baseline(removal):
    assert "awaiting independent" in removal.run()["reason"]
    saved = removal.store.load()
    assert saved.approval_id == identity(90) and not saved.submitted
    removal.approved = True
    assert removal.run()["state"] == removal.run()["state"] == "succeeded"
    assert (
        removal.sends == 1
        and sum(method == "POST" for method, _, _ in removal.calls) == 1
    )
    assert removal.store.load().provider_baseline == saved.provider_baseline


def test_lost_approval_reply_keeps_original_request_and_baseline(removal):
    removal.lose_approval = True
    with pytest.raises(EvidenceError):
        removal.run()
    saved = removal.store.load()
    assert saved.approval_id is None and not saved.submitted
    removal.lose_approval = False
    removal.run()
    posts = [body for method, _, body in removal.calls if method == "POST"]
    assert len(posts) == 2 and posts[0] == posts[1]
    assert removal.store.load().provider_baseline == saved.provider_baseline


def test_lost_removal_reply_recovers_without_replay(removal):
    removal.approved = removal.lose_reply = True
    assert "uncertain" in removal.run()["reason"]
    assert removal.store.load().submitted and removal.sends == 1
    assert removal.run()["state"] == "succeeded" and removal.sends == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("approval_id", identity(99)),
        ("revision", "f" * 64),
        ("request_id", identity(98)),
        ("provider_baseline", {"fixture": "other"}),
        ("ready_observed_at", "2000-01-01T00:00:00+00:00"),
    ],
)
def test_checkpoint_cannot_replace_fixed_identity_or_evidence(removal, field, value):
    removal.run()
    saved = removal.store.load()
    with pytest.raises((EvidenceError, AssertionError)):
        removal.store.save(replace(saved, **{field: value}))
    assert removal.store.load() == saved


@pytest.mark.parametrize(
    "failure", ["revoked", "self-approved", "wrong-action", "expired", "window"]
)
def test_invalid_approval_or_window_never_sends(removal, failure):
    removal.approved = True

    def change(ticket):
        if failure == "revoked":
            ticket["revoked"] = True
        elif failure == "self-approved":
            ticket["decided_by"] = removal.selected.requester_id
        elif failure == "wrong-action":
            ticket["request"]["action"] = "provision"
        elif failure == "expired":
            ticket["expires_at"] = removal.now.isoformat()
        return ticket

    removal.change = change
    if failure == "window":
        removal.now = removal.selected.deadline
    with pytest.raises(EvidenceError):
        removal.run()
    assert removal.sends == 0


def test_failed_durable_write_prevents_transmission(removal, monkeypatch):
    removal.run()
    removal.approved = True
    save = removal.store.save

    def fail(state):
        if state.submitted:
            raise OSError("private disk failure")
        save(state)

    monkeypatch.setattr(removal.store, "save", fail)
    assert removal.run()["status"] == "BLOCKED"
    assert removal.sends == 0 and not removal.store.load().submitted


@pytest.mark.parametrize(
    "provider_state", ["pass", "denied", "remaining-owned", "missing-survivor"]
)
def test_cli_recovered_demo_report_requires_independent_absence_and_survivors(
    removal, monkeypatch, provider_state, capsys
):
    from superplane_acceptance import demo1_cli, demo1_runtime, demo1_session

    removal.approved = True
    removal.run()
    saved = removal.store.load()
    provider = {
        "status": "OBSERVED",
        "inventory_complete": True,
        "full_inventory_complete": False,
        "cost_usd": None,
        "checks": {
            "owned_absence": {"status": "PASS"},
            "survivors": {"status": "PASS"},
        },
    }
    if provider_state == "denied":
        provider.update(status="BLOCKED", inventory_complete=False)
    elif provider_state == "remaining-owned":
        provider["checks"]["owned_absence"]["status"] = "FAIL"
    elif provider_state == "missing-survivor":
        provider["checks"]["survivors"]["status"] = "FAIL"

    def verified(selected, envelope, original, baseline, remaining, **kwargs):
        assert baseline == saved.provider_baseline and original == removal.original
        return provider

    monkeypatch.setattr(demo1_removal_provider, "verify_provider_removal", verified)
    monkeypatch.setattr(
        demo1_runtime,
        "RuntimeReader",
        lambda *args, **kwargs: SimpleNamespace(
            observe=lambda seconds: {"status": "OBSERVED"}
        ),
    )
    monkeypatch.setattr(
        demo1_session,
        "observe_in_browser",
        lambda selected, envelope, session, callback, **kwargs: callback(
            removal.transport
        ),
    )

    def workspace(method, path, body=None):
        assert method == "GET" and path.endswith("/workspaces/" + saved.workspace_id)
        return 200, {
            "id": saved.workspace_id,
            "org_id": removal.selected.org_id,
            "status": "retired",
            "provisioning_operation_id": saved.source_operation_id,
        }

    removal.transport.request = workspace
    # Reopen the real private checkpoints through the CLI, including its
    # preflight/report merge and published result. Only external I/O is synthetic.
    removal.release_locks()
    capsys.readouterr()
    directory = removal.driver.path
    report_path = directory / "final-removal-report.json"
    code = demo1_cli.main(
        [
            "--mode",
            "live",
            "--advance-removal",
            "--private-input",
            str(directory / "selection.json"),
            "--authority",
            str(directory / "authority.json"),
            "--browser-state",
            str(directory / "requester-state.json"),
            "--checkpoint",
            str(directory / "checkpoint.json"),
            "--retirement-checkpoint",
            str(directory / "cleanup.json"),
            "--removal-checkpoint",
            str(directory / "removal.json"),
            "--report",
            str(report_path),
        ]
    )
    result = json.loads(report_path.read_text())
    expected = "PASS" if provider_state == "pass" else "BLOCKED"
    assert code == (0 if expected == "PASS" else 2)
    assert expected in capsys.readouterr().out
    assert result["version"] == "demo1-live-result-v1"
    assert result["status"] == result["demo1"]["status"] == expected
    assert "dedicated Demo 1" in result["scope"]
    assert result["broader_acceptance"]["status"] == "UNVERIFIED"
    assert result["broader_acceptance"]["live_acceptance"] is False
    assert result["broader_acceptance"]["criteria"]["AC-02"] == "BLOCKED"
    assert "criteria" not in result and "live_acceptance" not in result
    assert result["demo1"]["cost_usd"] is None
    assert (
        result["browser"]["removal"]["ready_before_removal"]["observed_at"]
        == saved.ready_observed_at
    )
    assert removal.sends == 1
