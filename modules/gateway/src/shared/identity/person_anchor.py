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
"""

import logging

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.exceptions import BedrockGatewayError
from src.shared.identity.providers import IdentityProvider
from src.shared.models.organization import User

logger = logging.getLogger("bedrockgateway.identity")

# The provider namespace qualifier on a person anchor. Pinned to
# ``IdentityProvider.github`` by a test so the two cannot drift: the anchor's
# namespace and the ``user_identities.provider`` value it is resolved through are
# the same concept spelled twice.
PERSON_ANCHOR_GITHUB_PREFIX = f"{IdentityProvider.github.value}:"

# Named in the 422 so a caller who supplied the wrong thing is told the right
# thing rather than left to guess.
_ACCEPTED_ANCHOR_FORM = f"an anchor of the form '{PERSON_ANCHOR_GITHUB_PREFIX}<github_numeric_user_id>'"


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


def format_person_anchor(github_user_id: str) -> str:
    """Build a person anchor from a GitHub numeric user id.

    The single place the qualifier is applied, so an authored anchor and a
    resolved one cannot differ by a prefix.
    """
    return f"{PERSON_ANCHOR_GITHUB_PREFIX}{github_user_id}"


def parse_person_anchor(anchor: str) -> str:
    """Validate an anchor's *shape* and return the GitHub id inside it.

    Shape only — this does not check that the id names anybody. Use
    :func:`resolve_person_anchor` when the anchor is about to be persisted.

    Raises:
        UnresolvablePersonAnchorError: 422. The three rejected shapes each map to
        a real way an inert row gets written: a missing/wrong prefix (an id in
        some other namespace), an empty id (a key no ledger row can carry), and a
        whitespace-padded id (a key that compares unequal to the same person's
        real one, so the cap silently governs nobody).
    """
    if not anchor.startswith(PERSON_ANCHOR_GITHUB_PREFIX):
        raise UnresolvablePersonAnchorError(anchor, f"the anchor must start with '{PERSON_ANCHOR_GITHUB_PREFIX}'")

    github_user_id = anchor[len(PERSON_ANCHOR_GITHUB_PREFIX) :]
    if not github_user_id:
        raise UnresolvablePersonAnchorError(anchor, "the anchor carries no GitHub user id")
    if github_user_id != github_user_id.strip():
        raise UnresolvablePersonAnchorError(anchor, "the GitHub user id must not carry surrounding whitespace")
    return github_user_id


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

    Args:
        db: Async session bound to the gateway DB.
        supplied_anchor: The raw anchor from the request path.

    Returns:
        The anchor, normalised through :func:`format_person_anchor` so a stored
        key can never differ from a resolved one by spelling.

    Raises:
        UnresolvablePersonAnchorError: 422; nothing is persisted.
    """
    github_user_id = parse_person_anchor(supplied_anchor)

    # Imported here, not at module scope: src.shared.models.vault imports
    # src.shared.identity.providers, so a top-level import of UserIdentity makes
    # this package and the models package circular at collection time. Same
    # reasoning as resolver._resolve_via_github_identity.
    from src.shared.models.vault import UserIdentity

    linked = await db.scalar(
        select(UserIdentity.id)
        .where(
            UserIdentity.provider == IdentityProvider.github,
            UserIdentity.provider_user_id == github_user_id,
        )
        .limit(1)
    )
    if linked is None:
        raise UnresolvablePersonAnchorError(supplied_anchor, "no GitHub identity with this id is linked to any user on this platform")

    return format_person_anchor(github_user_id)


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
    2. That user's GitHub ``user_identities`` row, for the anchor id.

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
            no linked GitHub identity. A person with no GitHub identity has no
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

    github_user_id = await db.scalar(
        select(UserIdentity.provider_user_id)
        .where(
            UserIdentity.user_id == user_pk,
            UserIdentity.provider == IdentityProvider.github,
        )
        # Deterministic pick (review fix on #4661): (user_id, provider) is not
        # unique, and the enforcement-side resolver orders the same way — an
        # unordered pick on either side lets a two-GitHub-row user author a cap
        # under one anchor while enforcement reads another (inert cap, #4511).
        .order_by(UserIdentity.provider_user_id)
        .limit(1)
    )
    if not github_user_id:
        raise UnresolvablePersonAnchorError(
            f"{PERSON_ANCHOR_GITHUB_PREFIX}<unresolved>",
            "your account has no linked GitHub identity, so it has no cross-organization key a personal limit could be stored against",
        )

    return format_person_anchor(github_user_id), user_pk
