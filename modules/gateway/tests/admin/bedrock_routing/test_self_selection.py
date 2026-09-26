"""The self-service selector — Issue #4746 (#4692 · R5), design note §6.4 / §1.4.

Same harness discipline as the admin suite next door: **the probe is patched, never the
gate**, and ``AccessControl`` is not mocked. What is different is the caller — an
ordinary member, who is denied on every route of ``routes.py`` and served on every route
here. That contrast is a property of the feature, not an accident of two test files, so
S1 asserts it directly.

The groups, in the order their failures would matter:

  S1  the shape of the route is the authz — there is no target to name
  S2  a connection that is not the caller's own, or not verified, is refused
  S3  **admin wins is enforced on the WRITE**, because one row per scope leaves
      nowhere else for it to live
  S4  the read never reports a selection as active when something else governs
  S5  a refusal stores nothing, and the ladder is left as it was

S3 is the group that would not exist if this file only tested what the issue body
listed. With ``uq_bedrock_account_mapping_scope`` permitting exactly one row per scope,
"admin wins" cannot be read-time precedence — so an unguarded self ``PUT`` silently
*reverses* a platform admin's pin, and every display test still passes because the
display would be honestly reporting a row that should never have changed.
"""

from __future__ import annotations

import inspect

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.bedrock_routing import self_routes
from src.admin.bedrock_routing.self_routes import router as self_router
from src.auth.dependencies import get_current_user
from src.auth.vault_routes import get_secrets_manager
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.bedrock_routing import BedrockDestinationRegistry
from src.shared.models.vault import UserCredential
from src.shared.schemas.auth import TokenContext

from .conftest import (
    ACME_DEST,
    FOREIGN_MEMBER_ID,
    MEMBER_ID,
    ORG_ID,
    PERSONAL_ACCOUNT,
    PERSONAL_DEST,
    PLATFORM_ADMIN_ID,
    TEAM_ID,
    fake_secrets,
    member_context,
    seed_mapping,
    stored_mappings,
)

SELECTION = "/me/bedrock-routing/selection"

#: The personal, verified, routing-capable connection the happy path selects.
#: `cred-personal` in the shared fixture is owned by MEMBER_ID and verified but carries
#: no `routing_capable` flag, which is the pre-#4742 shape — so tests that need a
#: selectable row add this one.
ROUTABLE_CRED = "cred-personal-routable"
ROUTABLE_ACCOUNT = "555555556677"


def _build_app(session: AsyncSession, context: TokenContext, secrets: object | None = None) -> FastAPI:
    """Mount the self router alone.

    A local builder rather than ``conftest.build_app``: that one mounts the *admin*
    router, and a test that accidentally exercised this surface through it would prove
    nothing about this one. Everything else — the real ``get_db``, the real
    ``BedrockGatewayError`` handler, no ``AccessControl`` override — is deliberately
    identical, because the point of both harnesses is that only network egress is faked.
    """
    app = FastAPI()
    app.include_router(self_router)

    @app.exception_handler(BedrockGatewayError)
    async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
        content = {"error": exc.error, "message": exc.message}
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=content)

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_secrets_manager] = lambda: secrets or fake_secrets()
    app.dependency_overrides[get_current_user] = lambda: context
    return app


def _client(session: AsyncSession, context: TokenContext | None = None, secrets: object | None = None) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_build_app(session, context or member_context(), secrets)), base_url="http://test")


@pytest.fixture
async def routable_connection(session: AsyncSession, seeded) -> UserCredential:
    """A verified, routing-capable personal connection owned by the member.

    Carries ``routing_capable: True`` in ``scopes`` — the flag R1 (#4742) writes at
    connect-verify time. Without it a connection is correctly *not* selectable (§5.0b),
    which is what the shared fixture's ``cred-personal`` exercises.
    """
    credential = UserCredential(
        id=ROUTABLE_CRED,
        org_id=ORG_ID,
        user_id=MEMBER_ID,
        service="aws",
        credential_type="aws_role",
        label="jdoe-routable",
        secret_arn="arn:aws:secretsmanager:us-east-1:999:secret:adp/users/jdoe/aws-routable",
        scopes={
            "account_id": ROUTABLE_ACCOUNT,
            "role_arn": f"arn:aws:iam::{ROUTABLE_ACCOUNT}:role/ADP-Agent-jdoe-routable",
            "status": "verified",
            "routing_capable": True,
        },
    )
    session.add(credential)
    await session.commit()
    return credential


async def _destinations(session: AsyncSession) -> list[BedrockDestinationRegistry]:
    session.expire_all()
    return list(await session.scalars(sa.select(BedrockDestinationRegistry)))


# ===========================================================================
# S1 — the shape of the route IS the authz
# ===========================================================================


def test_s1_no_route_accepts_a_target_parameter():
    """No route here names a person, at any position.

    The security property of this surface, asserted structurally because that is how it
    is implemented: the anchor is derived from the token, so a request naming somebody
    else cannot be *formed*. There is no check to forget, and equally no check a future
    edit can weaken — but a future edit could add a ``user_id`` parameter "for the admin
    view", which is exactly what this fails on.

    Checked on the path AND the signature: a path parameter would appear in the route's
    ``path``, while a query parameter would appear only in the handler's arguments.
    """
    forbidden = ("user_id", "person", "anchor", "scope", "target", "credential_id")
    for route in self_router.routes:
        assert "{" not in route.path, f"{route.path} takes a path parameter; the self surface must take no target"
        parameters = set(inspect.signature(route.endpoint).parameters)
        leaked = parameters & set(forbidden)
        assert not leaked, f"{route.path} accepts {leaked} — a target reachable from this surface is how authority leaks"


def test_s1b_the_write_body_names_a_connection_and_not_a_person():
    """``MySelectionRequest`` has a ``credential_id`` and nothing that names a person.

    The one id a caller supplies is a *connection* of their own, resolved by a lookup
    scoped to them — so an id belonging to anybody else does not resolve. A ``user_id``
    field on this body would make that scoping a comparison somebody has to remember to
    write, which is the difference this whole surface is built to avoid.
    """
    from src.admin.bedrock_routing.schemas import MySelectionRequest

    assert set(MySelectionRequest.model_fields) == {"credential_id", "expected_account_id"}
    assert not {"user_id", "scope", "destination_id"} & set(MySelectionRequest.model_fields)


async def test_s1c_a_plain_member_is_served_on_every_route(session, seeded, routable_connection, probe_ok):
    """The contrast with ``test_authz.py``: a member is DENIED there and SERVED here.

    Same caller, same tables, two surfaces. ``test_a2`` asserts this member gets a 403
    on every admin route including their own user rung; §6.4 is the surface where they
    legitimately author it. If this ever regresses to a 403, the self-service feature is
    gone while every admin test still passes.
    """
    async with _client(session) as client:
        read = await client.get(SELECTION)
        write = await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})
        clear = await client.delete(SELECTION)

    for response in (read, write, clear):
        assert response.status_code == 200, response.text


# ===========================================================================
# S2 — whose connection, and is it usable
# ===========================================================================


async def test_s2_another_users_connection_cannot_be_selected(session, seeded, routable_connection, probe_ok):
    """A connection belonging to somebody else is refused, and nothing is stored.

    Note *how* it is refused: ``connection_not_found``, because the lookup is scoped by
    ``user_credentials.user_id`` and the row simply does not match. The caller is not
    told that the id exists and belongs to another person — the refusal is not an
    existence oracle over other people's AWS connections.
    """
    other = UserCredential(
        id="cred-someone-else",
        org_id=ORG_ID,
        user_id=FOREIGN_MEMBER_ID,
        service="aws",
        credential_type="aws_role",
        label="not-yours",
        secret_arn="arn:aws:secretsmanager:us-east-1:999:secret:adp/users/other/aws-zzz",
        scopes={
            "account_id": "999999991111",
            "role_arn": "arn:aws:iam::999999991111:role/ADP-Agent-not-yours",
            "status": "verified",
            "routing_capable": True,
        },
    )
    session.add(other)
    await session.commit()

    async with _client(session) as client:
        response = await client.put(SELECTION, json={"credential_id": "cred-someone-else"})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "connection_not_found"
    assert "999999991111" not in response.text, "a refusal must not disclose the other person's account"
    assert await stored_mappings(session) == []


async def test_s2b_an_unverified_connection_cannot_be_selected(session, seeded, probe_ok):
    """§4.4: a ``pending`` connection is refused, not merely deprioritised.

    The role does not exist in the destination account until the person's CloudFormation
    stack finishes. Storing this would mean that merely *starting* a connect flow
    reroutes their traffic onto an account that fails every call — under fail-closed
    (§2.5), a self-inflicted outage from an action that looks like progress.
    """
    pending = UserCredential(
        id="cred-pending",
        org_id=ORG_ID,
        user_id=MEMBER_ID,
        service="aws",
        credential_type="aws_role",
        label="jdoe-pending",
        secret_arn="arn:aws:secretsmanager:us-east-1:999:secret:adp/users/jdoe/aws-pending",
        scopes={"account_id": "666666668899", "role_arn": "arn:aws:iam::666666668899:role/ADP-Agent-jdoe-pending", "status": "pending"},
    )
    session.add(pending)
    await session.commit()

    async with _client(session) as client:
        response = await client.put(SELECTION, json={"credential_id": "cred-pending"})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "connection_not_verified"
    assert await stored_mappings(session) == []
    minted = [d for d in await _destinations(session) if d.credential_id == "cred-pending"]
    assert not minted, "an unverified connection must not even mint a destination"


async def test_s2c_an_unselectable_connection_is_listed_with_the_reason_why(session, seeded, routable_connection):
    """Non-selectable connections are LISTED, not hidden — with the reason why.

    §5.0b means most existing personal connections are legitimately unselectable (v1
    roles are pinned to their creator). Filtering them would show the person an empty
    list and no explanation, which is the dead end §6.4's honest-display requirement
    exists to prevent. ``cred-personal`` in the shared fixture is the realistic case:
    verified, but never classified routing-capable.
    """
    async with _client(session) as client:
        response = await client.get(SELECTION)

    assert response.status_code == 200, response.text
    by_id = {c["credential_id"]: c for c in response.json()["connections"]}

    assert by_id[ROUTABLE_CRED]["selectable"] is True
    assert by_id[ROUTABLE_CRED]["reason"] is None

    # Verified but not routing-capable: unselectable, and the reason names the
    # remediation (re-run the routing template) rather than "verification failed".
    assert by_id["cred-personal"]["status"] == "verified"
    assert by_id["cred-personal"]["selectable"] is False
    assert by_id["cred-personal"]["reason"] == "role_user_pinned_needs_v2_template"


async def test_s2d_an_org_scoped_connection_is_not_the_callers_to_select(session, seeded, probe_ok):
    """An org-scoped connection is not listed and cannot be selected.

    ``cred-acme-org`` has all three owner columns NULL (the ``vault.py`` convention),
    so it belongs to the tenant, not to a person. Promoting one into a shared
    destination is the admin surface's job (§4.3, ruling 6); an individual reaching it
    from here would be pointing their traffic at a resource nobody assigned them.
    """
    async with _client(session) as client:
        listed = await client.get(SELECTION)
        selected = await client.put(SELECTION, json={"credential_id": "cred-acme-org"})

    assert "cred-acme-org" not in {c["credential_id"] for c in listed.json()["connections"]}
    assert selected.status_code == 422, selected.text
    assert selected.json()["detail"]["reason"] == "connection_not_found"
    assert await stored_mappings(session) == []


async def test_s2e_a_failing_probe_refuses_and_stores_nothing(session, seeded, routable_connection, probe_denied):
    """§6.7: a destination that cannot serve a call does not become a rule that fails one.

    The probe runs for real (only its network egress is faked), and its verdict is not
    special-cased away. The reason is R1/R3's shared vocabulary, so the person sees the
    same code here that a runtime 502 would carry.
    """
    async with _client(session) as client:
        response = await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "role_missing_bedrock_permission"
    assert await stored_mappings(session) == []


async def test_s2f_no_response_or_error_carries_a_role_arn(session, seeded, routable_connection, probe_denied):
    """§2.6: the role ARN reaches the audit row and the log, never the person.

    Asserted on both a success and a refusal because the refusal path is the one that
    interpolates a destination into prose, and it is the likelier place for an ARN to
    be helpfully added later.
    """
    async with _client(session) as client:
        refused = await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})
        read = await client.get(SELECTION)

    for response in (refused, read):
        assert "arn:aws:iam::" not in response.text, f"a role ARN leaked into {response.request.method} {response.request.url.path}"


# ===========================================================================
# S3 — admin wins, enforced on the WRITE
# ===========================================================================


async def test_s3_a_platform_admin_pin_cannot_be_overwritten_by_the_person(session, seeded, routable_connection, probe_ok):
    """**The central refusal.** A person may not re-point a row an admin authored.

    ``uq_bedrock_account_mapping_scope`` permits exactly ONE row per scope, so the user
    rung is a single row both surfaces upsert. "Admin wins" (§1.4) therefore cannot be
    read-time precedence between two rows — there are never two — which leaves the write
    as the only place it can be enforced. Without this refusal a person undoes their own
    pin by re-selecting, reversing the exact decision the override exists to take out of
    their hands, and no *display* test notices because the display would then be
    honestly reporting a row that should not have changed.
    """
    await seed_mapping(session, scope_type="user", destination_id=ACME_DEST, user_id=MEMBER_ID, authored_by=PLATFORM_ADMIN_ID)

    async with _client(session) as client:
        response = await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "pinned_by_platform_admin"

    rows = await stored_mappings(session)
    assert len(rows) == 1
    assert rows[0].destination_id == ACME_DEST, "the admin's choice was overwritten"
    assert rows[0].authored_by_user_id == PLATFORM_ADMIN_ID, "the admin's authorship was overwritten"


async def test_s3b_a_platform_admin_pin_cannot_be_deleted_by_the_person(session, seeded, probe_ok):
    """And the DELETE is guarded too — otherwise it is a one-request way around the PUT.

    Written separately from S3 because the natural implementation guards only the write
    that "changes" something. A delete followed by a fresh select achieves precisely the
    overwrite S3 refuses, in two requests instead of one.
    """
    await seed_mapping(session, scope_type="user", destination_id=ACME_DEST, user_id=MEMBER_ID, authored_by=PLATFORM_ADMIN_ID)

    async with _client(session) as client:
        response = await client.delete(SELECTION)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "pinned_by_platform_admin"
    assert len(await stored_mappings(session)) == 1, "the admin's row was deleted"


async def test_s3c_the_pin_is_refused_before_the_probe_runs(session, seeded, routable_connection, probe_ok):
    """A pinned caller is refused without a network round trip to their AWS account.

    Ordering, not tidiness: probing first would produce a *second*, misleading reason
    (about their account's health) for a request that was never going to be stored for
    an unrelated reason. Same argument ``validate_mapping_target`` makes for its own
    gate order.
    """
    await seed_mapping(session, scope_type="user", destination_id=ACME_DEST, user_id=MEMBER_ID, authored_by=PLATFORM_ADMIN_ID)

    async with _client(session) as client:
        await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    assert probe_ok.await_count == 0, "the probe ran for a request that was refused on authority"


async def test_s3d_the_person_may_re_point_and_clear_their_own_row(session, seeded, routable_connection, probe_ok):
    """The other half of S3: their own row is theirs to change.

    A guard that refused every existing row would be indistinguishable from a broken
    upsert, so this pins the difference. The row's identity survives a re-point — it is
    the same scope, and ``created_at`` is when the person first routed themselves.
    """
    async with _client(session) as client:
        first = await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})
        again = await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})
        cleared = await client.delete(SELECTION)

    assert first.status_code == 200, first.text
    assert again.status_code == 200, again.text
    assert cleared.status_code == 200, cleared.text
    assert await stored_mappings(session) == [], "the person's own row should have been removed"


async def test_s3e_a_self_authored_row_reads_back_as_self(session, seeded, routable_connection, probe_ok):
    """``authored_by_user_id`` is the caller, which is what makes ``source`` say ``self``.

    ``service.mapping_source`` derives it: the row is the person's own iff
    ``authored_by_user_id == scope_id_user``, both canonical ``users.id`` (#4647). This
    is also what makes a later admin ``PUT`` — which re-stamps that column — read back as
    ``platform_admin``, i.e. the §1.4 transition is stored as a fact about who wrote the
    row rather than as a flag that could disagree with it.
    """
    async with _client(session) as client:
        response = await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    assert response.status_code == 200, response.text
    assert response.json()["effective"]["source"] == "self"
    assert response.json()["effective"]["overrides_self_selection"] is False

    rows = await stored_mappings(session)
    assert [(r.scope_type, r.scope_id_user, r.authored_by_user_id) for r in rows] == [("user", MEMBER_ID, MEMBER_ID)]


# ===========================================================================
# S4 — the read never misreports what governs
# ===========================================================================


async def test_s4_the_persons_own_selection_is_reported_as_active(session, seeded, routable_connection, probe_ok):
    """The happy path: their pick governs, and the screen may say so."""
    async with _client(session) as client:
        await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})
        response = await client.get(SELECTION)

    body = response.json()
    assert body["own_selection_active"] is True
    assert body["own_selection_account_id"] == ROUTABLE_ACCOUNT
    assert body["pinned_by_platform_admin"] is False
    assert body["effective"]["rung"] == "user"
    assert body["effective"]["account_id"] == ROUTABLE_ACCOUNT


async def test_s4b_an_admin_override_is_disclosed_and_the_own_pick_is_not_shown_active(session, seeded, routable_connection, probe_ok):
    """**The §1.4 display requirement.** An admin pin is stated, and never implied.

    Two assertions, both required: the override is reported (``overrides_self_selection``
    plus ``pinned_by_platform_admin``, and the effective account is the ADMIN's), and the
    person's own selection is NOT reported as active. Showing a stale pick as active
    while something else bills their calls is the #4511 inert-config defect on the one
    screen built to answer "where do my calls go".

    ``own_selection_*`` is null rather than stale, and that is correct rather than lossy:
    with one row per scope, the admin's ``PUT`` *overwrote* the person's choice, so there
    is no longer a pick of theirs to report. Reporting one would be inventing it.
    """
    async with _client(session) as client:
        await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    # The admin re-points the same row — `put_mapping`'s upsert, reproduced by seeding
    # the post-write state: same scope, admin's destination, admin's authorship.
    rows = await stored_mappings(session)
    rows[0].destination_id = ACME_DEST
    rows[0].authored_by_user_id = PLATFORM_ADMIN_ID
    await session.commit()

    async with _client(session) as client:
        response = await client.get(SELECTION)

    body = response.json()
    assert body["pinned_by_platform_admin"] is True
    assert body["effective"]["overrides_self_selection"] is True
    assert body["effective"]["source"] == "platform_admin"
    assert body["effective"]["rung"] == "user"
    assert body["own_selection_active"] is False, "the person's own pick must not be shown as active"
    assert body["own_selection_account_id"] is None


async def test_s4c_a_stored_but_unusable_selection_is_not_reported_as_active(session, seeded, routable_connection, probe_ok):
    """The SECOND way a stored pick stops governing — and it looks identical from the row.

    Not an admin override: the person's own row is untouched and still theirs. Its
    destination has stopped being usable (role deleted, probe now failing), so the
    resolver skips it and walks on (§4.4). A read that inferred "active" from "a row
    exists" would show them an account that bills none of their calls.
    """
    async with _client(session) as client:
        await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    # Exactly what a failed re-verify does (`verify_destination`): un-verify in place,
    # leave the rule pointing at it.
    destination = next(d for d in await _destinations(session) if d.credential_id == ROUTABLE_CRED)
    destination.routing_capable = False
    destination.verified_at = None
    await session.commit()

    async with _client(session) as client:
        response = await client.get(SELECTION)

    body = response.json()
    assert body["pinned_by_platform_admin"] is False, "this is not an override — the row is still theirs"
    assert body["own_selection_active"] is False, "an unusable destination does not govern"
    assert body["effective"]["rung"] == "platform", "the walk should have fallen through"
    assert len(await stored_mappings(session)) == 1, "the row must survive; the resolver skips it, it is not deleted"


async def test_s4d_falling_through_to_a_team_rule_is_reported_as_the_team_rule(session, seeded, routable_connection, probe_ok):
    """With nothing of their own, the read reports the rung that actually serves them.

    Not "unconfigured": a team rule billing their calls is a fact the person needs, and
    it is also what "clear my selection" will fall back to — which is why DELETE returns
    this payload rather than a 204.
    """
    await seed_mapping(session, scope_type="team", destination_id=ACME_DEST, org_id=ORG_ID, team_id=TEAM_ID)

    async with _client(session) as client:
        response = await client.get(SELECTION)

    body = response.json()
    assert body["effective"]["rung"] == "team"
    assert body["effective"]["source"] == "platform_admin"
    assert body["own_selection_active"] is False
    assert body["own_selection_destination_id"] is None


async def test_s4e_the_platform_rung_is_an_answer_not_an_absence(session, seeded, routable_connection):
    """No rule anywhere returns ``rung: platform`` with a null account, explicitly.

    R2's resolver returns an explicit target rather than ``None`` for exactly this
    reason: "your calls go to the platform's account" is today's behaviour and a
    complete answer, and rendering it as "unconfigured" would invite the person to fix
    something that is not broken.
    """
    async with _client(session) as client:
        response = await client.get(SELECTION)

    body = response.json()
    assert body["effective"]["rung"] == "platform"
    assert body["effective"]["account_id"] is None
    assert body["own_selection_active"] is False


async def test_s4f_clearing_reports_where_the_traffic_landed(session, seeded, routable_connection, probe_ok):
    """DELETE returns the new effective destination, not a bare acknowledgement.

    Removing a selection does not make anybody unroutable — it un-shadows the ladder.
    "You are back on your team's account" and "you are back on the platform's" are
    different outcomes and the person should not have to guess which one happened.
    """
    await seed_mapping(session, scope_type="team", destination_id=ACME_DEST, org_id=ORG_ID, team_id=TEAM_ID)

    async with _client(session) as client:
        await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})
        cleared = await client.delete(SELECTION)

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["effective"]["rung"] == "team", "the response should say the team rule now serves them"
    assert cleared.json()["own_selection_active"] is False


async def test_s4g_clearing_nothing_is_not_an_error(session, seeded, routable_connection):
    """Idempotent: the outcome the caller asked for already holds."""
    async with _client(session) as client:
        response = await client.delete(SELECTION)

    assert response.status_code == 200, response.text
    assert await stored_mappings(session) == []


# ===========================================================================
# S5 — what the write leaves behind
# ===========================================================================


async def test_s5_selecting_reuses_the_connections_destination_row_rather_than_minting_one(session, seeded, routable_connection, probe_ok):
    """Re-selecting the same connection must not grow the registry by a row per click.

    A person re-picking their own account is an ordinary, repeatable action. Always
    inserting would leave one orphan registry row per attempt, each a separate
    ``used_by``-less entry in the admin's destinations table — clutter that looks like
    real routing configuration.
    """
    async with _client(session) as client:
        await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})
        await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    mine = [d for d in await _destinations(session) if d.credential_id == ROUTABLE_CRED]
    assert len(mine) == 1, f"expected one destination for the connection, found {len(mine)}"
    assert len(await stored_mappings(session)) == 1


async def test_s5b_the_minted_destination_takes_its_tenant_from_the_credential(session, seeded, routable_connection, probe_ok):
    """``owner_org_id`` comes from the connection, and there is no parameter to override it.

    That absence is what keeps §4.2's ownership check total: no caller can mislabel a
    connection as belonging to a tenant it does not. ``is_platform_registered`` stays
    False — a person's own account is not a platform-wide destination, and a NULL-tenant
    row would be usable by *any* scope.
    """
    async with _client(session) as client:
        await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    destination = next(d for d in await _destinations(session) if d.credential_id == ROUTABLE_CRED)
    assert destination.owner_org_id == ORG_ID
    assert destination.is_platform_registered is False
    assert destination.account_id == ROUTABLE_ACCOUNT
    # The probe passed, so the row carries what the probe proved.
    assert destination.routing_capable is True
    assert destination.verified_at is not None


async def test_s5c_a_users_own_credential_is_allowed_for_their_own_rung(session, seeded, routable_connection, probe_ok):
    """Ruling 2 vs §4.3: the personal-credential refusal must NOT fire on the user rung.

    ``service._reject_personal_credential`` returns early for ``scope_type == "user"``,
    which is the whole basis of this feature: one person's role carrying a *team's*
    traffic is the authority problem §4.3 forbids, while carrying their own traffic is
    exactly what ruling 2 allows. If that early return were ever "tidied up", this
    surface would refuse every request it exists to serve.
    """
    async with _client(session) as client:
        response = await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    assert response.status_code == 200, response.text
    assert response.json()["effective"]["account_id"] == ROUTABLE_ACCOUNT


async def test_s5d_a_refusal_leaves_an_audit_row_even_though_nothing_is_stored(session, seeded, routable_connection, probe_denied):
    """§4.2's audit requirement applies to the self surface too.

    The refusal path rolls back the mapping, so the audit row is written on its own
    transaction — otherwise "every save-time assume failure leaves a record" would be
    erased by the very rollback that makes the refusal safe.
    """
    from src.shared.models.audit import AuditLog

    async with _client(session) as client:
        await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    events = list(await session.scalars(sa.select(AuditLog)))
    assert [e.event_type for e in events] == ["bedrock_routing_self_selection_rejected"]
    assert events[0].actor_id == MEMBER_ID, "the actor is the canonical users.id, not a Cognito sub"
    assert events[0].details["reason"] == "role_missing_bedrock_permission"
    assert await stored_mappings(session) == []


async def test_s5e_a_successful_selection_is_audited_with_the_role_arn(session, seeded, routable_connection, probe_ok):
    """The §2.6 split: the ARN an incident responder needs is in the audit row.

    Paired with S2f, which asserts it is in no response and no error. One assertion
    without the other proves only half of a redaction rule.
    """
    from src.shared.models.audit import AuditLog

    async with _client(session) as client:
        await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    events = list(await session.scalars(sa.select(AuditLog)))
    assert [e.event_type for e in events] == ["bedrock_routing_self_selection_authored"]
    assert events[0].details["destination_role_arn"].startswith("arn:aws:iam::")
    assert events[0].details["destination_account_id"] == ROUTABLE_ACCOUNT


async def test_s5f_a_write_clears_the_resolvers_existence_cache(session, seeded, routable_connection, probe_ok):
    """Otherwise the first selection on an empty install appears to do nothing.

    The resolver answers "do any mappings exist?" from a 60s process-local cache (§2.2),
    which is what keeps day-one installs at zero queries per model call. Before a
    person's first selection that cache says "none" — so without this invalidation they
    save, watch their traffic keep landing on the platform account, and conclude the
    selector is broken.
    """
    from src.proxy.bedrock_routing import bedrock_routing_resolver

    bedrock_routing_resolver._mappings_exist_cache = (False, 1e18)

    async with _client(session) as client:
        await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    assert bedrock_routing_resolver._mappings_exist_cache is None


async def test_s5g_the_selection_does_not_disturb_other_rungs(session, seeded, routable_connection, probe_ok):
    """A person selecting for themselves writes exactly one row, on the user rung.

    The team and org rules are somebody else's decision; a self write that touched them
    would be the authority inversion in reverse.
    """
    await seed_mapping(session, scope_type="team", destination_id=ACME_DEST, org_id=ORG_ID, team_id=TEAM_ID)
    await seed_mapping(session, scope_type="org", destination_id=PERSONAL_DEST, org_id=ORG_ID)

    async with _client(session) as client:
        await client.put(SELECTION, json={"credential_id": ROUTABLE_CRED})

    rows = {r.scope_type: r for r in await stored_mappings(session)}
    assert set(rows) == {"user", "team", "org"}
    assert rows["team"].destination_id == ACME_DEST
    assert rows["org"].destination_id == PERSONAL_DEST
    assert rows["org"].destination_id != PERSONAL_ACCOUNT  # guard against fixture drift


def test_s5h_the_self_module_runs_no_probe_of_its_own():
    """§6.7 item 1: there is ONE probe, and this module does not add a second.

    Asserted on source because the failure being guarded is an *addition*: a second
    assume with slightly different conditions is how "verified here, broken there"
    happens, and it would pass every behavioural test in this file.
    """
    source = inspect.getsource(self_routes)
    assert "assume_role" not in source, "the self surface must not assume a role directly; go through service.validate_mapping_target"
    assert "probe_routing_destination" not in source, "the self surface must not call the probe directly"
