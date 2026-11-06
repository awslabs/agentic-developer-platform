"""Keep the original vendor routes on their signed backend generation."""

import json
import os

from fastapi import HTTPException
from sqlalchemy import select

from src.shared.models.vault import ToolAdapterActivation


async def require_legacy_generation(db, claims):
    activations = (await db.scalars(select(ToolAdapterActivation).with_for_update(read=True))).all()
    if not activations:
        if claims.get("adapter_release") is not None:
            raise HTTPException(403, "tool release unavailable")
        return
    if len(activations) != 1:
        raise HTTPException(503, "tool release unavailable")
    activation = activations[0]
    try:
        release = json.loads(os.environ["ADP_TOOL_ADAPTER_RELEASE"])
    except (KeyError, ValueError):
        raise HTTPException(503, "tool release unavailable") from None
    if (
        activation.status != "legacy"
        or release != activation.release
        or activation.group != release.get("group")
        or claims.get("adapter_release") != release
    ):
        raise HTTPException(403, "tool release fenced")
