"""Authenticated installation observations; never mounted by the public proxy."""

import os

from fastapi import APIRouter, Depends, Request
from superplane_contracts import Submitter

from app.installation import capabilities
from app.routers.heartbeat import _authenticated_submitter

router = APIRouter(prefix="/internal")


@router.get("/installation")
async def installation_readiness(
    request: Request, submitter: Submitter = Depends(_authenticated_submitter)
):
    return {
        "release_id": os.environ.get("SUPERPLANE_RELEASE_ID"),
        "source_revision": os.environ.get("SUPERPLANE_SOURCE_REVISION"),
        "domain_auth_enforced": getattr(request.app.state, "domain_policy", None)
        is not None,
        "capabilities": capabilities(),
        "observations": {
            key: value
            for key, value in getattr(
                request.app.state, "observation_delivery", {}
            ).items()
            if value["workspace"] in submitter.workspaces
        },
    }
