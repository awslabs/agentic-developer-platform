"""Offline MR-12 scenarios against stored PostgreSQL flows and private inventory."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from runpy import run_path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration.compile import ApprovalContext
from src.orchestration.dispatch_pass import product_binding_hold
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
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.proposal import ProposedEdge, ProposedNode, RepositoryConnection
from src.orchestration.repository_binding import VerifiedRepository
from src.products.models import ProductProjectBinding, Project
from src.shared.models.organization import Organization
from tests.migrations.conftest_postgres import to_async_url, upgrade
from tests.orchestration.legacy_plan import persist_legacy_plan
from tests.orchestration.product_fixture import seed_product
from tests.orchestration.test_compile import address, valid_proposal


@pytest.fixture
async def stored_legacy(pg_url):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actor = ApprovalContext(org_id="tenant-scn-01", actor_id="operator-scn-01", actor_role="org_admin")
    async with sessions() as session:
        session.add(Organization(id=actor.org_id, name="Fictional workspace"))
        await session.flush()
        base = valid_proposal(org_id=actor.org_id, product_id=None)
        explicit = base.nodes[0].model_copy(
            update={
                "repository_identity": "fictional/accepted",
                "provider_repository_id": 101,
                "repository_connection": RepositoryConnection(provider="github", installation_id=11),
            }
        )
        started = ProposedNode(address=address("story-started"), kind="story", title="Started", issue_ref="4197")
        ambiguous = ProposedNode(address=address("story-ambiguous"), kind="story", title="Unresolved", issue_ref="4198")
        proposal = valid_proposal(
            org_id=actor.org_id,
            product_id=None,
            nodes=[explicit, base.nodes[1].model_copy(update={"issue_ref": "4199"}), *base.nodes[2:], started, ambiguous],
            edges=[
                *base.edges,
                ProposedEdge(from_address=started.address, to_address=address("eval")),
                ProposedEdge(from_address=ambiguous.address, to_address=address("eval")),
            ],
        )
        accepted = await persist_legacy_plan(session, proposal, actor)
        flow = await session.get(OrchestrationFlow, accepted.flow_id)
        flow.execution_paused = False
        explicit_node = await session.get(OrchestrationNode, accepted.node_ids[explicit.address])
        draft = await session.get(OrchestrationNode, accepted.node_ids[address("story-b")])
        draft.repository_identity = "fictional/untouched"
        draft.provider_repository_id = 202
        draft.repository_connection = {"provider": "github", "installation_id": 22}
        started_node = await session.get(OrchestrationNode, accepted.node_ids[started.address])
        started_node.attempts = 1
        ambiguous_node = await session.get(OrchestrationNode, accepted.node_ids[ambiguous.address])
        ambiguous_node.attempts = 1
        session.add(
            OrchestrationPullRequestBinding(
                org_id=actor.org_id,
                flow_id=accepted.flow_id,
                node_id=started_node.id,
                attempt=1,
                provider_repository_id=303,
                provider_pr_node_id="PR_started",
                repo="fictional/started",
                pr_number=7,
                installation_id=33,
                head_sha="c" * 40,
                registered_by="worker",
                registered_by_kind="service",
                state="active",
            )
        )
        claim = OrchestrationWorkClaim(
            org_id=actor.org_id,
            provider_repository_id=303,
            issue_number=4197,
            owner_kind="engine_flow",
            owner_ref=accepted.flow_id,
            generation=3,
            state="held",
        )
        session.add(claim)
        await session.flush()
        execution = OrchestrationExecution(
            org_id=actor.org_id,
            flow_id=accepted.flow_id,
            node_id=started_node.id,
            phase="prepare",
            status="running",
            claim_id=claim.id,
            claim_generation=claim.generation,
            accepted_plan_version=accepted.plan_version,
        )
        session.add(execution)
        await session.flush()
        session.add(
            OrchestrationAction(
                id="pending-scn-01",
                org_id=actor.org_id,
                execution_id=execution.id,
                operation_key="open_pr:story-started:cycle-1",
                kind="open_pr",
                status="unknown",
                attempt=1,
            )
        )
        await session.commit()
    try:
        yield sessions, engine, accepted, actor, explicit_node.id, pg_url
    finally:
        await engine.dispose()


async def test_scn_01_stored_inventory_is_private_read_only_and_separates_product(stored_legacy, monkeypatch, tmp_path):
    sessions, engine, accepted, actor, explicit_id, pg_url = stored_legacy

    async def snapshot():
        async with sessions() as session:
            plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == accepted.flow_id))
            nodes = (
                await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == accepted.flow_id).order_by(OrchestrationNode.id))
            ).all()
            claims = (await session.scalars(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.org_id == actor.org_id))).all()
            actions = (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.org_id == actor.org_id))).all()
            return (
                plan.plan_document,
                plan.plan_hash,
                [(node.id, node.state, node.attempts, node.repository_identity, node.provider_repository_id) for node in nodes],
                [(claim.id, claim.generation, claim.state) for claim in claims],
                [(action.id, action.status, action.operation_key) for action in actions],
            )

    before = await snapshot()
    writes = []

    def inspect_write(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("UPDATE ", "INSERT ", "DELETE ")):
            writes.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", inspect_write)
    try:
        async with sessions() as session:
            inventory = await inventory_legacy_bindings(session, org_id=actor.org_id)
            assert await inventory_legacy_bindings(session, org_id="other-tenant") == []
            assert await product_binding_hold(session, org_id=actor.org_id, flow_id=accepted.flow_id) is None
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", inspect_write)
    assert writes == []
    assert len(inventory) == 1
    report = inventory[0]
    assert report["product_missing"] is True and report["attributed_product_id"] is None
    assert report["accepted_plan_version"] == accepted.plan_version
    assert report["accepted_plan_hash"] == before[1]
    by_name = {node["address"].rsplit("/", 1)[-1]: node for node in report["nodes"]}
    assert by_name["story-a"]["node_id"] == explicit_id
    assert by_name["story-a"]["classification"] == "explicit_accepted_binding"
    assert by_name["story-b"]["classification"] == "untouched_target_requires_verification"
    assert by_name["story-started"]["classification"] == "started_provenance_requires_verification"
    assert by_name["story-started"]["pending_action_ids"] == ["pending-scn-01"]
    assert by_name["story-started"]["action"] == "reconcile_pending_effects"
    assert by_name["story-ambiguous"]["classification"] == "legacy_binding_unresolved"

    monkeypatch.setenv("DATABASE_URL", to_async_url(pg_url))
    script = run_path(str(Path(__file__).resolve().parents[2] / "scripts/legacy-binding-migration.py"))
    output = tmp_path / "private-scn-01.json"
    await script["run"](actor.org_id, accepted.flow_id, output)
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["inventory"] == inventory
    assert await snapshot() == before


@pytest.fixture
async def authorized_migration(stored_legacy):
    sessions, _, accepted, actor, _, _ = stored_legacy
    async with sessions() as session:
        draft = await session.scalar(
            select(OrchestrationNode).where(OrchestrationNode.flow_id == accepted.flow_id, OrchestrationNode.node_ref == "story-b")
        )
        draft.repository_identity = None
        draft.provider_repository_id = None
        draft.repository_connection = None
        await seed_product(session, org_id=actor.org_id, actor_id=actor.actor_id, product_id="product-scn-02")
        session.add(
            Project(
                tenant_id=actor.org_id,
                id="project-scn-02",
                name="Fictional Project",
                owner_user_id=actor.actor_id,
                configuration={"governance": {"criticality": "low"}},
            )
        )
        await session.flush()
        session.add(
            ProductProjectBinding(
                tenant_id=actor.org_id,
                product_id="product-scn-02",
                project_id="project-scn-02",
                relationship_revision=1,
                valid_from=datetime.now(UTC),
                recorded_at=datetime.now(UTC),
                provenance={"source": "test"},
            )
        )
        await session.commit()
    return sessions, accepted, actor


async def test_scn_02_authorized_replay_and_unresolved_migration_refusal(authorized_migration, monkeypatch):
    sessions, accepted, actor = authorized_migration
    product_request = LegacyMigrationRequest(
        request_id="scn02-product",
        expected_plan_version=accepted.plan_version,
        expected_plan_hash=accepted.plan_hash,
        reason="Explicit operator-reviewed Product attribution",
        product_id="product-scn-02",
    )
    async with sessions() as session:
        original = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == accepted.flow_id))
        original_document, original_hash = original.plan_document, original.plan_hash
        claim = await session.scalar(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.owner_ref == accepted.flow_id))
        original_claim = (claim.id, claim.generation, claim.state)
        started_binding = await session.scalar(
            select(OrchestrationPullRequestBinding).where(OrchestrationPullRequestBinding.flow_id == accepted.flow_id)
        )
        original_assignment = (started_binding.node_id, started_binding.provider_repository_id, started_binding.pr_number)
        with pytest.raises(LegacyMigrationError, match="pending_effect_reconciliation_required"):
            await apply_legacy_migration(session, org_id=actor.org_id, flow_id=accepted.flow_id, actor=actor, request=product_request)
        with pytest.raises(LegacyMigrationError, match="operator_authority_required"):
            await apply_legacy_migration(
                session,
                org_id=actor.org_id,
                flow_id=accepted.flow_id,
                actor=ApprovalContext(org_id="other-tenant", actor_id=actor.actor_id, actor_role=actor.actor_role),
                request=product_request,
            )
        assert (await session.scalars(select(OrchestrationLegacyMigration))).all() == []
        action = await session.get(OrchestrationAction, "pending-scn-01")
        action.status = "succeeded"
        action.receipt_ref = "observed-provider-effect-scn-02"
        action.observed_at = datetime.now(UTC)
        await session.commit()

    async def verified(_session, *, tenant_id, name, issue, operation, repository_id, installation_id):
        assert (tenant_id, name, issue, operation, repository_id, installation_id) == (
            actor.org_id,
            "fictional/untouched",
            4199,
            "accept",
            202,
            22,
        )
        return VerifiedRepository("github", repository_id, name, installation_id, name, issue, operation)

    monkeypatch.setattr("src.orchestration.legacy_binding_apply.resolve_story_repository", verified)
    async with sessions() as session:
        ambiguous = await session.scalar(
            select(OrchestrationNode).where(OrchestrationNode.flow_id == accepted.flow_id, OrchestrationNode.node_ref == "story-ambiguous")
        )
        with pytest.raises(LegacyMigrationError, match="untouched_story_required"):
            await apply_legacy_migration(
                session,
                org_id=actor.org_id,
                flow_id=accepted.flow_id,
                actor=actor,
                request=LegacyMigrationRequest(
                    request_id="scn02-ambiguous",
                    expected_plan_version=accepted.plan_version,
                    expected_plan_hash=accepted.plan_hash,
                    reason="Do not infer the started story's target",
                    repository=RepositoryTarget(node_id=ambiguous.id, name="fictional/guess", repository_id=404, installation_id=44),
                ),
            )
        first = await apply_legacy_migration(session, org_id=actor.org_id, flow_id=accepted.flow_id, actor=actor, request=product_request)
        assert first.result_plan_version == accepted.plan_version and not first.already_applied
        await session.commit()
    async with sessions() as session:
        replay = await apply_legacy_migration(session, org_id=actor.org_id, flow_id=accepted.flow_id, actor=actor, request=product_request)
        assert replay.receipt_id == first.receipt_id and replay.already_applied
        draft = await session.scalar(
            select(OrchestrationNode).where(OrchestrationNode.flow_id == accepted.flow_id, OrchestrationNode.node_ref == "story-b")
        )
        binding_request = LegacyMigrationRequest(
            request_id="scn02-binding",
            expected_plan_version=accepted.plan_version,
            expected_plan_hash=accepted.plan_hash,
            reason="Verified untouched repository identity and issue ownership",
            repository=RepositoryTarget(node_id=draft.id, name="fictional/untouched", repository_id=202, installation_id=22),
        )
        bound = await apply_legacy_migration(session, org_id=actor.org_id, flow_id=accepted.flow_id, actor=actor, request=binding_request)
        assert bound.result_plan_version == accepted.plan_version + 1 and not bound.already_applied
        await session.commit()
    async with sessions() as session:
        binding_replay = await apply_legacy_migration(session, org_id=actor.org_id, flow_id=accepted.flow_id, actor=actor, request=binding_request)
        assert binding_replay.receipt_id == bound.receipt_id and binding_replay.already_applied
        with pytest.raises(LegacyMigrationError, match="accepted_plan_stale"):
            await apply_legacy_migration(
                session,
                org_id=actor.org_id,
                flow_id=accepted.flow_id,
                actor=actor,
                request=binding_request.model_copy(update={"request_id": "scn02-stale"}),
            )
        plans = (
            await session.scalars(
                select(OrchestrationAcceptedPlan)
                .where(OrchestrationAcceptedPlan.flow_id == accepted.flow_id)
                .order_by(OrchestrationAcceptedPlan.version)
            )
        ).all()
        assert len(plans) == 2 and (plans[0].plan_document, plans[0].plan_hash) == (original_document, original_hash)
        assert next(node for node in plans[1].plan_document["nodes"] if node["address"] == address("story-b"))["provider_repository_id"] == 202
        claim = await session.scalar(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.owner_ref == accepted.flow_id))
        assert (claim.id, claim.generation, claim.state) == original_claim
        started_binding = await session.scalar(
            select(OrchestrationPullRequestBinding).where(OrchestrationPullRequestBinding.flow_id == accepted.flow_id)
        )
        assert (started_binding.node_id, started_binding.provider_repository_id, started_binding.pr_number) == original_assignment
        assert (await session.get(OrchestrationAction, "pending-scn-01")).receipt_ref == "observed-provider-effect-scn-02"
        flow = await session.get(OrchestrationFlow, accepted.flow_id)
        assert flow.product_id is None and flow.execution_paused is False
        inventory = (await inventory_legacy_bindings(session, org_id=actor.org_id))[0]
        assert inventory["attributed_product_id"] == "product-scn-02"
        assert (
            next(node for node in inventory["nodes"] if node["address"] == address("story-ambiguous"))["classification"]
            == "legacy_binding_unresolved"
        )
        assert (
            len((await session.scalars(select(OrchestrationLegacyMigration).where(OrchestrationLegacyMigration.flow_id == accepted.flow_id))).all())
            == 2
        )
        assert (
            len(
                (
                    await session.scalars(
                        select(OrchestrationDecision).where(
                            OrchestrationDecision.flow_id == accepted.flow_id, OrchestrationDecision.kind == "product_attributed"
                        )
                    )
                ).all()
            )
            == 1
        )
