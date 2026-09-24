"""The endpoint inventory is complete and decided — R6 acc. 2 (issue #5044).

The story calls the inventory "the real work", and the reason is that a partial
one is worse than none: the coverage it replaces is *inconsistent* rather than
uniformly absent, so a reader cannot tell an endpoint that was decided from one
that was forgotten. Quota endpoints have no RBAC at all, some endpoints claim
org-admin only in a docstring, and role enforcement is unreachable by any real
token because no production-issuable ADP token carries a role claim.

So the inventory needs a test that can actually fail. This file derives the
endpoint surface from ``modules/gateway/cli/adp-superplane.py`` — the in-repo
client that calls every one of these endpoints — and compares it to
:data:`ENDPOINT_INVENTORY` in both directions.

Why derive rather than list
---------------------------
A hand-written list of expected endpoints in a test is the same artifact as the
inventory, so it agrees with it by construction and proves nothing. Deriving from
the client means adding a call without adding an inventory entry fails this
suite, which is the drift this criterion is asking to prevent.

Why the CLI is the right source
-------------------------------
It is the only complete enumeration of the surface inside this repository — the
handlers are upstream. It is also the surface a user reaches, so an endpoint
missing from it is not reachable by ADP's own client. The extraction is an AST
walk, not a regex, because two functions in that file both use a local named
``path`` for different endpoints and a textual scan silently conflates them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from superplane_auth.policy import (
    ENDPOINT_INVENTORY,
    InventoryError,
    Permission,
    required_permission,
)

_CLI = Path(__file__).resolve().parents[2] / "cli" / "adp-superplane.py"

API_PREFIX = "/superplane/v1"
PLACEHOLDER = "{x}"


def _normalize(path_template: str) -> str:
    """Collapse every path parameter to one placeholder.

    The inventory names its parameters (``{workspace}``, ``{deployment}``) and the
    CLI interpolates expressions, so the two are only comparable once parameter
    *names* are removed. Position and count still matter, which is what
    distinguishes ``/deployments`` from ``/deployments/{deployment}``.
    """
    out: list[str] = []
    depth = 0
    for char in path_template:
        if char == "{":
            if depth == 0:
                out.append(PLACEHOLDER)
            depth += 1
        elif char == "}":
            depth -= 1
        elif depth == 0:
            out.append(char)
    return "".join(out)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value = _render(node.value, {}, constants)
            if value is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value
    return constants


def _render(node: ast.expr, scope: dict[str, str], constants: dict[str, str]) -> str | None:
    """Render a path expression to a template, or ``None`` if it is not a path.

    Any interpolated value that is not itself a path fragment becomes the
    placeholder — that is exactly the path-parameter case.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return scope.get(node.id) or constants.get(node.id)
    if isinstance(node, ast.JoinedStr):
        rendered = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                rendered += part.value
            elif isinstance(part, ast.FormattedValue):
                inner = _render(part.value, scope, constants)
                rendered += inner if inner and inner.startswith("/") else PLACEHOLDER
        return rendered
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _render(node.left, scope, constants)
        right = _render(node.right, scope, constants)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.Call):
        # `query(path, params)` wraps a path in a querystring builder.
        if isinstance(node.func, ast.Name) and node.func.id == "query" and node.args:
            return _render(node.args[0], scope, constants)
    return None


def cli_endpoints() -> set[tuple[str, str]]:
    """Every ``(method, normalized path)`` the CLI calls under the domain prefix.

    Locals are resolved per function so a name reused across functions cannot
    leak; the CLI does reuse ``path`` and ``base`` this way.

    Nodes are visited in source order, not ``ast.walk`` order. ``ast.walk`` is
    breadth-first, so a function that assigns the same local twice on different
    branches -- as ``cost()`` does, one for the org route and one for the
    workspace route -- has both assignments visited before either ``request``
    call. Every call then resolves the name to whichever assignment came last in
    breadth-first order, so one endpoint is silently attributed to the other's
    path and the other is never recorded at all. Sorting by position makes each
    call see the assignment that actually precedes it.
    """
    tree = ast.parse(_CLI.read_text())
    constants = _module_constants(tree)
    found: set[tuple[str, str]] = set()

    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        scope: dict[str, str] = {}
        body = sorted(
            (n for n in ast.walk(function) if isinstance(n, ast.Assign | ast.Call)),
            key=lambda n: (n.lineno, n.col_offset),
        )
        for node in body:
            if isinstance(node, ast.Assign):
                rendered = _render(node.value, scope, constants)
                if rendered and "/" in rendered:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            scope[target.id] = rendered
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "request" and len(node.args) >= 2:
                method = _render(node.args[0], scope, constants)
                path = _render(node.args[1], scope, constants)
                if method and path and path.startswith(API_PREFIX):
                    found.add((method, _normalize(path)))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                # Both helpers forward their caller-selected path as a POST.
                # Resolve that argument at the callsite, not in the helper's
                # unrelated local scope.
                path_index = {"replay_safe_create": 2, "deployment_preview": 3}.get(node.func.id)
                if path_index is not None and len(node.args) > path_index:
                    path = _render(node.args[path_index], scope, constants)
                    if path and path.startswith(API_PREFIX):
                        found.add(("POST", _normalize(path)))
    return found


def inventory_endpoints() -> set[tuple[str, str]]:
    return {(method, _normalize(path)) for method, path in ENDPOINT_INVENTORY}


# --- the extractor works, so the comparisons below mean something ------------


def test_the_extractor_finds_a_plausible_surface():
    """A guard on the guard.

    Without it, a broken extractor returning the empty set would make the
    both-directions comparisons below pass trivially — an inventory test that
    silently stops testing is precisely the failure this file exists to prevent.

    A lower bound rather than an exact count, deliberately: adding an endpoint
    should fail the *completeness* test with a message naming the endpoint, not
    this one with "expected 18, got 19". The bound only has to be high enough that
    a broken extractor cannot clear it, and every HTTP method the surface uses has
    to be represented — a resolver that silently handled only plain string
    concatenation would find the ``GET``s and lose the interpolated ``DELETE``s.
    """
    endpoints = cli_endpoints()

    assert len(endpoints) >= 15
    assert {method for method, _ in endpoints} == {"GET", "POST", "PATCH", "DELETE"}


def test_the_extractor_resolves_a_local_alias_and_a_path_parameter():
    """The two constructions a naive scan gets wrong.

    ``DELETE .../deployments/{deployment}`` is built from a local ``base`` plus an
    interpolated name, so finding it proves both alias resolution and parameter
    collapsing work.
    """
    assert ("DELETE", f"{API_PREFIX}/workspaces/{PLACEHOLDER}/deployments/{PLACEHOLDER}") in cli_endpoints()


def test_the_extractor_distinguishes_two_locals_with_the_same_name():
    """``path`` means quota in one function and cost in another.

    Both must appear. A textual scan resolves the name once and loses one of them.
    """
    endpoints = cli_endpoints()

    assert ("PATCH", f"{API_PREFIX}/workspaces/{PLACEHOLDER}/quota") in endpoints
    assert ("GET", f"{API_PREFIX}/orgs/cost") in endpoints


def test_the_extractor_distinguishes_two_locals_of_the_same_name_in_one_function():
    """``cost()`` assigns ``path`` twice — once per branch — and both must survive.

    This is the within-function case, and it is the one ``ast.walk`` gets wrong:
    breadth-first order visits both assignments before either ``request`` call, so
    both calls resolve to the same branch's path. The org route would be recorded
    twice and the workspace route never, with no failure anywhere -- the inventory
    comparison would simply report a missing endpoint the client does call and a
    stale one it does.
    """
    endpoints = cli_endpoints()

    assert ("GET", f"{API_PREFIX}/orgs/cost") in endpoints
    assert ("GET", f"{API_PREFIX}/workspaces/{PLACEHOLDER}/cost") in endpoints


def test_normalizing_keeps_path_depth_significant():
    """Collapsing parameter names must not collapse two different endpoints."""
    assert _normalize("/a/{x}/b") != _normalize("/a/{x}/b/{y}")
    assert _normalize("/a/{workspace}") == _normalize("/a/{deployment}")


# --- completeness, in both directions ---------------------------------------


def test_every_endpoint_the_client_calls_has_a_required_permission():
    """R6 acc. 2. An endpoint added without a permission decision fails here."""
    missing = cli_endpoints() - inventory_endpoints()

    assert missing == set(), f"endpoints with no recorded permission: {sorted(missing)}"


UNCALLED_BY_DESIGN: set[tuple[str, str]] = set()


def test_the_inventory_has_no_endpoints_that_do_not_exist():
    """The other direction: a stale entry is drift too.

    A leftover entry is not a security hole, but it makes the inventory a
    less reliable description of the surface — and a reader who finds one wrong
    entry stops trusting the rest.
    """
    stale = inventory_endpoints() - cli_endpoints() - UNCALLED_BY_DESIGN

    assert stale == set(), f"inventory entries with no caller: {sorted(stale)}"


def test_the_uncalled_endpoints_are_still_inventoried_and_still_enforced():
    """The exemption must not become a way to drop an endpoint from the inventory.

    Each exempted entry has to be present with a real permission. Otherwise
    `UNCALLED_BY_DESIGN` would be a place to hide an endpoint that nobody decided,
    which is the state this file exists to make impossible.
    """
    for method, path in UNCALLED_BY_DESIGN:
        assert (method, path) in inventory_endpoints()
        assert isinstance(required_permission(method, path.replace(PLACEHOLDER, "{account}")), Permission)


def test_every_inventory_entry_resolves_through_the_public_lookup():
    """The lookup, not just the table, covers everything."""
    for method, path in ENDPOINT_INVENTORY:
        assert isinstance(required_permission(method, path), Permission)


def test_an_endpoint_outside_the_inventory_raises_rather_than_defaulting():
    """No default permission exists — permissive or restrictive.

    A permissive default makes a forgotten endpoint reachable; a restrictive one
    makes it silently dead and debugged as an outage. Both hide that nobody
    decided, so the lookup raises.
    """
    with pytest.raises(InventoryError) as failure:
        required_permission("POST", "/superplane/v1/workspaces/{workspace}/something-new")

    assert "undecided" in str(failure.value)


def test_the_lookup_is_case_insensitive_on_the_method_only():
    """Methods are upper-cased; paths are not touched.

    Lower-casing a path would make ``/Workspaces`` resolve, and path matching is
    case-sensitive in every router this will sit behind.
    """
    assert required_permission("get", "/superplane/v1/workspaces") is Permission.READ

    with pytest.raises(InventoryError):
        required_permission("GET", "/superplane/v1/Workspaces")


# --- the permissions assigned are the intended ones --------------------------


def test_no_endpoint_is_recorded_with_administer():
    """ADMINISTER governs the authorization records themselves, not domain calls.

    If a domain endpoint ever required it, every operator of a workspace would
    need the authority to change who else can reach it — which is the escalation
    the permission split exists to avoid.
    """
    assert Permission.ADMINISTER not in set(ENDPOINT_INVENTORY.values())


def test_every_mutating_endpoint_requires_more_than_read():
    """Execution and credential writes require more than the read boundary.

    Derived from the method rather than listed, so a new mutating endpoint is
    covered the moment it is added to the inventory. Approval requests resolve
    their target permission from the body in the domain ApprovalService; this
    organization-scoped inventory entry cannot grant approval or dispatch work.
    """
    for (method, path), permission in ENDPOINT_INVENTORY.items():
        if (method, path) == ("POST", "/superplane/v1/operation-approvals"):
            assert permission is Permission.READ
            continue
        if method in {"POST", "PATCH", "PUT", "DELETE"}:
            assert permission is not Permission.READ, f"{method} {path}"


def test_reads_require_only_read():
    """And the converse: no GET is gated behind a mutating permission.

    Kubeconfig used to be that exception. It is a ``POST`` now -- it mints
    credentials rather than returning a document -- so it is covered by the
    mutating-endpoint rule above and no ``GET`` exception remains. The assertion
    that it is still PROVISION stays, because the method changing must not
    quietly downgrade what it requires.
    """
    for (method, path), permission in ENDPOINT_INVENTORY.items():
        if method == "GET":
            assert permission is Permission.READ, f"{method} {path}"

    assert ENDPOINT_INVENTORY[("POST", "/superplane/v1/workspaces/{workspace}/kubeconfig")] is Permission.PROVISION


def test_credential_endpoints_require_the_credential_permission():
    """Registering or removing a provider credential is its own authority.

    Separate from PROVISION because R6 lists credential renewal as a distinct
    re-check point: a caller who may create capacity is not thereby allowed to
    rebind the credential that capacity runs on.
    """
    assert required_permission("POST", "/superplane/v1/vault/credentials") is Permission.RENEW_CREDENTIAL
    assert required_permission("DELETE", "/superplane/v1/vault/credentials/{credential}") is Permission.RENEW_CREDENTIAL


def test_deployment_and_quota_writes_require_spend():
    """Both are ways of spending money, and quota writes had no RBAC at all."""
    assert required_permission("POST", "/superplane/v1/workspaces/{workspace}/deployments") is Permission.SPEND
    assert required_permission("PATCH", "/superplane/v1/workspaces/{workspace}/quota") is Permission.SPEND
