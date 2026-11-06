import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.installation_reader import authorize_installation_reader
from src.agentauth.installation_status import read_installation_status
from src.auth.dependencies import get_current_user
from src.features.routes import _is_enabled, _is_enabled_strict
from src.shared.database import get_db
from src.shared.identity import resolve_canonical_user_id
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/assistant/installations", tags=["assistant"])


@router.get("/{installation_id}/status")
async def installation_status(
    installation_id: Annotated[int, Path(gt=0, lt=10**20)],
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    headers = {"Cache-Control": "no-store"}
    if (
        not _is_enabled_strict("FEATURE_ASSISTANT_UI_ENABLED")
        or not _is_enabled("FEATURE_CHAT_ENABLED")
        or os.environ.get("ADP_CHAT_DATA_ENABLED") != "true"
    ):
        raise HTTPException(503, detail="Installation diagnostics unavailable", headers=headers)
    if current_user.account_type != "human" or current_user.auth_source != "jwt":
        raise HTTPException(404, detail="Installation unavailable", headers=headers)
    user_id = await resolve_canonical_user_id(db, current_user.user_id, org_id=current_user.org_id)
    try:
        scope = await authorize_installation_reader(db, tenant_id=current_user.org_id, user_id=user_id, installation_id=installation_id)
    except ChatAuthorizationRefusedError:
        raise HTTPException(404, detail="Installation unavailable", headers=headers) from None
    status = await read_installation_status(db, scope)
    return JSONResponse({**status, "installation_id": str(scope.installation_id)}, headers=headers)
