"""Real assignment/claim/policy fences with canonical provider check selection."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration.merge_evidence import CheckEvidence, CheckRequirement, GitHubMergeObserver, RepositoryRequirements, evaluate_required_checks
from src.orchestration.models import OrchestrationWorkClaim
from src.orchestration.reviewer_checks import observe_reviewer_checks
from src.orchestration.run_reports import OrchestrationRunReport, RunReportError
from tests.orchestration.test_merge_controller import prepared_merge
from tests.orchestration.test_review_cycle import tick
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


def observation(head):
    return SimpleNamespace(
        head_sha=head,
        base_sha="b" * 40,
        open=True,
        merged=False,
        mergeable=True,
        mergeable_state="clean",
        checks=(),
        requirements=RepositoryRequirements((), 0, False, False, False, False),
    )


@pytest.mark.parametrize("case", ["none", "missing", "pending", "pass", "failed", "optional", "wrong_app", "legacy_failed"])
def test_same_ci_policy_as_merge(case):
    obs = observation("a" * 40)
    if case != "none":
        obs.requirements = replace(obs.requirements, checks=(CheckRequirement("unit", 9),), checks_declared=True)
    if case not in {"none", "missing"}:
        state = {"pending": "pending", "failed": "failure"}.get(case, "success")
        obs.checks = (CheckEvidence("unit", 8 if case == "wrong_app" else 9, state, 17, "check_run"),)
    if case == "optional":
        obs.checks += (CheckEvidence("optional", 9, "failure", 18, "check_run"),)
    if case == "legacy_failed":
        obs.checks += (CheckEvidence("unit", None, "failure", 18, "status"),)
    reasons, checks = evaluate_required_checks(obs)
    expected = {
        "missing": "required_check_missing",
        "wrong_app": "required_check_missing",
        "pending": "required_check_pending",
        "failed": "required_check_failed",
        "legacy_failed": "required_check_failed",
    }.get(case)
    assert [reason.value for reason in reasons] == ([expected] if expected else [])
    if case == "optional":
        assert len(checks) == 1


async def test_no_ci_report_uses_trusted_bound_pr_without_terminal_receipt(shared, monkeypatch):  # noqa: F811
    await tick(shared)
    envelope = shared.calls[-1]
    observe = AsyncMock(return_value=observation(shared.head))
    monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", observe)
    provider = SimpleNamespace(token=AsyncMock(return_value="scoped-token"))
    async with shared.factory() as db:
        result = await observe_reviewer_checks(db, credential=envelope["run_report"]["credential"], head_sha=shared.head, provider=provider)
        assert result["state"] == "passed" and result["checks"] == []
        row = await db.get(OrchestrationRunReport, envelope["message_id"])
        assert row.terminal_receipt is None and row.review_receipt is None
    assert observe.call_args.args[0].id == shared.binding.id
    assert provider.token.call_args.kwargs == {"evidence": True}


@pytest.mark.parametrize("change", ["head", "claim", "terminal"])
async def test_ci_observation_rechecks_current_assignment_after_provider_io(shared, monkeypatch, change):  # noqa: F811
    await tick(shared)
    envelope = shared.calls[-1]

    async def observe(self, binding):
        if change == "head":
            return observation("f" * 40)
        async with shared.factory() as db:
            if change == "claim":
                claim = await db.get(OrchestrationWorkClaim, shared.identity.claim_id)
                claim.active_run_id = "another-run"
            else:
                row = await db.get(OrchestrationRunReport, envelope["message_id"])
                row.terminal_receipt = {"outcome": "complete"}
            await db.commit()
        return observation(shared.head)

    monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", observe)
    async with shared.factory() as db:
        with pytest.raises(RunReportError):
            await observe_reviewer_checks(
                db,
                credential=envelope["run_report"]["credential"],
                head_sha=shared.head,
                provider=SimpleNamespace(token=AsyncMock(return_value="scoped-token")),
            )


@pytest.mark.parametrize("missing_evidence", [False, True])
async def test_active_reviewer_merge_permission_requires_accepted_evidence_and_never_merges(shared, monkeypatch, missing_evidence):  # noqa: F811
    from sqlalchemy import delete

    from src.orchestration.models import OrchestrationAction
    from src.orchestration.review_cycle import CycleBlockedError

    async with prepared_merge(shared, monkeypatch) as ctx:
        envelope = ctx.calls[-1]
        async with ctx.factory() as db:
            report = await db.get(OrchestrationRunReport, envelope["message_id"])
            report.terminal_receipt = None
            report.worker_receipt = {"ownership_nonce": "a" * 32}
            if missing_evidence:
                await db.execute(delete(OrchestrationAction).where(OrchestrationAction.kind == "review_evidence"))
            await db.commit()
        observed = await GitHubMergeObserver(ctx.merge_services.provider.client, "scoped-test-token", lambda: datetime.now(UTC)).observe(ctx.binding)
        monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", AsyncMock(return_value=observed))
        async with ctx.factory() as db:
            if missing_evidence:
                with pytest.raises(CycleBlockedError, match="fresh_verified_review_missing"):
                    await observe_reviewer_checks(
                        db,
                        credential=envelope["run_report"]["credential"],
                        head_sha=ctx.head,
                        for_merge=True,
                        provider=ctx.merge_services.provider,
                        storage=ctx.merge_services.storage,
                    )
            else:
                result = await observe_reviewer_checks(
                    db,
                    credential=envelope["run_report"]["credential"],
                    head_sha=ctx.head,
                    for_merge=True,
                    provider=ctx.merge_services.provider,
                    storage=ctx.merge_services.storage,
                )
                assert result["merge_state"] == "eligible" and result["merge_method"] == "squash"
                assert (await db.get(OrchestrationRunReport, envelope["message_id"])).terminal_receipt is None
        assert ctx.mutations == []
