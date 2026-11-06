"""A Dev Box's `gh` and `git` act as the owner through the host's signed run identity (#8555).

Same proof as the model and tools routes (:func:`src.crew.host_tools.current_host_target`
plus the gateway-minted model token): an IAM caller with a Dev Box instance identity whose
run is the owner's current host run. The owner's GitHub connection then supplies the
token (:func:`src.crew.github_access.owner_github_token`); the box keeps it in memory and
never on disk (the checkpoint allowlist refuses ``hosts.yml`` and ``.git-credentials``).
A stopped or replaced box loses this with its lease, exactly like model access.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from starlette.responses import JSONResponse

from src.auth.connections_routes import get_identity_client
from src.crew import model_token
from src.crew.github_access import NO_STORE, enabled, owner_github_token
from src.crew.host_routes import host_run_route
from src.crew.host_tools import _denied, current_host_target
from src.proxy.routes import get_token_context, set_agent_run_id_from_header
from src.shared.database import get_session_factory
from src.shared.models.audit import AuditLog
from src.shared.schemas.auth import TokenContext

router = APIRouter(tags=["crew"])


@router.post(host_run_route("/crew/github-token"), dependencies=[Depends(enabled)])
async def mint_crew_github_token(
    context: Annotated[TokenContext, Depends(get_token_context)],
    run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    token: str | None = Header(default=None, alias="X-Adp-Crew-Model-Token"),
    identity=Depends(get_identity_client),
):
    now = int(time.time())
    owner, target = await current_host_target(context, run_id, now=now)
    claims = await model_token.verify_model_token(token or "", target, now=now)
    if claims["run"] != run_id:
        raise _denied()
    async with get_session_factory()() as db:
        minted = await owner_github_token(db, org_id=owner.tenant_id, user_id=owner.user_id, identity=identity, now=now)
        db.add(
            AuditLog(
                org_id=owner.tenant_id,
                actor_id=owner.user_id,
                event_type="crew_github_token_minted",
                details={
                    "run_id": run_id,
                    "box_id": target.lease.target_ref,
                    "instance_id": target.host_instance_ref,
                    "generation": target.lease.generation,
                    "connection_id": minted.credential_id,
                    "login": minted.login,
                    "expires_at": minted.expires_at,
                },
            )
        )
        await db.commit()
    return JSONResponse({"token": minted.token, "login": minted.login, "expires_at": minted.expires_at}, headers=NO_STORE)
