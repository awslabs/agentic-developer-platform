"""The maintained deployment workflow fits observation bounds, not dispatch authority."""

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.agentauth.actions_routes import _definition, _evidence_transport
from src.agentauth.github_operation_service import OperationRefusedError
from src.orchestration.deployment_manifest import WorkflowRef
from src.orchestration.deployment_workflow_provider import WorkflowProvider
from src.orchestration.review_cycle import CycleBlockedError

REVISION = "a" * 40
PATH = ".github/workflows/gateway-deploy.yml"


@pytest.fixture
async def gateway_workflow(monkeypatch):
    raw = (Path(__file__).resolve().parents[4] / PATH).read_bytes()
    binding = SimpleNamespace(repo="owner/repo", provider_repository_id=17)
    state = SimpleNamespace(raw=raw, overrides={}, extra={}, calls=[])

    def respond(request):
        state.calls.append(request.method)
        if request.url.path == "/repos/owner/repo":
            return httpx.Response(200, json={"id": 17, "default_branch": "main"})
        assert "/contents/" in request.url.path
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "size": len(state.raw),
                "content": base64.encodebytes(state.raw).decode(),
                "sha": hashlib.sha1(b"blob " + str(len(state.raw)).encode() + b"\0" + state.raw).hexdigest(),
                **state.overrides,
                **state.extra,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = WorkflowProvider(client=client)

        async def token(*args, **kwargs):
            return "probe"

        monkeypatch.setattr(provider, "token", token)
        yield state, provider, binding


async def test_maintained_gateway_workflow_observed_by_both_readers(gateway_workflow):
    state, provider, binding = gateway_workflow
    defaults = await _definition(
        provider, binding, workflow="gateway-deploy.yml", revision=REVISION, branch="main", token="probe", for_dispatch=False, head=REVISION
    )
    assert "adp_approved_revision" in defaults
    assert await _evidence_transport(provider, binding, workflow="gateway-deploy.yml", revision=REVISION, token="probe") == "s3-oidc-v1"
    workflow = WorkflowRef(PATH, REVISION, {}, correlation_input="adp_correlation")
    definition = await provider.definition(binding, workflow, REVISION, for_dispatch=False)
    assert definition.evidence_transport == "s3-oidc-v1"
    assert await provider.workflow_content_matches(binding, workflow, REVISION, REVISION)
    with pytest.raises(OperationRefusedError, match="human_approval_input"):
        await _definition(provider, binding, workflow="gateway-deploy.yml", revision=REVISION, branch="main", token="probe")
    assert set(state.calls) == {"GET"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"size": 131073},
        {"size": -1},
        {"size": True},
        {"size": 10},
        {"content": "%%%"},
        {"content": "YWJj\u00a0"},
        {"content": None},
    ],
)
@pytest.mark.parametrize("reader", ["definition", "match", "transport"])
async def test_workflow_readers_refuse_invalid_contents(gateway_workflow, overrides, reader):
    state, provider, binding = gateway_workflow
    state.overrides = overrides
    workflow = WorkflowRef(PATH, REVISION, {}, correlation_input="adp_correlation")
    with pytest.raises((CycleBlockedError, OperationRefusedError)):
        if reader == "definition":
            await provider.definition(binding, workflow, REVISION, for_dispatch=False)
        elif reader == "match":
            await provider.workflow_content_matches(binding, workflow, REVISION, REVISION)
        else:
            await _evidence_transport(provider, binding, workflow="gateway-deploy.yml", revision=REVISION, token="probe")


async def test_workflow_envelope_remains_stream_bounded(gateway_workflow):
    state, provider, binding = gateway_workflow
    state.extra = {"unexpected": "x" * (256 * 1024)}
    with pytest.raises(CycleBlockedError, match="deployment_provider_response_limit"):
        await provider.definition(binding, WorkflowRef(PATH, REVISION, {}), REVISION, for_dispatch=False)
