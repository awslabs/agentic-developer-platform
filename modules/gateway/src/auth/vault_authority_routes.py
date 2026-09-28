"""Supported vault-owner workflows; no caller-supplied validation readings."""

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import get_settings
from src.shared.database import get_db

from .middleware import get_current_user_context
from .provider_validation import ValidationUnavailableError
from .vault_authority import ValidationConflictError, set_workspace_delegation, validate_workspace_credential
from .vault_routes import _resolve_user_id_in_context, get_secrets_manager
from .vault_service import CredentialNotFoundError, InsufficientPrivilegesError

router = APIRouter(prefix="/auth/credentials", tags=["vault"])


async def _delegation(credential_id, workspace_id, active, caller, db):
    await _resolve_user_id_in_context(caller, db)
    try:
        return await set_workspace_delegation(db, caller, credential_id=credential_id, workspace_id=workspace_id, active=active)
    except CredentialNotFoundError:
        raise HTTPException(404, "credential not found") from None
    except InsufficientPrivilegesError:
        raise HTTPException(403, "credential administration required") from None


@router.put("/{credential_id}/workspaces/{workspace_id}")
async def delegate_workspace(
    credential_id: str = Path(min_length=1, max_length=36),
    workspace_id: str = Path(min_length=1, max_length=255),
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Let the credential's owner authorize use in one workspace of their tenant."""
    return await _delegation(credential_id, workspace_id, True, caller, db)


@router.delete("/{credential_id}/workspaces/{workspace_id}")
async def withdraw_workspace(
    credential_id: str = Path(min_length=1, max_length=36),
    workspace_id: str = Path(min_length=1, max_length=255),
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Withdraw delivery authority and invalidate its provider evidence atomically."""
    return await _delegation(credential_id, workspace_id, False, caller, db)


@router.post("/{credential_id}/workspaces/{workspace_id}/validation")
async def validate_workspace(
    credential_id: str = Path(min_length=1, max_length=36),
    workspace_id: str = Path(min_length=1, max_length=255),
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    sm=Depends(get_secrets_manager),
    settings=Depends(get_settings),
):
    """Independently validate the current version; accepts no report or secret body."""
    await _resolve_user_id_in_context(caller, db)
    try:
        return await validate_workspace_credential(db, sm, settings, caller, credential_id=credential_id, workspace_id=workspace_id)
    except CredentialNotFoundError:
        raise HTTPException(404, "credential not found") from None
    except InsufficientPrivilegesError:
        raise HTTPException(403, "credential administration required") from None
    except ValidationConflictError:
        raise HTTPException(409, "credential authority changed or delegation is missing") from None
    except ValidationUnavailableError:
        raise HTTPException(503, "provider validation unavailable") from None
