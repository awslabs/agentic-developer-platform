"""Actions mutations never trust a caller-selected repository or workflow authority."""

import base64
import os
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi import HTTPException

from src.agentauth import actions_routes


@pytest.mark.parametrize("mutate", [False, True])
async def test_authority_keeps_reads_available_while_published_repair_is_unmerged(monkeypatch, mutate):
    from src.orchestration import operations_repair

    caller = SimpleNamespace(tenant_id="org", invocation_id="run-1")
    grant = SimpleNamespace(tenant_id="org")
    execution = {
        "persona": {"S": "operations"},
        "orchestration_node_attempt": {"N": "3"},
        "installation_id": {"N": "123"},
        "source_revision": {"S": "a" * 40},
        "definition_revision": {"S": "b" * 40},
        "default_branch": {"S": "main"},
    }
    runtime = SimpleNamespace(validate_flow=AsyncMock(), store=SimpleNamespace(_read=lambda *_: execution))
    issued = SimpleNamespace(node_id="node", plan_version=11, repository="owner/repo")
    session = SimpleNamespace(commit=AsyncMock())

    @asynccontextmanager
    async def session_factory():
        yield session

    async def revisions(_session, **kwargs):
        if kwargs["require_merged"]:
            raise operations_repair.RepairMergeRequiredError("operations_repair_merge_required")
        return kwargs["pins"]

    monkeypatch.setattr(actions_routes, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(actions_routes, "_authenticated", AsyncMock(return_value=(None, caller, {}, grant)))
    monkeypatch.setattr(actions_routes, "authorize_worker_credential", AsyncMock(return_value=SimpleNamespace(provider_repository_id=789)))
    for method in ("authorize_operations_grant", "authorize_actions_read_grant"):
        monkeypatch.setattr(actions_routes, method, AsyncMock(return_value=issued))
    monkeypatch.setattr(actions_routes, "current_claim_generation", AsyncMock(return_value=1))
    claim = AsyncMock()
    monkeypatch.setattr(actions_routes, "require_current_claim", claim)
    monkeypatch.setattr(operations_repair, "repair_revisions", revisions)
    if mutate:
        with pytest.raises(actions_routes.OperationRefusedError, match="operations_repair_merge_required"):
            await actions_routes._authority(None, runtime, mutate=True)
        claim.assert_awaited_once()
    else:
        assert (await actions_routes._authority(None, runtime, mutate=False))[2] == execution
        claim.assert_not_awaited()


@pytest.fixture(params=[base64.b64encode, base64.encodebytes], ids=["single-line", "github-wrapped"])
def broker(monkeypatch, request):
    # The broker fixture stubs authority/DB; persistence is covered by the real-ledger tests.
    monkeypatch.setattr(actions_routes, "_record_dispatch_check", AsyncMock())
    issued = SimpleNamespace(
        workflow_allowlist={"deploy.yml"},
        repository="owner/repo",
        permissions={"actions": "write", "metadata": "read"},
    )
    binding = SimpleNamespace(repo="owner/repo", org_id="org", installation_id=123, provider_repository_id=789)
    execution = {
        "source_revision": {"S": "a" * 40},
        "definition_revision": {"S": "b" * 40},
        "invocation_id": {"S": "run-1"},
        "default_branch": {"S": "main"},
    }
    calls = []

    async def authority(*_args, **kwargs):
        calls.append(kwargs)
        return issued, binding, execution

    async def token(**_kwargs):
        return "test-token"

    class Provider:
        requests = []
        workflow = (
            b"on:\n  workflow_dispatch:\n    inputs:\n      adp_correlation: {}\n      adp_source_revision: {}\n      adp_definition_revision: {}\n"
        )
        run = {}
        definition_overrides = {}
        head = "b" * 40
        history = ["b" * 40]
        workflow_at_head = None

        async def request(self, _binding, method, path, **kwargs):
            self.requests.append((method, path, kwargs))
            if "/git/ref/heads/" in path:
                return SimpleNamespace(json=lambda: {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.head}})
            if path == "/graphql":
                return SimpleNamespace(
                    json=lambda: {
                        "data": {
                            "repository": {
                                "object": {"history": {"nodes": [{"oid": oid} for oid in self.history], "pageInfo": {"hasNextPage": False}}}
                            }
                        }
                    }
                )
            if "/contents/" in path:
                content = self.workflow_at_head if self.workflow_at_head is not None and kwargs["params"]["ref"] == self.head else self.workflow
                return SimpleNamespace(
                    json=lambda: {
                        "type": "file",
                        "encoding": "base64",
                        "size": len(content),
                        "content": request.param(content).decode(),
                        **self.definition_overrides,
                    }
                )
            if method == "GET":
                return SimpleNamespace(json=lambda: self.run)
            return SimpleNamespace(status_code=204)

    Provider.issued = issued
    Provider.execution = execution
    monkeypatch.setattr(actions_routes, "_authority", authority)
    monkeypatch.setattr(actions_routes, "installation_token", token)
    monkeypatch.setattr(actions_routes, "WorkflowProvider", Provider)
    return Provider, calls


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("rerun", [False, True])
async def test_unmerged_repair_refuses_dispatch_and_rerun_before_provider_mutation(broker, monkeypatch, late, rerun):
    provider, calls = broker
    provider.run = {
        "path": ".github/workflows/deploy.yml",
        "repository": {"id": 789},
        "event": "push",
        "head_sha": "b" * 40,
        "head_branch": "main",
        "conclusion": "failure",
    }
    original = actions_routes._authority

    async def authority(*args, **kwargs):
        if len(calls) == int(late):
            raise actions_routes.OperationRefusedError("operations_repair_merge_required")
        return await original(*args, **kwargs)

    monkeypatch.setattr(actions_routes, "_authority", authority)
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow=None if rerun else "deploy.yml", run_id=7 if rerun else None, inputs={})
    assert error.value.status_code == 409
    assert error.value.detail == "operations_repair_merge_required"
    assert not any(method == "POST" for method, _, _ in provider.requests)


async def test_grouped_operations_dispatch_refuses_without_admission_before_provider_post(broker, monkeypatch):
    provider, calls = broker
    provider.execution["persona"] = {"S": "operations"}
    admission = AsyncMock(side_effect=actions_routes.OperationRefusedError("shared release admission is not installed"))
    monkeypatch.setattr(actions_routes, "_require_shared_release_admission", admission)

    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})

    assert error.value.status_code == 404
    admission.assert_awaited_once()
    assert len(calls) == 2
    assert not any(method == "POST" for method, _, _ in provider.requests)


async def test_operations_dispatch_uses_controller_session_factory_before_provider_post(broker, monkeypatch):
    provider, calls = broker
    provider.execution["persona"] = {"S": "operations"}
    controller_factory = object()
    admission = AsyncMock(side_effect=actions_routes.OperationRefusedError("shared release admission is not installed"))
    monkeypatch.setattr(actions_routes, "_require_shared_release_admission", admission)

    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={}, factory=controller_factory)

    assert error.value.status_code == 404
    assert admission.await_args.args[0] == provider.issued
    assert admission.await_args.args[2:] == ("deploy.yml", controller_factory, provider.execution)
    assert len(calls) == 2
    assert not any(method == "POST" for method, _, _ in provider.requests)


@pytest.mark.parametrize(
    "wave,selected,refusal",
    [
        ("w03b-foundation-release", "plain", "shared release admission"),
        ("w05b-connections", "grouped", "shared release admission"),
        ("w05b-connections", "plain", None),
        ("w05b-connections", "wrong-workflow", "operations release target changed"),
    ],
)
async def test_shared_release_preflight_uses_accepted_target_not_another_entry(monkeypatch, wave, selected, refusal):
    from src.orchestration import accepted_node, deployment_manifest
    from src.orchestration.models import OrchestrationFlow, OrchestrationNode

    node = SimpleNamespace(id="node", flow_id="flow", org_id="org", wave_ref=wave)
    flow = SimpleNamespace(id="flow", org_id="org", slug="atlassian-adapters-8445-parallel")
    release = SimpleNamespace(target=selected, repository="owner/repo", component="gateway-backend", workflows=["deploy.yml"])
    contract = SimpleNamespace(
        plan_version=1,
        environment_connection_ids={"approved-connection"},
        node=SimpleNamespace(evidence_expectations=["runtime_verified:gateway-backend"], operational_release_binding=release),
    )

    def entry(identifier, *, grouped=False, workflow="deploy.yml"):
        return SimpleNamespace(
            entry_id=identifier,
            status=deployment_manifest.EntryStatus.ENABLED,
            docs_only=False,
            release_group="atlassian-foundation" if grouped else None,
            connection_id="approved-connection",
            workflow=SimpleNamespace(path=f".github/workflows/{workflow}"),
            covers=lambda component: component == "gateway-backend",
        )

    entries = {
        "grouped": entry("grouped", grouped=True),
        "plain": entry("plain"),
        "wrong-workflow": entry("wrong-workflow", workflow="other.yml"),
    }

    class Session:
        async def get(self, model, identifier):
            return {OrchestrationNode: node, OrchestrationFlow: flow}[model]

    @asynccontextmanager
    async def session_factory():
        yield Session()

    monkeypatch.setattr(accepted_node, "accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr(deployment_manifest, "load_packaged_manifest", lambda: SimpleNamespace(entry=entries.get))
    issued = SimpleNamespace(node_id=node.id, plan_version=1)
    binding = SimpleNamespace(org_id="org", repo="owner/repo")
    if refusal:
        with pytest.raises(actions_routes.OperationRefusedError, match=refusal):
            await actions_routes._require_shared_release_admission(issued, binding, "deploy.yml", session_factory, {})
    else:
        assert await actions_routes._require_shared_release_admission(issued, binding, "deploy.yml", session_factory, {}) is None


@pytest.mark.parametrize("change", ["ok", "continuation", "missing_report", "wrong_environment", "rerun", "reservation_refused"])
@pytest.mark.parametrize("component", ["gateway-backend", "tool-adapters"])
async def test_foundation_dispatch_requires_fresh_authorized_reservation(monkeypatch, change, component):
    from src.orchestration import accepted_node, deployment_manifest, operations_shared_release, review_cycle_dispatch
    from src.orchestration.models import OrchestrationExecution, OrchestrationFlow, OrchestrationNode
    from src.orchestration.review_cycle import CycleBlockedError
    from src.orchestration.run_reports import OrchestrationRunReport

    correlation = "orch:12345678-1234-1234-1234-123456789abc"
    node = SimpleNamespace(id="node", org_id="org", flow_id="flow", attempts=1, wave_ref="w03b-foundation-release")
    flow = SimpleNamespace(id="flow", org_id="org", slug="atlassian-adapters-8445-parallel")
    row = SimpleNamespace(id="execution", org_id="org", flow_id="flow", claim_id="claim", claim_generation=3, cycle=1, revision=7)
    report = SimpleNamespace(run_id=correlation, org_id="org", node_id="node", repo="owner/repo", provider_repository_id=123)
    binding = SimpleNamespace(org_id="org", repo="owner/repo", provider_repository_id=123)
    entry = SimpleNamespace(
        entry_id="adapter",
        release_group="atlassian-foundation",
        status=deployment_manifest.EntryStatus.ENABLED,
        docs_only=False,
        connection_id="connection",
        workflow=SimpleNamespace(path=".github/workflows/deploy.yml"),
        covers=lambda candidate: candidate == component,
    )
    contract = SimpleNamespace(
        plan_version=1,
        environment_connection_ids={"connection"},
        node=SimpleNamespace(
            evidence_expectations=[f"runtime_verified:{component}"],
            operational_release_binding=SimpleNamespace(target="adapter", repository="owner/repo", component=component, workflows=["deploy.yml"]),
        ),
    )
    execution = {"invocation_id": {"S": correlation}, "definition_revision": {"S": "b" * 40}}

    class Session:
        commit = AsyncMock()

        async def get(self, model, identifier):
            return {
                OrchestrationNode: node,
                OrchestrationFlow: flow,
                OrchestrationRunReport: None if change == "missing_report" else report,
            }[model]

        async def scalar(self, statement):
            assert statement.column_descriptions[0]["entity"] is OrchestrationExecution
            return row

    session = Session()

    @asynccontextmanager
    async def factory():
        yield session

    reserve = AsyncMock(return_value=SimpleNamespace(applied=True))
    if change == "reservation_refused":
        reserve.side_effect = CycleBlockedError("operations_shared_release_held")
    authority = AsyncMock(return_value=(None, None, "policy", "human", None, "authorization"))
    monkeypatch.setattr(accepted_node, "accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr(deployment_manifest, "load_packaged_manifest", lambda: SimpleNamespace(entry=lambda identifier: entry))
    monkeypatch.setattr(operations_shared_release, "reserve_native_release", reserve)
    monkeypatch.setattr(review_cycle_dispatch, "ReviewCycleServices", lambda factory: SimpleNamespace(authority_context=authority))
    if change not in {"ok", "continuation"}:
        with pytest.raises(actions_routes.OperationRefusedError):
            await actions_routes._require_shared_release_admission(
                SimpleNamespace(node_id=node.id, plan_version=1),
                binding,
                "deploy.yml",
                factory,
                execution,
                environment="prod" if change == "wrong_environment" else "dev",
                initial=change != "rerun",
                dispatch_revision="c" * 40,
            )
        session.commit.assert_not_awaited()
        if change in {"missing_report", "wrong_environment", "rerun"}:
            reserve.assert_not_awaited()
        return
    controller_context = SimpleNamespace(identity=SimpleNamespace(claim_id="claim", claim_generation=3), execution=row)
    admitted_component = await actions_routes._require_shared_release_admission(
        SimpleNamespace(node_id=node.id, plan_version=1),
        binding,
        "deploy.yml",
        factory,
        execution,
        environment="dev",
        initial=change != "continuation",
        dispatch_revision="c" * 40,
        continuation_context=(controller_context, object()) if change == "continuation" else None,
    )
    assert admitted_component == component
    authority.assert_awaited_once()
    assert reserve.await_args.kwargs["initial"] is (change != "continuation")
    if change == "continuation":
        assert reserve.await_args.kwargs["context"] is controller_context
    assert reserve.await_args.kwargs["dispatch_revision"] == "c" * 40
    assert execution["definition_revision"]["S"] == "b" * 40
    assert reserve.await_args.kwargs["context"].identity.claim_generation == 3
    session.commit.assert_awaited_once()


async def test_ungrouped_legacy_operation_does_not_require_one_runtime_component(monkeypatch):
    from src.orchestration import accepted_node
    from src.orchestration.models import OrchestrationFlow, OrchestrationNode

    node = SimpleNamespace(id="node", flow_id="flow", org_id="org", wave_ref="other")
    flow = SimpleNamespace(id="flow", org_id="org", slug="unrelated")
    contract = SimpleNamespace(plan_version=1, node=SimpleNamespace(evidence_expectations=["runtime_verified:first", "runtime_verified:second"]))

    class Session:
        async def get(self, model, identifier):
            return {OrchestrationNode: node, OrchestrationFlow: flow}[model]

    @asynccontextmanager
    async def session_factory():
        yield Session()

    monkeypatch.setattr(accepted_node, "accepted_node_contract", AsyncMock(return_value=contract))
    assert (
        await actions_routes._require_shared_release_admission(
            SimpleNamespace(node_id="node", plan_version=1), SimpleNamespace(org_id="org"), "deploy.yml", session_factory, {}
        )
        is None
    )


@pytest.mark.parametrize(
    "target,other,blocked",
    [
        ("grouped", None, True),
        ("plain", None, False),
        ("plain", "grouped-other-connection", False),
        ("plain", "grouped-other-revision", False),
        ("grouped", "plain", True),
    ],
)
async def test_legacy_operations_dispatch_resolves_accepted_pins_before_grouping(broker, monkeypatch, target, other, blocked):
    from src.orchestration import accepted_node, deployment_manifest
    from src.orchestration.models import OrchestrationFlow, OrchestrationNode

    provider, _ = broker
    provider.execution["persona"] = {"S": "operations"}
    provider.issued.node_id = "legacy-node"
    provider.issued.plan_version = 1
    node = SimpleNamespace(id="legacy-node", org_id="org", flow_id="flow", wave_ref="other")
    flow = SimpleNamespace(id="flow", org_id="org", slug="legacy-maintenance")
    contract = SimpleNamespace(
        plan_version=1,
        environment_connection_ids={"approved"},
        node=SimpleNamespace(evidence_expectations=["runtime_verified:gateway-backend"], operational_release_binding=None),
    )

    def entry(name):
        return SimpleNamespace(
            status=deployment_manifest.EntryStatus.ENABLED,
            docs_only=False,
            connection_id="unapproved" if name == "grouped-other-connection" else "approved",
            artifact_revision="e" * 40 if name == "grouped-other-revision" else "a" * 40,
            workflow=SimpleNamespace(path=".github/workflows/deploy.yml", definition_revision="b" * 40),
            release_group="atlassian-foundation" if name.startswith("grouped") else None,
            covers=lambda component: component == "gateway-backend",
        )

    class Session:
        async def get(self, model, identifier):
            return {OrchestrationNode: node, OrchestrationFlow: flow}[model]

    @asynccontextmanager
    async def session_factory():
        yield Session()

    monkeypatch.setattr(actions_routes, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(accepted_node, "accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr(
        deployment_manifest,
        "load_packaged_manifest",
        lambda: SimpleNamespace(entries=[entry(target), *([entry(other)] if other else [])]),
    )
    if blocked:
        with pytest.raises(HTTPException) as error:
            await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})
        assert error.value.status_code == 404
        assert not any(method == "POST" for method, _, _ in provider.requests)
    else:
        assert (await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={}))["dispatched"] is True
        assert len([1 for method, _, _ in provider.requests if method == "POST"]) == 1


async def test_dispatch_carries_protected_correlation_and_rechecks_authority(broker):
    provider, calls = broker
    result = await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})
    assert result == {"dispatched": True, "workflow": "deploy.yml", "correlation": "run-1"}
    assert len(calls) == 2 and all(call == {"mutate": True} for call in calls)
    method, path, options = provider.requests[-1]
    assert method == "POST" and path.endswith("/actions/workflows/deploy.yml/dispatches")
    assert options["json"]["ref"] == "main"
    assert options["json"]["inputs"] == {
        "adp_correlation": "run-1",
        "adp_source_revision": "a" * 40,
        "adp_definition_revision": "b" * 40,
    }
    assert all("test-token" == request[2]["token"] for request in provider.requests)


async def test_adapter_stage_requires_accepted_native_reservation_before_post(broker, monkeypatch):
    provider, _ = broker
    provider.execution["persona"] = {"S": "operations"}
    provider.issued.workflow_allowlist.add("tools-platform-deploy.yml")
    provider.workflow += b"      stage:\n        default: platform\n      environment:\n        default: dev\n"
    reserve = AsyncMock(side_effect=actions_routes.OperationRefusedError("shared release reservation unavailable"))
    monkeypatch.setattr(actions_routes, "_require_shared_release_admission", reserve)
    with pytest.raises(HTTPException):
        await actions_routes._perform(None, None, workflow="tools-platform-deploy.yml", run_id=None, inputs={})
    assert not any(method == "POST" for method, _, _ in provider.requests)
    reserve.side_effect = None
    reserve.return_value = "tool-adapters"
    await actions_routes._perform(None, None, workflow="tools-platform-deploy.yml", run_id=None, inputs={})
    assert provider.requests[-1][2]["json"]["inputs"]["stage"] == "tool-adapters"
    assert len([method for method, _, _ in provider.requests if method == "POST"]) == 1
    provider.workflow = provider.workflow.replace(b"default: platform", b"default: other")
    with pytest.raises(HTTPException):
        await actions_routes._perform(None, None, workflow="tools-platform-deploy.yml", run_id=None, inputs={})
    assert len([method for method, _, _ in provider.requests if method == "POST"]) == 1


async def test_gateway_foundation_dispatch_requires_reserved_release_and_guarded_workflow(broker, monkeypatch):
    provider, _ = broker
    provider.execution["persona"] = {"S": "operations"}
    provider.issued.workflow_allowlist.add("gateway-deploy.yml")
    provider.workflow += b"      release_group:\n        default: none\n      environment:\n        default: dev\n"
    admission = AsyncMock(side_effect=actions_routes.OperationRefusedError("shared release reservation unavailable"))
    monkeypatch.setattr(actions_routes, "_require_shared_release_admission", admission)
    with pytest.raises(HTTPException):
        await actions_routes._perform(None, None, workflow="gateway-deploy.yml", run_id=None, inputs={})
    assert not any(method == "POST" for method, _, _ in provider.requests)
    admission.side_effect = None
    admission.return_value = "gateway-backend"
    await actions_routes._perform(None, None, workflow="gateway-deploy.yml", run_id=None, inputs={})
    assert provider.requests[-1][2]["json"]["inputs"]["release_group"] == "atlassian-foundation"
    provider.workflow = provider.workflow.replace(b"default: none", b"default: unknown")
    with pytest.raises(HTTPException):
        await actions_routes._perform(None, None, workflow="gateway-deploy.yml", run_id=None, inputs={})
    assert len([method for method, _, _ in provider.requests if method == "POST"]) == 1


async def test_ordinary_gateway_dispatch_does_not_select_foundation(broker, monkeypatch):
    provider, _ = broker
    provider.execution["persona"] = {"S": "operations"}
    provider.issued.workflow_allowlist.add("gateway-deploy.yml")
    provider.workflow += b"      release_group:\n        default: none\n      environment:\n        default: dev\n"
    monkeypatch.setattr(actions_routes, "_require_shared_release_admission", AsyncMock(return_value=None))
    await actions_routes._perform(None, None, workflow="gateway-deploy.yml", run_id=None, inputs={})
    assert "release_group" not in provider.requests[-1][2]["json"]["inputs"]


def test_gateway_foundation_context_requires_explicit_selected_group():
    defaults = {"release_group": "none", "environment": "dev"}
    selected = {"release_group": "atlassian-foundation", "environment": "dev"}
    assert actions_routes._inputs_match_defaults(selected, defaults, foundation_gateway=True)
    assert not actions_routes._inputs_match_defaults(selected, defaults)
    for value in (None, "none", "other"):
        recorded = {"environment": "dev", **({"release_group": value} if value is not None else {})}
        assert not actions_routes._inputs_match_defaults(recorded, defaults, foundation_gateway=True)
    assert not actions_routes._inputs_match_defaults(selected, defaults, foundation_gateway=True, product_cases="other")


async def test_native_retry_requires_trusted_reservation_before_post(broker, monkeypatch):
    from src.orchestration.operations_publication_recovery import ImageAbsence
    from src.orchestration.operations_release_job import NativeReleaseJob

    provider, _ = broker
    provider.execution["persona"] = {"S": "operations"}
    provider.workflow += b"      environment:\n        default: dev\n"
    context, services, factory = object(), object(), object()
    recovery = AsyncMock(return_value=None)
    monkeypatch.setattr(NativeReleaseJob, "recover_terminal_publication", recovery)
    with pytest.raises(HTTPException) as forbidden:
        await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={}, factory=factory, native_retry=(context, services))
    assert forbidden.value.status_code == 404
    recovery.assert_not_awaited()
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(
            None,
            None,
            workflow="deploy.yml",
            run_id=None,
            inputs={},
            authority=lambda: actions_routes._authority(None, None, mutate=True),
            factory=factory,
            native_retry=(context, services),
        )
    assert error.value.status_code == 404
    assert not any(method == "POST" for method, _, _ in provider.requests)
    recovery.return_value = ImageAbsence("000000000000", "us-east-1", "adp-tools-dev-adapters", "a" * 40, "sha256:" + "b" * 64, datetime.now(UTC))
    assert (
        await actions_routes._perform(
            None,
            None,
            workflow="deploy.yml",
            run_id=None,
            inputs={},
            authority=lambda: actions_routes._authority(None, None, mutate=True),
            factory=factory,
            native_retry=(context, services),
        )
    )["dispatched"] is True
    assert recovery.await_args.kwargs == {
        "reserve_retry": True,
        "expected_workflow": "deploy.yml",
        "expected_dispatch_revision": "b" * 40,
    }
    assert len([method for method, _, _ in provider.requests if method == "POST"]) == 1


async def test_controller_continuation_reserves_before_dispatch_and_refuses_untrusted_replay(broker, monkeypatch):
    provider, _ = broker
    provider.execution["persona"] = {"S": "operations"}
    provider.workflow += b"      environment:\n        default: dev\n"
    factory = object()
    context = (object(), object())
    reserve = AsyncMock(side_effect=actions_routes.OperationRefusedError("shared release reservation unavailable"))
    monkeypatch.setattr(actions_routes, "_require_shared_release_admission", reserve)
    with pytest.raises(HTTPException) as forbidden:
        await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={}, factory=factory, continuation_context=context)
    assert forbidden.value.status_code == 404
    reserve.assert_not_awaited()
    with pytest.raises(HTTPException) as refused:
        await actions_routes._perform(
            None,
            None,
            workflow="deploy.yml",
            run_id=None,
            inputs={},
            authority=lambda: actions_routes._authority(None, None, mutate=True),
            factory=factory,
            continuation_context=context,
        )
    assert refused.value.status_code == 404
    assert reserve.await_args.kwargs["initial"] is False
    assert reserve.await_args.kwargs["dispatch_revision"] == "b" * 40
    assert reserve.await_args.kwargs["continuation_context"] is context
    assert not any(method == "POST" for method, _, _ in provider.requests)
    reserve.side_effect = None
    result = await actions_routes._perform(
        None,
        None,
        workflow="deploy.yml",
        run_id=None,
        inputs={},
        authority=lambda: actions_routes._authority(None, None, mutate=True),
        factory=factory,
        continuation_context=context,
    )
    assert result["dispatched"] is True
    assert len([method for method, _, _ in provider.requests if method == "POST"]) == 1


async def test_adapter_absence_retry_keeps_original_stage_selection(broker, monkeypatch):
    from src.orchestration.operations_publication_recovery import ImageAbsence
    from src.orchestration.operations_release_job import NativeReleaseJob

    provider, _ = broker
    provider.execution["persona"] = {"S": "operations"}
    provider.issued.workflow_allowlist.add("tools-platform-deploy.yml")
    provider.workflow += b"      stage:\n        default: platform\n      environment:\n        default: dev\n"
    recovered = ImageAbsence("000000000000", "us-east-1", "adp-tools-dev-adapters", "a" * 40, "sha256:" + "b" * 64, datetime.now(UTC))
    reserve = AsyncMock(return_value=recovered)
    monkeypatch.setattr(NativeReleaseJob, "recover_terminal_publication", reserve)
    context = (object(), object())
    await actions_routes._perform(
        None,
        None,
        workflow="tools-platform-deploy.yml",
        run_id=None,
        inputs={},
        authority=lambda: actions_routes._authority(None, None, mutate=True),
        factory=object(),
        native_retry=context,
    )
    assert provider.requests[-1][2]["json"]["inputs"]["stage"] == "tool-adapters"
    reserve.return_value = ImageAbsence("000000000000", "us-east-1", "another-repository", "a" * 40, "sha256:" + "b" * 64, datetime.now(UTC))
    with pytest.raises(HTTPException):
        await actions_routes._perform(
            None,
            None,
            workflow="tools-platform-deploy.yml",
            run_id=None,
            inputs={},
            authority=lambda: actions_routes._authority(None, None, mutate=True),
            factory=object(),
            native_retry=context,
        )
    assert len([method for method, _, _ in provider.requests if method == "POST"]) == 1


@pytest.mark.parametrize(
    "workflow,selection",
    [
        ("gateway-deploy.yml", "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"),
        ("gateway-frontend-deploy.yml", "live_browser_create,live_browser_conflict_denial,live_browser_evidence"),
    ],
)
async def test_accepted_product_inventory_dispatches_only_through_reviewed_gateway_workflow(broker, workflow, selection):
    provider, calls = broker
    provider.issued.workflow_allowlist = {workflow}
    provider.issued.product_cases = selection
    provider.workflow = (
        b"on:\n  workflow_dispatch:\n    inputs:\n      adp_correlation: {}\n"
        b"      adp_source_revision: {}\n      adp_definition_revision: {}\n"
        b"      environment:\n        default: dev\n      product_cases:\n        default: none\n"
    )
    result = await actions_routes._perform(None, None, workflow=workflow, run_id=None, inputs={})
    assert result["dispatched"] is True
    assert len(calls) == 2
    assert provider.requests[-1][2]["json"]["inputs"] == {
        "adp_correlation": "run-1",
        "adp_source_revision": "a" * 40,
        "adp_definition_revision": "b" * 40,
        "product_cases": provider.issued.product_cases,
    }


@pytest.mark.parametrize(
    "workflow,selection",
    [
        ("gateway-deploy.yml", "live_browser_create,live_browser_conflict_denial,live_browser_evidence"),
        ("gateway-frontend-deploy.yml", "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"),
        ("gateway-frontend-deploy.yml", "live_browser_create"),
    ],
)
async def test_product_selection_cannot_cross_workflows_or_select_partial_inventory(broker, workflow, selection):
    provider, _calls = broker
    provider.workflow = maintained_gateway_workflow(workflow)
    provider.issued.workflow_allowlist = {workflow}
    provider.issued.product_cases = selection
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow=workflow, run_id=None, inputs={})
    assert error.value.status_code == 404
    assert not any(method == "POST" for method, _path, _options in provider.requests)


@pytest.mark.parametrize("environment,selection", [("staging", "none"), ("dev", "api_product_lifecycle"), ("dev", "api_denial_oracle")])
async def test_accepted_product_inventory_refuses_wrong_workflow_defaults(broker, environment, selection):
    provider, _calls = broker
    provider.issued.workflow_allowlist = {"gateway-deploy.yml"}
    provider.issued.product_cases = "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"
    provider.workflow = (
        b"on:\n  workflow_dispatch:\n    inputs:\n      adp_correlation: {}\n"
        b"      adp_source_revision: {}\n      adp_definition_revision: {}\n"
        + f"      environment:\n        default: {environment}\n      product_cases:\n        default: {selection}\n".encode()
    )
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow="gateway-deploy.yml", run_id=None, inputs={})
    assert error.value.status_code == 404
    assert not any(method == "POST" for method, _path, _options in provider.requests)


def test_correlated_product_inventory_requires_exact_selected_input():
    defaults = {"environment": "dev", "product_cases": "none"}
    selected = "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"
    assert actions_routes._inputs_match_defaults({"environment": "dev", "product_cases": selected}, defaults, product_cases=selected)
    for recorded in (
        {"environment": "dev"},
        {"environment": "dev", "product_cases": "none"},
        {"environment": "staging", "product_cases": selected},
        {"environment": "dev", "product_cases": selected, "manual_source_revision": "a" * 40},
    ):
        assert not actions_routes._inputs_match_defaults(recorded, defaults, product_cases=selected)
    assert not actions_routes._inputs_match_defaults({"product_cases": selected}, {"product_cases": selected}, product_cases=selected)


async def test_dispatch_rejects_approval_input_even_when_its_value_is_blank(broker):
    provider, _calls = broker
    provider.workflow += b"      adp_approved_revision:\n        default: ''\n"
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})
    assert error.value.status_code == 403
    assert error.value.detail == "human_approval_input"
    assert not any(method == "POST" for method, _path, _options in provider.requests)


def maintained_gateway_workflow(workflow="gateway-deploy.yml"):
    return (Path(__file__).resolve().parents[4] / ".github/workflows" / workflow).read_bytes()


@pytest.mark.parametrize("workflow", ["gateway-deploy.yml", "gateway-frontend-deploy.yml"])
def test_maintained_gateway_workflows_fit_the_authority_reader_byte_bound(workflow):
    from src.orchestration.deployment_workflow_provider import MAX_WORKFLOW_BYTES

    raw = maintained_gateway_workflow(workflow)
    assert len(raw) <= MAX_WORKFLOW_BYTES, f"{workflow} is {len(raw)} bytes; extract commands into source-bound scripts before growing the workflow"


@pytest.mark.parametrize("workflow", ["gateway-deploy.yml", "gateway-frontend-deploy.yml"])
async def test_later_identical_definition_dispatch_passes_real_guard_and_branch_race_fails(broker, monkeypatch, workflow):
    provider, calls = broker
    provider.workflow = maintained_gateway_workflow(workflow)
    provider.issued.workflow_allowlist = {workflow}
    provider.execution["persona"] = {"S": "operations"}
    provider.head = "c" * 40
    provider.history = [provider.head, "b" * 40]
    provider.execution["invocation_id"]["S"] = "orch:11111111-2222-3333-4444-555555555555"
    admission = AsyncMock()
    monkeypatch.setattr(actions_routes, "_require_shared_release_admission", admission)
    await actions_routes._perform(None, None, workflow=workflow, run_id=None, inputs={})

    payload = provider.requests[-1][2]["json"]
    assert payload["ref"] == "main"
    assert payload["inputs"]["adp_source_revision"] == provider.execution["source_revision"]["S"] == "a" * 40
    assert payload["inputs"]["adp_definition_revision"] == provider.head
    assert provider.execution["definition_revision"]["S"] == "b" * 40
    assert admission.await_args.kwargs["dispatch_revision"] == payload["inputs"]["adp_definition_revision"]
    assert admission.await_args.kwargs["initial"] is True
    assert len(calls) == 2
    assert [options["params"]["ref"] for _, path, options in provider.requests if "/contents/" in path] == ["b" * 40, provider.head]

    document = yaml.safe_load(provider.workflow)
    guards = [
        step["run"] for job in document["jobs"].values() for step in job.get("steps", []) if step.get("name") == "Guard engine delivery revisions"
    ]
    assert guards
    env = {
        **os.environ,
        "ADP_MANUAL_SOURCE": "",
        "ADP_CORRELATION": payload["inputs"]["adp_correlation"],
        "ADP_SOURCE": payload["inputs"]["adp_source_revision"],
        "ADP_DEFINITION": payload["inputs"]["adp_definition_revision"],
        "INPUT_PRODUCT_CASES": "none",
        "INPUT_ENVIRONMENT": "dev",
    }
    for guard in guards:
        for actual, expected in [(provider.head, 0), ("d" * 40, 1)]:
            result = subprocess.run(["bash", "-c", guard], env={**env, "ACTUAL_DEFINITION": actual}, capture_output=True)
            assert result.returncode == expected, result.stderr


@pytest.mark.parametrize("reason", ["unrelated", "changed", "invalid_head"])
async def test_dispatch_refuses_unverified_branch_before_mutation(broker, monkeypatch, reason):
    provider, _calls = broker
    provider.execution["persona"] = {"S": "operations"}
    admission = AsyncMock()
    monkeypatch.setattr(actions_routes, "_require_shared_release_admission", admission)
    provider.head = "c" * 40
    provider.history = [provider.head, "b" * 40]
    if reason == "unrelated":
        provider.history = [provider.head]
    elif reason == "changed":
        provider.workflow_at_head = provider.workflow + b"# changed workflow\n"
    else:
        provider.head = "invalid"
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})
    assert error.value.status_code == 404
    admission.assert_not_awaited()
    assert not any(method == "POST" and path != "/graphql" for method, path, _options in provider.requests)


@pytest.mark.parametrize("workflow", ["gateway-deploy.yml", "gateway-frontend-deploy.yml"])
async def test_maintained_gateway_dev_dispatch_does_not_require_non_dev_approval(broker, workflow):
    provider, calls = broker
    provider.workflow = maintained_gateway_workflow(workflow)
    provider.issued.workflow_allowlist = {workflow}
    product_inputs = {}
    provider.issued.product_cases = (
        "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"
        if workflow == "gateway-deploy.yml"
        else "live_browser_create,live_browser_conflict_denial,live_browser_evidence"
    )
    product_inputs = {"product_cases": provider.issued.product_cases}

    result = await actions_routes._perform(None, None, workflow=workflow, run_id=None, inputs={})

    assert result == {"dispatched": True, "workflow": workflow, "correlation": "run-1"}
    assert len(calls) == 2
    assert provider.requests[-1][2]["json"] == {
        "ref": "main",
        "inputs": {
            "adp_correlation": "run-1",
            "adp_source_revision": "a" * 40,
            "adp_definition_revision": "b" * 40,
            **product_inputs,
        },
    }


async def test_native_product_dispatch_selects_recovery_and_matching_run_adoption(broker):
    provider, _calls = broker
    provider.workflow = maintained_gateway_workflow()
    provider.issued.workflow_allowlist = {"gateway-deploy.yml"}
    provider.issued.product_cases = "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"
    await actions_routes._perform(None, None, workflow="gateway-deploy.yml", run_id=None, inputs={})
    workflow = yaml.safe_load(provider.workflow)
    specs = workflow[True]["workflow_dispatch"]["inputs"]
    defaults = {name: str(spec.get("default", "")) for name, spec in specs.items()}
    recorded = {**defaults, **provider.requests[-1][2]["json"]["inputs"]}
    assert actions_routes._inputs_match_defaults(recorded, defaults, product_cases=provider.issued.product_cases)
    expression = workflow["jobs"]["product-recovery"]["if"]
    for selection, environment, ref, acceptance, eligible in (
        (recorded["product_cases"], recorded["environment"], "refs/heads/main", "success", True),
        ("none", "dev", "refs/heads/main", "success", False),
        ("api_product_lifecycle", "dev", "refs/heads/main", "success", False),
        (recorded["product_cases"], "prod", "refs/heads/main", "success", False),
        (recorded["product_cases"], "dev", "refs/heads/other", "success", False),
        (recorded["product_cases"], "dev", "refs/heads/main", "failure", False),
    ):
        condition = expression
        for reference, value in {
            "always()": True,
            "github.ref": ref,
            "inputs.environment": environment,
            "inputs.product_cases": selection,
            "needs.product-acceptance.result": acceptance,
            "needs.product-acceptance-guard.result": "success",
            "needs.product-cli-evidence.result": "success",
        }.items():
            condition = condition.replace(reference, repr(value))
        assert bool(eval(condition.replace("&&", "and").replace("||", "or"), {"__builtins__": {}})) is eligible
    for change in ({"product_recovery_hold": "true"}, {"environment": "prod"}, {"product_cases": "none"}):
        assert not actions_routes._inputs_match_defaults({**recorded, **change}, defaults, product_cases=provider.issued.product_cases)
    with pytest.raises(HTTPException):
        await actions_routes._perform(None, None, workflow="gateway-deploy.yml", run_id=None, inputs={"product_recovery_hold": "true"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment", {"default": "staging"}),
        ("environment", {"default": "prod"}),
        ("environment", {}),
        ("environment", "dev"),
        ("adp_approved_revision", {"default": "a" * 40}),
        ("adp_approved_revision", {"default": "", "required": True}),
        ("adp_approved_revision", {}),
        ("adp_approved_revision", None),
    ],
)
@pytest.mark.parametrize("workflow", ["gateway-deploy.yml", "gateway-frontend-deploy.yml"])
async def test_gateway_approval_exception_requires_optional_blank_approval_and_dev_default(broker, field, value, workflow):
    provider, _calls = broker
    document = yaml.safe_load(maintained_gateway_workflow(workflow))
    document[True]["workflow_dispatch"]["inputs"][field] = value
    provider.workflow = yaml.safe_dump(document).encode()
    provider.issued.workflow_allowlist = {workflow}

    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow=workflow, run_id=None, inputs={})

    assert error.value.status_code == 403
    assert error.value.detail == "human_approval_input"
    assert not any(method == "POST" for method, _path, _options in provider.requests)


async def test_gateway_approval_exception_does_not_apply_to_other_workflows(broker):
    provider, _calls = broker
    provider.workflow = maintained_gateway_workflow()
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})
    assert error.value.status_code == 403
    assert not any(method == "POST" for method, _path, _options in provider.requests)


@pytest.mark.parametrize("inputs", [{"environment": "prod"}, {"adp_approved_revision": "a" * 40}])
@pytest.mark.parametrize("workflow", ["gateway-deploy.yml", "gateway-frontend-deploy.yml"])
async def test_gateway_dev_dispatch_still_refuses_custom_inputs(broker, inputs, workflow):
    provider, _calls = broker
    provider.workflow = maintained_gateway_workflow(workflow)
    provider.issued.workflow_allowlist = {workflow}
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow=workflow, run_id=None, inputs=inputs)
    assert error.value.status_code == 404
    assert not any(method == "POST" for method, _path, _options in provider.requests)


@pytest.mark.parametrize("workflow", ["gateway-deploy.yml", "gateway-frontend-deploy.yml"])
async def test_gateway_dev_dispatch_still_requires_identical_pinned_definition(broker, workflow):
    provider, _calls = broker
    provider.workflow = maintained_gateway_workflow(workflow)
    provider.issued.workflow_allowlist = {workflow}
    provider.head = "c" * 40
    provider.history = [provider.head, "b" * 40]
    provider.workflow_at_head = provider.workflow + b"\n# A newer workflow needs a new reviewed definition pin.\n"
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow=workflow, run_id=None, inputs={})
    assert error.value.status_code == 404
    assert not any(method == "POST" and path != "/graphql" for method, path, _options in provider.requests)


@pytest.mark.parametrize("workflow", ["gateway-deploy.yml", "gateway-frontend-deploy.yml"])
async def test_gateway_manual_rerun_cannot_claim_dev_defaults_from_correlation_alone(broker, workflow):
    provider, _calls = broker
    provider.workflow = maintained_gateway_workflow(workflow)
    provider.issued.workflow_allowlist = {workflow}
    provider.run = {
        "path": f".github/workflows/{workflow}",
        "repository": {"id": 789},
        "event": "workflow_dispatch",
        "display_title": "ADP deployment run-1",
        "head_sha": "b" * 40,
        "head_branch": "main",
        "conclusion": "failure",
    }
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow=None, run_id=45, inputs={})
    assert error.value.status_code == 403
    assert error.value.detail == "human_approval_input"
    assert not any(method == "POST" for method, _path, _options in provider.requests)


@pytest.mark.parametrize("content", ["a", "%%%%", "YWJj!", "YQ==extra", "YWJj\u00a0", None, 1, [], {}])
async def test_dispatch_refuses_malformed_base64_content(broker, content):
    provider, _calls = broker
    provider.definition_overrides = {"content": content}
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})
    assert error.value.status_code == 404
    assert error.value.detail == "actions operation is not authorized"
    assert not any(method == "POST" for method, _path, _options in provider.requests)


async def test_dispatch_accepts_ascii_whitespace_in_base64_content(broker):
    provider, _calls = broker
    encoded = base64.b64encode(provider.workflow).decode()
    provider.definition_overrides = {"content": " \t\r\n\v\f".join(encoded)}
    result = await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})
    assert result["dispatched"] is True


@pytest.mark.parametrize("size", [-1, True, 131073])
async def test_dispatch_refuses_invalid_declared_size(broker, size):
    provider, _calls = broker
    provider.definition_overrides = {"size": size}
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})
    assert error.value.status_code == 404
    assert not any(method == "POST" for method, _path, _options in provider.requests)


async def test_dispatch_refuses_decoded_size_mismatch(broker):
    provider, _calls = broker
    provider.definition_overrides = {"size": len(provider.workflow) - 1}
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})
    assert error.value.status_code == 404
    assert not any(method == "POST" for method, _path, _options in provider.requests)


@pytest.mark.parametrize("size", [131072, 131073])
async def test_dispatch_enforces_decoded_size_limit(broker, size):
    provider, _calls = broker
    provider.workflow += b"#" + b"x" * (size - len(provider.workflow) - 1)
    if size == 131072:
        result = await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})
        assert result["dispatched"] is True
    else:
        with pytest.raises(HTTPException) as error:
            await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})
        assert error.value.status_code == 404
        assert not any(method == "POST" for method, _path, _options in provider.requests)


@pytest.mark.parametrize("workflow", [b"on: [", b"\xff", b"on:\n  workflow_dispatch:\n    inputs: null\n"])
async def test_dispatch_refuses_malformed_workflow_document(broker, workflow):
    provider, _calls = broker
    provider.workflow = workflow
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow="deploy.yml", run_id=None, inputs={})
    assert error.value.status_code == 404
    assert not any(method == "POST" for method, _path, _options in provider.requests)


async def test_dispatch_refuses_unlisted_workflow(broker):
    provider, _calls = broker
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow="unsafe.yml", run_id=None, inputs={})
    assert error.value.status_code == 404
    assert provider.requests == []


async def test_rerun_requires_failed_correlated_run(broker):
    provider, _calls = broker
    provider.run = {
        "path": ".github/workflows/deploy.yml",
        "repository": {"id": 789},
        "event": "workflow_dispatch",
        "display_title": "ADP deployment other-run",
        "head_sha": "b" * 40,
        "head_branch": "main",
        "conclusion": "failure",
    }
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow=None, run_id=12, inputs={})
    assert error.value.status_code == 404
    assert not any(method == "POST" for method, _path, _options in provider.requests)
    provider.run["display_title"] = "ADP deployment run-1"
    result = await actions_routes._perform(None, None, workflow=None, run_id=12, inputs={})
    assert result["rerun"] is True
    assert provider.requests[-1][1].endswith("/actions/runs/12/rerun")


async def test_rerun_automatic_deployment_requires_current_revision_and_branch(broker):
    provider, _calls = broker
    provider.run = {
        "path": ".github/workflows/deploy.yml",
        "repository": {"id": 789},
        "event": "push",
        "head_sha": "b" * 40,
        "head_branch": "main",
        "conclusion": "failure",
    }
    provider.workflow = b"on: [push]\n"
    assert (await actions_routes._perform(None, None, workflow=None, run_id=12, inputs={}))["rerun"]
    provider.requests.clear()
    for name, value in [("head_sha", "c" * 40), ("head_branch", "other"), ("event", "pull_request")]:
        previous = provider.run[name]
        provider.run[name] = value
        with pytest.raises(HTTPException):
            await actions_routes._perform(None, None, workflow=None, run_id=12, inputs={})
        assert not any(method == "POST" for method, _, _ in provider.requests)
        provider.run[name] = previous


async def test_rerun_of_the_workers_own_dispatch_is_allowed_at_a_later_head(broker):
    """#7828: engine dispatches run at the default-branch tip, which is past the pinned
    definition as soon as main moves (door3: run head e76eea36, pin bcf48fa7). The
    correlation title proves the run is this execution's own dispatch; the workflow is
    then verified byte-identical at the run's own head."""
    provider, _calls = broker
    provider.run = {
        "path": ".github/workflows/deploy.yml",
        "repository": {"id": 789},
        "event": "workflow_dispatch",
        "display_title": "ADP deployment run-1",
        "head_sha": "c" * 40,
        "head_branch": "main",
        "conclusion": "failure",
    }
    result = await actions_routes._perform(None, None, workflow=None, run_id=12, inputs={})
    assert result["rerun"] is True
    refs = [options.get("params", {}).get("ref") for _method, path, options in provider.requests if "/contents/" in path]
    assert refs == ["b" * 40, "c" * 40]  # pin, then the run's own head — never the moving branch tip
    assert provider.requests[-1][1].endswith("/actions/runs/12/rerun")

    provider.requests.clear()
    provider.run["display_title"] = "ADP deployment someone-else"
    with pytest.raises(HTTPException):
        await actions_routes._perform(None, None, workflow=None, run_id=12, inputs={})
    assert not any(method == "POST" for method, _path, _options in provider.requests)


async def test_rerun_refuses_a_push_run_at_another_head_even_when_titled(broker):
    provider, _calls = broker
    provider.run = {
        "path": ".github/workflows/deploy.yml",
        "repository": {"id": 789},
        "event": "push",
        "display_title": "ADP deployment run-1",
        "head_sha": "c" * 40,
        "head_branch": "main",
        "conclusion": "failure",
    }
    provider.workflow = b"on: [push]\n"
    with pytest.raises(HTTPException):
        await actions_routes._perform(None, None, workflow=None, run_id=12, inputs={})
    assert not any(method == "POST" for method, _path, _options in provider.requests)


async def test_logs_can_be_narrowed_to_the_end_of_the_tail(broker, monkeypatch):
    provider, _calls = broker
    provider.run = {
        "id": 12,
        "repository": {"id": 789},
        "path": ".github/workflows/deploy.yml",
        "event": "workflow_dispatch",
        "display_title": "ADP deployment run-1",
        "status": "completed",
        "conclusion": "failure",
        "head_sha": "c" * 40,
        "head_branch": "main",
        "run_attempt": 1,
        "html_url": "https://github.com/org/repo/actions/runs/12",
        "created_at": "2026-10-15T17:18:57Z",
        "updated_at": "2026-10-15T17:23:09Z",
    }
    full = "setup " * 400 + "##[error]Failed to CreateArtifact: quota"
    monkeypatch.setattr(actions_routes, "fetch_log_tail", AsyncMock(return_value=(full, False)))
    narrowed = await actions_routes._observe(None, None, run_id=12, logs=True, tail_bytes=512)
    assert actions_routes.fetch_log_tail.await_args.kwargs["failure_focus"] is False
    assert narrowed["truncated"] is True and len(narrowed["log_tail"].encode()) == 512
    assert narrowed["log_tail"].endswith("##[error]Failed to CreateArtifact: quota")
    whole = await actions_routes._observe(None, None, run_id=12, logs=True)
    assert actions_routes.fetch_log_tail.await_args.kwargs["failure_focus"] is True
    assert whole["log_tail"] == full and whole["truncated"] is False


def test_runtime_hold_reasons_name_cluster_read_refusals():
    """The worker must see a refused cluster read-back by name, not as "unavailable" (#7870)."""
    from src.agentauth.actions_routes import RUNTIME_HOLD_REASONS

    assert {
        "deployment_runtime_read_forbidden",
        "deployment_runtime_read_failed",
        "deployment_door_identity_probe_unreachable",
        "deployment_provider_response_limit",
    } <= RUNTIME_HOLD_REASONS
    assert "deployment_door_identity_probe_failed" not in RUNTIME_HOLD_REASONS  # a Door that does not enforce is a wrong runtime
    assert "deployment_runtime_mismatch" not in RUNTIME_HOLD_REASONS


async def test_manual_rerun_rejects_approval_input_even_for_own_failed_run(broker):
    provider, _calls = broker
    provider.workflow += b"      adp_approved_revision:\n        default: ''\n"
    provider.run = {
        "path": ".github/workflows/deploy.yml",
        "repository": {"id": 789},
        "event": "workflow_dispatch",
        "display_title": "ADP deployment run-1",
        "head_sha": "b" * 40,
        "head_branch": "main",
        "conclusion": "failure",
    }
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow=None, run_id=12, inputs={})
    assert error.value.status_code == 403
    assert error.value.detail == "human_approval_input"
    assert not any(method == "POST" for method, _path, _options in provider.requests)


def proof_workflow():
    return (
        b"on:\n  workflow_dispatch:\n    inputs:\n      adp_correlation: {}\n"
        b"      adp_source_revision: {}\n      adp_definition_revision: {}\n"
        b"      environment:\n        default: dev\n      product_cases:\n        default: none\n"
        b"      proof_phase:\n        default: none\n"
    )


async def test_identity_proof_phase_dispatch_binds_revisions_and_selection(broker):
    provider, calls = broker
    provider.workflow = proof_workflow()
    provider.issued.workflow_allowlist = {"gateway-deploy.yml"}
    provider.issued.proof_phase = "stage.identity-proof"
    result = await actions_routes._perform(None, None, workflow="gateway-deploy.yml", run_id=None, inputs={})
    assert result == {"dispatched": True, "workflow": "gateway-deploy.yml", "correlation": "run-1"}
    assert len(calls) == 2
    assert provider.requests[-1][2]["json"]["inputs"] == {
        "adp_correlation": "run-1",
        "adp_source_revision": "a" * 40,
        "adp_definition_revision": "b" * 40,
        "proof_phase": "stage.identity-proof",
    }


async def test_existing_gateway_deploy_without_accepted_proof_never_selects_proof(broker):
    provider, _calls = broker
    provider.workflow = proof_workflow()
    provider.issued.workflow_allowlist = {"gateway-deploy.yml"}
    await actions_routes._perform(None, None, workflow="gateway-deploy.yml", run_id=None, inputs={})
    assert "proof_phase" not in provider.requests[-1][2]["json"]["inputs"]


@pytest.mark.parametrize(
    "phase,workflow,custom",
    [
        ("stage.identity-proof", "gateway-deploy.yml", {"gateway_revision": "f" * 40}),
        ("stage.identity-proof", "tools-live-evaluation.yml", {}),
        ("unreviewed", "gateway-deploy.yml", {}),
    ],
)
async def test_proof_selection_rejects_caller_inputs_and_wrong_reviewed_workflow(broker, phase, workflow, custom):
    provider, _calls = broker
    provider.workflow = proof_workflow()
    provider.issued.workflow_allowlist = {workflow}
    provider.issued.proof_phase = phase
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow=workflow, run_id=None, inputs=custom)
    assert error.value.status_code == 404
    assert not any(method == "POST" for method, _path, _options in provider.requests)


@pytest.mark.parametrize(
    "definition",
    [
        proof_workflow().replace(b"default: none\n", b"default: stage.identity-proof\n"),
        proof_workflow().replace(b"default: dev", b"default: staging"),
        proof_workflow().replace(b"      proof_phase:\n        default: none\n", b""),
    ],
)
async def test_proof_selection_rejects_changed_definition_defaults(broker, definition):
    provider, _calls = broker
    provider.workflow = definition
    provider.issued.workflow_allowlist = {"gateway-deploy.yml"}
    provider.issued.proof_phase = "stage.identity-proof"
    with pytest.raises(HTTPException):
        await actions_routes._perform(None, None, workflow="gateway-deploy.yml", run_id=None, inputs={})
    assert not any(method == "POST" for method, _path, _options in provider.requests)


def test_adapter_stage_context_requires_exact_selected_input():
    defaults = {"stage": "platform", "environment": "dev", "mode": "release"}
    selected = {"stage": "tool-adapters", "environment": "dev", "mode": "release"}
    assert actions_routes._inputs_match_defaults(selected, defaults, adapter_stage=True)
    assert not actions_routes._inputs_match_defaults(selected, defaults)
    for changed in ("platform", "other", ""):
        assert not actions_routes._inputs_match_defaults({**selected, "stage": changed}, defaults, adapter_stage=True)
    assert not actions_routes._inputs_match_defaults({**selected, "mode": "drift"}, defaults, adapter_stage=True)
    assert not actions_routes._inputs_match_defaults(selected, {**defaults, "stage": "other"}, adapter_stage=True)


def test_proof_context_requires_exact_server_bound_phase_and_source_revision():
    defaults = {"environment": "dev", "product_cases": "none", "proof_phase": "none"}
    recorded = {"environment": "dev", "product_cases": "none", "proof_phase": "stage.identity-proof"}

    def matches(inputs, definitions=defaults):
        return actions_routes._inputs_match_defaults(inputs, definitions, proof_phase="stage.identity-proof", source_revision="a" * 40)

    assert matches(recorded)
    for changes in (
        {"proof_phase": "none"},
        {"environment": "staging"},
        {"product_cases": "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"},
        {"manual_source_revision": "a" * 40},
    ):
        assert not matches({**recorded, **changes})
    assert matches({"proof_phase": "stage.identity-proof"})
    assert not matches({"environment": "dev"})
    assert not matches(recorded, {**defaults, "proof_phase": "stage.identity-proof"})
    assert not actions_routes._inputs_match_defaults(recorded, defaults, proof_phase="stage.identity-proof", source_revision="unbound")


@pytest.mark.parametrize(
    "component,workflow,suite",
    [
        ("payments-api", "ship-payments.yml", "api-smoke"),
        ("stock-worker", "publish-stock.yml", "cli-roundtrip"),
    ],
)
async def test_generic_contract_dispatches_only_accepted_product_inputs(broker, component, workflow, suite):
    from src.orchestration.delivery_contract import DeliveryContract

    provider, _calls = broker
    delivery = DeliveryContract(
        schema_version=2,
        connection_id="fictional-connection",
        resource_kind="service",
        resource_id=component,
        baseline_revision="a" * 40,
        definition_revision="b" * 40,
        workflow_inputs={"environment": "test", "suite": suite},
        required_checks=[suite],
    )
    provider.issued.delivery_contract = delivery
    provider.issued.workflow_allowlist = {workflow}
    provider.workflow += b"      adp_contract_digest: {}\n      environment:\n        default: prod\n      suite:\n        default: none\n"
    result = await actions_routes._perform(None, None, workflow=workflow, run_id=None, inputs={})
    assert result["dispatched"] is True
    posted = next(kwargs["json"] for method, path, kwargs in provider.requests if method == "POST")
    assert posted["inputs"]["environment"] == "test"
    assert posted["inputs"]["suite"] == suite
    assert posted["inputs"]["adp_contract_digest"] == delivery.digest
    assert actions_routes._inputs_match_defaults(
        delivery.inputs, {"environment": "prod", "suite": "none", "adp_contract_digest": ""}, accepted_inputs=delivery.inputs
    )
    assert not actions_routes._inputs_match_defaults(
        {**delivery.inputs, "suite": "other"}, {"environment": "prod", "suite": "none", "adp_contract_digest": ""}, accepted_inputs=delivery.inputs
    )
    with pytest.raises(HTTPException) as error:
        await actions_routes._perform(None, None, workflow=workflow, run_id=None, inputs={"environment": "prod"})
    assert error.value.status_code == 409
    assert error.value.detail == "custom_workflow_inputs_are_not_admitted"


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "startup_failure", "success"])
async def test_generic_contract_can_rerun_terminal_failures(broker, conclusion):
    from tests.orchestration.test_generic_delivery import config

    provider, _calls = broker
    provider.issued.delivery_contract = config().model_copy(update={"workflow_inputs": {}})
    provider.workflow += b"      adp_contract_digest: {}\n"
    provider.run = {
        "id": 42,
        "run_attempt": 1,
        "path": ".github/workflows/deploy.yml",
        "repository": {"id": 789},
        "event": "workflow_dispatch",
        "display_title": "ADP deployment run-1",
        "head_sha": "b" * 40,
        "head_branch": "main",
        "status": "completed",
        "conclusion": conclusion,
    }
    if conclusion == "success":
        with pytest.raises(HTTPException) as error:
            await actions_routes._perform(None, None, workflow=None, run_id=42, inputs={})
        assert error.value.detail == "run_is_not_a_failed_deployment_for_this_revision"
        assert not any(method == "POST" for method, _path, _kwargs in provider.requests)
    else:
        result = await actions_routes._perform(None, None, workflow=None, run_id=42, inputs={})
        assert result["rerun"] is True
        assert provider.requests[-1][:2] == ("POST", "/repos/owner/repo/actions/runs/42/rerun")


@pytest.mark.parametrize("generic_delivery,expected", [(True, "false"), (False, "False")])
async def test_actions_boolean_defaults_preserve_the_evidence_protocol(broker, generic_delivery, expected):
    provider, _calls = broker
    provider.workflow += b"      dry_run:\n        type: boolean\n        default: false\n"
    _, binding, _ = await actions_routes._authority(None, None, mutate=True)
    defaults = await actions_routes._definition(
        provider(), binding, workflow="deploy.yml", revision="b" * 40, branch="main", token="test-token", generic_delivery=generic_delivery
    )
    assert defaults["dry_run"] == expected


async def test_grouped_release_admission_follows_the_manifest_not_one_wave(monkeypatch):
    """A reviewed release group installs admission for every accepted node that owns it.

    The adapter release story holds its own operations node for the grouped
    adp-dev-tool-adapters target (#8453). While admission was keyed to a single flow
    slug and wave name, that node's correlated dispatch was refused as "not installed"
    and no deployment run could ever exist. The manifest entry's release_group is the
    reviewed approval; reserve_native_release still re-verifies pins, the signed job
    and the shared lease under lock.
    """
    from src.orchestration import accepted_node, operations_shared_release, review_cycle_dispatch
    from src.orchestration.deployment_manifest import EntryStatus
    from src.orchestration.models import OrchestrationFlow, OrchestrationNode
    from src.orchestration.run_reports import OrchestrationRunReport

    node = SimpleNamespace(id="node-1", org_id="org", flow_id="flow", wave_ref="w07b-adapter-release", attempts=2)
    flow = SimpleNamespace(id="flow", org_id="org", slug="atlassian-adapters-8445-parallel")
    release = SimpleNamespace(
        target="adp-dev-tool-adapters",
        component="tool-adapters",
        repository="owner/repo",
        workflows=["tools-platform-deploy.yml"],
    )
    contract = SimpleNamespace(
        plan_version=28,
        node=SimpleNamespace(operational_release_binding=release, evidence_expectations=["runtime_verified:tool-adapters"]),
        environment_connection_ids=["connection"],
    )
    entry = SimpleNamespace(
        entry_id="adp-dev-tool-adapters",
        status=EntryStatus.ENABLED,
        docs_only=False,
        release_group="atlassian-foundation",
        connection_id="connection",
        covers=lambda component: component == "tool-adapters",
        workflow=SimpleNamespace(path=".github/workflows/tools-platform-deploy.yml"),
    )
    row = SimpleNamespace(id="execution", org_id="org", node_id="node-1", flow_id="flow", cycle=2, claim_id="claim", claim_generation=4, revision=3)
    report = SimpleNamespace(org_id="org", node_id="node-1", run_id="run-1", repo="owner/repo", provider_repository_id=789)

    class Session:
        committed = False

        async def get(self, model, _identifier, **_kwargs):
            return {OrchestrationNode: node, OrchestrationFlow: flow, OrchestrationRunReport: report}[model]

        async def scalar(self, _statement):
            return row

        async def commit(self):
            self.committed = True

    session = Session()

    @asynccontextmanager
    async def factory():
        yield session

    reserve = AsyncMock()
    monkeypatch.setattr(accepted_node, "accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr(actions_routes, "release_manifest", lambda *_args: SimpleNamespace(entry=lambda _target: entry))
    monkeypatch.setattr(operations_shared_release, "reserve_native_release", reserve)
    monkeypatch.setattr(
        review_cycle_dispatch, "ReviewCycleServices", lambda _factory: SimpleNamespace(authority_context=AsyncMock(return_value=("values",)))
    )

    component = await actions_routes._require_shared_release_admission(
        SimpleNamespace(node_id="node-1", plan_version=28),
        SimpleNamespace(repo="owner/repo", org_id="org", provider_repository_id=789),
        "tools-platform-deploy.yml",
        factory,
        {"invocation_id": {"S": "run-1"}, "source_revision": {"S": "a" * 40}, "definition_revision": {"S": "b" * 40}},
        environment="dev",
        initial=True,
        dispatch_revision="c" * 40,
    )

    assert component == "tool-adapters"
    assert session.committed is True
    assert reserve.await_args.kwargs["workflow"] == "tools-platform-deploy.yml"
    assert reserve.await_args.kwargs["environment"] == "dev"
    assert reserve.await_args.kwargs["initial"] is True
    assert reserve.await_args.kwargs["dispatch_revision"] == "c" * 40
