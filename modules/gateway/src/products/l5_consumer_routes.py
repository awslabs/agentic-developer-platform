"""Product-scoped consumer projection and later-task use; no native submissions."""

import re

from fastapi import APIRouter, Depends, Request
from l5_services.models.publication import Publication
from starlette.responses import JSONResponse

from src.auth.dependencies import get_current_user
from src.internal.l5_consumer_routes import human_consumer_request
from src.l5_consumer_authority import denied
from src.shared.database import get_session_factory

router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")


async def selected(actor, product_id, project_id, service_id, contribution_id):
    if actor.account_type != "human":
        raise denied()
    async with get_session_factory()() as db:
        publication = await db.get(Publication, (actor.org_id, contribution_id))
        from l5_services.models.run import Run

        run = await db.get(Run, (actor.org_id, publication.payload["run_id"])) if publication else None
        if run is None or (run.product_id, run.project_id, run.service_id) != (product_id, project_id, service_id):
            raise denied()
        return publication


@router.get("/contributions/{contribution_id}/consumer/{operation}")
async def read_consumer(product_id: str, project_id: str, service_id: str, contribution_id: str, operation: str, actor=Depends(get_current_user)):
    publication = await selected(actor, product_id, project_id, service_id, contribution_id)
    result = await human_consumer_request(actor, publication, operation=operation)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.api_route("/contributions/{contribution_id}/consumer/tasks/{task_id}", methods=["GET", "POST"])
async def use_consumer(
    product_id: str, project_id: str, service_id: str, contribution_id: str, task_id: str, request: Request, actor=Depends(get_current_user)
):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id) or await request.body() not in {b"", b"{}"}:
        raise denied()
    publication = await selected(actor, product_id, project_id, service_id, contribution_id)
    result = await human_consumer_request(actor, publication, task_id=task_id, read_only=request.method == "GET")
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
