"""Bounded process-local runtime observation through the verified IAM edge."""

import asyncio
import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from l5_services.release_identity import release_identity
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from alembic.config import Config
from alembic.script import ScriptDirectory
from src.auth.agent_registry import parse_assumed_role_arn
from src.auth.caller_provenance import verified_caller_identity
from src.shared.database import get_engine

router = APIRouter(prefix="/internal/v1/runtime-proof", tags=["runtime-proof"])
ROLE_PATTERN = re.compile(r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+")


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pod_uid: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$")
    namespace: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9-]+$")
    nonce: str = Field(pattern=r"^[a-f0-9]{64}$")


async def process_proof():
    async with get_engine().connect() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        await connection.execute(text("SET LOCAL statement_timeout = '8s'"))
        await connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        heads = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    return {
        "database_heads": sorted(heads),
        "image_heads": sorted(ScriptDirectory.from_config(Config("/app/alembic.ini")).get_heads()),
        **release_identity(),
        "cli_manifest_sha256": hashlib.sha256(Path("/app/cli/command-manifest.json").read_bytes()).hexdigest(),
        "product_profile_enabled": os.getenv("BG_PRODUCT_PROFILE_ENABLED"),
        "product_onboarding_enabled": os.getenv("BG_PRODUCT_ONBOARDING_ENABLED"),
    }


def require_runtime_proof_role(request: Request):
    caller = verified_caller_identity(request)
    roles = os.getenv("ADP_RUNTIME_PROOF_ROLE_ARNS", "").split(",")
    if not caller or not all(ROLE_PATTERN.fullmatch(role) for role in roles) or parse_assumed_role_arn(caller) not in roles:
        raise HTTPException(403, "protected runtime reader required")


@router.post("/schema", dependencies=[Depends(require_runtime_proof_role)])
async def runtime_schema(probe: ProbeRequest):
    uid, namespace = os.getenv("ADP_POD_UID"), os.getenv("ADP_POD_NAMESPACE")
    if not uid or not namespace:
        raise HTTPException(503, "runtime pod identity unavailable")
    if namespace != probe.namespace:
        raise HTTPException(403, "runtime namespace differs")
    if uid != probe.pod_uid:
        raise HTTPException(409, "runtime pod differs")
    try:
        proof = await asyncio.wait_for(process_proof(), timeout=12)
    except Exception:
        raise HTTPException(503, "runtime observation unavailable") from None
    return {**proof, "pod_uid": uid, "namespace": namespace, "nonce": probe.nonce, "observed_at": datetime.now(UTC).isoformat()}
