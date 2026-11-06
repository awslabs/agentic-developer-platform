"""Real botocore send hooks commit evidence before transport and preserve ambiguity."""

import asyncio
import json
from types import SimpleNamespace

import boto3
from botocore.awsrequest import AWSResponse
from botocore.config import Config
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.well_architected.transport_observations import StartObserver
from l5_services.models.wa_periodic import WATransportObservation


@pytest.fixture
async def transport_store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'transport.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(WATransportObservation.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield sessions
    await engine.dispose()


async def rows(sessions):
    async with sessions() as session:
        return (await session.scalars(select(WATransportObservation).order_by(WATransportObservation.observed_at))).all()


def observer(sessions):
    run = SimpleNamespace(tenant_id="tenant-example", configuration_id="config-example", configuration_revision=1,
                          run_id="run-example", manifest={"archive": {"sha256": "a" * 64}})
    prepared = SimpleNamespace(request_hash="b" * 64, profile_arn="arn:aws:wellarchitected:us-east-1:123456789012:agent-profile/example",
                               name="adp-wa-example")
    return StartObserver(sessions, run, SimpleNamespace(attempt_id="attempt-example"), prepared)


@pytest.mark.parametrize("response_lost", [False, True])
async def test_actual_sdk_transport_cannot_run_before_committed_send_record(transport_store, response_lost):
    selected = observer(transport_store)
    client = boto3.client("wellarchitected", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing",
                          config=Config(retries={"total_max_attempts": 1}))
    selected.install(client)
    sends = []

    def send(request):
        committed = asyncio.run_coroutine_threadsafe(rows(transport_store), selected.loop).result(5)
        assert [row.phase for row in committed] == ["before_send"]
        sends.append(request.url)
        if response_lost:
            raise ConnectionError("simulated lost response")
        body = json.dumps({"id": "native-generation", "profileArn": selected.identity["profile_arn"], "name": "adp-wa-example"}).encode()
        return AWSResponse(request.url, 200, {"x-amzn-requestid": "native-request", "content-type": "application/json"},
                           SimpleNamespace(stream=lambda: iter([body])))

    client._endpoint.http_session.send = send
    call = asyncio.to_thread(client.start_agent_recommendation_generation, profileArn=selected.identity["profile_arn"],
                             types=["ARCHITECTURE"], name="adp-wa-example", scope={"pillars": ["SECURITY"]})
    if response_lost:
        with pytest.raises(ConnectionError):
            await call
    else:
        assert (await call)["id"] == "native-generation"
    observed = await rows(transport_store)
    assert len(sends) == 1
    assert [row.phase for row in observed] == (["before_send"] if response_lost else ["before_send", "after_call"])
    assert all(row.facts["native_effect_count"] is None for row in observed)
    if not response_lost:
        assert observed[1].facts["provider_request_id"] == "native-request"
        assert observed[1].facts["generation_id"] == "native-generation"


async def test_failed_journal_and_second_transmission_fail_closed(transport_store):
    selected = observer(transport_store)
    await asyncio.to_thread(selected.before_send, None)
    with pytest.raises(RuntimeError, match="second"):
        await asyncio.to_thread(selected.before_send, None)
    assert len(await rows(transport_store)) == 1
    broken = observer(transport_store)

    async def fail(*args):
        raise RuntimeError("journal unavailable")

    broken.append = fail
    with pytest.raises(RuntimeError, match="unavailable"):
        await asyncio.to_thread(broken.before_send, None)
    assert len(await rows(transport_store)) == 1
