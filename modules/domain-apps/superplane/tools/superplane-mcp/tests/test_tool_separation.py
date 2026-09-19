"""R9 acc. 3 and acc. 5 — read/spend separation, and one decision per transport.

Two properties are under test, and both are asserted structurally rather than by
example, because an example-only test passes while leaving the property false for
the case nobody wrote:

* a discovery tool cannot reach a spending or mutating code path;
* every transport resolves to the same authorization decision.

The transport-parity tests are parameterized over a transport registry, so a
transport added later without being added to the registry is itself caught by
`test_every_transport_is_covered_by_parity_tests`. Without that, a third
transport could be added, be authorized differently, and this file would still
be green — which is the exact failure mode ("the weaker path is the real policy")
the issue's impact table names.
"""

from __future__ import annotations

import json

import pytest
from superplane_mcp import (
    HANDLERS,
    READ_ONLY_TOOLS,
    SPENDING_OR_MUTATING_TOOLS,
    TOOL_CAPABILITIES,
    TOOLS,
    CapacityContract,
    dispatch_tool,
    mcp_call_tool,
    rest_call,
    transports,
)

# A caller holding every capability and bound to ws-alpha. Used so that denials
# in these tests are attributable to the property under test and not to a
# missing capability.
FULL_HEADERS = {
    "x-adp-principal": "user-1",
    "x-adp-tenant": "tenant-1",
    "x-adp-capabilities": (
        "superplane:capacity:read,"
        "superplane:capacity:allocate,"
        "superplane:capacity:release"
    ),
    "x-adp-workspaces": "ws-alpha",
}

READ_ONLY_HEADERS = {
    "x-adp-principal": "user-2",
    "x-adp-tenant": "tenant-1",
    "x-adp-capabilities": "superplane:capacity:read",
    "x-adp-workspaces": "ws-alpha",
}


# ---------------------------------------------------------------------------
# Transport registry. Each entry normalizes a transport to
# (name, arguments, headers) -> result dict, so parity can be asserted on
# identical inputs across every entry point the surface exposes.
# ---------------------------------------------------------------------------


def _via_rest(name, arguments, headers, contract=None):
    return rest_call({"name": name, "arguments": arguments}, headers, contract=contract)


def _via_mcp(name, arguments, headers, contract=None):
    wrapped = mcp_call_tool(name, arguments, headers, contract=contract)
    # Unwrap the MCP content block back to the underlying result so the two
    # transports are compared on the same shape.
    return json.loads(wrapped["content"][0]["text"])


def _via_direct(name, arguments, headers, contract=None):
    return dispatch_tool(name, arguments, headers, contract=contract)


TRANSPORTS = {
    "rest": _via_rest,
    "mcp": _via_mcp,
    "direct": _via_direct,
}


# ---------------------------------------------------------------------------
# R9 acc. 3 — read-only discovery is a distinct tool from spending/mutation
# ---------------------------------------------------------------------------


def test_read_and_spend_tool_sets_are_disjoint_and_total() -> None:
    """Every declared tool is classified exactly once as read-only or spending."""
    assert not (READ_ONLY_TOOLS & SPENDING_OR_MUTATING_TOOLS), (
        "a tool classified as both read-only and spending defeats the split"
    )
    declared = {tool["name"] for tool in TOOLS}
    classified = READ_ONLY_TOOLS | SPENDING_OR_MUTATING_TOOLS
    assert declared == classified, (
        f"unclassified tools: {declared - classified}; "
        f"classified but not declared: {classified - declared}"
    )


def test_discovery_is_a_distinct_tool_from_spending() -> None:
    """Discovery is its own tool name, not a mode of a spending tool."""
    assert READ_ONLY_TOOLS, "there must be at least one read-only tool"
    assert SPENDING_OR_MUTATING_TOOLS, "there must be at least one spending tool"
    assert READ_ONLY_TOOLS != SPENDING_OR_MUTATING_TOOLS


def test_discovery_capability_differs_from_spending_capabilities() -> None:
    """A read grant must not confer a spend grant.

    If discovery and allocation required the same capability, the tools would be
    separate in name only and a read-only caller could spend.
    """
    read_caps = {TOOL_CAPABILITIES[t] for t in READ_ONLY_TOOLS}
    spend_caps = {TOOL_CAPABILITIES[t] for t in SPENDING_OR_MUTATING_TOOLS}
    assert not (read_caps & spend_caps), (
        f"capability shared between read and spend tools: {read_caps & spend_caps}"
    )


def test_discovery_handler_cannot_reach_a_spending_contract_method() -> None:
    """The discovery handler's code path never touches allocate/release.

    Enforced by a contract double that raises if a spending method is called, so
    this asserts the *code path*, not merely the returned value. A handler that
    called `allocate()` and discarded the result would still fail here.
    """

    class TripwireContract(CapacityContract):
        def allocate(self, workspace, offer_id):  # type: ignore[override]
            raise AssertionError(
                "discovery reached allocate() — read/spend separation is broken"
            )

        def release(self, workspace, allocation_id):  # type: ignore[override]
            raise AssertionError(
                "discovery reached release() — read/spend separation is broken"
            )

    result = dispatch_tool(
        "superplane_discover_capacity",
        {"workspace": "ws-alpha"},
        FULL_HEADERS,
        contract=TripwireContract(),
    )
    assert "error" not in result
    assert result["count"] >= 1


def test_read_only_caller_cannot_spend() -> None:
    """A caller with only the read capability is denied both spending tools."""
    for tool in sorted(SPENDING_OR_MUTATING_TOOLS):
        result = dispatch_tool(
            tool,
            {
                "workspace": "ws-alpha",
                "offer_id": "offer-a1",
                "allocation_id": "alloc-1",
            },
            READ_ONLY_HEADERS,
        )
        assert result.get("error") == "not authorized", (
            f"{tool} allowed a read-only caller"
        )


def test_discovery_does_not_mutate_state() -> None:
    """Repeated discovery leaves no allocation behind."""
    contract = CapacityContract()
    for _ in range(3):
        dispatch_tool(
            "superplane_discover_capacity",
            {"workspace": "ws-alpha"},
            FULL_HEADERS,
            contract,
        )
    assert contract._allocations == {}, "discovery created allocation state"


def test_spending_tool_descriptions_announce_cost() -> None:
    """A model choosing a tool must be able to see which ones spend.

    The separation is only useful if it is legible at selection time — the tool
    list is what the model reads before it picks.
    """
    by_name = {tool["name"]: tool for tool in TOOLS}
    for tool in sorted(SPENDING_OR_MUTATING_TOOLS):
        description = by_name[tool]["description"]
        assert any(word in description for word in ("SPENDS MONEY", "MUTATES")), (
            f"{tool} description does not announce that it spends or mutates"
        )


# ---------------------------------------------------------------------------
# R9 acc. 5 — authorization identical across every transport
# ---------------------------------------------------------------------------

# (label, tool, arguments, headers, expect_authorized)
AUTHZ_CASES = [
    (
        "no identity is denied",
        "superplane_discover_capacity",
        {"workspace": "ws-alpha"},
        {},
        False,
    ),
    (
        "missing capability is denied",
        "superplane_allocate_capacity",
        {"workspace": "ws-alpha", "offer_id": "offer-a1"},
        READ_ONLY_HEADERS,
        False,
    ),
    (
        "unbound workspace is denied",
        "superplane_discover_capacity",
        {"workspace": "ws-beta"},
        FULL_HEADERS,
        False,
    ),
    (
        "unknown tool is denied",
        "superplane_nonexistent_tool",
        {"workspace": "ws-alpha"},
        FULL_HEADERS,
        False,
    ),
    (
        "granted read is allowed",
        "superplane_discover_capacity",
        {"workspace": "ws-alpha"},
        FULL_HEADERS,
        True,
    ),
    (
        "granted spend is allowed",
        "superplane_allocate_capacity",
        {"workspace": "ws-alpha", "offer_id": "offer-a1"},
        FULL_HEADERS,
        True,
    ),
]


@pytest.mark.parametrize("transport_name", sorted(TRANSPORTS))
@pytest.mark.parametrize(
    "label,tool,arguments,headers,expect_authorized",
    AUTHZ_CASES,
    ids=[case[0] for case in AUTHZ_CASES],
)
def test_authorization_is_identical_across_transports(
    transport_name, label, tool, arguments, headers, expect_authorized
) -> None:
    """Each transport reaches the same authorization outcome for the same input."""
    invoke = TRANSPORTS[transport_name]
    result = invoke(tool, arguments, headers, CapacityContract())
    denied = result.get("error") in ("not authorized", f"unknown tool: {tool}")
    assert denied is not expect_authorized, (
        f"{transport_name} disagreed on '{label}': {result}"
    )


@pytest.mark.parametrize(
    "label,tool,arguments,headers,expect_authorized",
    AUTHZ_CASES,
    ids=[case[0] for case in AUTHZ_CASES],
)
def test_every_transport_returns_the_same_denial_reason(
    label, tool, arguments, headers, expect_authorized
) -> None:
    """Denial reasons match across transports, not just the allow/deny verdict.

    A transport that denied for a different reason would indicate it reached a
    different decision point — the divergence this criterion is about, even
    though the coarse verdict agrees.
    """
    results = [
        invoke(tool, arguments, headers, CapacityContract())
        for invoke in (TRANSPORTS[name] for name in sorted(TRANSPORTS))
    ]
    reasons = {json.dumps(r.get("reason", ""), sort_keys=True) for r in results}
    assert len(reasons) == 1, (
        f"transports gave different reasons for '{label}': {reasons}"
    )


def test_every_transport_is_covered_by_parity_tests() -> None:
    """A new transport must be registered here, or this fails.

    Without this, adding a transport that authorizes differently would leave the
    parity tests above green because they never invoke it.
    """
    # Functions DEFINED in transports.py — not names merely imported into it.
    # Comparing against `dir()` would also pick up `CapacityContract`,
    # `dispatch_tool` and typing helpers, which are imports rather than entry
    # points and would make this assertion permanently red.
    defined = {
        name
        for name, obj in vars(transports).items()
        if not name.startswith("_")
        and callable(obj)
        and getattr(obj, "__module__", None) == transports.__name__
    }
    # Helpers that are not themselves a way to invoke a tool: `normalize_headers`
    # only reshapes headers, and `mcp_list_tools` advertises the tool set without
    # dispatching. Both are covered by their own tests above.
    helpers = {"normalize_headers", "mcp_list_tools"}
    entry_points = defined - helpers
    covered = {"rest_call", "mcp_call_tool"}
    assert entry_points == covered, (
        f"transport entry points not covered by parity tests: {entry_points - covered}. "
        f"Add each to TRANSPORTS in this module so its authorization is compared "
        f"against the others."
    )


def test_transports_do_not_duplicate_authorization_logic() -> None:
    """Transports must stay thin shims — no authorization branch of their own.

    R9 acc. 5 is guaranteed by there being exactly one decision point. A
    transport that grew its own capability check would break that guarantee
    while every behavioural test above still passed, so the constraint is
    asserted against the transport source directly.
    """
    source = __import__("pathlib").Path(transports.__file__).read_text(encoding="utf-8")
    # Strip the module docstring, which legitimately discusses authorization.
    body = source.split('"""', 2)[-1]
    for forbidden in ("capabilities", "TOOL_CAPABILITIES", "authorize(", "workspaces"):
        assert forbidden not in body, (
            f"transports.py references {forbidden!r} outside its docstring — "
            f"authorization must live only in authz.py behind dispatch_tool()"
        )


def test_handler_table_matches_declared_tools_and_capabilities() -> None:
    """TOOLS, HANDLERS and TOOL_CAPABILITIES must agree.

    A name in one and not another is the "registered without its implementation"
    class of bug: dispatch would deny or fall through at run time while the tool
    is still advertised.
    """
    declared = {tool["name"] for tool in TOOLS}
    assert declared == set(HANDLERS), (
        f"TOOLS/HANDLERS mismatch: {declared ^ set(HANDLERS)}"
    )
    assert declared == set(TOOL_CAPABILITIES), (
        f"TOOLS/TOOL_CAPABILITIES mismatch: {declared ^ set(TOOL_CAPABILITIES)}"
    )


def test_mcp_and_rest_advertise_the_same_tool_set() -> None:
    """Neither transport may hide or add a tool relative to the other."""
    assert {t["name"] for t in transports.mcp_list_tools()} == {
        t["name"] for t in TOOLS
    }


def test_mcp_list_tools_marks_required_parameters() -> None:
    """The MCP schema preserves which parameters are required."""
    listed = {t["name"]: t for t in transports.mcp_list_tools()}
    schema = listed["superplane_allocate_capacity"]["inputSchema"]
    assert set(schema["required"]) == {"workspace", "offer_id"}
    assert schema["properties"]["workspace"]["type"] == "string"


def test_mcp_flags_errors_in_its_envelope() -> None:
    """An unauthorized MCP call is marked isError for the client."""
    wrapped = mcp_call_tool(
        "superplane_discover_capacity", {"workspace": "ws-alpha"}, {}
    )
    assert wrapped["isError"] is True


def test_successful_mcp_call_is_not_flagged_as_error() -> None:
    wrapped = mcp_call_tool(
        "superplane_discover_capacity", {"workspace": "ws-alpha"}, FULL_HEADERS
    )
    assert wrapped["isError"] is False


# ---------------------------------------------------------------------------
# Tenant isolation and argument handling
# ---------------------------------------------------------------------------


def test_workspace_denial_does_not_reveal_existence() -> None:
    """An unbound real workspace and a nonexistent one deny identically.

    Distinguishing them would leak the existence of another tenant's workspace.
    """
    real_but_unbound = dispatch_tool(
        "superplane_discover_capacity", {"workspace": "ws-beta"}, FULL_HEADERS
    )
    nonexistent = dispatch_tool(
        "superplane_discover_capacity", {"workspace": "ws-does-not-exist"}, FULL_HEADERS
    )
    assert real_but_unbound == nonexistent


def test_allocation_is_scoped_to_the_bound_workspace() -> None:
    """An offer from another workspace cannot be allocated into a bound one."""
    result = dispatch_tool(
        "superplane_allocate_capacity",
        {"workspace": "ws-alpha", "offer_id": "offer-b1"},
        FULL_HEADERS,
    )
    assert "error" in result


def test_allocate_then_release_round_trip() -> None:
    """The spending path works for an authorized, bound caller."""
    contract = CapacityContract()
    allocated = dispatch_tool(
        "superplane_allocate_capacity",
        {"workspace": "ws-alpha", "offer_id": "offer-a1"},
        FULL_HEADERS,
        contract,
    )
    allocation_id = allocated["allocation"]["allocation_id"]
    assert allocated["allocation"]["state"] == "allocated"

    released = dispatch_tool(
        "superplane_release_allocation",
        {"workspace": "ws-alpha", "allocation_id": allocation_id},
        FULL_HEADERS,
        contract,
    )
    assert released["allocation"]["state"] == "released"


def test_release_of_unknown_allocation_errors() -> None:
    result = dispatch_tool(
        "superplane_release_allocation",
        {"workspace": "ws-alpha", "allocation_id": "alloc-nope"},
        FULL_HEADERS,
    )
    assert "error" in result


def test_missing_required_arguments_are_rejected() -> None:
    """Absent or blank required arguments produce an error, not a crash."""
    for tool, arguments in (
        ("superplane_discover_capacity", {}),
        ("superplane_allocate_capacity", {"workspace": "ws-alpha"}),
        (
            "superplane_release_allocation",
            {"workspace": "ws-alpha", "allocation_id": "  "},
        ),
    ):
        result = dispatch_tool(tool, arguments, FULL_HEADERS)
        assert "error" in result, f"{tool} accepted {arguments}"


def test_blank_workspace_is_denied() -> None:
    result = dispatch_tool(
        "superplane_discover_capacity", {"workspace": "   "}, FULL_HEADERS
    )
    assert result.get("error") == "not authorized"


def test_accelerator_filter_narrows_discovery() -> None:
    result = dispatch_tool(
        "superplane_discover_capacity",
        {"workspace": "ws-alpha", "accelerator": "nvidia-a100"},
        FULL_HEADERS,
    )
    assert result["count"] == 1
    assert result["offers"][0]["accelerator"] == "nvidia-a100"


def test_discovery_reports_that_the_contract_is_mocked() -> None:
    """A mocked reading must be self-identifying.

    Recorded per R9's allowance for mocking an unavailable contract: a result
    that looked live would let a mock be mistaken for a provider reading.
    """
    result = dispatch_tool(
        "superplane_discover_capacity", {"workspace": "ws-alpha"}, FULL_HEADERS
    )
    assert result["source"] == "mock"
    assert result["contract_version"] == "v1"


def test_header_case_and_byte_forms_resolve_identically() -> None:
    """Header casing and bytes/str form must not change the decision.

    The transports normalize differently in production (ASGI scope vs framework
    request), so a case-sensitive lookup would authorize the same caller on one
    transport and deny them on the other.
    """
    upper = {k.upper(): v for k, v in FULL_HEADERS.items()}
    byte_pairs = [(k.encode(), v.encode()) for k, v in FULL_HEADERS.items()]
    baseline = rest_call(
        {
            "name": "superplane_discover_capacity",
            "arguments": {"workspace": "ws-alpha"},
        },
        FULL_HEADERS,
    )
    for variant in (upper, byte_pairs):
        result = rest_call(
            {
                "name": "superplane_discover_capacity",
                "arguments": {"workspace": "ws-alpha"},
            },
            variant,
        )
        assert result == baseline


def test_dispatch_tolerates_absent_arguments_and_headers() -> None:
    """None arguments/headers deny cleanly rather than raising."""
    result = dispatch_tool("superplane_discover_capacity", None, None)
    assert result.get("error") == "not authorized"
