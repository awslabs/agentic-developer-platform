"""Admit one real scan execution, using the normal claim and action ledgers."""

import hashlib
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote

import yaml
from sqlalchemy import select

from .deployment_manifest import WorkflowRef
from .deployment_workflow_provider import CORRELATION_INPUT, TRANSPORT_INPUTS, WorkflowProvider
from .evaluation_acceptance import accepted_contract
from .evaluation_plan import accepted_evaluation
from .execution_runner import OperationIdentity, RunnerContext
from .execution_state import ActionIntent, ExecutionIdentity, ExecutionPhase, Observation, ObservedOutcome, OutcomeKind
from .execution_store import create_execution, load_execution, prepare_action, record_observation
from .models import OrchestrationAcceptedPlan, OrchestrationAction, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from .repository_evaluation import authorize, record, sources_for
from .repository_evaluation_contract import canonical, harness_digest
from .repository_evaluation_provider import RepositoryEvidenceProvider, require
from .runtime_policy import flow_started_at
from .state import ActorKind, NodeState, transition
from .work_claims import ClaimBinding, ClaimOwner, Disposition, OwnerKind, claim_work

CONTEXT_KIND = "repository_scan_context"
PRODUCER_KIND = "repository_scan_dispatch"
CLI_PRODUCER_KIND = "cli_qualification_dispatch"


def producer_kind(spec):
    return CLI_PRODUCER_KIND if spec.evidence_schema == "cli-live-evaluation/v1" else PRODUCER_KIND


def context_kind(spec):
    return "cli_qualification_context" if spec.evidence_schema == "cli-live-evaluation/v1" else CONTEXT_KIND


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class RepositoryScanProvider(WorkflowProvider):
    def __init__(self, *, evidence=None, **kwargs):
        super().__init__(**kwargs)
        self.evidence = evidence or RepositoryEvidenceProvider(client=self.client, clock=self.clock)

    async def token(self, binding, *, write=False):
        # Reuse the repository-scoped reader across preflight/reconciliation.
        # An actions-write credential is minted only for the single dispatch.
        return await super().token(binding, write=True) if write else await self.evidence.token(binding)

    async def observe(self, binding, *, workflow, definition, target, source_revision, inputs, correlation):
        # Repository-wide history is unbounded and the scanned source can differ
        # from GitHub's dispatch head. Search only this workflow at its pinned
        # dispatch revision, then reuse the provider's full context validation.
        require(bool(correlation), "producer_correlation_missing")
        rows = await self.pages(
            binding,
            f"/repos/{binding.repo}/actions/workflows/{quote(workflow.path.rsplit('/', 1)[1], safe='')}/runs",
            "workflow_runs",
            event="workflow_dispatch",
            head_sha=definition.dispatch_revision,
        )
        matches = [row for row in rows if row.get("display_title") == "ADP deployment " + correlation]
        require(len(matches) <= 1, "producer_run_ambiguous")
        if not matches:
            return None, False
        run = matches[0]
        require(
            run.get("event") == "workflow_dispatch"
            and run.get("head_sha") == definition.dispatch_revision
            and (run.get("repository") or {}).get("id") == binding.provider_repository_id
            and str(run.get("path", "")).split("@", 1)[0] == workflow.path,
            "producer_run_identity_mismatch",
        )
        observed, incomplete = await super().observe(
            binding,
            workflow=workflow,
            definition=definition,
            target=target,
            source_revision=source_revision,
            inputs=inputs,
            correlation=correlation,
            run_id=int(run["id"]),
        )
        require(observed is not None or incomplete, "producer_correlated_run_unverifiable")
        return observed, incomplete

    async def preflight(self, binding, spec, sources):
        producer, workflow = spec.producer, spec.workflows[0]
        require(producer is not None and producer.target.resource_id == binding.repo, "producer_target_repository_changed")
        _, revisions = await self.evidence.verify_sources(binding, spec, sources)
        source = workflow.source.revision or revisions[workflow.source.predecessor]
        definition_revision = workflow.definition.revision or revisions[workflow.definition.predecessor]
        ref = WorkflowRef(
            path=workflow.path,
            definition_revision=definition_revision,
            allowed_inputs={name: frozenset({value}) for name, value in producer.inputs.items()},
            correlation_input=CORRELATION_INPUT,
        )
        definition = await self.definition(binding, ref, source)
        require(TRANSPORT_INPUTS <= definition.defaults.keys(), "producer_correlation_unavailable")
        require(definition.defaults.keys() - TRANSPORT_INPUTS == producer.inputs.keys(), "producer_unaccepted_default_inputs")
        _, content = await self.evidence.definition_blob(binding, workflow.path, definition_revision)
        document = yaml.safe_load(content)
        events = document.get("on", document.get(True)) if isinstance(document, dict) else None
        names = set(events) if isinstance(events, dict | list) else {events} if isinstance(events, str) else set()
        expected_events = (
            {"workflow_dispatch", "schedule", "pull_request"} if spec.evidence_schema == "cli-live-evaluation/v1" else {"workflow_dispatch"}
        )
        require(names == expected_events, "producer_workflow_events_changed")
        if spec.evidence_schema == "cli-live-evaluation/v1":
            from .cli_live_contract import NIGHTLY_SCHEDULE

            require(isinstance(events, dict) and events["schedule"] == NIGHTLY_SCHEDULE, "cli_nightly_schedule_changed")
        concurrency = document.get("concurrency")
        require(
            isinstance(concurrency, dict)
            and isinstance(concurrency.get("group"), str)
            and bool(concurrency["group"])
            and concurrency.get("cancel-in-progress") is False,
            "producer_workflow_concurrency_missing",
        )
        require(document.get("run-name") == "${{ format('ADP deployment {0}', inputs.adp_correlation) }}", "producer_run_name_unverifiable")
        return dict(source_revision=source, definition=asdict(definition), workflow=asdict(ref))


def workflow_ref(data):
    raw = data["workflow"]
    return WorkflowRef(**{**raw, "allowed_inputs": {key: frozenset(values) for key, values in raw["allowed_inputs"].items()}})


async def binding_for(session, node, spec):
    from .dispatch_pass import resolve_installation_id

    installation = await resolve_installation_id(session, org_id=node.org_id)
    require(installation is not None, "installation_missing")
    return SimpleNamespace(
        org_id=node.org_id, installation_id=installation, repo=spec.runner.repository, provider_repository_id=spec.runner.repository_id
    )


async def producer_state(session, context):
    loaded = await load_execution(session, identity=context.identity)
    require(loaded is not None and loaded.kind is OutcomeKind.APPLIED, "producer_execution_changed")
    node = await session.get(OrchestrationNode, context.identity.node_id, populate_existing=True)
    require(node is not None and node.org_id == context.identity.org_id and node.flow_id == context.execution.flow_id, "producer_node_changed")
    require(node.kind == "eval" and node.state == "running" and node.attempts == context.identity.cycle, "producer_cycle_changed")
    accepted = await accepted_evaluation(session, node)
    require(accepted is not None and accepted[1].producer is not None, "producer_contract_missing")
    plan, spec, _ = accepted
    attached = await accepted_contract(session, node=node, plan=plan)
    require(attached is not None and plan.version == context.identity.accepted_plan_version, "producer_acceptance_changed")
    rows = list(
        await session.scalars(
            select(OrchestrationAction)
            .where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.execution_id == context.execution.id,
                OrchestrationAction.kind == context_kind(spec),
                OrchestrationAction.status == "succeeded",
            )
            .limit(2)
        )
    )
    require(len(rows) == 1, "producer_context_missing")
    data = rows[0].detail
    require(
        data["acceptance_decision_id"] == attached[0].id
        and data["evaluation_policy_hash"] == attached[2].policy_hash
        and data["specification_hash"] == digest(spec.model_dump(mode="json")),
        "producer_acceptance_changed",
    )
    require(
        data["plan_id"] == plan.id and data["plan_hash"] == plan.plan_hash and spec.runner.harness_sha256 == harness_digest(),
        "producer_plan_or_harness_changed",
    )
    claim = await session.get(OrchestrationWorkClaim, context.identity.claim_id, populate_existing=True)
    require(
        claim is not None
        and claim.state == "held"
        and claim.generation == context.identity.claim_generation
        and claim.owner_kind == "engine_flow"
        and claim.owner_ref == node.flow_id
        and claim.provider_repository_id == spec.runner.repository_id
        and str(claim.issue_number) == str(node.issue_ref).lstrip("#"),
        "producer_claim_changed",
    )
    sources = await sources_for(session, node, plan, spec)
    require(digest(sources) == data["source_snapshot_hash"], "producer_sources_changed")
    return node, plan, spec, attached, data, sources, await binding_for(session, node, spec)


async def admit_producer(session, node, *, provider=None):
    evidence = provider or RepositoryEvidenceProvider()
    producer_provider = RepositoryScanProvider(evidence=evidence)
    accepted = await accepted_evaluation(session, node)
    require(accepted is not None and accepted[1].producer is not None, "producer_contract_missing")
    plan, spec, _ = accepted
    require(spec.runner.harness_sha256 == harness_digest(), "producer_harness_changed")
    attached = await accepted_contract(session, node=node, plan=plan)
    require(attached is not None, "producer_explicit_acceptance_required")
    require(node.state == "ready", "producer_node_not_ready")
    binding = await binding_for(session, node, spec)
    authority = await authorize(session, node, plan, spec, binding, evidence)
    sources = await sources_for(session, node, plan, spec)
    await producer_provider.preflight(binding, spec, sources)
    expected = (plan.id, plan.version, plan.plan_hash, attached[0].id, digest(sources), node.attempts)
    # Provider reads finish before the normal flow/plan/node/claim lock order.
    flow = await session.scalar(
        select(OrchestrationFlow).where(OrchestrationFlow.id == node.flow_id).with_for_update().execution_options(populate_existing=True)
    )
    current = await session.scalar(
        select(OrchestrationAcceptedPlan)
        .where(OrchestrationAcceptedPlan.flow_id == node.flow_id, OrchestrationAcceptedPlan.superseded_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    node = await session.scalar(
        select(OrchestrationNode).where(OrchestrationNode.id == node.id).with_for_update().execution_options(populate_existing=True)
    )
    require(flow.state == "running" and node.state == "ready", "producer_admission_raced")
    require(spec.runner.harness_sha256 == harness_digest(), "producer_harness_changed")
    fresh = await accepted_contract(session, node=node, plan=current)
    require(
        fresh is not None
        and (
            current.id,
            current.version,
            current.plan_hash,
            fresh[0].id,
            digest(await sources_for(session, node, current, spec, lock=True)),
            node.attempts,
        )
        == expected,
        "producer_admission_changed",
    )
    require(await authorize(session, node, current, spec, binding, evidence) == authority, "producer_authority_changed")
    previous = await session.scalar(
        select(OrchestrationAction.id)
        .where(
            OrchestrationAction.org_id == node.org_id,
            OrchestrationAction.kind == context_kind(spec),
            OrchestrationAction.detail["acceptance_decision_id"].as_string() == fresh[0].id,
        )
        .limit(1)
    )
    require(previous is None, "producer_acceptance_already_admitted")
    started = await flow_started_at(session, org_id=node.org_id, flow_id=node.flow_id)
    require(started is not None, "producer_start_time_unverifiable")
    deadline = min(fresh[2].expires_at, started + timedelta(seconds=fresh[2].limits.max_wall_clock_seconds))
    claim = await claim_work(
        session,
        binding=ClaimBinding(node.org_id, spec.runner.repository_id, int(str(node.issue_ref).lstrip("#"))),
        owner=ClaimOwner(OwnerKind.ENGINE_FLOW, node.flow_id),
        event_id="repository-scan:" + fresh[0].id,
    )
    require(claim.disposition is Disposition.ADMITTED, "producer_claim_unavailable")
    require(
        transition(node.state, NodeState.RUNNING, actor_kind=ActorKind.SERVICE, reason="Accepted one-off repository scan").allowed,
        "producer_transition_refused",
    )
    node.state = "running"
    node.attempts += 1
    identity = ExecutionIdentity(node.org_id, node.id, node.attempts, current.version, claim.claim_id, claim.generation)
    created = await create_execution(
        session,
        identity=identity,
        flow_id=node.flow_id,
        phase=ExecutionPhase.EVALUATION_PENDING,
        next_check_at=datetime.now(UTC),
        deadline_at=deadline,
    )
    require(created.kind is OutcomeKind.APPLIED, "producer_execution_conflict")
    context = RunnerContext(identity, created.record, datetime.now(UTC))
    key = OperationIdentity.from_context(context, context_kind(spec), fresh[0].id).key
    data = dict(
        acceptance_decision_id=fresh[0].id,
        specification_hash=digest(spec.model_dump(mode="json")),
        source_snapshot_hash=digest(sources),
        plan_id=current.id,
        plan_hash=current.plan_hash,
        evaluation_policy_hash=authority[1],
    )
    require(
        (await prepare_action(session, identity=identity, intent=ActionIntent(key, context_kind(spec), detail=data))).kind is OutcomeKind.APPLIED,
        "producer_context_conflict",
    )
    require(
        (
            await record_observation(
                session,
                identity=identity,
                observation=Observation(key, ObservedOutcome.SUCCEEDED, receipt_ref="evaluation/context/" + created.record.id),
            )
        ).kind
        is OutcomeKind.APPLIED,
        "producer_context_conflict",
    )
    await record(
        session, node, dict(action="repository_scan_admitted", execution_id=created.record.id, acceptance_decision_id=fresh[0].id), before="ready"
    )
    return True
