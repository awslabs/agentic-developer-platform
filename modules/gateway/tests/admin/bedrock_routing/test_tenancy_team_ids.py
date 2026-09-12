"""The team rung's ID NAMESPACE, end to end — Issue #4947 (#4692 × #4839 integration).

The panel's team picker used to be populated from the Cognito-derived group list
(``GET /admin/organizations/{org}/cognito/teams``, a scan for distinct
``custom:team_id`` attribute *values*), while the request path matches
``scope_id_team`` against ``users.team_id`` — a ``teams.id``. For an org built in the
tenancy admin console the two never met: the picker was empty, so the team rung could
not be authored at all.

Repointing the picker at the tenancy list fixes the *availability* half. This module
pins the half that a UI change cannot prove on its own: **that the id the tenancy list
hands an admin is the id a live request is matched on.** Both directions are asserted,
because each fails silently in its own way and neither is visible on screen:

  T1  a rule authored with a ``teams.id`` fires for a member of that team, resolved
      through the same claim a real JWT carries — the property the picker's whole
      value rests on
  T2  the id that reaches the claim is the PRIMARY team's, written by
      ``team_memberships`` rather than assumed — the pointer is a cache, and a test
      that set ``users.team_id`` by hand would prove nothing about the real writer.
      T2b then pins that the save-time gate and the resolver agree about which team
      is routable, in both directions
  T3  a rule authored with a team NAME (the shape a name-vs-id slip in the picker
      would produce) matches nobody — this is the defect class, asserted as a miss
      rather than trusted to be impossible
  T4  the save-time gate refuses a team nobody is on, which the tenancy list *can*
      offer where the old user-derived list could not by construction

T1 deliberately spans two modules that have no import between them: the admin write
path (``PUT /admin/bedrock-routing/mappings/team:<org>:<team>``) and
``BedrockRoutingResolver``. That gap is where the defect lived, so the test is written
across it — an assertion confined to either side would have passed throughout.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin import team_memberships
from src.admin.bedrock_routing import service
from src.proxy.bedrock_routing import BedrockRoutingResolver
from src.shared.models.organization import Department, Team, User

from .conftest import (
    ACME_ACCOUNT,
    ACME_DEST,
    MEMBER_ID,
    MEMBER_SUB,
    ORG_ID,
    client_for,
    context_for,
    platform_admin_context,
    seed_mapping,
)

# A tenancy `teams.id` shaped like the ones `new_uuid()` mints, and deliberately
# sharing no substring with the display name beside it: the whole defect class is an
# id/label confusion, so an assertion that could pass by matching either string would
# not notice it.
APP_DEV_TEAM_ID = "49470000-0000-4000-8000-0000000000a1"
APP_DEV_TEAM_NAME = "App-Dev"
# A second team the member joins non-primarily (T2b).
SECOND_TEAM_ID = "49470000-0000-4000-8000-0000000000b2"
SECOND_TEAM_NAME = "Platform-admin"
# A team the tenancy list offers and nobody has joined (T4).
EMPTY_TEAM_ID = "49470000-0000-4000-8000-0000000000c3"
EMPTY_TEAM_NAME = "Data-science"


@pytest.fixture
async def tenancy_teams(session: AsyncSession, seeded) -> None:
    """Two tenancy-model teams in ``ORG_ID``, and one member on the first.

    Built the way the #4841 admin console builds them — ``departments`` + ``teams``
    rows, and membership through ``team_memberships.add_membership`` — rather than by
    writing ``users.team_id`` directly. That distinction is the point of T2: the
    pointer the claim is projected from is a *cache* of the membership table, so a
    fixture that set it by hand would assert the resolver against a value no
    production writer produces.

    ``seeded`` leaves every user on the legacy ``TEAM_ID`` string, which names no
    ``teams`` row — exactly the pre-tenancy state an org is migrated out of.
    """
    session.add(Department(id="dept-4947-eng", org_id=ORG_ID, name="Engineering"))
    session.add(Team(id=APP_DEV_TEAM_ID, org_id=ORG_ID, department_id="dept-4947-eng", name=APP_DEV_TEAM_NAME))
    session.add(Team(id=SECOND_TEAM_ID, org_id=ORG_ID, department_id="dept-4947-eng", name=SECOND_TEAM_NAME))
    session.add(Team(id=EMPTY_TEAM_ID, org_id=ORG_ID, department_id="dept-4947-eng", name=EMPTY_TEAM_NAME))
    await session.flush()

    await team_memberships.add_membership(session, user_id=MEMBER_ID, team_id=APP_DEV_TEAM_ID, org_id=ORG_ID, is_primary=True)
    await session.commit()


async def _author_team_rule(session: AsyncSession, team_id: str, destination_id: str = ACME_DEST):
    async with client_for(session, platform_admin_context()) as client:
        return await client.put(f"/admin/bedrock-routing/mappings/team:{ORG_ID}:{team_id}", json={"destination_id": destination_id})


async def _resolve_for_member(session: AsyncSession):
    """Resolve as the member's own live request would.

    The context is built from ``users.team_id`` as it stands in the database — the
    value the Cognito pre-token Lambda projects into ``custom:team_id`` and
    ``auth/cognito_jwt.py`` reads back — and ``user_id`` is the COGNITO SUB, as it is
    on the real JWT path. Hard-coding the team id here instead would make this test
    agree with itself while production disagreed.
    """
    session.expire_all()
    member = await session.get(User, MEMBER_ID)
    context = context_for(MEMBER_SUB, org_id=member.org_id, team_id=member.team_id)
    return await BedrockRoutingResolver().resolve(session, context)


# ===========================================================================
# T1 — the authored id is the matched id
# ===========================================================================


async def test_t1_a_rule_authored_with_a_teams_id_serves_that_team_s_member(session, tenancy_teams, probe_ok):
    """The integration the picker's value depends on.

    An admin picks "App-Dev" from the tenancy list; the rule stores that team's
    ``teams.id``; a member of App-Dev makes a call and is routed by it. Before #4947
    the picker could not offer this team at all, so this path had never been walked.
    """
    response = await _author_team_rule(session, APP_DEV_TEAM_ID)
    assert response.status_code == 200, response.text

    target = await _resolve_for_member(session)

    assert target.rung == "team"
    assert target.account_id == ACME_ACCOUNT


async def test_t1b_the_stored_scope_id_is_the_teams_id_verbatim(session, tenancy_teams, probe_ok):
    """No normalisation, no translation, no shim between the two namespaces.

    Asserted on the stored column because the fix rests on the claim that these are
    *one* namespace. If a mapping ever needed rewriting on the way in, the picker's
    contract would be a coincidence rather than a design property.
    """
    await _author_team_rule(session, APP_DEV_TEAM_ID)

    stored = await service.load_mapping_for_scope(session, "team", ORG_ID, APP_DEV_TEAM_ID, None)
    assert stored is not None
    assert stored.scope_id_team == APP_DEV_TEAM_ID


# ===========================================================================
# T2 — the claim carries the PRIMARY team's id, written by the real writer
# ===========================================================================


async def test_t2_the_membership_write_points_users_team_id_at_the_teams_id(session, tenancy_teams):
    """``users.team_id`` — and therefore ``custom:team_id`` — holds a ``teams.id``.

    This is the link in the chain the issue asked to verify: the picker's namespace is
    correct only because this pointer is a ``teams.id``. Asserted against what
    ``team_memberships.add_membership`` actually wrote, so a future change to the
    pointer's contents fails here rather than silently making every team rule inert.
    """
    session.expire_all()
    member = await session.get(User, MEMBER_ID)
    assert member.team_id == APP_DEV_TEAM_ID


async def test_t2b_a_team_that_is_nobody_s_primary_is_refused_and_would_not_have_matched(session, tenancy_teams, probe_ok):
    """Multi-team membership routes on the PRIMARY only — and the gate agrees.

    ``TeamMembership`` makes a person a member of many teams while ``custom:team_id``
    stays single-valued: it carries the *primary*. So a rule on a team the member holds
    only as a secondary membership can never match one of their requests, and the
    save-time gate refuses it — ``require_scope_exists`` looks for a ``users.team_id``
    carrying the pair, which is the primary pointer.

    Both halves are asserted, because the pair is the property that matters. If the
    gate accepted what the resolver cannot match, the panel would happily store a rule
    that governs nobody (#4511); if the gate refused something the resolver *would*
    match, an admin could not author a rule that works. Agreement in both directions
    is what makes the refusal a correct answer rather than an obstacle.

    This is also the one thing an admin picking from the tenancy list should know: the
    list offers every team in the org, and a team nobody holds as their primary is not
    a routable population.
    """
    await team_memberships.add_membership(session, user_id=MEMBER_ID, team_id=SECOND_TEAM_ID, org_id=ORG_ID)
    await session.commit()

    # Half 1: the gate refuses it, naming the reason rather than storing it inert.
    response = await _author_team_rule(session, SECOND_TEAM_ID)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "scope_not_found"

    # Half 2: had it been stored, it would have matched nothing — so the refusal above
    # withheld a rule that governs nobody, which is exactly what it is for. Seeded
    # around the gate on purpose; that is the only way to observe the counterfactual.
    await seed_mapping(session, scope_type="team", destination_id=ACME_DEST, org_id=ORG_ID, team_id=SECOND_TEAM_ID)
    target = await _resolve_for_member(session)
    assert target.rung == "platform"


# ===========================================================================
# T3 — the name namespace matches nobody
# ===========================================================================


async def test_t3_a_rule_authored_with_a_team_name_matches_nobody(session, tenancy_teams, probe_ok):
    """The defect class, asserted as the miss it is.

    A picker submitting its display label instead of its value would store this row.
    It saves cleanly if the gate lets it through, reads back in the rules table as
    configured routing, and fires for no request ever — the #4511 inert-config class
    on the surface that decides whose bill pays. So the miss is asserted rather than
    assumed away by the picker's implementation.
    """
    # Refused at write time: no member carries the NAME as their team pointer, so the
    # #4696 existence gate catches the slip before it is stored. That gate is the
    # server-side control; the picker submitting `teams.id` is why an admin never
    # provokes it.
    response = await _author_team_rule(session, APP_DEV_TEAM_NAME)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "scope_not_found"

    # And the counterfactual the gate withheld: seeded around it, such a row matches
    # nothing — the member's claim carries the id, never the name. Asserted rather than
    # left to the gate alone, because "the gate happens to refuse it today" is a weaker
    # guarantee than "it could not have worked".
    await seed_mapping(session, scope_type="team", destination_id=ACME_DEST, org_id=ORG_ID, team_id=APP_DEV_TEAM_NAME)
    target = await _resolve_for_member(session)
    assert target.rung == "platform"


# ===========================================================================
# T4 — a team nobody is on is refused
# ===========================================================================


async def test_t4_a_team_with_no_members_is_refused(session, tenancy_teams, probe_ok):
    """The one behaviour the repointed picker newly exposes.

    The old Cognito-derived list was built *from users*, so every team it offered had
    a member by construction. The tenancy list is built from the ``teams`` table and
    can legitimately offer a team nobody has joined yet — which
    ``require_scope_exists`` refuses, because such a rule would govern nobody.

    The refusal is correct and stays. What #4947 adds is the panel disclosing the
    condition next to the picker, so an admin meets it as a stated rule rather than as
    a surprise 422 after a real assume-role probe.
    """
    response = await _author_team_rule(session, EMPTY_TEAM_ID)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["detail"]["reason"] == "scope_not_found"
    assert EMPTY_TEAM_ID in body["detail"]["message"]
