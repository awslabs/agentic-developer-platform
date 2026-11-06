"""A native job's signed identity is insufficient without its retained release."""

import importlib.util
import json
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from src.orchestration import operations_release_job as jobs
from src.orchestration.deployment_manifest import EntryStatus, PhysicalTarget, TargetEvidence
from src.orchestration.environment_leases import foundation_release_scope
from src.orchestration.execution_policy import Action, AuthorizationContext, ExecutionPolicy, PolicyLimits
from src.orchestration.models import OrchestrationAction, OrchestrationExecution, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.run_reports import OrchestrationRunReport


@pytest.fixture
def job(monkeypatch):
    source, definition, head = "a" * 40, "b" * 40, "c" * 40
    correlation = "orch:12345678-1234-1234-1234-123456789abc"
    physical = PhysicalTarget(
        "aws", "000000000000", "us-east-1", "eks-namespace", "cluster/namespace", TargetEvidence("test", "2026-10-22T00:00:00Z")
    )
    entry = SimpleNamespace(
        entry_id="foundation",
        status=EntryStatus.ENABLED,
        docs_only=False,
        release_group="atlassian-foundation",
        mutation_role_arn="arn:aws:iam::000000000000:role/adp-dev-trusted-deployment",
        connection_id="connection",
        resource_kind=physical.resource_kind,
        resource_id=physical.resource_id,
        artifact_revision=source,
        workflow=SimpleNamespace(path=".github/workflows/deploy.yml", definition_revision=definition),
        covers=lambda component: component == "gateway-backend",
    )
    node = SimpleNamespace(id="node-1", org_id="org", flow_id="flow", state="running", attempts=1, epic_ref="E1", wave_ref="W1", node_ref="N1")
    flow = SimpleNamespace(id="flow", org_id="org", slug="cycle", state="running", execution_paused=False, archived_at=None)
    shared, owner = foundation_release_scope(
        node=node, flow=flow, entry=entry, component="gateway-backend", target=physical, environment="dev", source_revision=source
    )
    action = SimpleNamespace(
        id="action",
        org_id="org",
        execution_id="execution",
        kind="operations_native_release_dispatch",
        status="prepared",
        detail={
            "dispatch_started": True,
            "correlation": correlation,
            "source_revision": source,
            "dispatch_revision": head,
            "definition_revision": definition,
            "workflow": "deploy.yml",
            "repo": "owner/repo",
            "provider_repository_id": 789,
            "branch": "main",
            "shared_release": {
                "target": asdict(physical),
                "mutation_role_arn": entry.mutation_role_arn,
                "target_key": shared.canonical_key,
                "owner": owner,
                "generation": 3,
                "manifest_entry_id": entry.entry_id,
                "environment": "dev",
            },
        },
    )
    execution = SimpleNamespace(
        id="execution",
        node_id="node-1",
        flow_id="flow",
        org_id="org",
        status="runnable",
        cycle=1,
        accepted_plan_version=1,
        claim_id="claim",
        claim_generation=3,
        handoff_receipt_ref=None,
    )
    claim = SimpleNamespace(
        provider_repository_id=789,
        id="claim",
        org_id="org",
        state="held",
        generation=3,
        active_run_id=correlation,
        claim_event_id=correlation,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    report = SimpleNamespace(
        org_id="org",
        flow_id="flow",
        node_id="node-1",
        attempt=1,
        repo="owner/repo",
        provider_repository_id=789,
        installation_id=123,
        dispatch_metadata={
            "orchestration": {
                "release_binding": {
                    "target": "foundation",
                    "connection_id": "connection",
                    "mutation_role_arn": entry.mutation_role_arn,
                    "resource_kind": physical.resource_kind,
                    "resource_id": physical.resource_id,
                    "manifest_artifact_revision": source,
                    "manifest_definition_revision": definition,
                    "workflow": entry.workflow.path,
                }
            }
        },
    )
    release = SimpleNamespace(
        target="foundation", component="gateway-backend", repository="owner/repo", workflows=["deploy.yml"], maintenance_adoption_id=None
    )
    contract = SimpleNamespace(
        environment_connection_ids=["connection"],
        plan_version=1,
        node=SimpleNamespace(operational_release_binding=release, evidence_expectations=["runtime_verified:gateway-backend"]),
    )
    monkeypatch.setattr(jobs, "accepted_node_contract", AsyncMock(return_value=contract))
    pins = AsyncMock(return_value={"source_revision": source, "definition_revision": definition, "default_branch": "main"})
    monkeypatch.setattr(jobs, "accepted_release_pins", pins)
    monkeypatch.setattr(
        jobs,
        "load_in_force_policy",
        AsyncMock(return_value=SimpleNamespace(plan_version=1, refusal=None, policy=SimpleNamespace(permits=lambda action: True))),
    )
    held = AsyncMock(return_value=True)
    monkeypatch.setattr(jobs, "shared_release_held", held)
    order = []
    monkeypatch.setattr(jobs, "_live_authority_conflict", AsyncMock(side_effect=lambda *args, **kwargs: order.append("flow_claim_plan") or None))
    monkeypatch.setattr(jobs, "resolve_tenant_app_credentials", AsyncMock(return_value=("app", "key")))
    monkeypatch.setattr(jobs, "_mint_app_jwt", lambda *args: "app-jwt")

    class Session:
        async def scalars(self, statement):
            return SimpleNamespace(all=lambda: [action])

        async def get(self, model, identifier, **kwargs):
            if kwargs.get("with_for_update"):
                order.append(model.__name__)

            return {
                OrchestrationAction: action,
                OrchestrationExecution: execution,
                OrchestrationNode: node,
                OrchestrationWorkClaim: claim,
                OrchestrationRunReport: report,
                OrchestrationFlow: flow,
            }[model]

        async def commit(self):
            return None

    @asynccontextmanager
    async def factory():
        yield Session()

    body = jobs.NativeJobRequest(
        correlation=correlation,
        source_revision=source,
        dispatch_revision=head,
        workflow="deploy.yml",
        role_arn="arn:aws:iam::000000000000:role/test",
        mutation_role_arn=entry.mutation_role_arn,
    )
    claims = dict(
        event_name="workflow_dispatch",
        run_attempt="1",
        repository="owner/repo",
        repository_id="789",
        workflow_ref="owner/repo/.github/workflows/deploy.yml@refs/heads/main",
        sha=head,
        workflow_sha=head,
        ref="refs/heads/main",
        run_id="42",
        actor_id="77",
    )
    run = dict(
        id=42,
        run_attempt=1,
        event="workflow_dispatch",
        status="in_progress",
        repository={"id": 789},
        head_sha=head,
        path=".github/workflows/deploy.yml",
        display_title="ADP deployment " + correlation,
        actor={"id": 77},
        triggering_actor={"id": 77},
        created_at=datetime.now(UTC).isoformat(),
    )

    async def request(binding, method, path, **kwargs):
        value = {"slug": "test-app"} if path == "/app" else {"type": "Bot", "id": 77} if path.startswith("/users/") else run
        return SimpleNamespace(json=lambda: value)

    provider = SimpleNamespace(request=AsyncMock(side_effect=request))
    policy = ExecutionPolicy(
        schema_version=2,
        org_id="org",
        principal_id="human",
        policy_id="policy",
        policy_hash="a" * 64,
        repository_ids=["owner/repo"],
        environment_connection_ids=["connection"],
        allowed_actions=[Action.DEPLOY],
        user_credentials={
            "permission_mode": "user_configured",
            "lifetime": "provider_managed",
            "vault_credential_ids": ["connection"],
            "aws_role_arns": ["arn:aws:iam::000000000000:role/test"],
            "actions": [Action.DEPLOY],
        },
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        limits=PolicyLimits(max_wall_clock_seconds=3600, max_spend_usd=Decimal(25), max_attempts_per_node=8, max_concurrent_actions=2),
    )
    auth = AuthorizationContext(
        policy=policy,
        accepted_plan_version=1,
        in_force_plan_version=1,
        principal_id="human",
        member_org_id="org",
        principal_can_authorize=True,
        now=datetime.now(UTC),
        observed_spend_usd=Decimal(2),
    )
    authority = SimpleNamespace(
        authority_context=AsyncMock(return_value=(None, None, SimpleNamespace(policy=policy, plan_version=1), "human", None, auth))
    )
    target = SimpleNamespace(physical=physical, role_arn=body.role_arn)

    async def resolve(session, *, authorize_scope, inspect_target=None, **kwargs):
        authorize_scope("connection", "arn:aws:iam::000000000000:role/test")
        if inspect_target is not None:
            target.observation = inspect_target(object(), None, None)
        return target

    targets = SimpleNamespace(resolve=AsyncMock(side_effect=resolve))
    return SimpleNamespace(
        guard=jobs.NativeReleaseJob(
            factory, provider=provider, manifest_loader=lambda: SimpleNamespace(entry=lambda key: entry), authority=authority, targets=targets
        ),
        body=body,
        claims=claims,
        action=action,
        execution=execution,
        claim=claim,
        report=report,
        contract=contract,
        entry=entry,
        run=run,
        held=held,
        provider=provider,
        lock_order=order,
        authority=authority,
        targets=targets,
        auth=auth,
        policy=policy,
        pins=pins,
    )


async def test_native_job_requires_signed_run_and_held_release(job):
    await job.guard.authorize(job.body, job.claims)
    assert job.held.await_count == 1
    assert job.held.await_args.kwargs["action_id"] == job.action.detail["shared_release"]["owner"]
    assert job.held.await_args.kwargs["started_at"] == datetime.fromisoformat(job.run["created_at"])
    assert job.authority.authority_context.await_count == 1
    assert job.authority.authority_context.await_args.args[4] == job.body.correlation
    assert job.targets.resolve.await_count == 1
    assert job.provider.request.await_count == 3
    assert job.action.detail["release_job"] == {"run_id": "42", "run_attempt": "1"}
    # The node anchor is taken FOR UPDATE before the authority read so the guard
    # follows the result observer's canonical node -> flow/claim/plan -> execution
    # -> action order instead of inverting against it.
    assert job.lock_order == [
        "OrchestrationNode",
        "flow_claim_plan",
        "OrchestrationExecution",
        "OrchestrationNode",
        "OrchestrationAction",
    ]


@pytest.mark.parametrize("revoked", [False, True])
async def test_deadlock_reopens_transaction_and_rechecks_current_claim(job, monkeypatch, revoked):
    class DeadlockError(Exception):
        sqlstate = "40P01"

    original = job.guard.factory
    closed, checked = [], []

    @asynccontextmanager
    async def factory():
        async with original() as session:
            try:
                yield session
            finally:
                closed.append(session)

    async def check(session, *args, **kwargs):
        checked.append(session)
        if len(checked) == 1:
            if revoked:
                job.claim.state = "released"
            raise DBAPIError(None, None, DeadlockError())
        assert checked[0] in closed and checked[0] is not session

    job.guard.factory = factory
    monkeypatch.setattr(jobs, "_live_authority_conflict", check)
    if revoked:
        with pytest.raises(HTTPException) as error:
            await job.guard.authorize(job.body, job.claims)
        assert error.value.status_code == 403
        assert "release_job" not in job.action.detail
    else:
        await job.guard.authorize(job.body, job.claims)
        assert job.action.detail["release_job"] == {"run_id": "42", "run_attempt": "1"}
    assert len(checked) == 2


@pytest.mark.parametrize("sqlstate,calls", [("40P01", 2), ("08006", 1), ("40003", 1)])
async def test_database_retry_is_bounded_and_never_replays_uncertain_outcomes(job, monkeypatch, sqlstate, calls):
    original = Exception("database error")
    original.sqlstate = sqlstate
    check = AsyncMock(side_effect=DBAPIError(None, None, original))
    monkeypatch.setattr(jobs, "_live_authority_conflict", check)
    with pytest.raises(DBAPIError):
        await job.guard.authorize(job.body, job.claims)
    assert check.await_count == calls
    assert "release_job" not in job.action.detail


async def test_native_job_accepts_selected_descendant_of_manifest_baselines(job):
    job.entry.artifact_revision = "d" * 40
    job.entry.workflow.definition_revision = "e" * 40
    job.report.dispatch_metadata["orchestration"]["release_binding"].update(
        manifest_artifact_revision="d" * 40, manifest_definition_revision="e" * 40
    )
    await job.guard.authorize(job.body, job.claims)
    assert job.action.detail["release_job"] == {"run_id": "42", "run_attempt": "1"}
    assert job.held.await_args.kwargs["release_ref"] == job.body.source_revision


@pytest.mark.parametrize("change", ["source", "definition", "branch", "ledger", "baseline", "target", "workflow", "lease"])
async def test_descendant_job_cannot_substitute_reserved_release(job, change):
    job.entry.artifact_revision = "d" * 40
    job.entry.workflow.definition_revision = "e" * 40
    job.report.dispatch_metadata["orchestration"]["release_binding"].update(
        manifest_artifact_revision="d" * 40, manifest_definition_revision="e" * 40
    )
    if change == "source":
        job.body = job.body.model_copy(update={"source_revision": "f" * 40})
        job.action.detail["source_revision"] = "f" * 40
    elif change == "definition":
        job.action.detail["definition_revision"] = "f" * 40
    elif change == "branch":
        job.action.detail["branch"] = "unapproved"
        job.claims.update(ref="refs/heads/unapproved", workflow_ref="owner/repo/.github/workflows/deploy.yml@refs/heads/unapproved")
    elif change == "ledger":
        job.pins.return_value = None
    elif change == "baseline":
        job.entry.artifact_revision = "f" * 40
    elif change == "target":
        job.entry.resource_id = "other/namespace"
    elif change == "workflow":
        job.entry.workflow.path = ".github/workflows/unsupported.yml"
    elif change == "lease":
        job.held.return_value = False
    with pytest.raises(HTTPException) as error:
        await job.guard.authorize(job.body, job.claims)
    assert error.value.status_code == 403
    assert "release_job" not in job.action.detail


async def test_replacement_writer_requires_current_transfer_and_new_signed_run(job, monkeypatch):
    transfer = AsyncMock(return_value={"verified": True})
    monkeypatch.setattr(jobs, "transfer", transfer)
    job.action.detail.update(retry_started_at=datetime.now(UTC).isoformat(), retry_previous_run_id="42")
    job.execution.handoff_receipt_ref = "operations-controller:verified"
    job.claim.active_run_id = job.claim.claim_event_id = job.execution.handoff_receipt_ref
    job.claims["run_id"] = "43"
    job.run["id"] = 43
    job.run["created_at"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    with pytest.raises(HTTPException):
        await job.guard.authorize(job.body, {**job.claims, "run_id": "42"})
    with pytest.raises(HTTPException):
        await job.guard.authorize(job.body, job.claims)
    job.run["created_at"] = datetime.now(UTC).isoformat()
    await job.guard.authorize(job.body, job.claims)
    assert job.action.detail["release_job"] == {"run_id": "43", "run_attempt": "1"}
    assert transfer.await_count >= 1
    transfer.return_value = None
    with pytest.raises(HTTPException):
        await job.guard.authorize(job.body, job.claims)


async def test_controller_dispatch_requires_held_handoff_and_current_transfer(job, monkeypatch):
    transfer = AsyncMock(return_value={"verified": True})
    monkeypatch.setattr(jobs, "transfer", transfer)
    job.action.detail["controller_dispatch"] = True
    with pytest.raises(HTTPException):
        await job.guard.authorize(job.body, job.claims)
    transfer.assert_not_awaited()
    job.execution.handoff_receipt_ref = "operations-controller:verified"
    job.claim.active_run_id = job.claim.claim_event_id = job.execution.handoff_receipt_ref
    await job.guard.authorize(job.body, job.claims)
    assert transfer.await_count >= 1
    transfer.return_value = None
    with pytest.raises(HTTPException):
        await job.guard.authorize(job.body, job.claims)


async def test_publication_intent_pins_exact_digest_after_signed_job_authorization(job, monkeypatch):
    job.action.detail["shared_release"]["source_revision"] = job.body.source_revision
    marker = AsyncMock(return_value=True)
    monkeypatch.setattr(jobs, "begin_shared_publication", marker)
    body = jobs.NativePublicationRequest(**job.body.model_dump(), image_digest="sha256:" + "d" * 64)
    await job.guard.mark_publication(body, job.claims)
    assert marker.await_args.kwargs["digest"] == body.image_digest
    assert marker.await_args.kwargs["holder"].action_id == job.action.detail["shared_release"]["owner"]
    assert job.held.await_count == 1
    marker.return_value = False
    with pytest.raises(HTTPException) as error:
        await job.guard.mark_publication(body, job.claims)
    assert error.value.status_code == 403


async def test_native_job_authorizes_connection_role_not_adapter_pod_role(job):
    pod_role = "arn:aws:iam::000000000000:role/adapter-pod"
    assert job.body.role_arn != pod_role
    await job.guard.authorize(job.body, job.claims)
    assert job.held.await_count == job.targets.resolve.await_count == 1
    with pytest.raises(HTTPException) as error:
        await job.guard.authorize(job.body.model_copy(update={"role_arn": pod_role}), job.claims)
    assert error.value.status_code == 403


async def test_native_job_refuses_same_connection_and_mutation_role(job, monkeypatch):
    role = job.entry.mutation_role_arn
    body = job.body.model_copy(update={"role_arn": role})
    monkeypatch.setattr(jobs, "authorize_action", lambda *_args, **_kwargs: SimpleNamespace(permitted=True))
    original = job.targets.resolve.side_effect

    async def resolve(*args, **kwargs):
        def authorize_scope(credential_id, _role_arn):
            kwargs["authorize_scope"](credential_id, role)

        result = await original(*args, **{**kwargs, "authorize_scope": authorize_scope})
        return SimpleNamespace(physical=result.physical, role_arn=role)

    job.targets.resolve.side_effect = resolve
    with pytest.raises(HTTPException) as error:
        await job.guard.authorize(body, job.claims)
    assert error.value.status_code == 403
    assert "release_job" not in job.action.detail


@pytest.mark.parametrize("change", ["requested_role", "manifest_role", "other_account", "admitted_drift", "missing_mapping", "reservation_drift"])
async def test_native_job_requires_reviewed_mutation_role_and_target_account(job, change):
    body = job.body
    if change == "requested_role":
        body = body.model_copy(update={"mutation_role_arn": "arn:aws:iam::000000000000:role/adp-other-trusted-deployment"})
    elif change == "manifest_role":
        job.entry.mutation_role_arn = "arn:aws:iam::000000000000:role/adp-other-trusted-deployment"
    elif change == "admitted_drift":
        different = "arn:aws:iam::000000000000:role/adp-other-trusted-deployment"
        job.entry.mutation_role_arn = different
        body = body.model_copy(update={"mutation_role_arn": different})
    elif change == "missing_mapping":
        del job.report.dispatch_metadata["orchestration"]["release_binding"]["mutation_role_arn"]
    elif change == "reservation_drift":
        job.action.detail["shared_release"]["mutation_role_arn"] = "arn:aws:iam::000000000000:role/adp-other-trusted-deployment"
    else:
        other = "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment"
        job.entry.mutation_role_arn = other
        body = body.model_copy(update={"mutation_role_arn": other})
    with pytest.raises(HTTPException) as error:
        await job.guard.authorize(body, job.claims)
    assert error.value.status_code == 403
    assert "release_job" not in job.action.detail
    job.held.assert_not_awaited()


async def test_adapter_client_body_authorizes_registered_role_but_not_pod_role(job):
    workflow = "tools-platform-deploy.yml"
    job.entry.workflow.path = ".github/workflows/" + workflow
    job.action.detail["workflow"] = workflow
    job.report.dispatch_metadata["orchestration"]["release_binding"]["workflow"] = job.entry.workflow.path
    job.contract.node.operational_release_binding.workflows = [workflow]
    job.run["path"] = job.entry.workflow.path
    job.claims["workflow_ref"] = "owner/repo/" + job.entry.workflow.path + "@refs/heads/main"
    job.body = job.body.model_copy(update={"workflow": workflow})

    spec = importlib.util.spec_from_file_location("adapter_release_guard", Path(__file__).resolve().parents[3] / "tools/adapters/guard_release.py")
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ATTEMPT": "1",
        "ADP_SOURCE_REVISION": job.body.source_revision,
        "ADP_DEFINITION_REVISION": job.body.dispatch_revision,
        "ADP_CORRELATION": job.body.correlation,
        "ADP_TOOLS_DEPLOY_ROLE_ARN": job.body.mutation_role_arn,
        "ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN": job.body.role_arn,
        "ADP_TOOLS_ADAPTER_ROLE_ARN": "arn:aws:iam::000000000000:role/adapter-pod",
        "ADP_TOOLS_EVAL_GATEWAY_URL": "https://gateway.example.test",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.actions.githubusercontent.com/token?request=one",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "private-test-request-token",
    }

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            return json.dumps(self.payload).encode()[:size]

    class Opener:
        def __init__(self):
            self.requests = []

        def open(self, request, timeout):
            assert timeout == (15 if not self.requests else 45)
            self.requests.append(request)
            return Response({"value": "header.body.signature"} if len(self.requests) == 1 else {"authorized": True})

    opener = Opener()
    client.authorize(environment, opener)
    assert opener.requests[1].full_url == "https://gateway.example.test/api/orchestration/operations-release-job/check"
    emitted = jobs.NativeJobRequest.model_validate_json(opener.requests[1].data)
    assert emitted.mutation_role_arn == job.entry.mutation_role_arn
    assert emitted.role_arn != environment["ADP_TOOLS_ADAPTER_ROLE_ARN"]
    await job.guard.authorize(emitted, job.claims)
    with pytest.raises(HTTPException) as error:
        await job.guard.authorize(emitted.model_copy(update={"role_arn": environment["ADP_TOOLS_ADAPTER_ROLE_ARN"]}), job.claims)
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    "change",
    ["expired", "membership", "grant", "unknown_budget", "exhausted_budget", "environment", "role", "credential", "authority"],
)
async def test_native_job_rechecks_current_deployment_authority_before_pin(job, change):
    auth = job.auth
    policy = job.policy
    if change == "expired":
        policy = policy.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    elif change == "membership":
        auth = replace(auth, member_org_id=None)
    elif change == "grant":
        auth = replace(auth, grant_revoked=True)
    elif change == "unknown_budget":
        auth = replace(auth, observed_spend_usd=None)
    elif change == "exhausted_budget":
        auth = replace(auth, observed_spend_usd=Decimal(25))
    elif change == "environment":
        policy = policy.model_copy(update={"environment_connection_ids": []})
    elif change == "authority":
        from src.orchestration.review_cycle import CycleBlockedError

        job.authority.authority_context.side_effect = CycleBlockedError("grant_revoked_or_unverifiable")
    else:

        async def resolve(session, *, authorize_scope, **kwargs):
            authorize_scope("other" if change == "credential" else "connection", "arn:aws:iam::000000000000:role/other")
            return SimpleNamespace(physical=job.targets.resolve.return_value)

        job.targets.resolve.side_effect = resolve
    job.authority.authority_context.return_value = (
        None,
        None,
        SimpleNamespace(policy=policy, plan_version=1),
        "human",
        None,
        replace(auth, policy=policy, now=datetime.now(UTC)),
    )
    with pytest.raises(HTTPException) as error:
        await job.guard.authorize(job.body, job.claims)
    assert error.value.status_code == 403
    assert "release_job" not in job.action.detail


@pytest.mark.parametrize("change", ["fresh_evidence", "different_account", "missing_scope"])
async def test_native_job_requires_fresh_verified_same_target(job, change):
    raw = job.action.detail["shared_release"]["target"]
    original = PhysicalTarget(**{**raw, "evidence": TargetEvidence(**raw["evidence"])})

    async def resolve(session, *, authorize_scope, **kwargs):
        if change != "missing_scope":
            authorize_scope("connection", "arn:aws:iam::000000000000:role/test")
        physical = replace(
            original,
            account_id="111111111111" if change == "different_account" else original.account_id,
            evidence=TargetEvidence("fresh-readback", datetime.now(UTC).isoformat()),
        )
        return SimpleNamespace(physical=physical, role_arn=job.body.role_arn)

    job.targets.resolve.side_effect = resolve
    if change == "fresh_evidence":
        await job.guard.authorize(job.body, job.claims)
        assert job.action.detail["release_job"] == {"run_id": "42", "run_attempt": "1"}
    else:
        with pytest.raises(HTTPException) as error:
            await job.guard.authorize(job.body, job.claims)
        assert error.value.status_code == 403
        assert "release_job" not in job.action.detail


@pytest.mark.parametrize(
    "change",
    ["rerun", "push", "repository", "source", "plan", "blocked", "released", "expired", "wrong_owner", "policy", "group", "binding", "lease"],
)
async def test_native_job_refuses_stale_source_or_owner(job, change, monkeypatch):
    if change == "rerun":
        job.claims["run_attempt"] = "2"
    elif change == "push":
        job.claims["event_name"] = "push"
    elif change == "repository":
        job.claims["repository_id"] = "790"
    elif change == "source":
        job.body = job.body.model_copy(update={"source_revision": "f" * 40})
    elif change == "plan":
        job.execution.accepted_plan_version = 2
    elif change == "blocked":
        job.execution.status = "blocked"
    elif change == "released":
        job.claim.state = "released"
    elif change == "expired":
        job.claim.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "wrong_owner":
        job.action.detail["shared_release"]["owner"] = "other"
    elif change == "policy":
        jobs.load_in_force_policy.return_value.refusal = "revoked"
    elif change == "group":
        job.entry.release_group = "other"
    elif change == "binding":
        job.report.dispatch_metadata["orchestration"]["release_binding"]["connection_id"] = "other"
    elif change == "lease":
        job.held.return_value = False
    with pytest.raises(HTTPException) as error:
        await job.guard.authorize(job.body, job.claims)
    assert error.value.status_code == 403


@pytest.mark.parametrize("change", ["actor", "trigger", "run_attempt", "event", "run_sha", "created", "earlier", "displaced"])
async def test_native_job_refuses_wrong_or_outdated_provider_run(job, change):
    if change == "actor":
        job.run["actor"]["id"] = 78
    elif change == "trigger":
        job.run["triggering_actor"]["id"] = 78
    elif change == "run_attempt":
        job.run["run_attempt"] = 2
    elif change == "event":
        job.run["event"] = "push"
    elif change == "run_sha":
        job.run["head_sha"] = "f" * 40
    elif change == "created":
        job.run.pop("created_at")
    elif change == "earlier":
        job.held.return_value = False
    elif change == "displaced":
        original = job.provider.request.side_effect

        async def displaced(*args, **kwargs):
            result = await original(*args, **kwargs)
            if "/actions/runs/" in args[2]:
                job.claim.generation += 1
            return result

        job.provider.request.side_effect = displaced
    with pytest.raises(HTTPException) as error:
        await job.guard.authorize(job.body, job.claims)
    assert error.value.status_code == 403


async def test_native_job_route_requires_distinct_signed_audience(job, monkeypatch):
    authority = AsyncMock(return_value=job.claims)
    monkeypatch.setattr(jobs, "github_claims", authority)
    guard = SimpleNamespace(authorize=AsyncMock())
    response = SimpleNamespace(headers={})
    request = SimpleNamespace(headers={"authorization": "Bearer signed-oidc"})
    assert await jobs.check_native_job(job.body, request, response, guard=guard) == {"authorized": True}
    authority.assert_awaited_once_with("signed-oidc", audience=jobs.AUDIENCE)
    guard.authorize.assert_awaited_once_with(job.body, job.claims)
    assert response.headers["Cache-Control"] == "no-store"
    with pytest.raises(HTTPException):
        await jobs.check_native_job(job.body, SimpleNamespace(headers={}), response, guard=guard)
    guard.authorize.assert_awaited_once()


async def test_target_route_requires_signed_job_and_returns_only_authorized_identity(job, monkeypatch):
    authority = AsyncMock(return_value=job.claims)
    monkeypatch.setattr(jobs, "github_claims", authority)
    selected = {
        "connection_id": "selected",
        "resource_kind": "eks-namespace",
        "resource_id": "cluster/namespace",
        "account_id": "000000000000",
        "region": "us-east-1",
    }
    guard = SimpleNamespace(target_identity=AsyncMock(return_value=selected))
    response = SimpleNamespace(headers={})
    request = SimpleNamespace(headers={"authorization": "Bearer signed-oidc"})
    body = job.body.model_copy(update={"contract_digest": "c" * 64})
    result = await jobs.read_native_target(body, request, response, guard=guard)
    assert result == {
        "authorized": True,
        "target": selected,
        "source_revision": body.source_revision,
        "contract_digest": body.contract_digest,
        "run_id": job.claims["run_id"],
        "run_attempt": job.claims["run_attempt"],
    }
    authority.assert_awaited_once_with("signed-oidc", audience=jobs.AUDIENCE)
    guard.target_identity.assert_awaited_once_with(body, job.claims)
    assert response.headers["Cache-Control"] == "no-store"
    with pytest.raises(HTTPException):
        await jobs.read_native_target(body, SimpleNamespace(headers={}), response, guard=guard)
    guard.target_identity.assert_awaited_once()


async def test_target_route_refuses_legacy_job_without_delivery_contract(job):
    with pytest.raises(HTTPException) as error:
        await job.guard.target_identity(job.body, job.claims)
    assert error.value.status_code == 403


async def test_native_job_pins_first_run_and_rejects_distinct_valid_first_attempt(job):
    await job.guard.authorize(job.body, job.claims)
    await job.guard.authorize(job.body, job.claims)
    assert job.action.detail["release_job"] == {"run_id": "42", "run_attempt": "1"}
    job.claims["run_id"] = "43"
    job.run["id"] = 43
    with pytest.raises(HTTPException) as error:
        await job.guard.authorize(job.body, job.claims)
    assert error.value.status_code == 403
    assert job.action.detail["release_job"] == {"run_id": "42", "run_attempt": "1"}


async def test_native_job_refuses_role_different_from_current_registered_credential(job):
    job.body = job.body.model_copy(update={"role_arn": "arn:aws:iam::000000000000:role/other"})
    with pytest.raises(HTTPException) as error:
        await job.guard.authorize(job.body, job.claims)
    assert error.value.status_code == 403
    assert "release_job" not in job.action.detail


async def test_publication_settlement_requires_same_pinned_run_and_digest(job, monkeypatch):
    job.action.detail["shared_release"]["source_revision"] = job.body.source_revision
    marker = AsyncMock(return_value=True)
    monkeypatch.setattr(jobs, "settle_shared_publication", marker)
    body = jobs.NativePublicationRequest(**job.body.model_dump(), image_digest="sha256:" + "d" * 64)
    from src.orchestration.operations_publication_recovery import ImageReadback

    observed = ImageReadback("000000000000", "us-east-1", "adp-tools-dev-adapters", body.source_revision, body.image_digest, datetime.now(UTC))
    read = Mock(return_value=observed)
    monkeypatch.setattr(jobs, "read_pending_image", read)
    await job.guard.settle_publication(body, job.claims)
    assert read.call_args.kwargs["account_id"] == "000000000000"
    assert read.call_args.kwargs["image_digest"] == body.image_digest
    assert marker.await_args.kwargs["digest"] == body.image_digest
    assert marker.await_args.kwargs["holder"].action_id == job.action.detail["shared_release"]["owner"]
    with pytest.raises(HTTPException) as error:
        await job.guard.settle_publication(body, {**job.claims, "run_id": "43"})
    assert error.value.status_code == 403
    assert marker.await_count == 1


async def test_publication_settlement_refuses_resolver_without_readback(job, monkeypatch):
    job.action.detail["shared_release"]["source_revision"] = job.body.source_revision
    marker = AsyncMock(return_value=True)
    monkeypatch.setattr(jobs, "settle_shared_publication", marker)
    original = job.targets.resolve.side_effect

    async def ignore_readback(session, *, authorize_scope, **kwargs):
        return await original(session, authorize_scope=authorize_scope)

    job.targets.resolve.side_effect = ignore_readback
    body = jobs.NativePublicationRequest(**job.body.model_dump(), image_digest="sha256:" + "d" * 64)
    with pytest.raises(HTTPException) as error:
        await job.guard.settle_publication(body, job.claims)
    assert error.value.status_code == 403
    marker.assert_not_awaited()


@pytest.mark.parametrize("readback", [None, "wrong_digest", "read_unavailable"])
async def test_publication_settlement_keeps_pending_marker_without_matching_scoped_readback(job, monkeypatch, readback):
    from src.orchestration.operations_publication_recovery import ImageReadback
    from src.orchestration.review_cycle import CycleBlockedError

    job.action.detail["shared_release"]["source_revision"] = job.body.source_revision
    marker = AsyncMock(return_value=True)
    monkeypatch.setattr(jobs, "settle_shared_publication", marker)
    body = jobs.NativePublicationRequest(**job.body.model_dump(), image_digest="sha256:" + "d" * 64)

    def read(*args, **kwargs):
        if readback == "read_unavailable":
            raise CycleBlockedError("operations_publication_read_unavailable")
        if readback == "wrong_digest":
            return ImageReadback("000000000000", "us-east-1", "adp-tools-dev-adapters", body.source_revision, "sha256:" + "f" * 64, datetime.now(UTC))
        return None

    monkeypatch.setattr(jobs, "read_pending_image", read)
    with pytest.raises(HTTPException) as error:
        await job.guard.settle_publication(body, job.claims)
    assert error.value.status_code == 403
    marker.assert_not_awaited()


async def test_terminal_run_requires_recorded_original_pin_before_provider_read(job):
    binding = SimpleNamespace(org_id="org", repo="owner/repo", provider_repository_id=789)
    with pytest.raises(HTTPException) as error:
        await job.guard.verify_terminal_run(binding, job.action.detail)
    assert error.value.status_code == 403
    assert job.provider.request.await_count == 0


async def test_terminal_run_rereads_original_app_actor_and_job(job):
    await job.guard.authorize(job.body, job.claims)
    job.action.detail["shared_release"]["source_revision"] = job.body.source_revision
    job.run.update(status="completed", conclusion="failure")
    binding = SimpleNamespace(org_id="org", repo="owner/repo", provider_repository_id=789)
    run, started_at = await job.guard.verify_terminal_run(binding, job.action.detail)
    assert run is job.run
    assert started_at == datetime.fromisoformat(job.run["created_at"])
    assert job.provider.request.await_count == 6
    assert job.held.await_count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"status": "in_progress"},
        {"conclusion": None},
        {"run_attempt": 2},
        {"actor": {"id": 88}},
        {"triggering_actor": {"id": 88}},
        {"head_sha": "e" * 40},
        {"repository": {"id": 790}},
        {"path": ".github/workflows/other.yml"},
        {"created_at": "2999-01-01T00:00:00Z"},
    ],
)
async def test_terminal_run_refuses_other_runs_and_nonterminal_states(job, change):
    await job.guard.authorize(job.body, job.claims)
    job.action.detail["shared_release"]["source_revision"] = job.body.source_revision
    job.run.update({"status": "completed", "conclusion": "success", **change})
    binding = SimpleNamespace(org_id="org", repo="owner/repo", provider_repository_id=789)
    with pytest.raises(HTTPException) as error:
        await job.guard.verify_terminal_run(binding, job.action.detail)
    assert error.value.status_code == 403
    assert job.held.await_count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"source_revision": None},
        {"release_job": {"run_id": "43", "run_attempt": "1"}},
        {"release_job": {"run_id": "42", "run_attempt": "2"}},
        {"shared_release": {"source_revision": "f" * 40}},
        {"dispatch_revision": "e" * 40},
        {"workflow": "other.yml"},
        {"repo": "other/repo"},
    ],
)
async def test_terminal_run_refuses_changed_saved_job_and_source_pins(job, change):
    await job.guard.authorize(job.body, job.claims)
    job.action.detail["shared_release"]["source_revision"] = job.body.source_revision
    job.run.update(status="completed", conclusion="success")
    binding = SimpleNamespace(org_id="org", repo="owner/repo", provider_repository_id=789)
    with pytest.raises(HTTPException) as error:
        await job.guard.verify_terminal_run(binding, {**job.action.detail, **change})
    assert error.value.status_code == 403
    assert job.held.await_count == 1


@pytest.mark.parametrize(
    "change",
    [
        "ok",
        "absent_image",
        "retry",
        "nonterminal",
        "missing_image",
        "displaced",
        "action_changed",
        "lease_changed",
        "connection_changed",
        "report_changed",
        "selected_descendant",
        "unapproved_source",
        "unapproved_definition",
    ],
)
async def test_controller_recovers_only_original_terminal_image(job, monkeypatch, change):
    from src.orchestration import operations_release_job as release_jobs
    from src.orchestration.review_cycle import CycleBlockedError

    await job.guard.authorize(job.body, job.claims)
    job.action.detail["shared_release"]["source_revision"] = job.body.source_revision
    if change == "selected_descendant":
        job.entry.artifact_revision = "d" * 40
        job.entry.workflow.definition_revision = "e" * 40
        job.report.dispatch_metadata["orchestration"]["release_binding"].update(
            manifest_artifact_revision="d" * 40, manifest_definition_revision="e" * 40
        )
    if change == "unapproved_source":
        job.action.detail["source_revision"] = "f" * 40
        job.action.detail["shared_release"]["source_revision"] = "f" * 40
    if change == "unapproved_definition":
        job.action.detail["definition_revision"] = "f" * 40
    job.execution.claim_generation += 1
    job.run.update(status="completed", conclusion="failure")
    if change == "nonterminal":
        job.run.update(status="in_progress", conclusion=None)
    if change == "displaced":
        job.action.detail["shared_release"]["generation"] = job.execution.claim_generation + 1
    if change == "connection_changed":
        job.contract.environment_connection_ids = ["another-connection"]
    if change == "report_changed":
        job.report.dispatch_metadata["orchestration"]["release_binding"]["connection_id"] = "another-connection"
    digest = "sha256:" + "d" * 64
    original_factory = job.guard.factory
    async with original_factory() as session:
        node = await session.get(OrchestrationNode, "node-1")
    state = SimpleNamespace(
        node=node,
        row=job.execution,
        report=SimpleNamespace(run_id=job.body.correlation, dispatch_metadata=job.report.dispatch_metadata),
        contract=job.contract,
        binding=SimpleNamespace(org_id="org", repo="owner/repo", provider_repository_id=789),
        values=(None, None, SimpleNamespace(policy=job.policy), "human", None, job.auth),
    )
    services = SimpleNamespace(authorize=AsyncMock(return_value=state))
    context = SimpleNamespace(identity=SimpleNamespace(node_id="node-1"))
    commits = []

    @asynccontextmanager
    async def factory():
        async with original_factory() as session:
            session.scalar = AsyncMock(return_value=SimpleNamespace(publication_pending_digest=digest))
            session.commit = AsyncMock(side_effect=lambda: commits.append(True))
            yield session

    job.guard.factory = factory
    raw = job.action.detail["shared_release"]["target"]
    image = release_jobs.ImageReadback(
        raw["account_id"], raw["region"], "adp-tools-dev-adapters", job.body.source_revision, digest, datetime.now(UTC)
    )
    absence = release_jobs.ImageAbsence(
        raw["account_id"], raw["region"], "adp-tools-dev-adapters", job.body.source_revision, digest, datetime.now(UTC)
    )
    monkeypatch.setattr(
        release_jobs,
        "read_pending_image",
        lambda *args, **kwargs: None if change == "missing_image" else absence if change in {"absent_image", "retry"} else image,
    )
    recover = AsyncMock(return_value=change != "lease_changed")
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(release_jobs, "recover_shared_publication", recover)
    monkeypatch.setattr(release_jobs, "record_shared_publication_absence", record)
    rollforward = AsyncMock(return_value=True)
    monkeypatch.setattr(release_jobs, "rollforward_shared_publication", rollforward)
    if change == "action_changed":
        original = job.targets.resolve.side_effect

        async def change_after_read(*args, **kwargs):
            result = await original(*args, **kwargs)
            job.action.detail = {**job.action.detail, "dispatch_revision": "e" * 40}
            return result

        job.targets.resolve.side_effect = change_after_read
    if change == "absent_image":
        assert await job.guard.recover_terminal_publication(context, services) == absence
        assert record.await_args.kwargs["image_absence"] == absence
        assert record.await_args.kwargs["terminal_evidence"] == "github/owner/repo/actions/runs/42:failure"
        recover.assert_not_awaited()
        assert commits == [True]
        return
    if change == "retry":
        assert (
            await job.guard.recover_terminal_publication(
                context,
                services,
                reserve_retry=True,
                expected_workflow=job.body.workflow,
                expected_dispatch_revision=job.body.dispatch_revision,
            )
            == absence
        )
        assert rollforward.await_args.kwargs["successor"].generation == 4
        assert job.action.detail["retry_previous_run_id"] == "42"
        assert "release_job" not in job.action.detail
        assert job.action.detail["shared_release"]["generation"] == 4
        assert len(commits) == 1
        return
    if change == "nonterminal":
        assert await job.guard.recover_terminal_publication(context, services) is False
        assert not commits
        record.assert_not_awaited()
        recover.assert_not_awaited()
        return
    if change not in {"ok", "selected_descendant"}:
        with pytest.raises(CycleBlockedError):
            await job.guard.recover_terminal_publication(context, services)
        assert not commits
        if change in {"nonterminal", "missing_image", "displaced", "action_changed", "connection_changed", "report_changed"}:
            recover.assert_not_awaited()
        record.assert_not_awaited()
        return
    assert await job.guard.recover_terminal_publication(context, services)
    assert recover.await_args.kwargs["holder"].generation == 3
    assert recover.await_args.kwargs["image_readback"] == image
    assert recover.await_args.kwargs["terminal_evidence"] == "github/owner/repo/actions/runs/42:failure"
    assert services.authorize.await_count == 2
    assert commits == [True]
    record.assert_not_awaited()
