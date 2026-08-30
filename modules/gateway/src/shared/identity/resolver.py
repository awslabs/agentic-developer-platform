"""Canonical identity resolution: Cognito sub to users.id.

This module provides the gateway's standard resolver for mapping a Cognito
``sub`` claim (the provider identity carried in JWT tokens) to the canonical
``users.id`` UUID stored in Postgres.

The resolver is the complement of ``src/shared/services/canonical_user.py``
which resolves by primary key (``users.id`` -> User row). This module resolves
in the opposite direction: ``cognito_sub`` -> ``users.id``.

Contract:
- If a ``users`` row exists with ``cognito_sub == sub``, return its ``id``.
- If no matching row exists (identity not yet provisioned), fall back to the
  raw ``cognito_sub`` value so callers degrade gracefully rather than failing.

``resolve_user_entity_id`` (issue #4511) resolves in the *same* direction as
``resolve_canonical_user_id``'s input — it normalises any of several accepted
identity forms **to** the Cognito sub — because that is the key the budget
ledger and enforcement path use. Note the deliberately opposite failure
semantics: ``resolve_canonical_user_id`` degrades to the raw input so a read
path never 500s, whereas ``resolve_user_entity_id`` raises, because its callers
are *write* paths where persisting an unresolvable id is precisely the bug
(#4511: a budget keyed on something enforcement can never match).
"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.exceptions import BedrockGatewayError
from src.shared.identity.providers import IdentityProvider
from src.shared.models.organization import User

logger = logging.getLogger("bedrockgateway.identity")

# Cognito usernames minted by the GitHub auth broker are ``GitHub_<github_id>``
# (lambda/github-auth-broker/cognito_provisioner.py). Matching is
# case-insensitive on purpose: the broker writes a capital G while other call
# sites in this repo parse a lowercase ``github_``, and a case-sensitive
# comparison here would silently fail for exactly the population #4511 affects.
_GITHUB_USERNAME_PREFIX = "github_"

# Named in the 422 so an operator who typed the wrong thing is told what the
# right thing is, rather than being left to guess.
_ACCEPTED_FORMS = "a Cognito sub, a canonical ADP user id, or a Cognito username of the form GitHub_<github_user_id>"


async def resolve_canonical_user_id(db: AsyncSession, cognito_sub: str) -> str:
    """Resolve a Cognito sub to the canonical ADP user_id (``users.id``).

    Args:
        db: Async database session. MUST be a session bound to the gateway DB —
            the ``users`` table lives there, not in the agent_context DB. Passing
            the wrong session raises UndefinedTableError, which is caught below
            and degrades to the raw sub (see #2213 follow-up).
        cognito_sub: The ``sub`` claim from the Cognito JWT (i.e.
            ``TokenContext.user_id``).

    Returns:
        The canonical ``users.id`` UUID if a matching row exists, otherwise
        the raw ``cognito_sub`` value (graceful fallback for unprovisioned
        identities, or if the ``users`` table is unreachable on this session).
    """
    try:
        canonical = await db.scalar(select(User.id).where(User.cognito_sub == cognito_sub))
    except SQLAlchemyError:
        # Defense-in-depth: a wrong-DB session (no ``users`` table) or a transient
        # DB error must not 500 the caller. Degrade to the raw sub — the same
        # graceful-fallback contract as "no matching row".
        logger.warning(
            "users lookup failed for cognito_sub=%s (wrong DB session or DB error); falling back to raw token user_id",
            cognito_sub,
            exc_info=True,
        )
        return cognito_sub
    if canonical:
        return canonical
    logger.warning(
        "No users row for cognito_sub=%s; falling back to raw token user_id",
        cognito_sub,
    )
    return cognito_sub


class UnresolvableUserEntityError(BedrockGatewayError):
    """A supplied ``user`` entity id cannot be resolved to a Cognito sub.

    422 rather than 400: the value is syntactically fine, it just does not
    identify an enforceable user in this organization. The message names the
    accepted forms because the caller is usually a human filling in a form.

    Deliberately raised — never swallowed — for the ``cognito_sub IS NULL``
    case. A user row without a sub cannot be matched by enforcement or by the
    ``/api/me/budget`` read path, so persisting a budget against it would
    recreate #4511 (a cap that exists but never enforces and never shows spend).
    """

    def __init__(self, supplied_id: str, reason: str):
        super().__init__(
            "unresolvable_user_entity",
            f"Cannot resolve user '{supplied_id}' to an enforceable identity in this organization: {reason}. Expected {_ACCEPTED_FORMS}.",
            422,
            {"supplied_id": supplied_id, "reason": reason, "accepted_forms": _ACCEPTED_FORMS},
        )


async def resolve_user_entity_id(db: AsyncSession, org_id: str, supplied_id: str) -> str:
    """Resolve a supplied ``user`` entity id to the Cognito sub used as its key.

    Issue #4511. Budget enforcement builds ``(EntityType.USER, context.user_id)``
    where ``context.user_id`` is the Cognito ``sub`` claim
    (``src/auth/middleware.py``, ``src/budget/enforcement_service.py``), and the
    ``/api/me/budget`` read path matches ``budget_configs`` on that exact value
    (``src/budget/me_routes.py::_read_cap``). Any ``user``-keyed row written with
    a different identity form is therefore *inert*: invisible to the owner and
    unenforceable. This function is the single chokepoint that guarantees a
    written key is one the engine can match.

    Accepted forms, tried in order:

    1. **Cognito sub** — passed through, but only after confirming a ``users``
       row with that sub exists *in this org*. A sub with no local row would
       still produce an inert budget, so it is rejected, not trusted.
    2. **Canonical ``users.id``** — mapped to that row's ``cognito_sub``.
    3. **Cognito username** ``GitHub_<github_user_id>`` (case-insensitive
       prefix) — the prefix is stripped and the remainder resolved through
       ``user_identities``, which is the only bridge actually populated for
       GitHub-onboarded users. ``users.cognito_username`` is deliberately NOT
       consulted: it is only ever set on the admin-invite path (to the email),
       so it is NULL for precisely the population this bug affects.

    Anything else raises. So does any form that resolves to a user whose
    ``cognito_sub`` is NULL.

    Args:
        db: Async session bound to the gateway DB.
        org_id: The **target** org from the route path — already authorized by
            the caller's ``check_permission(..., target_org_id=org_id)``. This is
            the tenant written to ``BudgetConfig.org_id``, so scoping resolution
            to it keeps the resolution scope and the row's tenant identical. It
            is deliberately NOT the caller's own org: a platform admin
            legitimately administers orgs that are not their own.
        supplied_id: The raw id from the request body or path.

    Returns:
        The Cognito sub to persist and match on.

    Raises:
        UnresolvableUserEntityError: 422; nothing is persisted.
    """
    candidate = supplied_id.strip()
    if not candidate:
        raise UnresolvableUserEntityError(supplied_id, "the id is empty")

    # 1. Already a sub? Confirm it names a real user in this org before trusting
    #    it — an unknown sub is as inert as a username.
    if await db.scalar(select(User.id).where(User.org_id == org_id, User.cognito_sub == candidate)):
        return candidate

    # 2. Canonical users.id.
    user_row = (await db.execute(select(User).where(User.org_id == org_id, User.id == candidate))).scalar_one_or_none()
    if user_row is not None:
        return _require_sub(user_row, supplied_id)

    # 3. Cognito username of the form GitHub_<github_user_id>.
    if candidate.lower().startswith(_GITHUB_USERNAME_PREFIX):
        github_user_id = candidate[len(_GITHUB_USERNAME_PREFIX) :]
        if not github_user_id:
            raise UnresolvableUserEntityError(supplied_id, "the username carries no GitHub user id")
        return await _resolve_via_github_identity(db, org_id, github_user_id, supplied_id)

    # Email is intentionally not an accepted form: users.email carries no
    # uniqueness constraint, so resolving by it could land a spend cap on a
    # different person than the operator intended.
    raise UnresolvableUserEntityError(supplied_id, "no user in this organization matches this id")


async def _resolve_via_github_identity(db: AsyncSession, org_id: str, github_user_id: str, supplied_id: str) -> str:
    """Bridge a GitHub numeric user id to a Cognito sub via ``user_identities``.

    The ``org_id`` filter is in SQL and non-optional. Since migration 021
    (#2961) ``user_identities`` is unique per ``(provider, provider_user_id,
    org_id)``, so one GitHub account may legitimately be linked in several
    tenants with a *different* ``users.id`` and a different sub in each.
    Selecting without the filter would both risk MultipleResultsFound and let a
    budget in one tenant be keyed on another tenant's user.
    """
    # Imported here, not at module scope: src.shared.models.vault imports
    # src.shared.identity.providers, so a top-level import of UserIdentity makes
    # this package and the models package circular at collection time.
    from src.shared.models.vault import UserIdentity

    identity_user_id = await db.scalar(
        select(UserIdentity.user_id).where(
            UserIdentity.org_id == org_id,
            UserIdentity.provider == IdentityProvider.github,
            UserIdentity.provider_user_id == github_user_id,
        )
    )
    if identity_user_id is None:
        raise UnresolvableUserEntityError(supplied_id, "no GitHub identity is linked to a user in this organization")

    # Re-filter on org_id rather than trusting the identity row's FK: this keeps
    # the invariant that resolution never walks sideways out of the target org
    # (see the security note in src/shared/services/canonical_user.py).
    user_row = (await db.execute(select(User).where(User.org_id == org_id, User.id == identity_user_id))).scalar_one_or_none()
    if user_row is None:
        raise UnresolvableUserEntityError(supplied_id, "the linked GitHub identity points at a user outside this organization")

    resolved = _require_sub(user_row, supplied_id)
    logger.warning(
        "Resolved user entity id %r to cognito_sub %r via user_identities (org_id=%s)",
        supplied_id,
        resolved,
        org_id,
    )
    return resolved


def _require_sub(user_row: User, supplied_id: str) -> str:
    """Return the row's ``cognito_sub``, or raise if it has none.

    ``users.cognito_sub`` is nullable (shadow users, invited-but-never-logged-in
    users). Such a user has no identity the budget engine can ever present, so a
    budget for them can only ever be inert — 422 is the honest answer.
    """
    if not user_row.cognito_sub:
        raise UnresolvableUserEntityError(
            supplied_id,
            "the user has not signed in yet, so they have no identity the budget engine can match",
        )
    return user_row.cognito_sub
