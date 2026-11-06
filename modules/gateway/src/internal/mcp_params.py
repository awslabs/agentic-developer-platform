"""Separate MCP transport metadata from operation parameters and authority."""

from fastapi import HTTPException


def operation_params(packet: dict):
    """Ignore optional MCP `_meta`; signed tool arguments remain unchanged.

    MCP clients may add progress tokens or other transport metadata. It is not
    caller authority and must neither enter the tool's arguments nor reach the
    downstream provider. Keep all other parameters for strict method validation.
    Null is the equivalent of absent metadata in SDK-serialized requests.
    """
    params = packet.get("params", {})
    if not isinstance(params, dict) or "_meta" not in params:
        return params
    if params["_meta"] is not None and not isinstance(params["_meta"], dict):
        raise HTTPException(400, "invalid MCP transport metadata")
    return {key: value for key, value in params.items() if key != "_meta"}
