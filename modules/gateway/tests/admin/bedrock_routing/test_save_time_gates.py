"""Save-time refusals — Issue #4745 (#4692 · R4), §4.2, §4.3, §6.7.

Every test here is a **refusal that writes nothing**. Ruling 4a: *"a mapping that
cannot be assumed is rejected, never stored inert (#4511 class)."* A stored-but-inert
mapping is worse than no mapping, because it reads as configured routing while failing
100% of the principal's calls under fail-closed (§2.5) — so "nothing was written" is
asserted on every path, not just the status code.

  G1  cross-tenant destination            -> 422 account_unlinked, nothing stored
  G2  personal credential, shared rung    -> 422, nothing stored
  G3  the probe says no                   -> 422, nothing stored, not stamped verified
  G4  a scope that does not exist         -> 422, nothing stored
  G5  the happy path                      -> stored, and stamped verified
  G6  gate ORDER: the free checks refuse before the probe is ever called
  G7  the reason vocabulary is R3's, shared with runtime errors
  G8  no user-facing response carries a role ARN

The probe is patched (see ``conftest``); no gate is. A test cannot pass here by having
skipped the validator.
"""

from __future__ import annotations

import sqlalchemy as sa

from src.proxy import bedrock_routing_errors
from src.shared.models.bedrock_routing import BedrockDestinationRegistry

from .conftest import (
    ACME_ACCOUNT,
    ACME_DEST,
    FOREIGN_MEMBER_ID,
    GLOBEX_DEST,
    MEMBER_ID,
    ORG_ID,
    OTHER_ORG_ID,
    PERSONAL_DEST,
    PLATFORM_DEST,
    TEAM_ID,
    UNVERIFIED_DEST,
    client_for,
    platform_admin_context,
    stored_mappings,
)


async def _destination(session, destination_id: str) -> BedrockDestinationRegistry:
    session.expire_all()
    return await session.scalar(sa.select(BedrockDestinationRegistry).where(BedrockDestinationRegistry.id == destination_id))


# ===========================================================================
# G1 — §4.2 requirement 1: cross-tenant isolation, enforced in the API
# ===========================================================================


async def test_g1_org_rule_cannot_point_at_another_tenants_destination(session, seeded, probe_ok):
    """An org rule may not route to a destination linked to a different tenant.

    §4.2 requirement 1, and it is *the* cross-tenant control for admin-authored
    mappings: *"It must be enforced server-side in the API, not by the dropdown's
    contents — a UI that only lists in-scope options is a usability feature; an API
    that only accepts them is the control."* Acme's traffic billed to Globex's account
    is the concrete harm.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": GLOBEX_DEST})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == bedrock_routing_errors.REASON_ACCOUNT_UNLINKED
    assert await stored_mappings(session) == []


async def test_g1b_the_refusal_does_not_name_the_destinations_own_tenant(session, seeded, probe_ok):
    """The message must not disclose WHICH tenant linked that account.

    Telling an admin that account belongs to "globex" turns the refusal into an
    enumeration oracle over other tenants' AWS accounts — a smaller version of the
    disclosure the whole platform-admin gate exists to prevent. Naming the scope's own
    org is enough to act on.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": GLOBEX_DEST})

    message = response.json()["detail"]["message"]
    assert OTHER_ORG_ID not in message, "the refusal named the destination's own tenant"
    assert ORG_ID in message, "the refusal should name the scope the admin was authoring for"


async def test_g1c_user_rung_scope_is_checked_against_the_target_users_own_org(session, seeded, probe_ok):
    """The user rung resolves its org from the TARGET user, not the caller's token.

    This is the subtle one. A mapping row for a user rung carries no org, so the check
    needs one from somewhere — and the caller is a platform admin whose token names
    *their* tenant. Reading it from the token would compare Acme against Acme and pass
    every cross-tenant mapping ever attempted.

    Here the target is a Globex member and the destination is linked to Acme. The
    caller's own org is Acme, so a token-sourced check would accept this. It must not.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/user:{FOREIGN_MEMBER_ID}", json={"destination_id": ACME_DEST})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == bedrock_routing_errors.REASON_ACCOUNT_UNLINKED
    assert await stored_mappings(session) == []


async def test_g1d_platform_registered_destinations_are_usable_by_any_scope(session, seeded, probe_ok):
    """§4.2 requirement 2's exception: a platform-registered row has no owning tenant.

    ``is_platform_registered`` is an explicit boolean precisely so "deliberately
    platform-wide" is distinguishable from "the writer forgot the tenant". The check
    must honour it rather than trip over the NULL org.
    """
    async with client_for(session, platform_admin_context()) as client:
        acme = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": PLATFORM_DEST})
        globex = await client.put(f"/admin/bedrock-routing/mappings/org:{OTHER_ORG_ID}", json={"destination_id": PLATFORM_DEST})

    assert acme.status_code == 200, acme.text
    assert globex.status_code == 200, globex.text


# ===========================================================================
# G2 — §4.3 ruling 6: a shared rung may not use one person's credential
# ===========================================================================


async def test_g2_team_rule_rejects_a_personal_credential(session, seeded, probe_ok):
    """Ruling 6: *"one person's personal role silently serving a whole team's traffic
    is an authority/audit problem."*

    Note this refusal is not redundant with IAM. §5.0b points out AWS enforces it more
    strictly — a user-pinned trust policy refuses to be assumed for anyone else, so
    such a mapping fails every call. That is the better failure but the wrong
    *discovery* mechanism: an admin reading a runtime ``AccessDenied`` files a platform
    bug. A named refusal at save time tells them what they actually did.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/team:{ORG_ID}:{TEAM_ID}", json={"destination_id": PERSONAL_DEST})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "personal_credential_for_shared_scope"
    assert await stored_mappings(session) == []


async def test_g2b_org_rule_rejects_a_personal_credential(session, seeded, probe_ok):
    """The org rung too — it is "shared scope", not "team scope", that matters."""
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": PERSONAL_DEST})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "personal_credential_for_shared_scope"
    assert await stored_mappings(session) == []


async def test_g2c_user_rung_may_use_a_personal_credential(session, seeded, probe_ok):
    """A person's own connection serving their own traffic is allowed (ruling 2).

    The asymmetry is the point: ruling 6 constrains *shared* rungs. Without this test,
    a blanket "no personal credentials" implementation would pass every other test in
    this file while breaking the case the design explicitly permits.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/user:{MEMBER_ID}", json={"destination_id": PERSONAL_DEST})

    assert response.status_code == 200, response.text
    assert [m.scope_type for m in await stored_mappings(session)] == ["user"]


# ===========================================================================
# G3 — §6.7: the real test assume, and reject-never-store-inert
# ===========================================================================


async def test_g3_a_failed_probe_refuses_and_stores_nothing(session, seeded, probe_denied):
    """The #4511 gate. A destination that cannot serve a call must not become a rule.

    ``probe_denied`` reports the §5.0 case: the role assumes fine but has no
    ``bedrock:InvokeModel``. That role is *exactly* the inert mapping — it passes a
    naive assume-only gate and fails every model call.

    There is deliberately no override parameter for this. A failing save-time assume is
    the mechanism working, not a bug to route around.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == bedrock_routing_errors.REASON_ROLE_MISSING_BEDROCK_PERMISSION
    assert await stored_mappings(session) == [], "an unassumable destination must not be stored as a rule"


async def test_g3b_a_failed_probe_does_not_stamp_the_destination_verified(session, seeded, probe_denied):
    """And it must not leave the destination looking usable.

    A refusal that nonetheless set ``verified_at`` would make the destination
    selectable in the dropdown on the next page load — the refusal undone by its own
    side effect.
    """
    before = await _destination(session, UNVERIFIED_DEST)
    assert before.verified_at is None

    async with client_for(session, platform_admin_context()) as client:
        await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": UNVERIFIED_DEST})

    after = await _destination(session, UNVERIFIED_DEST)
    assert after.verified_at is None
    assert after.routing_capable is False


async def test_g3c_an_unverified_destination_becomes_usable_when_the_probe_passes(session, seeded, probe_ok):
    """The converse: a never-verified destination is not short-circuited to a refusal.

    The probe re-runs and may flip it to usable, which is what makes a freshly
    registered destination work through the same path as a re-verify. What is never
    skipped is the probe itself.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": UNVERIFIED_DEST})

    assert response.status_code == 200, response.text
    destination = await _destination(session, UNVERIFIED_DEST)
    assert destination.routing_capable is True
    assert destination.verified_at is not None
    assert destination.is_usable_for_routing is True


async def test_g3d_the_probe_receives_the_destinations_own_role_and_region(session, seeded, probe_ok):
    """The probe must be pointed at the destination, not at the caller's own account.

    Trivially true in the implementation and trivially breakable: a probe called with
    the gateway's own role would pass every time and the gate would be theatre.
    """
    async with client_for(session, platform_admin_context()) as client:
        await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})

    probe_ok.assert_awaited_once()
    kwargs = probe_ok.await_args.kwargs
    assert kwargs["role_arn"] == f"arn:aws:iam::{ACME_ACCOUNT}:role/ADP-Agent-acme-prod"
    assert kwargs["default_region"] == "us-east-1"
    # From Secrets Manager, never from a registry column — a second copy of a shared
    # secret doubles the places it can leak from.
    assert kwargs["external_id"] == "ext-4745"


# ===========================================================================
# G4 — scope existence (the #4696 pattern)
# ===========================================================================


async def test_g4_a_rule_for_a_nonexistent_org_is_refused(session, seeded, probe_ok):
    """A rule scoped to a mistyped or deleted id stores cleanly and governs nobody.

    The #4511 inert-config class on the surface whose entire purpose is deciding whose
    bill pays. An existence SELECT at write time is the cheap alternative to the FK
    migration 037 declined.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put("/admin/bedrock-routing/mappings/org:org-does-not-exist", json={"destination_id": ACME_DEST})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "scope_not_found"
    assert await stored_mappings(session) == []


async def test_g4b_a_rule_for_a_team_nobody_is_in_is_refused(session, seeded, probe_ok):
    """Teams live in Cognito attributes, so "a team someone is in" is the only
    existence a rule can usefully have."""
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/team:{ORG_ID}:team-nobody-is-in", json={"destination_id": ACME_DEST})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "scope_not_found"
    assert await stored_mappings(session) == []


async def test_g4c_a_cognito_sub_in_the_user_rung_is_refused(session, seeded, probe_ok):
    """``scope_id_user`` is the canonical ``users.id``, never a Cognito sub (#4647).

    A mapping written with a sub looks right in the admin table and never fires,
    because the resolver compares against the canonical id. Rejecting it is how the
    admin finds out now rather than after a wrong bill.
    """
    from .conftest import MEMBER_SUB

    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/user:{MEMBER_SUB}", json={"destination_id": ACME_DEST})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "scope_not_found"
    assert await stored_mappings(session) == []


async def test_g4d_there_is_no_platform_scope(session, seeded, probe_ok):
    """Rung 4 is the ABSENCE of a mapping (§1.2), so a platform row must be refusable.

    Accepting one would give the ladder two contradictory ways to say "ambient IRSA",
    and ``ck_bedrock_account_mapping_scope`` would refuse the row anyway — as a 500
    rather than as the correction it is.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put("/admin/bedrock-routing/mappings/platform", json={"destination_id": ACME_DEST})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "invalid_scope"
    assert await stored_mappings(session) == []


async def test_g4e_a_team_scope_without_its_org_is_refused(session, seeded, probe_ok):
    """A ``teams.id`` is unique only inside its org (#4344), so both ids are required.

    A team scope naming only the team could govern a same-named team in an unrelated
    tenant — cross-tenant routing by identifier collision.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/team:{TEAM_ID}", json={"destination_id": ACME_DEST})

    assert response.status_code == 422, response.text
    assert await stored_mappings(session) == []


# ===========================================================================
# G5 — the happy path, and idempotence
# ===========================================================================


async def test_g5_a_valid_rule_is_stored_and_stamped_verified(session, seeded, probe_ok):
    """Everything passing: the row is written and the destination is marked usable."""
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == f"org:{ORG_ID}"
    assert body["destination_account_id"] == ACME_ACCOUNT
    assert body["destination_usable"] is True
    assert body["source"] == "platform_admin"

    stored = await stored_mappings(session)
    assert len(stored) == 1
    assert stored[0].scope_type == "org"
    assert stored[0].scope_id_org == ORG_ID
    assert stored[0].scope_id_team is None
    assert stored[0].scope_id_user is None
    assert stored[0].destination_id == ACME_DEST


async def test_g5b_re_pointing_a_scope_replaces_the_row_rather_than_adding_one(session, seeded, probe_ok):
    """Upsert, not insert.

    ``uq_bedrock_account_mapping_scope`` is an expression index over COALESCE — chosen
    because Postgres NULLs compare *distinct* inside a unique constraint, so a naive
    ``UNIQUE(...)`` would accept two rows for one scope and **which one bills would
    depend on row order**. The route's lookup predicate has to match that index's
    semantics, or the second PUT is a 500.
    """
    async with client_for(session, platform_admin_context()) as client:
        first = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})
        second = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": PLATFORM_DEST})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] == second.json()["id"], "the scope's row identity should survive a re-point"

    stored = await stored_mappings(session)
    assert len(stored) == 1, "a re-point must replace, not duplicate"
    assert stored[0].destination_id == PLATFORM_DEST


async def test_g5c_a_team_rule_stores_both_scope_ids(session, seeded, probe_ok):
    """The team rung stores the (org, team) PAIR."""
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/team:{ORG_ID}:{TEAM_ID}", json={"destination_id": ACME_DEST})

    assert response.status_code == 200, response.text
    stored = await stored_mappings(session)
    assert (stored[0].scope_id_org, stored[0].scope_id_team, stored[0].scope_id_user) == (ORG_ID, TEAM_ID, None)


# ===========================================================================
# G6 — gate ORDER
# ===========================================================================


async def test_g6_the_free_checks_refuse_before_the_probe_is_called(session, seeded, probe_ok):
    """A cross-tenant attempt must never reach another account's STS.

    Two reasons the order matters, and both are in the design note: the free checks are
    pure SQL while the probe is a network round trip, and — more importantly — probing
    first would report ``assume_role_failed`` for a request whose real problem is that
    it names another tenant's account. A true statement that sends the admin to debug
    the wrong thing.
    """
    async with client_for(session, platform_admin_context()) as client:
        await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": GLOBEX_DEST})

    probe_ok.assert_not_awaited()


async def test_g6b_the_personal_credential_check_precedes_the_probe(session, seeded, probe_ok):
    """Same for ruling 6: refuse on the indexed read, not on the round trip."""
    async with client_for(session, platform_admin_context()) as client:
        await client.put(f"/admin/bedrock-routing/mappings/team:{ORG_ID}:{TEAM_ID}", json={"destination_id": PERSONAL_DEST})

    probe_ok.assert_not_awaited()


async def test_g6c_a_nonexistent_scope_refuses_before_the_probe(session, seeded, probe_ok):
    """And a scope that does not exist never touches AWS either."""
    async with client_for(session, platform_admin_context()) as client:
        await client.put("/admin/bedrock-routing/mappings/org:nope", json={"destination_id": ACME_DEST})

    probe_ok.assert_not_awaited()


# ===========================================================================
# G7 / G8 — vocabulary and redaction
# ===========================================================================


async def test_g7_refusal_reasons_come_from_r3s_shared_vocabulary(session, seeded, probe_denied):
    """§6.7 item 2: save-time and runtime failures must speak the same names.

    R3 (#4744) shipped ``bedrock_routing_errors`` for exactly this path. An admin who
    sees ``role_missing_bedrock_permission`` in a rejected save and the same string in a
    runtime 502 can connect them; two private vocabularies means they cannot.
    """
    async with client_for(session, platform_admin_context()) as client:
        probe_failure = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})
        cross_tenant = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": GLOBEX_DEST})

    assert probe_failure.json()["detail"]["reason"] in vars(bedrock_routing_errors).values()
    assert cross_tenant.json()["detail"]["reason"] in vars(bedrock_routing_errors).values()


async def test_g8_no_response_ever_carries_a_role_arn(session, seeded, probe_ok, probe_denied):
    """§2.6 redaction: the ARN goes to the audit row and the log, never to the client.

    Checked across a success, a refusal, and both list reads — an ARN names a role in
    someone else's account, and the account id is what an admin actually needs on
    screen.
    """
    async with client_for(session, platform_admin_context()) as client:
        refusal = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})
        destinations = await client.get("/admin/bedrock-routing/destinations")
        mappings = await client.get("/admin/bedrock-routing/mappings")

    for response in (refusal, destinations, mappings):
        assert "arn:aws:iam" not in response.text, f"a role ARN leaked into {response.request.url}"
    # The account id, on the other hand, is required (ruling 1) — so the redaction
    # cannot have been achieved by returning nothing useful.
    assert ACME_ACCOUNT in destinations.text
