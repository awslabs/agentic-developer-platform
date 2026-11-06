"""Late result reads retain current consent and cannot acquire execution permissions."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.run import Run
from l5_services.models.work import Work
from l5_services.security_recovery import request_recovery
from sqlalchemy import delete, select

from src.internal.l5_credential_routes import CredentialSessionRequest, verify_l5_work
from src.products.models import ProductSourceBinding
from src.shared.models.vault import CredentialServiceDelegation
from tests.products.test_security_capture_continuation import (
    admitted_run,
    capture_authority,  # noqa: F401
    durable_review,  # noqa: F401
    finish_capture,
    queued_capture,
)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "submit",
        "cancel",
        "cancelled",
        "revoked",
        "expired_grant",
        "stale_fence",
        "foreign_job",
        "expired_window",
        "parent_revoked",
        "source_revoked",
        "binding_generation",
    ],
)
async def test_security_late_read_requires_current_authority(durable_review, db_session, fault):  # noqa: F811
    fixture = durable_review
    template, backend = fixture.grant, fixture.backend
    preparation, _, deadline = await queued_capture(
        backend.submit(template.product_id, template.project_id, "security", fixture.run_input, "late-review"), fixture, "submit", "late-review"
    )
    await finish_capture(fixture, preparation)
    admitted = await admitted_run(fixture, "submit", "late-review", deadline)
    now = datetime.now(UTC)
    run = await db_session.get(Run, (template.tenant_id, admitted.run_id), populate_existing=True)
    grant = await db_session.get(CredentialServiceDelegation, run.admission["delegation_id"])
    work = await db_session.scalar(select(Work).where(Work.run_id == run.run_id))
    native = {
        "tenant_id": run.tenant_id,
        "project_id": run.project_id,
        "source_connection_id": run.admission["connection_ref"]["source_connection_id"],
        "record_type": "native_job",
        "record_id": "job-original",
        "record_revision": "review-original",
    }
    run.native_job_ref, run.state, run.provider_state = native, "expired", "running"
    run.deadline_at = now - timedelta(hours=2)
    run.monitoring = request_recovery(run, now)
    work.kind, work.lease_owner, work.lease_until, work.fence = "poll", "late-worker", now + timedelta(seconds=60), 1
    db_session.add(
        WorkerAttempt(
            tenant_id=run.tenant_id,
            attempt_id="provider-attempt",
            run_id=run.run_id,
            operation="submit",
            first_work_id=work.work_id,
            first_fence=1,
            input_hash="a" * 64,
            idempotency_token="b" * 64,
            started_at=now - timedelta(hours=3),
            status="accepted",
            native_job_ref=native,
        )
    )
    body = CredentialSessionRequest(
        delegation_id=grant.id,
        delegation_revision=grant.revision,
        operation_id=run.run_id,
        attempt_id=work.work_id,
        lease_owner=work.lease_owner,
        lease_fence=1,
        connection_ref={**run.admission["connection_ref"], "record_type": "credential"},
    )
    if fault in {"submit", "cancel"}:
        work.kind = fault
    elif fault == "cancelled":
        run.state = "cancelled"
    elif fault == "revoked":
        grant.revoked_at = now
    elif fault == "expired_grant":
        grant.expires_at = now - timedelta(seconds=1)
    elif fault == "stale_fence":
        work.fence = 2
    elif fault == "foreign_job":
        run.native_job_ref = {**native, "record_id": "different-job"}
    elif fault == "expired_window":
        run.monitoring = request_recovery(run, now - timedelta(minutes=16))
    elif fault == "parent_revoked":
        template.revoked_at = now
    elif fault == "source_revoked":
        await db_session.execute(
            delete(ProductSourceBinding).where(ProductSourceBinding.project_id == run.project_id, ProductSourceBinding.kind == "repository")
        )
    elif fault == "binding_generation":
        run.binding_generation += 1
    await db_session.commit()
    if fault:
        with pytest.raises(HTTPException) as denied:
            await verify_l5_work(db_session, grant, body)
        assert denied.value.status_code == 409
        return
    permit = await verify_l5_work(db_session, grant, body)
    policy = json.loads(permit.session_policy)
    assert len(policy["Statement"]) == 1
    statement = policy["Statement"][0]
    assert statement["Resource"].endswith("/space-one")
    assert set(statement["Action"]) == {
        "securityagent:BatchGetCodeReviewJobs",
        "securityagent:ListCodeReviewJobTasks",
        "securityagent:ListFindings",
        "securityagent:BatchGetFindings",
    }
    assert now < permit.expires_at <= now + timedelta(seconds=60)
    assert run.deadline_at.replace(tzinfo=UTC) < now
