"""Authenticated installation observations; never mounted by the public proxy."""

import os

from fastapi import APIRouter, Depends, Request
from superplane_contracts import Submitter

from app.installation import capabilities_async
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
        # Exactly the four booleans, unchanged: the post-rollout recheck asserts
        # `len(capabilities) == 4` (installation/runner.py:1188). The probe detail is
        # deliberately not added here — this is an authenticated but tenant-facing
        # response, and an adapter's refusal message is the one place a provider
        # error or another tenant's identifier could have been interpolated. The
        # image-local CLI is where that detail belongs.
        "capabilities": await capabilities_async(),
        "observations": {
            key: value
            for key, value in getattr(
                request.app.state, "observation_delivery", {}
            ).items()
            if value["workspace"] in submitter.workspaces
        },
    }
