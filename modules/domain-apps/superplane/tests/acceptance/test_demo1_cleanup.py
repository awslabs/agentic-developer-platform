"""Cleanup-preparation admission and no-replay recovery through the guarded CLI."""

import copy
import json
import shlex
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_demo1_retirement as retirement_fixtures
from test_demo1_browser import identity
from test_demo1_journey import expire_after_submission

from superplane_acceptance import demo1_cli
from superplane_acceptance.demo1_cleanup import PrivateCleanup
from superplane_acceptance.demo1_evidence import EvidenceError

driver = retirement_fixtures.driver
prepared = retirement_fixtures.prepared


@pytest.fixture
def cleanup(prepared, monkeypatch):
    driver = prepared.driver
    review = prepared.document
    request = driver.page.service.request
    state = SimpleNamespace(
        driver=driver,
        review=review,
        original=prepared.original,
        original_bytes=prepared.saved,
        approved=False,
        calls=[],
        admitted=False,
        registered=False,
        admissions=0,
        lost=False,
        issued_lost=False,
        missing=False,
        ticket_change=lambda ticket: ticket,
        receipt_change=lambda value: value,
        admission_change=lambda value: value,
    )

    def ticket():
        return {
            "approval_id": identity(80),
            "workspace_id": state.original.workspace_id,
            "requester": driver.selected.requester_id,
            "approvers": [driver.selected.approver_id],
            "request": {
                "contract_version": "v1",
                **{
                    key: value
                    for key, value in review["approval_request"].items()
                    if key != "workspace_id"
                },
            },
            "plan_digest": review["revision"],
            "envelope": {
                key: int(review["approval_request"]["parameters"][key])
                for key in (
                    "max_resource_units",
                    "max_runtime_seconds",
                    "max_cost_micros",
                )
            },
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            "result": "allowed-once" if state.approved else "pending",
            "revoked": False,
            "decided_by": driver.selected.approver_id if state.approved else None,
            "decided_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            if state.approved
            else None,
        }

    def serve(method, path, body=None):
        state.calls.append((method, path, copy.deepcopy(body)))
        if path.endswith("/operation-approvals") and method == "POST":
            assert body == review["approval_request"]
            if state.issued_lost:
                raise ConnectionError("private approval reply lost")
            return 200, ticket()
        if path.endswith("/operation-approvals/" + identity(80)):
            return 200, ticket()
        if path.endswith("/operations/by-idempotency/" + review["request_id"]):
            if not state.admitted or state.missing:
                return 404, {"detail": "unavailable"}
            return 200, state.admission_change(
                {
                    "request_id": review["request_id"],
                    "workspace_id": state.original.workspace_id,
                    "provisioning_operation_id": identity(81),
                    "state": "pending",
                    "phase": "execution",
                }
            )
        if path.endswith("/retirement/access"):
            assert body == {
                "operation_id": state.original.retirement_request_id,
                "plan_revision": review["revision"],
                "approval_id": identity(80),
            }
            saved = json.loads((driver.path / "cleanup.json").read_text())["checkpoint"]
            assert saved["submitted"] is True
            if not state.admitted:
                assert state.approved
                state.admitted = True
                state.admissions += 1
            if state.lost:
                raise ConnectionError("private registry/reply lost")
            state.registered = True
            return 200, state.receipt_change(
                {
                    "retirement_request_id": state.original.retirement_request_id,
                    "request_id": review["request_id"],
                    "workspace_id": state.original.workspace_id,
                    "control_operation_id": identity(81),
                    "phase": "prepare-retirement-access",
                    "state": "pending",
                    "retryable": False,
                    "retirement_complete": False,
                }
            )
        return request(method, path, body)

    evaluate = driver.page.evaluate

    def evaluated(script, arguments):
        result = evaluate(script, arguments)
        if (
            isinstance(result, list)
            and isinstance(result[1], dict)
            and result[1].get("approval_id") == identity(80)
        ):
            result[1] = state.ticket_change(result[1])
        return result

    monkeypatch.setattr(driver.page.service, "request", serve)
    monkeypatch.setattr(driver.page, "evaluate", evaluated)

    def run(*extra):
        report = (
            driver.path
            / f"cleanup-report-{len(list(driver.path.glob('cleanup-report-*')))}.json"
        )
        assert (
            demo1_cli.main(
                [
                    "--mode",
                    "live",
                    "--advance-retirement-access",
                    "--retirement-checkpoint",
                    str(driver.path / "cleanup.json"),
                    "--private-input",
                    str(driver.path / "selection.json"),
                    "--authority",
                    str(driver.path / "authority.json"),
                    "--browser-state",
                    str(driver.path / "requester-state.json"),
                    "--checkpoint",
                    str(driver.path / "checkpoint.json"),
                    "--report",
                    str(report),
                    *extra,
                ]
            )
            == 2
        )
        assert (driver.path / "checkpoint.json").read_bytes() == state.original_bytes
        return json.loads(report.read_text()) if report.exists() else None

    state.run = run
    return state


@pytest.mark.parametrize("lost", [False, True])
def test_separate_human_approval_submits_one_preparation_and_recovers_registration(
    cleanup, lost
):
    first = cleanup.run()["browser"]["cleanup_preparation"]
    assert "awaiting independent" in first["reason"] and cleanup.admissions == 0
    assert (
        "awaiting independent"
        in cleanup.run()["browser"]["cleanup_preparation"]["reason"]
    )
    cleanup.approved, cleanup.lost = True, lost
    report = cleanup.run()
    assert cleanup.admissions == 1 and report["status"] == "BLOCKED"
    checkpoint = cleanup.driver.path / "cleanup.json"
    saved = checkpoint.read_bytes()
    assert json.loads(saved)["checkpoint"]["submitted"] is True
    cleanup.lost = False
    cleanup.ticket_change = lambda ticket: pytest.fail(
        "recovery must not read or renew approval"
    )
    before = len(cleanup.calls)
    for _attempt in range(2):
        report = cleanup.run()
        phase = report["browser"]["cleanup_preparation"]
        assert (
            phase["submission_observed"] is True
            and phase["retirement_complete"] is False
        )
        assert "recovered" in phase["reason"] and not report["live_acceptance"]
    assert (
        cleanup.admissions == 1
        and cleanup.registered
        and checkpoint.read_bytes() == saved
    )
    recovery = cleanup.calls[before:]
    assert (
        sum(
            "/operations/by-idempotency/" + cleanup.review["request_id"] in path
            for _, path, _ in recovery
        )
        == 2
    )
    assert all(
        method == "GET" or path.endswith("/retirement/access")
        for method, path, _ in recovery
    )
    assert not any(
        path.endswith(("/decision", "/retirement")) for _, path, _ in cleanup.calls
    )


@pytest.mark.parametrize(
    "change",
    [
        "expired",
        "revoked",
        "self",
        "foreign_request",
        "parameters",
        "digest",
        "envelope",
    ],
)
def test_invalid_approval_cannot_submit_preparation(cleanup, change):
    cleanup.run()
    cleanup.approved = True

    def invalid(ticket):
        if change == "expired":
            ticket["expires_at"] = (
                datetime.now(UTC) - timedelta(seconds=1)
            ).isoformat()
        elif change == "revoked":
            ticket["revoked"] = True
        elif change == "self":
            ticket["decided_by"] = cleanup.driver.selected.requester_id
        elif change == "foreign_request":
            ticket["request"]["idempotency_key"] = identity(99)
        elif change == "parameters":
            ticket["request"]["parameters"] = {}
        elif change == "digest":
            ticket["plan_digest"] = "f" * 64
        elif change == "envelope":
            ticket["envelope"]["max_cost_micros"] = 1
        return ticket

    cleanup.ticket_change = invalid
    saved = (cleanup.driver.path / "cleanup.json").read_bytes()
    report = cleanup.run()
    assert "cleanup_preparation" not in report.get("browser", {})
    assert (
        cleanup.admissions == 0
        and (cleanup.driver.path / "cleanup.json").read_bytes() == saved
    )


@pytest.mark.parametrize(
    "change", ["missing", "request", "workspace", "source", "receipt"]
)
def test_uncertain_recovery_never_creates_a_replacement(cleanup, change):
    cleanup.run()
    cleanup.approved, cleanup.lost = True, True
    cleanup.run()
    cleanup.lost = False
    if change == "missing":
        cleanup.missing = True
    elif change == "receipt":
        cleanup.receipt_change = lambda receipt: {
            **receipt,
            "control_operation_id": identity(99),
        }
    else:
        field = {
            "request": "request_id",
            "workspace": "workspace_id",
            "source": "provisioning_operation_id",
        }[change]
        cleanup.admission_change = lambda admitted: {
            **admitted,
            field: identity(14 if change == "source" else 99),
        }
    saved = (cleanup.driver.path / "cleanup.json").read_bytes()
    before = len(cleanup.calls)
    report = cleanup.run()
    assert "cleanup_preparation" not in report.get("browser", {})
    assert (
        cleanup.admissions == 1
        and (cleanup.driver.path / "cleanup.json").read_bytes() == saved
    )
    if change != "receipt":
        assert all(method == "GET" for method, _, _ in cleanup.calls[before:])


def test_failed_presend_check_remains_retryable_but_failed_persistence_never_sends(
    cleanup, monkeypatch
):
    cleanup.run()
    cleanup.approved = True
    save = PrivateCleanup.save

    def refuse(store, state):
        if state.submitted:
            raise EvidenceError("fixture persistence refused")
        return save(store, state)

    monkeypatch.setattr(PrivateCleanup, "save", refuse)
    report = cleanup.run()
    assert "checkpoint write uncertain" in report["reason"] and cleanup.admissions == 0
    assert (
        json.loads((cleanup.driver.path / "cleanup.json").read_text())["checkpoint"][
            "submitted"
        ]
        is False
    )
    monkeypatch.setattr(PrivateCleanup, "save", save)
    assert cleanup.run()["browser"]["cleanup_preparation"]["submission_observed"]
    assert cleanup.admissions == 1


def test_private_cleanup_checkpoint_refuses_changed_identity_and_submitted_rollback(
    cleanup,
):
    cleanup.run()
    cleanup.approved = True
    cleanup.run()
    driver = cleanup.driver
    from superplane_acceptance.demo1_browser import CreationCheckpoint

    original = CreationCheckpoint(**vars(cleanup.original))
    with PrivateCleanup(
        driver.path / "cleanup.json", driver.selected, driver.envelope, original
    ) as store:
        saved = store.load()
        for changed in (
            replace(saved, submitted=False),
            replace(saved, approval_id=identity(99)),
            replace(saved, retirement_request_id=identity(99)),
        ):
            with pytest.raises(EvidenceError):
                store.save(changed)


def test_cleanup_flag_requires_its_checkpoint_and_cannot_combine_provider_reads(
    cleanup,
):
    before = len(cleanup.calls)
    assert cleanup.run("--observe-provider") is None
    assert len(cleanup.calls) == before
    assert demo1_cli.main(["--mode", "live", "--advance-retirement-access"]) == 2
    assert (
        demo1_cli.main(["--mode", "fixture", "--retirement-checkpoint", "unused"]) == 2
    )


def test_lost_approval_reply_reuses_exact_preparation_request(cleanup):
    cleanup.issued_lost = True
    assert "cleanup_preparation" not in cleanup.run().get("browser", {})
    assert (
        not (cleanup.driver.path / "cleanup.json").exists() and cleanup.admissions == 0
    )
    cleanup.issued_lost = False
    assert (
        "awaiting independent"
        in cleanup.run()["browser"]["cleanup_preparation"]["reason"]
    )
    requests = [
        body
        for method, path, body in cleanup.calls
        if method == "POST" and path.endswith("/operation-approvals")
    ]
    assert len(requests) == 2 and requests[0] == requests[1]


@pytest.mark.parametrize("failure", ["release", "source", "changed_plan"])
def test_last_moment_refusal_keeps_preparation_unsent(cleanup, monkeypatch, failure):
    cleanup.run()
    cleanup.approved = True
    evaluate = cleanup.driver.page.evaluate
    ticket_seen = False

    def intercept(script, arguments):
        nonlocal ticket_seen
        path = arguments.get("path", "")
        if failure == "release" and ticket_seen and path.endswith("/capabilities"):
            return [503, None, cleanup.driver.page.release]
        result = evaluate(script, arguments)
        if path.endswith("/operation-approvals/" + identity(80)):
            ticket_seen = True
        if (
            failure == "source"
            and ticket_seen
            and path.endswith("/workspaces/" + identity(10))
        ):
            result[1]["provisioning_operation_id"] = identity(99)
        return result

    monkeypatch.setattr(cleanup.driver.page, "evaluate", intercept)
    if failure == "changed_plan":
        cleanup.review["revision"] = "f" * 64
    report = cleanup.run()
    assert report["status"] == "BLOCKED" and cleanup.admissions == 0
    assert (
        json.loads((cleanup.driver.path / "cleanup.json").read_text())["checkpoint"][
            "submitted"
        ]
        is False
    )


def test_documented_cleanup_command_executes_guarded_admission_path(cleanup):
    document = (Path(__file__).parent / "README.md").read_text()
    command = next(
        block.split("\n```", 1)[0]
        for block in document.split("```sh\n")[1:]
        if "--advance-retirement-access" in block.split("\n```", 1)[0]
    )
    arguments = shlex.split(
        command.replace("\\\n", " ").replace(
            "$DEMO1_PRIVATE_DIR", str(cleanup.driver.path)
        )
    )
    assert demo1_cli.main(arguments[arguments.index("--mode") :]) == 2
    assert cleanup.admissions == 0
    report = json.loads((cleanup.driver.path / "cleanup-report.json").read_text())
    assert "awaiting independent" in report["browser"]["cleanup_preparation"]["reason"]


def test_approval_expiry_during_final_workspace_read_prevents_admission(
    cleanup, monkeypatch
):
    from superplane_acceptance import demo1_journey

    cleanup.run()
    cleanup.approved = True
    current = datetime.now(UTC)
    ticket_seen = False
    evaluate = cleanup.driver.page.evaluate
    advance = demo1_journey.advance_browser

    def advancing(*args, **kwargs):
        return advance(*args, **kwargs, clock=lambda: current)

    def delayed_read(script, arguments):
        nonlocal current, ticket_seen
        result = evaluate(script, arguments)
        path = arguments.get("path", "")
        if path.endswith("/operation-approvals/" + identity(80)):
            ticket_seen = True
        elif ticket_seen and path.endswith("/workspaces/" + identity(10)):
            current += timedelta(minutes=16)
        return result

    monkeypatch.setattr(demo1_journey, "advance_browser", advancing)
    monkeypatch.setattr(cleanup.driver.page, "evaluate", delayed_read)
    cleanup.run()
    assert cleanup.admissions == 0
    assert (
        json.loads((cleanup.driver.path / "cleanup.json").read_text())["checkpoint"][
            "submitted"
        ]
        is False
    )


def test_cleanup_timeout_after_persistence_retries_original_unsent_preparation(
    cleanup, monkeypatch
):
    cleanup.run()
    checkpoint = cleanup.driver.path / "cleanup.json"
    original = checkpoint.read_bytes()
    cleanup.approved = True
    clock = expire_after_submission(monkeypatch, cleanup.driver, PrivateCleanup)
    before = len(cleanup.calls)
    cleanup.run()
    assert cleanup.admissions == 0
    assert not any(
        method == "POST" and path.endswith("/retirement/access")
        for method, path, _ in cleanup.calls[before:]
    )
    assert checkpoint.read_bytes() == original
    clock.enabled = False
    report = cleanup.run()
    assert report["browser"]["cleanup_preparation"]["submission_observed"]
    assert cleanup.admissions == 1
    saved = json.loads(checkpoint.read_text())["checkpoint"]
    assert saved == {**json.loads(original)["checkpoint"], "submitted": True}


def test_failed_unsent_restoration_preserves_checkpoint_without_replay(
    cleanup, monkeypatch
):
    cleanup.run()
    cleanup.approved = True
    clock = expire_after_submission(monkeypatch, cleanup.driver, PrivateCleanup)
    write = PrivateCleanup._write

    def refuse_restore(store, checkpoint):
        previous = store.load()
        if previous and previous.submitted and not checkpoint.submitted:
            raise EvidenceError("fixture: unsent restoration write refused")
        return write(store, checkpoint)

    monkeypatch.setattr(PrivateCleanup, "_write", refuse_restore)
    report = cleanup.run()
    assert "restoration write refused" in report["reason"]
    checkpoint = cleanup.driver.path / "cleanup.json"
    saved = checkpoint.read_bytes()
    assert json.loads(saved)["checkpoint"]["submitted"] is True
    assert cleanup.admissions == 0
    clock.enabled = False
    before = len(cleanup.calls)
    cleanup.run()
    assert checkpoint.read_bytes() == saved and cleanup.admissions == 0
    assert all(method == "GET" for method, _, _ in cleanup.calls[before:])
