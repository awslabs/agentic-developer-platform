"""Cleanup grants cannot be created from an open or exchanged allocation."""

from types import SimpleNamespace

import pytest

from workspace_provisioning.retirement_access_context import require_original_seal
from workspace_provisioning.runtime_config import LifecycleRefused

from .postgres_bridge import Harness, requires_harness_postgres

pytestmark = requires_harness_postgres


@pytest.fixture
def harness(tmp_path_factory, request):
    with Harness.started(tmp_path_factory, request.node.name) as instance:
        yield instance


def test_access_keeps_original_allocation_sealed_and_distinct(harness):
    source = SimpleNamespace(
        org_id="org-a",
        workspace_id="ws-a",
        admitted_request=lambda: SimpleNamespace(
            parameters={"allocation_id": "original-allocation"}
        ),
    )
    parameters = {"original_allocation_id": "original-allocation"}

    async def check():
        async with harness.connect() as connection:
            with pytest.raises(LifecycleRefused, match="sealed"):
                await require_original_seal(connection, source, parameters)
            with pytest.raises(LifecycleRefused, match="changed"):
                await require_original_seal(
                    connection, source, {"original_allocation_id": "control-allocation"}
                )
            await connection.execute(
                "INSERT INTO harness_allocation_seal "
                "(org_id,workspace_id,allocation_id,sealed_revision,operation_id,"
                "attempt_id,executor_id,fence_token) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                source.org_id,
                source.workspace_id,
                "original-allocation",
                "revision",
                "bootstrap",
                "attempt",
                "worker",
                1,
            )
            await require_original_seal(connection, source, parameters)
            await connection.execute(
                "UPDATE harness_allocation_seal SET allocation_id=$1 "
                "WHERE org_id=$2 AND workspace_id=$3",
                "control-allocation",
                source.org_id,
                source.workspace_id,
            )
            with pytest.raises(LifecycleRefused, match="sealed"):
                await require_original_seal(connection, source, parameters)

    harness.run(check())
