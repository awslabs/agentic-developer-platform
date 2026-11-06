"""Attach an owned, unused chat session to its Product's execution Project."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.orchestration import chat_history
from src.products.models import ProjectRun
from src.products.routes import current_owner
from src.products.work_scope import ProjectScopeError, authorize_project_selection, bind_run
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

router = APIRouter(tags=["products"])


class SessionProject(BaseModel):
    product_id: str = Field(min_length=1, max_length=36)
    project_id: str | None = Field(default=None, min_length=1, max_length=255)


@router.put("/chat/sessions/{session_id}/project", dependencies=[Depends(chat_history.no_store)])
async def select_session_project(
    session_id: str,
    body: SessionProject,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[TokenContext, Depends(get_current_user)],
    table=Depends(chat_history.store),
):
    session = await chat_history.owned_session(session_id, actor, table, db)
    member = await current_owner(db, actor)
    try:
        scope = await authorize_project_selection(
            db, user_id=member.id, tenant_id=actor.org_id, product_id=body.product_id, project_id=body.project_id
        )
        existing = await db.get(ProjectRun, (actor.org_id, session_id))
        if existing is None and (session.get("messages") or session.get("threads")):
            raise HTTPException(409, "Choose the Project before sending the first message.")
        result = await bind_run(db, scope=scope, run_id=session_id, owner_user_id=member.id)
        await db.commit()
    except ProjectScopeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"product_id": result.product_id, "project_id": result.project_id}
