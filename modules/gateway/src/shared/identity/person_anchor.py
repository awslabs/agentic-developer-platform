"""The cross-org person anchor — Issue #4629 (#4620 · C3).

Design note ``docs/design-notes/4620-cross-org-person-budgets.md`` §3.3, §4.1.

A person-level spend cap must be keyed on something that is **the same string in
every tenant that person works in**. This module produces and validates that key.

**Why not ``users.id``.** ``users`` carries ``TenantMixin``, so a person
independently onboarded into two orgs legitimately has TWO ``users.id`` values
(``tests/shared/test_resolve_root_user_entity_id.py`` covers exactly that shape:
one GitHub account, distinct ids per org). A cap keyed on one of them would miss
the person's spend in the other org — which is the inert-cap class of #4511, one
layer up: a cap that exists, shows a number, and governs nothing. ``users.id``
stays the *ledger* key (``budget_usage.entity_id`` for ``root_user`` rows); the
anchor is the cross-org *join* key.

**Why the GitHub numeric id.** It is the id ``user_identities`` already stores
per tenant for the same GitHub account (``resolver._resolve_via_github_identity``),
it is immutable under a username change, and it is what the webhook identity
resolver keys a person by with no org in the lookup key. §3.3 names it as the
anchor for precisely this reason.

**Why the ``github:`` qualifier.** The same anti-collision reasoning as #4344's
``service:`` prefix: a bare numeric id could alias another provider's id space
once a second provider is anchored, and two id namespaces under one unique
constraint is how one person's cap comes to govern another person's spend.
Qualification happens here, in one place, so the authored key and any future
reader's key are the same string by construction.

Both public resolvers **raise** rather than degrading, and that direction is
deliberate: every caller is a *write* path (authoring a cap) or a read that must
not invent a target, and persisting an unresolvable anchor is precisely the bug
(#4511). Contrast ``resolve_canonical_user_id``, which degrades to its input
because its callers are read paths that must not 500.

The namespace registry (#4843)
------------------------------

This module is now the **single registry** of accepted anchor namespaces and the
**total order** they are resolved in (design note
``docs/design-notes/4828-platform-native-org-team-user.md`` §4, ruling R7):

1. ``github:<numeric_id>`` — **priority 1, unchanged.**
2. ``directory:<idp_object_id>`` — an AD/Entra object id, for a person the
   directory manages and who may have no GitHub identity at all.
3. ``users:<canonical_id>`` — the pre-existing terminal fallback.

**Why GitHub keeps priority 1, and why that is the whole safety property.**
``person_budget_configs.person_anchor`` stores the anchor *string* durably. Any
reorder that put ``directory:`` above ``github:`` would silently re-key every
person holding both identities: their live cap row would still exist, still
display a number, and never match again — the #4511 inert-cap class, applied to
every dual-identity person at once. So the order is not a preference, it is a
compatibility constraint, and ``tests/shared/identity/test_person_anchor_namespaces.py``
pins it.

**Why the parser had to change (defect (a) in §4).** It previously hard-rejected
every string not starting with ``github:``, while the read/fusion path
(``person_ledger.resolve_person_identity``) *already produced* ``users:<id>`` for
anybody with no GitHub identity. So a GitHub-less person got an anchor from the
read side that the authoring side could not parse at all. The parser is now
registry-aware over all three namespaces.

**Authorable is narrower than parseable, deliberately.** ``users:`` parses (the
read surface produces it and must be able to round-trip its own output) but is
**refused by the write guard** with a 422. Nothing enforces a cap keyed on the
internal fallback: the enforcement layer resolves a person to a provider-backed
anchor, so a ``users:``-keyed row would display a limit and govern nothing. That
is the same honest refusal the module already applies to an unlinked GitHub id,
for the same reason — a rejection is better than a row that looks like success.

**One composer.** ``format_person_anchor`` is the only place an anchor string is
built. Before #4843 the ``github:`` form was hand-rolled in two other modules
(``person_ledger``, ``enforcement_service``) with only the prefix constant shared;
with a second namespace in play that is a write/read mismatch waiting to happen,
so both now call this function and a test greps for regressions.
"""

import logging

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.exceptions import BedrockGatewayError
from src.shared.identity.providers import IdentityProvider
from src.shared.models.organization import User

logger = logging.getLogger("bedrockgateway.identity")

# ---------------------------------------------------------------------------
# The namespace registry. See the module docstring for why the ORDER is a
# compatibility constraint rather than a preference.
# ---------------------------------------------------------------------------

# Provider-backed namespaces, in resolution order. A person is anchored on the
# FIRST of these for which they hold a `user_identities` row. Each entry is an
# `IdentityProvider` rather than a bare string so the anchor's namespace and the
# `user_identities.provider` value it is resolved through cannot drift — they are
# the same concept, and spelling it twice is how they diverge.
PERSON_ANCHOR_PROVIDER_PRECEDENCE: tuple[IdentityProvider, ...] = (
    IdentityProvider.github,
    IdentityProvider.directory,
)

# The terminal fallback namespace: this platform's own canonical `users.id`. NOT
# an `IdentityProvider` — there is no `user_identities` row behind it, which is
# precisely the state it names ("we know who this is, and they have no external
# identity we can anchor on"). Produced by the read/fusion path, parseable here,
# and NOT authorable — see the module docstring.
PERSON_ANCHOR_INTERNAL_NAMESPACE = "users"

# Kept as a module-level name because callers import it and a test pins it to
# `IdentityProvider.github`. DERIVED from the registry rather than declared
# beside it, so the precedence tuple is the one source of truth.
PERSON_ANCHOR_GITHUB_PREFIX = f"{PERSON_ANCHOR_PROVIDER_PRECEDENCE[0].value}:"

# Every namespace `parse_person_anchor` accepts, in precedence order with the
# internal fallback last. Parseable ⊃ authorable, deliberately.
PERSON_ANCHOR_NAMESPACES: tuple[str, ...] = tuple(p.value for p in PERSON_ANCHOR_PROVIDER_PRECEDENCE) + (PERSON_ANCHOR_INTERNAL_NAMESPACE,)

# Namespaces a cap may be STORED under. The internal fallback is excluded: see
# the module docstring's "authorable is narrower than parseable".
PERSON_ANCHOR_AUTHORABLE_NAMESPACES: frozenset[str] = frozenset(p.value for p in PERSON_ANCHOR_PROVIDER_PRECEDENCE)

# Named in the 422 so a caller who supplied the wrong thing is told the right
# thing rather than left to guess. Built from the registry so a new namespace
# cannot be admitted while the error message still names only the old ones.
_ACCEPTED_ANCHOR_FORM = "an anchor of the form " + " or ".join(f"'{ns}:<id>'" for ns in PERSON_ANCHOR_NAMESPACES)


class UnresolvablePersonAnchorError(BedrockGatewayError):
    """A person anchor cannot be resolved to a real, linked GitHub identity.

    422 rather than 400 or 404: the value is syntactically checkable but it does
    not identify a person whose spend this platform can ever attribute. Storing a
    cap against it would produce a row that displays a limit and governs nothing —
    the #4511 failure mode, which is worse than a rejection because it looks like
    success.

    Deliberately NOT a 404 for the "no such identity" case either: that would make
    this endpoint an existence oracle for accounts on the platform.
    """

    def __init__(self, supplied_anchor: str, reason: str):
        super().__init__(
            "unresolvable_person_anchor",
            f"Cannot resolve person anchor '{supplied_anchor}': {reason}. Expected {_ACCEPTED_ANCHOR_FORM}.",
            422,
            {"supplied_anchor": supplied_anchor, "reason": reason, "accepted_form": _ACCEPTED_ANCHOR_FORM},
        )


def format_person_anchor(identifier: str, namespace: str = IdentityProvider.github.value) -> str:
    """Build a person anchor from an identifier and its namespace.

    **The single place an anchor string is composed** — that is this function's
    entire job, and the reason it exists rather than an f-string at each call
    site. An authored anchor and a resolved one must be byte-identical or the cap
    is inert (#4511), and two hand-rolled f-strings in different modules is how
    they come to differ.

    ``namespace`` defaults to ``github`` so every pre-#4843 call is unchanged.

    Args:
        identifier: The id inside the namespace — a GitHub numeric id, a directory
            object id, or a canonical ``users.id``.
        namespace: One of :data:`PERSON_ANCHOR_NAMESPACES`.

    Returns:
        The qualified anchor string.

    Raises:
        ValueError: An unregistered namespace. A programming error, not a request
            error, so it is NOT the 422: no caller passes user input here (the
            namespace always comes from the registry or from
            :func:`parse_person_anchor`'s validated output), and silently
            composing an anchor in an unknown namespace would write a key nothing
            can ever resolve.
    """
    if namespace not in PERSON_ANCHOR_NAMESPACES:
        raise ValueError(f"Unknown person-anchor namespace {namespace!r}. Must be one of: {sorted(PERSON_ANCHOR_NAMESPACES)}")
    return f"{namespace}:{identifier}"


def parse_person_anchor(anchor: str) -> tuple[str, str]:
    """Validate an anchor's *shape* and return ``(namespace, identifier)``.

    Shape only — this does not check that the identifier names anybody, nor that
    the namespace is one a cap may be stored under. Use
    :func:`resolve_person_anchor` when the anchor is about to be persisted.

    Registry-aware over all of :data:`PERSON_ANCHOR_NAMESPACES` (#4843, defect
    (a)). Previously this accepted ``github:`` alone, which made the read path's
    own ``users:`` output unparseable by the authoring side — so a person with no
    GitHub identity could not be handled end-to-end at all.

    Returns:
        ``(namespace, identifier)``. The namespace is returned rather than
        discarded because every caller needs it: the resolver must know which
        ``user_identities.provider`` to look the identifier up in, and the write
        guard must know whether the namespace is authorable.

    Raises:
        UnresolvablePersonAnchorError: 422. The three rejected shapes each map to
        a real way an inert row gets written: an unregistered/missing namespace
        (an id in some namespace nothing resolves), an empty id (a key no ledger
        row can carry), and a whitespace-padded id (a key that compares unequal
        to the same person's real one, so the cap silently governs nobody).
    """
    namespace, separator, identifier = anchor.partition(":")
    if not separator or namespace not in PERSON_ANCHOR_NAMESPACES:
        raise UnresolvablePersonAnchorError(
            anchor,
            f"the anchor must start with one of {sorted(PERSON_ANCHOR_NAMESPACES)} followed by ':'",
        )

    if not identifier:
        raise UnresolvablePersonAnchorError(anchor, f"the anchor carries no {namespace} identifier")
    if identifier != identifier.strip():
        raise UnresolvablePersonAnchorError(anchor, f"the {namespace} identifier must not carry surrounding whitespace")
    return namespace, identifier


def is_authorable_person_anchor(anchor: str) -> bool:
    """Whether a cap row may be STORED under this anchor.

    True for a provider-backed namespace, False for the internal ``users:``
    fallback and for anything unparseable. Exists so the enforcement layer can
    ask the registry the question instead of testing ``startswith(github:)`` —
    a test that silently excluded every new namespace from enforcement while the
    authoring side happily stored one (a cap that displays and never governs).
    """
    try:
        namespace, _ = parse_person_anchor(anchor)
    except UnresolvablePersonAnchorError:
        return False
    return namespace in PERSON_ANCHOR_AUTHORABLE_NAMESPACES


async def resolve_person_anchor(db: AsyncSession, supplied_anchor: str) -> str:
    """Validate a supplied anchor names a real linked GitHub identity.

    The guard on the platform-admin authoring path, where the anchor arrives from
    a request rather than from a token. Checking the shape is not enough: an
    anchor whose id no ``user_identities`` row carries can never match a settled
    ``root_user`` ledger row, so the cap it keys would be inert.

    The lookup is **deliberately org-free** — that is the whole point of the
    anchor, and it is not a relaxed tenant predicate: the caller has already been
    established as a platform admin (the only party §4.2 permits to author for
    somebody else), and the query reads nothing but the existence of an identity
    link. No tenant's figures are returned.

    The namespace is taken from the supplied anchor and the identifier is looked
    up in THAT provider (#4843) — not in ``github`` regardless, which would
    resolve a ``directory:`` anchor against GitHub's id space and either 422 a
    valid directory user or, worse, match an unrelated person whose GitHub id
    happens to equal the supplied object id. Two id namespaces under one lookup is
    exactly what the ``github:`` qualifier exists to prevent.

    Args:
        db: Async session bound to the gateway DB.
        supplied_anchor: The raw anchor from the request path.

    Returns:
        The anchor, normalised through :func:`format_person_anchor` so a stored
        key can never differ from a resolved one by spelling.

    Raises:
        UnresolvablePersonAnchorError: 422; nothing is persisted. Including for a
            syntactically valid ``users:`` anchor — see below.
    """
    namespace, identifier = parse_person_anchor(supplied_anchor)

    if namespace not in PERSON_ANCHOR_AUTHORABLE_NAMESPACES:
        # Parseable but not authorable (#4843). The read surface produces
        # `users:<canonical id>` for a person with no external identity, so the
        # parser must accept it — but no cap can be ENFORCED under it: the
        # enforcement layer resolves a person to a provider-backed anchor, so a
        # row keyed on the internal fallback would display a limit and stop
        # nothing. Refusing it is the same #4511 reasoning as an unlinked id.
        raise UnresolvablePersonAnchorError(
            supplied_anchor,
            f"'{namespace}:' is this platform's internal fallback key and no limit can be enforced against it; "
            f"anchor the person on a linked external identity instead",
        )

    # Imported here, not at module scope: src.shared.models.vault imports
    # src.shared.identity.providers, so a top-level import of UserIdentity makes
    # this package and the models package circular at collection time. Same
    # reasoning as resolver._resolve_via_github_identity.
    from src.shared.models.vault import UserIdentity

    linked = await db.scalar(
        select(UserIdentity.id)
        .where(
            UserIdentity.provider == namespace,
            UserIdentity.provider_user_id == identifier,
        )
        .limit(1)
    )
    if linked is None:
        raise UnresolvablePersonAnchorError(supplied_anchor, f"no {namespace} identity with this id is linked to any user on this platform")

    return format_person_anchor(identifier, namespace)


async def resolve_caller_person_anchor(db: AsyncSession, caller_id: str) -> tuple[str, str]:
    """Resolve the signed-in caller's own anchor from their token identity.

    Used by the self-service path, which accepts **no** anchor parameter — the
    caller's anchor is derived here so there is nothing for a request to name and
    therefore no way to author a cap for somebody else. That derivation is the
    access control on that route, exactly as ``me_routes.py``'s parameterlessness
    is for the read path.

    Two lookups, both org-free:

    1. ``caller_id`` (the Cognito ``sub``) to a ``users`` row. Matched on
       ``cognito_sub`` **or** ``id`` because some callers reach budget code with
       ``user_id`` already rewritten to the canonical id
       (``auth/vault_routes._resolve_user_id_in_context``, #3989) — matching only
       the sub would deny those callers their own cap.
    2. That user's ``user_identities`` row in the FIRST provider of
       :data:`PERSON_ANCHOR_PROVIDER_PRECEDENCE` they hold, for the anchor id.

    Step 2 walks the precedence rather than querying ``github`` alone (#4843), so a
    directory-managed person with no GitHub account resolves their own anchor
    instead of 422ing. GitHub is tried first, so a person holding both identities
    resolves to the byte-identical string they resolved to before this change and
    their existing cap keeps enforcing.

    Args:
        db: Async session bound to the gateway DB.
        caller_id: ``TokenContext.user_id``.

    Returns:
        ``(anchor, canonical_user_id)`` — the caller's own person anchor plus the
        canonical ``users.id`` it was resolved through. The second element exists
        for the audit trail: ``TokenContext.user_id`` is a Cognito sub on the
        ordinary JWT path but a canonical id on #3989-rewritten paths, and
        persisting it raw would accumulate two id namespaces in
        ``authored_by_user_id`` (whose column contract is canonical ids). The
        canonical id is already in hand here, so callers persist this one.

    Known limitation (#4620 review): only the ``users`` row the caller's session
    id matches is consulted for a GitHub identity. A person who onboarded org A
    via GitHub but was email-invited into org B (a second ``users`` row with its
    own Cognito account and NO ``user_identities`` row) resolves nothing from an
    org-B session and 422s, even though their cross-org key exists on row A.
    There is no server-side join between two such rows — they share no key — so
    this is honest-refusal territory until an identity-linking flow exists.

    Raises:
        UnresolvablePersonAnchorError: 422 when the caller has no ``users`` row or
            no identity in any registered provider namespace. Such a person has no
            cross-org key, so a cap for them could only ever be inert — the honest
            answer is to refuse it, not to store it under a fabricated key.
    """
    from src.shared.models.vault import UserIdentity

    user_pk = await db.scalar(select(User.id).where(or_(User.cognito_sub == caller_id, User.id == caller_id)).limit(1))
    if user_pk is None:
        # Not logged with the caller id at INFO+ elsewhere in this module; the id
        # is in the token, so a warning naming it is enough to diagnose without
        # putting an identifier into a response.
        logger.warning("No users row for caller id; cannot derive a person anchor")
        raise UnresolvablePersonAnchorError(
            f"{PERSON_ANCHOR_GITHUB_PREFIX}<unresolved>",
            "your account is not provisioned in this platform's user directory",
        )

    # One query per namespace, in precedence order, stopping at the first hit.
    # Sequential rather than one `provider IN (...)` query with a CASE ordering:
    # the overwhelmingly common case is a GitHub identity found on the first
    # iteration, which costs exactly the single indexed lookup it cost before
    # #4843, and the precedence is then legible as the loop order rather than
    # encoded in a sort expression that a later edit could reorder by accident.
    for provider in PERSON_ANCHOR_PROVIDER_PRECEDENCE:
        provider_user_id = await db.scalar(
            select(UserIdentity.provider_user_id)
            .where(
                UserIdentity.user_id == user_pk,
                UserIdentity.provider == provider,
            )
            # Deterministic pick (review fix on #4661): (user_id, provider) is not
            # unique, and the enforcement-side resolver orders the same way — an
            # unordered pick on either side lets a two-GitHub-row user author a cap
            # under one anchor while enforcement reads another (inert cap, #4511).
            # `is_primary` leads the sort as of #4843's partial unique index, which
            # makes the choice a DB invariant instead of a convention replicated
            # across call sites; `provider_user_id` remains the tiebreaker so the
            # order is still total on rows the backfill did not flag, and so the
            # result is byte-identical to the pre-#4843 pick.
            .order_by(UserIdentity.is_primary.desc(), UserIdentity.provider_user_id)
            .limit(1)
        )
        if provider_user_id:
            return format_person_anchor(provider_user_id, provider.value), user_pk

    raise UnresolvablePersonAnchorError(
        f"{PERSON_ANCHOR_GITHUB_PREFIX}<unresolved>",
        "your account has no linked external identity, so it has no cross-organization key a personal limit could be stored against",
    )
