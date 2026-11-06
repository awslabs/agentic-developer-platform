"""An offline release with real plan, grant, policy, reservation and job guards.

Only provider/credential transport is replaced: moto DynamoDB, fakeredis and
explicit GitHub/AWS responses. No authorization decision or snapshot is stubbed.
The fixture represents a retained original dispatch, not live acceptance proof.
"""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import fakeredis.aioredis
from sqlalchemy import select

from src.agentauth.engine import EngineAuthorityWriter
from src.budget.reservations import ReservationStore
from src.orchestration import flow_budget, generic_delivery_dispatch, operations_release_job
from src.orchestration.accepted_node import accepted_node_contract
from src.orchestration.compile import plan_hash
from src.orchestration.deployment_target import DeploymentTargetResolver
from src.orchestration.dispatch_pass import DispatchPassConfig, PendingPublish, _build_envelope, attempt_run_id
from src.orchestration.execution_policy import Action
from src.orchestration.flow_meter import meter_target
from src.orchestration.genesis import resolve_engine_genesis
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationWorkClaim
from src.orchestration.operations_release_job import NativeJobRequest, NativeReleaseJob
from src.orchestration.proposal import LoopProposal
from src.orchestration.review_cycle_dispatch import ReviewCycleServices
from src.orchestration.run_reports import OrchestrationRunReport
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserCredential
from tests.agentauth.test_human_dispatch import store  # noqa: F401
from tests.orchestration.test_policy_admission import (
    APPROVER,
    ORG_A,
    REPO,
    _accept_policy,
    _make_approval,
    _make_flow,
    _make_member,
    _make_node,
    _make_org,
    _policy,
)

ROLE = "arn:aws:iam::123456789012:role/security-fixture-reader"
MUTATION_ROLE = "arn:aws:iam::123456789012:role/security-fixture-release"
CONNECTION = "security-release-connection"
SOURCE = "d" * 40
DEFINITION = "b" * 40
DISPATCH = "e" * 40
WORKFLOW = "l5-services-release.yml"


async def build_original_security_release(db, factory, monkeypatch, *, store):  # noqa: F811
    """Create a retained original release; caller closes ``result.close()``."""
    now = datetime.now(UTC)
    if await db.get(Organization, ORG_A) is None:
        await _make_org(db)
    if await db.get(User, APPROVER) is None:
        await _make_member(db)
    flow = await _make_flow(db, slug="security-original-release")
    flow.state = "running"
    node = await _make_node(db, flow, attempts=1, state="running")
    policy = _policy(
        schema_version=2,
        expires_at=now + timedelta(hours=1),
        allowed_actions=[Action.OPERATE, Action.DEPLOY, Action.EVALUATE],
        environment_connection_ids=[CONNECTION],
        user_credentials={
            "permission_mode": "user_configured",
            "lifetime": "provider_managed",
            "vault_credential_ids": [CONNECTION],
            "aws_role_arns": [ROLE],
            "actions": [Action.DEPLOY],
        },
    )
    plan = await _accept_policy(db, flow, policy)
    approval = await _make_approval(db, flow)
    plan.accepted_by_decision_id = approval.id
    address = f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"
    predecessor = f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/build"
    plan.plan_document = {
        **plan.plan_document,
        "title": flow.title,
        "org_id": ORG_A,
        "spec_revision": "security-original-test-v1",
        "edges": [],
        "nodes": [
            {
                "address": address,
                "kind": "story",
                "title": node.title,
                "repository_identity": REPO,
                "executor": {"kind": "agent", "role": "operate", "persona": "operations"},
                "evidence_expectations": ["runtime_verified:l5-security"],
                "operational_release_binding": {
                    "predecessors": [{"address": predecessor, "receipt_kind": "verified_merge"}],
                    "build_prerequisites": [predecessor],
                    "repository": REPO,
                    "target": "security-release",
                    "component": "l5-security",
                    "workflows": [WORKFLOW],
                    "default_branch": "main",
                    "delivery_contract": {
                        "schema_version": 2,
                        "connection_id": CONNECTION,
                        "resource_kind": "l5-service",
                        "resource_id": "security",
                        "definition_revision": DEFINITION,
                        "baseline_revision": "c" * 40,
                        "required_checks": ["ui-cli-mcp-parity", "two-non-adp-projects", "cross-project-denial"],
                        "job_mutation_role_arn": MUTATION_ROLE,
                    },
                },
            }
        ],
    }
    plan.plan_hash = plan_hash(LoopProposal.model_validate(plan.plan_document))
    correlation = attempt_run_id(node.id, node.attempts)
    claim = OrchestrationWorkClaim(
        org_id=ORG_A,
        provider_repository_id=123,
        issue_number=int(node.issue_ref),
        owner_kind="engine_flow",
        owner_ref=flow.id,
        generation=1,
        state="held",
        active_run_id=correlation,
        claim_event_id=correlation,
        lease_expires_at=now + timedelta(hours=1),
    )
    db.add(claim)
    await db.flush()
    execution = OrchestrationExecution(
        org_id=ORG_A,
        flow_id=flow.id,
        node_id=node.id,
        cycle=1,
        phase="delivering",
        status="awaiting_external",
        accepted_plan_version=1,
        claim_id=claim.id,
        claim_generation=1,
    )
    db.add(execution)
    await db.flush()
    contract = await accepted_node_contract(db, node)
    release = contract.node.operational_release_binding
    pins = {"source_revision": SOURCE, "definition_revision": DEFINITION, "default_branch": "main"}
    # These are the immutable records produced before the retained workflow.
    # The read path must compare the journal to the report and accepted contract.
    release_record = {
        "accepted_plan_version": 1,
        "accepted_plan_hash": plan.plan_hash,
        "repository": REPO,
        "target": release.target,
        "component": release.component,
        "workflow": ".github/workflows/" + WORKFLOW,
        "connection_id": CONNECTION,
        "resource_kind": "l5-service",
        "resource_id": "security",
        "delivery_contract_digest": release.delivery_contract.digest,
        "pins": pins,
        "receipts": [{"address": predecessor}],
    }
    report = OrchestrationRunReport(
        run_id=correlation,
        credential_hash="a" * 64,
        org_id=ORG_A,
        flow_id=flow.id,
        node_id=node.id,
        attempt=1,
        persona="operations",
        repo=REPO,
        installation_id=456,
        provider_repository_id=123,
        dispatch_metadata={
            "orchestration": {"release_binding": copy.deepcopy(release_record), "deployment_revisions": pins},
            "handoff_expect": {
                "execution_id": execution.id,
                "accepted_plan_version": 1,
                "claim_id": claim.id,
                "claim_generation": 1,
            },
        },
        expires_at=now + timedelta(days=1),
    )
    db.add_all(
        [
            report,
            OrchestrationAction(
                org_id=ORG_A,
                execution_id=execution.id,
                operation_key="operations-release:1",
                kind="operations_release_binding",
                status="succeeded",
                attempt=1,
                detail=copy.deepcopy(release_record),
            ),
            OrchestrationDecision(
                org_id=ORG_A,
                flow_id=flow.id,
                node_id=node.id,
                kind="node_dispatched",
                actor_id="system:engine",
                actor_kind="service",
                actor_role="service",
                reason=json.dumps({"invocation_id": correlation, "node_attempt": 1}),
            ),
            UserCredential(
                id=CONNECTION,
                org_id=ORG_A,
                user_id=APPROVER,
                service="aws",
                credential_type="aws_role",
                label="security-release",
                secret_arn="offline-security-release-role",
                agent_consent=True,
            ),
        ]
    )
    genesis = await resolve_engine_genesis(db, org_id=ORG_A, decision_id=approval.id)
    await db.commit()

    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    writer = EngineAuthorityWriter(store=store, events_table="security-release-events")
    store.client.create_table(
        TableName=writer.events_table,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
    )
    envelope = _build_envelope(
        node=node,
        genesis=genesis,
        graph_address=address,
        installation_id=456,
        issue=int(node.issue_ref),
        config=DispatchPassConfig(queue_url="https://sqs.test/security.fifo", repo=REPO, persona="operations"),
        user_id=APPROVER,
        cognito_sub="sub:" + APPROVER,
    )
    envelope["source_ref"]["provider_repository_id"] = 123
    envelope["orchestration"].update(conclusion_authority="evidence", deployment_revisions=pins)
    writer.provision(
        PendingPublish(
            node_id=node.id, org_id=ORG_A, envelope=envelope, group_id=flow.id, deduplication_id=correlation, genesis=genesis, node_attempt=1
        )
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    reservations = ReservationStore(redis_url=None, ttl_seconds=120, client=redis)
    monkeypatch.setattr(flow_budget, "_reservations", reservations)
    target = meter_target(org_id=ORG_A, flow_id=flow.id, policy=policy)
    initialized = await reservations.reserve("__initialized__", Decimal(0), [replace(target, require_initialization=False)])
    assert initialized.admitted

    secrets = SimpleNamespace(get_secret=Mock(return_value=json.dumps({"role_arn": ROLE, "default_region": "us-east-1"})))
    temporary = SimpleNamespace(
        access_key_id="testing",
        secret_access_key="testing",
        session_token="testing",
        region="us-east-1",
        expiration=(now + timedelta(hours=1)).isoformat(),
    )
    assume = Mock(return_value=temporary)
    sts = SimpleNamespace(get_caller_identity=Mock(return_value={"Account": "123456789012"}))
    aws_session = Mock(return_value=SimpleNamespace(client=lambda name, **kwargs: {"sts": sts}[name]))
    targets = DeploymentTargetResolver(secrets=secrets, assume=assume, session_factory=aws_session)
    authority = ReviewCycleServices(factory, writer=writer)
    # Dependency construction selects real services with offline transports.
    monkeypatch.setattr(generic_delivery_dispatch, "ReviewCycleServices", lambda factory: authority)
    monkeypatch.setattr(generic_delivery_dispatch, "DeploymentTargetResolver", lambda: targets)
    binding = SimpleNamespace(org_id=ORG_A, repo=REPO, installation_id=456, provider_repository_id=123)
    protected = {"invocation_id": {"S": correlation}, **{key: {"S": value} for key, value in pins.items()}}
    reservation = await generic_delivery_dispatch.reserve_dispatch(
        db,
        factory=factory,
        node=node,
        flow=flow,
        contract=contract,
        entry=release.delivery_contract.manifest(release).entry(release.target),
        binding=binding,
        execution=protected,
        dispatch_revision=DISPATCH,
    )
    await db.commit()
    await generic_delivery_dispatch.settle_dispatch(factory, reservation, org_id=ORG_A, succeeded=True)
    provider_run = {
        "id": 72,
        "run_attempt": 1,
        "status": "in_progress",
        "conclusion": None,
        "repository": {"id": 123},
        "head_sha": DISPATCH,
        "event": "workflow_dispatch",
        "path": ".github/workflows/" + WORKFLOW,
        "display_title": "ADP deployment " + correlation,
        "actor": {"id": 789},
        "triggering_actor": {"id": 789},
        "created_at": datetime.now(UTC).isoformat(),
    }

    async def provider_request(selected, method, path, **kwargs):
        assert (selected.org_id, selected.repo, selected.installation_id, selected.provider_repository_id) == (ORG_A, REPO, 456, 123)
        assert method == "GET"
        responses = {"/app": {"slug": "adp-test"}, "/users/adp-test[bot]": {"id": 789, "type": "Bot"}, f"/repos/{REPO}/actions/runs/72": provider_run}
        return Mock(json=lambda: copy.deepcopy(responses[path]))

    provider = SimpleNamespace(request=AsyncMock(side_effect=provider_request))
    monkeypatch.setattr(operations_release_job, "resolve_tenant_app_credentials", AsyncMock(return_value=(1, "test-key")))
    monkeypatch.setattr(operations_release_job, "_mint_app_jwt", Mock(return_value="test-app-token"))
    service = NativeReleaseJob(factory, provider=provider, authority=authority, targets=targets)
    job = NativeJobRequest(
        correlation=correlation,
        source_revision=SOURCE,
        dispatch_revision=DISPATCH,
        workflow=WORKFLOW,
        role_arn=ROLE,
        mutation_role_arn=MUTATION_ROLE,
        contract_digest=release.delivery_contract.digest,
    )
    claims = {
        "event_name": "workflow_dispatch",
        "run_id": "72",
        "run_attempt": "1",
        "repository": REPO,
        "repository_id": "123",
        "actor_id": "789",
        "workflow_ref": f"{REPO}/.github/workflows/{WORKFLOW}@refs/heads/main",
        "sha": DISPATCH,
        "workflow_sha": DISPATCH,
        "ref": "refs/heads/main",
    }
    await service.authorize(job, claims)
    action = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.id == reservation["id"]).execution_options(populate_existing=True))
    await db.commit()
    return SimpleNamespace(
        flow=flow,
        node=node,
        plan=plan,
        policy=policy,
        claim=claim,
        execution=execution,
        report=report,
        action=action,
        job=job,
        claims=claims,
        service=service,
        provider=provider,
        provider_run=provider_run,
        authority=authority,
        targets=targets,
        assume=assume,
        sts=sts,
        writer=writer,
        reservations=reservations,
        close=redis.aclose,
    )
