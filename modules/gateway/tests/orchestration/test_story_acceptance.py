"""Acceptance remains truthful across polling, old history, and retained work."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.orchestration.delivery_progress import DeliveryProgress
from src.orchestration.story_acceptance import AcceptanceRecord, scope_digest, with_story_acceptance

NOW = datetime(2026, 11, 2, tzinfo=UTC)


def view(*, code_only=False, deployment=False, complete=False, current=True, head="a" * 40, record=True, automation="engine", status="waiting"):
    saved = AcceptanceRecord(
        node_id="story",
        cycle=2,
        plan_version=1,
        binding_id="binding",
        scope_digest=scope_digest("accepted-scope"),
        head_sha="a" * 40,
        merge_sha="b" * 40,
        code_only=code_only,
        merge_action_id="merge-ledger-id",
        merged_at=NOW,
        requirements_verified_at=NOW,
        deployment_verified_at=NOW if deployment else None,
        deployment_action_id="deploy-ledger-id" if deployment else None,
        completed_at=NOW if complete else None,
    )
    progress = DeliveryProgress(
        stage="complete" if complete else "deployment_pending",
        actor="engine",
        detail="Observing the accepted deployment.",
        next_action="Observe deployment",
        next_check_at=NOW.isoformat(),
        automation=automation,
    )
    return with_story_acceptance(
        progress,
        node=SimpleNamespace(kind="story", state="passed"),
        execution=SimpleNamespace(id="execution", status=status, phase="evaluation_pending" if deployment else "deployment_pending"),
        binding=SimpleNamespace(id="binding", head_sha=head, accepted_scope="accepted-scope"),
        records={"execution": saved} if record else {},
        current=current,
    ).acceptance


def test_merge_is_not_deployment_or_acceptance_and_completed_checks_stay_visible():
    result = view()
    assert [check.status for check in result.checks] == ["passed", "passed", "passed", "waiting", "pending"]
    assert result.status == "in_progress"
    verified = view(deployment=True)
    assert verified.checks[-2].status == "passed"
    assert verified.checks[-1].status == "waiting"
    assert "separate dependent evaluation" in verified.checks[-1].detail
    done = view(deployment=True, complete=True)
    assert all(check.status == "passed" for check in done.checks)
    assert done.next_action is None and done.next_check_at is None


def test_code_only_does_not_invent_deployment_requirements():
    result = view(code_only=True, complete=True)
    assert [check.id for check in result.checks] == ["merge", "review", "ci", "completion"]


@pytest.mark.parametrize("kwargs", [{"current": False}, {"head": "c" * 40}, {"record": False}])
def test_stale_or_missing_history_never_fabricates_passed_checks(kwargs):
    result = view(**kwargs)
    assert result.status == "history_unavailable" and result.checks == []
    assert "still in progress" in result.detail
    old = view(complete=True, **kwargs)
    assert old.detail == "Accepted previously; detailed checks were not recorded."


@pytest.mark.parametrize("automation,status,expected", [("paused", "waiting", "paused"), ("engine", "blocked", "blocked")])
def test_pause_and_block_do_not_erase_passed_checks(automation, status, expected):
    result = view(automation=automation, status=status)
    assert [check.status for check in result.checks[:3]] == ["passed"] * 3
    assert result.checks[3].status == expected
