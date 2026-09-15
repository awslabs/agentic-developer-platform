"""Producer-only work admission, authenticated by an invocation-bound STS proof.

Ingress has no pod identity and the worker's shared key/header cannot identify
it. STS verifies the producer's signature against a fixed GetCallerIdentity
request; the invocation is included in the signed headers. No arbitrary URL,
AWS operation, tenant, owner, handover or release is accepted here.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import xml.etree.ElementTree as ET

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.routes import AgentRuntime, get_agent_runtime
from src.agentauth.store import AuthorityStoreError
from src.auth.agent_registry import parse_assumed_role_arn
from src.orchestration.work_admission import admit_pending
from src.orchestration.work_claims import WorkClaimError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/v1/agent/work", tags=["work-admission"])
PROOF_HEADER = "X-Adp-Producer-Proof"
INVOCATION_HEADER = "x-adp-work-invocation"
STS_BODY = "Action=GetCallerIdentity&Version=2011-06-15"


class WorkAdmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invocation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")


async def verify_producer(proof: str, invocation_id: str) -> None:
    allowed = set(filter(None, os.environ.get("ADP_WORK_CLAIM_PRODUCER_ROLES", "").split(",")))
    try:
        if not allowed or not proof or len(proof) > 12000:
            raise ValueError()
        headers = json.loads(base64.b64decode(proof, validate=True))
        permitted = {"authorization", "x-amz-date", "x-amz-security-token", "content-type", INVOCATION_HEADER}
        if not isinstance(headers, dict) or set(headers) - permitted or any(not isinstance(v, str) for v in headers.values()):
            raise ValueError()
        authorization = headers.get("authorization", "")
        signed_headers = authorization.split("SignedHeaders=", 1)[1].split(",", 1)[0].split(";")
        if headers.get(INVOCATION_HEADER) != invocation_id or INVOCATION_HEADER not in signed_headers:
            raise ValueError()
        region = os.environ.get("AWS_REGION", "us-east-1")
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
            response = await client.post(f"https://sts.{region}.amazonaws.com/", headers=headers, content=STS_BODY)
            response.raise_for_status()
        # Parse only bounded STS output from the fixed TLS endpoint.
        if len(response.content) > 8192:
            raise ValueError()
        root = ET.fromstring(response.content)
        arn = root.findtext("{*}GetCallerIdentityResult/{*}Arn", default="")
        if parse_assumed_role_arn(arn) not in allowed:
            raise ValueError()
    except (ValueError, KeyError, IndexError, TypeError, httpx.HTTPError, ET.ParseError):
        raise HTTPException(403, "forbidden") from None


@router.post("/admit")
async def admit_work(body: WorkAdmissionRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime)) -> JSONResponse:
    await verify_producer(request.headers.get(PROOF_HEADER, ""), body.invocation_id)
    try:
        receipt = await admit_pending(runtime.store, body.invocation_id)
        return JSONResponse(receipt, headers={"Cache-Control": "no-store"})
    except WorkClaimError as exc:
        logger.info("work admission refused invocation=%s reason=%s", body.invocation_id, exc.code)
        raise HTTPException(409, "work ownership refused") from None
    except (BootstrapRefusedError, AuthorityStoreError):
        raise HTTPException(404, "not found") from None
