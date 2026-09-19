"""Bounded own-run artifact writes. Workers receive no bucket credential (#5195)."""

import asyncio
import hashlib
import os

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.artifact_keys import artifact_prefix
from src.agentauth.routes import AgentRuntime, get_agent_runtime, require_agent_transport
from src.agentauth.run_services import live_context

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_KINDS = {
    "transcript": ("AGENT_RUN_LOGS_BUCKET", "md", "text/markdown"),
    "spill": ("AGENT_RUN_LOGS_BUCKET", "txt", "text/plain"),
    "comment": ("AGENT_FALLBACK_BUCKET", "md", "text/markdown"),
    "git-changes": ("AGENT_FALLBACK_BUCKET", "tar.gz", "application/gzip"),
    "git-manifest": ("AGENT_FALLBACK_BUCKET", "md", "text/markdown"),
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
            final = await live_context(request, runtime)
            if current[1:] != final[1:]:
                raise HTTPException(404, "not found")
            return JSONResponse({"key": key, "uri": f"s3://{bucket}/{key}", "sha256": digest}, headers={"Cache-Control": "no-store"})
    except (BotoCoreError, ClientError, TimeoutError):
        raise HTTPException(503, "artifact storage unavailable") from None
