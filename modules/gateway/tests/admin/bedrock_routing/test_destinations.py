"""Destination registry and lifecycle — Issue #4745 (#4692 · R4), §4.2, §6.6, §6.7.

D1  the dropdown filter is org-scoped (usability), the API check is the control
D2  the destinations TABLE shows unusable rows — that is what re-verify is for
D3  registering a connection reads its provenance, never accepts it
D4  registering a new account: v2 template, and NOT usable until verified
D5  re-verify always re-probes, and a failure un-verifies
D6  used_by, so the panel can show what a change would break
D7  DELETE is 204 whether or not a rule existed
D8  every write is audited, including the refusals (§4.2)
"""

from __future__ import annotations

import sqlalchemy as sa

from src.shared.models.audit import AuditLog
from src.shared.models.bedrock_routing import BedrockDestinationRegistry
from src.shared.models.vault import UserCredential

from .conftest import (
    ACME_ACCOUNT,
    ACME_DEST,
    GLOBEX_DEST,
    MEMBER_ID,
    ORG_ID,
    OTHER_ORG_ID,
    PLATFORM_DEST,
    TEAM_ID,
    UNVERIFIED_DEST,
    client_for,
    fake_secrets,
    platform_admin_context,
    seed_mapping,
    stored_mappings,
)

NEW_ACCOUNT = "555555550001"


async def _audit_events(session) -> list[AuditLog]:
    session.expire_all()
    result = await session.scalars(sa.select(AuditLog).order_by(AuditLog.created_at))
    return list(result)


# ===========================================================================
# D1 / D2 — listing
# ===========================================================================


async def test_d1_the_org_filter_lists_only_what_a_rule_for_that_org_may_name(session, seeded):
    """The mockup's *"Only verified destinations linked to acme-corp are listed"*.

    Org-linked plus platform-registered, which is exactly the set ``PUT`` would accept
    for that tenant. Keeping the two in step is what stops the panel offering a choice
    the API then refuses.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.get("/admin/bedrock-routing/destinations", params={"org_id": ORG_ID})

    assert response.status_code == 200, response.text
    ids = {row["id"] for row in response.json()}
    assert GLOBEX_DEST not in ids, "another tenant's destination must not be offered"
    assert {ACME_DEST, PLATFORM_DEST} <= ids


async def test_d1b_the_filter_is_a_convenience_and_the_api_is_the_control(session, seeded, probe_ok):
    """A destination absent from the filtered list is also refused by ``PUT``.

    §4.2 requirement 1 spells out why both exist: *"a UI that only lists in-scope
    options is a usability feature; an API that only accepts them is the control."* A
    caller who ignores the dropdown must not get further than one who uses it.
    """
    async with client_for(session, platform_admin_context()) as client:
        listed = await client.get("/admin/bedrock-routing/destinations", params={"org_id": ORG_ID})
        forced = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": GLOBEX_DEST})

    assert GLOBEX_DEST not in {row["id"] for row in listed.json()}
    assert forced.status_code == 422, forced.text
    assert await stored_mappings(session) == []


async def test_d2_the_unfiltered_table_shows_unusable_destinations(session, seeded):
    """The mockup renders a failed destination in red with its reason.

    Hiding it would hide the row the admin needs to act on — the destinations table is
    where a fail-closed outage gets diagnosed, so a broken destination has to be
    visible and marked, not filtered away.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.get("/admin/bedrock-routing/destinations")

    rows = {row["id"]: row for row in response.json()}
    assert UNVERIFIED_DEST in rows
    assert rows[UNVERIFIED_DEST]["usable_for_routing"] is False
    assert rows[UNVERIFIED_DEST]["verified_at"] is None
    assert rows[ACME_DEST]["usable_for_routing"] is True


async def test_d2b_the_source_column_distinguishes_org_linked_from_admin_registered(session, seeded):
    """The mockup's SOURCE column. An explicit string, not a nullable org to interpret."""
    async with client_for(session, platform_admin_context()) as client:
        rows = {row["id"]: row for row in (await client.get("/admin/bedrock-routing/destinations")).json()}

    assert rows[ACME_DEST]["source"] == "org-linked"
    assert rows[ACME_DEST]["owner_org_id"] == ORG_ID
    assert rows[PLATFORM_DEST]["source"] == "admin-registered"
    assert rows[PLATFORM_DEST]["owner_org_id"] is None


# ===========================================================================
# D3 — registering from an existing connection
# ===========================================================================


async def test_d3_promoting_a_connection_takes_the_org_from_the_credential(session, seeded):
    """The tenant is READ from the connection, never accepted as a parameter.

    There is no field with which an admin could label a connection as belonging to a
    tenant it does not — which is what makes the §4.2 ownership check meaningful later.
    A settable owner would turn the control into a formality.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.post(
            "/admin/bedrock-routing/destinations",
            json={"source": "connection", "credential_id": "cred-acme-org", "label": "promoted-acme"},
        )

    assert response.status_code == 201, response.text
    destination = response.json()["destination"]
    assert destination["owner_org_id"] == ORG_ID
    assert destination["account_id"] == ACME_ACCOUNT
    assert destination["source"] == "org-linked"
    # Nothing to launch: the role already exists and has been assumed at least once.
    assert response.json()["launch_url"] is None


async def test_d3b_a_pending_connection_cannot_be_promoted(session, seeded):
    """§4.4: ``pending`` rows are EXCLUDED, not deprioritised.

    A connection whose CloudFormation stack has not finished has no role to assume, so
    a destination built on it fails every call. Refused at registration rather than
    discovered at runtime.
    """
    session.add(
        UserCredential(
            id="cred-pending",
            org_id=ORG_ID,
            service="aws",
            credential_type="aws_role",
            label="half-connected",
            secret_arn="arn:aws:secretsmanager:us-east-1:999:secret:adp/orgs/acme/pending",
            scopes={"account_id": "666666660001", "role_arn": "arn:aws:iam::666666660001:role/x", "status": "pending"},
        )
    )
    await session.commit()

    async with client_for(session, platform_admin_context()) as client:
        response = await client.post("/admin/bedrock-routing/destinations", json={"source": "connection", "credential_id": "cred-pending"})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "connection_not_verified"


async def test_d3c_an_unknown_connection_is_refused(session, seeded):
    async with client_for(session, platform_admin_context()) as client:
        response = await client.post("/admin/bedrock-routing/destinations", json={"source": "connection", "credential_id": "cred-nope"})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "connection_not_found"


# ===========================================================================
# D4 — registering a brand-new account (§6.6)
# ===========================================================================


async def test_d4_registering_a_new_account_returns_a_v2_quick_create_url(session, seeded, monkeypatch):
    """§5.0b: the admin path must use the v2 template, not v1.

    v1 pins its trust policy to a single user id, so a v1 destination cannot serve the
    team and org rules this endpoint exists to enable — every member except whoever ran
    the stack would get AccessDenied. Passing v1 here would make the whole surface look
    functional and route nothing.
    """
    captured = {}

    def fake_launch_url(**kwargs):
        captured.update(kwargs)
        return "https://console.aws.amazon.com/cloudformation/quickcreate?stub=1"

    monkeypatch.setattr("src.admin.bedrock_routing.routes.build_launch_url", fake_launch_url)

    async with client_for(session, platform_admin_context()) as client:
        response = await client.post(
            "/admin/bedrock-routing/destinations",
            json={"source": "new_account", "account_id": NEW_ACCOUNT, "label": "ml-sandbox", "link_to_org_id": ORG_ID},
        )

    assert response.status_code == 201, response.text
    assert response.json()["launch_url"] is not None
    assert captured["template_version"] == "v2"


async def test_d4b_a_newly_registered_destination_is_not_usable_yet(session, seeded, monkeypatch):
    """Fail-closed applied to registration itself.

    Merely *starting* a registration must not be able to reroute traffic onto an
    account where the role does not exist yet. ``routing_capable`` stays False until a
    real probe says otherwise — which is the ``PUT`` gate or the verify action, both of
    which run the same one probe.
    """
    monkeypatch.setattr("src.admin.bedrock_routing.routes.build_launch_url", lambda **kwargs: "https://stub")

    async with client_for(session, platform_admin_context()) as client:
        response = await client.post(
            "/admin/bedrock-routing/destinations",
            json={"source": "new_account", "account_id": NEW_ACCOUNT, "label": "ml-sandbox", "link_to_org_id": ORG_ID},
        )

    destination = response.json()["destination"]
    assert destination["routing_capable"] is False
    assert destination["verified_at"] is None
    assert destination["usable_for_routing"] is False


async def test_d4c_the_new_accounts_credential_is_org_scoped_not_user_owned(session, seeded, monkeypatch):
    """It must pass §4.3's ruling-6 check, which a user-owned row cannot.

    §5.0b observes this is in fact the *only* workable path for team and org rungs
    today: a tenant's own connection is user-owned, and ruling 6 forbids those for
    shared scopes. So the credential this endpoint writes has all three owner columns
    NULL — the ``vault.py`` org-scope convention.
    """
    monkeypatch.setattr("src.admin.bedrock_routing.routes.build_launch_url", lambda **kwargs: "https://stub")

    async with client_for(session, platform_admin_context()) as client:
        response = await client.post(
            "/admin/bedrock-routing/destinations",
            json={"source": "new_account", "account_id": NEW_ACCOUNT, "label": "ml-sandbox", "link_to_org_id": ORG_ID},
        )

    session.expire_all()
    destination = await session.scalar(
        sa.select(BedrockDestinationRegistry).where(BedrockDestinationRegistry.id == response.json()["destination"]["id"])
    )
    credential = await session.scalar(sa.select(UserCredential).where(UserCredential.id == destination.credential_id))
    assert credential.user_id is None
    assert credential.team_id is None
    assert credential.domain_app_id is None
    assert credential.org_id == ORG_ID


async def test_d4d_registering_for_a_nonexistent_org_is_refused(session, seeded):
    """``link_to_org_id`` is required and is validated — an unowned row is the leak
    shape §4.2 requirement 2 names as the thing to avoid."""
    async with client_for(session, platform_admin_context()) as client:
        response = await client.post(
            "/admin/bedrock-routing/destinations",
            json={"source": "new_account", "account_id": NEW_ACCOUNT, "label": "orphan", "link_to_org_id": "org-nope"},
        )

    assert response.status_code == 422, response.text


async def test_d4e_the_external_id_never_appears_in_the_response(session, seeded, monkeypatch):
    """The ExternalId is the load-bearing authorization condition on a v2 role.

    With v1's single-user session-tag condition deliberately gone, it is the only thing
    standing between the destination role and a confused-deputy assume. It reaches
    Secrets Manager and the quick-create URL and nowhere else — no registry column, no
    audit detail, no response body.
    """
    captured = {}

    def fake_launch_url(**kwargs):
        captured.update(kwargs)
        return "https://stub"

    monkeypatch.setattr("src.admin.bedrock_routing.routes.build_launch_url", fake_launch_url)

    async with client_for(session, platform_admin_context()) as client:
        response = await client.post(
            "/admin/bedrock-routing/destinations",
            json={"source": "new_account", "account_id": NEW_ACCOUNT, "label": "ml-sandbox", "link_to_org_id": ORG_ID},
        )

    external_id = captured["external_id"]
    assert external_id, "an ExternalId must be generated"
    assert external_id not in response.text, "the ExternalId leaked into the response body"
    for event in await _audit_events(session):
        assert external_id not in str(event.details), "the ExternalId leaked into an audit row"


# ===========================================================================
# D5 — on-demand re-validation (§6.7 item 5)
# ===========================================================================


async def test_d5_verify_always_re_probes_rather_than_replaying_a_verdict(session, seeded, probe_ok):
    """A cached answer is the one thing this endpoint must not give.

    The caller is asking precisely because they doubt the stored verdict — §6.7 item 4:
    a pass is a statement about now, not a permanent guarantee. The destination here is
    already stored as verified, and the probe must still run.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.post(f"/admin/bedrock-routing/destinations/{ACME_DEST}/verify")

    assert response.status_code == 200, response.text
    assert response.json()["verified"] is True
    probe_ok.assert_awaited_once()


async def test_d5b_a_failed_verify_un_verifies_the_destination(session, seeded, probe_denied):
    """A destination that has stopped working stops being selectable.

    Staying green on a stale pass is how an admin picks a destination that fails every
    call. The reason travels with it so the panel can render the mockup's red row.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.post(f"/admin/bedrock-routing/destinations/{ACME_DEST}/verify")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] is False
    assert body["reason"] == "role_missing_bedrock_permission"
    assert body["destination"]["usable_for_routing"] is False

    session.expire_all()
    stored = await session.scalar(sa.select(BedrockDestinationRegistry).where(BedrockDestinationRegistry.id == ACME_DEST))
    assert stored.verified_at is None
    assert stored.routing_capable is False


async def test_d5c_a_failed_verify_leaves_existing_rules_in_place(session, seeded, probe_denied):
    """It un-verifies the destination; it does not delete anybody's rules.

    The resolver skips an unusable destination and falls through (§4.4), so the rules
    remain meaningful. Silently deleting an admin's rules on a transient probe failure
    would be a far larger action than they asked for — and irreversible.
    """
    await seed_mapping(session, scope_type="org", destination_id=ACME_DEST, org_id=ORG_ID)

    async with client_for(session, platform_admin_context()) as client:
        await client.post(f"/admin/bedrock-routing/destinations/{ACME_DEST}/verify")

    assert len(await stored_mappings(session)) == 1


async def test_d5d_verifying_an_unknown_destination_is_a_404(session, seeded, probe_ok):
    async with client_for(session, platform_admin_context()) as client:
        response = await client.post("/admin/bedrock-routing/destinations/dest-nope/verify")

    assert response.status_code == 404, response.text
    probe_ok.assert_not_awaited()


# ===========================================================================
# D6 — used_by
# ===========================================================================


async def test_d6_used_by_counts_the_rules_pointing_at_each_destination(session, seeded):
    """The mockup's "USED BY — 1 rule" column.

    Under fail-closed, removing a destination a rule still references is an outage
    (§8.3), so the count is what lets the panel show what a change would break. Always
    rendered, which is why it is one grouped query rather than one per row.
    """
    await seed_mapping(session, scope_type="org", destination_id=ACME_DEST, org_id=ORG_ID)
    await seed_mapping(session, scope_type="team", destination_id=ACME_DEST, org_id=ORG_ID, team_id=TEAM_ID)

    async with client_for(session, platform_admin_context()) as client:
        rows = {row["id"]: row for row in (await client.get("/admin/bedrock-routing/destinations")).json()}

    assert rows[ACME_DEST]["used_by"] == 2
    assert rows[PLATFORM_DEST]["used_by"] == 0


# ===========================================================================
# D7 — DELETE
# ===========================================================================


async def test_d7_deleting_a_rule_removes_it_and_returns_204(session, seeded):
    await seed_mapping(session, scope_type="org", destination_id=ACME_DEST, org_id=ORG_ID)

    async with client_for(session, platform_admin_context()) as client:
        response = await client.delete(f"/admin/bedrock-routing/mappings/org:{ORG_ID}")

    assert response.status_code == 204, response.text
    assert await stored_mappings(session) == []


async def test_d7b_deleting_an_absent_rule_is_also_204(session, seeded):
    """The outcome the caller asked for holds either way.

    A 404 on the already-absent case would make a retried delete look like a failure,
    and there is nothing for the admin to do differently about it.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.delete(f"/admin/bedrock-routing/mappings/org:{ORG_ID}")

    assert response.status_code == 204, response.text


async def test_d7c_deleting_one_rung_leaves_the_others(session, seeded):
    """The scope predicate must address exactly one row.

    A predicate too loose here would delete a team's and an org's rules together — and
    the caller would see the same 204.
    """
    await seed_mapping(session, scope_type="org", destination_id=ACME_DEST, org_id=ORG_ID)
    await seed_mapping(session, scope_type="team", destination_id=ACME_DEST, org_id=ORG_ID, team_id=TEAM_ID)
    await seed_mapping(session, scope_type="user", destination_id=ACME_DEST, user_id=MEMBER_ID)

    async with client_for(session, platform_admin_context()) as client:
        await client.delete(f"/admin/bedrock-routing/mappings/team:{ORG_ID}:{TEAM_ID}")

    assert sorted(m.scope_type for m in await stored_mappings(session)) == ["org", "user"]


async def test_d7d_a_malformed_scope_on_delete_is_422(session, seeded):
    async with client_for(session, platform_admin_context()) as client:
        response = await client.delete("/admin/bedrock-routing/mappings/garbage")

    assert response.status_code == 422, response.text


# ===========================================================================
# D8 — audit (§4.2)
# ===========================================================================


async def test_d8_authoring_a_rule_is_audited_with_the_role_arn(session, seeded, probe_ok):
    """§4.2: *"this is the record of who decided whose bill pays."*

    The ARN belongs here and nowhere user-facing (§2.6) — it is what an incident
    responder needs, and the audit row is the one place it is safe.
    """
    async with client_for(session, platform_admin_context()) as client:
        await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})

    events = [e for e in await _audit_events(session) if e.event_type == "bedrock_routing_mapping_authored"]
    assert len(events) == 1
    assert events[0].details["scope"] == f"org:{ORG_ID}"
    assert events[0].details["destination_account_id"] == ACME_ACCOUNT
    assert events[0].details["destination_role_arn"].startswith("arn:aws:iam::")


async def test_d8b_every_save_time_failure_is_audited_even_though_nothing_is_stored(session, seeded, probe_denied):
    """§4.2 asks for *"every save-time assume failure"*, and a refusal rolls back.

    So the refusal audit commits on its own transaction — otherwise the rollback that
    guarantees "nothing was stored" would also erase the record that somebody tried.
    """
    async with client_for(session, platform_admin_context()) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})

    assert response.status_code == 422
    assert await stored_mappings(session) == [], "the refusal must still store no mapping"

    events = [e for e in await _audit_events(session) if e.event_type == "bedrock_routing_mapping_rejected"]
    assert len(events) == 1
    assert events[0].details["reason"] == "role_missing_bedrock_permission"


async def test_d8c_a_cross_tenant_attempt_is_audited(session, seeded, probe_ok):
    """The refusal an operator would most want a record of."""
    async with client_for(session, platform_admin_context()) as client:
        await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": GLOBEX_DEST})

    events = [e for e in await _audit_events(session) if e.event_type == "bedrock_routing_mapping_rejected"]
    assert len(events) == 1
    assert events[0].details["reason"] == "account_unlinked"


async def test_d8d_deletes_verifies_and_registrations_are_audited(session, seeded, probe_ok):
    """Every write on the surface leaves a record, not only the authoring ones."""
    await seed_mapping(session, scope_type="org", destination_id=ACME_DEST, org_id=ORG_ID)

    async with client_for(session, platform_admin_context()) as client:
        await client.delete(f"/admin/bedrock-routing/mappings/org:{ORG_ID}")
        await client.post(f"/admin/bedrock-routing/destinations/{ACME_DEST}/verify")
        await client.post("/admin/bedrock-routing/destinations", json={"source": "connection", "credential_id": "cred-acme-org"})

    types = {e.event_type for e in await _audit_events(session)}
    assert {
        "bedrock_routing_mapping_deleted",
        "bedrock_routing_destination_verified",
        "bedrock_routing_destination_registered",
    } <= types


async def test_d8e_a_user_rung_event_files_under_the_platform_sentinel(session, seeded, probe_ok):
    """``AuditLog.org_id`` is NOT NULL, and a user-rung mapping names no org.

    An empty string there reads as a bug; the sentinel reads as deliberate and cannot
    collide with a real ``organizations.id``.
    """
    async with client_for(session, platform_admin_context()) as client:
        await client.put(f"/admin/bedrock-routing/mappings/user:{MEMBER_ID}", json={"destination_id": PLATFORM_DEST})

    events = [e for e in await _audit_events(session) if e.event_type == "bedrock_routing_mapping_authored"]
    assert events[0].org_id == "__platform__"


async def test_d8f_the_actor_is_the_canonical_user_id_not_a_cognito_sub(session, seeded, probe_ok):
    """#4647: audit columns hold canonical ``users.id``.

    Persisting ``TokenContext.user_id`` raw would mix two id namespaces in one column,
    and "who decided whose bill pays" would stop joining to anything.
    """
    from .conftest import PLATFORM_ADMIN_ID, PLATFORM_ADMIN_SUB

    async with client_for(session, platform_admin_context()) as client:
        await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})

    events = [e for e in await _audit_events(session) if e.event_type == "bedrock_routing_mapping_authored"]
    assert events[0].actor_id == PLATFORM_ADMIN_ID
    assert events[0].actor_id != PLATFORM_ADMIN_SUB

    stored = await stored_mappings(session)
    assert stored[0].authored_by_user_id == PLATFORM_ADMIN_ID


# ===========================================================================
# Cache invalidation — the "my rule did nothing" trap
# ===========================================================================


async def test_a_mapping_write_clears_the_resolvers_existence_cache(session, seeded, probe_ok):
    """On an install whose mapping table was empty, the resolver caches "none exist".

    That 60s cache is the decision keeping day-one installs at zero queries per model
    call (§2.2). Its consequence for this surface: without clearing it, an admin saves
    the first rule, watches traffic keep landing on the platform account, and concludes
    the feature is broken.
    """
    from src.proxy.bedrock_routing import bedrock_routing_resolver

    bedrock_routing_resolver._mappings_exist_cache = (False, 0.0)

    async with client_for(session, platform_admin_context()) as client:
        await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})

    assert bedrock_routing_resolver._mappings_exist_cache is None


async def test_a_re_verify_invalidates_cached_destination_credentials(session, seeded, probe_ok, monkeypatch):
    """R3 left the hook for exactly this (``bedrock_signing.py``).

    Correctness does not depend on it — ``destination_updated_at`` is in the cache key,
    so an edit is self-evicting. What the hook adds is promptness: a re-verify is where
    an operator most wants the old session gone rather than merely unreachable.
    """
    calls = []
    monkeypatch.setattr("src.admin.bedrock_routing.service.invalidate_signer_cache", calls.append)

    async with client_for(session, platform_admin_context()) as client:
        await client.post(f"/admin/bedrock-routing/destinations/{ACME_DEST}/verify")

    assert calls == [f"arn:aws:iam::{ACME_ACCOUNT}:role/ADP-Agent-acme-prod"]


async def test_the_secrets_helper_is_the_injected_one(session, seeded, probe_ok):
    """The probe's ExternalId comes from the injected helper, so it is testable at all.

    Reusing ``vault_routes.get_secrets_manager`` rather than constructing a helper
    inline is what makes the whole save-time path exercisable without AWS — and it is
    the module-level singleton the rest of the codebase already overrides in tests.
    """
    secrets = fake_secrets(external_id="ext-injected")

    async with client_for(session, platform_admin_context(), secrets) as client:
        await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})

    assert probe_ok.await_args.kwargs["external_id"] == "ext-injected"
    secrets.get_secret.assert_called_once()


async def test_an_unreadable_secret_does_not_skip_the_probe(session, seeded, probe_ok):
    """A bookkeeping gap must not become a shortcut past the gate.

    When the ExternalId cannot be read, the probe still runs — without one. Any role
    whose trust policy requires it then rejects the assume, which is exactly the
    outcome §6.7 wants surfaced. Guessing a refusal reason from a missing secret would
    be less accurate than letting the probe speak.
    """
    secrets = fake_secrets()
    secrets.get_secret.side_effect = RuntimeError("secret arn arn:aws:secretsmanager:...:leaky is gone")

    async with client_for(session, platform_admin_context(), secrets) as client:
        response = await client.put(f"/admin/bedrock-routing/mappings/org:{ORG_ID}", json={"destination_id": ACME_DEST})

    probe_ok.assert_awaited_once()
    assert probe_ok.await_args.kwargs["external_id"] is None
    assert response.status_code == 200, response.text


async def test_org_scoped_listing_does_not_leak_other_tenants_labels(session, seeded):
    """A filtered list must not include another tenant's account id anywhere in it."""
    async with client_for(session, platform_admin_context()) as client:
        response = await client.get("/admin/bedrock-routing/destinations", params={"org_id": OTHER_ORG_ID})

    assert ACME_ACCOUNT not in response.text
    assert "acme-prod" not in response.text
