"""A Dev Box host reaches the OpenAI Responses passthrough only with the model-route proof (Codex on GPT models)."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from src.crew.model_token import mint_model_token
from src.proxy.mantle_service import MantleResponse
from src.proxy.model_resolver import ModelResolver
from src.proxy.routes import get_mantle_service, get_model_resolver, get_token_context, router, set_agent_run_id_from_header
from tests.crew.test_host_tools import INSTANCE, active, host_context, registry  # noqa: F401  (fixtures)
from tests.proxy.test_mantle_route import StubMantleService


@pytest.fixture
def api(active, monkeypatch):  # noqa: F811
    monkeypatch.setattr("src.proxy.routes._mantle_model_configured", lambda model: model.startswith("openai."))
    stub = StubMantleService(MantleResponse(status_code=200, content=b'{"output_text":"pong"}', usage={"input_tokens": 1, "output_tokens": 1}))
    app = FastAPI()
    app.include_router(router)
    identity = SimpleNamespace(context=host_context())

    async def authenticated(request: Request):
        request.state.token_context = identity.context
        return identity.context

    async def run_header(request: Request):
        return request.headers.get("x-agent-runid")

    app.dependency_overrides[get_token_context] = authenticated
    app.dependency_overrides[set_agent_run_id_from_header] = run_header
    app.dependency_overrides[get_mantle_service] = lambda: stub
    app.dependency_overrides[get_model_resolver] = lambda: ModelResolver(allowed_models_config={"tenant-a:": ["openai.*"], "default": ["openai.*"]})
    return SimpleNamespace(client=AsyncClient(transport=ASGITransport(app=app), base_url="http://host"), stub=stub, identity=identity)


async def post(api, active, token):  # noqa: F811
    headers = {"X-Agent-RunId": active.run_id, "Authorization": "fictional-sigv4", "Content-Type": "application/json"}
    if token is not None:
        headers["X-Adp-Crew-Model-Token"] = token
    return await api.client.post("/openai/v1/responses", json={"model": "openai.gpt-6-astra", "input": "pong", "stream": False}, headers=headers)


async def test_current_host_with_its_model_token_is_passed_through_verbatim(api, active):  # noqa: F811
    token = await mint_model_token(active.owner, active.target, now=active.now)
    response = await post(api, active, token)
    assert response.status_code == 200, response.text
    assert response.content == b'{"output_text":"pong"}'
    assert api.stub.calls[0]["model"] == "openai.gpt-6-astra" and b'"openai.gpt-6-astra"' in api.stub.calls[0]["body"]


async def test_missing_or_foreign_token_and_fenced_host_never_reach_mantle(api, active):  # noqa: F811
    assert (await post(api, active, None)).status_code == 401
    assert (await post(api, active, "eyJ0ZXN0IjoidG9rZW4ifQ.c2lnbmF0dXJl")).status_code == 401
    token = await mint_model_token(active.owner, active.target, now=active.now)
    api.identity.context = host_context("i-0fedcba9876543210")
    assert (await post(api, active, token)).status_code == 403
    api.identity.context = host_context()
    active.resolver.fenced = True
    assert (await post(api, active, token)).status_code in (403, 409)
    assert api.stub.calls == []
