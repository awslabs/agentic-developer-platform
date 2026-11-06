"""Fixture-backed checks for independent financial observations."""

from copy import deepcopy

import pytest

from tests.e2e.cli_uplift.product_financials import (
    DATE_PATH,
    verify_financial_denial,
    verify_financial_progress,
    verify_financial_resume,
    verify_selective_date_import,
)


@pytest.fixture
def selective_import():
    allocation = {
        "amount": {"amount": "75.00", "currency": "USD"},
        "approval_status": "proposed",
        "period_start": "2026-10-01",
        "period_end": "2026-12-31",
    }
    source_ref = {"record_id": "fictional-business-case", "record_revision": "1"}
    original_proof = {
        "paths": ["/configuration/financials/allocations"],
        "state": "manual",
        "source_refs": [],
    }
    before_profile = {
        "revision": 2,
        "configuration": {"financials": {"allocations": [allocation]}},
        "product": {"name": "Example"},
        "provenance": [original_proof],
    }
    proposal = {
        "id": "suggestion-1",
        "remaining_paths": ["/configuration/financials/allocations", DATE_PATH],
    }
    before_session = {
        "id": "session-1",
        "tenant_id": "tenant-1",
        "product_id": "product-1",
        "project_id": "project-1",
        "revision": 2,
        "proposals": [
            proposal,
            {"id": "suggestion-2", "remaining_paths": ["/product/purpose"]},
        ],
    }
    after_profile = deepcopy(before_profile)
    after_profile["revision"] = 3
    after_profile["configuration"]["financials"]["release_target_date"] = "2026-12-01"
    after_profile["provenance"].append(
        {
            "paths": [DATE_PATH],
            "state": "accepted",
            "source_refs": [source_ref],
            "confirmed_by": {"id": "member-1"},
        }
    )
    after_session = deepcopy(before_session)
    after_session["revision"] = 3
    after_session["proposals"][0]["remaining_paths"] = [
        "/configuration/financials/allocations"
    ]
    return [
        before_profile,
        before_session,
        deepcopy(after_session),
        after_profile,
        after_session,
        deepcopy(after_session),
    ], {
        "proposal_id": "suggestion-1",
        "expected_date": "2026-12-01",
        "source_ref": source_ref,
    }


def test_selective_date_import_preserves_manual_plan_and_pending_suggestions(
    selective_import,
):
    observations, expected = selective_import
    verify_selective_date_import(*observations, **expected)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda observations: observations[3]["configuration"]["financials"][
            "allocations"
        ][0]["amount"].update(amount="100.00"),
        lambda observations: observations[3]["configuration"]["financials"][
            "allocations"
        ][0].update(period_end="2027-01-01"),
        lambda observations: observations[3]["provenance"][0].update(state="accepted"),
        lambda observations: observations[3]["provenance"][1].update(source_refs=[]),
        lambda observations: observations[4]["proposals"].clear(),
        lambda observations: observations[5].update(revision=4),
    ],
)
def test_selective_date_import_rejects_inconsistent_observations(
    selective_import, mutation
):
    observations, expected = selective_import
    mutation(observations)
    with pytest.raises(AssertionError):
        verify_selective_date_import(*observations, **expected)


@pytest.fixture
def denied_snapshot(selective_import):
    observations, _ = selective_import
    snapshot = {
        "profile": observations[0],
        "session": observations[1],
        "operations": [],
        "funding": {"approvals": [], "limits": []},
        "actuals": [],
        "fx": [],
    }
    return snapshot, deepcopy(snapshot)


@pytest.mark.parametrize(
    ("case", "status"),
    [("stale", 412), ("expired", 410), ("revoked", 403), ("invalid_period", 422)],
)
def test_financial_denial_preserves_every_authoritative_effect(
    denied_snapshot, case, status
):
    before, after = denied_snapshot
    response = {"status": status, "body": {"detail": {"code": "rejected"}}}
    verify_financial_denial(
        before, response, after, case=case, source_markers=["private-source-id"]
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda after: after["profile"]["configuration"]["financials"]["allocations"][0][
            "amount"
        ].update(amount="100.00"),
        lambda after: after["profile"]["provenance"].append(
            {"paths": [DATE_PATH], "state": "accepted"}
        ),
        lambda after: after["session"].update(revision=3),
        lambda after: after["operations"].append({"kind": "confirm"}),
        lambda after: after["funding"]["limits"].append({"amount": "75.00"}),
        lambda after: after["actuals"].append({"amount": "75.00"}),
        lambda after: after["fx"].append({"currency": "EUR"}),
    ],
)
def test_financial_denial_rejects_partial_effects(denied_snapshot, change):
    before, after = denied_snapshot
    change(after)
    with pytest.raises(AssertionError):
        verify_financial_denial(
            before,
            {"status": 412, "body": {"detail": {"code": "stale"}}},
            after,
            case="stale",
            source_markers=[],
        )


def test_financial_denial_rejects_disclosure_and_wrong_error(denied_snapshot):
    before, after = denied_snapshot
    for response in (
        {"status": 200, "body": {"detail": {"code": "ok"}}},
        {
            "status": 403,
            "body": {"detail": {"code": "denied", "source": "private-source-id"}},
        },
    ):
        with pytest.raises(AssertionError):
            verify_financial_denial(
                before,
                response,
                after,
                case="revoked",
                source_markers=["private-source-id"],
            )


def test_financial_denial_needs_effect_readback(denied_snapshot):
    before, after = denied_snapshot
    del after["funding"]
    with pytest.raises(AssertionError, match="independent"):
        verify_financial_denial(
            before,
            {"status": 403, "body": {"detail": {"code": "denied"}}},
            after,
            case="revoked",
            source_markers=[],
        )


def progress_choice(choice):
    checks = [
        {"id": "information", "status": "complete", "required": True},
        {"id": "project_scope", "status": "complete", "required": True},
        {"id": "financials", "status": choice, "required": False},
    ]
    step = {"status": choice}
    if choice == "not_applicable":
        step["reason"] = checks[-1]["reason"] = "Planning deferred"
    progress = {
        "rule_version": "profile-1",
        "checks": checks,
        "completed": 2,
        "applicable": 3 if choice == "later" else 2,
        "percentage": 66 if choice == "later" else 100,
    }
    return {"progress": progress, "steps": {"financials": step}}, {
        "progress": deepcopy(progress),
        "configuration": {
            "financials": {
                "allocations": [
                    {
                        "approval_status": "proposed",
                        "amount": {"amount": "75.00", "currency": "USD"},
                    }
                ]
            }
        },
    }


def test_financial_progress_counts_later_but_excludes_not_applicable():
    verify_financial_progress(
        *progress_choice("later"), *progress_choice("not_applicable")
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda later, excluded: later[0]["progress"].update(percentage=67),
        lambda later, excluded: excluded[1]["progress"].update(applicable=3),
        lambda later, excluded: later[0]["steps"]["financials"].update(
            status="complete"
        ),
        lambda later, excluded: excluded[0]["steps"]["financials"].pop("reason"),
        lambda later, excluded: later[1]["configuration"]["financials"]["allocations"][
            0
        ].update(approval_status="approved"),
        lambda later, excluded: excluded[1]["configuration"].update(
            governance={"execution_policy_ref": {"id": "grant"}}
        ),
    ],
)
def test_financial_progress_rejects_false_completion_and_approval(mutation):
    later, excluded = progress_choice("later"), progress_choice("not_applicable")
    mutation(later, excluded)
    with pytest.raises(AssertionError):
        verify_financial_progress(*later, *excluded)


@pytest.fixture
def recovered_confirmation(selective_import):
    observations, expected = selective_import
    (
        before_profile,
        before_session,
        browser_session,
        after_profile,
        after_session,
        cli_session,
    ) = observations
    arguments = [
        before_profile,
        before_session,
        browser_session,
        cli_session,
        after_profile,
        after_session,
        deepcopy(after_session),
        [],
        [
            {
                "idempotency_key": "confirm-date-key-123",
                "action": "confirm",
                "confirmed_paths": [DATE_PATH],
            }
        ],
        {"revision": "source-1"},
        {"revision": "source-1"},
        {
            "run_id": "owned-run",
            "status": "verified",
            "owned_absent": True,
            "shared_unchanged": True,
            "receipt_ref": "private:cleanup",
        },
    ]
    expected.update(receipt_id="confirm-date-key-123", run_id="owned-run")
    return arguments, expected


def test_financial_recovery_reconciles_one_confirmation_and_owned_cleanup(
    recovered_confirmation,
):
    arguments, expected = recovered_confirmation
    verify_financial_resume(*arguments, **expected)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda arguments: arguments[6].update(revision=4),
        lambda arguments: arguments[8].append(deepcopy(arguments[8][0])),
        lambda arguments: arguments[8][0].update(
            confirmed_paths=[DATE_PATH, "/configuration/financials/allocations"]
        ),
        lambda arguments: arguments[10].update(revision="source-2"),
        lambda arguments: arguments[11].update(owned_absent=False),
    ],
)
def test_financial_recovery_refuses_duplicate_receipt_or_changed_source(
    recovered_confirmation, mutation
):
    arguments, expected = recovered_confirmation
    mutation(arguments)
    with pytest.raises(AssertionError):
        verify_financial_resume(*arguments, **expected)
