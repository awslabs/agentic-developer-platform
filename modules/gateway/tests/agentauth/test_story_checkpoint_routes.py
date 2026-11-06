"""Real report storage/transport boundaries for private resumable progress."""

import copy
from types import SimpleNamespace

import pytest

from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.story_checkpoints import read_story_checkpoint, report_scope
from src.orchestration.story_run_insights import project_insight
from tests.agentauth import test_run_report_routes as fixtures

URL = fixtures.URL
reports = fixtures.reports


async def body_for(reports):
    async with reports.sessions() as session:
        row = await session.get(OrchestrationRunReport, reports.envelope["message_id"])
        row.persona = "operations"
        row.worker_receipt = {"run_id": row.run_id, "attempt": 1}
        row.dispatch_metadata = {
            **row.dispatch_metadata,
            "source_ref": {**row.dispatch_metadata["source_ref"], "issue": 50},
            "handoff_expect": {"accepted_plan_version": 1, "claim_generation": 1},
        }
        scope = (await report_scope(session, row)).model_dump()
        await session.commit()
    return {
        "generation": 1,
        "sequence": 1,
        "observed_at": "2026-10-31T10:00:00Z",
        "text": "- ☑ C1",
        "expected_checkpoint_revision": None,
        "story_checkpoint": {
            "scope": scope,
            "delta": {
                "version": 1,
                "progress_only": True,
                "source": {
                    "issue": 50,
                    "repository_id": 42,
                    "branch": "agent/issue-50",
                    "path": ".adp/tasks/50.json",
                    "sha256": "a" * 64,
                    "confirmed_source_revision": "b" * 40,
                },
                "updates": {"C1": {"base_task_hash": "d" * 64, "status": "done"}},
                "additions": {"NEW-1": {"id": "NEW-1", "kind": "infra", "title": "Select genuine targets", "status": "blocked"}},
                "conflicts": [],
                "history": [],
            },
        },
    }


async def test_cas_idempotent_replay_legacy_display_and_public_projection(reports):
    body = await body_for(reports)

    def send(value):
        return reports.client.post(URL + "/task-board", headers=reports.headers, json=value)

    first = await send(body)
    assert first.status_code == 200, first.text
    digest = first.json()["checkpoint_revision"]
    assert (await send(body)).json()["checkpoint_revision"] == digest
    changed = copy.deepcopy(body)
    changed["story_checkpoint"]["delta"]["updates"]["C1"]["status"] = "open"
    assert (await send(changed)).status_code == 409
    changed["expected_checkpoint_revision"] = digest
    changed["sequence"] = 2
    next_write = await send(changed)
    assert next_write.status_code == 200
    assert next_write.json()["checkpoint_revision"] != digest
    legacy = {key: body[key] for key in ("generation", "sequence", "observed_at", "text")}
    legacy.update(sequence=3, text="New display without structured data")
    assert (await send(legacy)).status_code == 200
    async with reports.sessions() as session:
        row = await session.get(OrchestrationRunReport, reports.envelope["message_id"])
        assert row.task_board_snapshot["story_checkpoint"]["revision"] == next_write.json()["checkpoint_revision"]
        assert row.task_board_snapshot["text"] == legacy["text"]
        public = project_insight(row, may_read_board=True).model_dump_json()
        assert "base_task_hash" not in public and "Select genuine targets" not in public
        assert legacy["text"] in public
    assert (await reports.client.post(URL + "/terminal", headers=reports.headers, json={"outcome": "failed"})).status_code == 200
    assert (await send(changed)).status_code == 409


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "foreign"),
        ("flow_id", "foreign"),
        ("node_id", "foreign"),
        ("run_id", "foreign"),
        ("attempt", 2),
        ("claim_generation", 2),
        ("accepted_plan_version", 2),
        ("installation_id", 999),
        ("repository", "other/repo"),
    ],
)
async def test_private_write_cannot_select_another_assignment(reports, field, value):
    body = await body_for(reports)
    body["story_checkpoint"]["scope"][field] = value
    response = await reports.client.post(URL + "/task-board", headers=reports.headers, json=body)
    assert response.status_code == 409
    async with reports.sessions() as session:
        assert (await session.get(OrchestrationRunReport, reports.envelope["message_id"])).task_board_snapshot is None


@pytest.mark.parametrize("fault", ["branch", "repository_id", "secret", "oversized", "duplicate_identity", "bool_version", "int_progress"])
async def test_bounded_schema_rejects_foreign_or_unsafe_progress(reports, fault):
    body = await body_for(reports)
    delta = body["story_checkpoint"]["delta"]
    if fault == "branch":
        delta["source"]["branch"] = "agent/issue-51"
    if fault == "repository_id":
        delta["source"]["repository_id"] = 99
    if fault == "secret":
        delta["additions"]["NEW-1"]["title"] = "Bearer do-not-store-this-value"
    if fault == "duplicate_identity":
        delta["additions"]["NEW-1"]["id"] = "another"
    if fault == "bool_version":
        delta["version"] = True
    if fault == "int_progress":
        delta["progress_only"] = 1
    if fault == "oversized":
        delta["additions"] = {f"N{i}": {"id": f"N{i}", "kind": "code", "status": "open", "title": "x" * 1000} for i in range(70)}
    response = await reports.client.post(URL + "/task-board", headers=reports.headers, json=body)
    assert response.status_code == 422


async def test_new_attempt_restores_only_scoped_prior_checkpoint(reports):
    body = await body_for(reports)
    first = await reports.client.post(URL + "/task-board", headers=reports.headers, json=body)
    assert first.status_code == 200
    async with reports.sessions() as session:
        old = await session.get(OrchestrationRunReport, reports.envelope["message_id"])
        new = OrchestrationRunReport(
            run_id="new-run",
            credential_hash="f" * 64,
            org_id=old.org_id,
            flow_id=old.flow_id,
            node_id=old.node_id,
            attempt=2,
            persona="agent-codex-operations",
            repo=old.repo,
            installation_id=old.installation_id,
            provider_repository_id=old.provider_repository_id,
            dispatch_metadata=copy.deepcopy(old.dispatch_metadata),
            expires_at=old.expires_at,
        )
        new.dispatch_metadata["handoff_expect"]["claim_generation"] = 2
        session.add(new)
        await session.flush()
        assigned = SimpleNamespace(
            invocation_id=new.run_id,
            tenant_id=new.org_id,
            node_id=new.node_id,
            attempt=2,
            claim_generation=2,
            accepted_plan_version=1,
            repository_id=42,
            installation_id=123,
            repository=new.repo,
            branch="agent/issue-50",
        )
        restored = await read_story_checkpoint(session, assigned)
        assert restored["state"] == "available"
        assert restored["checkpoint"]["revision"] == first.json()["checkpoint_revision"]
        assert restored["checkpoint"]["scope"]["run_id"] == old.run_id
        assigned.tenant_id = "foreign"
        assert (await read_story_checkpoint(session, assigned))["state"] == "unavailable"
