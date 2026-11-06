import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.shared.middleware.proxy_body_limit import ProxyBodyLimitMiddleware


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions", "/bedrock/invoke", "/openai/v1/responses"])
async def test_streamed_body_bound_and_normal_payload_preservation(path):
    app = FastAPI()
    called = []

    @app.post(path)
    async def echo(request: Request):
        body = await request.body()
        called.append(body)
        return {"body": body.decode()}

    app.add_middleware(ProxyBodyLimitMiddleware, max_bytes=8)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.post(path, content="12345678")).json() == {"body": "12345678"}

        async def chunks():
            yield b"12345"
            yield b"67890"

        assert (await client.post(path, content=chunks(), headers={"content-length": "1"})).status_code == 413
    assert called == [b"12345678"]
