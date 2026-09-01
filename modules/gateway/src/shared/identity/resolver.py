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

``resolve_root_user_entity_id`` (issue #4536) is that same write-path resolver
aimed one ledger over. A person's spend lands in two id namespaces: their
**direct** traffic under ``entity_type="user"`` keyed by Cognito sub, and the
spend of agent chains they triggered under ``entity_type="root_user"`` keyed by
canonical ``users.id`` (#4300). Both resolvers therefore accept the *same* input
forms and differ only in which key they return — one shared lookup, two targets,
so a form that resolves for one kind of person-budget cannot fail for the other.
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

# Mirrors `src.budget.schemas.SERVICE_PRINCIPAL_QUALIFIER` (#4344). Duplicated
# rather than imported: importing src.budget from src.shared would invert the
# dependency direction (src.budget.__init__ pulls in routes -> src.auth). Pinned
# equal to the canonical constant by a test.
_SERVICE_PRINCIPAL_QUALIFIER = "service:"


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
    """A supplied person-scoped entity id cannot be resolved to a real user.

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

    Accepted forms are :func:`_resolve_user_row`'s — a Cognito sub, a canonical
    ``users.id``, or a ``GitHub_<github_user_id>`` Cognito username — each scoped
    to ``org_id``. Anything else raises. So does any form that resolves to a user
    whose ``cognito_sub`` is NULL: they have no identity the budget engine can
    ever present, so a direct-use budget for them could only ever be inert. (The
    cloud-agent ledger has no such restriction — see
    :func:`resolve_root_user_entity_id`.)

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
    user_row = await _resolve_user_row(db, org_id, supplied_id)
    return _require_sub(user_row, supplied_id)


async def resolve_root_user_entity_id(db: AsyncSession, org_id: str, supplied_id: str) -> str:
    """Resolve a supplied ``root_user`` entity id to its canonical ``users.id``.

    Issue #4536. The cloud-agent ledger is keyed by canonical ``users.id``, not by
    Cognito sub: the budget-usage tracker writes ``root_user`` rows from the
    lineage plane's ``root_human_id`` (#4300), and the ``/api/me/budget`` read
    path derives the same key by resolving the caller's sub through ``users``
    (``me_routes._resolve_root_principal``). A ``root_user`` cap written in any
    other namespace is inert in exactly the way #4511 was one ledger over.

    Accepts the same input forms as :func:`resolve_user_entity_id` — one shared
    lookup, so the person picker can offer both budget kinds without a form
    resolving for one and failing for the other. Two deliberate differences:

    * The return value is the canonical id rather than the sub.
    * A member whose ``cognito_sub`` is NULL **is** resolvable. A canonical id
      always exists, and cloud-agent spend is attributed from the run's lineage
      rather than from a signed-in session, so such a cap is enforceable — unlike
      a direct-use cap for the same person.

    ``service:``-qualified root principals (unattended CI/EventBridge triggers,
    #4344) are passed through unchanged: they are already canonical ``root_user``
    keys and have no ``users`` row to resolve against, by design.

    Args:
        db: Async session bound to the gateway DB.
        org_id: The **target** org from the route path, already authorized by the
            caller — see :func:`resolve_user_entity_id` for why it is not the
            caller's own org.
        supplied_id: The raw id from the request body or path.

    Returns:
        The canonical ``users.id`` to persist and match on.

    Raises:
        UnresolvableUserEntityError: 422; nothing is persisted.
    """
    # Checked before the lookup: a qualified service key is not a person and must
    # not be run through a `users` search that could only ever miss.
    candidate = supplied_id.strip()
    if candidate.startswith(_SERVICE_PRINCIPAL_QUALIFIER):
        principal = candidate[len(_SERVICE_PRINCIPAL_QUALIFIER) :]
        # Enforcement only ever writes ``service:<non-empty registry id>``, so a
        # blank or whitespace-padded principal is a key no ledger row will ever
        # carry — an inert cap of exactly the #4511 class this resolver blocks.
        if not principal or principal != principal.strip():
            raise UnresolvableUserEntityError(
                supplied_id,
                f"the id after '{_SERVICE_PRINCIPAL_QUALIFIER}' must be a non-empty service principal id without surrounding whitespace",
            )
        return candidate

    user_row = await _resolve_user_row(db, org_id, supplied_id)
    return user_row.id


async def _resolve_user_row(db: AsyncSession, org_id: str, supplied_id: str) -> User:
    """Find the ``users`` row a supplied person-scoped id names, or raise.

    The single lookup behind both resolvers, so the accepted input forms cannot
    drift apart between the two person-scoped budget kinds. It returns the row
    rather than a key: which column becomes the ledger key is the caller's
    decision, and it differs per ledger.

    Forms, tried in order:

    1. **Cognito sub** — matched against a ``users`` row *in this org*. A sub with
       no local row would still produce an inert budget, so it is rejected, not
       trusted.
    2. **Canonical ``users.id``**.
    3. **Cognito username** ``GitHub_<github_user_id>`` (case-insensitive prefix)
       — the prefix is stripped and the remainder resolved through
       ``user_identities``, which is the only bridge actually populated for
       GitHub-onboarded users. ``users.cognito_username`` is deliberately NOT
       consulted: it is only ever set on the admin-invite path (to the email), so
       it is NULL for precisely the population #4511 affected.
    """
    candidate = supplied_id.strip()
    if not candidate:
        raise UnresolvableUserEntityError(supplied_id, "the id is empty")

    # 1. Already a sub?
    user_row = (await db.execute(select(User).where(User.org_id == org_id, User.cognito_sub == candidate))).scalar_one_or_none()
    if user_row is not None:
        return user_row

    # 2. Canonical users.id.
    user_row = (await db.execute(select(User).where(User.org_id == org_id, User.id == candidate))).scalar_one_or_none()
    if user_row is not None:
        return user_row

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


async def _resolve_via_github_identity(db: AsyncSession, org_id: str, github_user_id: str, supplied_id: str) -> User:
    """Bridge a GitHub numeric user id to its ``users`` row via ``user_identities``.

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

    logger.warning(
        "Resolved person-scoped entity id %r to users.id %r via user_identities (org_id=%s)",
        supplied_id,
        user_row.id,
        org_id,
    )
    return user_row


def _require_sub(user_row: User, supplied_id: str) -> str:
    """Return the row's ``cognito_sub``, or raise if it has none.

    ``users.cognito_sub`` is nullable (shadow users, invited-but-never-logged-in
    users). Such a user has no identity the budget engine can ever present for
    their *direct* traffic, so a ``user`` budget for them can only ever be inert —
    422 is the honest answer. Deliberately NOT applied on the ``root_user`` path,
    where the canonical id is the key and always exists (#4536).
    """
    if not user_row.cognito_sub:
        raise UnresolvableUserEntityError(
            supplied_id,
            "the user has not signed in yet, so they have no identity the budget engine can match",
        )
    return user_row.cognito_sub
