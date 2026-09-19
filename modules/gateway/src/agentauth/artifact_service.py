"""Bounded own-run artifact writes. Workers receive no bucket credential (#5195)."""

import asyncio
import hashlib
import json
import os
import re

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.artifact_keys import artifact_prefix
from src.agentauth.review_upload import REVIEW_RESULT_KIND, ReviewUploadRefusedError, observe_review_upload
from src.agentauth.routes import AgentRuntime, get_agent_runtime, require_agent_transport
from src.agentauth.run_services import live_context
from src.agentauth.store import AuthorityStoreError

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_KINDS = {
    "transcript": ("AGENT_RUN_LOGS_BUCKET", "md", "text/markdown"),
    "spill": ("AGENT_RUN_LOGS_BUCKET", "txt", "text/plain"),
    "comment": ("AGENT_FALLBACK_BUCKET", "md", "text/markdown"),
    "git-changes": ("AGENT_FALLBACK_BUCKET", "tar.gz", "application/gzip"),
    "git-manifest": ("AGENT_FALLBACK_BUCKET", "md", "text/markdown"),
    # #5146. Stored like every other own-run artifact — the review result *is* one,
    # and the server-derived key is what later makes a reference to it verifiable.
    # Unlike the others it is additionally *observed*: see `_observe_review_result`.
    REVIEW_RESULT_KIND: ("AGENT_RUN_LOGS_BUCKET", "json", "application/json"),
}
router = APIRouter(prefix="/internal/v1/agent/self", tags=["agent-authority"], dependencies=[Depends(require_agent_transport)])


def artifact_storage(runtime: AgentRuntime = Depends(get_agent_runtime)):
    env = os.environ if runtime.env is None else runtime.env
    return boto3.client(
        "s3", region_name=env.get("AWS_REGION", "us-east-1"), config=Config(connect_timeout=3, read_timeout=15, retries={"total_max_attempts": 1})
    )


@router.post("/artifacts/{kind}")
async def upload_artifact(kind: str, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime), storage=Depends(artifact_storage)):
    if kind not in _KINDS or request.url.query or request.headers.get("content-encoding"):
        raise HTTPException(404, "not found")
    initial = await live_context(request, runtime)
    config = os.environ if runtime.env is None else runtime.env
    bucket_env, extension, content_type = _KINDS[kind]
    bucket = config.get(bucket_env, "")
    if not bucket:
        raise HTTPException(503, "artifact storage unavailable")
    try:
        async with asyncio.timeout(30):
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise HTTPException(413, "artifact too large")
                chunks.append(chunk)
            if not size:
                raise HTTPException(422, "artifact is empty")
            body = b"".join(chunks)
            digest = hashlib.sha256(body).hexdigest()
            current = await live_context(request, runtime)
            if initial[1:] != current[1:]:
                raise HTTPException(404, "not found")
            key = f"{artifact_prefix(current[2])}{kind}/{digest}.{extension}"
            # Same bytes converge on the same key; arbitrary metadata, ACLs,
            # bucket, encryption keys and destination headers are never forwarded.
            await run_in_threadpool(storage.put_object, Bucket=bucket, Key=key, Body=body, ContentType=content_type)
            receipt = {"key": key, "uri": f"s3://{bucket}/{key}", "sha256": digest}
            if kind == REVIEW_RESULT_KIND:
                # Stored first, then observed. The document survives a refusal: an
                # attempted-and-refused review is a fact an operator needs, and a
                # reviewer told "refused" with nothing retained cannot evidence it.
                receipt |= await _observe_review_result(request, runtime, current, body=body, receipt=receipt, storage=storage)
            final = await live_context(request, runtime)
            if current[1:] != final[1:]:
                raise HTTPException(404, "not found")
            return JSONResponse(receipt, headers={"Cache-Control": "no-store"})
    except (BotoCoreError, ClientError, TimeoutError):
        raise HTTPException(503, "artifact storage unavailable") from None


async def _observe_review_result(request: Request, runtime: AgentRuntime, context, *, body: bytes, receipt: dict, storage) -> dict:
    """Validate and record an uploaded review result (#5146).

    Only this kind reaches orchestration state, so the work lives in
    :mod:`src.agentauth.review_upload` and the session boundary is its own. What
    stays here is the transport's own rule: a caller who is not a dispatched
    reviewer gets the same 404 as every other authorization failure, because a
    caller able to distinguish them learns about runs it does not own.

    A *validation* refusal is the one thing reported specifically. It is reachable
    only after the caller authenticated as itself, and the arm is the whole
    diagnostic value — it is what the reviewer reports and what an operator acts on.
    Returned in the receipt with HTTP 200 rather than as an error status: the upload
    genuinely succeeded and the bytes are stored, so a 4xx would tell the worker its
    document was lost.
    """
    _, _, record, _ = context
    try:
        document = json.loads(body)
    except ValueError:
        # Not JSON at all. A 422 rather than a 404: the caller is authenticated and
        # this is a body defect it can fix, not an authorization answer.
        raise HTTPException(422, "review result is not valid JSON") from None
    if not isinstance(document, dict):
        raise HTTPException(422, "review result is not an object") from None

    try:
        execution = await run_in_threadpool(runtime.store._read, f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}")
    except AuthorityStoreError:
        raise HTTPException(503, "agent authority unavailable") from None
    if not execution:
        raise HTTPException(404, "not found")

    async def reverify() -> None:
        # Re-checked inside the observer's transaction, immediately before it
        # commits. `live_context` raises its own 404 on a changed authority.
        if (await live_context(request, runtime))[1:] != context[1:]:
            raise HTTPException(404, "not found")

    verified: dict[str, bool] = {}

    async def resolve_artifact_ref(key: str) -> bool:
        if key not in verified:
            verified[key] = await run_in_threadpool(_verify_stored_artifact, key, record=record, runtime=runtime, storage=storage)
        return verified[key]

    try:
        return await observe_review_upload(
            record,
            execution,
            document=document,
            reverify=reverify,
            stored_artifact_ref=f"{receipt['uri']}#sha256={receipt['sha256']}",
            resolve_artifact_ref=resolve_artifact_ref,
        )
    except ReviewUploadRefusedError as refused:
        return {"recorded": False, "refusal": refused.code, "detail": refused.detail}
    except (KeyError, TypeError, ValueError):
        # No engine assignment on the execution row, or not a reviewer persona:
        # not a dispatched reviewer, and indistinguishable from a run that does
        # not exist.
        raise HTTPException(404, "not found") from None


def _verify_stored_artifact(key: str, *, record, runtime, storage) -> bool:
    """Resolve only server-supported own-run keys and verify the stored bytes."""
    prefix = artifact_prefix(record)
    if not key.startswith(prefix):
        return False
    relative = key[len(prefix) :]
    kind, separator, filename = relative.partition("/")
    if not separator or kind not in _KINDS:
        return False
    bucket_env, extension, content_type = _KINDS[kind]
    match = re.fullmatch(r"([a-f0-9]{64})\." + re.escape(extension), filename)
    config = os.environ if runtime.env is None else runtime.env
    bucket = config.get(bucket_env)
    if match is None or not bucket:
        return False
    try:
        obj = storage.get_object(Bucket=bucket, Key=key)
        with obj["Body"] as source:
            data = source.read(MAX_ARTIFACT_BYTES + 1)
        return 0 < len(data) <= MAX_ARTIFACT_BYTES and obj.get("ContentType") == content_type and hashlib.sha256(data).hexdigest() == match[1]
    except (BotoCoreError, ClientError, OSError):
        return False
