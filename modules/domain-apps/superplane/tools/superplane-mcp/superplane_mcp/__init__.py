"""Superplane MCP tool surface (EPIC #4910, unit U4 / issue #5038, R9).

Thin MCP adapters over the Superplane domain contract, structured so that two
invariants hold by construction rather than by convention:

* **One authorization decision, reached by every transport** (R9 acc. 5) —
  `server.dispatch_tool()` is the only route to a handler and it authorizes
  itself. Transports (`transports.py`) are shims that pass raw headers.
* **Read-only discovery is a distinct tool from spending or mutation**
  (R9 acc. 3) — separate tools bound to separate contract methods.

Every result also passes the redaction boundary (`redaction.py`) on the single
way out, so no tool result carries a provider secret (R9 acc. 4).

`contract.py` is a **recorded mock**: the versioned domain contract is owned by
U8 and has not landed, and R9 explicitly permits mocking an unavailable contract
provided the mock is recorded. See that module's docstring for what is stubbed
and the one seam to replace when the real contract exists.
"""

from .authz import (
    READ_ONLY_TOOLS,
    SPENDING_OR_MUTATING_TOOLS,
    TOOL_CAPABILITIES,
    CallerPrincipal,
    Decision,
    authorize,
    extract_caller_principal,
)
from .contract import CONTRACT_VERSION, IS_MOCK, CapacityContract, ContractError
from .redaction import PLACEHOLDER, contains_secret, redact
from .server import HANDLERS, TOOLS, dispatch_tool
from .transports import mcp_call_tool, mcp_list_tools, normalize_headers, rest_call

__all__ = [
    "CONTRACT_VERSION",
    "HANDLERS",
    "IS_MOCK",
    "PLACEHOLDER",
    "READ_ONLY_TOOLS",
    "SPENDING_OR_MUTATING_TOOLS",
    "TOOLS",
    "TOOL_CAPABILITIES",
    "CallerPrincipal",
    "CapacityContract",
    "ContractError",
    "Decision",
    "authorize",
    "contains_secret",
    "dispatch_tool",
    "extract_caller_principal",
    "mcp_call_tool",
    "mcp_list_tools",
    "normalize_headers",
    "redact",
    "rest_call",
]
