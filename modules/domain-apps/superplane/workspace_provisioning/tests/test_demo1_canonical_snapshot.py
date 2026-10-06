"""Canonical loader observations against disposable PostgreSQL, without mutation locks."""

import json
from uuid import uuid4

import pytest
from superplane_bootstrap.errors import BootstrapRefused

from superplane_acceptance._demo1_cleanup_probe import canonical_inventory

from .test_retirement_inventory import load


def observe(runtime, *, org=None):
    store = runtime.store.store

    async def read():
        async with store._connection.transaction(
            isolation="repeatable_read", readonly=True
        ):
            inventory = await canonical_inventory(
                store._connection,
                org or runtime.target.org_id,
                runtime.target.workspace_id,
            )
            assert (
                await store._connection.fetchval("SHOW transaction_read_only") == "on"
            )
            assert (
                await store._connection.fetchval(
                    "SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'"
                )
                == 0
            )
            return inventory

    return store._loop.run(read())


def test_readonly_snapshot_matches_maintained_locked_inventory(runtime):
    assert runtime.run().ready
    assert observe(runtime) == load(runtime)


@pytest.mark.parametrize(
    "change", ["foreign", "sharing", "peer", "pending", "missing", "metadata"]
)
def test_canonical_snapshot_refuses_foreign_shared_or_incomplete_ownership(
    runtime, change
):
    assert runtime.run().ready
    store = runtime.store.store
    if change == "foreign":
        with pytest.raises(BootstrapRefused):
            observe(runtime, org=str(uuid4()))
        return
    with store.transaction():
        if change == "sharing":
            store.execute("UPDATE clusters SET sharing_enabled=true", {})
        elif change == "peer":
            peer, membership = str(uuid4()), str(uuid4())
            store.execute(
                "INSERT INTO workspaces(id,org_id,name,isolation_mode,status,is_default) "
                "SELECT CAST(:peer AS uuid),org_id,'peer','namespace','Provisioning',false "
                "FROM workspaces WHERE id=CAST(:owner AS uuid)",
                {"peer": peer, "owner": runtime.target.workspace_id},
            )
            store.execute(
                "INSERT INTO cluster_memberships(id,org_id,workspace_id,cluster_id,generation,namespace,state) "
                "SELECT CAST(:membership AS uuid),org_id,CAST(:peer AS uuid),id,:generation,'peer','reserved' "
                "FROM clusters WHERE workspace_id=CAST(:owner AS uuid)",
                {
                    "membership": membership,
                    "peer": peer,
                    "generation": "a" * 64,
                    "owner": runtime.target.workspace_id,
                },
            )
        elif change == "metadata":
            store.execute("UPDATE clusters SET actual_state_json='{}'", {})
        else:
            rows = store.execute(
                "SELECT generation, progress_json FROM workspace_bootstrap_authority",
                {},
            )
            progress = json.loads(rows[0]["progress_json"])
            if change == "pending":
                progress["phase"] = "revoking"
            else:
                progress.pop("prerequisite_inventory")
            store.execute(
                "UPDATE workspace_bootstrap_authority SET progress_json=:progress WHERE generation=:generation",
                {"progress": json.dumps(progress), "generation": rows[0]["generation"]},
            )
    with pytest.raises(BootstrapRefused):
        observe(runtime)


def test_snapshot_is_consistent_but_cannot_authorize_after_a_concurrent_change(
    runtime, database
):
    assert runtime.run().ready
    store, other = runtime.store.store, database()

    async def read():
        async with store._connection.transaction(
            isolation="repeatable_read", readonly=True
        ):
            first = await canonical_inventory(
                store._connection, runtime.target.org_id, runtime.target.workspace_id
            )
            await other._connection.execute("UPDATE clusters SET sharing_enabled=true")
            second = await canonical_inventory(
                store._connection, runtime.target.org_id, runtime.target.workspace_id
            )
            assert first == second

    store._loop.run(read())
    with pytest.raises(BootstrapRefused):
        observe(runtime)


@pytest.mark.parametrize(
    "readonly,isolation", [(False, "repeatable_read"), (True, "read_committed")]
)
def test_canonical_observation_requires_its_readonly_repeatable_snapshot(
    runtime, readonly, isolation
):
    store = runtime.store.store

    async def read():
        with pytest.raises(ValueError, match="observation refused"):
            async with store._connection.transaction(
                isolation=isolation, readonly=readonly
            ):
                await canonical_inventory(
                    store._connection,
                    runtime.target.org_id,
                    runtime.target.workspace_id,
                )

    store._loop.run(read())
