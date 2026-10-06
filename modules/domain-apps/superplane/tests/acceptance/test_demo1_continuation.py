"""Continuation admission over offline browser/runtime doubles; no paid operations."""

import copy
import json
import shlex
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_demo1_journey as journey_fixtures
import test_demo1_ownership as ownership_fixtures
from test_demo1_cli import identifier
from test_demo1_lineage import LineageProducer, Records

from superplane_acceptance import demo1_cli, demo1_journey
from superplane_acceptance.demo1_continuation import PrivateContinuation
from superplane_acceptance.demo1_evidence import EvidenceError
from superplane_acceptance.demo1_live import PrivateCheckpoint
from superplane_acceptance.demo1_runtime import RuntimeReader

driver = journey_fixtures.driver
producer_imports = ownership_fixtures.producer_imports


@pytest.fixture(params=["apply-infrastructure", "bootstrap-workspace"])
def continuation(driver, monkeypatch, request):
    from harness_jobs.identity import (
        OperationRequest,
        decode_payload,
        encode_payload,
        payload_digest,
    )

    driver.run()
    driver.page.service.approved = True
    assert driver.run()["browser"]["creation_observed"]
    original_bytes = (driver.path / "checkpoint.json").read_bytes()
    phase = request.param
    source = identifier(12 if phase == "apply-infrastructure" else 64)
    operation = identifier(64 if phase == "apply-infrastructure" else 68)
    records = Records(browser=True, bootstrap=phase == "bootstrap-workspace")
    producer = LineageProducer(records)
    monkeypatch.setattr(
        demo1_journey,
        "RuntimeReader",
        lambda selected, target: RuntimeReader(selected, target, runner=producer),
    )
    selected = driver.selected
    target = {
        "org_id": selected.org_id,
        "workspace_id": identifier(10),
        "account_id": selected.account,
        "aws_region": selected.region,
    }
    proposal = {
        "status": "awaiting_plan_approval",
        "workspace_id": identifier(10),
        "source_operation_id": source,
        "artifact_id": "a" * 64,
        "request_revision": selected.plan_revision,
        "account_id": selected.account,
        "target": target,
        "phase": phase,
        "plan_file_sha256": "b" * 64,
        "plan_json_sha256": "c" * 64,
    }
    parameters = {
        "plan_revision": selected.plan_revision,
        "lifecycle_phase": phase,
        "lifecycle_source_operation_id": source,
        "lifecycle_artifact_id": "a" * 64,
        "provider": "aws",
        "provider_account_id": selected.account,
        "aws_account_id": selected.account,
        "lifecycle_request": json.dumps(
            {
                "mode": "existing-account-managed",
                "target_account_id": selected.account,
                "workspace_id": identifier(10),
                "region": selected.region,
            }
        ),
        "lifecycle_inputs": json.dumps({"name": selected.workspace_name}),
        "max_cost_micros": "1000000" if phase == "apply-infrastructure" else "0",
        "max_runtime_seconds": "600",
        "max_resource_units": "10" if phase == "apply-infrastructure" else "0",
    }
    state = SimpleNamespace(
        approved=False,
        source=source,
        workspace_operation=source,
        phase=phase,
        operation=operation,
        proposal=proposal,
        parameters=parameters,
        approval=None,
        request_id=None,
        calls=[],
        lost=False,
        registration_lost=False,
        issued_lost=False,
        denied_recovery=False,
        unavailable=False,
        ticket_change=lambda ticket: ticket,
        review_change=lambda review: review,
        admitted=0,
        missing=False,
    )
    base_request = driver.page.service.request

    def request_api(method, path, body=None):
        state.calls.append((method, path, copy.deepcopy(body)))
        if path.endswith("/operations/" + state.source):
            return 200, {
                "request_id": selected.request_id
                if phase == "apply-infrastructure"
                else identifier(63),
                "workspace_id": identifier(10),
                "provisioning_operation_id": state.source,
                "state": "succeeded",
            }
        if path.endswith("/operations/" + operation):
            return 200, {
                "request_id": state.request_id,
                "workspace_id": identifier(10),
                "provisioning_operation_id": operation,
                "state": "pending",
            }
        if path.endswith("/lifecycle-proposals"):
            return 200, {
                "workspace_id": identifier(10),
                "proposals": [] if state.missing else [state.proposal],
            }
        if "/lifecycle-proposals/" in path and path.endswith("/preview"):
            state.request_id = body["operation_id"]
            admitted_request = OperationRequest(
                "provision", state.request_id, state.parameters
            )
            state.approval = {
                "workspace_id": identifier(10),
                "action": "provision",
                "idempotency_key": state.request_id,
                "parameters": state.parameters,
            }
            return 200, state.review_change(
                {
                    **state.proposal,
                    "request_id": state.request_id,
                    "revision": payload_digest(admitted_request),
                    "approval_request": copy.deepcopy(state.approval),
                }
            )
        if path.endswith(
            ("/operation-approvals", "/operation-approvals/" + identifier(80))
        ):
            if method == "POST":
                assert body == state.approval
                if state.issued_lost:
                    raise RuntimeError("private approval transport failure")
            admitted_request = OperationRequest(
                "provision", state.request_id, state.parameters
            )
            ticket = {
                "approval_id": identifier(80),
                "workspace_id": identifier(10),
                "requester": selected.requester_id,
                "request": json.loads(encode_payload(admitted_request)),
                "plan_digest": payload_digest(admitted_request),
                "envelope": {
                    key: int(state.parameters[key])
                    for key in (
                        "max_cost_micros",
                        "max_runtime_seconds",
                        "max_resource_units",
                    )
                },
                "revoked": False,
                "approvers": [selected.approver_id],
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "result": "allowed-once" if state.approved else "pending",
                "decided_by": selected.approver_id if state.approved else None,
                "decided_at": datetime.now(UTC).isoformat() if state.approved else None,
            }
            return 200, state.ticket_change(ticket)
        if path.endswith("/continue"):
            assert path.endswith(
                f"/lifecycle-proposals/{state.proposal['artifact_id']}/continue"
            )
            assert body == {
                "operation_id": state.request_id,
                "approval_id": identifier(80),
            }
            assert state.approved
            durable = json.loads((driver.path / "phase.json").read_text())["checkpoint"]
            assert (
                durable["submitted"] is True
                and durable["request_id"] == state.request_id
            )
            if not state.admitted:
                state.admitted += 1
                current = records.operations[operation]
                admitted_request = decode_payload(current["request_payload"])
                admitted_request = OperationRequest(
                    "provision", state.request_id, admitted_request.parameters
                )
                current.update(
                    idempotency_key=state.request_id,
                    request_payload=encode_payload(admitted_request),
                    plan_digest=payload_digest(admitted_request),
                )
            if state.registration_lost:
                raise RuntimeError("private lost workspace commit")
            if state.workspace_operation == source:
                state.workspace_operation = operation
                records.workspace_current = operation
            if state.lost:
                raise RuntimeError("private lost continuation response")
            return 200, {
                "request_id": state.request_id,
                "workspace_id": identifier(10),
                "provisioning_operation_id": operation,
                "phase": phase,
                "state": "pending",
            }
        if state.request_id and path.endswith(
            "/operations/by-idempotency/" + state.request_id
        ):
            if state.denied_recovery:
                return 404, {"detail": "not found"}
            return 200, {
                "request_id": state.request_id,
                "workspace_id": identifier(10),
                "provisioning_operation_id": operation,
                "phase": "execution",
                "state": "pending",
            }
        if state.unavailable and path.endswith("/capabilities") and state.approved:
            return 503, {}
        status, response = base_request(method, path, body)
        if path.endswith("/operations/by-idempotency/" + selected.request_id):
            response = producer.native_operation(state.workspace_operation)
        if path.endswith("/workspaces/" + identifier(10)) and method == "GET":
            response["provisioning_operation_id"] = state.workspace_operation
            records.workspace_current = state.workspace_operation
        return status, response

    monkeypatch.setattr(driver.page.service, "request", request_api)

    def run(*extra):
        report = (
            driver.path
            / f"phase-report-{len(list(driver.path.glob('phase-report-*')))}.json"
        )
        arguments = [
            "--mode",
            "live",
            "--advance-continuation",
            phase,
            "--continuation-checkpoint",
            str(driver.path / "phase.json"),
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
        assert demo1_cli.main(arguments) == 2
        assert (driver.path / "checkpoint.json").read_bytes() == original_bytes
        return json.loads(report.read_text()) if report.exists() else None

    state.run, state.driver, state.path = run, driver, driver.path / "phase.json"
    return state


def test_continuation_timeout_after_persistence_retries_same_unsent_phase(
    continuation, monkeypatch
):
    continuation.run()
    original = continuation.path.read_bytes()
    continuation.approved = True
    clock = journey_fixtures.expire_after_submission(
        monkeypatch, continuation.driver, PrivateContinuation
    )
    before = len(continuation.calls)
    continuation.run()
    assert continuation.admitted == 0
    assert not any(
        method == "POST" and path.endswith("/continue")
        for method, path, _ in continuation.calls[before:]
    )
    assert continuation.path.read_bytes() == original
    clock.enabled = False
    assert continuation.run()["browser"]["continuation"]["submission_observed"]
    assert continuation.admitted == 1
    saved = json.loads(continuation.path.read_text())["checkpoint"]
    assert saved == {**json.loads(original)["checkpoint"], "submitted": True}


@pytest.mark.parametrize("lost", [False, True])
def test_approved_phase_submits_once_and_recovers_without_preview_or_approval(
    continuation, lost
):
    state = continuation
    first = state.run()
    assert (
        first["browser"]["continuation"]["reason"]
        == "awaiting independent continuation approval"
    )
    saved = state.path.read_bytes()
    assert state.run()["browser"]["continuation"]["phase"] == state.phase
    assert state.path.read_bytes() == saved
    state.approved, state.lost = True, lost
    report = state.run()
    assert state.admitted == 1
    assert json.loads(state.path.read_text())["checkpoint"]["submitted"] is True
    assert not report["live_acceptance"] and report["status"] == "BLOCKED"
    before = len(state.calls)
    state.ticket_change = lambda ticket: {**ticket, "revoked": True}
    assert state.run()["browser"]["continuation"]["submission_observed"]
    assert state.admitted == 1
    assert all(method == "GET" for method, _, _ in state.calls[before:])
    assert not any(
        "/decision" in path or "/retirement/" in path for _, path, _ in state.calls
    )
    assert state.request_id not in json.dumps(report)


def test_admitted_continuation_repairs_lost_registration_with_original_identity(
    continuation,
):
    state = continuation
    state.run()
    state.approved, state.registration_lost = True, True
    state.run()
    assert state.admitted == 1 and state.workspace_operation == state.source
    saved = state.path.read_bytes()
    state.registration_lost = False
    state.ticket_change = lambda ticket: {
        **ticket,
        "expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    }
    state.missing = True
    before = len(state.calls)
    report = state.run()
    assert state.workspace_operation == state.operation
    assert state.admitted == 1 and state.path.read_bytes() == saved
    assert report["browser"]["continuation"]["submission_observed"]
    posts = [
        (path, body) for method, path, body in state.calls[before:] if method == "POST"
    ]
    assert posts == [
        (
            (
                f"/api/superplane/v1/workspaces/{identifier(10)}"
                f"/lifecycle-proposals/{state.proposal['artifact_id']}/continue"
            ),
            {"operation_id": state.request_id, "approval_id": identifier(80)},
        )
    ]
    before = len(state.calls)
    assert state.run()["browser"]["continuation"]["submission_observed"]
    assert all(method == "GET" for method, _, _ in state.calls[before:])
    assert state.admitted == 1 and state.path.read_bytes() == saved


@pytest.fixture
def unregistered_continuation(continuation):
    state = continuation
    state.run()
    state.approved, state.registration_lost = True, True
    state.run()
    assert state.admitted == 1 and state.workspace_operation == state.source
    state.registration_lost = False
    return state


@pytest.mark.parametrize(
    "failure",
    [
        "missing-admission",
        "request",
        "admission-workspace",
        "source-operation",
        "invalid-operation",
        "workspace",
        "organization",
        "name",
        "foreign-pointer",
        "pointer-race",
    ],
)
def test_unverified_registration_recovery_never_posts(
    unregistered_continuation, monkeypatch, failure
):
    state = unregistered_continuation
    request = state.driver.page.service.request
    saved, before = state.path.read_bytes(), len(state.calls)
    admission_seen = False

    def intercept(method, path, body=None):
        nonlocal admission_seen
        status, response = request(method, path, body)
        if path.endswith("/operations/by-idempotency/" + state.request_id):
            admission_seen = True
            if failure == "missing-admission":
                return 404, {}
            changes = {
                "request": {"request_id": identifier(90)},
                "admission-workspace": {"workspace_id": identifier(90)},
                "source-operation": {"provisioning_operation_id": state.source},
                "invalid-operation": {"provisioning_operation_id": "invalid"},
            }
            response = {**response, **changes.get(failure, {})}
        elif admission_seen and path.endswith("/workspaces/" + identifier(10)):
            changes = {
                "workspace": {"id": identifier(90)},
                "organization": {"org_id": identifier(90)},
                "name": {"name": "different-workspace"},
                "foreign-pointer": {"provisioning_operation_id": identifier(90)},
                "pointer-race": {"provisioning_operation_id": state.operation},
            }
            response = {**response, **changes.get(failure, {})}
        return status, response

    monkeypatch.setattr(state.driver.page.service, "request", intercept)
    report = state.run()
    assert (
        not report.get("browser", {}).get("continuation", {}).get("submission_observed")
    )
    assert state.admitted == 1 and state.path.read_bytes() == saved
    assert state.workspace_operation == state.source
    assert all(method == "GET" for method, _, _ in state.calls[before:])


@pytest.mark.parametrize(
    "failure",
    [
        "lost-before-commit",
        "lost-after-commit",
        "denied",
        "wrong-request",
        "wrong-workspace",
        "wrong-operation",
        "wrong-phase",
        "unchanged-registration",
        "foreign-registration",
        "foreign-organization",
    ],
)
def test_uncertain_reconciliation_retains_original_checkpoint_and_recovers(
    unregistered_continuation, monkeypatch, failure
):
    state = unregistered_continuation
    request = state.driver.page.service.request
    saved = state.path.read_bytes()
    reconciled = False

    def intercept(method, path, body=None):
        nonlocal reconciled
        if path.endswith("/continue"):
            reconciled = True
            if failure == "lost-before-commit":
                raise RuntimeError("private lost reconciliation before commit")
            if failure == "denied":
                return 403, {}
            status, response = request(method, path, body)
            if failure == "lost-after-commit":
                raise RuntimeError("private lost reconciliation after commit")
            changes = {
                "wrong-request": {"request_id": identifier(90)},
                "wrong-workspace": {"workspace_id": identifier(90)},
                "wrong-operation": {"provisioning_operation_id": identifier(90)},
                "wrong-phase": {"phase": "retire"},
            }
            return status, {**response, **changes.get(failure, {})}
        status, response = request(method, path, body)
        if reconciled and path.endswith("/workspaces/" + identifier(10)):
            changes = {
                "unchanged-registration": {"provisioning_operation_id": state.source},
                "foreign-registration": {"provisioning_operation_id": identifier(90)},
                "foreign-organization": {"org_id": identifier(90)},
            }
            response = {**response, **changes.get(failure, {})}
        return status, response

    monkeypatch.setattr(state.driver.page.service, "request", intercept)
    report = state.run()
    assert (
        not report.get("browser", {}).get("continuation", {}).get("submission_observed")
    )
    assert state.admitted == 1 and state.path.read_bytes() == saved
    assert not report["live_acceptance"] and report["status"] == "BLOCKED"
    monkeypatch.setattr(state.driver.page.service, "request", request)
    assert state.run()["browser"]["continuation"]["submission_observed"]
    assert state.workspace_operation == state.operation
    assert state.admitted == 1 and state.path.read_bytes() == saved


@pytest.mark.parametrize(
    "failure",
    [
        "target",
        "phase",
        "source",
        "artifact",
        "budget",
        "runtime",
        "placement",
        "foreign-request",
    ],
)
def test_foreign_or_unbounded_review_stops_before_approval(continuation, failure):
    state = continuation
    if failure == "budget":
        state.parameters["max_cost_micros"] = "100000000000000000"
    elif failure == "runtime":
        state.parameters["max_runtime_seconds"] = "99999999"
    elif failure == "placement":
        state.parameters["lifecycle_inputs"] = '{"cluster_placement":"shared"}'
    else:
        key, value = {
            "target": ("account_id", "000000000000"),
            "phase": ("phase", "retire"),
            "source": ("source_operation_id", identifier(90)),
            "artifact": ("artifact_id", "f" * 64),
            "foreign-request": ("request_id", identifier(90)),
        }[failure]
        state.review_change = lambda review: {**review, key: value}
    report = state.run()
    assert "continuation:" in report["reason"]
    assert not state.path.exists() and state.admitted == 0
    assert not any(path.endswith("/operation-approvals") for _, path, _ in state.calls)


@pytest.mark.parametrize(
    "failure",
    [
        "requester",
        "approver",
        "revoked",
        "parameters",
        "digest",
        "action",
        "approval-id",
        "contract-version",
    ],
)
def test_foreign_or_invalid_approval_never_submits(continuation, failure):
    state = continuation
    state.run()
    state.approved = True
    saved = state.path.read_bytes()

    def change(ticket):
        if failure == "parameters":
            ticket["request"]["parameters"]["max_cost_micros"] = "12345"
        elif failure == "action":
            ticket["request"]["action"] = "teardown"
        elif failure == "contract-version":
            ticket["request"]["contract_version"] = "unknown"
        else:
            key, value = {
                "requester": ("requester", identifier(90)),
                "approver": ("decided_by", state.driver.selected.requester_id),
                "revoked": ("revoked", True),
                "digest": ("plan_digest", "f" * 64),
                "approval-id": ("approval_id", identifier(90)),
            }[failure]
            ticket[key] = value
        return ticket

    state.ticket_change = change
    assert (
        not state.run()
        .get("browser", {})
        .get("continuation", {})
        .get("submission_observed")
    )
    assert state.admitted == 0 and state.path.read_bytes() == saved


def test_lost_approval_reply_reuses_deterministic_request_before_paid_admission(
    continuation,
):
    state = continuation
    state.issued_lost = True
    state.run()
    original = state.request_id
    assert not state.path.exists() and state.admitted == 0
    state.issued_lost = False
    state.run()
    assert state.request_id == original
    assert json.loads(state.path.read_text())["checkpoint"]["request_id"] == original


def test_uncertain_admission_and_missing_recovery_never_replays(continuation):
    state = continuation
    state.run()
    state.approved, state.lost = True, True
    state.run()
    state.denied_recovery = True
    saved = state.path.read_bytes()
    state.run()
    assert state.admitted == 1 and state.path.read_bytes() == saved


@pytest.mark.parametrize("written", [False, True])
def test_durable_checkpoint_failure_prevents_continuation_send(
    continuation, monkeypatch, written
):
    state = continuation
    state.run()
    state.approved = True
    save = PrivateContinuation.save

    def refuse(store, checkpoint):
        if checkpoint.submitted:
            if written:
                save(store, checkpoint)
            raise EvidenceError("checkpoint: injected private write failure")
        save(store, checkpoint)

    monkeypatch.setattr(PrivateContinuation, "save", refuse)
    state.run()
    assert state.admitted == 0
    assert json.loads(state.path.read_text())["checkpoint"]["submitted"] is written


def test_phase_checkpoint_rejects_rebinding_and_submitted_rollback(continuation):
    state = continuation
    state.run()
    selected, envelope = state.driver.selected, state.driver.envelope
    with PrivateCheckpoint(
        str(state.driver.path / "checkpoint.json"),
        selected,
        envelope.origin,
        envelope=envelope,
    ) as root:
        with PrivateContinuation(
            str(state.path), selected, envelope, root.load(), state.phase
        ) as store:
            saved = store.load()
            for changed in (
                replace(saved, artifact_id="f" * 64),
                replace(saved, source_operation_id=identifier(90)),
                replace(saved, revision="f" * 64),
            ):
                with pytest.raises(EvidenceError):
                    store.save(changed)
            store.save(replace(saved, submitted=True))
            with pytest.raises(EvidenceError):
                store.save(saved)
        other = (
            "bootstrap-workspace"
            if state.phase == "apply-infrastructure"
            else "apply-infrastructure"
        )
        with (
            PrivateContinuation(
                str(state.path), selected, envelope, root.load(), other
            ) as store,
            pytest.raises(EvidenceError),
        ):
            store.load()


@pytest.mark.parametrize("failure", ["expired", "envelope", "source-race", "presend"])
def test_last_moment_refusals_keep_paid_phase_unsent(
    continuation, monkeypatch, failure
):
    state = continuation
    state.run()
    saved = state.path.read_bytes()
    state.approved = True
    evaluate = state.driver.page.evaluate
    reads = 0
    ticket_seen = False

    def intercept(script, arguments):
        nonlocal reads, ticket_seen
        if "path" not in arguments:
            return evaluate(script, arguments)
        path = arguments["path"]
        if failure == "presend" and ticket_seen and path.endswith("/capabilities"):
            return [503, {}, state.driver.page.release]
        status, body, release = evaluate(script, arguments)
        if path.endswith("/operation-approvals/" + identifier(80)):
            ticket_seen = True
            if failure == "expired":
                body["expires_at"] = (
                    datetime.now(UTC) - timedelta(seconds=1)
                ).isoformat()
            if failure == "envelope":
                body["envelope"]["max_resource_units"] += 1
        if failure == "source-race" and path.endswith("/workspaces/" + identifier(10)):
            reads += 1
            if reads == (2 if state.phase == "apply-infrastructure" else 3):
                body["provisioning_operation_id"] = identifier(90)
        return [status, body, release]

    monkeypatch.setattr(state.driver.page, "evaluate", intercept)
    report = state.run()
    assert state.admitted == 0 and state.path.read_bytes() == saved
    assert report["status"] == "BLOCKED"
    if failure == "presend":
        assert report["browser"]["continuation"]["reason"].startswith(
            "continuation not sent"
        )
        monkeypatch.setattr(state.driver.page, "evaluate", evaluate)
        state.run()
        assert state.admitted == 1


def test_documented_continuation_command_uses_the_executable_driver(continuation):
    state = continuation
    document = (Path(__file__).parent / "README.md").read_text()
    command = next(
        block.split("```", 1)[0]
        for block in document.split("```sh\n")[1:]
        if "--advance-continuation apply-infrastructure" in block.split("```", 1)[0]
    )
    arguments = shlex.split(
        command.replace("\\\n", "").replace(
            "$DEMO1_PRIVATE_DIR", str(state.driver.path)
        )
    )
    arguments = arguments[arguments.index("superplane_acceptance.demo1_cli") + 1 :]
    arguments[arguments.index("--advance-continuation") + 1] = state.phase
    assert demo1_cli.main(arguments) == 2
    report = json.loads((state.driver.path / "apply-report-01.json").read_text())
    assert (
        report["browser"]["continuation"]["reason"]
        == "awaiting independent continuation approval"
    )
    assert state.admitted == 0


def test_missing_proposal_is_not_an_admission_or_pass(continuation):
    state = continuation
    state.missing = True
    assert (
        state.run()["browser"]["continuation"]["reason"]
        == "selected continuation proposal unavailable"
    )
    assert state.admitted == 0 and not state.path.exists()


def test_continuation_options_refuse_provider_combination_before_remote_calls(
    continuation,
):
    state = continuation
    before = len(state.calls)
    assert state.run("--observe-provider") is None
    assert len(state.calls) == before
