"""Deployment progress reflects completed effects and actual worker liveness."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.provisioning_progress import project_progress

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def projection(*, next_phases=(), journals=(), state="running", live=True, active=False):
    workspace = SimpleNamespace(
        status="Active" if active else "Provisioning",
        cluster_id="cluster" if active else None,
        provisioning_operation_id="operation",
    )
    return project_progress(
        workspace=workspace,
        artifacts=[{"artifact_metadata_json": json.dumps({"next_phase": phase})} for phase in next_phases],
        journals=[{"progress_json": json.dumps(value)} for value in journals],
        operation={"state": state, "expires_at": NOW + timedelta(seconds=30 if live else -30)},
        now=NOW,
    )


def test_created_cluster_and_failed_bootstrap_retain_completed_steps():
    value = projection(next_phases=["apply-infrastructure", "bootstrap-workspace"], live=False,
                       journals=[{"phase": "revoked", "complete": True, "workspace-namespace": {"phase": "grant_intent"}}])
    assert value["completed"] == 3
    assert value["total"] == 6
    assert value["current_step"] == "configure"
    assert value["steps"][3]["state"] == "paused"
    assert all(step["state"] == "pending" for step in value["steps"][4:])


def test_expired_execution_is_never_shown_as_running():
    assert projection(live=False)["steps"][1]["state"] == "paused"
    assert projection(live=True)["steps"][1]["state"] == "running"


def test_completed_plan_waits_for_next_phase_approval():
    value = projection(next_phases=["apply-infrastructure"], state="succeeded")
    assert value["completed"] == 2
    assert value["steps"][2]["state"] == "waiting"
    assert "approve" in value["message"]


def test_revoked_access_alone_does_not_complete_configuration():
    value = projection(journals=[{"phase": "revoked", "complete": True}])
    assert value["steps"][3]["state"] == "pending"


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_terminal_failure_keeps_remaining_steps_pending(state):
    value = projection(state=state)
    assert value["steps"][1]["state"] == "failed"
    assert value["completed"] == 1


def test_active_registration_finishes_deployment_steps():
    value = projection(active=True, state="succeeded")
    assert value["completed"] == value["total"] == 6
    assert value["current_step"] is None
