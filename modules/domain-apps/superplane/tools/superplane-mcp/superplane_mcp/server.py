"""Superplane MCP tool surface — the single dispatch every transport goes through.

Shape copied from the one real MCP precedent in this repo: the agent-context
door's single `_dispatch_tool` behind two transports, with the per-transport
handlers as explicit thin shims (`door/server.py:484-512`, `door/mcp_app.py:10`).
R9 acc. 5 names that structure specifically, because the structure is what
*guarantees* uniform authorization — a restated policy does not.

Two invariants are structural here rather than remembered:

**Uniform authorization (R9 acc. 5).** `dispatch_tool()` is the only way to reach
a handler, and it calls `authorize()` itself. Transports pass raw headers; they
cannot pass a principal, cannot pass a decision, and have no flag that skips the
check. Adding a third transport cannot weaken authorization because there is no
parameter through which it could. This tightens the door's own arrangement, where
each transport calls `extract_caller_principal()` separately and hands the result
in — centralized dispatch, but duplicated extraction.

**Read/spend separation (R9 acc. 3).** Discovery and spending are different tools
bound to different contract methods. `superplane_discover_capacity` is wired to
`CapacityContract.list_capacity` and has no reference to `allocate` or `release`
in its call graph, so a model that meant to look at capacity cannot buy it by
passing a different argument. The separation is a property of the handler table,
and `tests/test_tool_separation.py` asserts it against that table rather than
trusting this docstring.

Results additionally pass through `redact()` on the single way out (R9 acc. 4),
so no handler can return a provider secret even if a future contract response
starts carrying one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .authz import (
    READ_ONLY_TOOLS,
    SPENDING_OR_MUTATING_TOOLS,
    TOOL_CAPABILITIES,
    authorize,
)
from .contract import CONTRACT_VERSION, IS_MOCK, CapacityContract, ContractError
from .redaction import redact

# Tool declarations. Single source of truth for both transports: the MCP shim
# derives its schema from this, so a tool cannot exist on one transport only.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "superplane_discover_capacity",
        "description": (
            "Read-only. List available Superplane capacity offers for a workspace, "
            "with price and availability. Spends nothing and changes nothing."
        ),
        "parameters": {
            "workspace": {"type": "string", "required": True},
            "accelerator": {"type": "string", "required": False},
        },
    },
    {
        "name": "superplane_allocate_capacity",
        "description": (
            "SPENDS MONEY. Allocate a capacity offer in a workspace. Call "
            "superplane_discover_capacity first to choose an offer_id."
        ),
        "parameters": {
            "workspace": {"type": "string", "required": True},
            "offer_id": {"type": "string", "required": True},
        },
    },
    {
        "name": "superplane_release_allocation",
        "description": (
            "MUTATES provider state. Release a previously allocated capacity "
            "allocation so it stops incurring cost."
        ),
        "parameters": {
            "workspace": {"type": "string", "required": True},
            "allocation_id": {"type": "string", "required": True},
        },
    },
]


def _missing(arguments: dict[str, Any], *names: str) -> str | None:
    """Return an error string naming the first absent required argument."""
    for name in names:
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            return f"{name} is required"
    return None


# ---------------------------------------------------------------------------
# Handlers. Each is bound to exactly one contract capability.
# ---------------------------------------------------------------------------


def _handle_discover_capacity(
    arguments: dict[str, Any], contract: CapacityContract
) -> dict[str, Any]:
    """Read-only capacity discovery.

    Touches `list_capacity` only. It does not receive, and cannot construct, a
    path to `allocate` or `release`.
    """
    error = _missing(arguments, "workspace")
    if error:
        return {"error": error}
    offers = contract.list_capacity(
        workspace=arguments["workspace"],
        accelerator=arguments.get("accelerator"),
    )
    return {
        "offers": offers,
        "count": len(offers),
        "contract_version": CONTRACT_VERSION,
        # Surfaced deliberately: a mocked reading must be distinguishable from a
        # provider reading by whoever consumes it. See contract.py.
        "source": "mock" if IS_MOCK else "live",
    }


def _handle_allocate_capacity(
    arguments: dict[str, Any], contract: CapacityContract
) -> dict[str, Any]:
    """Spending operation — allocates capacity that costs money."""
    error = _missing(arguments, "workspace", "offer_id")
    if error:
        return {"error": error}
    try:
        allocation = contract.allocate(
            workspace=arguments["workspace"], offer_id=arguments["offer_id"]
        )
    except ContractError as exc:
        return {"error": str(exc)}
    return {"allocation": allocation, "contract_version": CONTRACT_VERSION}


def _handle_release_allocation(
    arguments: dict[str, Any], contract: CapacityContract
) -> dict[str, Any]:
    """Mutating operation — releases an allocation."""
    error = _missing(arguments, "workspace", "allocation_id")
    if error:
        return {"error": error}
    try:
        allocation = contract.release(
            workspace=arguments["workspace"],
            allocation_id=arguments["allocation_id"],
        )
    except ContractError as exc:
        return {"error": str(exc)}
    return {"allocation": allocation, "contract_version": CONTRACT_VERSION}


# Handler table. The read/spend split is visible and testable here: the tests
# assert this mapping against READ_ONLY_TOOLS / SPENDING_OR_MUTATING_TOOLS, so a
# tool moved between categories without moving its handler fails CI.
HANDLERS: dict[str, Callable[[dict[str, Any], CapacityContract], dict[str, Any]]] = {
    "superplane_discover_capacity": _handle_discover_capacity,
    "superplane_allocate_capacity": _handle_allocate_capacity,
    "superplane_release_allocation": _handle_release_allocation,
}


def _assert_tool_tables_agree() -> None:
    """Fail at import time if the tool tables disagree.

    Four tables describe the same tool set — `TOOLS` (what is advertised),
    `HANDLERS` (what runs), `TOOL_CAPABILITIES` (what is required) and the
    read/spend classification (which side of the R9 acc. 3 split a tool is on).
    A name present in some but not all of them is the "registered without its
    implementation" bug class the issue's impact table names: the tool is
    advertised to the model and then denies or falls through when called.

    Checked here, at import, rather than only in tests, so the failure is
    immediate and local to the mistake. The tests assert the same invariant, but
    a test only fails where tests are run; this fails wherever the module loads.
    """
    declared = {tool["name"] for tool in TOOLS}
    classified = READ_ONLY_TOOLS | SPENDING_OR_MUTATING_TOOLS
    for label, table in (
        ("HANDLERS", set(HANDLERS)),
        ("TOOL_CAPABILITIES", set(TOOL_CAPABILITIES)),
        ("read/spend classification", classified),
    ):
        if table != declared:
            raise RuntimeError(
                f"superplane-mcp tool tables disagree: TOOLS vs {label} "
                f"differ by {sorted(declared ^ table)}"
            )
    overlap = READ_ONLY_TOOLS & SPENDING_OR_MUTATING_TOOLS
    if overlap:
        raise RuntimeError(
            f"tool(s) classified as both read-only and spending: {sorted(overlap)}"
        )


_assert_tool_tables_agree()


# ---------------------------------------------------------------------------
# The single dispatch. Every transport enters here and nowhere else.
# ---------------------------------------------------------------------------


def dispatch_tool(
    name: str,
    arguments: dict[str, Any] | None,
    headers: dict[str, str] | None,
    contract: CapacityContract | None = None,
) -> dict[str, Any]:
    """Authorize, then run a tool, then redact the result.

    The order is the design. Authorization happens before any handler is looked
    up so an unauthorized call cannot be distinguished from an unknown tool by
    timing or by error shape, and redaction happens on the single return path so
    no handler can bypass it.

    `contract` is injected for tests; production callers omit it.
    """
    arguments = arguments or {}
    headers = headers or {}

    decision = authorize(name, arguments, headers)
    if not decision.allowed:
        # No `caller` echoed back and no handler consulted.
        return {"error": "not authorized", "reason": decision.reason}

    handler = HANDLERS.get(name)
    if handler is None:
        # Unreachable while TOOL_CAPABILITIES and HANDLERS agree — a condition
        # the tests assert directly. Handled rather than allowed to raise, so a
        # future mismatch degrades to a denial instead of a 500 that leaks a
        # traceback into a model-visible result.
        return {"error": f"unknown tool: {name}"}

    result = handler(arguments, contract or CapacityContract())
    return redact(result)
