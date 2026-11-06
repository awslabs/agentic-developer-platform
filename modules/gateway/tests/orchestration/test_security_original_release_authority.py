"""The original release fixture reaches actual export authority and its fences."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy import select

from src.orchestration import security_evidence_release
from src.orchestration.l5_observation_export import authorized_read
from src.orchestration.models import OrchestrationEnvironmentLease, OrchestrationWorkClaim
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.run_reports import OrchestrationRunReport
from src.shared.models.vault import UserCredential
from src.shared.services.credential_resolver import CredentialNotFoundError
from tests.orchestration.security_original_release_fixture import CONNECTION, build_original_security_release, store  # noqa: F401


@pytest.mark.parametrize("change", ["claim", "lease", "provider", "credential", "protected_assignment", "pins"])
async def test_original_release_reads_and_refuses_changed_authority(db_session, db_session_factory, monkeypatch, store, change):  # noqa: F811
    c = await build_original_security_release(db_session, db_session_factory, monkeypatch, store=store)
    try:

        async def reader(db, action):
            assert action.id == c.action.id
            return action.detail["release_job"]

        assert await authorized_read(c.service, c.job, c.claims, reader) == {"run_id": "72", "run_attempt": "1"}
        assert c.assume.call_count >= 3
        async with db_session_factory() as db:
            if change == "claim":
                claim = await db.get(OrchestrationWorkClaim, c.claim.id)
                claim.generation += 1
            elif change == "lease":
                lease = await db.scalar(
                    select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.owner_action_id == f"delivery:{c.execution.id}")
                )
                lease.owner_generation += 1
            elif change == "credential":
                await db.delete(await db.get(UserCredential, CONNECTION))
            elif change == "pins":
                report = await db.get(OrchestrationRunReport, c.job.correlation)
                report.dispatch_metadata = {**report.dispatch_metadata, "orchestration": {}}
            elif change == "provider":
                c.provider_run["triggering_actor"] = {"id": 790}
            else:
                row = c.writer.store._read(f"TENANT#{c.flow.org_id}", f"EXEC#{c.job.correlation}")
                row["repo"] = {"S": "other/repository"}
                c.writer.store.client.put_item(TableName=c.writer.store.table, Item=row)
            await db.commit()
        with pytest.raises((HTTPException, CycleBlockedError, CredentialNotFoundError)):
            await authorized_read(c.service, c.job, c.claims, reader)
        if change in {"credential", "provider"}:
            monkeypatch.setattr(security_evidence_release, "github_claims", AsyncMock(return_value=c.claims))

            async def operation(guard, body, claims):
                return await authorized_read(guard, body.job, claims, reader)

            request = Request({"type": "http", "headers": [(b"authorization", b"Bearer offline-test-oidc")]})
            response = Response()
            with pytest.raises(HTTPException) as error:
                await security_evidence_release.call(request, response, c.service, SimpleNamespace(job=c.job), operation)
            assert error.value.status_code == 403
            code = "security_evidence_connection_unavailable" if change == "credential" else "security_evidence_release_job_refused"
            assert error.value.detail == {"code": code, "next_action": security_evidence_release.NEXT_ACTION[code]}
            assert response.headers["Cache-Control"] == "no-store"
    finally:
        await c.close()


@pytest.mark.parametrize(
    "failure", [RuntimeError("unexpected bug"), HTTPException(502, "provider unavailable"), HTTPException(403, "another denial")]
)
async def test_release_errors_do_not_mask_unexpected_failures(monkeypatch, failure):
    monkeypatch.setattr(security_evidence_release, "github_claims", AsyncMock(return_value={}))
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer offline-test-oidc")]})
    operation = AsyncMock(side_effect=failure)
    with pytest.raises(type(failure)) as error:
        await security_evidence_release.call(request, Response(), object(), object(), operation)
    assert error.value is failure
