"""Who may author a routing rule — Issue #4745 (#4692 · R4).

**These come first, before any happy path**, because the whole point of the unit is
who decides whose AWS account is billed for a principal's model calls (design ruling
4, §6.5):

  A1  every route, org admin        -> 403, nothing written
  A2  every route, plain member     -> 403, nothing written
  A3  a denial must not leak state it refused to show
  A4  authority is the FIRST statement, so 403 beats 422 on a bad path too
  A5  no route in the module lacks the gate (source-level, fail-closed)

A1 is the assertion the dev environment cannot make. ``bedrock-routing-validate.sh``
says so outright: dev has no real org_admin identity, so *"the org_admin case is
provable cheaply only in the backend test suite with a synthetic org_admin context, and
that test is required at PR time."* This file is that requirement.

The org admin here is a real ``org_admin`` in ``tenant_memberships`` who legitimately
holds their own org's permissions. That is what makes the denial meaningful rather than
incidental: a partition check like ``check_permission(..., target_org_id=<their own
org>)`` would pass for this caller. Only a claim about the caller —
``require_platform_admin``, which excludes ``org_admin`` by construction — denies them.
"""

from __future__ import annotations

import inspect

import pytest

from src.admin.bedrock_routing import routes as routes_module

from .conftest import (
    ACME_DEST,
    MEMBER_ID,
    ORG_ID,
    TEAM_ID,
    client_for,
    member_context,
    org_admin_context,
    seed_mapping,
    stored_mappings,
)

#: Every route on the surface, as (method, path, body). Parameterised as one list so a
#: route added later without a gate fails A1/A2 rather than quietly going untested.
ALL_ROUTES = [
    ("GET", "/admin/bedrock-routing/mappings", None),
    ("PUT", f"/admin/bedrock-routing/mappings/org:{ORG_ID}", {"destination_id": ACME_DEST}),
    ("DELETE", f"/admin/bedrock-routing/mappings/org:{ORG_ID}", None),
    ("GET", f"/admin/bedrock-routing/effective/{MEMBER_ID}", None),
    ("GET", "/admin/bedrock-routing/destinations", None),
    ("POST", "/admin/bedrock-routing/destinations", {"source": "connection", "credential_id": "cred-acme-org"}),
    ("POST", f"/admin/bedrock-routing/destinations/{ACME_DEST}/verify", None),
]


async def _call(client, method: str, path: str, body: dict | None):
    if method == "GET":
        return await client.get(path)
    if method == "DELETE":
        return await client.delete(path)
    if method == "PUT":
        return await client.put(path, json=body)
    return await client.post(path, json=body)


# ===========================================================================
# A1 — the org admin. The assertion dev cannot make.
# ===========================================================================


@pytest.mark.parametrize(("method", "path", "body"), ALL_ROUTES, ids=lambda v: str(v))
async def test_a1_org_admin_is_denied_on_every_route(session, seeded, probe_ok, method, path, body):
    """An org admin is denied on every route, and their attempt writes nothing.

    Design ruling 4 removed the org-admin rung and §6.5 makes it a deliberate
    non-goal. The reason is an authority inversion: a user-rung mapping names a person
    who may work in several tenants, so an org admin authoring one would reach into
    tenants they have no membership in, cannot see, and cannot be audited by.

    Reads are denied too, not only writes — the destination list spans tenants (ruling
    4b), so showing it to an org admin discloses which AWS accounts other tenants have
    linked.
    """
    async with client_for(session, org_admin_context()) as client:
        response = await _call(client, method, path, body)

    assert response.status_code == 403, f"{method} {path} -> {response.status_code}: {response.text}"
    assert await stored_mappings(session) == [], "a denied request must write nothing"


async def test_a1b_org_admin_denial_holds_for_their_own_org(session, seeded, probe_ok):
    """ "It's my own org" is not a softer door.

    Written separately because same-tenant is the intuitive exception someone would
    add, and it is exactly the one the ruling rejects. The scope here names the org
    admin's OWN organization and the destination is legitimately linked to it — every
    ownership gate in the module would pass. The refusal comes from the caller's
    authority alone.
    """
    async with client_for(session, org_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})

    assert response.status_code == 403, response.text
    assert await stored_mappings(session) == []


async def test_a1c_org_admin_cannot_author_for_a_member_of_their_own_team(session, seeded, probe_ok):
    """Nor for a team they administer, nor for one of their own members."""
    async with client_for(session, org_admin_context()) as client:
        team = await client.put(f"/admin/bedrock-routing/mappings/team:{ORG_ID}:{TEAM_ID}", json={"destination_id": ACME_DEST})
        user = await client.put(f"/admin/bedrock-routing/mappings/user:{MEMBER_ID}", json={"destination_id": ACME_DEST})

    assert team.status_code == 403, team.text
    assert user.status_code == 403, user.text
    assert await stored_mappings(session) == []


# ===========================================================================
# A2 — the plain member
# ===========================================================================


@pytest.mark.parametrize(("method", "path", "body"), ALL_ROUTES, ids=lambda v: str(v))
async def test_a2_member_is_denied_on_every_route(session, seeded, probe_ok, method, path, body):
    """A plain member is denied everywhere, including on their own user rung.

    §6.4 gives a person a self-service selector on their *credentials* page; this is
    not it. A user-rung rule authored here is the admin override (§1.4 "admin wins"),
    so a member reaching this route would be granting themselves the authority the
    override exists to hold over them.
    """
    async with client_for(session, member_context()) as client:
        response = await _call(client, method, path, body)

    assert response.status_code == 403, f"{method} {path} -> {response.status_code}: {response.text}"
    assert await stored_mappings(session) == []


async def test_a2b_member_cannot_author_their_own_user_rung(session, seeded, probe_ok):
    """Explicitly: not even for themselves, through this surface."""
    async with client_for(session, member_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/user:{MEMBER_ID}", json={"destination_id": ACME_DEST})

    assert response.status_code == 403, response.text
    assert await stored_mappings(session) == []


# ===========================================================================
# A3 — a denial must not leak
# ===========================================================================


async def test_a3_denied_reads_leak_no_routing_state(session, seeded, probe_ok):
    """A 403 must not disclose the accounts, labels or rules it refused to show.

    The destination registry spans tenants (ruling 4b), so leaking a label or an
    account number in a denial body would turn the 403 into the disclosure it exists
    to prevent.
    """
    await seed_mapping(session, scope_type="org", destination_id=ACME_DEST, org_id=ORG_ID)

    async with client_for(session, org_admin_context()) as client:
        mappings = await client.get("/admin/bedrock-routing/mappings")
        destinations = await client.get("/admin/bedrock-routing/destinations")
        effective = await client.get(f"/admin/bedrock-routing/effective/{MEMBER_ID}")

    for response in (mappings, destinations, effective):
        assert response.status_code == 403
        for secret in ("acme-prod", "111111114821", "globex-prod", "222222227733"):
            assert secret not in response.text, f"a denial leaked {secret}"


# ===========================================================================
# A4 — authority before parsing
# ===========================================================================


async def test_a4_authority_precedes_scope_parsing(session, seeded, probe_ok):
    """A malformed scope from a non-admin gets 403, not 422.

    The ordering is the security property, not tidiness: if parsing ran first, the
    difference between 422 (this scope is malformed) and 403 (you may not) would tell
    an unauthorised caller which scope strings and which ids the platform recognises —
    an existence oracle over every org, team and user. Hence the gate is the first
    statement in every handler.
    """
    async with client_for(session, member_context()) as client:
        malformed = await client.put("/admin/bedrock-routing/mappings/nonsense", json={"destination_id": ACME_DEST})
        unknown_user = await client.get("/admin/bedrock-routing/effective/no-such-user-id")
        unknown_dest = await client.post("/admin/bedrock-routing/destinations/no-such-destination/verify")

    assert malformed.status_code == 403, malformed.text
    assert unknown_user.status_code == 403, unknown_user.text
    # 403 rather than the 404 an authorised caller would get: whether a destination
    # exists is itself platform state.
    assert unknown_dest.status_code == 403, unknown_dest.text


# ===========================================================================
# A5 — no route may lack the gate (fail closed as the module grows)
# ===========================================================================


def test_a5_every_route_handler_calls_require_platform_admin():
    """Read the module's own source: no handler may be missing the gate.

    The parameterised tests above cover the routes that exist today. This one covers
    the route somebody adds next year: a handler registered without
    ``require_platform_admin`` would be an unauthenticated-by-omission surface onto
    whose-bill-pays, and nothing else in the suite would notice.

    Asserted on source text rather than by calling, because the failure being guarded
    is an *absent* call, and you cannot exercise a call that is not there.
    """
    for route in routes_module.router.routes:
        source = inspect.getsource(route.endpoint)
        assert "require_platform_admin(current_user)" in source, f"{route.path} has no platform-admin gate"


def test_a5b_the_gate_is_the_first_statement_in_every_handler():
    """And it is the first statement, so the A4 ordering property cannot regress.

    Checks that the gate appears before any ``await`` in each handler body — an
    ordering a future edit could break silently while every 403 test above still
    passed, because the difference only shows on inputs that are *also* invalid.
    """
    for route in routes_module.router.routes:
        body = inspect.getsource(route.endpoint).split('"""')[-1]
        gate = body.find("require_platform_admin(current_user)")
        assert gate != -1, f"{route.path} has no platform-admin gate"
        first_await = body.find("await ")
        if first_await != -1:
            assert gate < first_await, f"{route.path} does work before checking authority"
