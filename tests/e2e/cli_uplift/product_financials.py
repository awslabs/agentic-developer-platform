"""Independent assertions for financial onboarding client journeys."""

import json

DATE_PATH = "/configuration/financials/release_target_date"
DENIAL_STATUSES = {"stale": 412, "expired": 410, "revoked": 403, "invalid_period": 422}


def verify_selective_date_import(
    before_profile,
    before_session,
    client_session,
    after_profile,
    after_session,
    independent_session,
    *,
    proposal_id,
    expected_date,
    source_ref,
):
    """Compare client confirmation with separately fetched profile and session."""
    assert client_session == after_session == independent_session
    assert after_session["revision"] == before_session["revision"] + 1
    assert after_profile["revision"] == after_session["revision"]
    assert after_session["id"] == before_session["id"]
    assert all(
        after_session[key] == before_session[key]
        for key in ("tenant_id", "product_id", "project_id")
    )

    before_financials = before_profile["configuration"]["financials"]
    assert before_financials["allocations"]
    assert all(
        allocation["amount"] == {"amount": "75.00", "currency": "USD"}
        and allocation["approval_status"] == "proposed"
        and allocation["period_start"] == "2026-10-01"
        and allocation["period_end"] == "2026-12-31"
        for allocation in before_financials["allocations"]
    )
    assert after_profile["configuration"] == {
        **before_profile["configuration"],
        "financials": {**before_financials, "release_target_date": expected_date},
    }
    assert after_profile["product"] == before_profile["product"]

    before_proofs = before_profile["provenance"]
    after_proofs = after_profile["provenance"]
    assert [proof for proof in after_proofs if DATE_PATH not in proof["paths"]] == [
        proof for proof in before_proofs if DATE_PATH not in proof["paths"]
    ]
    date_proofs = [proof for proof in after_proofs if DATE_PATH in proof["paths"]]
    assert len(date_proofs) == 1
    assert date_proofs[0]["paths"] == [DATE_PATH]
    assert date_proofs[0]["state"] == "accepted"
    assert date_proofs[0]["source_refs"] == [source_ref]
    assert date_proofs[0]["confirmed_by"]["id"]

    before_proposals = before_session["proposals"]
    selected = [
        proposal for proposal in before_proposals if proposal["id"] == proposal_id
    ]
    assert len(selected) == 1
    assert DATE_PATH in selected[0]["remaining_paths"]
    assert len(selected[0]["remaining_paths"]) > 1
    assert after_session["proposals"] == [
        {
            **proposal,
            "remaining_paths": [
                path for path in proposal["remaining_paths"] if path != DATE_PATH
            ],
        }
        if proposal["id"] == proposal_id
        else proposal
        for proposal in before_proposals
    ]


def verify_financial_denial(before, response, after, *, case, source_markers):
    """Reject a mutation only when every authoritative effect ledger is unchanged."""
    if case not in DENIAL_STATUSES:
        raise ValueError("Unknown financial denial case")
    if set(before) != {
        "profile",
        "session",
        "operations",
        "funding",
        "actuals",
        "fx",
    } or set(after) != set(before):
        raise AssertionError(
            "Financial denial needs independent profile, session and effect readbacks"
        )
    assert response["status"] == DENIAL_STATUSES[case]
    assert isinstance(response["body"]["detail"]["code"], str)
    assert response["body"]["detail"]["code"]
    assert all(marker not in json.dumps(response["body"]) for marker in source_markers)

    allocations = before["profile"]["configuration"]["financials"]["allocations"]
    assert allocations and all(
        allocation["amount"] == {"amount": "75.00", "currency": "USD"}
        and allocation["approval_status"] == "proposed"
        for allocation in allocations
    )
    assert before["funding"] == {"approvals": [], "limits": []}
    assert before["actuals"] == [] and before["fx"] == []
    for name in before:
        assert after[name] == before[name], f"Denied financial request changed {name}"


def verify_financial_progress(
    later_session, later_profile, excluded_session, excluded_profile
):
    """Derive completion from independent check states rather than the reported totals."""
    for choice, session, profile in (
        ("later", later_session, later_profile),
        ("not_applicable", excluded_session, excluded_profile),
    ):
        assert session["progress"] == profile["progress"]
        progress = session["progress"]
        assert progress["rule_version"] == "profile-1"
        checks = {check["id"]: check for check in progress["checks"]}
        assert len(checks) == len(progress["checks"])
        assert set(checks) == {"information", "project_scope", "financials"}
        assert all(
            checks[name]["status"] == "complete" and checks[name]["required"] is True
            for name in ("information", "project_scope")
        )
        assert (
            checks["financials"]["status"]
            == session["steps"]["financials"]["status"]
            == choice
        )
        assert checks["financials"]["required"] is False
        if choice == "not_applicable":
            assert checks["financials"].get("reason") == session["steps"][
                "financials"
            ].get("reason")
            assert checks["financials"].get("reason")
        completed = sum(check["status"] == "complete" for check in checks.values())
        applicable = sum(
            check["status"] != "not_applicable" for check in checks.values()
        )
        assert (
            progress["completed"],
            progress["applicable"],
            progress["percentage"],
        ) == (completed, applicable, 100 * completed // applicable)
        assert not (profile["configuration"].get("financials") or {}).get(
            "budget_policy_ref"
        )
        assert not (profile["configuration"].get("governance") or {}).get(
            "execution_policy_ref"
        )
        assert all(
            allocation["approval_status"] == "proposed"
            for allocation in (profile["configuration"].get("financials") or {}).get(
                "allocations", []
            )
        )


def verify_financial_resume(
    before_profile,
    before_session,
    browser_session,
    cli_session,
    after_profile,
    after_session,
    replay_session,
    before_receipts,
    after_receipts,
    source_before,
    source_after,
    cleanup,
    *,
    proposal_id,
    expected_date,
    source_ref,
    receipt_id,
    run_id,
):
    """Reconcile a lost acknowledgement against saved state and one confirmation receipt."""
    verify_selective_date_import(
        before_profile,
        before_session,
        browser_session,
        after_profile,
        after_session,
        cli_session,
        proposal_id=proposal_id,
        expected_date=expected_date,
        source_ref=source_ref,
    )
    assert replay_session == after_session
    assert source_before and source_after == source_before
    assert isinstance(before_receipts, list) and isinstance(after_receipts, list)
    assert len(after_receipts) == len(before_receipts) + 1
    assert len({receipt["idempotency_key"] for receipt in after_receipts}) == len(
        after_receipts
    )
    assert all(receipt in after_receipts for receipt in before_receipts)
    added = [receipt for receipt in after_receipts if receipt not in before_receipts]
    assert len(added) == 1
    assert added[0]["idempotency_key"] == receipt_id
    assert added[0]["action"] == "confirm"
    assert added[0]["confirmed_paths"] == [DATE_PATH]
    assert (
        cleanup.get("run_id") == run_id
        and cleanup.get("status") == "verified"
        and cleanup.get("owned_absent") is True
        and cleanup.get("shared_unchanged") is True
        and cleanup.get("receipt_ref")
    )
