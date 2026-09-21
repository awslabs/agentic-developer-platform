"""R1 upload adapter for the same authenticated shared-worker report assignment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

import boto3
from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.agentauth.artifact_keys import artifact_prefix
from src.agentauth.routes import require_agent_transport
from src.shared.database import get_session_factory

from .review_ingest import ingest_review_result

router = APIRouter(prefix="/internal/v1/agent/report", dependencies=[Depends(require_agent_transport)])


class SharedReviewUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Keep exact submitted bytes: the worker and merge observer verify the same
    # digest, independent of JSON key ordering or pretty-printing.
    content: str = Field(min_length=1, max_length=256 * 1024)


def shared_review_storage():
    return boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(connect_timeout=3, read_timeout=15, retries={"total_max_attempts": 1}),
    )


async def record_shared_review(session, *, credential, content, storage):
    from src.agentauth.artifact_service import _verify_stored_artifact

    from .run_reports import authenticate_run_report
    from .shared_cycle import validate_current_report_assignment

    row = await authenticate_run_report(session, credential, lock=False)
    await validate_current_report_assignment(session, row)
    if row.persona not in {"reviewer", "agent-codex-reviewer"} or not row.dispatch_metadata.get("review_expect"):
        raise HTTPException(404, "not found")
    expected = row.dispatch_metadata["review_expect"]
    # The accepted assignment, not the document, names author, scope and reviewer.
    if expected.get("author_run_id") == row.run_id:
        raise HTTPException(404, "not found")
    data = content.encode("utf-8")
    if len(data) > 256 * 1024:
        raise HTTPException(413, "review result too large")
    try:
        document = json.loads(data)
    except ValueError:
        raise HTTPException(422, "review result is not JSON") from None
    if not isinstance(document, dict):
        raise HTTPException(422, "review result is not an object")
    bucket = os.environ.get("AGENT_RUN_LOGS_BUCKET")
    if not bucket:
        raise HTTPException(503, "artifact storage unavailable")
    digest = hashlib.sha256(data).hexdigest()
    record = SimpleNamespace(tenant_id=row.org_id, invocation_id=row.run_id, current_attempt=1)
    prefix = artifact_prefix(record)
    key = f"{prefix}review-result/{digest}.json"
    await asyncio.to_thread(storage.put_object, Bucket=bucket, Key=key, Body=data, ContentType="application/json")

    async def resolve_artifact_ref(key):
        return await asyncio.to_thread(_verify_stored_artifact, key, record=record, runtime=SimpleNamespace(env=None), storage=storage)

    outcome = await ingest_review_result(
        session,
        document=document,
        org_id=row.org_id,
        node_id=row.node_id,
        attempt=row.attempt,
        reviewer_run_id=row.run_id,
        installation_id=row.installation_id,
        own_artifact_prefix=prefix,
        stored_artifact_ref=f"s3://{bucket}/{key}#sha256={digest}",
        resolve_artifact_ref=resolve_artifact_ref,
    )
    # Credential revocation, attempt movement, or flow cancellation during provider
    # reads must abort the ledger transaction, even though immutable bytes remain.
    current = await authenticate_run_report(session, credential, lock=True)
    await validate_current_report_assignment(session, current)
    if current.run_id != row.run_id or current.dispatch_metadata != row.dispatch_metadata:
        raise HTTPException(404, "not found")
    receipt = {
        "contract_version": 1,
        "run_id": row.run_id,
        "attempt": row.attempt,
        "key": key,
        "uri": f"s3://{bucket}/{key}",
        "sha256": digest,
        "recorded": outcome.recorded,
        "refusal": outcome.refusal.value if outcome.refusal else None,
        "detail": outcome.detail,
    }
    if outcome.recorded:
        current.review_receipt = receipt
    return receipt


@router.post("/review-result")
async def upload_shared_review(body: SharedReviewUpload, request: Request, storage=Depends(shared_review_storage)):
    from .run_reports import RunReportError

    credential = request.headers.get("X-Adp-Report-Credential", "")
    try:
        async with get_session_factory()() as session:
            receipt = await record_shared_review(session, credential=credential, content=body.content, storage=storage)
            await session.commit()
        return receipt
    except RunReportError as error:
        raise HTTPException(503 if error.retryable else 404, "report unavailable" if error.retryable else "not found") from None
