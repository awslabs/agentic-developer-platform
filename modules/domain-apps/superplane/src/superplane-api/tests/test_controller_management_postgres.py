"""Two controller processes contend using the actual PostgreSQL lease store."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from superplane_contracts import Submitter

from app.models.observation import ObservationLease
from app.routers.controller_management import ReconcileRequest, reconcile
from tests.test_installation_postgres import (  # noqa: F401
    admin_membership,
    bootstrap_database,
    installation_postgres_url,
    isolated_database,
)


async def test_two_processes_zero_targets_restart_and_expired_lease(bootstrap_database):  # noqa: F811
    bootstrap, factory, config, _ = bootstrap_database
    config = {k: config[k] for k in ("org_id", "adp_org_id", "origin")}
    config["control_plane_only"] = True
    await bootstrap.bootstrap(config, "verified-test-token", membership_reader=admin_membership)
    org = uuid.UUID(config["org_id"])
    scope = f"controller_management/{org}"
    submitter = Submitter(submitter_id="management", workspaces=frozenset(), lease_scopes=frozenset({scope}))
    requests = [ReconcileRequest(org_id=org, instance_id=uuid.uuid4()) for _ in range(2)]

    async def call(body):
        async with factory() as db:
            try:
                return await reconcile(body, submitter, db)
            except HTTPException as error:
                return error.status_code

    results = await asyncio.gather(*(call(body) for body in requests))
    assert results.count(409) == 1
    first = next(result for result in results if isinstance(result, dict))
    assert first["targets"] == []
    async with factory() as db:
        lease = await db.get(ObservationLease, scope, with_for_update=True)
        assert lease.fence_token == first["fence_token"]
        lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    restarted = await call(ReconcileRequest(org_id=org, instance_id=uuid.uuid4()))
    assert restarted["targets"] == []
    assert restarted["fence_token"] > first["fence_token"]
    assert restarted["governed_provisioning"] is False
