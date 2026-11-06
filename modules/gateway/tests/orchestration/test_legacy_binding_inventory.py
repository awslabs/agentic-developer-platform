"""Stored legacy work is classified without creating repository or Product authority."""

import pytest
from sqlalchemy import event, select

from src.orchestration.dispatch_pass import product_binding_hold
from src.orchestration.legacy_binding_migration import inventory_legacy_bindings
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationFlow, OrchestrationNode, OrchestrationPullRequestBinding


async def _node(session, flow, name, *, identity=None, repository_id=None, installation_id=None, attempts=0, state="pending"):
    node = OrchestrationNode(
        org_id=flow.org_id,
        flow_id=flow.id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref=name,
        kind="story",
        title=name,
        state=state,
        attempts=attempts,
        repository_identity=identity,
        provider_repository_id=repository_id,
        repository_connection={"provider": "github", "installation_id": installation_id} if installation_id else None,
    )
    session.add(node)
    await session.flush()
    return node


async def test_inventory_classifies_legacy_binding_independently_of_product_without_writes(db_session, test_engine):
    flow = OrchestrationFlow(org_id="tenant-a", slug="flow", title="Legacy", product_id=None, state="running", execution_paused=False)
    db_session.add(flow)
    await db_session.flush()
    explicit = await _node(db_session, flow, "explicit", identity="fictional/a", repository_id=101, installation_id=11)
    await _node(db_session, flow, "draft", identity="fictional/b", repository_id=202, installation_id=22)
    await _node(db_session, flow, "ambiguous", identity="fictional/c", attempts=1, state="running")
    accepted = OrchestrationAcceptedPlan(
        org_id=flow.org_id,
        flow_id=flow.id,
        version=6,
        plan_hash="a" * 64,
        plan_document={
            "nodes": [
                {
                    "address": "flow/epic/wave/explicit",
                    "kind": "story",
                    "repository_identity": "fictional/a",
                    "provider_repository_id": 101,
                    "repository_connection": {"provider": "github", "installation_id": 11},
                },
                {"address": "flow/epic/wave/draft", "kind": "story"},
                {"address": "flow/epic/wave/ambiguous", "kind": "story"},
            ]
        },
    )
    db_session.add(accepted)
    await db_session.flush()
    writes = []

    def inspect_write(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("UPDATE ", "INSERT ", "DELETE ")):
            writes.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", inspect_write)
    try:
        report = await inventory_legacy_bindings(db_session, org_id="tenant-a")
        assert await inventory_legacy_bindings(db_session, org_id="tenant-other") == []
        assert await product_binding_hold(db_session, org_id="tenant-a", flow_id=flow.id) is None
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", inspect_write)
    assert writes == []
    assert report[0]["product_missing"] is True
    assert report[0]["accepted_plan_hash"] == "a" * 64
    states = {item["address"].rsplit("/", 1)[-1]: item["classification"] for item in report[0]["nodes"]}
    assert states == {
        "explicit": "explicit_accepted_binding",
        "draft": "untouched_target_requires_verification",
        "ambiguous": "legacy_binding_unresolved",
    }
    assert (await db_session.scalars(select(OrchestrationAcceptedPlan))).one().plan_document == accepted.plan_document
    assert explicit.repository_identity == "fictional/a" and flow.product_id is None


async def test_started_provenance_requires_provider_verification_and_conflicts_remain_held(db_session):
    flow = OrchestrationFlow(org_id="tenant-b", slug="other", title="Other")
    db_session.add(flow)
    await db_session.flush()
    started = await _node(db_session, flow, "started", attempts=1)
    conflicted = await _node(db_session, flow, "conflict", identity="fictional/other", repository_id=404, installation_id=44, attempts=1)
    for node in (started, conflicted):
        db_session.add(
            OrchestrationPullRequestBinding(
                org_id=flow.org_id,
                flow_id=flow.id,
                node_id=node.id,
                attempt=1,
                provider_repository_id=303,
                provider_pr_node_id=f"PR_{node.node_ref}",
                repo="fictional/start",
                pr_number=9,
                installation_id=33,
                head_sha="b" * 40,
                registered_by="worker",
                registered_by_kind="service",
                state="active",
            )
        )
    await db_session.flush()
    rows = (await inventory_legacy_bindings(db_session, org_id=flow.org_id))[0]["nodes"]
    assert {row["address"].rsplit("/", 1)[-1]: row["classification"] for row in rows} == {
        "started": "started_provenance_requires_verification",
        "conflict": "legacy_binding_unresolved",
    }


def test_private_inventory_survives_restart_and_cannot_be_overwritten(tmp_path):
    import json
    import os
    from pathlib import Path
    from runpy import run_path

    script = Path(__file__).resolve().parents[2] / "scripts/legacy-binding-migration.py"
    writer = run_path(str(script))["write_private_inventory"]
    output = tmp_path / "private-inventory.json"
    writer(output, [{"flow_id": "fictional-flow", "pending_action_ids": ["action-one"]}])
    original = output.read_bytes()
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert json.loads(original)["inventory"][0]["pending_action_ids"] == ["action-one"]
    with pytest.raises(FileExistsError):
        writer(output, [{"flow_id": "another-flow"}])
    assert output.read_bytes() == original
