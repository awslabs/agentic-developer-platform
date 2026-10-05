"""Save-time gates, the effective-mapping walk, and audit — Issue #4745 (#4692 · R4).

The routes are HTTP; this module is the decisions. Each function here answers one
question the design note makes a requirement, and each is separately testable — which
matters because most of them are refusals, and a refusal that silently stops refusing
is invisible from the outside.

**The gate order in :func:`validate_mapping_target` is deliberate and is the cheapest
thing in this file.** Ownership and personal-credential checks are pure SQL reads;
the test assume is a network round trip to somebody else's AWS account. Running the
free checks first means a cross-tenant attempt is refused without ever touching STS,
and the admin gets the *actionable* reason rather than whichever failure came back
first.

**Everything here refuses rather than stores.** Ruling 4a: "a mapping that cannot be
assumed is rejected, never stored inert (#4511 class)." So every gate raises before a
row is added, and the audit row for a refusal is written on its own transaction — a
failure has to leave a trace even though it leaves no mapping.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.proxy.bedrock_routing import bedrock_routing_resolver
from src.proxy.bedrock_routing_errors import REASON_ACCOUNT_UNLINKED
from src.shared.models.audit import AuditLog
from src.shared.models.base import new_uuid
from src.shared.models.bedrock_routing import BedrockAccountMapping, BedrockConnectionGrant, BedrockDestinationRegistry
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserCredential
from src.shared.services.routing_probe import probe_routing_destination
from src.shared.services.secrets_manager import SecretsManagerHelper

logger = logging.getLogger("bedrockgateway.admin.bedrock_routing")

#: Rungs, narrowest first — the same walk order the resolver uses. Imported shape
#: rather than a second literal would be better still, but the resolver's is private
#: to its module; the parity is asserted in the tests instead.
RUNG_ORDER: tuple[str, ...] = ("user", "team", "org")

#: ``AuditLog`` carries ``TenantMixin``, whose ``org_id`` is ``nullable=False``, but a
#: platform-admin routing decision has no tenant of its own — the same mismatch
#: ``bedrock_account_mappings`` avoids by not carrying the mixin at all. A `user`-rung
#: mapping in particular names no org. So platform-scoped events file under this
#: sentinel: a value that cannot collide with a real ``organizations.id`` and that
#: reads as deliberate, rather than an empty string that reads as a bug.
PLATFORM_AUDIT_ORG = "__platform__"


class MappingRejectedError(Exception):
    """A save-time gate refused. Carries the shared reason code, not just prose.

    ``reason`` is R3's vocabulary (:mod:`src.proxy.bedrock_routing_errors`) plus R1's
    two probe codes, so an admin who sees ``assume_role_failed`` here and
    ``assume_role_failed`` in a runtime 502 can connect them (§6.7 item 2).

    ``message`` is user-facing and therefore **never contains the role ARN** — the
    §2.6 redaction rule. The ARN reaches the audit row and the server log instead.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# Scope parsing and existence
# ---------------------------------------------------------------------------


def parse_scope(scope: str) -> tuple[str, str | None, str | None, str | None]:
    """Parse the path scope into ``(scope_type, org_id, team_id, user_id)``.

    Wire form: ``org:<org_id>`` | ``team:<org_id>:<team_id>`` | ``user:<users.id>``.

    One path segment rather than several query parameters, for the reason
    ``person_cap_routes._parse_scope`` gives: the scope IS the identity of the
    resource being addressed, so ``PUT …/mappings/org:acme`` is a complete statement
    while ``PUT …/mappings?scope_type=org`` with no org is a request that has to be
    rejected rather than one that cannot be formed.

    The team form carries BOTH ids because ``teams`` carries ``TenantMixin`` — a
    ``teams.id`` is unique only inside its org, so a team scope naming only the team
    could govern a same-named team in an unrelated tenant (#4344 collision class).

    There is deliberately **no ``platform`` form**: rung 4 is the absence of a
    mapping (§1.2), and ``MAPPING_SCOPE_TYPES`` omits it. Accepting one would give
    the ladder two contradictory ways to say "ambient IRSA".

    The user form takes a canonical ``users.id``, never a Cognito sub (#4647) — see
    :func:`require_scope_exists`, which rejects a sub outright rather than storing a
    mapping that resolves for nobody.

    Raises:
        MappingRejectedError: For any other shape, or an empty id in any position.
            Rejected here rather than stored: ``ck_bedrock_account_mapping_scope``
            would refuse the row anyway, and an IntegrityError from a *client*
            mistake surfaces as a 500 rather than as the correction it is.
    """
    parts = scope.split(":")

    if len(parts) == 2 and parts[0] == "org" and parts[1]:
        return "org", parts[1], None, None
    if len(parts) == 3 and parts[0] == "team" and parts[1] and parts[2]:
        return "team", parts[1], parts[2], None
    if len(parts) == 2 and parts[0] == "user" and parts[1]:
        return "user", None, None, parts[1]

    raise MappingRejectedError(
        "invalid_scope",
        "Scope must be 'org:<org_id>', 'team:<org_id>:<team_id>', or 'user:<user_id>'. "
        "A team scope needs its organization id too, because a team id is unique only "
        "within its own organization. There is no 'platform' scope: the platform "
        "account serves anyone with no rule.",
    )


def format_scope(mapping: BedrockAccountMapping) -> str:
    """The wire scope string for a stored row — the inverse of :func:`parse_scope`.

    Returned on every mapping so the UI addresses rows by a string the server
    produced, rather than re-assembling one and risking a 422 on its own read-back.
    """
    if mapping.scope_type == "user":
        return f"user:{mapping.scope_id_user}"
    if mapping.scope_type == "team":
        return f"team:{mapping.scope_id_org}:{mapping.scope_id_team}"
    return f"org:{mapping.scope_id_org}"


async def load_mapping_for_scope(
    db: AsyncSession, scope_type: str, org_id: str | None, team_id: str | None, user_id: str | None
) -> BedrockAccountMapping | None:
    """The single mapping row addressing this scope, or None.

    Shared by the upsert and the delete so they address a row the same way — two
    hand-written copies of this predicate is how a PUT starts inserting duplicates the
    DELETE then cannot find.

    Uses ``.is_(None)`` for the absent halves rather than ``== None``, because the
    predicate must line up with ``uq_bedrock_account_mapping_scope`` — an expression
    index over ``COALESCE(col, '')``, chosen precisely because SQL NULL comparison is
    not equality. Written with ``==``, a re-author would match nothing, and every
    second PUT for the same scope would trip the unique index as a 500 instead of
    replacing the row.
    """
    return await db.scalar(
        select(BedrockAccountMapping).where(
            BedrockAccountMapping.scope_type == scope_type,
            BedrockAccountMapping.scope_id_org.is_(None) if org_id is None else BedrockAccountMapping.scope_id_org == org_id,
            BedrockAccountMapping.scope_id_team.is_(None) if team_id is None else BedrockAccountMapping.scope_id_team == team_id,
            BedrockAccountMapping.scope_id_user.is_(None) if user_id is None else BedrockAccountMapping.scope_id_user == user_id,
        )
    )


async def require_scope_exists(db: AsyncSession, scope_type: str, org_id: str | None, team_id: str | None, user_id: str | None) -> None:
    """Refuse a mapping aimed at a scope that does not exist (the #4696 pattern).

    A rule scoped to a mistyped, foreign, or deleted id stores cleanly, reads back as
    a configured route, and governs NOBODY — the #4511 inert-config class on the
    surface whose entire purpose is deciding whose bill pays. An existence SELECT at
    write time is the cheap, lifecycle-decoupled alternative to the FK migration 037
    deliberately declined.

    Per rung, mirroring ``person_cap_routes._require_scope_exists``:

    * ``org`` — the ``organizations`` row must exist.
    * ``team`` — at least one ``users`` row must carry the (org, team) pair. Teams
      live in Cognito attributes, so "a team someone is actually in" is the only
      existence a rule can usefully have; a team no user carries governs nobody by
      construction.
    * ``user`` — a ``users`` row with that **id** must exist. Matched on ``id`` only,
      NOT on ``cognito_sub``: ``scope_id_user`` is the canonical id (#4647) and the
      resolver compares it to a canonical id, so accepting a sub here would store a
      row that looks right in the table and never fires. Rejecting it is how the
      admin finds out now instead of after a wrong bill.
    """
    if scope_type == "user":
        exists = await db.scalar(select(User.id).where(User.id == user_id).limit(1))
        if exists is None:
            raise MappingRejectedError(
                "scope_not_found",
                f"No user with id '{user_id}' exists on this platform, so the rule would govern nobody. "
                "This field takes the platform's own user id, not a Cognito sub or a GitHub login.",
            )
        return

    org_exists = await db.scalar(select(Organization.id).where(Organization.id == org_id).limit(1))
    if org_exists is None:
        raise MappingRejectedError(
            "scope_not_found",
            f"No organization with id '{org_id}' exists on this platform; the rule would govern nobody.",
        )

    if scope_type == "team":
        member_exists = await db.scalar(select(User.id).where(User.org_id == org_id, User.team_id == team_id).limit(1))
        if member_exists is None:
            raise MappingRejectedError(
                "scope_not_found",
                f"No member of organization '{org_id}' carries team id '{team_id}'; the rule would govern nobody.",
            )


# ---------------------------------------------------------------------------
# The save-time gates (§4.2, §4.3, §6.7)
# ---------------------------------------------------------------------------


def _require_destination_in_scope(destination: BedrockDestinationRegistry, scope_org_id: str) -> None:
    """§4.2 requirement 1: is this destination legitimate for the scope's tenant?

    *"It must be enforced server-side in the API, not by the dropdown's contents — a
    UI that only lists in-scope options is a usability feature; an API that only
    accepts them is the control."* This function is that control, and it is the whole
    of cross-tenant isolation for admin-authored mappings.

    ``scope_org_id`` is **required, including for the user rung**, where the mapping
    row itself carries no org. The route resolves it from the *target user's own*
    ``users.org_id`` rather than from the caller's token — the caller is a platform
    admin whose token names their own tenant, not the target's, so reading it from the
    token would compare the wrong two values and pass everything.

    ``is_platform_registered`` rows are the §4.2 requirement-2 exception: they have no
    owning tenant to compare against, so any scope an admin names may use them. This
    API does not mint such rows (see ``RegisterNewDestination``), but the registry is
    platform-scoped and R2's schema permits them, so the check handles one rather than
    tripping over a NULL.
    """
    if destination.is_platform_registered:
        return

    if destination.owner_org_id != scope_org_id:
        # The destination's own org is NOT named in the message. Telling an admin
        # which tenant linked an account they may not administer would make this
        # refusal an enumeration oracle over other tenants' AWS accounts.
        raise MappingRejectedError(
            REASON_ACCOUNT_UNLINKED,
            f"That destination is not linked to organization '{scope_org_id}', so a rule for this scope may not route to it. "
            "Register the account for this organization, or pick a destination linked to it.",
        )


async def _reject_personal_credential(db: AsyncSession, destination: BedrockDestinationRegistry, scope_type: str) -> None:
    """Ruling 6 / §4.3, amended for explicit organization Bedrock grants.

    A platform admin may explicitly link an existing personal connection to an
    organization after the shared capability probe passes. That scoped grant is
    the only exception to the original personal-credential prohibition below.

    *"One person's personal role silently serving a whole team's traffic is an
    authority/audit problem."* Expressed as a constraint on the *reference*, which is
    why §1.1 separates mappings from destinations: the predicate is
    ``user_credentials.user_id IS NULL`` (org-scoped, per the all-owners-NULL
    convention in ``vault.py``), or a platform-registered row with no credential at
    all.

    Checked on the column, not on ``UserCredential.owner_scope`` — that is a read-only
    Python property and cannot be filtered in SQL, so a query written against it
    would silently match everything.

    §5.0b notes IAM enforces this *more* strictly than the ruling asks: a user-owned
    connection's trust policy refuses to be assumed for anyone else, so such a
    mapping fails every call. That is the better failure but must not be the
    *discovery* mechanism — an admin reading the runtime ``AccessDenied`` would file a
    platform bug. Hence a named refusal at save time.
    """
    if scope_type == "user":
        # A person's own credential serving their own traffic is the self-service
        # case (§6.4), which is what ruling 2 explicitly allows.
        return
    if destination.credential_id is None:
        return

    owner_user_id = await db.scalar(select(UserCredential.user_id).where(UserCredential.id == destination.credential_id))
    if owner_user_id is not None:
        # An explicit admin link authorizes Bedrock use for this org without
        # transferring the original personal credential to the shared vault.
        grant = await db.scalar(
            select(BedrockConnectionGrant.destination_id).where(
                BedrockConnectionGrant.destination_id == destination.id,
                BedrockConnectionGrant.credential_id == destination.credential_id,
                BedrockConnectionGrant.org_id == destination.owner_org_id,
            )
        )
        if grant is not None:
            return
        raise MappingRejectedError(
            "personal_credential_for_shared_scope",
            f"That destination is one person's personal AWS connection, so it cannot serve a {scope_type}-wide rule. "
            "Use 'Use existing AWS connection' to explicitly link it to this organization after verifying shared Bedrock access.",
        )


def require_routable_connection(credential: UserCredential) -> tuple[str, str]:
    """A connection's ``(account_id, role_arn)``, or a refusal. Shared by both surfaces.

    Two callers need exactly this check and must refuse identically: the platform
    admin promoting somebody's connection into the registry
    (``routes.register_destination``) and the person selecting their own on the
    credentials page (``self_routes.put_my_selection``, §6.4). A second copy is how one
    of them starts accepting a ``pending`` row.

    §4.4 is why ``pending`` is a refusal and not a lower priority: the role does not
    exist in the destination account until the customer's CloudFormation stack finishes,
    so merely *starting* a connect flow would otherwise be able to reroute traffic onto
    an account that fails every call.

    Raises:
        MappingRejectedError: ``connection_not_verified`` when the row is not verified,
            or is verified but carries no account id or role — a shape that cannot be
            probed, let alone routed to.
    """
    scopes = credential.scopes or {}
    if scopes.get("status") != "verified":
        raise MappingRejectedError(
            "connection_not_verified",
            "That AWS connection has not been verified yet. Finish its CloudFormation stack and verify it first.",
        )
    account_id = scopes.get("account_id")
    role_arn = scopes.get("role_arn")
    if not account_id or not role_arn:
        raise MappingRejectedError(
            "connection_not_verified",
            "That AWS connection is missing its account id or role, so it cannot be used as a routing destination.",
        )
    return account_id, role_arn


def build_destination_from_credential(
    credential: UserCredential,
    *,
    account_id: str,
    role_arn: str,
    actor_id: str,
    label: str | None = None,
) -> BedrockDestinationRegistry:
    """A registry row for an existing AWS connection. Not added to the session.

    ``owner_org_id`` comes from the credential's **own** tenant and there is no
    parameter to override it — the same construction R4 relied on, kept in one place now
    that the self-service path builds these rows too. That absence is what makes the
    §4.2 ownership check meaningful later: no caller can mislabel a connection as
    belonging to a tenant it does not.

    ``routing_capable`` and ``verified_at`` are left at their defaults (False / NULL).
    Whoever registers the row runs the probe, and :func:`validate_mapping_target` is
    what stamps them — so a row that has never been probed cannot claim it has.
    """
    return BedrockDestinationRegistry(
        id=new_uuid(),
        account_id=account_id,
        role_arn=role_arn,
        credential_id=credential.id,
        owner_org_id=credential.org_id,
        is_platform_registered=False,
        label=label or credential.label,
        region=(credential.scopes or {}).get("default_region", "us-east-1"),
        registered_by_user_id=actor_id,
    )


async def find_or_create_destination_for_credential(
    db: AsyncSession,
    credential: UserCredential,
    *,
    actor_id: str,
) -> BedrockDestinationRegistry:
    """The registry row for this connection, reusing an existing one if there is one.

    A mapping references a *destination*, never a credential (ruling 4a), so the
    self-service path has to have a registry row to point at. It is the only writer that
    **re-uses** one rather than always minting one, and the reason is the shape of its
    caller: a person re-picking their own account is an ordinary, repeatable action, so
    always inserting would grow the registry by one orphan row per click, each of them a
    separate ``used_by``-less entry in the admin's destinations table.

    Keyed on ``credential_id``, which is the identity of the thing being pointed at. Not
    on ``account_id``: one account may legitimately appear several times in the registry
    (the model documents this — platform-wide plus tenant-linked, with different roles),
    and collapsing those would let a person's selection silently land on somebody else's
    row for the same account.

    Raises:
        MappingRejectedError: from :func:`require_routable_connection`.
    """
    account_id, role_arn = require_routable_connection(credential)

    existing = await db.scalar(
        select(BedrockDestinationRegistry).where(
            BedrockDestinationRegistry.credential_id == credential.id,
            BedrockDestinationRegistry.owner_org_id == credential.org_id,
            ~select(BedrockConnectionGrant.destination_id).where(BedrockConnectionGrant.destination_id == BedrockDestinationRegistry.id).exists(),
        )
    )
    if existing is not None:
        return existing

    destination = build_destination_from_credential(
        credential,
        account_id=account_id,
        role_arn=role_arn,
        actor_id=actor_id,
    )
    db.add(destination)
    # Flushed so the row has been assigned before a mapping references it: the mapping
    # carries `destination_id` with no FK, and a caller that committed the mapping while
    # this row was still pending would leave a rule pointing at nothing.
    await db.flush()
    return destination


async def load_destination(db: AsyncSession, destination_id: str) -> BedrockDestinationRegistry:
    """Fetch a destination row, or refuse with the shared ``account_unlinked`` reason.

    Refusing rather than 404ing: from the admin's side "the destination you picked is
    gone" is the same class of event as "the destination does not work", and both leave
    the mapping unwritten. Using R3's code keeps the vocabulary one vocabulary.
    """
    destination = await db.scalar(select(BedrockDestinationRegistry).where(BedrockDestinationRegistry.id == destination_id).with_for_update())
    if destination is None:
        raise MappingRejectedError(
            REASON_ACCOUNT_UNLINKED,
            "That destination no longer exists. Refresh the destinations list and pick again.",
        )
    return destination


async def _destination_external_id(db: AsyncSession, destination: BedrockDestinationRegistry, secrets: SecretsManagerHelper) -> str | None:
    """The destination's ExternalId, read from Secrets Manager at probe time.

    Deliberately **not** copied onto the registry row: it is a shared secret for
    confused-deputy protection, and a second copy doubles the places it can leak
    from. Same read the signer does (``bedrock_signing._resolve_external_id``), same
    payload shape ``connect_start`` writes.

    The value is returned for the probe to send and is **never logged** — not on the
    success path, not in the exception handler, not in an audit detail. A probe's
    diagnostics name the account and the STS error code; that is what an operator acts
    on, and it is all they need.

    Returns None when there is nothing to read — no linked connection, a missing
    credential row, or an unreadable secret. None is not a shortcut past the gate: the
    probe then assumes *without* an ExternalId, which any role whose trust policy
    requires one will reject, and that rejection is exactly the outcome §6.7 wants
    surfaced at save time. Guessing a refusal reason from a bookkeeping gap would be
    less accurate than letting the probe speak.
    """
    # Platform registration issues the stable trust ID; personal connections
    # continue to use their separately verified credential trust binding.
    if destination.is_platform_registered and destination.owner_org_id is None and not destination.credential_id:
        return f"adp-platform:{destination.id}"

    if not destination.credential_id:
        return None

    secret_arn = await db.scalar(select(UserCredential.secret_arn).where(UserCredential.id == destination.credential_id))
    if secret_arn is None:
        return None
    try:
        payload = json.loads(await asyncio.to_thread(secrets.get_secret, secret_arn))
    except Exception as exc:  # noqa: BLE001 - SM throttle, deleted secret and bad JSON share one remediation
        # Type name only. The exception text can carry the secret ARN.
        logger.warning(
            "Routing save-time probe could not read the destination credential secret",
            extra={"destination_id": destination.id, "error": type(exc).__name__},
        )
        return None
    return payload.get("external_id")


async def test_assume_destination(
    db: AsyncSession,
    destination: BedrockDestinationRegistry,
    *,
    secrets: SecretsManagerHelper,
    probe_user_id: str,
) -> tuple[bool, str | None]:
    """Run the real test assume + Bedrock invoke check. §6.7 items 1 and 3.

    Delegates to :func:`src.shared.services.routing_probe.probe_routing_destination` —
    the ONE probe. §6.7 item 1: *"Reuse it; do not write a second assume probe. Two
    probes with different conditions is how 'verified here, broken there' happens."*
    There is no assume-role call anywhere else in this package, and the tests assert
    that by reading this module's source.

    A failure here is **not** special-cased away. A destination that cannot be assumed
    from the gateway account SHOULD come back not-capable: that is the gate working.

    Returns ``(capable, reason)``; never raises.
    """
    external_id = await _destination_external_id(db, destination, secrets)
    return await probe_routing_destination(
        role_arn=destination.role_arn,
        external_id=external_id,
        default_region=destination.region,
        user_id=probe_user_id,
        label=destination.label,
    )


async def validate_mapping_target(
    db: AsyncSession,
    *,
    destination: BedrockDestinationRegistry,
    scope_type: str,
    scope_org_id: str,
    secrets: SecretsManagerHelper,
    probe_user_id: str,
) -> None:
    """Every save-time gate, cheapest first. Raises :class:`MappingRejectedError` or returns.

    Order (and it matters):

    1. **Ownership** (§4.2 req 1) — a pure SQL comparison. A cross-tenant attempt is
       refused without ever contacting another account's STS.
    2. **Personal credential** (§4.3) — one indexed read.
    3. **Test assume + invoke** (§6.7) — the network round trip, run last and only
       for a target that has already passed the free checks.

    The alternative ordering would probe first and report "assume_role_failed" for a
    request whose real problem is that it names another tenant's account — a true
    statement that sends the admin to debug the wrong thing.

    A destination whose stored ``routing_capable``/``verified_at`` are already false is
    NOT short-circuited to a refusal: the probe re-runs and may flip it to true, which
    is what makes the "Re-verify" action and a fresh registration work through the same
    path. What is never skipped is the probe itself.
    """
    _require_destination_in_scope(destination, scope_org_id)
    await _reject_personal_credential(db, destination, scope_type)

    capable, reason = await test_assume_destination(db, destination, secrets=secrets, probe_user_id=probe_user_id)
    if not capable:
        raise MappingRejectedError(
            reason or "routing_probe_inconclusive",
            f"The platform could not prove it can serve Bedrock calls from account {destination.account_id}, "
            "so this rule was rejected rather than stored. Nothing has changed. "
            "Re-run the routing CloudFormation template in that account, then try again.",
        )

    # The probe just proved both properties. Recording them is what makes the
    # destination selectable and keeps the resolver's `is_usable_for_routing` read
    # consistent with the last thing actually observed.
    destination.routing_capable = True
    destination.verified_at = datetime.now(UTC)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def mapping_source(mapping: BedrockAccountMapping) -> str:
    """``self`` or ``platform_admin`` for one mapping row — derived, not stored.

    §1.4 requires the effective display to name who authored the winning rule, and
    migration 037 has no ``authored_by_role`` column. It does not need one: on a
    ``user`` rung, ``authored_by_user_id == scope_id_user`` iff the person wrote their
    own row, because both are canonical ``users.id`` (#4647) in the same namespace.
    Team and org rungs have no self author by construction.

    Deriving beats adding a column here: a stored flag could disagree with the ids it
    describes, and there is no migration in this issue's scope to add one anyway.
    """
    if mapping.scope_type == "user" and mapping.authored_by_user_id == mapping.scope_id_user:
        return "self"
    return "platform_admin"


def require_not_admin_pinned(mapping: BedrockAccountMapping | None) -> None:
    """Refuse a self-service write over a row a platform admin authored (§1.4).

    **This is the enforcement half of "admin wins", and without it the self surface
    silently reverses a platform admin's decision.** ``uq_bedrock_account_mapping_scope``
    permits exactly one row per scope, so the user rung is a single row that both the
    admin surface and the self surface upsert. "Admin wins" therefore cannot be a
    precedence question between two rows — there is only ever one — which leaves the
    write itself as the only place the precedence can live. A self ``PUT`` that
    overwrote an admin-authored row, or a self ``DELETE`` that removed one, would be a
    person granting themselves authority over the very decision the override exists to
    take away from them.

    :func:`mapping_source` is the discriminator, and it works because both ids are
    canonical ``users.id`` in one namespace (#4647): the row is the person's own iff
    ``authored_by_user_id == scope_id_user``.

    A ``None`` mapping is not pinned — there is nothing to overwrite.

    Raises:
        MappingRejectedError: ``pinned_by_platform_admin``. The message names the
            account so the person can see what governs them, and says who to ask —
            neither the role ARN (§2.6) nor which admin authored it.
    """
    if mapping is None:
        return
    if mapping_source(mapping) == "platform_admin":
        raise MappingRejectedError(
            "pinned_by_platform_admin",
            "A platform admin has chosen which AWS account serves your Bedrock calls, and that choice takes "
            "precedence over your own. Ask a platform admin to change or remove it.",
        )


async def load_self_selection(db: AsyncSession, user_id: str) -> BedrockAccountMapping | None:
    """The caller's own user-rung mapping row, whoever authored it.

    Returned including the admin-authored case, deliberately: the two writes that guard
    against one (:func:`require_not_admin_pinned`) and the read that has to *disclose*
    one both need the same row, and a loader that filtered admin rows out would make the
    override look like an absence — the display §1.4 forbids.
    """
    return await load_mapping_for_scope(db, "user", None, None, user_id)


async def destination_usage_counts(db: AsyncSession, destination_ids: list[str]) -> dict[str, int]:
    """How many mappings reference each destination — the "USED BY (n rules)" column.

    One grouped query for the whole page rather than one per row: under fail-closed,
    deleting a referenced destination is an outage (§8.3), so this count has to be
    cheap enough to always render. ``ix_bedrock_account_mapping_destination`` exists
    for exactly this lookup.

    Destinations with no mappings are absent from the result; callers default to 0.
    """
    if not destination_ids:
        return {}
    rows = await db.execute(
        select(BedrockAccountMapping.destination_id, func.count(BedrockAccountMapping.id))
        .where(BedrockAccountMapping.destination_id.in_(destination_ids))
        .group_by(BedrockAccountMapping.destination_id)
    )
    return {destination_id: count for destination_id, count in rows.tuples().all()}


async def resolve_effective(db: AsyncSession, user_id: str) -> dict:
    """Which destination serves this person, from which rung, and what it shadows.

    §6.3 element 1. Walks the same ladder in the same order as
    :class:`~src.proxy.bedrock_routing.BedrockRoutingResolver`, with the same
    ``is_usable_for_routing`` skip rule — an unusable destination is NO MATCH and the
    walk continues (§4.4), because that is what the request path does and a lookup
    that disagreed with it would be worse than no lookup at all.

    Not delegated to the resolver itself, and the difference is the point: the
    resolver takes a :class:`TokenContext` (a live request's authenticated claims) and
    returns only the winner. This answers "who serves *that* person" for an admin
    holding no token of theirs, and it must also report the runner-up so the panel can
    say what removing a rule would do. What is shared is the ordering and the skip
    rule, and the tests pin both against the resolver's own constants.

    Returns a dict shaped for :class:`~.schemas.EffectiveMappingResponse`.
    """
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise MappingRejectedError(
            "scope_not_found",
            f"No user with id '{user_id}' exists on this platform, so nothing can be resolved for them.",
        )

    # One query for all three rungs, same predicate shape as the resolver's, then
    # ordered in Python. Each rung's predicate names every column that identifies it:
    # the team rung matches the (org, team) PAIR because a `teams.id` is unique only
    # inside its org, so matching the team alone would pick up another tenant's rule
    # for a same-named team.
    predicates = [
        and_(
            BedrockAccountMapping.scope_type == "user",
            BedrockAccountMapping.scope_id_user == user.id,
        )
    ]
    if user.org_id and user.team_id:
        predicates.append(
            and_(
                BedrockAccountMapping.scope_type == "team",
                BedrockAccountMapping.scope_id_org == user.org_id,
                BedrockAccountMapping.scope_id_team == user.team_id,
            )
        )
    if user.org_id:
        predicates.append(
            and_(
                BedrockAccountMapping.scope_type == "org",
                BedrockAccountMapping.scope_id_org == user.org_id,
            )
        )

    rows = (
        (
            await db.execute(
                select(BedrockAccountMapping, BedrockDestinationRegistry)
                .join(
                    BedrockDestinationRegistry,
                    BedrockDestinationRegistry.id == BedrockAccountMapping.destination_id,
                )
                .where(or_(*predicates))
            )
        )
        .tuples()
        .all()
    )

    by_rung: dict[str, list[tuple[BedrockAccountMapping, BedrockDestinationRegistry]]] = {}
    for mapping, destination in rows:
        by_rung.setdefault(mapping.scope_type, []).append((mapping, destination))

    # Every usable match in ladder order. The first is the winner; the second is what
    # the winner shadows, which is what "remove this rule" would fall through to.
    usable: list[tuple[str, BedrockAccountMapping, BedrockDestinationRegistry]] = [
        (rung, mapping, destination) for rung in RUNG_ORDER for mapping, destination in by_rung.get(rung, []) if destination.is_usable_for_routing
    ]

    if not usable:
        return {
            "user_id": user_id,
            "rung": "platform",
            "account_id": None,
            "destination_id": None,
            "destination_label": None,
            "source": None,
            "overrides_self_selection": False,
            "shadowed_rung": None,
            "shadowed_account_id": None,
        }

    rung, mapping, destination = usable[0]
    shadowed = usable[1] if len(usable) > 1 else None
    source = mapping_source(mapping)
    return {
        "user_id": user_id,
        "rung": rung,
        "account_id": destination.account_id,
        "destination_id": destination.id,
        "destination_label": destination.label,
        "source": source,
        # §1.4 "admin wins": an admin-authored USER rung row IS the override of the
        # person's own selection, and that is the state the UI must state outright.
        "overrides_self_selection": rung == "user" and source == "platform_admin",
        "shadowed_rung": shadowed[0] if shadowed else "platform",
        "shadowed_account_id": shadowed[2].account_id if shadowed else None,
    }


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def write_audit(
    db: AsyncSession,
    *,
    event_type: str,
    org_id: str,
    actor_id: str | None,
    details: dict | None,
) -> None:
    """Record a routing authoring event. Same shape as ``assume_role_routes._write_audit``.

    §4.2's audit requirement: *"audit the authoring events too (who mapped which scope
    to which destination, and every save-time assume failure) — under ruling 4's
    platform-admin model this is the record of who decided whose bill pays, which is
    the question the whole feature exists to answer."*

    ``role_arn`` belongs in ``details`` and **nowhere user-facing** (§2.6) — the same
    split ``assume_role_routes`` makes at its failed-assume call site.

    Flushes rather than commits, so the audit row shares the caller's transaction and
    an authoring write plus its audit record either both land or neither does. The
    refusal path is the exception and commits deliberately — see
    :func:`write_refusal_audit`.
    """
    db.add(AuditLog(org_id=org_id, event_type=event_type, actor_id=actor_id, details=details))
    await db.flush()


async def write_refusal_audit(
    db: AsyncSession,
    *,
    event_type: str,
    org_id: str,
    actor_id: str | None,
    details: dict | None,
) -> None:
    """Record a refused write, on its own transaction, and never fail because of it.

    A refusal writes no mapping, so there is no caller transaction to ride — and
    rolling back is exactly what the route is about to do. Committing here is what
    makes "every save-time assume failure" (§4.2) an actual record rather than
    something the rollback erases.

    Swallows its own failures: a save-time refusal must reach the admin as a clear
    422 even if the audit table is unwritable. Losing the audit row is bad; replacing
    an actionable rejection with a 500 is worse, because the admin then cannot tell a
    refused mapping from a broken platform.
    """
    try:
        db.add(AuditLog(org_id=org_id, event_type=event_type, actor_id=actor_id, details=details))
        await db.commit()
    except Exception:  # noqa: BLE001 - the refusal must survive an audit-write failure
        logger.exception("Could not record a Bedrock routing refusal audit event", extra={"event_type": event_type})
        await db.rollback()


def invalidate_signer_cache(role_arn: str) -> None:
    """Drop cached destination credentials for a role. Called on delete and re-verify.

    R3 left this hook for R4 (``bedrock_signing.py:241``) and was explicit that
    *correctness* does not depend on it — ``destination_updated_at`` is in the cache
    key, so an edit is already self-evicting. What the hook adds is promptness on
    **delete**, where there is no updated row to change the key, and immediate removal
    of credentials rather than merely making them unreachable.

    Per-process and in-memory, so with multiple gateway pods only the pod handling this
    request is cleared. That is acceptable for the same reason R3 gives: the key, not
    the hook, is the mechanism.

    Never raises — a cache-hygiene call must not fail an admin's delete.
    """
    try:
        from src.proxy.bedrock_signing import bedrock_destination_signer

        dropped = bedrock_destination_signer.cache.invalidate_destination(role_arn)
        if dropped:
            logger.info("Dropped cached destination credentials", extra={"entries": dropped})
    except Exception:  # noqa: BLE001 - hygiene, not correctness
        logger.warning("Could not invalidate cached destination credentials", exc_info=True)


def invalidate_resolver_existence_cache() -> None:
    """Force the resolver to re-read "do any mappings exist?" after a write.

    The resolver answers that question from a 60s process-local cache
    (``_MAPPING_EXISTENCE_TTL_SECONDS``) so an install with no mappings pays zero
    queries per model call (§2.2). The consequence for this surface: on an install
    whose mapping table was **empty**, the first rule authored here would otherwise
    not be consulted at all until the cache expired — an admin saves a rule, watches
    traffic keep landing on the platform account, and concludes the feature is broken.

    Clearing the flag on write removes that confusion for the pod that took the write.
    Other pods still wait out their own TTL, which is bounded and documented; this is
    a promptness improvement, not a distributed invalidation.
    """
    bedrock_routing_resolver._mappings_exist_cache = None
