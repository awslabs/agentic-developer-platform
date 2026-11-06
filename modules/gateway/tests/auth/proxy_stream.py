"""Adapt existing mocked upstream requests to the streaming proxy contract."""

import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace


@asynccontextmanager
async def stream_from_request(client, **kwargs):
    response = client.request(**kwargs)
    if inspect.isawaitable(response):
        response = await response

    async def body():
        yield response.text.encode("utf-8")

    yield SimpleNamespace(
        status_code=response.status_code,
        headers=response.headers,
        encoding="utf-8",
        aiter_raw=body,
    )
