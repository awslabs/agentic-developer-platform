"""Read-only Actions routes disclose only bounded data for the bound repository."""

import base64
import io
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.agentauth import actions_routes


@pytest.fixture
def read_broker(monkeypatch):
    issued = SimpleNamespace(repository="owner/repo", permissions={"actions": "read", "metadata": "read"})
    binding = SimpleNamespace(repo="owner/repo", org_id="org", installation_id=12, provider_repository_id=789)
    authority_calls = []
    provider_calls = []
    run = {
        "id": 42,
        "repository": {"id": 789},
        "display_title": "job ghp_" + "x" * 40,
        "name": "deploy",
        "path": ".github/workflows/deploy.yml",
        "status": "completed",
        "conclusion": "success",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as logs:
        logs.writestr("job.txt", b"a" * 70000 + b"\nghp_" + b"x" * 40 + b"\n")

    async def authority(*_args, **kwargs):
        authority_calls.append(kwargs)
        return issued, binding, {}

    async def token(**kwargs):
        assert kwargs["permissions"] == issued.permissions
        return "gateway-only"

    class Provider:
        async def request(self, _binding, method, path, **kwargs):
            provider_calls.append((method, path, kwargs))
            if path.endswith("/logs"):
                return SimpleNamespace(status_code=200, content=buffer.getvalue())
            if path.endswith("/runs"):
                return SimpleNamespace(json=lambda: {"workflow_runs": [run] * 25})
            return SimpleNamespace(json=lambda: run)

    monkeypatch.setattr(actions_routes, "_authority", authority)
    monkeypatch.setattr(actions_routes, "installation_token", token)
    monkeypatch.setattr(actions_routes, "WorkflowProvider", Provider)
    return run, authority_calls, provider_calls


async def test_runs_are_paged_and_titles_masked(read_broker):
    _run, authority_calls, provider_calls = read_broker
    result = await actions_routes._observe(None, None, page=2)
    assert len(result["runs"]) == 20 and result["page"] == 2
    assert "ghp_" not in result["runs"][0]["display_title"]
    assert provider_calls[0][2]["params"] == {"per_page": 20, "page": 2}
    assert authority_calls == [{"mutate": False}]
    assert actions_routes.RunsRequest(page=5).page == 5
    with pytest.raises(ValueError):
        actions_routes.RunsRequest(page=6)


async def test_runs_listing_is_scoped_to_allow_listed_workflows_on_the_deployment_branch(read_broker, monkeypatch):
    _run, _authority_calls, provider_calls = read_broker
    permissions = {"actions": "read", "metadata": "read"}
    issued = SimpleNamespace(repository="owner/repo", permissions=permissions, workflow_allowlist=frozenset({"deploy.yml"}))
    binding = SimpleNamespace(repo="owner/repo", org_id="org", installation_id=12, provider_repository_id=789)

    async def authority(*_args, **_kwargs):
        return issued, binding, {"default_branch": {"S": "main"}}

    monkeypatch.setattr(actions_routes, "_authority", authority)
    result = await actions_routes._observe(None, None, page=1)
    assert provider_calls[0][1] == "/repos/owner/repo/actions/workflows/deploy.yml/runs"
    assert provider_calls[0][2]["params"] == {"per_page": 10, "page": 1, "branch": "main"}
    assert provider_calls[0][2]["max_bytes"] == 512 * 1024
    assert len(result["runs"]) == 20 and result["runs"][0]["id"] == 42


async def test_run_detail_refuses_repository_identity_mismatch(read_broker):
    run, _authority_calls, _provider_calls = read_broker
    run["repository"] = {"id": 790}
    with pytest.raises(HTTPException) as error:
        await actions_routes._observe(None, None, run_id=42)
    assert error.value.status_code == 404


async def test_product_worker_can_observe_completed_push_without_acceptance_evidence(read_broker, monkeypatch):
    from unittest.mock import AsyncMock

    run, _authority_calls, provider_calls = read_broker
    issued = SimpleNamespace(
        repository="owner/repo",
        permissions={"actions": "read", "metadata": "read"},
        workflow_allowlist={"deploy.yml"},
        product_cases="api_product_lifecycle,api_denial_oracle,api_recovery_cleanup",
    )
    binding = SimpleNamespace(repo="owner/repo", org_id="org", installation_id=12, provider_repository_id=789)
    execution = {
        "invocation_id": {"S": "run-1"},
        "persona": {"S": "operations"},
        "source_revision": {"S": "a" * 40},
        "definition_revision": {"S": "a" * 40},
        "default_branch": {"S": "main"},
    }
    run.update(event="push", head_branch="main", head_sha="a" * 40, run_attempt=1)
    monkeypatch.setattr(actions_routes, "_authority", AsyncMock(return_value=(issued, binding, execution)))
    sessions = AsyncMock(side_effect=AssertionError("a push must not record Product acceptance"))
    monkeypatch.setattr(actions_routes, "get_session_factory", sessions)

    result = await actions_routes._observe(None, None, run_id=42)

    assert result["run"]["status"] == "completed"
    assert result["run"]["conclusion"] == "success"
    assert result["workflow_evidence_id"] is None
    assert "workflow_dispatch" in result["observation_note"]
    assert "runtime_evidence_id" not in result
    assert len(provider_calls) == 1
    sessions.assert_not_called()


async def test_logs_return_only_masked_64kb_tail(read_broker):
    _run, _authority_calls, provider_calls = read_broker
    result = await actions_routes._observe(None, None, run_id=42, logs=True)
    assert result["truncated"] is True and result["run_id"] == 42
    assert len(result["log_tail"].encode()) <= 65536
    assert "ghp_" not in result["log_tail"]
    assert provider_calls[-1][1] == "/repos/owner/repo/actions/runs/42/logs"
    assert "gateway-only" not in str(result)


@pytest.mark.parametrize("approval_input", [False, True])
@pytest.mark.parametrize("product_selection", [False, True])
async def test_correlated_success_context_is_recorded_only_after_revalidation(monkeypatch, approval_input, product_selection):
    from unittest.mock import AsyncMock

    from src.orchestration import operations_runtime_evidence, operations_workflow_evidence
    from src.orchestration.deployment_workflow_provider import WorkflowContext

    binding = SimpleNamespace(repo="owner/repo", org_id="org", installation_id=12, provider_repository_id=789)
    selected = "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"
    issued = SimpleNamespace(
        node_id="node-1", workflow_allowlist={"deploy.yml"}, plan_version=1, product_cases=selected if product_selection else "none"
    )
    execution = {
        "invocation_id": {"S": "run-1"},
        "persona": {"S": "operations"},
        "source_revision": {"S": "a" * 40},
        "definition_revision": {"S": "b" * 40},
        "default_branch": {"S": "main"},
    }
    run = {
        "id": 42,
        "run_attempt": 1,
        "path": ".github/workflows/deploy.yml",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "display_title": "ADP deployment run-1",
        "head_sha": "b" * 40,
        "head_branch": "main",
        "repository": {"id": 789},
    }
    context = WorkflowContext(
        schema_version=1,
        repository_id=789,
        run_id=42,
        run_attempt=1,
        workflow_path=run["path"],
        workflow_revision="b" * 40,
        source_revision="a" * 40,
        account_id="123456789012",
        region="us-east-1",
        resource_kind="test-target",
        resource_id="fictional-target",
        inputs={"environment": "dev", **({"product_cases": selected} if product_selection else {})},
        correlation="run-1",
    )
    reauthorize = AsyncMock(return_value=(issued, binding, execution))
    workflow = (
        b"on:\n  workflow_dispatch:\n    inputs:\n      environment:\n        default: dev\n"
        b"      adp_correlation: {}\n      adp_source_revision: {}\n      adp_definition_revision: {}\n"
    )
    if approval_input:
        workflow += b"      adp_approved_revision:\n        default: ''\n"
    if product_selection:
        workflow += b"      product_cases:\n        default: none\n"
    definition_response = SimpleNamespace(
        json=lambda: {
            "type": "file",
            "encoding": "base64",
            "size": len(workflow),
            "content": base64.encodebytes(workflow).decode(),
        }
    )
    verify_definition = AsyncMock(wraps=actions_routes._definition)
    provider = SimpleNamespace(
        context=AsyncMock(return_value=(context, 99, "f" * 64)),
        request=AsyncMock(return_value=definition_response),
    )
    recorded = AsyncMock(return_value="action-id")
    verified = AsyncMock(return_value="runtime-id")
    session = SimpleNamespace(commit=AsyncMock())

    class Sessions:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(actions_routes, "_authority", reauthorize)
    monkeypatch.setattr(actions_routes, "_definition", verify_definition)
    monkeypatch.setattr(actions_routes, "_evidence_transport", AsyncMock(return_value="github-artifact"))
    monkeypatch.setattr(actions_routes, "_head_contains_pin", AsyncMock(side_effect=lambda provider, binding, *, pin, head, token: head == pin))
    monkeypatch.setattr(actions_routes, "installation_token", AsyncMock(return_value="gateway-token"))
    monkeypatch.setattr(actions_routes, "get_session_factory", Sessions())
    monkeypatch.setattr(operations_workflow_evidence, "record_correlated_workflow", recorded)
    monkeypatch.setattr(operations_runtime_evidence, "verify_operations_runtime", verified)
    await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
    assert recorded.await_count == 1 and session.commit.await_count == 1
    assert recorded.await_args.kwargs["artifact_id"] == 99 and recorded.await_args.kwargs["reference"] is None
    await verified.await_args.kwargs["reauthorize"]()
    assert verified.await_args.kwargs["context_action_id"] == "action-id"
    assert reauthorize.await_count == 3
    if product_selection:
        provider.context.return_value = (context.model_copy(update={"inputs": {"environment": "dev"}}), 99, "f" * 64)
        with pytest.raises(actions_routes.OperationRefusedError):
            await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
        assert recorded.await_count == 1
        run["event"] = "push"
        result = await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
        assert result["workflow_evidence_id"] is None
        assert "runtime_evidence_id" not in result
        assert recorded.await_count == 1
        return
    run["head_sha"] = "c" * 40
    await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
    assert recorded.await_count == 1
    run["head_sha"] = "b" * 40
    # A context that omits a declared input ran with that input's default (GitHub
    # applies defaults before the workflow starts); publishers that record only the
    # inputs they received (tools-facade-deploy) must still bind.
    provider.context.return_value = (context.model_copy(update={"inputs": {}}), 99, "f" * 64)
    await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
    assert recorded.await_count == 2
    provider.context.return_value = (context.model_copy(update={"inputs": {"environment": "prod"}}), 99, "f" * 64)
    with pytest.raises(actions_routes.OperationRefusedError):
        await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
    provider.context.return_value = (context.model_copy(update={"inputs": {"environment": "dev", "mode": "drift"}}), 99, "f" * 64)
    with pytest.raises(actions_routes.OperationRefusedError):
        await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
    provider.context.return_value = (context.model_copy(update={"correlation": "other-run"}), 99, "f" * 64)
    with pytest.raises(actions_routes.OperationRefusedError):
        await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
    assert recorded.await_count == 2
    run.update(event="push", display_title="Automatic deployment")
    provider.context.return_value = (context.model_copy(update={"correlation": "push-correlation"}), 99, "f" * 64)
    await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
    assert recorded.await_count == 3
    assert verify_definition.await_args.kwargs["observing_push"] is True
    assert all(call.args[1] == "GET" for call in provider.request.await_args_list)


async def test_repaired_push_requires_current_revision_and_push_provenance(monkeypatch):
    from unittest.mock import AsyncMock

    from src.orchestration import operations_runtime_evidence, operations_workflow_evidence
    from src.orchestration.deployment_workflow_provider import WorkflowContext

    binding = SimpleNamespace(repo="owner/repo", org_id="org", installation_id=12, provider_repository_id=789)
    issued = SimpleNamespace(node_id="node-1", workflow_allowlist={"deploy.yml"}, plan_version=1)
    revision = "c" * 40
    execution = {
        "invocation_id": {"S": "operations-run"},
        "persona": {"S": "operations"},
        "source_revision": {"S": revision},
        "definition_revision": {"S": revision},
        "default_branch": {"S": "main"},
    }
    run = {
        "id": 42,
        "run_attempt": 2,
        "path": ".github/workflows/deploy.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "display_title": "Automatic deployment",
        "head_sha": revision,
        "head_branch": "main",
        "repository": {"id": 789},
    }
    context = WorkflowContext(
        schema_version=1,
        repository_id=789,
        run_id=42,
        run_attempt=2,
        workflow_path=run["path"],
        workflow_revision=revision,
        source_revision=revision,
        account_id="123456789012",
        region="us-east-1",
        resource_kind="test-target",
        resource_id="fictional-target",
        inputs={"environment": "dev"},
        correlation="automatic-push-42",
    )
    provider = SimpleNamespace(context=AsyncMock(return_value=(context, 99, "f" * 64)))
    current_head = AsyncMock(return_value=revision)
    recorded = AsyncMock(return_value="context-id")
    verified = AsyncMock(return_value="runtime-id")
    session = SimpleNamespace(commit=AsyncMock())

    class Sessions:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(actions_routes, "_authority", AsyncMock(return_value=(issued, binding, execution)))
    monkeypatch.setattr(actions_routes, "_definition", AsyncMock(return_value={"environment": "dev"}))
    monkeypatch.setattr(actions_routes, "_evidence_transport", AsyncMock(return_value="github-artifact"))
    monkeypatch.setattr(actions_routes, "_head_contains_pin", AsyncMock(side_effect=lambda provider, binding, *, pin, head, token: head == pin))
    monkeypatch.setattr(actions_routes, "installation_token", AsyncMock(return_value="gateway-token"))
    monkeypatch.setattr(actions_routes, "get_session_factory", Sessions())
    monkeypatch.setattr(operations_workflow_evidence, "current_push_head", current_head)
    monkeypatch.setattr(operations_workflow_evidence, "record_correlated_workflow", recorded)
    monkeypatch.setattr(operations_runtime_evidence, "verify_operations_runtime", verified)

    result = await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
    assert result == {"workflow_evidence_id": "context-id", "runtime_evidence_id": "runtime-id"}
    assert recorded.await_count == verified.await_count == 1
    current_head.return_value = "d" * 40
    assert await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider) is None
    assert recorded.await_count == 1
    current_head.return_value = revision
    run["repository"] = {"id": 790}
    assert await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider) is None
    run["repository"] = {"id": 789}
    provider.context.return_value = (context.model_copy(update={"correlation": "operations-run"}), 99, "f" * 64)
    with pytest.raises(actions_routes.OperationRefusedError):
        await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
    provider.context.return_value = (context.model_copy(update={"inputs": {"environment": "dev", "adp_correlation": "operations-run"}}), 99, "f" * 64)
    with pytest.raises(actions_routes.OperationRefusedError):
        await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
    assert recorded.await_count == 1


async def test_push_head_is_bound_to_repository_and_immutable_sha():
    from src.orchestration.operations_workflow_evidence import current_push_head
    from src.orchestration.review_cycle import CycleBlockedError

    binding = SimpleNamespace(repo="owner/repo", provider_repository_id=789)
    response = {"id": 789, "default_branch": "main"}
    branch = {"object": {"sha": "c" * 40}}
    calls = []

    async def request(_binding, _method, path):
        calls.append(path)
        return SimpleNamespace(json=lambda: branch if "/git/ref/" in path else response)

    provider = SimpleNamespace(request=request)
    assert await current_push_head(provider, binding, "main") == "c" * 40
    assert calls == ["/repos/owner/repo", "/repos/owner/repo/git/ref/heads/main"]
    response["id"] = 790
    with pytest.raises(CycleBlockedError, match="operations_push_repository_changed"):
        await current_push_head(provider, binding, "main")
    response["id"] = 789
    response["default_branch"] = "release"
    with pytest.raises(CycleBlockedError, match="operations_push_repository_changed"):
        await current_push_head(provider, binding, "main")
    response["default_branch"] = "main"
    branch["object"]["sha"] = "main"
    with pytest.raises(CycleBlockedError, match="operations_push_head_unavailable"):
        await current_push_head(provider, binding, "main")


async def test_run_route_forwards_provider_run_to_engine_observer(read_broker, monkeypatch):
    from unittest.mock import AsyncMock

    capture = AsyncMock(return_value={"workflow_evidence_id": "context-id", "runtime_evidence_id": "runtime-id"})
    monkeypatch.setattr(actions_routes, "_capture_correlated_run", capture)
    result = await actions_routes._observe(None, None, run_id=42)
    assert result["run"]["id"] == 42
    assert result["runtime_evidence_id"] == "runtime-id"
    assert capture.await_count == 1
    assert capture.await_args.kwargs["raw"] is read_broker[0]


async def test_correlated_success_reads_s3_context_when_the_workflow_publishes_to_s3(monkeypatch):
    # The tools workflows publish deployment evidence to S3 (s3-oidc-v1); the
    # operations observer must read it there or a green run never becomes evidence.
    from unittest.mock import AsyncMock

    from src.orchestration import operations_runtime_evidence, operations_workflow_evidence
    from src.orchestration.deployment_workflow_provider import WorkflowContext

    binding = SimpleNamespace(repo="owner/repo", org_id="org", installation_id=12, provider_repository_id=789)
    issued = SimpleNamespace(node_id="node-1", workflow_allowlist={"tools-platform-deploy.yml"}, plan_version=1)
    execution = {
        "invocation_id": {"S": "orch:run-1"},
        "persona": {"S": "operations"},
        "source_revision": {"S": "a" * 40},
        "definition_revision": {"S": "a" * 40},
        "default_branch": {"S": "main"},
    }
    run = {
        "id": 42,
        "run_attempt": 1,
        "path": ".github/workflows/tools-platform-deploy.yml",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "display_title": "ADP deployment orch:run-1",
        "head_sha": "a" * 40,
        "head_branch": "main",
        "repository": {"id": 789},
    }
    context = WorkflowContext(
        schema_version=1,
        repository_id=789,
        run_id=42,
        run_attempt=1,
        workflow_path=run["path"],
        workflow_revision="a" * 40,
        source_revision="a" * 40,
        account_id="123456789012",
        region="us-east-1",
        resource_kind="agentcore-gateway",
        resource_id="arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/g",
        inputs={"environment": "dev", "mode": "release"},
        correlation="orch:run-1",
    )
    reference = "s3://adp-dev-deployment-evidence-123456789012/deployment-evidence/v1/789/42/1/context/tools-platform-deploy.yml.json?versionId=v1"
    provider = SimpleNamespace(
        context=AsyncMock(side_effect=AssertionError("artifact transport must not be used")),
        s3_evidence=AsyncMock(return_value=(context.model_dump_json(), "e" * 64, reference)),
    )
    recorded = AsyncMock(return_value="action-id")
    verified = AsyncMock(return_value="runtime-id")
    session = SimpleNamespace(commit=AsyncMock())

    class Sessions:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(actions_routes, "_authority", AsyncMock(return_value=(issued, binding, execution)))
    monkeypatch.setattr(actions_routes, "_definition", AsyncMock(return_value={"environment": "dev", "mode": "release"}))
    monkeypatch.setattr(actions_routes, "_evidence_transport", AsyncMock(return_value="s3-oidc-v1"))
    monkeypatch.setattr(actions_routes, "_head_contains_pin", AsyncMock(side_effect=lambda provider, binding, *, pin, head, token: head == pin))
    monkeypatch.setattr(actions_routes, "_evidence_account", lambda: "123456789012")
    monkeypatch.setattr(actions_routes, "installation_token", AsyncMock(return_value="gateway-token"))
    monkeypatch.setattr(actions_routes, "get_session_factory", Sessions())
    monkeypatch.setattr(operations_workflow_evidence, "record_correlated_workflow", recorded)
    monkeypatch.setattr(operations_runtime_evidence, "verify_operations_runtime", verified)
    result = await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
    assert result["workflow_evidence_id"] == "action-id"
    kwargs = provider.s3_evidence.await_args.kwargs
    assert (kwargs["account"], kwargs["environment"], kwargs["kind"], kwargs["name"], kwargs["run_id"], kwargs["attempt"]) == (
        "123456789012",
        "dev",
        "context",
        "tools-platform-deploy.yml",
        42,
        1,
    )
    assert recorded.await_args.kwargs["artifact_id"] is None
    assert recorded.await_args.kwargs["reference"] == reference
    assert recorded.await_args.kwargs["digest"] == "e" * 64
    provider.s3_evidence.return_value = None
    assert await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider) is None


async def test_descendant_head_with_identical_workflow_binds_against_the_pin(monkeypatch):
    # #7662: main moves between the pin and the dispatch. A run at a descendant head
    # whose workflow file is byte-identical counts for the pinned definition.
    from unittest.mock import AsyncMock

    from src.orchestration import operations_runtime_evidence, operations_workflow_evidence
    from src.orchestration.deployment_workflow_provider import WorkflowContext

    binding = SimpleNamespace(repo="owner/repo", org_id="org", installation_id=12, provider_repository_id=789)
    issued = SimpleNamespace(node_id="node-1", workflow_allowlist={"tools-platform-deploy.yml"}, plan_version=1)
    pin, head = "a" * 40, "d" * 40
    execution = {
        "invocation_id": {"S": "orch:run-2"},
        "persona": {"S": "operations"},
        "source_revision": {"S": pin},
        "definition_revision": {"S": pin},
        "default_branch": {"S": "main"},
    }
    run = {
        "id": 43,
        "run_attempt": 1,
        "path": ".github/workflows/tools-platform-deploy.yml",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "display_title": "ADP deployment orch:run-2",
        "head_sha": head,
        "head_branch": "main",
        "repository": {"id": 789},
    }
    context = WorkflowContext(
        schema_version=1,
        repository_id=789,
        run_id=43,
        run_attempt=1,
        workflow_path=run["path"],
        workflow_revision=head,
        source_revision=pin,
        account_id="123456789012",
        region="us-east-1",
        resource_kind="agentcore-gateway",
        resource_id="arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/g",
        inputs={"environment": "dev", "mode": "release", "manual_source_revision": ""},
        correlation="orch:run-2",
    )
    provider = SimpleNamespace(s3_evidence=AsyncMock(return_value=(context.model_dump_json(), "e" * 64, "s3://bucket/key?versionId=v2")))
    recorded = AsyncMock(return_value="action-id")
    verified = AsyncMock(return_value="runtime-id")
    session = SimpleNamespace(commit=AsyncMock())

    class Sessions:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    definition = AsyncMock(return_value={"environment": "dev", "mode": "release", "manual_source_revision": ""})
    contains = AsyncMock(return_value=True)
    monkeypatch.setattr(actions_routes, "_authority", AsyncMock(return_value=(issued, binding, execution)))
    monkeypatch.setattr(actions_routes, "_definition", definition)
    monkeypatch.setattr(actions_routes, "_head_contains_pin", contains)
    monkeypatch.setattr(actions_routes, "_evidence_transport", AsyncMock(return_value="s3-oidc-v1"))
    monkeypatch.setattr(actions_routes, "_evidence_account", lambda: "123456789012")
    monkeypatch.setattr(actions_routes, "installation_token", AsyncMock(return_value="gateway-token"))
    monkeypatch.setattr(actions_routes, "get_session_factory", Sessions())
    monkeypatch.setattr(operations_workflow_evidence, "record_correlated_workflow", recorded)
    monkeypatch.setattr(operations_runtime_evidence, "verify_operations_runtime", verified)
    result = await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider)
    assert result["workflow_evidence_id"] == "action-id"
    assert contains.await_args.kwargs["pin"] == pin and contains.await_args.kwargs["head"] == head
    assert definition.await_args.kwargs["head"] == head
    assert provider.s3_evidence.await_args.kwargs["workflow_revision"] == head
    # A head that does not contain the pin is ignored silently, as before.
    contains.return_value = False
    assert await actions_routes._capture_correlated_run(None, None, execution=execution, binding=binding, raw=run, provider=provider) is None
    assert recorded.await_count == 1


async def test_head_contains_pin_walks_bounded_history_pages_instead_of_compare():
    """REST compare (every file patch) and even a 100-entry REST commits page exceed the
    provider read limit once main has moved past the pin; the ancestry check reads
    GraphQL history pages carrying only commit ids and stops at the pin."""
    pin, head = "a" * 40, "b" * 40
    pages = {
        None: ([f"{index:040x}" for index in range(100)], "cursor-1"),
        "cursor-1": (["c" * 40, pin, "d" * 40], None),
    }
    calls = []

    class Provider:
        async def request(self, binding, method, path, *, token=None, max_bytes=None, json=None, **_kwargs):
            calls.append((method, path, json["variables"], max_bytes))
            assert (method, path) == ("POST", "/graphql") and json["query"] == actions_routes.PIN_ANCESTRY_QUERY
            assert json["variables"]["head"] == head and json["variables"]["first"] == actions_routes.PIN_ANCESTRY_PAGE_SIZE
            oids, cursor = pages.get(json["variables"]["after"], ([], None))
            history = {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": [{"oid": oid} for oid in oids]}
            return SimpleNamespace(json=lambda: {"data": {"repository": {"object": {"history": history}}}})

    binding = SimpleNamespace(repo="o/r")
    assert await actions_routes._head_contains_pin(Provider(), binding, pin=pin, head=head, token="t") is True
    assert [call[2]["after"] for call in calls] == [None, "cursor-1"]
    assert all(call[3] == 512 * 1024 for call in calls)
    # A pin absent from the final page is not an ancestor; no further pages are read.
    calls.clear()
    pages["cursor-1"] = (["c" * 40], None)
    assert await actions_routes._head_contains_pin(Provider(), binding, pin=pin, head=head, token="t") is False
    assert [call[2]["after"] for call in calls] == [None, "cursor-1"]
    # The walk is bounded: a pin deeper than PIN_ANCESTRY_MAX_PAGES pages is refused.
    calls.clear()
    pages.clear()
    cursor = None
    for page in range(actions_routes.PIN_ANCESTRY_MAX_PAGES + 2):
        pages[cursor] = ([f"{page:02x}{index:038x}" for index in range(100)], f"cursor-{page + 1}")
        cursor = f"cursor-{page + 1}"
    assert await actions_routes._head_contains_pin(Provider(), binding, pin=pin, head=head, token="t") is False
    assert len(calls) == actions_routes.PIN_ANCESTRY_MAX_PAGES
    # GraphQL errors fail closed; identity and malformed heads never touch the provider.
    calls.clear()

    class Erroring(Provider):
        async def request(self, *args, **kwargs):
            return SimpleNamespace(json=lambda: {"errors": [{"message": "nope"}]})

    assert await actions_routes._head_contains_pin(Erroring(), binding, pin=pin, head=head, token="t") is False
    assert await actions_routes._head_contains_pin(Provider(), binding, pin=pin, head=pin, token="t") is True
    assert await actions_routes._head_contains_pin(Provider(), binding, pin=pin, head="main", token="t") is False
    assert calls == []
