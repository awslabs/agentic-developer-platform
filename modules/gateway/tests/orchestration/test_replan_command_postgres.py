"""Overlapping signed-command ticks commission one durable author, even on redelivery."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from src.orchestration.command_attribution import SIGNED_PAYLOAD_ATTR
from src.orchestration.engine_commands import flush_engine_commands, run_engine_command_pass
from src.orchestration.models import DecisionKind, OrchestrationAmendmentRequest, OrchestrationDecision

from .signed_command_rows import signed_row, signing_keyring  # noqa: F401
from .test_authoring_recovery import CONFIG, QUEUE, MarkerTable, replan_row, seed
from .test_pending_amendments_postgres import factory as _factory_fixture
from .test_pending_amendments_postgres import pg_server, pg_url  # noqa: F401
from .test_replan_authoring_dispatch import REPO, FakeSQS

factory = _factory_fixture


@pytest.mark.usefixtures("signing_keyring")
@pytest.mark.parametrize("new_marker", [False, True])
async def test_overlapping_signed_delivery_creates_one_author(factory, monkeypatch, new_marker):
    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", REPO)
    monkeypatch.setenv("BG_ORCH_DISPATCH_QUEUE_URL", QUEUE)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: factory)
    sqs = FakeSQS()
    monkeypatch.setattr("src.orchestration.authoring_dispatch._get_sqs_client", lambda _region: sqs)
    monkeypatch.setattr("src.orchestration.engine_commands._post_ack", AsyncMock())
    async with factory() as session:
        flow_id = await seed(session)

    original = replan_row()
    payload = dict(json.loads(original[SIGNED_PAYLOAD_ATTR]))
    if new_marker:
        # A webhook redelivery may get a fresh internal marker, but retains the
        # provider delivery identity. Both signatures are actually verified.
        payload["event_id"] = "redelivered-marker"
    first_table = MarkerTable([original])
    second_table = MarkerTable([signed_row(payload)])

    async with factory() as first, factory() as second, factory() as observer:
        first_report = await run_engine_command_pass(first, CONFIG, table=first_table)
        assert len(first_report.pending_authoring) == 1
        pid = await second.scalar(text("SELECT pg_backend_pid()"))
        repeated = asyncio.create_task(run_engine_command_pass(second, CONFIG, table=second_table))
        try:
            # Prove the second SQL transaction is waiting on the first one's
            # row lock, rather than merely scheduling a coroutine before commit.
            async with asyncio.timeout(5):
                while not await observer.scalar(text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}):
                    assert not repeated.done(), "duplicate pass did not wait on the flow lock"
                    await asyncio.sleep(0.01)
            await first.commit()
            second_report = await asyncio.wait_for(repeated, timeout=10)
            await second.commit()
        finally:
            if not repeated.done():
                repeated.cancel()
                await asyncio.gather(repeated, return_exceptions=True)

    assert len(second_report.pending_authoring) == 1
    a, b = first_report.pending_authoring[0], second_report.pending_authoring[0]
    assert (a.request_id, a.author_run_id) == (b.request_id, b.author_run_id)
    async with factory() as session:
        requests = list(await session.scalars(select(OrchestrationAmendmentRequest)))
        decisions = list(
            await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.REPLAN_REQUESTED.value))
        )
        assert len(requests) == len(decisions) == 1
        assert requests[0].flow_id == decisions[0].flow_id == flow_id
        assert requests[0].replan_decision_id == decisions[0].id
        assert requests[0].author_run_id == a.author_run_id

    await flush_engine_commands(first_report, CONFIG, table=first_table)
    await flush_engine_commands(second_report, CONFIG, table=second_table)
    assert sqs.calls
    assert len({call["MessageDeduplicationId"] for call in sqs.calls}) == 1
    assert {json.loads(call["MessageBody"])["message_id"] for call in sqs.calls} == {a.author_run_id}

    async with factory() as session:
        replay = await run_engine_command_pass(session, CONFIG, table=MarkerTable([original]))
        await session.commit()
        assert not replay.pending_authoring
