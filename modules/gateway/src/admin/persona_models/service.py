"""Persona-model preference service layer — Issue #5419 (PMM-02).

Business logic for preference CRUD, principal validation, optimistic concurrency,
the PMM-03 validation interface (fail-closed), and audit writes.

**Key design decisions:**

1. **Concurrency is the database's decision on both paths, not the reader's.**
   An update is ``UPDATE ... WHERE revision = :expected`` plus a rowcount check;
   a create lets ``uq_persona_model_pref_scope`` pick the winner and converts the
   loser's ``IntegrityError`` into the same 409. Neither path trusts a prior read,
   because a read-then-write pair can always be interleaved — which on this branch
   produced a silent lost update on the update path and an HTTP 500 on the create
   path. ``test_ac06_concurrent_create_yields_one_row_and_a_conflict`` pins it.

2. **Fail-closed validation** — the temporary ``validate_model_for_persona``
   refuses every write with ``probing_disabled`` until PMM-03 ships.
   AC-07 is NOT proven against a real catalogue.

3. **Audit completeness** — every mutation and refusal carries before/after model,
   actor kind, subject key, revision and provenance.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.audit import AuditLog
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.organization import User
from src.shared.models.persona_models import (
    PersonaModelPolicySetting,
    PersonaModelPreference,
    ServicePrincipal,
    ServicePrincipalAlias,
)

logger = logging.getLogger("bedrockgateway.persona_models")

# ── Interim persona catalogue (mirror; PMM-03 replaces it) ──────────────────
#
# The authority is ``VALID_PERSONAS`` in
# ``modules/agent-factory/webhook-ingress/lambda/common/personas.py``, mirrored
# in ``docs/agent-catalogue.md`` ("the authoritative list of every agent persona
# ADP ships") and pinned by that Lambda's ``test_persona_catalogue_parity.py``.
#
# That module lives in a different runtime (the webhook-ingress Lambda) and is
# not importable from the gateway today, so this is a hand-kept mirror of all
# twelve keys.  ``test_persona_catalogue_mirrors_authority`` re-derives the
# authority by parsing ``docs/agent-catalogue.md`` and fails on any drift, so
# this list cannot silently diverge the way its predecessor did.
#
# PMM-03 (#5420) replaces this with a catalogue derived at request time; design
# note 5420 is explicit that any second hand-maintained list "fails AC-01 by
# construction".  Delete this block when that endpoint lands — do not extend it.
#
# An earlier revision of this file invented four keys that exist nowhere in the
# platform (``planner``, ``evaluator``, ``researcher``, ``chat``) and omitted six
# real ones.  Inventing a key is the more harmful direction: a person could save
# a preference against a persona that will never run, which is the inert-config
# class this story exists to prevent.
INTERIM_PERSONA_CATALOGUE: list[dict] = [
    {"key": "aidlc", "display_name": "AIDLC", "configurable": True},
    {"key": "architect", "display_name": "Architect", "configurable": True},
    {"key": "codex", "display_name": "Codex", "configurable": True},
    {"key": "developer", "display_name": "Developer", "configurable": True},
    {"key": "malware-analysis-agent", "display_name": "Malware Analysis Agent", "configurable": True},
    {"key": "operations", "display_name": "Operations", "configurable": True},
    {"key": "pm", "display_name": "PM", "configurable": True},
    {"key": "product", "display_name": "Product", "configurable": True},
    # Not configurable: blocked on #4037.  Design note 5420 §2.3.
    {"key": "pt-superpower", "display_name": "PT Superpower", "configurable": False},
    {"key": "reviewer", "display_name": "Reviewer", "configurable": True},
    {"key": "superplane-operator", "display_name": "Superplane Operator", "configurable": True},
    {"key": "superplane-researcher", "display_name": "Superplane Researcher", "configurable": True},
]

# The unique index that makes the create path race-safe; see
# `_is_scope_uniqueness_violation`. Must match the name in the model's
# `__table_args__` and in migration 056.
_PREF_SCOPE_CONSTRAINT = "uq_persona_model_pref_scope"

PERSONA_KEYS = {p["key"] for p in INTERIM_PERSONA_CATALOGUE}
CONFIGURABLE_PERSONAS = {p["key"] for p in INTERIM_PERSONA_CATALOGUE if p["configurable"]}


# ── Exceptions ───────────────────────────────────────────────────────────────


class PreferenceRejectedError(Exception):
    """A write was refused with a stable reason code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class PreferenceConflictError(Exception):
    """Optimistic concurrency conflict — a 409 carrying the current row."""

    def __init__(self, row: PersonaModelPreference) -> None:
        super().__init__("revision conflict")
        self.row = row


def _extract_constraint_name(exc: IntegrityError) -> str:
    """Extract the constraint name from an IntegrityError's original exception.

    Supports three shapes:

    - **asyncpg via SQLAlchemy** — SQLAlchemy translates the raw asyncpg
      exception into an adapted DBAPI error at ``exc.orig``.  The adapted error
      has ``pgcode``/``sqlstate`` but NOT ``constraint_name``.  The *original*
      asyncpg exception with ``constraint_name`` is chained as
      ``exc.orig.__cause__`` (``raise translated from original``).
    - **psycopg2** — ``exc.orig`` is the raw psycopg2 error, which exposes
      ``diag.constraint_name``.
    - **SQLite** — no constraint name; caller falls through to message parsing.

    Returns the empty string when no constraint name is found.
    """
    orig = exc.orig

    # asyncpg path: the raw asyncpg exception is in the __cause__ chain.
    # Walk __cause__ to find an exception with constraint_name (asyncpg.UniqueViolationError
    # and friends expose it directly, unlike psycopg2 which uses .diag).
    cause = orig
    while cause is not None:
        name = getattr(cause, "constraint_name", None)
        if name:
            return name
        cause = getattr(cause, "__cause__", None)

    # psycopg2 path — constraint_name sits behind .diag
    diag = getattr(orig, "diag", None)
    if diag is not None:
        name = getattr(diag, "constraint_name", None)
        if name:
            return name

    return ""


def _is_scope_uniqueness_violation(exc: IntegrityError) -> bool:
    """True when ``exc`` is the preference scope-uniqueness refusal, not another defect.

    Narrow by design: a create that loses a race is a 409, but an unrelated
    integrity failure (a CHECK refusal, a missing FK) must keep propagating as a
    500 rather than be reported to the caller as "someone else got there first".

    The three driver shapes:

    - **asyncpg** (production) — ``sqlstate`` on the exception itself,
      ``constraint_name`` directly on the exception. See ``_extract_constraint_name``.
    - **psycopg2** — ``sqlstate`` on the exception, ``diag.constraint_name``.
    - **SQLite** — no sqlstate, constraint columns named in the error text.
    """
    sqlstate = getattr(exc.orig, "sqlstate", None)
    if sqlstate is not None:
        return sqlstate == "23505" and _PREF_SCOPE_CONSTRAINT in _extract_constraint_name(exc)
    message = str(exc.orig)
    return "persona_model_preferences" in message and "persona_key" in message


def _is_active_alias_uniqueness_violation(exc: IntegrityError) -> bool:
    """True when ``exc`` is the active-alias uniqueness refusal, not another defect.

    Narrow by constraint name: only ``uq_spa_active_alias`` counts as "someone
    else registered the same alias concurrently".  An unrelated IntegrityError —
    CHECK refusal, FK violation — must keep propagating as a 500 rather than be
    reported to the caller as a conflict.

    Supports asyncpg, psycopg2, and SQLite — see ``_extract_constraint_name``.
    """
    sqlstate = getattr(exc.orig, "sqlstate", None)
    if sqlstate is not None:
        return sqlstate == "23505" and _ACTIVE_ALIAS_CONSTRAINT in _extract_constraint_name(exc)
    message = str(exc.orig)
    return "service_principal_aliases" in message and "alias_id" in message


# ── Principal resolution ─────────────────────────────────────────────────────

ACCOUNT_TYPE_TO_KIND: dict[str, str] = {
    "human": "human",
    "service": "service_account",
}


def derive_principal_kind(account_type: str) -> str:
    """Map ``account_type`` to ``principal_kind``, or refuse unknown values."""
    kind = ACCOUNT_TYPE_TO_KIND.get(account_type)
    if kind is None:
        raise PreferenceRejectedError(
            "invalid_principal_kind",
            f"Unrecognised account type '{account_type}'; expected 'human' or 'service'.",
        )
    return kind


def derive_principal_source(account_type: str, auth_source: str, canonical_alias_source: str = "") -> str:
    """Derive the stored provenance value from the authenticated context.

    ``canonical_alias_source`` is the exact trusted source the auth path stamped on
    the token context.  When present it is used verbatim — it is the only truthful
    answer for a self-service write.

    For the admin surface (which acts on a target principal rather than on the
    caller's own identity), ``canonical_alias_source`` is typically empty and the
    admin route derives provenance from the target's first active alias instead.
    The coarse ``auth_source`` fallback returns ``"agent_registry"`` for IAM callers
    and ``"sa_registration"`` otherwise, but this path exists only for the admin
    surface where provenance is not being claimed about the caller.
    """
    if account_type == "human":
        return "self"
    if canonical_alias_source:
        return canonical_alias_source
    if auth_source == "iam":
        return "agent_registry"
    return "sa_registration"


# ── PMM-03 validation interface (fail-closed) ───────────────────────────────


async def validate_model_for_persona(
    db: AsyncSession,
    *,
    org_id: str,
    principal_kind: str,
    canonical_principal_id: str,
    persona_key: str,
    model: str,
) -> str:
    """Validate that ``model`` is selectable for this persona and principal.

    **FAIL-CLOSED** until PMM-03 (#5420) ships.  Every write is refused with
    ``probing_disabled`` — no preference row can be stored through this stub.
    AC-07 is explicitly unmet until the real validator is integrated.

    The real PMM-03 ``validate_selection`` returns a ``Selection`` with the
    canonical model ID, compatibility class, and evidence row — or a
    ``Rejection`` with reason and message.  This stub rejects unconditionally.
    """
    if not model or not model.strip():
        raise PreferenceRejectedError(
            "invalid_model",
            "Model identifier must not be empty.",
        )
    if persona_key not in PERSONA_KEYS:
        raise PreferenceRejectedError(
            "unknown_persona",
            f"Unknown persona key '{persona_key}'.",
        )
    if persona_key not in CONFIGURABLE_PERSONAS:
        raise PreferenceRejectedError(
            "persona_not_configurable",
            f"Persona '{persona_key}' is not configurable.",
        )
    # Fail-closed: refuse all writes until PMM-03 provides real validation.
    raise PreferenceRejectedError(
        "probing_disabled",
        "Model selection validation is not yet available. "
        "Preference writes are disabled until the persona catalogue "
        "and invocability probing service (PMM-03) is integrated. "
        f"Requested model: {model.strip()}",
    )


# ── Read operations ──────────────────────────────────────────────────────────


async def get_platform_default(db: AsyncSession) -> PersonaModelPolicySetting | None:
    """Load the Claude-class platform default settings record."""
    return await db.scalar(select(PersonaModelPolicySetting).where(PersonaModelPolicySetting.compatibility_class == "claude-agent-sdk"))


async def list_preferences(
    db: AsyncSession,
    *,
    org_id: str,
    principal_kind: str,
    principal_id: str,
) -> list[PersonaModelPreference]:
    """Load all saved preferences for a principal in a tenant."""
    result = await db.scalars(
        select(PersonaModelPreference).where(
            PersonaModelPreference.org_id == org_id,
            PersonaModelPreference.principal_kind == principal_kind,
            PersonaModelPreference.principal_id == principal_id,
        )
    )
    return list(result)


async def get_preference(
    db: AsyncSession,
    *,
    org_id: str,
    principal_kind: str,
    principal_id: str,
    persona_key: str,
) -> PersonaModelPreference | None:
    """Load a single saved preference, or None."""
    return await db.scalar(
        select(PersonaModelPreference).where(
            PersonaModelPreference.org_id == org_id,
            PersonaModelPreference.principal_kind == principal_kind,
            PersonaModelPreference.principal_id == principal_id,
            PersonaModelPreference.persona_key == persona_key,
        )
    )


async def build_preference_list(
    db: AsyncSession,
    *,
    org_id: str,
    principal_kind: str,
    principal_id: str,
) -> list[dict]:
    """Build the full persona list with effective values — saved or default."""
    saved = await list_preferences(db, org_id=org_id, principal_kind=principal_kind, principal_id=principal_id)
    saved_by_key = {p.persona_key: p for p in saved}

    platform_default = await get_platform_default(db)
    default_model_id = platform_default.active_default_model_id if platform_default else None

    entries = []
    for persona in INTERIM_PERSONA_CATALOGUE:
        key = persona["key"]
        pref = saved_by_key.get(key)

        if pref is not None:
            entries.append(
                {
                    "persona_key": key,
                    "persona_display_name": persona["display_name"],
                    "configurable": persona["configurable"],
                    "effective_model_id": pref.canonical_model_id,
                    "source": "principal-mapping",
                    "status": "configured",
                    "saved_model_id": pref.canonical_model_id,
                    "requested_alias": pref.requested_alias,
                    "revision": pref.revision,
                    "updated_at": pref.updated_at,
                }
            )
        else:
            entries.append(
                {
                    "persona_key": key,
                    "persona_display_name": persona["display_name"],
                    "configurable": persona["configurable"],
                    "effective_model_id": default_model_id,
                    "source": "system-default",
                    "status": "not-configured",
                }
            )

    return entries


async def build_explain(
    db: AsyncSession,
    *,
    org_id: str,
    principal_kind: str,
    principal_id: str,
    persona_key: str,
) -> dict:
    """Build the single-persona explainer response."""
    if persona_key not in PERSONA_KEYS:
        raise PreferenceRejectedError("unknown_persona", f"Unknown persona key '{persona_key}'.")

    pref = await get_preference(db, org_id=org_id, principal_kind=principal_kind, principal_id=principal_id, persona_key=persona_key)
    platform_default = await get_platform_default(db)
    default_model_id = platform_default.active_default_model_id if platform_default else None

    if pref is not None:
        return {
            "persona_key": persona_key,
            "effective_model_id": pref.canonical_model_id,
            "source": "principal-mapping",
            "status": "configured",
            "saved_model_id": pref.canonical_model_id,
            "requested_alias": pref.requested_alias,
            "revision": pref.revision,
            "updated_at": pref.updated_at,
            "default_model_id": default_model_id,
            "default_source": "claude-agent-sdk",
        }

    return {
        "persona_key": persona_key,
        "effective_model_id": default_model_id,
        "source": "system-default",
        "status": "not-configured",
        "default_model_id": default_model_id,
        "default_source": "claude-agent-sdk",
    }


# ── Write operations ─────────────────────────────────────────────────────────


async def set_preference(
    db: AsyncSession,
    *,
    org_id: str,
    principal_kind: str,
    principal_source: str,
    principal_id: str,
    persona_key: str,
    model: str,
    expected_revision: int | None,
    actor_id: str,
    actor_source: str,
) -> PersonaModelPreference:
    """Create or update a preference row with atomic optimistic concurrency.

    Create: ``expected_revision=None``.  If a row exists, returns 409.
    Update: ``expected_revision`` must match.  Uses ``UPDATE ... WHERE revision =``
    and checks rowcount to prevent lost updates.
    """
    # The persona key is checked HERE, not only inside `validate_model_for_persona`.
    # That function is a temporary seam PMM-03 (#5420) replaces, and its contract is
    # to validate the *model* — so a replacement that honours its contract exactly
    # would drop this check and let a row be stored under any persona key at all.
    # Verified: with the seam replaced by a model-only validator, a PUT to an unknown
    # key stored `('default', 'm')` and then raised out of `build_explain`. Which
    # personas exist is an invariant of the store, so the store enforces it.
    if persona_key not in PERSONA_KEYS:
        raise PreferenceRejectedError("unknown_persona", f"Unknown persona key '{persona_key}'.")
    if persona_key not in CONFIGURABLE_PERSONAS:
        raise PreferenceRejectedError("persona_not_configurable", f"Persona '{persona_key}' is not configurable.")

    # Validate model against PMM-03 interface (fail-closed)
    canonical_model_id = await validate_model_for_persona(
        db,
        org_id=org_id,
        principal_kind=principal_kind,
        canonical_principal_id=principal_id,
        persona_key=persona_key,
        model=model,
    )

    # Enforce one-kind-per-canonical-ID in the service layer.
    existing_other_kind = await db.scalar(
        select(PersonaModelPreference).where(
            PersonaModelPreference.org_id == org_id,
            PersonaModelPreference.principal_id == principal_id,
            PersonaModelPreference.principal_kind != principal_kind,
        )
    )
    if existing_other_kind is not None:
        raise PreferenceRejectedError(
            "principal_kind_conflict",
            f"Principal '{principal_id}' already has preferences as '{existing_other_kind.principal_kind}'; cannot create as '{principal_kind}'.",
        )

    existing = await get_preference(db, org_id=org_id, principal_kind=principal_kind, principal_id=principal_id, persona_key=persona_key)

    if existing is None:
        if expected_revision is not None:
            raise PreferenceRejectedError(
                "not_found",
                f"No preference exists for persona '{persona_key}'; omit expected_revision to create.",
            )
        row = PersonaModelPreference(
            id=new_uuid(),
            org_id=org_id,
            principal_kind=principal_kind,
            principal_source=principal_source,
            principal_id=principal_id,
            persona_key=persona_key,
            canonical_model_id=canonical_model_id,
            requested_alias=model if model != canonical_model_id else None,
            revision=1,
            updated_by=actor_id,
            updated_by_source=actor_source,
        )
        # Let `uq_persona_model_pref_scope` decide the winner, not the read above.
        #
        # The read-then-insert pair can always be interleaved: two concurrent
        # create-only saves both see `existing is None`, both INSERT, and the
        # unique index refuses the second. Before this flush the refusal surfaced
        # as an unmapped IntegrityError — i.e. HTTP 500 — rather than the orderly
        # 409 a stale-revision update already returns. Reproduced on SQLite with
        # two `asyncio.gather` saves from an empty table: one commit, one
        # IntegrityError, zero rows stored (the caller's rollback took the winner
        # with it).
        #
        # SAVEPOINT (`begin_nested`) is what makes the recovery safe. A bare
        # flush that raises leaves the whole transaction poisoned on PostgreSQL —
        # every later statement fails with 25P02 — so the 409 response could not
        # read the winning row to report it. The savepoint rolls back only the
        # failed INSERT and leaves the outer transaction usable.
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError as exc:
            if not _is_scope_uniqueness_violation(exc):
                raise
            # Another writer created the row between our read and this INSERT.
            # Report the row that won, so the caller sees a revision to retry against.
            #
            # Guard the expunge: the savepoint rollback usually detaches the
            # pending instance already, and expunging a non-member raises
            # InvalidRequestError, which would mask the conflict we are handling.
            if row in db:
                db.expunge(row)
            winner = await get_preference(db, org_id=org_id, principal_kind=principal_kind, principal_id=principal_id, persona_key=persona_key)
            if winner is None:
                # The competing transaction refused the row for a reason other
                # than a committed sibling. Do not disguise it as a conflict.
                raise
            raise PreferenceConflictError(winner) from exc
        return row

    # Existing row — atomic compare-and-set via UPDATE ... WHERE revision = expected
    if expected_revision is None:
        raise PreferenceConflictError(existing)

    if existing.revision != expected_revision:
        raise PreferenceConflictError(existing)

    now = utcnow()
    result = await db.execute(
        update(PersonaModelPreference)
        .where(
            PersonaModelPreference.id == existing.id,
            PersonaModelPreference.revision == expected_revision,
        )
        .values(
            canonical_model_id=canonical_model_id,
            requested_alias=model if model != canonical_model_id else None,
            revision=expected_revision + 1,
            updated_at=now,
            updated_by=actor_id,
            updated_by_source=actor_source,
        )
    )

    if result.rowcount == 0:
        # Another writer changed the row between our read and this UPDATE.
        await db.refresh(existing)
        raise PreferenceConflictError(existing)

    # Refresh so the caller sees the updated values.
    await db.refresh(existing)
    return existing


async def reset_preference(
    db: AsyncSession,
    *,
    org_id: str,
    principal_kind: str,
    principal_id: str,
    persona_key: str,
) -> bool:
    """Remove a saved preference so the default becomes effective.

    Returns True if a row was removed, False if nothing was stored.
    """
    existing = await get_preference(db, org_id=org_id, principal_kind=principal_kind, principal_id=principal_id, persona_key=persona_key)
    if existing is None:
        return False
    await db.delete(existing)
    return True


# ── Principal validation ─────────────────────────────────────────────────────


async def validate_human_principal(
    db: AsyncSession,
    *,
    user_id: str,
    org_id: str,
) -> str:
    """Resolve and validate a human principal.  Returns canonical ``users.id``."""
    from src.shared.identity.resolver import resolve_canonical_user_id

    canonical_id = await resolve_canonical_user_id(db, user_id, org_id=org_id)

    user = await db.scalar(select(User).where(User.id == canonical_id, User.org_id == org_id))
    if user is None:
        raise PreferenceRejectedError(
            "principal_not_found",
            "No user found in this tenant for the authenticated identity.",
        )
    return canonical_id


# ── Exact-source resolution ──────────────────────────────────────────────────
#
# Resolution requires an **exact trusted alias_source** plus alias_id and org_id.
# The trusted source is stamped on the token context during authentication:
#
#   - IAM / Agent Registry → ``agent_registry``
#   - Cognito client_credentials (M2M) → ``cognito_m2m``
#
# ``sa_registration``, ``eventbridge`` and ``github_actions`` are registrable and
# administrable alias sources but have no self-auth adapter today.  They can be
# linked to a canonical principal and administered by a human, but a caller
# arriving with one of those identities has no validated authentication path that
# stamps the source.  When an adapter exists, resolution works by the same rule:
# the adapter stamps its source, and resolution uses it.
#
# The multi-source search (`AUTH_SOURCE_TO_ALIAS_SOURCES`) was removed because it
# let one auth path accidentally resolve an alias registered under a different
# source when the alias_id string happened to collide — e.g. a Cognito M2M caller
# with client_id "X" could resolve a ``sa_registration`` alias with alias_id "X",
# binding the wrong identity to preferences.
#
# ``_ACTIVE_ALIAS_CONSTRAINT`` names the partial unique index that guarantees at
# most one active alias per ``(org_id, alias_source, alias_id)`` triple.  Used by
# the savepoint-safe race handler to narrow which IntegrityError counts as a
# conflict.
_ACTIVE_ALIAS_CONSTRAINT = "uq_spa_active_alias"


async def resolve_by_exact_source(
    db: AsyncSession,
    *,
    alias_source: str,
    alias_id: str,
    org_id: str,
) -> tuple[str | None, str]:
    """Resolve a service caller using a single trusted ``(alias_source, alias_id)``.

    Returns ``(canonical_id, alias_source)`` on success, ``(None, "")`` on miss.
    The unique index guarantees at most one active alias per triple, so ambiguity
    handling is structurally unnecessary.
    """
    alias_row = await db.scalar(
        select(ServicePrincipalAlias).where(
            ServicePrincipalAlias.org_id == org_id,
            ServicePrincipalAlias.alias_source == alias_source,
            ServicePrincipalAlias.alias_id == alias_id,
            ServicePrincipalAlias.is_active == True,  # noqa: E712
        )
    )
    if alias_row is None:
        return None, ""

    principal = await db.scalar(
        select(ServicePrincipal).where(
            ServicePrincipal.canonical_service_principal_id == alias_row.canonical_service_principal_id,
            ServicePrincipal.org_id == org_id,
            ServicePrincipal.status == "active",
        )
    )
    if principal is None:
        return None, ""

    return principal.canonical_service_principal_id, alias_row.alias_source


async def validate_target_service_principal(
    db: AsyncSession,
    *,
    canonical_id: str,
    org_id: str,
) -> ServicePrincipal:
    """Validate a target canonical service principal for administration."""
    principal = await db.scalar(
        select(ServicePrincipal).where(
            ServicePrincipal.canonical_service_principal_id == canonical_id,
            ServicePrincipal.org_id == org_id,
        )
    )
    if principal is None:
        raise PreferenceRejectedError(
            "principal_not_found",
            f"No service principal '{canonical_id}' found in this tenant.",
        )
    return principal


# ── Manageable principals ────────────────────────────────────────────────────


async def list_manageable_service_principals(
    db: AsyncSession,
    *,
    org_id: str,
) -> list[dict]:
    """List all service principals in a tenant for the administration picker."""
    principals = await db.scalars(
        select(ServicePrincipal).where(
            ServicePrincipal.org_id == org_id,
            ServicePrincipal.status == "active",
        )
    )

    # Every approved alias source has a truthful display label — no fallback
    # that mislabels eventbridge/github_actions as "service-accounts".
    source_map = {
        "agent_registry": "agent-registry",
        "sa_registration": "sa-registration",
        "cognito_m2m": "cognito-client",
        "eventbridge": "eventbridge",
        "github_actions": "github-actions",
    }

    result = []
    for sp in principals:
        alias = await db.scalar(
            select(ServicePrincipalAlias).where(
                ServicePrincipalAlias.canonical_service_principal_id == sp.canonical_service_principal_id,
                ServicePrincipalAlias.org_id == org_id,
                ServicePrincipalAlias.is_active == True,  # noqa: E712
            )
        )
        source = source_map.get(alias.alias_source, alias.alias_source) if alias else "unknown"

        result.append(
            {
                "canonical_principal_id": sp.canonical_service_principal_id,
                "principal_kind": "service_account",
                "display_name": sp.display_name,
                "tenant_label": sp.org_id,
                "source": source,
                "manageable": True,
            }
        )

    return result


# ── Service-principal lifecycle ─────────────────────────────────────────────


async def register_service_principal(
    db: AsyncSession,
    *,
    org_id: str,
    display_name: str,
    alias_source: str,
    alias_id: str,
    approved_by: str,
) -> tuple[ServicePrincipal, ServicePrincipalAlias]:
    """Register a new canonical service principal and its first alias.

    Refuses if an active alias already exists for the same (org, source, id).
    Re-registering a revoked alias creates a new canonical principal per the
    approved design (§4.6.1): revoked aliases cannot be reactivated.
    """
    existing = await db.scalar(
        select(ServicePrincipalAlias).where(
            ServicePrincipalAlias.org_id == org_id,
            ServicePrincipalAlias.alias_source == alias_source,
            ServicePrincipalAlias.alias_id == alias_id,
            ServicePrincipalAlias.is_active == True,  # noqa: E712
        )
    )
    if existing is not None:
        raise PreferenceRejectedError(
            "alias_already_active",
            f"An active alias already exists for ({alias_source}, {alias_id}) in this tenant.",
        )

    # Both the principal and alias must be created inside the SAVEPOINT so
    # a losing registration's principal is rolled back with the alias — no
    # orphan row.  Previously, db.add() ran before begin_nested(), and
    # SQLAlchemy could auto-flush pending state when entering the savepoint;
    # a uniqueness violation at that point occurred outside the savepoint,
    # poisoning the outer transaction.
    principal = ServicePrincipal(
        canonical_service_principal_id=new_uuid(),
        org_id=org_id,
        display_name=display_name,
        status="active",
        approved_by=approved_by,
    )

    alias = ServicePrincipalAlias(
        id=new_uuid(),
        canonical_service_principal_id=principal.canonical_service_principal_id,
        org_id=org_id,
        alias_source=alias_source,
        alias_id=alias_id,
        is_active=True,
        registered_by=approved_by,
    )

    try:
        async with db.begin_nested():
            db.add(principal)
            db.add(alias)
            await db.flush()
    except IntegrityError as exc:
        if not _is_active_alias_uniqueness_violation(exc):
            raise
        raise PreferenceRejectedError(
            "alias_already_active",
            f"An active alias already exists for ({alias_source}, {alias_id}) in this tenant (concurrent registration race).",
        ) from exc

    return principal, alias


async def link_alias(
    db: AsyncSession,
    *,
    canonical_id: str,
    org_id: str,
    alias_source: str,
    alias_id: str,
    registered_by: str,
) -> ServicePrincipalAlias:
    """Link an additional alias to an existing service principal.

    Refuses if an active alias for the same (org, source, id) already exists,
    if the target principal is not found in the caller's tenant, or if the
    principal is not in the ``active`` status (suspended and retired principals
    cannot accept new aliases).
    """
    principal = await db.scalar(
        select(ServicePrincipal).where(
            ServicePrincipal.canonical_service_principal_id == canonical_id,
            ServicePrincipal.org_id == org_id,
        )
    )
    if principal is None:
        raise PreferenceRejectedError(
            "principal_not_found",
            f"No service principal '{canonical_id}' found in this tenant.",
        )
    if principal.status != "active":
        raise PreferenceRejectedError(
            "principal_not_active",
            f"Cannot link an alias to a {principal.status} service principal. Only active principals accept new aliases.",
        )

    existing = await db.scalar(
        select(ServicePrincipalAlias).where(
            ServicePrincipalAlias.org_id == org_id,
            ServicePrincipalAlias.alias_source == alias_source,
            ServicePrincipalAlias.alias_id == alias_id,
            ServicePrincipalAlias.is_active == True,  # noqa: E712
        )
    )
    if existing is not None:
        raise PreferenceRejectedError(
            "alias_already_active",
            f"An active alias already exists for ({alias_source}, {alias_id}) in this tenant.",
        )

    alias = ServicePrincipalAlias(
        id=new_uuid(),
        canonical_service_principal_id=canonical_id,
        org_id=org_id,
        alias_source=alias_source,
        alias_id=alias_id,
        is_active=True,
        registered_by=registered_by,
    )

    # Add inside the SAVEPOINT so a uniqueness violation does not poison the
    # outer transaction.  See register_service_principal for the full rationale.
    try:
        async with db.begin_nested():
            db.add(alias)
            await db.flush()
    except IntegrityError as exc:
        if not _is_active_alias_uniqueness_violation(exc):
            raise
        raise PreferenceRejectedError(
            "alias_already_active",
            f"An active alias already exists for ({alias_source}, {alias_id}) in this tenant (concurrent link race).",
        ) from exc

    return alias


async def revoke_alias(
    db: AsyncSession,
    *,
    alias_row_id: str,
    canonical_id: str,
    org_id: str,
    revoked_by: str,
) -> ServicePrincipalAlias:
    """Revoke an alias. Revoked aliases cannot be reactivated — re-registration
    creates a new canonical principal.
    """
    alias = await db.scalar(
        select(ServicePrincipalAlias).where(
            ServicePrincipalAlias.id == alias_row_id,
            ServicePrincipalAlias.canonical_service_principal_id == canonical_id,
            ServicePrincipalAlias.org_id == org_id,
        )
    )
    if alias is None:
        raise PreferenceRejectedError(
            "alias_not_found",
            f"No alias '{alias_row_id}' found for principal '{canonical_id}' in this tenant.",
        )
    if not alias.is_active:
        raise PreferenceRejectedError(
            "alias_already_revoked",
            f"Alias '{alias_row_id}' is already revoked.",
        )

    alias.is_active = False
    alias.revoked_at = utcnow()
    alias.revoked_by = revoked_by
    return alias


# ── Lifecycle transitions ─────────────────────────────────────────────────

# Allowed status transitions per the approved design (§4.1):
# active → suspended, active → retired, suspended → active, suspended → retired.
# retired is terminal.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "active": {"suspended", "retired"},
    "suspended": {"active", "retired"},
    "retired": set(),
}


async def transition_service_principal_status(
    db: AsyncSession,
    *,
    canonical_id: str,
    org_id: str,
    new_status: str,
) -> ServicePrincipal:
    """Transition a service principal's lifecycle status.

    Validates the transition against the allowed-transitions graph.
    ``retired`` is terminal — no transitions out.
    """
    principal = await db.scalar(
        select(ServicePrincipal).where(
            ServicePrincipal.canonical_service_principal_id == canonical_id,
            ServicePrincipal.org_id == org_id,
        )
    )
    if principal is None:
        raise PreferenceRejectedError(
            "principal_not_found",
            f"No service principal '{canonical_id}' found in this tenant.",
        )

    allowed = ALLOWED_TRANSITIONS.get(principal.status, set())
    if new_status not in allowed:
        raise PreferenceRejectedError(
            "invalid_status_transition",
            f"Cannot transition from '{principal.status}' to '{new_status}'. Allowed: {sorted(allowed) if allowed else 'none (terminal state)'}.",
        )

    principal.status = new_status
    return principal


async def list_service_principals(
    db: AsyncSession,
    *,
    org_id: str,
) -> list[ServicePrincipal]:
    """List all service principals in a tenant."""
    result = await db.scalars(select(ServicePrincipal).where(ServicePrincipal.org_id == org_id))
    return list(result)


async def list_aliases_for_principal(
    db: AsyncSession,
    *,
    canonical_id: str,
    org_id: str,
) -> list[ServicePrincipalAlias]:
    """List all aliases (active and revoked) for a service principal."""
    result = await db.scalars(
        select(ServicePrincipalAlias).where(
            ServicePrincipalAlias.canonical_service_principal_id == canonical_id,
            ServicePrincipalAlias.org_id == org_id,
        )
    )
    return list(result)


# ── Canonical principal resolution (for auth path) ─────────────────────────


async def resolve_canonical_principal_id(
    db: AsyncSession,
    *,
    user_id: str,
    org_id: str,
    account_type: str,
    alias_source: str,
) -> str:
    """Resolve a token context to a canonical principal ID.

    For human callers, resolves through the user identity resolver.
    For service callers, resolves through the alias registry using the exact
    trusted ``alias_source`` stamped on the token context during authentication.
    Returns empty string if resolution fails (graceful degradation for humans;
    for service callers an empty result means no registered alias).
    """
    try:
        if account_type == "human":
            from src.shared.identity.resolver import resolve_canonical_user_id

            return await resolve_canonical_user_id(db, user_id, org_id=org_id)

        if not alias_source:
            return ""

        # Service caller: resolve through alias registry using exact source
        canonical_id, _matched_source = await resolve_by_exact_source(db, alias_source=alias_source, alias_id=user_id, org_id=org_id)
        return canonical_id or ""
    except Exception:
        logger.debug("Canonical principal resolution failed; returning empty", exc_info=True)
        return ""


# ── Audit ────────────────────────────────────────────────────────────────────


async def write_audit(
    db: AsyncSession,
    *,
    event_type: str,
    org_id: str,
    actor_id: str | None,
    details: dict | None,
) -> None:
    """Record a preference authoring event.  Flushes on the caller's transaction."""
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
    """Record a refused write on its own transaction, swallowing failures.

    A refusal writes no preference row, so there is no caller transaction to
    ride.  Committing here is what makes the refusal an actual audit record.
    """
    try:
        db.add(AuditLog(org_id=org_id, event_type=event_type, actor_id=actor_id, details=details))
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Could not record persona-model preference refusal audit", extra={"event_type": event_type})
        await db.rollback()
