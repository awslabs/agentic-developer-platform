"""Product tool arguments select scope; current server membership grants it."""

import json
import re
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from src.shared.identity.product_authority import stored_product_actor
from src.shared.schemas.auth import TokenContext


def requested_product(operation: str, arguments: dict) -> str | None:
    if "product_id" not in arguments:
        return None
    product_id = arguments["product_id"]
    if not isinstance(product_id, str) or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", product_id):
        raise HTTPException(422, "invalid product_id")
    # Legacy code/graph stores have only repository-name identity. They cannot
    # express independent Product registrations, so never fall through to them.
    if operation != "search" or arguments.get("scope", "code") not in {"code", "docs"} or arguments.get("project"):
        raise HTTPException(422, "Product knowledge supports search without a legacy project scope")
    return product_id


def product_from_body(body: bytes, path: str) -> str | None:
    try:
        packet = json.loads(body)
    except (ValueError, UnicodeError):
        return None  # Door retains responsibility for malformed non-Product calls.
    if not isinstance(packet, dict):
        return None
    if path == "/mcp/":
        if packet.get("method") != "tools/call":
            return None
        packet = packet.get("params", {})
    if not isinstance(packet, dict) or not isinstance(packet.get("arguments"), dict):
        return None
    return requested_product(packet.get("name", ""), packet["arguments"])


async def authorize_product(db, *, user_id: str, tenant_id: str, product_id: str):
    from src.knowledge.product_access import require_product_access

    # Identity values come only from the live run/facade principal, never tool
    # arguments or request headers. Try current Product team membership first.
    actor = TokenContext(
        user_id=user_id,
        org_id=tenant_id,
        team_id="",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    try:
        return await require_product_access(db, actor, product_id, lock=True)
    except HTTPException as denied:
        if denied.status_code != 404:
            raise

        # Both Knowledge bridges already verify and lock live human workspace
        # membership; the shared helper adds only fresh platform authority.
        verified = await stored_product_actor(db, user_id=user_id, tenant_id=tenant_id)
        if not verified.is_admin:
            raise denied
        return await require_product_access(db, verified, product_id, lock=True)
