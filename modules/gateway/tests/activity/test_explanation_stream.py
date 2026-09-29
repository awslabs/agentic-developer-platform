"""Real async stream delivery, authorization boundaries and cleanup."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.activity import explanation_stream as stream
from src.activity.control_service import ControlError, ControlService


def setup(monkeypatch, source, **changes):
    now = datetime.now(UTC)
    row = dict(
        event_id="run",
        arrived_at="now",
        status="in_progress",
        user_id="owner",
        tenant_id="tenant",
        control_address="10.42.0.2",
        control_port=8770,
        control_generation=1,
        control_token="secret",
        control_token_expires_at=(now + timedelta(minutes=5)).isoformat(),
    )
    row.update(changes)
    table = MagicMock()
    table.query.return_value = {"Items": [row]}

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            async for value in source():
                yield value

    def handler(request):
        assert request.headers["authorization"] == "Bearer secret"
        assert request.headers["x-adp-control-generation"] == "1"
        assert str(request.url) == "http://10.42.0.2:8770/agent/events"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    control = ControlService(
        table=table,
        http_client=client,
        authority_store=object(),
        env={
            "FEATURE_AGENT_EXPLANATIONS_ENABLED": "true",
            "FEATURE_AGENT_CONTROL_ENABLED": "false",
            "AGENT_CONTROL_CLUSTER_POD_CIDRS": "10.42.0.0/16",
            "AGENT_CONTROL_PORT": "8770",
        },
    )
    monkeypatch.setattr(stream, "require_canonical_protected_human_owner", AsyncMock())
    session = SimpleNamespace(user_id="owner", tenant_id="tenant", expires_at=now + timedelta(minutes=5))
    return control, session, client


def event(sequence, **changes):
    value = dict(
        version=1,
        invocation_id="run",
        generation=1,
        sequence=sequence,
        timestamp=datetime.now(UTC).isoformat(),
        kind="explanation",
        payload={"text": f"marker {sequence}"},
    )
    value.update(changes)
    return stream.frame("explanation", value, f"run:1:{sequence}")


@pytest.mark.asyncio
async def test_delivery_before_completion_and_cleanup(monkeypatch):
    release = asyncio.Event()

    async def source():
        yield event(1)
        yield event(2)
        await release.wait()

    control, session, client = setup(monkeypatch, source)
    response = await stream.open_explanation_stream(control, "run", session=session, reauthorize=AsyncMock())
    iterator = response.body_iterator
    assert b"marker 1" in await asyncio.wait_for(anext(iterator), 1)
    assert b"marker 2" in await asyncio.wait_for(anext(iterator), 1)
    assert not release.is_set()
    await iterator.aclose()
    await client.aclose()
    assert not stream._connections


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"tenant_id": "other"}, {"user_id": "other"}, {"control_address": "169.254.169.254"}])
async def test_foreign_owner_or_destination_refused(monkeypatch, change):
    async def source():
        yield event(1)

    control, session, client = setup(monkeypatch, source, **change)
    with pytest.raises(ControlError):
        await stream.open_explanation_stream(control, "run", session=session, reauthorize=AsyncMock())
    await client.aclose()
    assert not stream._connections


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw", [event(1, invocation_id="other"), event(1, generation=2), b"x" * 17000], ids=["foreign-run", "wrong-generation", "oversized"]
)
async def test_invalid_or_oversized_event_refused(monkeypatch, raw):
    async def source():
        yield raw

    control, session, client = setup(monkeypatch, source)
    response = await stream.open_explanation_stream(control, "run", session=session, reauthorize=AsyncMock())
    body = b"".join([value async for value in response.body_iterator])
    assert b"event: unavailable" in body and b"marker" not in body and b"secret" not in body
    await client.aclose()
    assert not stream._connections


@pytest.mark.asyncio
async def test_revocation_closes_idle_feed(monkeypatch):
    async def source():
        yield event(1)
        await asyncio.sleep(60)

    control, session, client = setup(monkeypatch, source)
    reauth = AsyncMock()
    response = await stream.open_explanation_stream(control, "run", session=session, reauthorize=reauth)
    iterator = response.body_iterator
    assert b"marker" in await anext(iterator)
    reauth.side_effect = ControlError(404, "membership revoked")
    assert b"unavailable" in await asyncio.wait_for(anext(iterator), 3)
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    await client.aclose()
    assert not stream._connections


@pytest.mark.asyncio
async def test_read_flag_is_independent(monkeypatch):
    async def source():
        yield event(1)

    control, session, client = setup(monkeypatch, source)
    control._env["FEATURE_AGENT_CONTROL_ENABLED"] = "true"
    control._env["FEATURE_AGENT_EXPLANATIONS_ENABLED"] = "false"
    with pytest.raises(ControlError) as error:
        await stream.open_explanation_stream(control, "run", session=session, reauthorize=AsyncMock())
    assert error.value.status_code == 503
    await client.aclose()


@pytest.mark.asyncio
async def test_disconnected_response_headers_release_upstream(monkeypatch):
    async def source():
        yield event(1)

    control, session, client = setup(monkeypatch, source)
    response = await stream.open_explanation_stream(control, "run", session=session, reauthorize=AsyncMock())

    async def disconnected(message):
        raise OSError("browser disconnected before headers")

    with pytest.raises(OSError):
        await response.stream_response(disconnected)
    assert not stream._connections
    await client.aclose()


@pytest.mark.asyncio
async def test_connection_limit_and_terminal_row(monkeypatch):
    async def source():
        yield event(1)
        await asyncio.sleep(60)

    control, session, client = setup(monkeypatch, source)
    responses = [await stream.open_explanation_stream(control, "run", session=session, reauthorize=AsyncMock()) for _ in range(4)]
    with pytest.raises(ControlError) as error:
        await stream.open_explanation_stream(control, "run", session=session, reauthorize=AsyncMock())
    assert error.value.status_code == 429
    iterator = responses[0].body_iterator
    await anext(iterator)
    control._table.query.return_value["Items"][0]["status"] = "aborted"
    assert b"finished" in await asyncio.wait_for(anext(iterator), 3)
    await iterator.aclose()
    for response in responses:
        await response.cleanup()
    assert not stream._connections
    await client.aclose()


@pytest.mark.asyncio
async def test_stalled_browser_send_times_out_and_releases(monkeypatch):
    async def source():
        yield event(1)

    control, session, client = setup(monkeypatch, source)
    response = await stream.open_explanation_stream(control, "run", session=session, reauthorize=AsyncMock())

    async def stalled(message):
        await asyncio.sleep(60)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(response.stream_response(stalled), 6)
    assert not stream._connections
    await client.aclose()


@pytest.mark.asyncio
async def test_shared_progress_metadata_is_forwarded_without_extra_fields(monkeypatch):
    progress = dict(id="tool-1", category="tool", state="running", started_at=datetime.now(UTC).isoformat(), raw_output="PRIVATE")

    async def source():
        yield event(1, payload={"text": "Running tests", "progress": progress})

    control, session, client = setup(monkeypatch, source)
    response = await stream.open_explanation_stream(control, "run", session=session, reauthorize=AsyncMock())
    chunks = b"".join([chunk async for chunk in response.body_iterator])
    assert b'"id": "tool-1"' in chunks
    assert b'"started_at"' in chunks
    assert b"PRIVATE" not in chunks
    await client.aclose()
