""" "Who serves this person?" — Issue #4745 (#4692 · R4), §1.2, §1.4, §4.4, §6.3.

  E1  the ladder: user beats team beats org beats platform
  E2  §1.4 SETTLED — admin wins at the user rung, and the UI is TOLD so
  E3  an unusable destination is NO MATCH: the walk continues (§4.4)
  E4  the shadowed rung — what removing the winning rule would do
  E5  no mapping at all is an ANSWER (platform), not an absence
  E6  the walk order matches the resolver's, asserted against its own constant

E2 is the one with a settled ruling behind it. §1.4 chose "admin wins" over
"user wins" *and* required the UI to say so: a setting shown as active while
something else governs is the #4511 inert-config defect wearing a different hat.
So the response carries ``overrides_self_selection``, and these tests pin it.

E6 exists because this walk is a second implementation of the resolver's ladder —
deliberately, since the resolver takes a live ``TokenContext`` and this answers for a
person whose token the admin does not hold. Two implementations of one order is exactly
the drift §6.7 warns about for probes, so the shared invariant is asserted directly.
"""

from __future__ import annotations

import sqlalchemy as sa

from src.admin.bedrock_routing import service
from src.proxy.bedrock_routing import _MAPPING_RUNG_ORDER
from src.shared.models.bedrock_routing import BedrockDestinationRegistry

from .conftest import (
    ACME_ACCOUNT,
    ACME_DEST,
    MEMBER_ID,
    ORG_ID,
    PERSONAL_ACCOUNT,
    PERSONAL_DEST,
    PLATFORM_ACCOUNT,
    PLATFORM_DEST,
    TEAM_ID,
    UNVERIFIED_DEST,
    client_for,
    platform_admin_context,
    seed_mapping,
)


async def _effective(session, user_id: str = MEMBER_ID) -> dict:
    async with client_for(session, platform_admin_context()) as client:
        response = await client.get(f"/admin/bedrock-routing/effective/{user_id}")
    assert response.status_code == 200, response.text
    return response.json()


# ===========================================================================
# E1 — the ladder
# ===========================================================================


async def test_e1_the_org_rung_serves_a_person_with_no_narrower_rule(session, seeded):
    await seed_mapping(session, scope_type="org", destination_id=ACME_DEST, org_id=ORG_ID)

    result = await _effective(session)
    assert result["rung"] == "org"
    assert result["account_id"] == ACME_ACCOUNT


async def test_e1b_the_team_rung_beats_the_org_rung(session, seeded):
    await seed_mapping(session, scope_type="org", destination_id=ACME_DEST, org_id=ORG_ID)
    await seed_mapping(session, scope_type="team", destination_id=PLATFORM_DEST, org_id=ORG_ID, team_id=TEAM_ID)

    result = await _effective(session)
    assert result["rung"] == "team"
    assert result["account_id"] == PLATFORM_ACCOUNT


async def test_e1c_the_user_rung_beats_everything(session, seeded):
    await seed_mapping(session, scope_type="org", destination_id=ACME_DEST, org_id=ORG_ID)
    await seed_mapping(session, scope_type="team", destination_id=PLATFORM_DEST, org_id=ORG_ID, team_id=TEAM_ID)
    await seed_mapping(session, scope_type="user", destination_id=PERSONAL_DEST, user_id=MEMBER_ID)

    result = await _effective(session)
    assert result["rung"] == "user"
    assert result["account_id"] == PERSONAL_ACCOUNT


async def test_e1d_another_tenants_org_rule_does_not_serve_this_person(session, seeded):
    """The narrowest correct rule, not merely the narrowest rule that exists.

    A rule for a different org must not be picked up. Trivial to state, and the exact
    thing a walk written with a too-loose predicate gets wrong.
    """
    from .conftest import GLOBEX_DEST, OTHER_ORG_ID

    await seed_mapping(session, scope_type="org", destination_id=GLOBEX_DEST, org_id=OTHER_ORG_ID)

    result = await _effective(session)
    assert result["rung"] == "platform"
    assert result["account_id"] is None


async def test_e1e_a_same_named_team_in_another_tenant_does_not_match(session, seeded):
    """The (org, team) pair, not the team alone — the #4344 collision class.

    ``teams.id`` is unique only within its org, so a walk matching on the team id alone
    would route this person's traffic using an unrelated tenant's rule.
    """
    from .conftest import GLOBEX_DEST, OTHER_ORG_ID

    await seed_mapping(session, scope_type="team", destination_id=GLOBEX_DEST, org_id=OTHER_ORG_ID, team_id=TEAM_ID)

    result = await _effective(session)
    assert result["rung"] == "platform"


# ===========================================================================
# E2 — §1.4 SETTLED: admin wins, and says so
# ===========================================================================


async def test_e2_an_admin_authored_user_rule_reports_itself_as_the_override(session, seeded):
    """§1.4: the admin's rule wins, **and the display must state that it does.**

    ``overrides_self_selection`` is the flag the panel renders as "an admin has pinned
    this person". Without it the UI would show the person's credentials page setting as
    active while something else governed their traffic — the #4511 defect, one layer up.

    Derived, not stored: ``authored_by_user_id != scope_id_user`` is sufficient because
    both are canonical ``users.id`` in one namespace (#4647), so no migration is needed
    and a stored flag cannot drift from the ids it describes.
    """
    from .conftest import PLATFORM_ADMIN_ID

    await seed_mapping(session, scope_type="user", destination_id=ACME_DEST, user_id=MEMBER_ID, authored_by=PLATFORM_ADMIN_ID)

    result = await _effective(session)
    assert result["rung"] == "user"
    assert result["source"] == "platform_admin"
    assert result["overrides_self_selection"] is True


async def test_e2b_a_self_authored_user_rule_is_not_an_override(session, seeded):
    """The person's own selection wins the ladder too, but it overrides nobody.

    The asymmetry is the whole content of the flag: both rows sit on the user rung and
    both win, and only one of them is an admin reaching over somebody's own choice.
    """
    await seed_mapping(session, scope_type="user", destination_id=PERSONAL_DEST, user_id=MEMBER_ID, authored_by=MEMBER_ID)

    result = await _effective(session)
    assert result["rung"] == "user"
    assert result["source"] == "self"
    assert result["overrides_self_selection"] is False


async def test_e2c_a_team_rule_is_never_a_self_selection(session, seeded):
    """Team and org rungs have no self author by construction."""
    await seed_mapping(session, scope_type="team", destination_id=ACME_DEST, org_id=ORG_ID, team_id=TEAM_ID, authored_by=MEMBER_ID)

    result = await _effective(session)
    assert result["source"] == "platform_admin"
    assert result["overrides_self_selection"] is False


async def test_e2d_authoring_over_a_self_selection_flips_it_to_admin_authored(session, seeded, probe_ok):
    """The §1.4 transition, end to end through the API.

    The person selects their own destination; an admin then pins them. One row, and
    ``source`` flips — which is what makes "admin wins" observable rather than a claim
    in a design note.
    """
    await seed_mapping(session, scope_type="user", destination_id=PERSONAL_DEST, user_id=MEMBER_ID, authored_by=MEMBER_ID)
    assert (await _effective(session))["source"] == "self"

    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/user:{MEMBER_ID}", json={"destination_id": ACME_DEST})
    assert response.status_code == 200, response.text

    result = await _effective(session)
    assert result["source"] == "platform_admin"
    assert result["overrides_self_selection"] is True
    assert result["account_id"] == ACME_ACCOUNT


# ===========================================================================
# E3 — §4.4: an unusable destination is NO MATCH
# ===========================================================================


async def test_e3_an_unusable_user_rule_falls_through_to_the_team_rung(session, seeded):
    """§4.4: *"treat an unusable destination as NO MATCH and continue the walk."*

    Not "match then fail" — the distinction is the difference between a person whose
    broken personal destination costs them every request and one whose traffic falls
    back to their team's account. The lookup must agree with the request path here, or
    an admin debugging an outage is reading a different ladder than the one running.
    """
    await seed_mapping(session, scope_type="user", destination_id=UNVERIFIED_DEST, user_id=MEMBER_ID)
    await seed_mapping(session, scope_type="team", destination_id=ACME_DEST, org_id=ORG_ID, team_id=TEAM_ID)

    result = await _effective(session)
    assert result["rung"] == "team"
    assert result["account_id"] == ACME_ACCOUNT


async def test_e3b_a_destination_that_assumes_but_cannot_invoke_is_also_no_match(session, seeded):
    """Both halves of ``is_usable_for_routing`` are required, and they differ.

    An unverified destination has never been proven assumable; a non-routing-capable
    one assumes fine and cannot invoke Bedrock (§5.0). Checking only one lets the other
    through, so the flags are flipped independently here.
    """
    destination = await session.scalar(sa.select(BedrockDestinationRegistry).where(BedrockDestinationRegistry.id == ACME_DEST))
    destination.routing_capable = False
    await session.commit()

    await seed_mapping(session, scope_type="org", destination_id=ACME_DEST, org_id=ORG_ID)

    result = await _effective(session)
    assert result["rung"] == "platform"


async def test_e3c_every_rung_unusable_resolves_to_the_platform(session, seeded):
    """With nothing usable anywhere, the answer is the platform account.

    Ruling 1 forbids a *fallback* when a mapping resolves and fails at runtime; this is
    a different case — no mapping resolved at all, which is rung 4 by definition.
    """
    await seed_mapping(session, scope_type="user", destination_id=UNVERIFIED_DEST, user_id=MEMBER_ID)

    result = await _effective(session)
    assert result["rung"] == "platform"
    assert result["destination_id"] is None


# ===========================================================================
# E4 — the shadowed rung
# ===========================================================================


async def test_e4_the_winner_reports_what_it_shadows(session, seeded):
    """The mockup's *"(would otherwise be ml-research via the team rule for ml-team)"*.

    The ladder walk already holds every candidate, so this costs nothing extra, and it
    is what turns "remove this rule" from a guess into a known outcome — the reason
    §6.3 asks for the source rung at all.
    """
    await seed_mapping(session, scope_type="user", destination_id=PERSONAL_DEST, user_id=MEMBER_ID)
    await seed_mapping(session, scope_type="team", destination_id=PLATFORM_DEST, org_id=ORG_ID, team_id=TEAM_ID)

    result = await _effective(session)
    assert result["rung"] == "user"
    assert result["shadowed_rung"] == "team"
    assert result["shadowed_account_id"] == PLATFORM_ACCOUNT


async def test_e4b_the_only_rule_shadows_the_platform(session, seeded):
    """Removing the only rule falls through to the platform account, and says so."""
    await seed_mapping(session, scope_type="org", destination_id=ACME_DEST, org_id=ORG_ID)

    result = await _effective(session)
    assert result["shadowed_rung"] == "platform"
    assert result["shadowed_account_id"] is None


async def test_e4c_an_unusable_rung_is_not_reported_as_shadowed(session, seeded):
    """What is shadowed is what would actually serve, not merely what a row says.

    A broken rule between the winner and the next usable one is skipped by the walk, so
    reporting it as the fall-through would name a destination that cannot serve
    anything.
    """
    await seed_mapping(session, scope_type="user", destination_id=PERSONAL_DEST, user_id=MEMBER_ID)
    await seed_mapping(session, scope_type="team", destination_id=UNVERIFIED_DEST, org_id=ORG_ID, team_id=TEAM_ID)
    await seed_mapping(session, scope_type="org", destination_id=ACME_DEST, org_id=ORG_ID)

    result = await _effective(session)
    assert result["rung"] == "user"
    assert result["shadowed_rung"] == "org", "the unusable team rung must be skipped"
    assert result["shadowed_account_id"] == ACME_ACCOUNT


# ===========================================================================
# E5 / E6 — the platform answer, and parity with the resolver
# ===========================================================================


async def test_e5_no_mapping_is_an_answer_not_an_error(session, seeded):
    """Rung 4 is the absence of a mapping, and "platform" is what that reads as.

    Reported as a 200 with ``rung="platform"`` rather than a 404, because "nobody has
    written a rule for this person" is today's correct, ambient-IRSA behaviour — the
    same reason R2's resolver returns an explicit target rather than None.
    """
    result = await _effective(session)
    assert result["rung"] == "platform"
    assert result["account_id"] is None
    assert result["source"] is None
    assert result["overrides_self_selection"] is False


async def test_e5b_an_unknown_user_is_refused_rather_than_answered(session, seeded):
    """A bad id must not come back as a confident "platform".

    That answer would read as a resolution, and the admin would conclude the person has
    no rule when in fact they typed a Cognito sub or a GitHub login.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.get("/admin/bedrock-routing/effective/no-such-user")

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "scope_not_found"


def test_e6_the_walk_order_matches_the_resolvers():
    """One ladder, two implementations — so the order is asserted, not assumed.

    §6.7 makes this argument about probes ("two probes with different conditions is how
    'verified here, broken there' happens"); it holds just as well for two walks. If
    the resolver ever gains a rung, this fails and points at the walk that has to learn
    about it.
    """
    assert service.RUNG_ORDER == _MAPPING_RUNG_ORDER
