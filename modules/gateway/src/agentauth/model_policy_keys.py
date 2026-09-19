"""Public verification-key discovery over the deployment's trusted HTTPS origin."""

import os

from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse

from src.agentauth.envelope import SIGNING_KEY_ID_ENV, EnvelopeError, _signing_key
from src.shared.database import get_db

router = APIRouter(prefix="/internal/v1/agent", tags=["agent-authority"])


@router.get("/model-policy-keys")
async def model_policy_keys():
    try:
        key_id = os.environ.get(SIGNING_KEY_ID_ENV)
        if not key_id:
            raise EnvelopeError("key unavailable")
        public = _signing_key().public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        return JSONResponse({"keys": {key_id: public}}, headers={"Cache-Control": "no-store"})
    except EnvelopeError:
        raise HTTPException(503, "model verification keys unavailable") from None


@router.post("/legacy-chat-preflight")
async def legacy_chat_preflight(request: Request, db=Depends(get_db)):
    """Legacy raw Bedrock has no registered harness; enforcement must retire it.

    This issues no model selection. The TLS/IAM response permits only unchanged
    legacy behavior while the committed platform posture is permissive.
    """
    from src.agentauth.routes import require_agent_transport
    from src.agentauth.runtime_posture import RuntimePostureError, read_live_posture

    await require_agent_transport(request)
    try:
        posture = await read_live_posture(db, compatibility_class="claude-agent-sdk")
    except RuntimePostureError:
        raise HTTPException(503, "model posture unavailable") from None
    if posture.enforcing:
        raise HTTPException(409, "legacy chat harness unsupported; use the current chat worker")
    return JSONResponse({"legacy_permitted": True, "posture": posture.posture}, headers={"Cache-Control": "no-store"})
