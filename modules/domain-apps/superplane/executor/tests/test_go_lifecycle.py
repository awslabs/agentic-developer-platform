"""The actual Go RPC worker drives the trusted AWS adapter and shared bookkeeping."""

import importlib.util
import json
import os
import tempfile
from pathlib import Path

from test_lifecycle_postgres import system as system


def go_transport():
    source = (
        Path(__file__).resolve().parents[2]
        / "src/superplane-controller/execution/integration/test_shared_transport.py"
    )
    spec = importlib.util.spec_from_file_location("superplane_go_transport", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.worker


async def test_real_go_worker_restart_completes_provider_lifecycle_and_cleanup(system):  # noqa: F811
    pool, admit, server, cloud, _, registry, _ = system
    worker = go_transport()
    binary = os.environ["SUPERPLANE_TEST_RPC_BINARY"]
    operation, token = await admit("provision")

    def token_file(value):
        return registry.token_dir / next(
            name for name, entry in registry.tokens.items() if entry[0] == value
        )

    with tempfile.TemporaryDirectory(prefix="sp-lifecycle-", dir="/tmp") as directory:
        socket = Path(directory) / "rpc.sock"
        async with server.serve(socket):
            async with worker(binary) as request:
                first = await request(
                    socket, token_file(token), operation.grant.lease, step="1"
                )
                assert not first["error"], first
                duplicate = await request(
                    socket, token_file(token), operation.grant.lease, step="1"
                )
                assert duplicate == first and cloud.launches == 1
            # A new isolated Go process reads the durable prefix and continues.
            async with worker(binary) as request:
                for step in ("2", "3", "4"):
                    result = await request(
                        socket, token_file(token), operation.grant.lease, step=step
                    )
                    assert not result["error"], result
            cleanup, token = await admit("teardown")
            async with worker(binary) as request:
                result = await request(
                    socket, token_file(token), cleanup.grant.lease, step="1"
                )
                assert not result["error"], result
    async with pool.acquire() as connection:
        report = json.loads(
            await connection.fetchval(
                "SELECT observation::text FROM controller_execution_accounting WHERE operation_id=$1",
                cleanup.grant.lease.operation_id,
            )
        )
        assert report["release_permitted"] is True and report["exposure"] == "none"
        assert (
            await connection.fetchval("SELECT state FROM controller_capacity")
            == "retired"
        )
        assert cloud.launches == 1
