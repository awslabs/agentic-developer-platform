"""Two unrelated products use the same engine contract and evidence path."""

import hashlib
import io
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src.orchestration.delivery_contract import DeliveryContract, WorkflowDeliveryEvidence, verify_delivery_evidence
from src.orchestration.deployment_release_provider import ReleaseProvider
from src.orchestration.deployment_runtime_contract import DeploymentReceipt
from src.orchestration.deployment_workflow_provider import WorkflowContext
from src.orchestration.models import OrchestrationAction, OrchestrationExecution
from src.orchestration.proposal import OperationalReleaseBinding
from src.orchestration.review_cycle import CycleBlockedError
from tests.orchestration.test_operations_evidence import ORG
from tests.orchestration.test_report_dispatch import engine, prepare, reporting, session, session_factory  # noqa: F401

PRODUCTS = [
    ("example/payments", "payments-api", "ship-payments.yml", "api-smoke"),
    ("example/inventory", "stock-worker", "publish-stock.yml", "cli-roundtrip"),
]


def config(check="api-smoke"):
    return DeliveryContract(
        schema_version=2,
        connection_id="fictional-connection",
        resource_kind="service",
        resource_id="fictional-test/service",
        baseline_revision="a" * 40,
        definition_revision="b" * 40,
        workflow_inputs={"environment": "test", "suite": check},
        required_checks=[check],
        cleanup_required=True,
    )


def release_binding(repo, component, workflow, delivery):
    return OperationalReleaseBinding(
        repository=repo,
        component=component,
        workflows=[workflow],
        target="test",
        default_branch="main",
        predecessors=[{"address": "flow/epic/wave/build", "receipt_kind": "verified_merge"}],
        build_prerequisites=["flow/epic/wave/build"],
        delivery_contract=delivery,
    )


def context_for(component, workflow, delivery, correlation="execution-1"):
    return WorkflowContext(
        schema_version=1,
        repository_id=123,
        run_id=42,
        run_attempt=1,
        workflow_path=f".github/workflows/{workflow}",
        workflow_revision="b" * 40,
        source_revision="a" * 40,
        account_id="123456789012",
        region="us-east-1",
        resource_kind=delivery.resource_kind,
        resource_id=delivery.resource_id,
        inputs=delivery.inputs,
        correlation=correlation,
    )


def evidence_for(component, delivery, context):
    return WorkflowDeliveryEvidence(
        **context.model_dump(exclude={"schema_version", "inputs"}),
        schema_version=2,
        contract_digest=delivery.digest,
        actual_revision=context.source_revision,
        component=component,
        checks=[{"id": check, "outcome": "passed", "evidence_ref": f"results/{check}.json"} for check in delivery.required_checks],
        cleanup="passed",
        produced_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("repo,component,workflow,check", PRODUCTS)
@pytest.mark.parametrize("context_reference", [None, "s3://evidence/context?versionId=verified-context-version"])
async def test_provider_artifact_is_authenticated_and_generic(repo, component, workflow, check, context_reference):
    delivery = config(check)
    context = context_for(component, workflow, delivery)
    payload = evidence_for(component, delivery, context)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("delivery.json", payload.model_dump_json())
    content = buffer.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    provider = ReleaseProvider()
    provider.pages = AsyncMock(return_value=[{"id": 88, "name": "delivery-evidence-1", "size_in_bytes": len(content), "digest": "sha256:" + digest}])
    provider.request = AsyncMock(return_value=SimpleNamespace(content=content))
    provider.s3_evidence = AsyncMock(side_effect=AssertionError("Generic release evidence must come from GitHub"))
    run = SimpleNamespace(context=context, run_id=42, run_attempt=1, artifact_ref=context_reference)
    artifact, actual_hash, reference = await provider.release(SimpleNamespace(repo=repo), run, component, delivery_contract=delivery)
    verified = verify_delivery_evidence(artifact, contract=delivery, context=context, component=component, digest=actual_hash, reference=reference)
    assert verified.component == component and verified.checks[0].id == check
    provider.pages.assert_awaited_once()
    provider.pages.return_value[0]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(CycleBlockedError, match="digest_mismatch"):
        await provider.release(SimpleNamespace(repo=repo), run, component, delivery_contract=delivery)
    provider.pages.return_value = []
    with pytest.raises(CycleBlockedError, match="artifact_missing_or_ambiguous"):
        await provider.release(SimpleNamespace(repo=repo), run, component, delivery_contract=delivery)
    provider.s3_evidence.assert_not_awaited()


@pytest.mark.parametrize("repo,component,workflow,check", PRODUCTS)
async def test_opted_in_signed_delivery_is_generic_and_never_falls_back(repo, component, workflow, check):
    delivery = config(check)
    context = context_for(component, workflow, delivery)
    payload = evidence_for(component, delivery, context)
    raw = payload.model_dump_json().encode()
    digest = hashlib.sha256(raw).hexdigest()
    reference = "s3://evidence/delivery?versionId=verified-delivery-version"
    provider = ReleaseProvider()
    provider.s3_evidence = AsyncMock(return_value=(raw, digest, reference))
    provider.pages = AsyncMock(side_effect=AssertionError("Do not substitute GitHub evidence"))
    run = SimpleNamespace(context=context, run_id=42, run_attempt=1, delivery_evidence_transport="s3-oidc-v1")
    binding = SimpleNamespace(repo=repo, provider_repository_id=123)
    artifact, actual_hash, actual_ref = await provider.release(binding, run, component, delivery_contract=delivery)
    verified = verify_delivery_evidence(artifact, contract=delivery, context=context, component=component, digest=actual_hash, reference=actual_ref)
    assert verified.component == component and actual_ref == reference and actual_hash == digest
    expected = provider.s3_evidence.await_args.kwargs
    assert expected == dict(
        account=context.account_id,
        environment="test",
        region=context.region,
        repository=repo,
        repository_id=123,
        run_id=42,
        attempt=1,
        kind="delivery",
        name=delivery.evidence_artifact,
        workflow_path=context.workflow_path,
        workflow_revision=context.workflow_revision,
    )
    provider.s3_evidence.return_value = None
    with pytest.raises(CycleBlockedError, match="artifact_missing_or_ambiguous"):
        await provider.release(binding, run, component, delivery_contract=delivery)
    provider.s3_evidence.return_value = (payload.model_copy(update={"run_attempt": 2}).model_dump_json(), digest, reference)
    with pytest.raises(CycleBlockedError, match="identity_mismatch"):
        await provider.release(binding, run, component, delivery_contract=delivery)
    provider.pages.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("repository_id", 999, "identity_mismatch"),
        ("run_id", 43, "identity_mismatch"),
        ("run_attempt", 2, "identity_mismatch"),
        ("source_revision", "c" * 40, "identity_mismatch"),
        ("actual_revision", "c" * 40, "identity_mismatch"),
        ("component", "other-product", "identity_mismatch"),
        ("correlation", "another-execution", "identity_mismatch"),
        ("contract_digest", "0" * 64, "identity_mismatch"),
        ("resource_id", "another-target", "identity_mismatch"),
        ("cleanup", "failed", "cleanup_failed"),
        ("cleanup", "not_required", "cleanup_failed"),
        ("checks", [{"id": "unrelated", "outcome": "passed", "evidence_ref": "result.json"}], "required_checks_missing"),
        ("checks", [{"id": "api-smoke", "outcome": "skipped", "evidence_ref": "result.json"}], "checks_failed"),
    ],
)
def test_scope_and_required_results_cannot_be_substituted(field, value, reason):
    delivery = config()
    context = context_for("payments-api", "ship.yml", delivery)
    data = evidence_for("payments-api", delivery, context).model_dump()
    data[field] = value
    with pytest.raises(CycleBlockedError, match=reason):
        verify_delivery_evidence(
            WorkflowDeliveryEvidence.model_validate(data),
            contract=delivery,
            context=context,
            component="payments-api",
            digest="f" * 64,
            reference="artifact/88",
        )


def test_legacy_serialization_and_duplicate_check_rejection():
    old = release_binding("example/payments", "payments-api", "ship.yml", None).model_dump(mode="json")
    assert "delivery_contract" not in old and "product_cases" not in old and "proof_phase" not in old
    with pytest.raises(ValidationError, match="unique"):
        DeliveryContract.model_validate({**config().model_dump(), "required_checks": ["same", "same"]})
    with pytest.raises(ValidationError, match="transport"):
        DeliveryContract.model_validate({**config().model_dump(), "workflow_inputs": {"adp_correlation": "spoof"}})


@pytest.mark.parametrize("branch", ["release/stable", "trunk.v2", "main"])
def test_product_release_branch_can_use_git_ref_names(branch):
    data = release_binding("example/payments", "payments-api", "ship.yml", config()).model_dump()
    assert OperationalReleaseBinding.model_validate({**data, "default_branch": branch}).default_branch == branch


@pytest.mark.parametrize("branch", ["../main", "main.lock", "release//stable", "main@{1}", "main\n", "-main"])
def test_product_release_branch_rejects_unsafe_refs(branch):
    data = release_binding("example/payments", "payments-api", "ship.yml", config()).model_dump()
    with pytest.raises(ValidationError, match="invalid release branch"):
        OperationalReleaseBinding.model_validate({**data, "default_branch": branch})


def test_full_configured_input_set_fits_workflow_context():
    from src.agentauth.actions_routes import DispatchRequest

    delivery = DeliveryContract.model_validate({**config().model_dump(), "workflow_inputs": {f"input_{i}": str(i) for i in range(32)}})
    request = DispatchRequest(workflow="ship.yml", inputs=delivery.workflow_inputs)
    context = context_for("payments-api", "ship.yml", delivery)
    context = WorkflowContext.model_validate(
        {
            **context.model_dump(),
            "inputs": {**context.inputs, "adp_correlation": "execution-1", "adp_source_revision": "a" * 40, "adp_definition_revision": "b" * 40},
        }
    )
    assert len(request.inputs) == 32 and len(context.inputs) == 36


def test_only_product_configured_checks_gate_delivery():
    delivery = config()
    context = context_for("payments-api", "ship.yml", delivery)
    evidence = evidence_for("payments-api", delivery, context)
    evidence = WorkflowDeliveryEvidence.model_validate(
        {
            **evidence.model_dump(),
            "checks": [*evidence.checks, {"id": "optional-probe", "outcome": "skipped", "evidence_ref": "optional.json"}],
        }
    )
    verified = verify_delivery_evidence(
        evidence, contract=delivery, context=context, component="payments-api", digest="f" * 64, reference="artifact/88"
    )
    assert [check.id for check in verified.checks] == delivery.required_checks


@pytest.mark.parametrize("later_attempt", [False, True])
@pytest.mark.parametrize(
    "changed",
    [None, "target", "claim", "running", "success", "ambiguous", "incomplete", "foreign-org", "foreign-node", "correlation", "head", "repository"],
)
async def test_failed_workflow_reconciliation_releases_only_its_target(session, changed, later_attempt):  # noqa: F811
    from src.orchestration.deployment_manifest import PhysicalTarget, TargetEvidence
    from src.orchestration.environment_leases import LeaseHolder, LeaseOutcomeKind, acquire_lease
    from src.orchestration.generic_delivery_recovery import reconcile_failed_dispatches
    from src.orchestration.models import OrchestrationEnvironmentLease

    node = await prepare(session)
    node.state, node.attempts = "running", 5 if later_attempt else 1
    row = OrchestrationExecution(
        org_id=ORG,
        flow_id=node.flow_id,
        node_id=node.id,
        cycle=1,
        phase="delivering",
        status="concluded" if later_attempt else "active",
        accepted_plan_version=1,
        claim_id="claim",
        claim_generation=5,
    )
    session.add(row)
    await session.flush()
    physical = PhysicalTarget(
        "aws",
        "123456789012",
        "us-east-1",
        "service",
        "test/payments",
        TargetEvidence(source="registered-aws-role:connection", verified_at=datetime.now(UTC).isoformat()),
    )
    await acquire_lease(session, target=physical, holder=LeaseHolder(ORG, f"delivery:{row.id}", 5), manifest_entry_id="test")
    dispatch = OrchestrationAction(
        org_id=ORG,
        execution_id=row.id,
        kind="delivery_workflow_dispatch",
        operation_key="dispatch",
        status="unknown",
        attempt=1,
        detail={
            "correlation": "original-attempt",
            "workflow": ".github/workflows/ship.yml",
            "source_revision": "a" * 40,
            "dispatch_revision": "b" * 40,
            "contract_digest": config().digest,
            "target": asdict(physical),
            "run_id": None,
            "run_attempt": None,
            "repo": "example/payments",
            "provider_repository_id": 123,
            "branch": "main",
            "claim_id": "other" if changed == "claim" else "claim",
            "claim_generation": 5,
        },
    )
    session.add(dispatch)
    await session.flush()
    raw = {
        "id": 42,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "failure",
        "event": "workflow_dispatch",
        "display_title": "ADP deployment original-attempt",
        "path": ".github/workflows/ship.yml",
        "head_sha": "b" * 40,
        "head_branch": "main",
        "repository": {"id": 123},
    }
    changes = {
        "running": {"status": "in_progress"},
        "success": {"conclusion": "success"},
        "correlation": {"display_title": "ADP deployment later-attempt"},
        "head": {"head_sha": "c" * 40},
        "repository": {"repository": {"id": 456}},
    }
    raw.update(changes.get(changed, {}))
    runs = [raw, raw] if changed == "ambiguous" else [raw]
    listing = {"workflow_runs": runs, "total_count": 31 if changed == "incomplete" else len(runs)}
    provider = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(json=lambda: listing)))
    kwargs = dict(
        binding=SimpleNamespace(org_id="foreign" if changed == "foreign-org" else ORG, repo="example/payments", provider_repository_id=123),
        node_id="foreign" if changed == "foreign-node" else node.id,
        provider=provider,
        token="test-token",
        target_key="other-target" if changed == "target" else physical.canonical_key,
    )
    assert await reconcile_failed_dispatches(session, **kwargs) == (1 if changed is None else 0)
    await session.flush()
    lease = await session.scalar(select(OrchestrationEnvironmentLease))
    assert lease.state == ("free" if changed is None else "held")
    assert node.state == "running"
    assert row.status == ("concluded" if later_attempt else "active")
    assert await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "deployment_verification")) is None
    if changed is None:
        assert dispatch.status == "succeeded"
        assert dispatch.detail["terminal_run"] == {"run_id": 42, "run_attempt": 1, "conclusion": "failure"}
        successor = await acquire_lease(session, target=physical, holder=LeaseHolder(ORG, "successor", 9), manifest_entry_id="next")
        assert successor.kind is LeaseOutcomeKind.APPLIED
        assert await reconcile_failed_dispatches(session, **kwargs) == 0
        assert lease.state == "held" and lease.owner_action_id == "successor"


@pytest.mark.parametrize("environment_input", [True, False])
@pytest.mark.parametrize("repo,component,workflow,check", PRODUCTS)
@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("run_revision", ["definition", "source", "descendant"])
async def test_generic_runtime_produces_completion_receipt(
    session,  # noqa: F811
    session_factory,  # noqa: F811
    monkeypatch,
    repo,
    component,
    workflow,
    check,
    environment_input,
    retry,
    run_revision,
):
    from src.orchestration import operations_runtime_evidence as runtime
    from src.orchestration.deployment_manifest import PhysicalTarget, TargetEvidence
    from src.orchestration.deployment_workflow_provider import WorkflowDefinition, WorkflowProvider
    from src.orchestration.dispatch_pass import attempt_run_id
    from src.orchestration.generic_delivery_dispatch import reserve_dispatch, settle_dispatch
    from src.orchestration.models import OrchestrationFlow
    from src.orchestration.operations_evidence import observed_operations_evidence
    from src.orchestration.operations_workflow_evidence import record_correlated_workflow
    from src.orchestration.run_reports import prepare_run_report, record_terminal
    from src.orchestration.validation_diagnostics import current_validation_checks, record_runtime_validation

    node = await prepare(session)
    node.state, node.attempts = "running", 1
    row = OrchestrationExecution(
        org_id=ORG,
        flow_id=node.flow_id,
        node_id=node.id,
        cycle=1,
        phase="delivering",
        status="active",
        accepted_plan_version=1,
        claim_id="accepted-claim",
        claim_generation=1,
    )
    session.add(row)
    await session.flush()
    delivery = config(check)
    if not environment_input:
        delivery = delivery.model_copy(
            update={
                "workflow_inputs": {
                    "channel": "preview",
                    "stage": "custom",
                    "release_group": "custom",
                    "product_cases": "custom",
                    "proof_phase": "custom",
                }
            }
        )
    release = release_binding(repo, component, workflow, delivery)
    invocation = attempt_run_id(node.id, 1)
    context = context_for(component, workflow, delivery, invocation)
    pins = {"source_revision": "a" * 40, "definition_revision": "b" * 40, "default_branch": "main"}
    workflow_revision = {"definition": pins["definition_revision"], "source": pins["source_revision"], "descendant": "c" * 40}[run_revision]
    context = context.model_copy(update={"workflow_revision": workflow_revision})
    release_record = {
        "target": release.target,
        "connection_id": delivery.connection_id,
        "resource_kind": delivery.resource_kind,
        "resource_id": delivery.resource_id,
        "manifest_artifact_revision": delivery.baseline_revision,
        "manifest_definition_revision": delivery.definition_revision,
        "repository": repo,
        "component": component,
        "workflow": context.workflow_path,
        "delivery_contract_digest": delivery.digest,
        "accepted_plan_version": 1,
        "pins": pins,
        "receipts": [{"address": "flow/epic/wave/build", "action_id": "merge-1"}],
    }
    session.add(
        OrchestrationAction(
            org_id=ORG,
            execution_id=row.id,
            kind="operations_release_binding",
            operation_key="operations-release:1",
            status="succeeded",
            attempt=1,
            detail=release_record,
        )
    )
    report = await prepare_run_report(
        session,
        {
            "message_id": invocation,
            "tenant_id": ORG,
            "persona": "operations",
            "source_ref": {"repo": repo, "installation_id": 42, "provider_repository_id": 123},
            "orchestration": {
                "node_id": node.id,
                "flow_id": node.flow_id,
                "attempt": 1,
                "executor_role": "operate",
                "deployment_revisions": pins,
                "release_binding": release_record,
            },
            "handoff_expect": {"accepted_plan_version": 1, "execution_id": row.id, "claim_id": row.claim_id, "claim_generation": 1},
        },
    )
    record_terminal(report, "complete")
    report.operations_report_receipt = {
        "recorded": True,
        "run_id": invocation,
        "attempt": 1,
        "node_id": node.id,
        "receipt_id": f"operations-report:{invocation}",
        "report": "Verified release",
        "sha256": hashlib.sha256(b"Verified release").hexdigest(),
    }
    contract = SimpleNamespace(
        plan_version=1,
        node=SimpleNamespace(
            repository_identity=repo,
            executor=SimpleNamespace(role="operate", persona="operations"),
            operational_release_binding=release,
            evidence_expectations=[f"runtime_verified:{component}"],
            required_inputs=[SimpleNamespace(source=f"deployment_workflow:{workflow}")],
        ),
    )
    for module in ["operations_runtime_evidence", "operations_workflow_evidence", "operations_evidence"]:
        monkeypatch.setattr(f"src.orchestration.{module}.accepted_node_contract", AsyncMock(return_value=contract))
    binding = SimpleNamespace(repo=repo, org_id=ORG, provider_repository_id=123, installation_id=42)
    issued = SimpleNamespace(node_id=node.id, plan_version=1, workflow_allowlist={workflow}, delivery_contract=delivery)
    protected = {"invocation_id": {"S": invocation}, "persona": {"S": "operations"}, **{key: {"S": value} for key, value in pins.items()}}
    raw_run = {
        "id": 42,
        "run_attempt": 1,
        "path": context.workflow_path,
        "event": "workflow_dispatch",
        "head_sha": workflow_revision,
        "repository": {"id": binding.provider_repository_id},
        "display_title": "ADP deployment " + invocation,
        "status": "completed",
        "conclusion": "success",
    }
    context_id = await record_correlated_workflow(
        session, execution=protected, binding=binding, issued=issued, raw_run=raw_run, context=context, artifact_id=99, digest="f" * 64
    )
    assert context_id is not None
    physical = PhysicalTarget(
        "aws",
        context.account_id,
        context.region,
        context.resource_kind,
        context.resource_id,
        TargetEvidence(source=f"registered-aws-role:{delivery.connection_id}", verified_at=datetime.now(UTC).isoformat()),
    )
    await session.commit()
    policy = SimpleNamespace(permits=lambda _: True, expires_at=datetime.now(UTC) + timedelta(hours=1))
    monkeypatch.setattr(
        runtime.ReviewCycleServices, "authority_context", AsyncMock(return_value=(None, None, SimpleNamespace(policy=policy), "user", None, object()))
    )
    monkeypatch.setattr(runtime, "require_current_claim", AsyncMock())
    monkeypatch.setattr(runtime, "load_in_force_policy", AsyncMock(return_value=SimpleNamespace(refusal=None, plan_version=1, policy=policy)))
    artifact = evidence_for(component, delivery, context)
    provider = SimpleNamespace(
        # A non-dispatch definition read starts at the artifact source, exactly
        # as WorkflowProvider.definition(for_dispatch=False) does in production.
        definition=AsyncMock(
            return_value=WorkflowDefinition(
                approved_revision=pins["definition_revision"],
                source_revision=pins["source_revision"],
                blob_sha="d" * 40,
                defaults={},
                dispatchable=True,
                dispatch_ref="",
                dispatch_revision=pins["source_revision"],
            )
        ),
        contains=AsyncMock(),
        workflow_content_matches=AsyncMock(return_value=True),
        observe=AsyncMock(
            return_value=(
                SimpleNamespace(
                    status="completed",
                    conclusion="success",
                    run_attempt=1,
                    artifact_id=99,
                    artifact_ref=None,
                    artifact_digest="f" * 64,
                    context=context,
                ),
                None,
            )
        ),
        release=AsyncMock(return_value=(artifact, "f" * 64, "github/actions/runs/42/artifacts/88")),
    )
    # Exercise the real run/context identity checks. A permissive observe mock
    # hid valid approved-definition runs being compared to the artifact source.
    observer = WorkflowProvider()
    observer.request = AsyncMock(side_effect=lambda *_args, **_kwargs: SimpleNamespace(json=lambda: raw_run))
    observer.context = AsyncMock(side_effect=lambda *_args: (provider.observe.return_value[0].context, 99, "f" * 64))
    provider.observe.side_effect = observer.observe
    targets = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(physical=physical)))
    monkeypatch.setattr("src.orchestration.generic_delivery_dispatch.DeploymentTargetResolver", lambda: targets)
    flow = await session.get(OrchestrationFlow, node.flow_id)
    arguments = dict(
        factory=session_factory,
        node=node,
        flow=flow,
        contract=contract,
        entry=delivery.manifest(release).entries[0],
        binding=binding,
        execution=protected,
    )
    reservation = await reserve_dispatch(session, **arguments, dispatch_revision=workflow_revision)
    await session.commit()
    # A crash after preparing a dispatch cannot publish another deployment.
    with pytest.raises(CycleBlockedError, match="delivery_dispatch_reconciliation_required"):
        await reserve_dispatch(session, **arguments)
    await session.commit()
    await settle_dispatch(session_factory, reservation, org_id=ORG, succeeded=True)
    duplicate = await reserve_dispatch(session, **arguments)
    assert duplicate["already_dispatched"] is True and duplicate["id"] == reservation["id"]
    await session.commit()
    if retry:
        from src.orchestration.generic_delivery_recovery import reconcile_failed_dispatches

        with pytest.raises(CycleBlockedError, match="delivery_dispatch_reconciliation_required"):
            await reserve_dispatch(session, **arguments, run_id=42, run_attempt=1)
        failure = {
            **raw_run,
            "status": "completed",
            "conclusion": "failure",
            "head_sha": workflow_revision,
            "head_branch": "main",
            "display_title": "ADP deployment " + invocation,
            "repository": {"id": 123},
        }
        failure_provider = SimpleNamespace(
            request=AsyncMock(return_value=SimpleNamespace(json=lambda: {"total_count": 1, "workflow_runs": [failure]}))
        )
        assert await reconcile_failed_dispatches(session, binding=binding, node_id=node.id, provider=failure_provider, token="test-token") == 1
        with pytest.raises(CycleBlockedError, match="delivery_dispatch_reconciliation_required"):
            await reserve_dispatch(session, **arguments, run_id=43, run_attempt=1)
        reservation = await reserve_dispatch(session, **arguments, run_id=42, run_attempt=1)
        assert reservation["already_dispatched"] is False
        await session.commit()
        await settle_dispatch(session_factory, reservation, org_id=ORG, succeeded=True)
        context = context.model_copy(update={"run_attempt": 2})
        raw_run = {**raw_run, "run_attempt": 2}
        context_id = await record_correlated_workflow(
            session, execution=protected, binding=binding, issued=issued, raw_run=raw_run, context=context, artifact_id=99, digest="f" * 64
        )
        assert context_id is not None
        await session.commit()
        artifact = evidence_for(component, delivery, context)
        provider.observe.return_value[0].context = context
        provider.observe.return_value[0].run_attempt = 2
    kwargs = dict(
        binding=binding,
        issued=issued,
        execution=protected,
        raw_run=raw_run,
        context_action_id=context_id,
        reauthorize=AsyncMock(),
        provider=provider,
        targets=targets,
        manifest_loader=lambda: pytest.fail("generic delivery must not load the engine product manifest"),
    )
    if run_revision != "definition":
        provider.workflow_content_matches.return_value = False
        with pytest.raises(CycleBlockedError, match="operations_runtime_definition_changed"):
            await runtime.verify_operations_runtime(session_factory, **kwargs)
        provider.workflow_content_matches.return_value = True
        provider.contains.side_effect = CycleBlockedError("deployment_revision_not_descendant")
        with pytest.raises(CycleBlockedError, match="deployment_revision_not_descendant"):
            await runtime.verify_operations_runtime(session_factory, **kwargs)
        provider.contains.side_effect = None
        assert provider.release.await_count == 0
    await record_runtime_validation(
        session_factory, binding=binding, issued=issued, code="delivery_dispatch_reconciliation_required", stage="dispatch", invocation_id=invocation
    )
    # A foreign/stale invocation cannot overwrite this attempt's diagnosis.
    await record_runtime_validation(
        session_factory, binding=binding, issued=issued, code="wrong_execution", stage="dispatch", invocation_id="foreign-invocation"
    )
    # Failure must be visible and must not free the target or fabricate a receipt.
    provider.release.return_value = (artifact.model_copy(update={"cleanup": "failed"}), "f" * 64, "artifact/88")
    with pytest.raises(CycleBlockedError, match="delivery_cleanup_failed"):
        await runtime.verify_operations_runtime(session_factory, **kwargs)
    await session.rollback()
    checks = await current_validation_checks(session, org_id=ORG, executions={node.id: row})
    assert {item.code for item in checks[row.id]} == {"delivery_dispatch_reconciliation_required", "delivery_cleanup_failed"}
    await session.commit()
    provider.release.return_value = (artifact, "f" * 64, "github/actions/runs/42/artifacts/88")
    receipt_id = await runtime.verify_operations_runtime(session_factory, **kwargs)
    assert receipt_id is not None
    from src.orchestration.environment_leases import LeaseHolder, LeaseOutcomeKind, acquire_lease

    # A later delivery can own this target while a client retries an old response.
    await session.rollback()
    successor = await acquire_lease(
        session, target=physical, holder=LeaseHolder(ORG, "another-delivery", 1), manifest_entry_id="another-target", release_ref="c" * 40
    )
    assert successor.kind is LeaseOutcomeKind.APPLIED
    await session.commit()
    duplicate = await reserve_dispatch(session, **arguments)
    assert duplicate["already_dispatched"] is True
    await session.commit()
    # Re-observation after response loss reuses the same receipt and lease disposition.
    assert await runtime.verify_operations_runtime(session_factory, **kwargs) == receipt_id
    await session.rollback()
    action = await session.get(OrchestrationAction, receipt_id)
    receipt = DeploymentReceipt.model_validate(action.detail["deployment_receipt"])
    assert receipt.schema_version == 2 and receipt.components[0].component == component
    assert receipt.components[0].checks[0].id == check
    assert provider.observe.await_args.kwargs["inputs"] == delivery.inputs
    assert provider.observe.await_args.kwargs["definition"].dispatch_revision == workflow_revision
    if run_revision != "definition":
        provider.contains.assert_awaited_with(binding, pins["source_revision"], workflow_revision)
        assert provider.workflow_content_matches.await_args.args[2:] == (pins["definition_revision"], workflow_revision)
    else:
        provider.contains.assert_not_awaited()
        provider.workflow_content_matches.assert_not_awaited()
    checks = await current_validation_checks(session, org_id=ORG, executions={node.id: row})
    assert all(item.status == "passed" for item in checks[row.id])
    result, reason = await observed_operations_evidence(
        session, node, {"run_id": invocation, "attempt": 1, "conclusion_authority": "evidence", "repo": repo}, expected=(contract, (component,))
    )
    assert result is not None and not reason
