"""Real accepted-plan, workflow and Product rows fence human observations."""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.evidence import AssessmentResult
from l5_services.models.run import Run
from sqlalchemy import delete

from src.admin.membership_revocation import revoke_membership
from src.orchestration import l5_observation_scope as scope_module
from src.orchestration import l5_observation_workflow as workflow_module
from src.orchestration import operations_release_job as job_module
from src.orchestration.compile import plan_hash
from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.l5_observation_contract import PrepareObservation
from src.orchestration.l5_observation_scope import ObservationError, product_scope, scope
from src.orchestration.models import OrchestrationAction, OrchestrationExecution, OrchestrationWorkClaim
from src.orchestration.proposal import LoopProposal
from src.orchestration.run_reports import OrchestrationRunReport
from src.products.models import Product, ProductProjectBinding, Project
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import TeamMembership, User
from tests.orchestration.test_policy_admission import (
    APPROVER,
    ORG_A,
    REPO,
    TEAM_A,
    _accept_policy,
    _make_approval,
    _make_flow,
    _make_member,
    _make_node,
    _make_org,
    _policy,
)


@pytest.fixture
async def product_case(db_session, test_engine):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    db = db_session
    await _make_org(db)
    await _make_member(db)
    product = Product(
        tenant_id=ORG_A,
        id="product",
        name="Selected",
        purpose="Test",
        intended_users=[],
        owner_user_id=APPROVER,
        team_id=TEAM_A,
        starting_point="new",
        created_by=APPROVER,
    )
    project = Project(tenant_id=ORG_A, id="project", name="Selected", owner_user_id=APPROVER)
    db.add_all([product, project])
    await db.flush()
    relationship = ProductProjectBinding(tenant_id=ORG_A, product_id=product.id, project_id=project.id, provenance={})
    config = Configuration(
        tenant_id=ORG_A,
        configuration_id="configuration",
        product_id=product.id,
        project_id=project.id,
        service_id="devops",
        relationship_revision="1",
        owner_id=APPROVER,
        current_revision=1,
        handoff_state="linked",
    )
    db.add_all([relationship, config])
    await db.flush()
    version = ConfigurationVersion(
        tenant_id=ORG_A,
        configuration_id=config.configuration_id,
        revision=1,
        settings={"enabled": True},
        inherited_fields=[],
        gaps=[],
        state="ready",
        hash="a" * 64,
    )
    binding = Binding(
        tenant_id=ORG_A,
        binding_id="binding",
        configuration_id=config.configuration_id,
        product_id=product.id,
        project_id=project.id,
        service_id="devops",
        scope={},
        revision=1,
        generation=1,
        state="ready",
        ownership="adp_created",
        connection_ref={},
        connection_key="connection",
        native_resource_ref={"record_id": "fixture-space"},
        native_key="native",
        provider_identity={},
        readiness=[],
        gaps=[],
    )
    db.add_all([version, binding])
    await db.commit()
    request = PrepareObservation(
        request_key="observation_fixture_0001",
        node_id="node",
        expected_plan_version=1,
        expected_plan_hash="a" * 64,
        expected_attempt=1,
        workflow_action_id="action",
        product_id=product.id,
        project_id=project.id,
        service_id="devops",
        binding_id=binding.binding_id,
        check_id="ui-cli-mcp-parity",
        observer_id=APPROVER,
        subject="configuration",
        reason="Observe the selected installed configuration",
    )
    return SimpleNamespace(
        db=db,
        product=product,
        project=project,
        relationship=relationship,
        config=config,
        version=version,
        binding=binding,
        request=request,
        now=datetime.now(UTC),
    )


async def test_product_snapshot_uses_current_server_configuration(product_case):
    c = product_case
    selected = await product_scope(c.db, tenant_id=ORG_A, owner_id=APPROVER, request=c.request)
    assert selected["configuration_revision"] == 1 and selected["configuration_sha256"] == c.version.hash
    assert selected["binding_generation"] == 1 and selected["binding_id"] == c.binding.binding_id


@pytest.mark.parametrize("change", ["team_revoked", "owner_changed", "relationship_changed", "binding_changed", "foreign_tenant"])
async def test_product_scope_rejects_revoked_or_changed_selection(product_case, change):
    c = product_case
    if change == "team_revoked":
        await c.db.execute(delete(TeamMembership).where(TeamMembership.user_id == APPROVER))
    elif change == "owner_changed":
        c.product.owner_user_id = "someone_else"
    elif change == "relationship_changed":
        c.relationship.relationship_revision += 1
    elif change == "binding_changed":
        c.binding.project_id = "foreign-project"
    await c.db.commit()
    with pytest.raises(ObservationError, match="not_found"):
        await product_scope(c.db, tenant_id="other" if change == "foreign_tenant" else ORG_A, owner_id=APPROVER, request=c.request)


async def test_negative_observer_is_a_current_member_without_target_access(product_case):
    c = product_case
    c.db.add(User(id="negative-observer", org_id=ORG_A, team_id=TEAM_A, email="negative@example.test", user_kind="human"))
    await c.db.flush()
    c.db.add(TenantMembership(user_id="negative-observer", tenant_id=ORG_A, role="member"))
    await c.db.commit()
    request = c.request.model_copy(update={"observer_id": "negative-observer", "subject": "native_denial"})
    assert (await product_scope(c.db, tenant_id=ORG_A, owner_id=APPROVER, request=request))[
        "native_hostname"
    ] == "fixture-space.aidevops.global.app.aws"
    with pytest.raises(ObservationError, match="not_found"):
        await product_scope(c.db, tenant_id=ORG_A, owner_id=APPROVER, request=request.model_copy(update={"subject": "native_ui"}))
    await revoke_membership(c.db, org_id=ORG_A, user_id="negative-observer")
    await c.db.commit()
    with pytest.raises(ObservationError, match="observer_revoked"):
        await product_scope(c.db, tenant_id=ORG_A, owner_id=APPROVER, request=request)


async def test_result_must_belong_to_selected_binding_generation(product_case):
    c = product_case
    run = Run(
        tenant_id=ORG_A,
        run_id="run",
        product_id=c.product.id,
        project_id=c.project.id,
        service_id="devops",
        configuration_id=c.config.configuration_id,
        configuration_revision=1,
        binding_id=c.binding.binding_id,
        binding_generation=1,
        trigger_id="manual",
        input={},
        deadline_at=c.now + timedelta(hours=1),
    )
    c.db.add(run)
    await c.db.flush()
    c.db.add(AssessmentResult(tenant_id=ORG_A, result_id="result", run_id=run.run_id, version=1, payload={"state": "completed"}))
    await c.db.commit()
    request = c.request.model_copy(update={"subject": "result", "result_id": "result"})
    assert (await product_scope(c.db, tenant_id=ORG_A, owner_id=APPROVER, request=request))["result"]["run_id"] == "run"
    c.binding.generation += 1
    await c.db.commit()
    with pytest.raises(ObservationError, match="not_found"):
        await product_scope(c.db, tenant_id=ORG_A, owner_id=APPROVER, request=request)


@pytest.fixture
async def scope_case(product_case, monkeypatch):
    c = product_case
    flow = await _make_flow(c.db)
    node = await _make_node(c.db, flow, attempts=1, state="running")
    plan = await _accept_policy(c.db, flow, _policy(expires_at=c.now + timedelta(hours=1)))
    approval = await _make_approval(c.db, flow)
    plan.accepted_by_decision_id = approval.id
    address = f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"
    predecessor = f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/build"
    plan.plan_document = {
        **plan.plan_document,
        "title": flow.title,
        "org_id": ORG_A,
        "spec_revision": "test-v1",
        "edges": [],
        "nodes": [
            {
                "address": address,
                "kind": "story",
                "title": node.title,
                "repository_identity": REPO,
                "executor": {"kind": "agent", "role": "operate", "persona": "operations"},
                "operational_release_binding": {
                    "predecessors": [{"address": predecessor, "receipt_kind": "verified_merge"}],
                    "build_prerequisites": [predecessor],
                    "repository": REPO,
                    "target": "target",
                    "component": "l5-devops",
                    "workflows": ["l5-services-release.yml"],
                    "default_branch": "main",
                    "delivery_contract": {
                        "schema_version": 2,
                        "connection_id": "connection",
                        "resource_kind": "l5-service",
                        "resource_id": "devops",
                        "definition_revision": "b" * 40,
                        "baseline_revision": "c" * 40,
                        "required_checks": ["ui-cli-mcp-parity"],
                    },
                },
            }
        ],
    }
    plan.plan_hash = plan_hash(LoopProposal.model_validate(plan.plan_document))
    claim = OrchestrationWorkClaim(
        org_id=ORG_A,
        provider_repository_id=123,
        issue_number=4,
        owner_kind="engine_flow",
        owner_ref=flow.id,
        generation=1,
        state="held",
        active_run_id=attempt_run_id(node.id, node.attempts),
        lease_expires_at=c.now + timedelta(hours=1),
    )
    c.db.add(claim)
    await c.db.flush()
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
    c.db.add(execution)
    await c.db.flush()
    correlation = attempt_run_id(node.id, node.attempts)
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
            "handoff_expect": {
                "execution_id": execution.id,
                "accepted_plan_version": 1,
                "claim_id": claim.id,
                "claim_generation": 1,
            }
        },
        expires_at=c.now + timedelta(days=15),
    )
    from src.orchestration.accepted_node import contract_from_plan

    delivery = contract_from_plan(flow, node, plan).node.operational_release_binding.delivery_contract
    action = OrchestrationAction(
        org_id=ORG_A,
        execution_id=execution.id,
        operation_key=f"delivery-dispatch:{'d' * 40}:initial",
        kind="delivery_workflow_dispatch",
        status="succeeded",
        attempt=1,
        detail={
            "correlation": correlation,
            "contract_digest": delivery.digest,
            "workflow": ".github/workflows/l5-services-release.yml",
            "source_revision": "d" * 40,
            "definition_revision": "b" * 40,
            "dispatch_revision": "e" * 40,
            "branch": "main",
            "run_id": None,
            "run_attempt": None,
            "repo": REPO,
            "provider_repository_id": 123,
            "claim_id": claim.id,
            "claim_generation": 1,
            "release_job": {"run_id": "72", "run_attempt": "1"},
        },
    )
    c.db.add_all([report, action])
    await c.db.commit()
    pins = {"source_revision": "d" * 40, "definition_revision": "b" * 40, "default_branch": "main"}
    monkeypatch.setattr(scope_module, "operational_execution_pins", AsyncMock(return_value=pins))
    monkeypatch.setattr(scope_module, "repair_revisions", AsyncMock(side_effect=lambda *args, **kwargs: kwargs["pins"]))
    monkeypatch.setattr(scope_module, "release_identity", Mock(return_value={"l5_wheel_sha256": "f" * 64}))
    # Exercise NativeReleaseJob's real provider verification; fake only transport
    # and tenant App credentials. No successful workflow/context receipt exists.
    c.provider_run = {
        "id": 72,
        "run_attempt": 1,
        "status": "in_progress",
        "conclusion": None,
        "repository": {"id": 123},
        "head_sha": "e" * 40,
        "event": "workflow_dispatch",
        "path": ".github/workflows/l5-services-release.yml",
        "display_title": "ADP deployment " + correlation,
        "actor": {"id": 789},
        "triggering_actor": {"id": 789},
        "created_at": (c.now - timedelta(minutes=2)).isoformat(),
        "run_started_at": (c.now - timedelta(minutes=1)).isoformat(),
    }

    async def provider_request(binding, method, path, **kwargs):
        assert (binding.org_id, binding.repo, binding.installation_id, binding.provider_repository_id) == (ORG_A, REPO, 456, 123)
        assert method == "GET"
        if path == "/app":
            data = {"slug": "adp-test"}
        elif path == "/users/adp-test[bot]":
            data = {"id": 789, "type": "Bot"}
        else:
            assert path == f"/repos/{REPO}/actions/runs/{c.action.detail['release_job']['run_id']}"
            data = c.provider_run
        return Mock(json=lambda: copy.deepcopy(data))

    c.provider = SimpleNamespace(request=AsyncMock(side_effect=provider_request))
    monkeypatch.setattr(job_module, "resolve_tenant_app_credentials", AsyncMock(return_value=(1, "test-key")))
    monkeypatch.setattr(job_module, "_mint_app_jwt", Mock(return_value="test-app-token"))
    monkeypatch.setattr(workflow_module, "NativeReleaseJob", lambda factory: job_module.NativeReleaseJob(factory, provider=c.provider))
    c.flow, c.node, c.plan, c.claim, c.execution, c.action = flow, node, plan, claim, execution, action
    c.report = report
    c.request = c.request.model_copy(update={"node_id": node.id, "expected_plan_hash": plan.plan_hash, "workflow_action_id": action.id})
    return c


async def test_actual_plan_workflow_and_product_scope_are_bound(scope_case):
    c = scope_case
    node, snapshot, expiry = await scope(c.db, flow_id=c.flow.id, tenant_id=ORG_A, owner_id=APPROVER, request=c.request, now=c.now)
    assert node.id == c.node.id and snapshot["plan_hash"] == c.plan.plan_hash
    assert snapshot["release"]["run_id"] == 72 and snapshot["selection"]["configuration_revision"] == 1
    assert expiry == c.now + timedelta(hours=1)
    assert snapshot["release"]["provenance"] == "oidc-authorized-delivery-dispatch-and-provider-run"
    assert snapshot["release"]["dispatch_action_id"] == c.action.id
    assert c.provider_run["status"] == "in_progress" and c.provider_run["conclusion"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"id": 73},
        {"run_attempt": 2},
        {"actor": {"id": 790}},
        {"triggering_actor": {"id": 790}},
        {"repository": {"id": 124}},
        {"head_sha": "0" * 40},
        {"display_title": "ADP deployment another-run"},
        {"event": "push"},
        {"path": ".github/workflows/another.yml"},
    ],
)
async def test_provider_must_confirm_exact_oidc_dispatch_identity(scope_case, change):
    c = scope_case
    c.provider_run.update(change)
    with pytest.raises(ObservationError, match="workflow_changed"):
        await scope(c.db, flow_id=c.flow.id, tenant_id=ORG_A, owner_id=APPROVER, request=c.request, now=c.now)


@pytest.mark.parametrize("status,conclusion", [("queued", None), ("completed", "success"), ("completed", "failure")])
async def test_only_running_workflow_can_collect_live_observations(scope_case, status, conclusion):
    c = scope_case
    c.provider_run.update(status=status, conclusion=conclusion)
    with pytest.raises(ObservationError, match="workflow_not_running"):
        await scope(c.db, flow_id=c.flow.id, tenant_id=ORG_A, owner_id=APPROVER, request=c.request, now=c.now)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"release_job": None}, "workflow_unverified"),
        ({"release_job": {"run_id": "72", "run_attempt": "2"}}, "workflow_changed"),
        ({"contract_digest": "0" * 64}, "workflow_changed"),
        ({"correlation": "another-run"}, "workflow_changed"),
        ({"claim_generation": 2}, "workflow_changed"),
        ({"terminal_run": {"run_id": 72, "run_attempt": 1}}, "workflow_changed"),
    ],
)
async def test_dispatch_requires_persisted_oidc_pin_and_current_contract(scope_case, change, code):
    c = scope_case
    c.action.detail = {**c.action.detail, **change}
    await c.db.commit()
    with pytest.raises(ObservationError, match=code):
        await scope(c.db, flow_id=c.flow.id, tenant_id=ORG_A, owner_id=APPROVER, request=c.request, now=c.now)
    c.provider.request.assert_not_awaited()


async def test_rerun_requires_next_attempt_of_the_same_authorized_workflow(scope_case):
    c = scope_case
    c.action.operation_key = f"delivery-dispatch:{'d' * 40}:rerun:72:1"
    c.action.detail = {**c.action.detail, "run_id": 72, "run_attempt": 1, "release_job": {"run_id": "72", "run_attempt": "2"}}
    c.provider_run["run_attempt"] = 2
    await c.db.commit()
    _, snapshot, _ = await scope(c.db, flow_id=c.flow.id, tenant_id=ORG_A, owner_id=APPROVER, request=c.request, now=c.now)
    assert snapshot["release"]["run_attempt"] == 2
    c.provider_run["run_attempt"] = 3
    with pytest.raises(ObservationError, match="workflow_changed"):
        await scope(c.db, flow_id=c.flow.id, tenant_id=ORG_A, owner_id=APPROVER, request=c.request, now=c.now)


async def test_dispatch_attempt_is_not_confused_with_restarted_node_cycle(scope_case):
    c = scope_case
    c.node.attempts = c.execution.cycle = c.report.attempt = 2
    correlation = attempt_run_id(c.node.id, 2)
    c.report.run_id = c.claim.active_run_id = correlation
    c.action.detail = {**c.action.detail, "correlation": correlation}
    c.provider_run["display_title"] = "ADP deployment " + correlation
    c.request = c.request.model_copy(update={"expected_attempt": 2})
    await c.db.commit()
    _, snapshot, _ = await scope(c.db, flow_id=c.flow.id, tenant_id=ORG_A, owner_id=APPROVER, request=c.request, now=c.now)
    assert c.action.attempt == 1 and snapshot["release"]["cycle"] == 2


async def test_provider_failure_is_bounded_and_actionable(scope_case):
    c = scope_case
    c.provider.request.side_effect = RuntimeError("sensitive provider context")
    with pytest.raises(ObservationError, match="workflow_provider_unavailable") as error:
        await scope(c.db, flow_id=c.flow.id, tenant_id=ORG_A, owner_id=APPROVER, request=c.request, now=c.now)
    assert error.value.status == 503 and "sensitive" not in str(error.value)


async def test_current_report_assignment_and_claim_lease_bound_challenge(scope_case):
    c = scope_case
    c.claim.lease_expires_at = c.now + timedelta(minutes=5)
    await c.db.commit()
    _, _, expiry = await scope(c.db, flow_id=c.flow.id, tenant_id=ORG_A, owner_id=APPROVER, request=c.request, now=c.now)
    assert expiry == c.now + timedelta(minutes=5)
    c.report.dispatch_metadata = {"handoff_expect": {"execution_id": "another-execution"}}
    await c.db.commit()
    with pytest.raises(ObservationError, match="assignment_changed"):
        await scope(c.db, flow_id=c.flow.id, tenant_id=ORG_A, owner_id=APPROVER, request=c.request, now=c.now)


@pytest.mark.parametrize(
    "change,code",
    [
        ("plan_tamper", "plan_changed"),
        ("check_excluded", "check_not_accepted"),
        ("workflow_tamper", "workflow_changed"),
        ("attempt", "plan_changed"),
        ("execution", "execution_changed"),
        ("claim", "claim_changed"),
        ("claim_released", "claim_changed"),
        ("claim_expired", "claim_changed"),
        ("claim_other_run", "claim_changed"),
        ("flow_paused", "execution_changed"),
        ("flow_completed", "execution_changed"),
        ("source", "source_changed"),
        ("missing_pins", "source_unverified"),
        ("runtime", "runtime_unavailable"),
        ("membership", "owner_revoked"),
    ],
)
async def test_current_server_authority_cannot_be_replaced_by_observer_claims(scope_case, monkeypatch, change, code):
    c = scope_case
    if change in {"plan_tamper", "check_excluded"}:
        document = copy.deepcopy(c.plan.plan_document)
        if change == "plan_tamper":
            document["nodes"][0]["title"] = "Tampered"
        else:
            document["nodes"][0]["operational_release_binding"]["delivery_contract"]["required_checks"] = ["schema-compatible"]
        c.plan.plan_document = document
        if change == "check_excluded":
            c.plan.plan_hash = plan_hash(LoopProposal.model_validate(document))
            c.request = c.request.model_copy(update={"expected_plan_hash": c.plan.plan_hash})
    elif change == "workflow_tamper":
        detail = copy.deepcopy(c.action.detail)
        detail["release_job"]["run_id"] = "73"
        c.action.detail = detail
    elif change == "attempt":
        c.node.attempts += 1
    elif change == "execution":
        c.execution.status = "concluded"
    elif change == "claim":
        c.claim.generation += 1
    elif change == "claim_released":
        c.claim.state = "released"
    elif change == "claim_expired":
        c.claim.lease_expires_at = c.now - timedelta(seconds=1)
    elif change == "claim_other_run":
        c.claim.active_run_id = "another-worker"
    elif change == "flow_paused":
        c.flow.execution_paused = True
    elif change == "flow_completed":
        c.flow.state = "completed"
    elif change == "source":
        monkeypatch.setattr(scope_module, "repair_revisions", AsyncMock(return_value={"source_revision": "1" * 40, "definition_revision": "b" * 40}))
    elif change == "missing_pins":
        monkeypatch.setattr(scope_module, "repair_revisions", AsyncMock(return_value=None))
    elif change == "runtime":
        monkeypatch.setattr(scope_module, "release_identity", Mock(side_effect=RuntimeError("package unreadable")))
    elif change == "membership":
        await revoke_membership(c.db, org_id=ORG_A, user_id=APPROVER)
    await c.db.commit()
    with pytest.raises(ObservationError, match=code):
        await scope(c.db, flow_id=c.flow.id, tenant_id=ORG_A, owner_id=APPROVER, request=c.request, now=c.now)
