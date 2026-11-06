"""Accepted predecessor receipts feed the real scheduler dispatch and its ledger."""

import asyncio
import base64
import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import tests.orchestration.test_policy_admission as policy_fixtures
from src.orchestration.accepted_node import accepted_node_contract
from src.orchestration.deployment_manifest import DeploymentManifest, EntryStatus, ManifestEntry, WorkflowRef
from src.orchestration.deployment_workflow_provider import WorkflowProvider
from src.orchestration.dispatch_pass import publish_pending, run_dispatch_pass
from src.orchestration.merge_controller import MergeReceipt
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationEnvironmentLease,
    OrchestrationExecution,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.operations_shared_release import accepted_release_pins
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.state import NodeState
from src.orchestration.tick import run_tick
from tests.orchestration.test_policy_admission import (
    ORG_A,
    REPO,
    _config,
    _fixture,
    _make_node,
    _make_usage,
    _policy,
)

engine = policy_fixtures.engine
session = policy_fixtures.session
policy_budget_initializers = policy_fixtures.policy_budget_initializers
healthy_policy_reservations = policy_fixtures.healthy_policy_reservations
governed_work_ownership = policy_fixtures.governed_work_ownership

SOURCE = "b" * 40
FIRST = "a" * 40
BASELINE = "c" * 40
DEFINITION = "d" * 40


def _receipt(node, execution, operation_key, sha):
    before = datetime.now(UTC) - timedelta(minutes=5)
    return MergeReceipt(
        org_id=node.org_id,
        execution_id=execution.id,
        flow_id=node.flow_id,
        node_id=node.id,
        cycle=1,
        accepted_plan_version=1,
        claim_id=execution.claim_id,
        claim_generation=execution.claim_generation,
        repo=REPO,
        pr_number=41,
        provider_repository_id=12345,
        provider_pr_node_id="pr-node-41",
        reviewed_head_sha=sha,
        reviewed_base_sha=BASELINE,
        merge_sha=sha,
        review_ref="review-41",
        operation_key=operation_key,
        method="merge",
        eligibility_digest="f" * 64,
        eligibility_observed_at=before,
        merged_at=before + timedelta(minutes=1),
        observed_at=before + timedelta(minutes=2),
        adopted=False,
    )


async def _merged(session, flow, *, node_ref, sha):
    node = await _make_node(session, flow, node_ref=node_ref, state=NodeState.PASSED, attempts=1)
    execution = OrchestrationExecution(
        org_id=ORG_A,
        flow_id=flow.id,
        node_id=node.id,
        cycle=1,
        phase="delivering",
        status="concluded",
        accepted_plan_version=1,
        claim_id=f"claim-{node_ref}",
        claim_generation=1,
    )
    session.add(execution)
    await session.flush()
    key = f"merge:{node_ref}"
    receipt = _receipt(node, execution, key, sha)
    action = OrchestrationAction(
        org_id=ORG_A,
        execution_id=execution.id,
        operation_key=key,
        kind="merge_pull_request",
        status="succeeded",
        attempt=1,
        detail={"merge_receipt": receipt.model_dump(mode="json")},
    )
    session.add(action)
    session.add(
        OrchestrationDecision(
            org_id=ORG_A,
            flow_id=flow.id,
            node_id=node.id,
            kind="result_observed",
            actor_id="system:merge-controller",
            actor_kind="service",
            actor_role="engine",
            from_state="running",
            to_state="passed",
            reason=json.dumps({"attempt": 1, "merge_receipt": receipt.model_dump(mode="json")}),
        )
    )
    await _make_usage(session, node)
    await session.flush()
    return node, action


async def _dispatched_release(session, monkeypatch, *, retry=False, repair_authority=False, prepare_release=None, expected_dispatched=1):
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    monkeypatch.setenv("ADP_SHARED_RUN_REPORTING_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-only-operations-report-key")
    flow, node = await _fixture(
        session,
        policy=_policy(
            allowed_actions=["operate", "repair", "merge"] if repair_authority else ["operate", "repair"] if retry else ["operate"],
            human_gates=[],
            environment_connection_ids=["conn-1"],
            **(
                {"schema_version": 3, "repair_scope": "repository"}
                if repair_authority == "repository"
                else {"schema_version": 3, "repair_paths": ["docs/release.md"]}
                if repair_authority
                else {}
            ),
        ),
    )
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))
    approval = await session.scalar(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id))
    plan.accepted_by_decision_id = approval.id
    first, _ = await _merged(session, flow, node_ref="build-1", sha=FIRST)
    second, _ = await _merged(session, flow, node_ref="build-2", sha=SOURCE)
    addresses = [f"{flow.slug}/{item.epic_ref}/{item.wave_ref}/{item.node_ref}" for item in (first, second)]
    plan.plan_document = {
        **plan.plan_document,
        "edges": [],
        "nodes": [
            *[{"address": address, "kind": "story", "title": item.title} for address, item in zip(addresses, (first, second), strict=True)],
            {
                "address": f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}",
                "kind": "story",
                "title": node.title,
                "repository_identity": REPO,
                "executor": {"kind": "agent", "role": "operate", "persona": "operations"},
                "required_inputs": [
                    {"name": "workflow", "source": "deployment_workflow:gateway-deploy.yml"},
                    *[{"name": "build", "source": f"node:{address}"} for address in addresses],
                ],
                "evidence_expectations": ["runtime_verified:gateway-backend"],
                "operational_release_binding": {
                    "predecessors": [{"address": address, "receipt_kind": "verified_merge"} for address in addresses],
                    "build_prerequisites": addresses,
                    "repository": REPO,
                    "target": "env-1",
                    "component": "gateway-backend",
                    "workflows": ["gateway-deploy.yml"],
                    "default_branch": "main",
                },
            },
        ],
    }
    manifest = DeploymentManifest(
        schema_version=1,
        entries=(
            ManifestEntry(
                entry_id="env-1",
                status=EntryStatus.ENABLED,
                connection_id="conn-1",
                component_selectors=("gateway-backend",),
                workflow=WorkflowRef(path=".github/workflows/gateway-deploy.yml", definition_revision=DEFINITION),
                artifact_revision=BASELINE,
                resource_kind="eks-namespace",
                resource_id="cluster-dev/adp",
                verification_adapter="gateway-health-verification",
            ),
        ),
    )
    monkeypatch.setattr("src.orchestration.operations_release_resolution.load_packaged_manifest", lambda: manifest)

    async def contains(_binding, source, actual):
        if source == SOURCE and actual == FIRST:
            raise CycleBlockedError("not_an_ancestor")

    provider = SimpleNamespace(
        contains=AsyncMock(side_effect=contains),
        definition=AsyncMock(
            return_value=SimpleNamespace(
                approved_revision=DEFINITION,
                source_revision=SOURCE,
                dispatch_ref="main",
                dispatch_revision=SOURCE,
            )
        ),
    )
    monkeypatch.setattr("src.orchestration.operations_release_resolution.ReleaseProvider", lambda: provider)
    if prepare_release is not None:
        prepared_manifest = await prepare_release(flow, node, plan, manifest, provider)
        if prepared_manifest is not None:
            manifest = prepared_manifest
    first_pass = await run_dispatch_pass(session, _config())
    assert first_pass.dispatched == expected_dispatched and len(first_pass.pending) == expected_dispatched, [
        item.rejection_reason
        for item in (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "transition_rejected"))).all()
    ]
    return SimpleNamespace(flow=flow, node=node, plan=plan, provider=provider, manifest=manifest, first_pass=first_pass)


async def test_grouped_release_records_reviewed_mutation_role_before_dispatch(session, engine, monkeypatch, governed_work_ownership):  # noqa: F811
    role = "arn:aws:iam::000000000000:role/adp-dev-trusted-deployment"

    async def group(_flow, _node, _plan, manifest, _provider):
        return replace(manifest, entries=(replace(manifest.entries[0], release_group="atlassian-foundation", mutation_role_arn=role),))

    fixture = await _dispatched_release(session, monkeypatch, prepare_release=group)
    recorded = fixture.first_pass.pending[0].envelope["orchestration"]["release_binding"]
    assert recorded["mutation_role_arn"] == role
    row = await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_release_binding"))
    assert row.detail == recorded


@pytest.mark.parametrize("invalid", [None, "missing_ledger", "source", "definition", "metadata", "workflow", "unmerged_repair"])
async def test_activated_gateway_uses_verified_descendant_release_ledger(session, monkeypatch, governed_work_ownership, invalid):
    async def activate(_flow, _node, plan, manifest, provider):
        document = copy.deepcopy(plan.plan_document)
        release = document["nodes"][-1]["operational_release_binding"]
        release["definition_predecessor"] = release["predecessors"][-1]["address"]
        plan.plan_document = document
        provider.definition.return_value.approved_revision = SOURCE
        return replace(
            manifest,
            entries=(
                replace(
                    manifest.entries[0],
                    release_group="atlassian-foundation",
                    mutation_role_arn="arn:aws:iam::000000000000:role/adp-dev-trusted-deployment",
                ),
            ),
        )

    fixture = await _dispatched_release(session, monkeypatch, prepare_release=activate)
    metadata = copy.deepcopy(fixture.first_pass.pending[0].envelope)
    ledger = await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_release_binding"))
    execution = await session.get(OrchestrationExecution, ledger.execution_id)
    contract = await accepted_node_contract(session, fixture.node)
    expected = {"source_revision": SOURCE, "definition_revision": SOURCE, "default_branch": "main"}
    assert metadata["orchestration"]["deployment_revisions"] == expected
    assert ledger.detail["manifest_artifact_revision"] == BASELINE != SOURCE
    assert ledger.detail["manifest_definition_revision"] == DEFINITION != SOURCE
    assert (BASELINE, SOURCE) in [call.args[1:] for call in fixture.provider.contains.await_args_list]
    assert fixture.provider.definition.await_args.args[1].definition_revision == SOURCE
    assert len(ledger.detail["receipts"]) == 2
    if invalid == "missing_ledger":
        await session.delete(ledger)
    elif invalid in {"source", "definition"}:
        detail = copy.deepcopy(ledger.detail)
        detail["pins"][f"{invalid}_revision"] = "f" * 40
        ledger.detail = detail
    elif invalid == "metadata":
        metadata["orchestration"]["deployment_revisions"]["source_revision"] = "f" * 40
    elif invalid == "workflow":
        ledger.detail = {**ledger.detail, "workflow": ".github/workflows/unsupported.yml"}
        metadata["orchestration"]["release_binding"] = copy.deepcopy(ledger.detail)
    elif invalid == "unmerged_repair":
        session.add(
            OrchestrationAction(
                org_id=fixture.node.org_id,
                execution_id=execution.id,
                operation_key="repair-pr",
                kind="operations_repair_pr",
                status="succeeded",
                attempt=1,
                receipt_ref="repair-pr:1",
                detail={"head": "f" * 40},
            )
        )
    await session.flush()
    pins = await accepted_release_pins(
        session, node=fixture.node, execution=execution, contract=contract, report=SimpleNamespace(dispatch_metadata=metadata)
    )
    assert pins == (expected if invalid is None else None)


async def test_preserved_execution_resolves_its_repairs_after_plan_amendment(session, monkeypatch, governed_work_ownership):
    from src.orchestration.policy_admission import load_in_force_policy

    fixture = await _dispatched_release(session, monkeypatch, repair_authority=True)
    ledger = await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_release_binding"))
    execution = await session.get(OrchestrationExecution, ledger.execution_id)
    execution.phase = execution.status = "concluded"
    pins = ledger.detail["pins"]
    policy = (await load_in_force_policy(session, org_id=fixture.node.org_id, flow_id=fixture.flow.id)).policy
    session.add(
        OrchestrationAction(
            org_id=fixture.node.org_id,
            execution_id=execution.id,
            kind="operations_repair_merge",
            status="succeeded",
            attempt=1,
            operation_key="operations-repair:77:" + "e" * 40,
            receipt_ref=f"github/{REPO}/pull/77",
            detail={
                "pull_number": 77,
                "head": "e" * 40,
                "merge_sha": "f" * 40,
                "repository": REPO,
                "default_branch": "main",
                "policy_hash": policy.policy_hash,
                "workflows": ["gateway-deploy.yml"],
                "repair_paths": sorted(policy.repair_paths),
                "before_source_revision": pins["source_revision"],
                "before_definition_revision": pins["definition_revision"],
                "claim_generation": execution.claim_generation,
                "run_id": "original-assignment",
            },
        )
    )
    # Keep the original execution and immutable release ledger; the amendment
    # creates a new plan, not a synthetic execution at that version.
    fixture.plan.superseded_at = datetime.now(UTC)
    amendment = OrchestrationDecision(
        org_id=fixture.node.org_id,
        flow_id=fixture.flow.id,
        kind="plan_amended",
        actor_id=policy_fixtures.APPROVER,
        actor_role="org_admin",
        actor_kind="human",
        reason="Preserve operation",
    )
    session.add(amendment)
    await session.flush()
    document = copy.deepcopy(fixture.plan.plan_document)
    document["nodes"][-1]["title"] = "Retitled preserved operation"
    session.add(
        OrchestrationAcceptedPlan(
            org_id=fixture.node.org_id,
            flow_id=fixture.flow.id,
            version=2,
            plan_hash="hash-v2",
            plan_document=document,
            accepted_by_decision_id=amendment.id,
        )
    )
    await session.flush()
    contract = await accepted_node_contract(session, fixture.node)
    assert contract.plan_version == 2 and execution.accepted_plan_version == 1
    monkeypatch.setattr("src.orchestration.operations_release_resolution.operational_execution_pins", AsyncMock(return_value=pins))
    actual = await accepted_release_pins(
        session,
        node=fixture.node,
        execution=execution,
        contract=contract,
        report=SimpleNamespace(dispatch_metadata=fixture.first_pass.pending[0].envelope),
    )
    assert actual == {**pins, "source_revision": "f" * 40, "definition_revision": "f" * 40}


@pytest.mark.parametrize("provisioned", [False, True])
@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "missing_action",
        "changed_target",
        "changed_claim",
        "advanced_claim",
        "report_metadata",
        "report_scope",
        "report_hash",
        "report_expired",
        "report_started",
        "report_terminal",
        "digest",
    ],
)
async def test_restart_reconciles_same_release(session, engine, monkeypatch, governed_work_ownership, invalid, provisioned):  # noqa: F811
    from src.agentauth.bootstrap import envelope_digest
    from src.orchestration.run_reports import OrchestrationRunReport

    fixture = await _dispatched_release(session, monkeypatch)
    flow, node, provider, first_pass = fixture.flow, fixture.node, fixture.provider, fixture.first_pass
    envelope = first_pass.pending[0].envelope
    deduplication_id = first_pass.pending[0].deduplication_id
    node_id, flow_id = node.id, flow.id
    action = await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_release_binding"))
    execution = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node_id))
    claim = await session.get(OrchestrationWorkClaim, execution.claim_id)
    initial_claim_generation = claim.generation
    initial_leases = {lease.id for lease in (await session.scalars(select(OrchestrationEnvironmentLease))).all()}
    assert action.detail == envelope["orchestration"]["release_binding"]
    await session.commit()
    raw = {"status": {"S": "pending"}, "envelope_digest": {"S": envelope_digest(envelope)}} if provisioned else None
    store = SimpleNamespace(_read=Mock(return_value=raw))
    writer = SimpleNamespace(store=store, provision=lambda pending: pending.envelope)
    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: writer)
    client = SimpleNamespace(send_message=Mock(side_effect=TimeoutError("queue response lost")))
    publish_pending(first_pass, _config(), client=client)
    assert first_pass.publish_failed == 1 and client.send_message.call_count == 1
    provider.definition.side_effect = AssertionError("restart must not resolve latest main")
    provider.contains.side_effect = AssertionError("restart must not recheck latest main")
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as restarted:
        action = await restarted.get(OrchestrationAction, action.id)
        execution = await restarted.get(OrchestrationExecution, execution.id)
        if invalid == "missing_action":
            await restarted.delete(action)
        elif invalid == "changed_target":
            action.detail = {**action.detail, "target": "another-environment"}
        elif invalid == "changed_claim":
            execution.claim_generation += 1
        elif invalid == "advanced_claim":
            (await restarted.get(OrchestrationWorkClaim, claim.id)).generation += 1
        elif invalid and invalid.startswith("report_"):
            assignment = await restarted.get(OrchestrationRunReport, envelope["message_id"])
            if invalid == "report_metadata":
                assignment.dispatch_metadata = {**assignment.dispatch_metadata, "persona": "developer"}
            elif invalid == "report_scope":
                assignment.repo = "another/repository"
            elif invalid == "report_hash":
                assignment.credential_hash = "0" * 64
            elif invalid == "report_expired":
                assignment.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif invalid == "report_started":
                assignment.worker_receipt = {"started": True}
            elif invalid == "report_terminal":
                assignment.terminal_receipt = {"state": "failed"}
        elif invalid == "digest" and raw:
            raw["envelope_digest"] = {"S": "0" * 64}
        await restarted.commit()
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as restarted:
        recovered = await run_dispatch_pass(restarted, _config())
        assert recovered.dispatched == 0
        expected_replay = invalid is None or (invalid == "digest" and not provisioned)
        assert (len(recovered.pending) == 1) == expected_replay
        if expected_replay:
            assert recovered.pending[0].envelope == envelope
            assert recovered.pending[0].deduplication_id == deduplication_id
            concurrent = await run_dispatch_pass(restarted, _config())
            assert len(concurrent.pending) == 1 and concurrent.pending[0].envelope == recovered.pending[0].envelope
            assert concurrent.pending[0].deduplication_id == deduplication_id
            barrier = Barrier(2, timeout=5)

            def provision_retry(pending):
                barrier.wait()
                return pending.envelope

            writer.provision = provision_retry
            client.send_message.side_effect = None
            await asyncio.gather(
                asyncio.to_thread(publish_pending, recovered, _config(), client=client),
                asyncio.to_thread(publish_pending, concurrent, _config(), client=client),
            )
            assert client.send_message.call_count == 3
            assert {call.kwargs["MessageDeduplicationId"] for call in client.send_message.call_args_list} == {deduplication_id}
        else:
            assert client.send_message.call_count == 1
        assert (await restarted.get(OrchestrationWorkClaim, claim.id)).generation == initial_claim_generation + (invalid == "advanced_claim")
        assert {lease.id for lease in (await restarted.scalars(select(OrchestrationEnvironmentLease))).all()} == initial_leases
        assert (await restarted.get(OrchestrationNode, node_id)).attempts == 1
        decisions = (await restarted.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "node_dispatched"))).all()
        assert len(decisions) == 1
        assert "run_report" not in json.loads(decisions[0].reason)["dispatch_envelope"]
        assignment = await restarted.get(OrchestrationRunReport, envelope["message_id"])
        assert "run_report" not in assignment.dispatch_metadata
        assert (await restarted.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node_id))).all() == [
            await restarted.get(OrchestrationExecution, execution.id)
        ]
        assert (
            await restarted.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.flow_id == flow_id,
                    OrchestrationDecision.kind == "node_dispatched",
                )
            )
        ).all()
        await restarted.commit()
    if invalid == "advanced_claim":
        store._read.assert_not_called()
    if invalid is None:
        store._read.return_value = {"status": {"S": "active"}, "workload_binding": {"S": "bound-worker"}}
        async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as restarted:
            started = await run_dispatch_pass(restarted, _config())
            assert started.pending == [] and started.dispatched == 0
            assert client.send_message.call_count == 3
            assert (await restarted.get(OrchestrationNode, node_id)).attempts == 1
    provider.definition.assert_awaited_once()


@pytest.mark.parametrize(
    ("start_pending", "invalid"),
    [
        (False, None),
        (True, None),
        (False, "wrong_action_kind"),
        (False, "missing_ancestry"),
        (False, "bounded_release_reads"),
        (False, "release_timeout"),
        (False, "skewed_merge_time"),
        (False, "missing_baseline"),
        (False, "non_predecessor"),
        (False, "wrong_target"),
        (False, "wrong_connection"),
        (False, "stale_receipt"),
        (False, "replayed_receipt"),
        (False, "wrong_component"),
        (False, "changed_definition"),
        (False, "changed_workflow_without_authority"),
        (False, "byte_equivalent_workflow"),
        (False, "changed_workflow_with_authority"),
        (False, "unreadable_manifest"),
        (False, "foreign_tenant"),
        (False, "foreign_flow"),
    ],
)
async def test_future_release_dispatch(session, monkeypatch, governed_work_ownership, start_pending, invalid):  # noqa: F811
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    monkeypatch.setenv("ADP_SHARED_RUN_REPORTING_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-only-operations-report-key")
    flow, node = await _fixture(
        session,
        policy=_policy(allowed_actions=["operate"], human_gates=[], environment_connection_ids=["conn-1"]),
        node_kwargs={"state": NodeState.PENDING} if start_pending else None,
    )
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))
    approval = await session.scalar(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id))
    plan.accepted_by_decision_id = approval.id
    older = []
    if invalid == "bounded_release_reads":
        older = [await _merged(session, flow, node_ref=f"older-{i}", sha=f"{i:040x}") for i in range(1, 12)]
    if invalid == "skewed_merge_time":
        second, second_action = await _merged(session, flow, node_ref="build-2", sha=SOURCE)
        first, first_action = await _merged(session, flow, node_ref="build-1", sha=FIRST)
    else:
        first, first_action = await _merged(session, flow, node_ref="build-1", sha=FIRST)
        second, second_action = await _merged(session, flow, node_ref="build-2", sha=SOURCE)
    predecessors = [item for item, _ in older] + [first, second]
    addresses = [f"{flow.slug}/{item.epic_ref}/{item.wave_ref}/{item.node_ref}" for item in predecessors]
    plan.plan_document = {
        **plan.plan_document,
        "nodes": [
            *[
                {"address": address, "kind": "story", "title": predecessor.title}
                for address, predecessor in zip(addresses, predecessors, strict=True)
            ],
            {
                "address": f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}",
                "kind": "story",
                "title": node.title,
                "repository_identity": REPO,
                "executor": {"kind": "agent", "role": "operate", "persona": "operations"},
                "required_inputs": [
                    {"name": "workflow", "source": "deployment_workflow:gateway-deploy.yml"},
                    *[{"name": "build", "source": f"node:{address}"} for address in addresses],
                ],
                "evidence_expectations": ["runtime_verified:gateway-backend"],
                "operational_release_binding": {
                    "predecessors": [{"address": address, "receipt_kind": "verified_merge"} for address in addresses],
                    "build_prerequisites": addresses,
                    "repository": REPO,
                    "target": "env-1",
                    "component": "gateway-backend",
                    "workflows": ["gateway-deploy.yml"],
                    "default_branch": "main",
                },
            },
        ],
    }
    manifest = DeploymentManifest(
        schema_version=1,
        entries=(
            ManifestEntry(
                entry_id="env-1",
                status=EntryStatus.ENABLED,
                connection_id="conn-1",
                component_selectors=("gateway-backend",),
                workflow=WorkflowRef(path=".github/workflows/gateway-deploy.yml", definition_revision=DEFINITION),
                artifact_revision=BASELINE,
                resource_kind="eks-namespace",
                resource_id="cluster-dev/adp",
                verification_adapter="gateway-health-verification",
            ),
        ),
    )
    monkeypatch.setattr("src.orchestration.operations_release_resolution.load_packaged_manifest", lambda: manifest)

    async def contains(_binding, source, actual):
        if source == SOURCE and actual != SOURCE:
            raise CycleBlockedError("not_an_ancestor")

    provider = SimpleNamespace(
        contains=AsyncMock(side_effect=contains),
        definition=AsyncMock(
            return_value=SimpleNamespace(approved_revision=DEFINITION, source_revision=SOURCE, dispatch_ref="main", dispatch_revision=SOURCE)
        ),
    )
    monkeypatch.setattr("src.orchestration.operations_release_resolution.ReleaseProvider", lambda: provider)
    await session.flush()
    if start_pending:
        original_receipt = second_action.detail
        second_action.detail = {}
        await session.flush()
        blocked = await run_tick(session)
        assert node.state == NodeState.PENDING.value
        assert blocked.blocked[node.id] == [f"required_input:build:node:{addresses[1]}"]
        assert not provider.contains.await_args_list
        assert plan.plan_document["nodes"][-1]["operational_release_binding"]["build_prerequisites"] == addresses
        second_action.detail = original_receipt
        await session.flush()
        released = await run_tick(session)
        assert released.transitions_effected == 1
        assert node.state == NodeState.READY.value
    if invalid == "wrong_action_kind":
        second_action.kind = "operations_repair_merge"
    elif invalid == "missing_baseline":

        async def missing_baseline(_binding, source, actual):
            if source == BASELINE or (source == SOURCE and actual != SOURCE):
                raise CycleBlockedError("not_an_ancestor")

        provider.contains.side_effect = missing_baseline
    elif invalid == "release_timeout":
        release_timeout = asyncio.timeout(None)
        cancelled_reads = []

        async def never_returns(*_args, **_kwargs):
            if release_timeout.when() is None:
                release_timeout.reschedule(asyncio.get_running_loop().time())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_reads.append(asyncio.current_task())
                raise

        monkeypatch.setattr("src.orchestration.dispatch_pass.asyncio", SimpleNamespace(timeout=lambda _seconds: release_timeout))
        provider.contains.side_effect = never_returns
    elif invalid == "missing_ancestry":

        async def missing_ancestry(_binding, source, actual):
            if (source, actual) in {(FIRST, SOURCE), (SOURCE, FIRST)}:
                raise CycleBlockedError("not_an_ancestor")

        provider.contains.side_effect = missing_ancestry
    elif invalid == "non_predecessor":
        unrelated, _ = await _merged(session, flow, node_ref="unrelated", sha="e" * 40)
        unrelated_address = f"{flow.slug}/{unrelated.epic_ref}/{unrelated.wave_ref}/{unrelated.node_ref}"
        document = copy.deepcopy(plan.plan_document)
        operating = document["nodes"][-1]
        operating["operational_release_binding"]["predecessors"][1]["address"] = unrelated_address
        operating["operational_release_binding"]["build_prerequisites"][1] = unrelated_address
        operating["required_inputs"][2]["source"] = f"node:{unrelated_address}"
        plan.plan_document = document
        assert unrelated_address not in {item["address"] for item in document["nodes"]}
    elif invalid in {"wrong_target", "wrong_component"}:
        document = copy.deepcopy(plan.plan_document)
        operating = document["nodes"][-1]
        if invalid == "wrong_target":
            operating["operational_release_binding"]["target"] = "unapproved-environment"
        else:
            operating["evidence_expectations"] = ["runtime_verified:other-component"]
        plan.plan_document = document
    elif invalid == "wrong_connection":
        other_manifest = DeploymentManifest(
            schema_version=manifest.schema_version,
            entries=(replace(manifest.entries[0], connection_id="outside-accepted-scope"),),
        )
        monkeypatch.setattr("src.orchestration.operations_release_resolution.load_packaged_manifest", lambda: other_manifest)
    elif invalid in {"foreign_tenant", "foreign_flow", "stale_receipt", "replayed_receipt"}:
        receipt = copy.deepcopy(first_action.detail["merge_receipt"] if invalid == "replayed_receipt" else second_action.detail["merge_receipt"])
        if invalid in {"foreign_tenant", "foreign_flow"}:
            receipt["org_id" if invalid == "foreign_tenant" else "flow_id"] = "foreign-scope"
        elif invalid == "stale_receipt":
            receipt["claim_generation"] += 1
        second_action.detail = {"merge_receipt": receipt}
        session.add(
            OrchestrationDecision(
                org_id=ORG_A,
                flow_id=flow.id,
                node_id=second.id,
                kind="result_observed",
                actor_id="system:merge-controller",
                actor_kind="service",
                actor_role="engine",
                from_state="running",
                to_state="passed",
                reason=json.dumps({"attempt": 1, "merge_receipt": receipt}),
            )
        )
    elif invalid == "changed_definition":
        provider.definition.side_effect = CycleBlockedError("deployment_workflow_revision_mismatch")
    elif invalid in {"changed_workflow_without_authority", "byte_equivalent_workflow", "changed_workflow_with_authority"}:
        if invalid == "changed_workflow_with_authority":
            document = copy.deepcopy(plan.plan_document)
            document["nodes"][-1]["operational_release_binding"]["definition_predecessor"] = addresses[1]
            plan.plan_document = document
        workflow_reader = WorkflowProvider()

        async def repository_read(binding, method, path, **kwargs):
            assert binding.repo == REPO and method == "GET"
            if path == f"/repos/{REPO}":
                payload = {"id": 12345, "default_branch": "main"}
            elif path == f"/repos/{REPO}/branches/main":
                payload = {"commit": {"sha": SOURCE}}
            else:
                assert path == f"/repos/{REPO}/contents/.github/workflows/gateway-deploy.yml"
                revision = kwargs["params"]["ref"]
                assert revision in {DEFINITION, SOURCE}
                changed = invalid != "byte_equivalent_workflow" and revision == SOURCE
                content = b"name: changed\non:\n  workflow_dispatch: {}\n" if changed else b"name: original\non:\n  workflow_dispatch: {}\n"
                payload = {
                    "type": "file",
                    "encoding": "base64",
                    "size": len(content),
                    "content": base64.b64encode(content).decode(),
                    "sha": hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False).hexdigest(),
                }
            return SimpleNamespace(json=lambda: payload)

        workflow_reader.request = AsyncMock(side_effect=repository_read)
        provider.definition.side_effect = workflow_reader.definition
    elif invalid == "unreadable_manifest":
        from src.orchestration.deployment_manifest import ManifestError

        def unreadable():
            raise ManifestError("invalid_manifest_entry", "broken")

        monkeypatch.setattr("src.orchestration.operations_release_resolution.load_packaged_manifest", unreadable)
    await session.flush()
    from src.orchestration.accepted_node import accepted_node_contract

    contract = await accepted_node_contract(session, node)
    assert contract.environment_connection_ids == frozenset({"conn-1"}), contract.environment_connection_ids
    assert contract.repository_ids == frozenset({REPO}), contract.repository_ids
    report = await run_dispatch_pass(session, _config())
    if invalid not in {None, "byte_equivalent_workflow", "changed_workflow_with_authority", "bounded_release_reads", "skewed_merge_time"}:
        assert report.dispatched == 0 and report.admission_refused == 1 and report.pending == []
        client = SimpleNamespace(send_message=Mock())
        publish_pending(report, _config(), client=client)
        client.send_message.assert_not_called()
        node_id = node.id
        await session.commit()
        session.expire_all()
        refusals = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "transition_rejected"))).all()
        assert len(refusals) == 1 and json.loads(refusals[0].rejection_reason)["block_code"] == "operations_release_unverified"
        assert not (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_release_binding"))).all()
        assert not (await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node_id))).all()
        assert not (await session.scalars(select(OrchestrationEnvironmentLease))).all()
        assert not (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "node_dispatched"))).all()
        await session.refresh(node)
        assert node.state == NodeState.READY.value and node.attempts == 0
        if invalid in {"foreign_tenant", "foreign_flow", "non_predecessor", "stale_receipt", "replayed_receipt", "wrong_connection"}:
            assert provider.contains.await_count == 0
            assert provider.definition.await_count == 0
        if invalid == "release_timeout":
            assert release_timeout.expired()
            assert len(cancelled_reads) == provider.contains.await_count > 0
            assert all(task.done() for task in cancelled_reads)
            assert json.loads(refusals[0].rejection_reason)["detail"] == "operations_release_verification_timed_out"
        if invalid == "missing_ancestry":
            assert any(call.args[1:] == (FIRST, SOURCE) for call in provider.contains.await_args_list)
            assert any(call.args[1:] == (SOURCE, FIRST) for call in provider.contains.await_args_list)
            assert provider.definition.await_count == 0
        if invalid == "changed_workflow_without_authority":
            assert provider.definition.await_count == 1
            assert {call.kwargs["params"]["ref"] for call in workflow_reader.request.await_args_list if "params" in call.kwargs} == {
                DEFINITION,
                SOURCE,
            }
        return
    assert report.dispatched == 1, report.per_org
    envelope = report.pending[0].envelope
    assert envelope["orchestration"]["deployment_revisions"] == {
        "source_revision": SOURCE,
        "definition_revision": SOURCE if invalid == "changed_workflow_with_authority" else DEFINITION,
        "default_branch": "main",
    }
    if invalid in {"byte_equivalent_workflow", "changed_workflow_with_authority"}:
        assert provider.definition.await_count == 1
        assert provider.definition.await_args.args[1].definition_revision == (SOURCE if invalid == "changed_workflow_with_authority" else DEFINITION)
    resolution = envelope["orchestration"]["release_binding"]
    assert {item["action_id"] for item in resolution["receipts"]} == {first_action.id, second_action.id, *(action.id for _, action in older)}
    assert (resolution["target"], resolution["connection_id"], resolution["resource_id"]) == ("env-1", "conn-1", "cluster-dev/adp")
    await session.commit()
    session.expire_all()
    row = await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_release_binding"))
    assert row is not None and row.detail == resolution
    assert any(call.args[1:] == (FIRST, SOURCE) for call in provider.contains.await_args_list)
    assert any(call.args[1:] == (BASELINE, SOURCE) for call in provider.contains.await_args_list)

    if invalid == "bounded_release_reads":
        assert provider.contains.await_count == len(predecessors)
        assert all(call.args[2] == SOURCE for call in provider.contains.await_args_list)


@pytest.mark.parametrize("cancel", [False, True])
async def test_ancestry_reads_are_bounded_and_drained(cancel):
    from src.orchestration.operations_release_resolution import _contains_all

    entered = asyncio.Event()
    release = asyncio.Event()
    active = set()
    observed = []
    peak = 0

    async def contains(_binding, source, _candidate):
        nonlocal peak
        task = asyncio.current_task()
        active.add(task)
        observed.append(source)
        peak = max(peak, len(active))
        if len(active) == 4:
            entered.set()
        try:
            await release.wait()
        finally:
            active.remove(task)

    proof = asyncio.create_task(_contains_all(SimpleNamespace(contains=contains), None, set(range(13)), 99))
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert peak == 4 and len(observed) == 4
    if cancel:
        proof.cancel()
        with pytest.raises(asyncio.CancelledError):
            await proof
    else:
        release.set()
        await proof
        assert set(observed) == set(range(13))
    assert not active and peak == 4


@pytest.mark.parametrize("invalid", [None, "live_execution", "wrong_recovery_actor", "uncertain_effect", "later_halt"])
async def test_resume_after_awaiting_merge_settles_without_losing_release(session, monkeypatch, governed_work_ownership, invalid):  # noqa: F811
    await test_settled_release_retries_with_identical_pins(
        session, monkeypatch, governed_work_ownership, invalid, "resume", "complete", initial_state="awaiting_merge"
    )


async def test_refused_ancestry_drains_sibling_reads():
    from src.orchestration.operations_release_resolution import _contains_all

    entered = asyncio.Event()
    active = set()

    async def contains(_binding, source, _candidate):
        task = asyncio.current_task()
        active.add(task)
        if len(active) == 4:
            entered.set()
        try:
            await entered.wait()
            if source == 0:
                raise CycleBlockedError("deployment_repository_identity_mismatch")
            await asyncio.Event().wait()
        finally:
            active.remove(task)

    with pytest.raises(CycleBlockedError, match="deployment_repository_identity_mismatch"):
        await asyncio.wait_for(_contains_all(SimpleNamespace(contains=contains), None, set(range(13)), 99), timeout=2)
    assert not active


@pytest.mark.parametrize("terminal_outcome", ["complete", "failed"])
@pytest.mark.parametrize("recovery_mode", ["automatic", "resume"])
@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "missing_recovery",
        "stale_recovery",
        "wrong_recovery_actor",
        "foreign_recovery",
        "later_halt",
        "live_execution",
        "uncertain_effect",
        "missing_report",
        "changed_claim",
        "changed_repository",
        "missing_release",
        "changed_metadata",
        "changed_pins",
        "changed_resource",
        "changed_baseline",
        "changed_definition",
        "missing_receipt",
        "changed_plan_hash",
        "preserved_plan",
        "workflow_drift_repair",
        "workflow_drift_repository_repair",
        "workflow_drift_no_repair",
        "workflow_drift_foreign_repo",
        "workflow_drift_branch",
        "workflow_drift_unrelated",
        "workflow_drift_missing_head",
        "workflow_drift_source_changed",
    ],
)
async def test_settled_release_retries_with_identical_pins(  # noqa: F811
    session,
    monkeypatch,
    governed_work_ownership,
    invalid,
    recovery_mode,
    terminal_outcome,
    initial_state="running",
    generic_role_change=None,
    native_outcome=None,
):
    from src.orchestration.developer_recovery import recover_developer, recovery_id
    from src.orchestration.results import observe_results
    from src.orchestration.run_reports import OrchestrationRunReport, record_terminal

    async def configure_generic(flow, node, plan, manifest, provider):
        from src.orchestration.delivery_contract import DeliveryContract

        entry = manifest.entries[0]
        delivery = DeliveryContract(
            schema_version=2,
            connection_id=entry.connection_id,
            resource_kind=entry.resource_kind,
            resource_id=entry.resource_id,
            baseline_revision=entry.artifact_revision,
            definition_revision=entry.workflow.definition_revision,
            required_checks=["runtime"],
            cleanup_required=True,
            job_mutation_role_arn="arn:aws:iam::123456789012:role/original-publisher" if generic_role_change in {"replace", "remove"} else None,
        )
        document = copy.deepcopy(plan.plan_document)
        document["nodes"][-1]["operational_release_binding"]["delivery_contract"] = delivery.model_dump(mode="json")
        plan.plan_document = document

    fixture = await _dispatched_release(
        session,
        monkeypatch,
        retry=True,
        repair_authority="repository"
        if invalid == "workflow_drift_repository_repair"
        else bool(invalid and invalid.startswith("workflow_drift_") and invalid != "workflow_drift_no_repair"),
        **({"prepare_release": configure_generic} if generic_role_change is not None else {}),
    )
    node = fixture.node
    node.state = initial_state
    first = fixture.first_pass.pending[0].envelope
    run_id = first["message_id"]
    report = await session.get(OrchestrationRunReport, run_id)
    assert report is not None
    text = "Deployment revision guard failed before mutation; runtime acceptance remains incomplete."
    report.operations_report_receipt = {
        "recorded": True,
        "run_id": run_id,
        "attempt": 1,
        "node_id": node.id,
        "receipt_id": f"operations-report:{run_id}",
        "report": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    record_terminal(report, terminal_outcome, failure={"category": "deadline", "exit_code": 124} if terminal_outcome == "failed" else None)
    for action in (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_task"))).all():
        action.status = "succeeded"
        action.detail = {**action.detail, "state": "done"}
    await session.commit()
    observed = await observe_results(session)
    assert observed.advanced == 1, observed
    assert node.state == "failed"
    if recovery_mode == "automatic":
        assert await recover_developer(session, node, now=datetime.now(UTC) + timedelta(minutes=5)) == "scheduled"
    else:
        from src.orchestration.controls import ResumeRequest, resume_node

        # Exercise the actual Resume route, with only its authenticated caller
        # and permission service supplied by the test. No retry receipt exists.
        resumed = await resume_node(
            node.id,
            ResumeRequest(reason="Retry after the operations tools were repaired."),
            SimpleNamespace(org_id=node.org_id, user_id=policy_fixtures.APPROVER),
            SimpleNamespace(check_permission=AsyncMock(), get_user_role=AsyncMock(return_value=[SimpleNamespace(value="org_admin")])),
            session,
            None,
        )
        assert resumed.state == "ready"
    await session.commit()
    previous = await session.get(OrchestrationExecution, first["handoff_expect"]["execution_id"])
    released = await session.scalar(
        select(OrchestrationAction).where(OrchestrationAction.execution_id == previous.id, OrchestrationAction.kind == "operations_release_binding")
    )
    recovery = await session.get(
        OrchestrationDecision, recovery_id(node.id, 1, operations=True) if recovery_mode == "automatic" else resumed.decision_id
    )
    if native_outcome is not None:
        pending = OrchestrationAction(
            org_id=node.org_id,
            execution_id=previous.id,
            operation_key="native-release:pending",
            kind="operations_native_release_dispatch",
            status="prepared",
            attempt=1,
            detail={},
        )
        session.add(pending)

        async def reconcile(database, **scope):
            assert database is session and scope["node"].id == node.id and scope["execution"].id == previous.id
            assert scope["report"].run_id == run_id and scope["node"].state == "ready"
            if native_outcome == "reconciled":
                pending.status = "failed"
                await database.flush()
            return native_outcome

        reconciler = AsyncMock(side_effect=reconcile)
        monkeypatch.setattr("src.orchestration.operations_cancelled_release.reconcile_cancelled_release", reconciler)
    if invalid == "missing_recovery":
        await session.delete(recovery)
    elif invalid in {"wrong_recovery_actor", "foreign_recovery"}:
        values = {"actor_kind": "service" if recovery_mode == "resume" else "human"} if invalid == "wrong_recovery_actor" else {"org_id": "other-org"}
        await (await session.connection()).execute(
            OrchestrationDecision.__table__.update().where(OrchestrationDecision.id == recovery.id).values(**values)
        )
        await session.refresh(recovery)
    elif invalid == "later_halt":
        node.state = "halted"
        session.add(
            OrchestrationDecision(
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind="node_halted",
                actor_id=policy_fixtures.APPROVER,
                actor_kind="human",
                actor_role="org_admin",
                from_state="ready",
                to_state="halted",
                reason="Hold the recovery.",
            )
        )
    elif invalid == "stale_recovery":
        # Corrupt the SQLite fixture below the append-only ORM boundary.
        if recovery_mode == "resume":
            recovery = await session.scalar(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == node.id,
                    OrchestrationDecision.kind == "result_observed",
                    OrchestrationDecision.to_state == "failed",
                )
            )
        await (await session.connection()).execute(
            OrchestrationDecision.__table__.update()
            .where(OrchestrationDecision.id == recovery.id)
            .values(reason=json.dumps({**json.loads(recovery.reason), "previous_attempt" if recovery_mode == "automatic" else "attempt": 2}))
        )
        await session.refresh(recovery)
    elif invalid == "live_execution":
        previous.phase, previous.status = "delivering", "runnable"
    elif invalid == "uncertain_effect":
        session.add(
            OrchestrationAction(
                org_id=node.org_id,
                execution_id=previous.id,
                operation_key="uncertain-deployment",
                kind="deployment_workflow",
                status="unknown",
                attempt=1,
                detail={},
            )
        )
    elif invalid == "missing_report":
        await session.delete(report)
    elif invalid == "changed_claim":
        previous.claim_generation += 1
    elif invalid == "changed_repository":
        report.provider_repository_id += 1
    elif invalid == "missing_release":
        await session.delete(released)
    elif invalid == "changed_metadata":
        report.dispatch_metadata = {**report.dispatch_metadata, "orchestration": {}}
    elif invalid == "changed_pins":
        pins = {**released.detail["pins"], "source_revision": "e" * 40}
        released.detail = {**released.detail, "pins": pins}
        metadata = copy.deepcopy(report.dispatch_metadata)
        metadata["orchestration"].update(release_binding=released.detail, deployment_revisions=pins)
        report.dispatch_metadata = metadata
    elif invalid in {"changed_resource", "changed_baseline", "changed_definition"}:
        entry = fixture.manifest.entries[0]
        change = (
            {"resource_id": "other-cluster/adp"}
            if invalid == "changed_resource"
            else (
                {"artifact_revision": "e" * 40}
                if invalid == "changed_baseline"
                else {"workflow": replace(entry.workflow, definition_revision="e" * 40)}
            )
        )
        manifest = replace(fixture.manifest, entries=(replace(entry, **change),))
        monkeypatch.setattr("src.orchestration.operations_release_resolution.load_packaged_manifest", lambda: manifest)
        if invalid == "changed_definition":
            fixture.provider.definition.return_value = SimpleNamespace(
                approved_revision="e" * 40, source_revision=SOURCE, dispatch_ref="main", dispatch_revision=SOURCE
            )
    elif invalid == "missing_receipt":
        build_action = await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "merge_pull_request").limit(1))
        await session.delete(build_action)
    elif invalid == "changed_plan_hash":
        released.detail = {**released.detail, "accepted_plan_hash": "e" * 64}
        metadata = copy.deepcopy(report.dispatch_metadata)
        metadata["orchestration"]["release_binding"] = released.detail
        report.dispatch_metadata = metadata
    elif invalid == "preserved_plan":
        fixture.plan.superseded_at = datetime.now(UTC)
        amendment = OrchestrationDecision(
            org_id=node.org_id,
            flow_id=node.flow_id,
            kind="plan_amended",
            actor_id=policy_fixtures.APPROVER,
            actor_role="org_admin",
            actor_kind="human",
            reason="Retitle only",
        )
        session.add(amendment)
        await session.flush()
        document = copy.deepcopy(fixture.plan.plan_document)
        document["nodes"][-1]["title"] = "Retitled operations story"
        if generic_role_change is not None:
            delivery = document["nodes"][-1]["operational_release_binding"]["delivery_contract"]
            delivery["job_mutation_role_arn"] = "arn:aws:iam::123456789012:role/reviewed-publisher"
            if generic_role_change == "target":
                delivery["resource_id"] = "other-cluster/other-service"
            elif generic_role_change == "checks":
                delivery["required_checks"] = ["different-check"]
            elif generic_role_change == "cleanup":
                delivery["cleanup_required"] = False
            elif generic_role_change == "remove":
                del delivery["job_mutation_role_arn"]
        session.add(
            OrchestrationAcceptedPlan(
                org_id=node.org_id,
                flow_id=node.flow_id,
                version=2,
                plan_hash="hash-v2",
                plan_document=document,
                accepted_by_decision_id=amendment.id,
            )
        )
    elif invalid and invalid.startswith("workflow_drift_"):
        current_head = "f" * 40
        reader = WorkflowProvider()
        original_workflow = b"on:\n  workflow_dispatch:\n    inputs: {}\njobs: {}\n"
        changed_workflow = original_workflow + b"env:\n  STORAGE: signed-s3\n"

        async def read(_binding, _method, path, **kwargs):
            if path.endswith("/contents/.github/workflows/gateway-deploy.yml"):
                revision = kwargs["params"]["ref"]
                changed = revision == current_head or invalid == "workflow_drift_source_changed" and revision == SOURCE
                content = changed_workflow if changed else original_workflow
                return SimpleNamespace(
                    json=lambda: {
                        "type": "file",
                        "encoding": "base64",
                        "size": len(content),
                        "content": base64.b64encode(content).decode(),
                        "sha": hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False).hexdigest(),
                    }
                )
            if "/branches/" in path:
                return SimpleNamespace(json=lambda: {"commit": {"sha": None if invalid == "workflow_drift_missing_head" else current_head}})
            return SimpleNamespace(
                json=lambda: {
                    "id": 54321 if invalid == "workflow_drift_foreign_repo" else 12345,
                    "default_branch": "other" if invalid == "workflow_drift_branch" else "main",
                }
            )

        reader.request = AsyncMock(side_effect=read)
        fixture.provider.request = reader.request
        fixture.provider.definition.side_effect = reader.definition
        if invalid == "workflow_drift_unrelated":
            original_contains = fixture.provider.contains.side_effect

            async def contains(binding, source, actual):
                if actual == current_head:
                    raise CycleBlockedError("not_an_ancestor")
                return await original_contains(binding, source, actual)

            fixture.provider.contains.side_effect = contains
    await session.commit()
    retried = await run_dispatch_pass(session, _config())
    if native_outcome is not None:
        reconciler.assert_awaited_once()
        if native_outcome != "reconciled":
            assert retried.dispatched == 0 and retried.pending == []
            assert node.state == "ready" and node.attempts == 1 and pending.status == "prepared"
            return
        assert pending.status == "failed"
    if generic_role_change not in {None, "add"}:
        assert retried.dispatched == 0 and retried.pending == [], retried.per_org
        assert node.attempts == 1 and node.state == "ready"
        assert released.detail == first["orchestration"]["release_binding"]
        return
    if invalid and invalid not in {"preserved_plan", "workflow_drift_repair", "workflow_drift_repository_repair"}:
        assert retried.dispatched == 0 and retried.pending == [], retried.per_org
        assert node.state == ("halted" if invalid == "later_halt" else "ready") and node.attempts == 1
        assert len((await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))).all()) == 1
        return
    assert retried.dispatched == 1, retried.per_org
    assert node.attempts == 2
    second = retried.pending[0].envelope
    assert second["orchestration"]["deployment_revisions"] == first["orchestration"]["deployment_revisions"]
    expected_release = copy.deepcopy(first["orchestration"]["release_binding"])
    if invalid == "preserved_plan":
        expected_release.update(accepted_plan_version=2, accepted_plan_hash="hash-v2")
        if generic_role_change == "add":
            current_contract = await accepted_node_contract(session, node)
            expected_release["delivery_contract_digest"] = current_contract.node.operational_release_binding.delivery_contract.digest
            assert expected_release["delivery_contract_digest"] != first["orchestration"]["release_binding"]["delivery_contract_digest"]
    assert second["orchestration"]["release_binding"] == expected_release
    assert second["message_id"] != first["message_id"]
    assert second["handoff_expect"]["execution_id"] != first["handoff_expect"]["execution_id"]
    assert second["handoff_expect"]["claim_generation"] > first["handoff_expect"]["claim_generation"]
    assert second["orchestration"]["operations_recovery"]["previous_run_id"] == run_id
    assert released.detail == first["orchestration"]["release_binding"]
    assert previous.status == "concluded" and previous.cycle == 1
    if invalid in {"workflow_drift_repair", "workflow_drift_repository_repair"}:
        # Admission retained old pins; current-main workflow is still not
        # dispatchable under them until the native repair merge advances source.
        binding = SimpleNamespace(repo=REPO, provider_repository_id=12345)
        with pytest.raises(CycleBlockedError, match="deployment_workflow_revision_mismatch"):
            await reader.definition(binding, fixture.manifest.entries[0].workflow, SOURCE)
        assert any(call.kwargs.get("for_dispatch") is False for call in fixture.provider.definition.await_args_list)
    from src.orchestration.operations_release_resolution import operational_execution_pins

    current = await session.get(OrchestrationExecution, second["handoff_expect"]["execution_id"])
    assert (
        await operational_execution_pins(
            session, node=node, execution=current, contract=await accepted_node_contract(session, node), run_id=second["message_id"]
        )
        == second["orchestration"]["deployment_revisions"]
    )


@pytest.mark.parametrize("outcome", ["reconciled", "native_cancel_recovery_jobs_unverified", "native_cancel_recovery_evidence_unavailable"])
async def test_resumed_release_reconciles_before_dispatch(session, monkeypatch, governed_work_ownership, outcome):  # noqa: F811
    await test_settled_release_retries_with_identical_pins(
        session, monkeypatch, governed_work_ownership, None, "resume", "complete", native_outcome=outcome
    )


@pytest.mark.parametrize("change", ["add", "replace", "remove", "target", "checks", "cleanup"])
async def test_settled_generic_release_role_amendment(session, monkeypatch, governed_work_ownership, change):  # noqa: F811
    await test_settled_release_retries_with_identical_pins(
        session, monkeypatch, governed_work_ownership, "preserved_plan", "automatic", "failed", generic_role_change=change
    )


@pytest.mark.parametrize(
    "component,workflow,check",
    [
        ("payments-api", "ship-payments.yml", "api-smoke"),
        ("stock-worker", "publish-stock.yml", "cli-roundtrip"),
    ],
)
async def test_generic_release_resolves_without_packaged_product_manifest(session, monkeypatch, governed_work_ownership, component, workflow, check):  # noqa: F811
    from src.orchestration.delivery_contract import DeliveryContract

    delivery = DeliveryContract(
        schema_version=2,
        connection_id="conn-1",
        resource_kind="service",
        resource_id=f"test/{component}",
        baseline_revision=BASELINE,
        definition_revision=DEFINITION,
        workflow_inputs={"suite": check},
        required_checks=[check],
    )

    async def configure(flow, node, plan, manifest, provider):
        document = copy.deepcopy(plan.plan_document)
        step = document["nodes"][-1]
        step["operational_release_binding"].update(component=component, workflows=[workflow], delivery_contract=delivery.model_dump(mode="json"))
        step["required_inputs"][0]["source"] = f"deployment_workflow:{workflow}"
        step["evidence_expectations"] = [f"runtime_verified:{component}"]
        plan.plan_document = document
        monkeypatch.setattr(
            "src.orchestration.operations_release_resolution.load_packaged_manifest", lambda: pytest.fail("global product manifest loaded")
        )

    scenario = await _dispatched_release(session, monkeypatch, prepare_release=configure)
    envelope = scenario.first_pass.pending[0].envelope
    record = envelope["orchestration"]["release_binding"]
    assert record["component"] == component and record["workflow"] == f".github/workflows/{workflow}"
    assert record["delivery_contract_digest"] == delivery.digest
    assert record["resource_id"] == f"test/{component}"
    assert scenario.provider.definition.await_args.kwargs["generic_delivery"] is True
