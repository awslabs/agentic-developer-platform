"""Trusted Cyber job admission for the existing GitHub Actions transport.

SQS is a broker-only delivery channel. Its bodies are not an authentication API.
The broker proves the registered workflow, runner role and verified human owner,
then grants a short-lived read of one immutable S3 version. Analysis workers
have no IAM grant on tenant objects and never receive the broker's credentials.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import re
import time
import uuid
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.arc_model import bindings, canonical_identity, github_claims, human_owner
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.work_routes import PROOF_HEADER, verify_producer
from src.shared.database import get_db
from src.shared.models.organization import TeamMembership, User

MAX_SAMPLE = 64 * 1024 * 1024
MAX_SCRIPT = 32 * 1024
TTL = 900
router = APIRouter(prefix="/internal/v1/agent/arc/cyber", tags=["agent-authority"])


class CyberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    github_oidc_token: str = Field(min_length=1, max_length=16384)
    artifact_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    sample_s3_uri: str = Field(min_length=1, max_length=2048)
    stage: str = Field(pattern=r"^(triage|static)$")
    script_base64: str | None = Field(default=None, max_length=44000)
    focus: list[str] = Field(default_factory=list, max_length=20)
    yara_rules: list[str] = Field(default_factory=list, max_length=20)


class ResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    github_oidc_token: str = Field(min_length=1, max_length=16384)
    job_id: str = Field(pattern=r"^cyber-[a-f0-9]{32}-[a-f0-9]{32}$")


def cyber_clients():
    config = Config(
        signature_version="s3v4", connect_timeout=3, read_timeout=30, retries={"total_max_attempts": 1}, s3={"addressing_style": "virtual"}
    )
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {name: boto3.client(name, region_name=region, config=config) for name in ("s3", "sqs", "dynamodb")}


async def caller(body, request: Request, db):
    registrations = [entry for entry in bindings() if entry.persona == "malware-analysis-agent"]
    role = await verify_producer(
        request.headers.get(PROOF_HEADER, ""), envelope_digest(body.model_dump()), allowed_roles={entry.runner_role for entry in registrations}
    )
    claims = await github_claims(body.github_oidc_token)
    matches = [
        entry
        for entry in registrations
        if entry.runner_role == role
        and entry.repository_id == claims["repository_id"]
        and entry.repository == claims["repository"]
        and entry.workflow_ref == claims["workflow_ref"]
        and entry.job_workflow_ref == claims.get("job_workflow_ref")
    ]
    # A service registration's registered_by human is NOT its sample owner.
    # Private human samples require a verified human-triggered workflow.
    if len(matches) != 1 or claims["event_name"] not in {"issues", "issue_comment", "workflow_dispatch"}:
        raise HTTPException(403, "Cyber workflow identity refused")
    binding = matches[0]
    owner = await human_owner(db, tenant=binding.tenant_id, actor_id=claims["actor_id"])
    user = await db.scalar(
        select(User).where(User.id == owner, User.org_id == binding.tenant_id, User.user_kind == "human").execution_options(populate_existing=True)
    )
    if user is None or not user.cognito_sub:
        raise HTTPException(403, "Cyber sample owner unavailable")
    teams = set(
        (
            await db.scalars(
                select(TeamMembership.team_id).where(
                    TeamMembership.user_id == owner,
                    TeamMembership.org_id == binding.tenant_id,
                )
            )
        ).all()
    )
    if not teams:
        raise HTTPException(403, "Cyber team membership unavailable")
    identity = {name: claims[name] for name in ("repository_id", "workflow_ref", "run_id", "run_attempt", "actor_id")}
    identity.update(org_id=binding.tenant_id, user_id=user.cognito_sub)
    prefix = "cyber-" + hashlib.sha256(canonical_identity(identity)).hexdigest()[:32] + "-"
    return binding.tenant_id, user.cognito_sub, teams, prefix


def sample_key(uri: str, *, bucket: str, org: str, user: str, teams: set[str]) -> tuple[str, str]:
    parsed = urlsplit(uri)
    parts = parsed.path.removeprefix("/").split("/")
    # Require the canonical ingest key including session/task/in. No flat or
    # caller-relabelled catalog row can stand in for ownership of the S3 path.
    if (
        parsed.scheme != "s3"
        or parsed.netloc != bucket
        or parsed.query
        or parsed.fragment
        or len(parts) != 11
        or any(not p or p in (".", "..") for p in parts)
        or parts[0:2] != ["o", org]
        or parts[2] != "t"
        or parts[3] not in teams
        or parts[4:6] != ["u", user]
        or parts[6] != "s"
        or parts[9] != "in"
    ):
        raise HTTPException(403, "Cyber sample ownership refused")
    return "/".join(parts), parts[3]


def register(body: CyberRequest, authority, clients) -> dict:
    org, user, teams, prefix = authority
    bucket = os.environ.get("CYBER_SAMPLE_BUCKET", "")
    queue = os.environ.get("CYBER_" + body.stage.upper() + "_QUEUE", "")
    if not bucket or not queue:
        raise HTTPException(503, "Cyber admission unavailable")
    key, team = sample_key(body.sample_s3_uri, bucket=bucket, org=org, user=user, teams=teams)
    script = None
    if body.script_base64 is not None:
        try:
            script = base64.b64decode(body.script_base64, validate=True)
            if body.stage != "static" or not 0 < len(script) <= MAX_SCRIPT:
                raise ValueError()
            ast.parse(script.decode("utf-8"))
        except (ValueError, UnicodeError, SyntaxError, RecursionError):
            raise HTTPException(422, "Cyber script registration refused") from None
    if any(not re.fullmatch(r"[A-Za-z0-9_. -]{1,128}", hint) for hint in body.focus + body.yara_rules):
        raise HTTPException(422, "Cyber analysis options refused")
    s3 = clients["s3"]
    head = s3.head_object(Bucket=bucket, Key=key)
    version = head.get("VersionId")
    if not version or version == "null" or not 0 < head["ContentLength"] <= MAX_SAMPLE:
        raise HTTPException(409, "Cyber requires a bounded, versioned sample")
    # Hash bytes, never invoke sample parsers in this credentialed process.
    obj = s3.get_object(Bucket=bucket, Key=key, VersionId=version)
    digest, size = hashlib.sha256(), 0
    with obj["Body"] as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            if size > MAX_SAMPLE:
                raise HTTPException(413, "Cyber sample too large")
            digest.update(chunk)
    if size != head["ContentLength"]:
        raise HTTPException(409, "Cyber sample changed")
    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key, "VersionId": version}, ExpiresIn=TTL)
    now = int(time.time())
    job_id = prefix + uuid.uuid4().hex
    manifest = {
        "artifact_id": job_id,
        "source_artifact_id": body.artifact_id,
        "stage": body.stage,
        "org_id": org,
        "team_id": team,
        "user_id": user,
        "sample_s3_uri": body.sample_s3_uri,
        "sample_download": {"url": url, "sha256": digest.hexdigest(), "size": size, "version": version},
        "issued_at": now,
        "expires_at": now + TTL,
        "focus": body.focus,
        "yara_rules": body.yara_rules,
        "registration_version": 1,
    }
    if script is not None:
        manifest.update(
            script_base64=base64.b64encode(script).decode(), script_sha256=hashlib.sha256(script).hexdigest(), script_validation="python-syntax-v1"
        )
    return {"queue": queue, "manifest": manifest}


@router.post("/jobs")
async def create_job(body: CyberRequest, request: Request, db: AsyncSession = Depends(get_db), clients=Depends(cyber_clients)):
    identity = await caller(body, request, db)
    try:
        prepared = await run_in_threadpool(register, body, identity, clients)
        if await caller(body, request, db) != identity:
            raise HTTPException(403, "Cyber authority changed")
        manifest = prepared["manifest"]
        await run_in_threadpool(
            clients["sqs"].send_message,
            QueueUrl=prepared["queue"],
            MessageBody=json.dumps(manifest),
            MessageGroupId=manifest["artifact_id"],
            MessageDeduplicationId=manifest["artifact_id"],
        )
        return JSONResponse({"job_id": manifest["artifact_id"], "expires_at": manifest["expires_at"]}, headers={"Cache-Control": "no-store"})
    except (BotoCoreError, ClientError):
        raise HTTPException(503, "Cyber admission unavailable") from None


@router.post("/result")
async def result(body: ResultRequest, request: Request, db: AsyncSession = Depends(get_db), clients=Depends(cyber_clients)):
    authority = await caller(body, request, db)
    org, user, teams, prefix = authority
    if not body.job_id.startswith(prefix):
        raise HTTPException(404, "not found")
    table = os.environ.get("CYBER_RESULTS_TABLE", "")
    if not table:
        raise HTTPException(503, "Cyber results unavailable")
    try:
        response = await run_in_threadpool(
            clients["dynamodb"].query,
            TableName=table,
            KeyConditionExpression="artifact_id = :id",
            ExpressionAttributeValues={":id": {"S": body.job_id}},
            ConsistentRead=True,
            ScanIndexForward=False,
            Limit=1,
        )
        # Membership can be revoked while the storage request is in flight.
        if await caller(body, request, db) != authority:
            raise HTTPException(403, "Cyber authority changed")
        items = response.get("Items", [])
        if not items:
            return JSONResponse({"status": "pending"}, headers={"Cache-Control": "no-store"})
        row = items[0]
        # Legacy unscoped results fail closed. The scope is written by the
        # trusted supervisor from the broker manifest, never by the analyzer.
        if row.get("org_id", {}).get("S") != org or row.get("user_id", {}).get("S") != user or row.get("team_id", {}).get("S") not in teams:
            raise HTTPException(404, "not found")
        return JSONResponse(
            {"status": row["status"]["S"], "stage": row["stage"]["S"], "findings": json.loads(row["findings"]["S"])},
            headers={"Cache-Control": "no-store"},
        )
    except (BotoCoreError, ClientError, ValueError, KeyError):
        raise HTTPException(503, "Cyber results unavailable") from None
