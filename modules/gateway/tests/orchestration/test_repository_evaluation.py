"""Repository observers consume real merge actions without inventing worker runs."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.dispatch import graph_address
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationEdge, OrchestrationNode
from src.orchestration.repository_evaluation import observe_repository_evaluation
from src.orchestration.repository_evaluation_contract import harness_digest
from tests.orchestration import test_merge_controller as protocol
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


@pytest.mark.parametrize("fault", [None, "wrong-connection", "wrong-target", "missing-target"])
def test_deployed_target_contract_binds_the_complete_source_set(fault):
    from src.orchestration.evaluation_contract import multi_repository_models
    from src.orchestration.repository_evaluation import deployed_target_contract
    from src.orchestration.repository_evaluation_contract import canonical
    from src.orchestration.review_cycle import CycleBlockedError

    target = {
        "provider": "aws",
        "account_id": "123456789012",
        "region": "us-east-1",
        "resource_kind": "namespace",
        "resource_id": "fixture-environment",
    }
    expected = {"runtime_contract": "api-v1", "targets": [target]}
    environment = multi_repository_models().Environment(
        connection_id="test-connection",
        target_contract_id="test-contract",
        target_contract_hash=hashlib.sha256(canonical(expected).encode()).hexdigest(),
        runtime_contract="api-v1",
        deployment_receipt_keys=["receipt-a", "receipt-b"],
    )
    sources = [
        {"address": address, "deployment_targets": [{**target, "evidence": {"source": "registered-aws-role:test-connection"}}]}
        for address in ("a", "b")
    ]
    if fault == "wrong-connection":
        sources[1]["deployment_targets"][0]["evidence"]["source"] = "registered-aws-role:other"
    elif fault == "wrong-target":
        sources[1]["deployment_targets"][0]["resource_id"] = "unrelated"
    elif fault == "missing-target":
        sources[1]["deployment_targets"] = []
    if fault is None:
        deployed_target_contract(sources, environment)
    else:
        with pytest.raises(CycleBlockedError, match="predecessor_target_"):
            deployed_target_contract(sources, environment)


@pytest.mark.parametrize("fault", [None, "unapproved-workload", "different-cluster"])
def test_qualification_api_and_client_share_one_approved_target_contract(fault):
    from src.orchestration.evaluation_contract import multi_repository_models
    from src.orchestration.repository_evaluation import deployed_target_contract
    from src.orchestration.repository_evaluation_contract import canonical
    from src.orchestration.review_cycle import CycleBlockedError

    common = {"provider": "aws", "account_id": "123456789012", "region": "us-east-1"}
    api_target = {**common, "resource_kind": "eks-deployment", "resource_id": "cluster/namespace/sample-api"}
    client_target = {**common, "resource_kind": "eks-namespace", "resource_id": "cluster/namespace"}
    accepted = {"runtime_contract": "api-and-client-v1", "targets": sorted((api_target, client_target), key=canonical)}
    environment = multi_repository_models().Environment(
        connection_id="delivery-connection",
        target_contract_id="qualification-v1",
        target_contract_hash=hashlib.sha256(canonical(accepted).encode()).hexdigest(),
        runtime_contract="api-and-client-v1",
        deployment_receipt_keys=["api-receipt", "client-receipt"],
    )
    sources = [
        {"address": address, "deployment_targets": [{**target, "evidence": {"source": "registered-aws-role:delivery-connection"}}]}
        for address, target in (("api", api_target), ("client", client_target))
    ]
    if fault == "unapproved-workload":
        sources[0]["deployment_targets"][0]["resource_id"] = "cluster/namespace/other-api"
    elif fault == "different-cluster":
        sources[1]["deployment_targets"][0]["resource_id"] = "another/namespace"
    if fault is None:
        deployed_target_contract(sources, environment)
    else:
        with pytest.raises(CycleBlockedError, match="predecessor_target_contract_mismatch"):
            deployed_target_contract(sources, environment)


@pytest.mark.parametrize("unavailable", [None, "fixture/source-a", "fixture/source-b", "fixture/harness"])
async def test_multi_repository_sources_resolve_product_sources_and_harness_independently(monkeypatch, unavailable):
    from src.orchestration import repository_binding, repository_evaluation
    from src.orchestration.models import OrchestrationFlow
    from src.orchestration.review_cycle import CycleBlockedError

    flow = SimpleNamespace(org_id="org-fixture", product_id="product-fixture")
    node = SimpleNamespace(
        org_id="org-fixture",
        flow_id="flow-fixture",
        repository_identity="fixture/harness",
        provider_repository_id=303,
        repository_connection={"installation_id": 903},
        issue_ref="19",
    )
    plan = SimpleNamespace(plan_document={"product_id": "product-fixture"})
    predecessors = [
        SimpleNamespace(address="a", repository_id=101),
        SimpleNamespace(address="b", repository_id=202),
    ]
    spec = SimpleNamespace(
        mode="repository",
        predecessors=predecessors,
        harness=SimpleNamespace(repository="fixture/harness", repository_id=303, installation_id=903),
    )
    parents = {
        "node-a": SimpleNamespace(
            org_id="org-fixture",
            flow_id="flow-fixture",
            issue_ref="17",
            repository_identity="fixture/source-a",
            repository_connection={"installation_id": 901},
        ),
        "node-b": SimpleNamespace(
            org_id="org-fixture",
            flow_id="flow-fixture",
            issue_ref="18",
            repository_identity="fixture/source-b",
            repository_connection={"installation_id": 902},
        ),
    }
    session = SimpleNamespace(get=AsyncMock(side_effect=lambda model, identity: flow if model is OrchestrationFlow else parents[identity]))
    source_rows = [
        {"address": "a", "node_id": "node-a", "repository_id": 101},
        {"address": "b", "node_id": "node-b", "repository_id": 202},
    ]
    monkeypatch.setattr(repository_evaluation, "sources_for", AsyncMock(return_value=source_rows))
    observed = []

    async def resolve(_session, **kwargs):
        observed.append(kwargs)
        if kwargs["name"] == unavailable:
            raise repository_binding.RepositoryBindingError("connection_revoked")
        return SimpleNamespace(repository_id=kwargs["repository_id"], installation_id=kwargs["installation_id"])

    monkeypatch.setattr(repository_binding, "resolve_story_repository", resolve)
    if unavailable is not None:
        expected = {
            "fixture/source-a": "repository_evaluation_source_connection_revoked:a",
            "fixture/source-b": "repository_evaluation_source_connection_revoked:b",
            "fixture/harness": "repository_evaluation_harness_connection_revoked",
        }[unavailable]
        with pytest.raises(CycleBlockedError, match=expected):
            await repository_evaluation.multi_repository_sources(session, node, plan, spec)
        assert observed[-1]["name"] == unavailable
        return
    sources, bindings, harness = await repository_evaluation.multi_repository_sources(session, node, plan, spec)
    assert sources == source_rows
    assert {key: value.repository_id for key, value in bindings.items()} == {"a": 101, "b": 202}
    assert harness.repository_id == 303
    assert [(item["name"], item["issue"], item["installation_id"]) for item in observed] == [
        ("fixture/source-a", 17, 901),
        ("fixture/source-b", 18, 902),
        ("fixture/harness", 19, 903),
    ]
    assert all(item["operation"] == "evaluate" and item["tenant_id"] == "org-fixture" for item in observed)


@pytest.fixture
async def repository_evaluation(shared, monkeypatch):  # noqa: F811
    async with protocol.prepared_merge(shared, monkeypatch) as ctx:
        ctx.remote["rules"], ctx.remote["protection"], ctx.remote["reviews"] = [], None, []
        ctx.remote["graphql"]["data"]["repository"]["pullRequest"]["reviewDecision"] = None
        ctx.merge_remote()  # Reviewer-owned delivery is already merged; the engine observes it.
        await protocol.tick(ctx)
        await protocol.tick(ctx)
        async with ctx.factory() as db:
            connection = await db.connection()
            await connection.run_sync(lambda sync: OrchestrationEdge.__table__.create(sync, checkfirst=True))
            parent = await db.get(OrchestrationNode, ctx.node.id)
            assert parent.state == "passed"
            address = graph_address(parent, flow_slug="cycle")
            ctx.spec = dict(
                evidence_schema="repository-evaluation/v1",
                runner=dict(adapter="engine-repository-evidence-v1", repository=ctx.binding.repo, repository_id=123, harness_sha256=harness_digest()),
                predecessors=[dict(address=address, required_checks=[dict(name="Unit tests", app_id=15368)])],
            )
            node = OrchestrationNode(
                org_id=parent.org_id,
                flow_id=parent.flow_id,
                epic_ref="E1",
                wave_ref="W1",
                node_ref="REPOEVAL",
                kind="eval",
                title="Verify repository evidence",
                issue_ref="999",
                state="ready",
                attempts=0,
            )
            db.add(node)
            await db.flush()
            db.add(OrchestrationEdge(org_id=parent.org_id, flow_id=parent.flow_id, from_node_id=parent.id, to_node_id=node.id))
            plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
            document = deepcopy(plan.plan_document)
            policy = {**document["execution_policy"]}
            policy["allowed_actions"] = [*policy["allowed_actions"], "evaluate"]
            policy["evaluation_acceptance"] = {graph_address(node, flow_slug="cycle"): "machine"}
            document["execution_policy"] = policy
            document["nodes"] = [
                *document.get("nodes", []),
                dict(
                    address=graph_address(node, flow_slug="cycle"),
                    kind=node.kind,
                    title=node.title,
                    issue_ref=node.issue_ref,
                    evaluation=ctx.spec,
                ),
            ]
            plan.plan_document = document
            await db.commit()
            ctx.eval_id = node.id
        ctx.provider = SimpleNamespace(token=AsyncMock(return_value="test-scoped-token"), observe=AsyncMock())

        async def observe(binding, spec, sources):
            return dict(
                pull_requests=[
                    dict(
                        **{key: value for key, value in source.items() if key not in {"required_checks", "provider_pr_node_id"}},
                        provider_pr_node_id=source.get("provider_pr_node_id", "PR_2"),
                        merged_at=datetime.now(UTC).isoformat(),
                        checks=[dict(**item, check_run_id=7, head_sha=source["head_sha"]) for item in source["required_checks"]],
                    )
                    for source in sources
                ],
                workflows=[],
                mandatory_passed=True,
            )

        ctx.provider.observe.side_effect = observe
        monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=42))
        ctx.claim_spy = AsyncMock(side_effect=AssertionError("evidence reads must not claim an issue"))
        monkeypatch.setattr("src.orchestration.work_admission.admit", ctx.claim_spy)
        yield ctx


async def run(ctx):
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        assert await observe_repository_evaluation(db, node, provider=ctx.provider)
        await db.commit()
        decisions = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id)))
        return node.state, node.attempts, decisions


@pytest.mark.parametrize("case", ["complete", "missing-source", "stale-source", "deployed-merge-only", "deployed-no-receipt"])
async def test_multi_repository_source_lineage_reads_real_merge_ledger(repository_evaluation, monkeypatch, case):
    from src.orchestration import repository_binding
    from src.orchestration import repository_evaluation as evaluation_module
    from src.orchestration.evaluation_contract import multi_repository_models
    from src.orchestration.models import OrchestrationFlow, OrchestrationPullRequestBinding

    ctx = repository_evaluation
    product_id = "00000000-0000-4000-8000-000000000001"
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        flow = await db.get(OrchestrationFlow, ctx.node.flow_id)
        parent = await db.get(OrchestrationNode, ctx.node.id)
        evaluation = await db.get(OrchestrationNode, ctx.eval_id)
        flow.product_id = product_id
        document = deepcopy(plan.plan_document)
        document["product_id"] = product_id
        plan.plan_document = document
        parent.repository_identity = ctx.binding.repo
        parent.provider_repository_id = ctx.binding.provider_repository_id
        parent.repository_connection = {"provider": "github", "installation_id": ctx.binding.installation_id}
        evaluation.repository_identity = "fixture/harness"
        evaluation.provider_repository_id = 303
        evaluation.repository_connection = {"provider": "github", "installation_id": 903}
        await db.flush()
        specification = multi_repository_models().EvaluationSpecification.model_validate(
            {
                "evidence_schema": "multi-repository-evaluation/v1",
                "mode": "repository",
                "acceptance_mode": "machine",
                "harness": {
                    "repository": "fixture/harness",
                    "repository_id": 303,
                    "installation_id": 903,
                    "source_revision": "a" * 40,
                    "harness_digest": "b" * 64,
                    "adapter": "fixture-adapter",
                    "operation": "evaluate",
                },
                "predecessors": [
                    {
                        "address": ctx.spec["predecessors"][0]["address"],
                        "repository_id": ctx.binding.provider_repository_id,
                        "evidence_kind": "merge",
                        "required_checks": [{"name": "Unit tests", "app_id": 15368}],
                    }
                ],
                "criteria": [{"criterion_id": "repository-checks"}],
            }
        )
        observed = []

        async def resolve(_session, **kwargs):
            observed.append(kwargs)
            return SimpleNamespace(repository_id=kwargs["repository_id"], installation_id=kwargs["installation_id"])

        monkeypatch.setattr(repository_binding, "resolve_story_repository", resolve)
        if case in {"deployed-merge-only", "deployed-no-receipt"}:
            deployed = specification.model_copy(
                update={
                    "mode": "deployed",
                    "environment": multi_repository_models().Environment(
                        connection_id="target-connection",
                        target_contract_id="target-v1",
                        target_contract_hash="d" * 64,
                        runtime_contract="api-v1",
                        deployment_receipt_keys=["deployment-a"],
                    ),
                    "predecessors": [
                        specification.predecessors[0].model_copy(update={"evidence_kind": "merge" if case == "deployed-merge-only" else "deployment"})
                    ],
                }
            )
            from src.orchestration.review_cycle import CycleBlockedError

            reason = "predecessor_deployment_required" if case == "deployed-merge-only" else "predecessor_deployment_receipt_missing"
            with pytest.raises(CycleBlockedError, match=reason):
                await evaluation_module.multi_repository_sources(db, evaluation, plan, deployed)
            assert observed == []
            return
        if case == "missing-source":
            missing_address = "cycle/E1/W1/missing-story"
            missing = multi_repository_models().DeclaredInput(address=missing_address, repository_id=202, evidence_kind="merge")
            incomplete = specification.model_copy(update={"predecessors": [*specification.predecessors, missing]})
            from src.orchestration.review_cycle import CycleBlockedError

            with pytest.raises(CycleBlockedError, match=f"repository_evaluation_source_missing:{missing_address}"):
                await evaluation_module.multi_repository_sources(db, evaluation, plan, incomplete)
            assert observed == []
            return
        if case == "stale-source":
            binding = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
            binding.head_sha = "0" * 40
            binding.revision += 1
            await db.flush()
            from src.orchestration.review_cycle import CycleBlockedError

            with pytest.raises(CycleBlockedError, match=f"repository_evaluation_source_stale:{specification.predecessors[0].address}"):
                await evaluation_module.multi_repository_sources(db, evaluation, plan, specification)
            assert evaluation.state == "ready" and observed == []
            return
        sources, bindings, harness = await evaluation_module.multi_repository_sources(db, evaluation, plan, specification)
        assert len(sources) == 1
        assert sources[0]["provider_pr_node_id"] == ctx.binding.provider_pr_node_id
        assert sources[0]["merge_sha"] and sources[0]["head_sha"] == ctx.binding.head_sha
        assert bindings[specification.predecessors[0].address].repository_id == ctx.binding.provider_repository_id
        assert harness.repository_id == 303
        assert [(item["name"], item["issue"]) for item in observed] == [
            (ctx.binding.repo, int(parent.issue_ref)),
            ("fixture/harness", 999),
        ]
        from src.orchestration.review_cycle import CycleBlockedError

        with pytest.raises(CycleBlockedError, match="source_flow_changed"):
            await evaluation_module.multi_repository_sources(
                db,
                evaluation,
                SimpleNamespace(plan_document={"product_id": "different-product"}),
                specification,
            )
        assert len(observed) == 2


@pytest.mark.parametrize("upgraded", [False, True])
async def test_verified_repository_evidence_passes_through_engine_without_worker_or_claim(repository_evaluation, monkeypatch, upgraded):
    ctx = repository_evaluation
    accepted_digest = ctx.spec["runner"]["harness_sha256"]
    installed_digest = "f" * 64 if upgraded else accepted_digest
    monkeypatch.setattr("src.orchestration.repository_evaluation.harness_digest", lambda: installed_digest)
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        accepted_plan = deepcopy(plan.plan_document)
    state, attempts, decisions = await run(ctx)
    assert state == "passed" and attempts == 1
    assert len(decisions) == 1 and decisions[0].actor_id == "system:repository-evaluation"
    import json

    receipt = json.loads(decisions[0].reason)["receipt"]
    assert receipt["accepted_plan_version"] == 1 and receipt["mandatory_passed"] is True
    assert receipt["live_attestation"] is False
    assert receipt["harness_sha256"] == accepted_digest
    assert receipt["collector_harness_sha256"] == installed_digest
    source = receipt["pull_requests"][0]
    assert source["head_sha"] == ctx.head and source["merge_sha"] == "c" * 40
    assert source["review_ref"] and source["merge_operation_key"] and source["execution_id"]
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        assert plan.plan_document == accepted_plan
    ctx.claim_spy.assert_not_awaited()


async def test_native_eval_becomes_ready_from_merged_code_without_deployment(repository_evaluation):
    from src.orchestration.tick import run_tick

    ctx = repository_evaluation
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        node.state = "pending"
        await db.commit()
        report = await run_tick(db)
        assert report.success and report.errors == 0 and ctx.eval_id not in report.blocked
        assert node.state == "ready"
    ctx.claim_spy.assert_not_awaited()


@pytest.mark.parametrize("case", ["no_authority", "changed_harness", "missing_parent", "changed_source", "failed_criterion"])
@pytest.mark.parametrize("upgraded", [False, True])
async def test_repository_refusals_never_pass_or_create_worker(repository_evaluation, monkeypatch, case, upgraded):
    ctx = repository_evaluation
    if upgraded:
        monkeypatch.setattr("src.orchestration.repository_evaluation.harness_digest", lambda: "f" * 64)
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        if case == "no_authority":
            document["execution_policy"] = {**document["execution_policy"], "allowed_actions": ["develop", "review", "repair", "merge"]}
        elif case == "changed_harness":
            document["nodes"][-1]["evaluation"]["runner"]["harness_sha256"] = "0" * 64
            document["nodes"][-1]["evaluation"]["runner"]["harness_policy"] = "exact"
        elif case == "missing_parent":
            node = await db.get(OrchestrationNode, ctx.node.id)
            node.state = "running"
        plan.plan_document = document
        await db.commit()
    if case == "failed_criterion":
        ctx.provider.observe.side_effect = None
        ctx.provider.observe.return_value = dict(pull_requests=[], workflows=[], mandatory_passed=False)
    elif case == "changed_source":
        original = ctx.provider.observe.side_effect

        async def changed(binding, spec, sources):
            from src.orchestration.models import OrchestrationPullRequestBinding

            async with ctx.factory() as db:
                row = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
                row.revision += 1
                await db.commit()
            return await original(binding, spec, sources)

        ctx.provider.observe.side_effect = changed
    state, attempts, decisions = await run(ctx)
    assert state == "ready" and attempts == 0 and decisions
    ctx.claim_spy.assert_not_awaited()
    if case in {"no_authority", "changed_harness", "missing_parent"}:
        ctx.provider.observe.assert_not_awaited()


async def test_external_pr_evidence_never_takes_the_foreign_issue_lane(repository_evaluation):
    ctx = repository_evaluation
    from sqlalchemy import delete

    async with ctx.factory() as db:
        await db.execute(delete(OrchestrationEdge).where(OrchestrationEdge.to_node_id == ctx.eval_id))
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        spec = {
            **document["nodes"][-1]["evaluation"],
            "predecessors": [],
            "external_pull_requests": [
                dict(
                    criterion_id="external-issue",
                    issue_number=5329,
                    pr_number=123,
                    head_sha="a" * 40,
                    merge_sha="b" * 40,
                    required_checks=[dict(name="Unit tests", app_id=15368)],
                )
            ],
        }
        document["nodes"][-1]["evaluation"] = spec
        plan.plan_document = document
        await db.commit()
    state, _, _ = await run(ctx)
    assert state == "passed"
    ctx.claim_spy.assert_not_awaited()
    assert ctx.provider.observe.call_args.args[2][0]["issue_number"] == 5329


async def test_slow_evaluations_share_one_small_budget_after_worker_admission(repository_evaluation, monkeypatch):
    import asyncio

    from src.orchestration import dispatch_pass
    from src.orchestration import repository_evaluation as evaluation_module

    ctx = repository_evaluation
    timeouts = []

    def observed_timeout(seconds):
        timeout = asyncio.timeout(seconds)
        timeouts.append((seconds, timeout))
        return timeout

    monkeypatch.setattr(evaluation_module, "asyncio", SimpleNamespace(timeout=observed_timeout))
    monkeypatch.setattr("src.orchestration.repository_evaluation.OBSERVATION_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr("src.orchestration.repository_evaluation.RepositoryEvidenceProvider", lambda: ctx.provider)
    monkeypatch.setattr("src.orchestration.report_dispatch.recover_pending_reports", AsyncMock())

    async def slow_token(binding):
        await asyncio.Future()

    ctx.provider.token.side_effect = slow_token
    admitted = []
    original = dispatch_pass._dispatch_one

    async def dispatch(session, node, *, config, report):
        if node.kind == "story":
            # Routing/admission has its own integration suite. This test proves
            # the scheduler reaches an eligible worker before any slow observer.
            assert ctx.provider.token.await_count == 0
            admitted.append(node.id)
            node.state = "running"
            report.record(node.org_id, "dispatched")
        else:
            await original(session, node, config=config, report=report)

    monkeypatch.setattr(dispatch_pass, "_dispatch_one", dispatch)
    async with ctx.factory() as db:
        first = await db.get(OrchestrationNode, ctx.eval_id)
        second = OrchestrationNode(
            org_id=first.org_id,
            flow_id=first.flow_id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="REPOEVAL2",
            kind="eval",
            title="Second evidence read",
            issue_ref="998",
            state="ready",
            attempts=0,
        )
        story = OrchestrationNode(
            org_id=first.org_id,
            flow_id=first.flow_id,
            epic_ref="E1",
            wave_ref="W2",
            node_ref="NEWSTORY",
            kind="story",
            title="Eligible coding work",
            issue_ref="997",
            state="ready",
            attempts=0,
        )
        db.add_all([second, story])
        await db.flush()
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        document["nodes"].append(
            dict(
                address=graph_address(second, flow_slug="cycle"),
                kind="eval",
                title=second.title,
                issue_ref=second.issue_ref,
                evaluation=ctx.spec,
            )
        )
        document["execution_policy"]["evaluation_acceptance"][graph_address(second, flow_slug="cycle")] = "machine"
        plan.plan_document = document
        await db.commit()
        report = await dispatch_pass.run_dispatch_pass(db, dispatch_pass.DispatchPassConfig(queue_url="queue", repo=ctx.binding.repo))
        assert len(timeouts) == 1
        seconds, timeout = timeouts[0]
        assert 0 <= seconds <= 0.02
        assert timeout.expired()
        assert admitted == [story.id] and report.dispatched == 1 and report.errors == 0
        assert ctx.provider.token.await_count == 1
        ctx.provider.observe.assert_not_awaited()
        await db.commit()
        decisions = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.actor_id == "system:repository-evaluation")))
        assert len(decisions) == 1 and "repository_evaluation_time_budget" in decisions[0].reason
        retry_order = await dispatch_pass._fetch_ready_nodes(db, limit=10)
        assert retry_order[0].id != decisions[0].node_id
