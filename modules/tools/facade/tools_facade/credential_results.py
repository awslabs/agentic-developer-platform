"""Keep credential delivery out of generic tool receipts and artifacts."""

from fastapi import HTTPException

PRIVATE_DELIVERY_TOOLS = frozenset({"credentials___assume_role", "credentials___materialize"})
PRIVATE_DELIVERY_REQUIRED = "private_credential_delivery_required"


def require_generic_tool(tool):
    if not isinstance(tool, str) or not tool or tool in PRIVATE_DELIVERY_TOOLS:
        raise HTTPException(409, PRIVATE_DELIVERY_REQUIRED)


def private_result(operation):
    tool = operation.get("tool", {}).get("S")
    return not isinstance(tool, str) or not tool or tool in PRIVATE_DELIVERY_TOOLS
