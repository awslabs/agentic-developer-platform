"""Clients for services whose shared credentials never enter a coding worker."""

from __future__ import annotations

import re

from lib.status_gateway_client import StatusGatewayError, _post

_MARKER_FIELDS = frozenset(
    {
        "correlation_id",
        "root_human_id",
        "is_human_rooted",
        "invocation_id",
        "chain_depth",
        "signature",
    }
)


def own_marker_fields() -> dict[str, str]:
    """Ask for this authenticated run's complete marker, without supplying identity."""
    result = _post("/marker", {})
    if not isinstance(result, dict) or set(result) != _MARKER_FIELDS:
        raise StatusGatewayError("marker response was invalid")
    if any(
        not isinstance(value, str) or not value or len(value) > 255 or re.search(r"[\s<>]", value)
        for value in result.values()
    ):
        raise StatusGatewayError("marker response was invalid")
    if result["is_human_rooted"] not in {"true", "false"} or not re.fullmatch(
        r"[0-9]{1,3}", result["chain_depth"]
    ):
        raise StatusGatewayError("marker response was invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", result["signature"]):
        raise StatusGatewayError("marker response was invalid")
    return result
