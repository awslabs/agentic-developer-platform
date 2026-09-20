"""Offline intervention omissions and retained audit evidence."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.report import now
from tests.e2e.orchestration.scenarios.audit import collect
from tests.e2e.orchestration.scenarios.delivery import Evidence
from tests.e2e.orchestration.scenarios.http import Unsupported


@pytest.fixture
def audit_session(valid_config):
    inventory = Inventory.create(
        valid_config.artifact_directory, "q-audit0123456789", valid_config.environment
    )
    inventory.record_planned(
        fixture_id="flow",
        kind="qualification-flow",
        intended_identity=inventory.qualification_id + "/flow",
        ownership_tags=valid_config.ownership_tags(inventory.qualification_id),
        idempotency_token="offline-flow",
    )
    inventory.mark_created("flow", "flow-id")
    graph = {
        "nodes": [
            {
                "id": "story",
                "node_ref": "first",
                "kind": "story",
                "execution_history": {"history_complete": True, "runs": []},
            },
            {"id": "gate", "node_ref": "release", "kind": "gate"},
        ]
    }
    decisions = [
        {
            "id": "approval",
            "node_id": "gate",
            "actor_kind": "human",
            "actor_id": "owner",
            "kind": "gate_approved",
            "created_at": now(),
        }
    ]
    client = Mock()
    client.get.side_effect = (
        lambda path: decisions if path.endswith("/decisions") else graph
    )
    client.pages.return_value = []
    session = NS(
        inventory=inventory,
        config=valid_config,
        client=client,
        interventions=[],
        evidence=Evidence(inventory),
    )
    return (
        session,
        graph,
        decisions,
        [{"pr": {"number": i}, "reviews": []} for i in (1, 2)],
    )


def test_real_decisions_are_attributed_and_preserved(audit_session):
    session, _, _, prs = audit_session
    observed = collect(session, prs)
    assert observed["entries"][0]["actor"] == "owner"
    assert observed["entries"][0]["kind"] == "planned_gate"
    assert observed["entries"][0]["target"] == "release"
    assert observed["complete"] is False
    assert "4539" in observed["remaining_boundary"]


@pytest.mark.parametrize(
    "kind", ["node_resumed", "halt_overridden", "replan_requested", "plan_amended"]
)
def test_unplanned_progress_is_never_erased(audit_session, kind):
    session, _, decisions, prs = audit_session
    decisions[0]["kind"] = kind
    observed = collect(session, prs)
    assert observed["entries"][0]["kind"] in {"coordinator_retrigger", "unplanned"}


def test_missing_lineage_blocks_intervention_completeness(audit_session):
    session, graph, _, prs = audit_session
    graph["nodes"][0]["execution_history"]["history_complete"] = False
    with pytest.raises(Unsupported, match="history"):
        collect(session, prs)


def test_fault_journal_cannot_disappear_from_accounting(audit_session):
    session, _, _, prs = audit_session
    (session.inventory.path.parent / "fault-worker-loss.json").write_text("{}")
    with pytest.raises(Unsupported, match="unaccounted"):
        collect(session, prs)
