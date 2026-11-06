"""Legacy migration is additive, audited, idempotent and tenant-scoped on PostgreSQL."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from runpy import run_path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration import legacy_binding_apply
from src.orchestration.compile import ApprovalContext, ProposalRejectedError
from src.orchestration.legacy_binding_apply import LegacyMigrationError, LegacyMigrationRequest, RepositoryTarget, apply_legacy_migration
from src.orchestration.legacy_binding_migration import inventory_legacy_bindings
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationLegacyMigration,
    OrchestrationNode,
    OrchestrationTrackerPublication,
    OrchestrationWorkClaim,
)
from src.orchestration.repository_binding import VerifiedRepository
from src.products.models import ProductProjectBinding, Project
from src.shared.models.organization import Organization
from tests.migrations.conftest_postgres import run_alembic, to_async_url, upgrade
from tests.migrations.head_contract import HEAD_DOWNGRADE_REFUSAL
from tests.orchestration.legacy_plan import persist_legacy_plan
from tests.orchestration.product_fixture import seed_product
from tests.orchestration.test_compile import valid_proposal


@pytest.fixture
async def legacy(pg_url):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actor = ApprovalContext(org_id="org-alpha", actor_id="operator-sub", actor_role="org_admin")
    async with sessions() as session:
        session.add(Organization(id=actor.org_id, name="Fictional workspace"))
        await session.flush()
        await seed_product(session, org_id=actor.org_id, actor_id=actor.actor_id, product_id="product-alpha")
        session.add(
            Project(
                tenant_id=actor.org_id,
                id="project-alpha",
                name="Fixture Project",
                owner_user_id=actor.actor_id,
                configuration={"governance": {"criticality": "low"}},
            )
        )
        await session.flush()
        session.add(
            ProductProjectBinding(
                tenant_id=actor.org_id,
                product_id="product-alpha",
                project_id="project-alpha",
                relationship_revision=1,
                valid_from=datetime.now(UTC),
                recorded_at=datetime.now(UTC),
                provenance={"source": "test"},
            )
        )
        proposal = valid_proposal(product_id=None)
        accepted = await persist_legacy_plan(session, proposal, actor)
        flow = await session.get(OrchestrationFlow, accepted.flow_id)
        flow.execution_paused = False
        await session.commit()
    yield sessions, accepted, actor, pg_url
    await engine.dispose()


def _request(accepted, **changes):
    return LegacyMigrationRequest(
        request_id=changes.get("request_id", "product-request-1"),
        expected_plan_version=changes.get("expected_plan_version", accepted.plan_version),
        expected_plan_hash=changes.get("expected_plan_hash", accepted.plan_hash),
        reason="Operator reviewed explicit provenance",
        product_id=changes.get("product_id", "product-alpha"),
        repository=changes.get("repository"),
    )


async def test_product_attribution_replays_without_mutating_accepted_or_assignment(legacy):
    sessions, accepted, actor, pg_url = legacy
    async with sessions() as session:
        flow = await session.get(OrchestrationFlow, accepted.flow_id)
        nodes_before = [
            (node.id, node.state, node.attempts, node.provider_repository_id)
            for node in (await session.scalars(select(OrchestrationNode).order_by(OrchestrationNode.id))).all()
        ]
        original = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))
        document, document_hash = original.plan_document, original.plan_hash
        request = _request(accepted)
        first = await apply_legacy_migration(session, org_id=actor.org_id, flow_id=flow.id, actor=actor, request=request)
        await session.commit()
    async with sessions() as session:
        replay = await apply_legacy_migration(session, org_id=actor.org_id, flow_id=accepted.flow_id, actor=actor, request=request)
        assert replay.receipt_id == first.receipt_id and replay.already_applied
        flow = await session.get(OrchestrationFlow, accepted.flow_id)
        assert flow.product_id is None and flow.execution_paused is False
        plans = (await session.scalars(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))).all()
        assert len(plans) == 1 and plans[0].plan_document == document and plans[0].plan_hash == document_hash
        assert [
            (node.id, node.state, node.attempts, node.provider_repository_id)
            for node in (await session.scalars(select(OrchestrationNode).order_by(OrchestrationNode.id))).all()
        ] == nodes_before
        assert len((await session.scalars(select(OrchestrationLegacyMigration))).all()) == 1
        assert len((await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "product_attributed"))).all()) == 1
        preview = (await inventory_legacy_bindings(session, org_id=actor.org_id))[0]
        assert preview["product_missing"] is True and preview["attributed_product_id"] == "product-alpha"
        with pytest.raises(LegacyMigrationError, match="accepted_plan_stale"):
            await apply_legacy_migration(
                session,
                org_id=actor.org_id,
                flow_id=flow.id,
                actor=actor,
                request=_request(accepted, request_id="new-request", expected_plan_version=0),
            )
        with pytest.raises(LegacyMigrationError, match="request_id_reused"):
            await apply_legacy_migration(
                session, org_id=actor.org_id, flow_id=flow.id, actor=actor, request=_request(accepted, product_id="different-product")
            )
        assert await inventory_legacy_bindings(session, org_id="different-tenant") == []
    rejected_downgrade = run_alembic(pg_url, "downgrade", "130_shared_publication_intent")
    assert rejected_downgrade.returncode != 0
    assert HEAD_DOWNGRADE_REFUSAL in rejected_downgrade.stderr


async def test_verified_untouched_binding_amends_once_and_rejects_ambiguous_target(legacy, monkeypatch):
    sessions, accepted, actor, _ = legacy

    async def verified(_session, *, tenant_id, name, issue, operation, repository_id, installation_id):
        assert tenant_id == actor.org_id and issue == 4196 and operation == "accept"
        return VerifiedRepository("github", repository_id, name, installation_id, name, issue, operation)

    monkeypatch.setattr(legacy_binding_apply, "resolve_story_repository", verified)
    async with sessions() as session:
        story = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.node_ref == "story-a"))
        request = _request(
            accepted,
            request_id="binding-request-1",
            product_id=None,
            repository=RepositoryTarget(node_id=story.id, name="fictional/alpha", repository_id=123, installation_id=42),
        )
        result = await apply_legacy_migration(session, org_id=actor.org_id, flow_id=accepted.flow_id, actor=actor, request=request)
        assert result.result_plan_version == accepted.plan_version + 1
        await session.commit()
    async with sessions() as session:
        replay = await apply_legacy_migration(session, org_id=actor.org_id, flow_id=accepted.flow_id, actor=actor, request=request)
        assert replay.receipt_id == result.receipt_id and replay.already_applied
        plans = (
            await session.scalars(
                select(OrchestrationAcceptedPlan)
                .where(OrchestrationAcceptedPlan.flow_id == accepted.flow_id)
                .order_by(OrchestrationAcceptedPlan.version)
            )
        ).all()
        assert len(plans) == 2 and plans[0].plan_hash == accepted.plan_hash
        assert "repository_identity" not in plans[0].plan_document["nodes"][0] or plans[0].plan_document["nodes"][0]["repository_identity"] is None
        assert plans[1].plan_document["nodes"][0]["provider_repository_id"] == 123
        with pytest.raises(LegacyMigrationError, match="accepted_plan_stale"):
            await apply_legacy_migration(
                session,
                org_id=actor.org_id,
                flow_id=accepted.flow_id,
                actor=actor,
                request=_request(accepted, request_id="other-binding", product_id=None, repository=request.repository),
            )


async def test_pending_publication_and_conflicting_repository_refuse_without_changes(legacy, monkeypatch):
    from src.orchestration.models import OrchestrationTrackerPublication

    sessions, accepted, actor, _ = legacy

    async def verified(_session, *, tenant_id, name, issue, operation, repository_id, installation_id):
        return VerifiedRepository("github", repository_id, name, installation_id, name, issue, operation)

    monkeypatch.setattr(legacy_binding_apply, "resolve_story_repository", verified)
    async with sessions() as session:
        story = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.node_ref == "story-a"))
        story.provider_repository_id = 555
        session.add(
            OrchestrationTrackerPublication(
                id="pending-pub",
                org_id=actor.org_id,
                flow_id=accepted.flow_id,
                repository_id=555,
                epic_ref="epic-1",
                repo="fictional/other",
                installation_id=42,
                issue_number=1,
                body_region="pending",
                version=1,
                watermark=1,
            )
        )
        await session.commit()
    async with sessions() as session:
        request = _request(
            accepted,
            request_id="conflict-target-1",
            product_id=None,
            repository=RepositoryTarget(node_id=story.id, name="fictional/alpha", repository_id=123, installation_id=42),
        )
        with pytest.raises(LegacyMigrationError, match="pending_publication_reconciliation_required"):
            await apply_legacy_migration(session, org_id=actor.org_id, flow_id=accepted.flow_id, actor=actor, request=request)
        publication = await session.get(OrchestrationTrackerPublication, "pending-pub")
        publication.completed_at = datetime.now(UTC)
        await session.commit()
    async with sessions() as session:
        with pytest.raises(LegacyMigrationError, match="projection_conflicts_with_verified_target"):
            await apply_legacy_migration(session, org_id=actor.org_id, flow_id=accepted.flow_id, actor=actor, request=request)
        with pytest.raises(ProposalRejectedError, match="inaccessible"):
            await apply_legacy_migration(
                session,
                org_id=actor.org_id,
                flow_id=accepted.flow_id,
                actor=actor,
                request=_request(accepted, request_id="unowned-product", product_id="other-product"),
            )
        assert (await session.scalars(select(OrchestrationLegacyMigration))).all() == []
        plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == accepted.flow_id))
        assert plan.version == accepted.plan_version and plan.plan_hash == accepted.plan_hash


async def test_scn_04_rollback_preserves_multi_repository_assignments_and_refuses_old_writers(legacy, monkeypatch, tmp_path):
    sessions, accepted, actor, pg_url = legacy
    async with sessions() as session:
        node = await session.scalar(
            select(OrchestrationNode).where(OrchestrationNode.flow_id == accepted.flow_id, OrchestrationNode.node_ref == "story-a")
        )
        node.repository_identity = "fictional/alpha"
        node.provider_repository_id = 123
        node.repository_connection = {"provider": "github", "installation_id": 42}
        node.attempts = 1
        untouched = await session.scalar(
            select(OrchestrationNode).where(OrchestrationNode.flow_id == accepted.flow_id, OrchestrationNode.node_ref == "story-b")
        )
        untouched.repository_identity = "fictional/beta"
        untouched.provider_repository_id = 456
        untouched.repository_connection = {"provider": "github", "installation_id": 43}
        claim = OrchestrationWorkClaim(
            org_id=actor.org_id,
            provider_repository_id=123,
            issue_number=4196,
            owner_kind="engine_flow",
            owner_ref=accepted.flow_id,
            generation=7,
            state="held",
        )
        session.add(claim)
        await session.flush()
        execution = OrchestrationExecution(
            org_id=actor.org_id,
            flow_id=accepted.flow_id,
            node_id=node.id,
            phase="prepare",
            status="running",
            claim_id=claim.id,
            claim_generation=7,
            accepted_plan_version=accepted.plan_version,
        )
        session.add(execution)
        await session.flush()
        action = OrchestrationAction(
            id="pending-action",
            org_id=actor.org_id,
            execution_id=execution.id,
            operation_key="open_pr:story-a:cycle-1",
            kind="open_pr",
            status="unknown",
            attempt=1,
        )
        session.add(action)
        session.add(
            OrchestrationTrackerPublication(
                id="pending-publication",
                org_id=actor.org_id,
                flow_id=accepted.flow_id,
                repository_id=123,
                epic_ref="epic-1",
                repo="fictional/alpha",
                installation_id=42,
                issue_number=4196,
                body_region="pending",
                version=1,
                watermark=1,
            )
        )
        await session.commit()

    async def snapshot():
        async with sessions() as session:
            plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == accepted.flow_id))
            node = await session.scalar(
                select(OrchestrationNode).where(OrchestrationNode.flow_id == accepted.flow_id, OrchestrationNode.node_ref == "story-a")
            )
            untouched = await session.scalar(
                select(OrchestrationNode).where(OrchestrationNode.flow_id == accepted.flow_id, OrchestrationNode.node_ref == "story-b")
            )
            claim = await session.scalar(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.owner_ref == accepted.flow_id))
            decisions = (await session.scalars(select(OrchestrationDecision.id).where(OrchestrationDecision.flow_id == accepted.flow_id))).all()
            actions = (await session.scalars(select(OrchestrationAction.id).where(OrchestrationAction.org_id == actor.org_id))).all()
            publication = await session.get(OrchestrationTrackerPublication, "pending-publication")
            return (
                plan.version,
                plan.plan_hash,
                plan.plan_document,
                node.repository_identity,
                node.provider_repository_id,
                node.repository_connection,
                node.attempts,
                untouched.repository_identity,
                untouched.provider_repository_id,
                untouched.repository_connection,
                untouched.state,
                untouched.attempts,
                claim.id,
                claim.generation,
                claim.state,
                decisions,
                actions,
                publication.id,
                publication.completed_at,
            )

    before = await snapshot()
    monkeypatch.setenv("DATABASE_URL", to_async_url(pg_url))
    script = run_path(str(Path(__file__).resolve().parents[2] / "scripts/legacy-binding-migration.py"))
    for reader in ("below-floor", "unknown"):
        output = tmp_path / f"{reader}.json"
        with pytest.raises(ValueError, match="keep gateway/tick writers quiesced"):
            await script["run"](actor.org_id, accepted.flow_id, output, reader)
        report = json.loads(output.read_text())
        assert output.stat().st_mode & 0o777 == 0o600
        assert report["rollback"]["writer_refusal"] is True
        assert report["rollback"]["pending_action_ids"] == ["pending-action"]
        assert report["rollback"]["pending_publication_ids"] == ["pending-publication"]
        assert report["inventory"][0]["accepted_plan_hash"] == accepted.plan_hash
        assert {node["classification"] for node in report["inventory"][0]["nodes"]} >= {
            "legacy_binding_unresolved",
            "untouched_target_requires_verification",
        }
        assert await snapshot() == before
    compatible = tmp_path / "compatible.json"
    await script["run"](actor.org_id, accepted.flow_id, compatible, "compatible")
    compatible_report = json.loads(compatible.read_text())
    assert compatible_report["rollback"]["writer_refusal"] is False
    assert compatible_report["rollback"]["pending_action_ids"] == ["pending-action"]
    assert compatible_report["rollback"]["required_action"] == "verify_image_and_reconcile_pending_effects"
    assert await snapshot() == before
    command_output = tmp_path / "command.json"
    command = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "scripts/legacy-binding-migration.py"),
            "--org-id",
            actor.org_id,
            "--flow-id",
            accepted.flow_id,
            "--output",
            str(command_output),
            "--candidate-reader",
            "below-floor",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert command.returncode == 2
    assert "keep gateway/tick writers quiesced" in command.stderr
    assert json.loads(command_output.read_text())["rollback"]["writer_refusal"] is True
    assert await snapshot() == before
    rejected = run_alembic(pg_url, "downgrade", "130_shared_publication_intent")
    assert rejected.returncode != 0
    assert HEAD_DOWNGRADE_REFUSAL in rejected.stderr
    assert await snapshot() == before


def test_empty_legacy_migration_table_cannot_be_downgraded(pg_url):
    upgrade(pg_url, "131_legacy_binding_migration")
    rejected = run_alembic(pg_url, "downgrade", "130_shared_publication_intent")
    assert rejected.returncode != 0
    assert "Retain legacy migration decisions and expanded schema" in rejected.stderr


async def test_operator_route_gates_dry_run_and_apply_before_database_read(legacy, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    from types import SimpleNamespace

    from fastapi import FastAPI, HTTPException
    from httpx import ASGITransport, AsyncClient

    from src.auth.dependencies import get_current_user
    from src.orchestration import legacy_binding_routes, routes
    from src.shared.database import get_db

    sessions, accepted, actor, _ = legacy
    allowed = False

    class Access:
        def __init__(self, _session):
            pass

        async def check_permission(self, _current_user, _permission, *, target_org_id):
            assert target_org_id == actor.org_id
            if not allowed:
                raise HTTPException(status_code=403, detail="permission denied")

    async def database():
        async with sessions() as session:
            yield session

    async def role(_access, _user):
        return "org_admin"

    monkeypatch.setattr(legacy_binding_routes, "AccessControl", Access)
    monkeypatch.setattr(routes, "_resolve_actor_role", role)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(org_id=actor.org_id, user_id=actor.actor_id, cognito_username="operator")
    app.dependency_overrides[get_db] = database
    path = f"/orchestration/flows/{accepted.flow_id}/legacy-migration"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.post(path, json=_request(accepted).model_dump(mode="json"))
        assert denied.status_code == 403
        denied_read = await client.get(path)
        assert denied_read.status_code == 403
        allowed = True
        preview = await client.get(path)
        assert preview.status_code == 200 and preview.json()["product_missing"]
        result = await client.post(path, json=_request(accepted).model_dump(mode="json"))
        assert result.status_code == 200 and result.json()["already_applied"] is False
        replay = await client.post(path, json=_request(accepted).model_dump(mode="json"))
        assert replay.status_code == 200 and replay.json()["receipt_id"] == result.json()["receipt_id"]
    async with sessions() as session:
        assert len((await session.scalars(select(OrchestrationLegacyMigration))).all()) == 1
