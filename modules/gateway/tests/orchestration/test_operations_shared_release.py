"""Native operations reservations use accepted lineage, not D2 correlation."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration import operations_shared_release as releases
from src.orchestration.deployment_manifest import EntryStatus, PhysicalTarget, TargetEvidence
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationExecution, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.review_cycle import CycleBlockedError


@pytest.fixture
def reservation(monkeypatch):
    source, definition = "a" * 40, "b" * 40
    physical = PhysicalTarget(
        "aws",
        "000000000000",
        "us-east-1",
        "eks-namespace",
        "sample-cluster/namespace",
        TargetEvidence("test-role", "2026-10-01T00:00:00+00:00"),
    )
    release = SimpleNamespace(target="entry", component="gateway-backend", repository="owner/repo", workflows=["deploy.yml"])
    node = SimpleNamespace(
        id="node-1",
        org_id="org",
        flow_id="flow",
        epic_ref="epic",
        wave_ref="w03b-foundation-release",
        node_ref="story",
        state="running",
        attempts=1,
    )
    flow = SimpleNamespace(
        id="flow",
        org_id="org",
        slug="atlassian-adapters-8445-parallel",
        state="running",
        execution_paused=False,
        archived_at=None,
    )
    row = SimpleNamespace(
        id="execution",
        org_id="org",
        node_id="node-1",
        flow_id="flow",
        cycle=1,
        claim_id="claim",
        claim_generation=3,
        revision=7,
        pending_action_key="dispatch",
    )
    action = SimpleNamespace(kind="operations_continuation_deploy", status="prepared", detail={"workflow": "deploy.yml"})
    workflow = SimpleNamespace(path=".github/workflows/deploy.yml", definition_revision=definition, allowed_inputs={"environment": ["dev"]})
    entry = SimpleNamespace(
        entry_id="entry",
        status=EntryStatus.ENABLED,
        docs_only=False,
        release_group="atlassian-foundation",
        mutation_role_arn="arn:aws:iam::000000000000:role/adp-dev-trusted-deployment",
        connection_id="connection",
        resource_kind=physical.resource_kind,
        resource_id=physical.resource_id,
        artifact_revision=source,
        workflow=workflow,
        covers=lambda component: component == "gateway-backend",
    )
    recorded = dict(
        target="entry",
        connection_id="connection",
        mutation_role_arn=entry.mutation_role_arn,
        resource_kind=entry.resource_kind,
        resource_id=entry.resource_id,
        manifest_artifact_revision=source,
        manifest_definition_revision=definition,
        workflow=workflow.path,
    )
    state = SimpleNamespace(
        node=node,
        row=row,
        report=SimpleNamespace(
            run_id="orch:12345678-1234-1234-1234-123456789abc", dispatch_metadata={"orchestration": {"release_binding": recorded}}
        ),
        contract=SimpleNamespace(
            node=SimpleNamespace(operational_release_binding=release, evidence_expectations=["runtime_verified:gateway-backend"]),
            environment_connection_ids=["connection"],
            plan_version=1,
        ),
        binding=SimpleNamespace(repo="owner/repo", provider_repository_id=789),
        execution={
            "source_revision": {"S": source},
            "definition_revision": {"S": definition},
            "default_branch": {"S": "main"},
            "invocation_id": {"S": "orch:12345678-1234-1234-1234-123456789abc"},
        },
        values=(None, None, SimpleNamespace(policy="test-policy"), "human", None, "test-auth"),
    )
    context = SimpleNamespace(
        execution=SimpleNamespace(id="execution", revision=7),
        identity=SimpleNamespace(claim_id="claim", claim_generation=3),
    )

    class Session:
        locked = False

        def add(self, row):
            row.id = "native-action"
            self.native_action = row

        async def flush(self):
            return None

        async def get(self, model, identifier, **kwargs):
            if kwargs.get("with_for_update"):
                self.locked = True
            return {OrchestrationFlow: flow, OrchestrationExecution: row, OrchestrationNode: node}[model]

        async def scalar(self, _statement):
            if _statement.column_descriptions[0]["entity"] is OrchestrationAcceptedPlan:
                return 1
            if any(str(value).startswith("native-release:") for value in _statement.compile().params.values()):
                return getattr(self, "native_action", None)
            return action

    resolver = SimpleNamespace(
        resolve=AsyncMock(return_value=SimpleNamespace(physical=physical, role_arn="arn:aws:iam::000000000000:role/connection"))
    )
    lease = AsyncMock(return_value=SimpleNamespace(applied=True))
    monkeypatch.setattr(releases, "authorize_action", lambda *args, **kwargs: SimpleNamespace(permitted=True))
    monkeypatch.setattr(releases, "acquire_lease", lease)
    monkeypatch.setattr(releases, "transfer", AsyncMock(return_value=True))
    monkeypatch.setattr(releases, "_live_authority_conflict", AsyncMock(return_value=None))
    monkeypatch.setattr(releases, "replace", lambda auth, **kwargs: auth)
    pins = AsyncMock(return_value={"source_revision": source, "definition_revision": definition, "default_branch": "main"})
    monkeypatch.setattr(releases, "accepted_release_pins", pins)
    return SimpleNamespace(
        session=Session(), state=state, context=context, entry=entry, resolver=resolver, lease=lease, action=action, physical=physical, pins=pins
    )


async def test_native_release_reserves_accepted_owner_and_physical_target(reservation):
    prepared = reservation
    result = await releases.reserve_native_release(
        prepared.session,
        context=prepared.context,
        state=prepared.state,
        workflow="deploy.yml",
        environment="dev",
        dispatch_revision="b" * 40,
        resolver=prepared.resolver,
        manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
    )
    assert result.applied
    assert prepared.lease.await_args.kwargs["holder"].action_id == "foundation:node-1:gateway-backend:" + "a" * 40
    assert prepared.lease.await_args.kwargs["target"].resource_kind == "shared-release-group"
    assert prepared.session.native_action.detail["shared_release"]["target"]["resource_id"] == prepared.physical.resource_id
    assert prepared.session.native_action.detail["controller_dispatch"] is True
    assert prepared.action.detail["native_release_action_id"] == prepared.session.native_action.id
    assert prepared.session.native_action.detail["shared_release"]["mutation_role_arn"] == prepared.entry.mutation_role_arn
    assert prepared.session.native_action.detail["shared_release"]["generation"] == 3


@pytest.mark.parametrize("approved", [False, True])
async def test_adapter_stage_requires_reviewed_workflow_input(reservation, approved):
    prepared = reservation
    workflow = "tools-platform-deploy.yml"
    release = prepared.state.contract.node.operational_release_binding
    release.component = "tool-adapters"
    release.workflows = [workflow]
    prepared.state.contract.node.evidence_expectations = ["runtime_verified:tool-adapters"]
    prepared.entry.workflow.path = f".github/workflows/{workflow}"
    prepared.entry.workflow.allowed_inputs["stage"] = ["tool-adapters"] if approved else []
    prepared.entry.covers = lambda component: component == "tool-adapters"
    prepared.state.report.dispatch_metadata["orchestration"]["release_binding"]["workflow"] = prepared.entry.workflow.path
    prepared.action.detail["workflow"] = workflow
    if not approved:
        with pytest.raises(CycleBlockedError, match="operations_shared_release_scope_changed"):
            await releases.reserve_native_release(
                prepared.session,
                context=prepared.context,
                state=prepared.state,
                workflow=workflow,
                environment="dev",
                dispatch_revision="b" * 40,
                resolver=prepared.resolver,
                manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
            )
        prepared.lease.assert_not_awaited()
        return
    result = await releases.reserve_native_release(
        prepared.session,
        context=prepared.context,
        state=prepared.state,
        workflow=workflow,
        environment="dev",
        dispatch_revision="b" * 40,
        resolver=prepared.resolver,
        manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
    )
    assert result.applied
    assert prepared.session.native_action.detail["workflow"] == workflow


async def test_continuation_reservation_refuses_previous_native_dispatch(reservation):
    prepared = reservation
    prepared.session.native_action = SimpleNamespace(id="old-native-action")
    with pytest.raises(CycleBlockedError, match="operations_shared_release_intent_changed"):
        await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="dev",
            dispatch_revision="b" * 40,
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
    prepared.lease.assert_not_awaited()


async def test_queued_continuation_reserves_once_after_lease_contention(reservation):
    prepared = reservation

    async def reserve():
        return await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="dev",
            dispatch_revision="b" * 40,
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )

    prepared.lease.return_value = SimpleNamespace(applied=False)
    with pytest.raises(CycleBlockedError, match="operations_shared_release_held"):
        await reserve()
    assert not hasattr(prepared.session, "native_action")
    # The runner records UNCERTAIN for the refused effect and preserves the
    # action status when it prepares the same operation key on a later tick.
    prepared.action.status = "unknown"
    prepared.lease.return_value = SimpleNamespace(applied=True)
    assert (await reserve()).applied
    native = prepared.session.native_action
    assert prepared.action.detail["native_release_action_id"] == native.id
    assert native.detail["controller_dispatch"] is True
    with pytest.raises(CycleBlockedError, match="operations_shared_release_intent_changed"):
        await reserve()
    assert prepared.session.native_action is native
    assert prepared.lease.await_count == 2


@pytest.mark.parametrize("marker", ["native_retry", "dispatch_started", "native_release_action_id", "shared_release"])
async def test_unknown_continuation_with_dispatch_or_retry_marker_cannot_reserve(reservation, marker):
    prepared = reservation
    prepared.action.status = "unknown"
    prepared.action.detail[marker] = "prior-dispatch" if marker == "native_release_action_id" else True
    with pytest.raises(CycleBlockedError, match="operations_shared_release_intent_changed"):
        await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="dev",
            dispatch_revision="b" * 40,
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
    prepared.lease.assert_not_awaited()
    assert not hasattr(prepared.session, "native_action")


@pytest.mark.parametrize("status", ["succeeded", "failed", "dispatched"])
async def test_completed_or_dispatched_continuation_cannot_reserve_again(reservation, status):
    prepared = reservation
    prepared.action.status = status
    with pytest.raises(CycleBlockedError, match="operations_shared_release_intent_changed"):
        await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="dev",
            dispatch_revision="b" * 40,
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
    prepared.lease.assert_not_awaited()
    assert not hasattr(prepared.session, "native_action")


@pytest.mark.parametrize("change", ["entry", "environment", "generation", "action", "contender", "admitted_drift", "missing_mapping"])
async def test_native_release_refuses_changed_scope_or_owner(reservation, change):
    prepared = reservation
    if change == "entry":
        prepared.entry.resource_id = "other/namespace"
    if change == "admitted_drift":
        prepared.entry.mutation_role_arn = "arn:aws:iam::000000000000:role/adp-other-trusted-deployment"
    if change == "missing_mapping":
        del prepared.state.report.dispatch_metadata["orchestration"]["release_binding"]["mutation_role_arn"]
    if change == "generation":
        prepared.context.identity.claim_generation = 2
    if change == "action":
        prepared.action.kind = "correlated_workflow_observation"
    if change == "contender":
        prepared.lease.return_value = SimpleNamespace(applied=False)
    with pytest.raises(CycleBlockedError):
        await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="prod" if change == "environment" else "dev",
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
    assert "shared_release" not in prepared.action.detail
    if change in {"entry", "environment", "generation", "action", "admitted_drift", "missing_mapping"}:
        prepared.lease.assert_not_awaited()


@pytest.mark.parametrize("change", ["other_account", "same_role", "missing_role"])
async def test_native_reservation_refuses_mutation_role_drift_before_acquiring_lease(reservation, change):
    prepared = reservation
    if change == "other_account":
        prepared.entry.mutation_role_arn = "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment"
        prepared.state.report.dispatch_metadata["orchestration"]["release_binding"]["mutation_role_arn"] = prepared.entry.mutation_role_arn
    elif change == "same_role":
        prepared.resolver.resolve.return_value.role_arn = prepared.entry.mutation_role_arn
    else:
        prepared.resolver.resolve.return_value.role_arn = None
    with pytest.raises(CycleBlockedError, match="operations_shared_release_mutation_role_changed"):
        await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="dev",
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
    prepared.lease.assert_not_awaited()
    assert "shared_release" not in prepared.action.detail


async def test_native_owner_loss_during_target_read_refuses_lease(reservation):
    prepared = reservation
    releases.transfer.return_value = None
    with pytest.raises(CycleBlockedError, match="operations_shared_release_owner_changed"):
        await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="dev",
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
    prepared.resolver.resolve.assert_awaited_once()
    prepared.lease.assert_not_awaited()
    assert "shared_release" not in prepared.action.detail


async def test_native_target_role_must_be_authorized_before_reserving(reservation, monkeypatch):
    prepared = reservation
    monkeypatch.setattr(releases, "authorize_action", lambda *args, **kwargs: SimpleNamespace(permitted=False))

    async def resolve(*args, **kwargs):
        kwargs["authorize_scope"]("credential", "arn:aws:iam::000000000000:role/example")
        return SimpleNamespace(physical=prepared.physical, role_arn="arn:aws:iam::000000000000:role/connection")

    prepared.resolver.resolve = AsyncMock(side_effect=resolve)
    with pytest.raises(CycleBlockedError, match="operations_shared_release_target_not_authorized"):
        await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="dev",
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
    prepared.lease.assert_not_awaited()
    assert "shared_release" not in prepared.action.detail


async def test_native_target_read_finishes_before_execution_lock(reservation):
    prepared = reservation

    async def resolve(*args, **kwargs):
        assert not prepared.session.locked
        return SimpleNamespace(physical=prepared.physical, role_arn="arn:aws:iam::000000000000:role/connection")

    prepared.resolver.resolve = AsyncMock(side_effect=resolve)
    await releases.reserve_native_release(
        prepared.session,
        context=prepared.context,
        state=prepared.state,
        workflow="deploy.yml",
        environment="dev",
        dispatch_revision="b" * 40,
        resolver=prepared.resolver,
        manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
    )
    assert prepared.session.locked
    prepared.lease.assert_awaited_once()


async def test_native_release_refuses_flow_paused_during_target_read(reservation):
    prepared = reservation

    async def resolve(*args, **kwargs):
        prepared.session.flow.state = "paused"
        return SimpleNamespace(physical=prepared.physical, role_arn="arn:aws:iam::000000000000:role/connection")

    prepared.session.flow = await prepared.session.get(OrchestrationFlow, "flow")
    prepared.resolver.resolve = AsyncMock(side_effect=resolve)
    with pytest.raises(CycleBlockedError, match="operations_shared_release_owner_changed"):
        await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="dev",
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
    prepared.lease.assert_not_awaited()


@pytest.fixture
def initial_reservation(reservation, monkeypatch):
    prepared = reservation
    run_id = releases.attempt_run_id(prepared.state.node.id, 1)
    prepared.state.node.attempts = 1
    prepared.state.node.state = "running"
    prepared.state.row.pending_action_key = None
    prepared.state.row.cycle = 1
    prepared.state.row.status = "runnable"
    prepared.state.row.handoff_receipt_ref = None
    prepared.state.row.accepted_plan_version = 1
    prepared.state.report.run_id = run_id
    prepared.state.report.repo = "owner/repo"
    prepared.state.report.provider_repository_id = 789
    prepared.state.binding.provider_repository_id = 789
    prepared.state.execution["invocation_id"] = {"S": run_id}
    prepared.state.execution["definition_revision"] = {"S": prepared.entry.workflow.definition_revision}
    prepared.state.execution["default_branch"] = {"S": "main"}
    original_scalar = prepared.session.scalar

    async def scalar(statement):
        if statement.column_descriptions[0]["entity"] is OrchestrationAcceptedPlan:
            return await original_scalar(statement)
        return prepared.session.action_result

    prepared.session.action_result = None
    prepared.session.scalar = AsyncMock(side_effect=scalar)
    claim = SimpleNamespace(
        org_id="org",
        state="held",
        generation=3,
        active_run_id=run_id,
        claim_event_id=run_id,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    original_get = prepared.session.get

    async def get(model, identifier, **kwargs):
        return claim if model is OrchestrationWorkClaim else await original_get(model, identifier, **kwargs)

    prepared.session.get = get
    prepared.claim = claim
    prepared.session.add = lambda action: setattr(prepared.session, "added", action)
    prepared.session.flush = AsyncMock()
    monkeypatch.setattr(releases, "assignment_matches", AsyncMock(return_value=True))
    return prepared


async def test_initial_release_prepares_unique_intent_after_exclusive_lease(initial_reservation):
    prepared = initial_reservation
    await releases.reserve_native_release(
        prepared.session,
        context=prepared.context,
        state=prepared.state,
        workflow="deploy.yml",
        environment="dev",
        initial=True,
        dispatch_revision="b" * 40,
        resolver=prepared.resolver,
        manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
    )
    action = prepared.session.added
    assert action.kind == "operations_native_release_dispatch"
    assert action.status == "prepared"
    assert action.detail["dispatch_started"] is True
    assert action.detail["correlation"] == prepared.state.report.run_id
    assert action.detail["dispatch_revision"] == "b" * 40
    assert action.detail["provider_repository_id"] == 789
    assert action.operation_key == f"native-release:node-1:1:{sha256(b'deploy.yml').hexdigest()}"
    assert action.detail["shared_release"]["target"]["resource_id"] == prepared.physical.resource_id
    prepared.lease.assert_awaited_once()
    assert prepared.session.locked
    prepared.session.flush.assert_awaited_once()
    prepared.session.action_result = action
    with pytest.raises(CycleBlockedError, match="operations_shared_release_intent_changed"):
        await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="dev",
            initial=True,
            dispatch_revision="b" * 40,
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
    prepared.lease.assert_awaited_once()


@pytest.mark.parametrize("initial", [True, False])
async def test_initial_release_accepts_selected_descendant_of_manifest_baselines(initial_reservation, initial):
    prepared = initial_reservation
    prepared.entry.artifact_revision = "c" * 40
    prepared.entry.workflow.definition_revision = "d" * 40
    recorded = prepared.state.report.dispatch_metadata["orchestration"]["release_binding"]
    recorded.update(manifest_artifact_revision="c" * 40, manifest_definition_revision="d" * 40)
    if not initial:
        prepared.state.row.pending_action_key = "dispatch"
        prepared.session.action_result = prepared.action
        original_scalar = prepared.session.scalar

        async def scalar(statement):
            if any(str(value).startswith("native-release:") for value in statement.compile().params.values()):
                return None
            return await original_scalar(statement)

        prepared.session.scalar = scalar
    await releases.reserve_native_release(
        prepared.session,
        context=prepared.context,
        state=prepared.state,
        workflow="deploy.yml",
        environment="dev",
        initial=initial,
        dispatch_revision="e" * 40,
        resolver=prepared.resolver,
        manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
    )
    assert prepared.session.added.detail["source_revision"] == "a" * 40
    assert prepared.session.added.detail["definition_revision"] == "b" * 40
    assert prepared.lease.await_args.kwargs["release_ref"] == "a" * 40


@pytest.mark.parametrize(
    "change",
    ["run", "plan", "handoff", "pending", "blocked", "definition", "dispatch", "repository", "claim", "expired", "displaced", "lease", "source"],
)
async def test_initial_release_refuses_stale_or_contended_intent(initial_reservation, change):
    prepared = initial_reservation
    if change == "run":
        prepared.state.execution["invocation_id"] = {"S": "wrong-run"}
    if change == "plan":
        prepared.state.row.accepted_plan_version = 2
    if change == "handoff":
        prepared.state.row.handoff_receipt_ref = "controller:unexpected"
    if change == "pending":
        prepared.state.row.pending_action_key = "another-action"
    if change == "blocked":
        prepared.state.row.status = "blocked"
    if change == "definition":
        prepared.state.execution["definition_revision"] = {"S": "f" * 40}
    if change == "repository":
        prepared.state.report.provider_repository_id = 790
    if change == "claim":
        releases.assignment_matches.return_value = False
    if change == "expired":
        prepared.claim.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if change == "displaced":
        prepared.claim.generation += 1
    if change == "lease":
        prepared.lease.return_value = SimpleNamespace(applied=False)
    if change == "source":
        prepared.state.execution["source_revision"] = {"S": "f" * 40}
    with pytest.raises(CycleBlockedError):
        await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="dev",
            initial=True,
            dispatch_revision="bad" if change == "dispatch" else "b" * 40,
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )


@pytest.mark.parametrize("change", ["source", "definition", "branch", "ledger", "baseline", "target", "workflow", "lease"])
async def test_descendant_reservation_retains_exact_admission_boundaries(initial_reservation, change):
    prepared = initial_reservation
    prepared.entry.artifact_revision = "c" * 40
    prepared.entry.workflow.definition_revision = "d" * 40
    prepared.state.report.dispatch_metadata["orchestration"]["release_binding"].update(
        manifest_artifact_revision="c" * 40, manifest_definition_revision="d" * 40
    )
    if change in {"source", "definition"}:
        prepared.state.execution[f"{change}_revision"] = {"S": "f" * 40}
    elif change == "branch":
        prepared.state.execution["default_branch"] = {"S": "unapproved"}
    elif change == "ledger":
        prepared.pins.return_value = None
    elif change == "baseline":
        prepared.entry.artifact_revision = "f" * 40
    elif change == "target":
        prepared.entry.resource_id = "other/namespace"
    elif change == "workflow":
        prepared.entry.workflow.path = ".github/workflows/unsupported.yml"
    elif change == "lease":
        prepared.lease.return_value.applied = False
    with pytest.raises(CycleBlockedError):
        await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="dev",
            initial=True,
            dispatch_revision="e" * 40,
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
    assert not hasattr(prepared.session, "added")
    assert not hasattr(prepared.session, "added")
    if change != "lease":
        prepared.lease.assert_not_awaited()


async def test_initial_release_reserves_engine_admitted_external_wait(initial_reservation):
    # The engine persists the admitted wait before the first native dispatch, so
    # the initial reservation has to accept it exactly as it accepts a runnable row.
    prepared = initial_reservation
    prepared.state.row.status = "awaiting_external"
    prepared.state.row.phase = "admitted"
    await releases.reserve_native_release(
        prepared.session,
        context=prepared.context,
        state=prepared.state,
        workflow="deploy.yml",
        environment="dev",
        initial=True,
        dispatch_revision="b" * 40,
        resolver=prepared.resolver,
        manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
    )
    action = prepared.session.added
    assert action.kind == "operations_native_release_dispatch"
    assert action.status == "prepared"
    assert action.detail["dispatch_started"] is True
    assert action.detail["correlation"] == prepared.state.report.run_id
    assert action.operation_key == f"native-release:node-1:1:{sha256(b'deploy.yml').hexdigest()}"
    prepared.lease.assert_awaited_once()


async def test_initial_release_refuses_external_wait_outside_admission(initial_reservation):
    # Only the admitted wait is a pre-dispatch state; waiting in any later phase
    # means an external effect is already outstanding and must not be reserved.
    prepared = initial_reservation
    prepared.state.row.status = "awaiting_external"
    prepared.state.row.phase = "deployment_pending"
    with pytest.raises(CycleBlockedError, match="operations_shared_release_intent_changed"):
        await releases.reserve_native_release(
            prepared.session,
            context=prepared.context,
            state=prepared.state,
            workflow="deploy.yml",
            environment="dev",
            initial=True,
            dispatch_revision="b" * 40,
            resolver=prepared.resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
    assert not hasattr(prepared.session, "added")
    prepared.lease.assert_not_awaited()
