"""Snapshot capture through the real paid source finalizer and retained proof."""

# ruff: noqa: F811
import asyncio
from types import SimpleNamespace

import pytest

from harness_jobs.identity import OperationRefused
from superplane_executor.cleanup_graph import header, steps
from superplane_executor.cleanup_snapshot import retained
from tests.test_batch_results_postgres import (
    output as output,
    batch_workload as batch_workload,
    batch_runtime as batch_runtime,
    workload as workload,
    lifecycle as lifecycle,
    ledger as ledger,
    installation_postgres_url as installation_postgres_url,
    pytestmark as pytestmark,
)
from tests.test_batch_deployment_postgres import stop


async def test_source_snapshot_commit_before_ack_reuses_original_after_fresh_attestation(
    output,
):
    worker = await output.runtime.publish(SimpleNamespace(**output.created))
    finalizer = worker.server._after_step

    async def die_after_snapshot(grant, result):
        assessment = await finalizer(grant, result)
        if assessment is not None:
            raise asyncio.CancelledError("source died after durable snapshot")

    worker.server._after_step = die_after_snapshot
    with pytest.raises(asyncio.CancelledError):
        await output.runtime.execute(worker)
    source, snapshot = await records(output)
    assert snapshot is not None and source["state"] == "running"
    async with output.c.connections.connect() as c:
        before = await c.fetchval(
            "SELECT count(*) FROM harness_provider_report WHERE operation_id=$1",
            source["operation_id"],
        )
    worker.server._after_step = finalizer
    await worker.server.dispatch(
        {
            "token": worker.token,
            "method": "execute_step",
            "arguments": {"step_id": worker.plan.steps[-1]["step_id"]},
        }
    )
    completed, reused = await records(output)
    assert completed["state"] == "succeeded" and reused == snapshot
    async with output.c.connections.connect() as c:
        assert (
            await c.fetchval(
                "SELECT count(*) FROM harness_provider_report WHERE operation_id=$1",
                source["operation_id"],
            )
            > before
        )
        assert await retained(c, completed, reused)
        assert (
            await c.fetchval("SELECT count(*) FROM controller_cleanup_snapshots") == 1
        )
    assert output.runtime.cloud.launches == 1


async def records(output):
    async with output.c.connections.connect() as c:
        source = await c.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1",
            output.created["operation_id"],
        )
        snapshot = await c.fetchrow(
            "SELECT * FROM controller_cleanup_snapshots WHERE source_operation_id=$1",
            source["operation_id"],
        )
        return source, snapshot


async def test_original_snapshot_survives_legitimate_cleanup_enumeration(output):
    runtime = output.runtime
    worker = await runtime.publish(SimpleNamespace(**output.created))
    assert (await records(output))[1] is None
    await runtime.execute(worker)
    source, snapshot = await records(output)
    assert snapshot is not None
    async with output.c.connections.connect() as c:
        document = await retained(c, source, snapshot)
    assert (
        len(document["compute"])
        == len(document["nodes"])
        == len(document["roots"])
        == 1
    )
    assert document["source_operation_id"] == output.created["operation_id"]
    graph = header(document)
    assert graph["snapshot_id"] == snapshot["snapshot_id"]
    assert '"step_id":"cordon:0"' in steps(graph, document["cluster_name"])
    stopped = await stop(output.c, output.created)
    await runtime.execute(await runtime.publish(SimpleNamespace(**stopped)))
    async with output.c.connections.connect() as c:
        # Cleanup has published its own enumeration/report. Original evidence is
        # still meaningful for identity, without becoming current release proof.
        assert await retained(c, source, snapshot) == document
        assert (
            await c.fetchval("SELECT count(*) FROM controller_cleanup_snapshots") == 1
        )
    assert (await records(output))[1] == snapshot


@pytest.mark.parametrize(
    "changed",
    ["body_sha256", "report_digest", "source_plan_digest", "enumeration_binding"],
)
async def test_changed_original_snapshot_or_proof_refuses(output, changed):
    await output.runtime.execute(
        await output.runtime.publish(SimpleNamespace(**output.created))
    )
    source, snapshot = await records(output)
    altered = dict(snapshot)
    altered[changed] = "f" * 64
    async with output.c.connections.connect() as c:
        with pytest.raises(OperationRefused):
            await retained(c, source, altered)


async def test_partial_paid_source_does_not_capture_complete_snapshot(output):
    worker = await output.runtime.publish(SimpleNamespace(**output.created))
    await worker.server.dispatch(
        {"token": worker.token, "method": "execute_step", "arguments": {"step_id": "1"}}
    )
    assert (await records(output))[1] is None
