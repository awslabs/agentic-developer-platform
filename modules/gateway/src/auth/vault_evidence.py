"""Trusted vault evidence for Superplane credential references — Issue #5528.

Wave 6 / w6-05. This module answers one question for the Superplane domain: *what
does the vault itself say about this credential?* Ownership, the workspaces its
owner delegated it to, the current stored version, expiry — and, when the caller
claims a provider validation attestation, whether that claim matches a validation
report the Gateway independently holds.

Why the attestation is recomputed, never echoed
-----------------------------------------------
The consumer sends a ``report_digest`` derived from a ``ValidationReport`` it holds
domain-side. That digest is request context, not proof: a caller that can put a
digest in a request can put any digest in a request. If this module compared the
caller's digest against itself, or copied it into the response, every caller could
manufacture an attestation for a credential that was never validated — which is
precisely the authority the digest is supposed to establish.

So the digest is recomputed here from ``credential_validation_evidence`` rows the
Gateway owns, using the identical serialisation the consumer uses, and the answer
is ``None`` unless the two independently-computed values agree. The recipe lives in
:func:`validation_digest` with the consumer's source pinned in its docstring;
:mod:`tests.auth.test_vault_evidence` derives the expected value from the
consumer's own code so the two cannot drift apart silently.

Why every failure returns ``None``
----------------------------------
The port's registry entry declares ``UnknownOutcome.NONE_MEANS_UNVERIFIED``: this
reader must return ``None`` for anything it cannot establish and must not raise to
signal refusal. The consumer maps ``None`` onto a 403 and a raise onto a 503, so a
raise would report a *denied* credential as a *broken vault* — turning an
authorization decision into a false outage. ``_MISSING`` conditions are therefore
all one return path with no distinguishing detail, which also keeps the reader from
becoming an oracle for which credentials exist in other tenants.

This module performs reads only. It never returns, logs, or stores a credential
value; delivery of material is :mod:`src.auth.vault_delivery`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.vault import (
    CredentialValidationEvidence,
    CredentialWorkspaceDelegation,
    UserCredential,
)
from src.shared.services.secrets_manager import SecretsManagerHelper

logger = logging.getLogger(__name__)

# The contract version this implementation is written against. Checked against the
# registry rather than assumed: w6-01's `check_port_version` refuses a version the
# registry does not declare, so a contract bump fails here instead of silently
# serving a stale shape. See `verify_contract_version` below.
DECLARED_CONTRACT_VERSION = "v1"

# The staging label naming the current stored value, mirrored from the Secrets
# Manager helper so a reader of this module can see what "current" means.
CURRENT_VERSION_UNKNOWN = "unknown"


@dataclass(frozen=True)
class VaultCredentialEvidence:
    """What the vault says about one credential, for one workspace.

    Deliberately NOT the contract's ``VerifiedCredentialEvidence``: that type lives
    in ``superplane_contracts``, which the Gateway does not depend on (its
    ``pyproject.toml`` lists no such dependency, and the domain's own models mirror
    rather than import for the same reason). The shared client in
    ``superplane_contracts``-land maps this onto the contract type at the boundary,
    so the Gateway stays installable without the contract package while the mapping
    stays in one reviewable place.

    ``owner_principal`` is the vault's own record, derived from the credential's
    ownership columns — never from anything in the request.

    ``attested_report_digest`` is populated only when a caller-supplied digest was
    independently recomputed and matched. ``None`` means "no attestation
    established", which is distinct from "attestation failed" only to the caller
    that supplied no digest at all.
    """

    org_id: str
    workspace_id: str
    credential_id: str
    service: str
    label: str
    owner_principal: str
    owner_scope: str
    delegated_to_workspaces: frozenset[str]
    current_version_id: str | None
    expires_at: datetime | None
    attested_report_digest: str | None = None
    report_checked_at: datetime | None = None


def validation_digest(
    *,
    credential_valid: bool,
    permissions_sufficient: bool,
    quota_available: bool,
    observed_capacity: int | None,
    detail: str,
) -> str:
    """Recompute the attestation digest over the four readings plus detail.

    PINNED to the consumer's recipe at
    ``modules/domain-apps/superplane/src/superplane-api/app/routers/provider_connections.py``
    (``_vault_evidence``), which does::

        readings = asdict(report)
        readings.pop("checked_at")
        digest = hashlib.sha256(
            json.dumps(readings, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    Three details are load-bearing and each would break every legitimate call if
    changed independently:

    * ``checked_at`` is EXCLUDED. It is the local receipt time on the consumer's
      side, not a provider measurement, so including it would make the digest
      depend on when the row was written.
    * ``sort_keys=True`` with ``separators=(",", ":")`` — the serialisation must be
      byte-identical on both sides, so key order and whitespace are fixed.
    * the field set is exactly ``ValidationReport`` minus ``checked_at``:
      ``credential_valid``, ``permissions_sufficient``, ``quota_available``,
      ``observed_capacity``, ``detail``. A field added to the contract's report
      must be added here, which is why the test derives the field list from the
      consumer rather than restating it.

    ``observed_capacity`` passes through untouched, including ``0`` and ``None``:
    the contract treats "not measured" and "measured as zero" as different facts,
    and they must digest differently.
    """
    readings = {
        "credential_valid": bool(credential_valid),
        "permissions_sufficient": bool(permissions_sufficient),
        "quota_available": bool(quota_available),
        "observed_capacity": observed_capacity,
        "detail": detail or "",
    }
    canonical = json.dumps(readings, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def verify_contract_version(declared: str = DECLARED_CONTRACT_VERSION) -> bool:
    """Whether this implementation's contract version is one the registry serves.

    Fails CLOSED and stays importable without the contract package: if
    ``superplane_contracts`` is absent (the Gateway does not depend on it) this
    returns False rather than raising, because "I cannot check the version" must
    not read as "the version is fine".
    """
    try:
        from superplane_contracts.integration import check_port_version
    except ImportError:
        return False
    try:
        return bool(check_port_version("credential_evidence", declared).accepted)
    except Exception:
        return False


def _owner_principal(credential: UserCredential) -> str | None:
    """The principal the vault records as owning *credential*.

    Derived from the ownership columns, whose invariant is that exactly one of
    ``user_id`` / ``team_id`` / ``domain_app_id`` is set (org scope being the
    all-NULL case). Prefixed per scope so a user id and a team id with the same
    string value are not the same principal — an unprefixed comparison would make
    ``is_owned_by`` true across scopes.

    ``None`` when the row violates its own invariant, which is unestablished
    ownership and therefore a denial rather than a best guess.
    """
    scope = credential.owner_scope
    if scope == "user":
        return f"user:{credential.user_id}" if credential.user_id else None
    if scope == "team":
        return f"team:{credential.team_id}" if credential.team_id else None
    if scope == "domain_app":
        return f"domain_app:{credential.domain_app_id}" if credential.domain_app_id else None
    if scope == "org":
        return f"org:{credential.org_id}" if credential.org_id else None
    return None


def _as_utc(value: datetime | None) -> datetime | None:
    """Treat a naive stored timestamp as UTC.

    SQLite drops tzinfo on round-trip while Postgres preserves it, so the same row
    reads differently per backend. The consumer REFUSES a naive ``expires_at``, so
    normalising here is what keeps behaviour identical on both.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def resolve_exact_credential(
    session: AsyncSession,
    *,
    org_id: str,
    credential_id: str,
    service: str | None = None,
    label: str | None = None,
) -> UserCredential | None:
    """Resolve EXACTLY the named credential inside *org_id*, or ``None``.

    Deliberately not ``CredentialResolver.resolve``: that method matches by service
    and then RANKS candidates, returning "the first matching credential" — correct
    for "find me a credential for this service" and wrong here, where returning a
    different credential than the one named is the credential-substitution failure
    this story has to refuse.

    Also deliberately not ``vault_service._get_owned_credential``: its own docstring
    states it is for MUTATIONS and applies the shared-scope admin gate, which would
    deny a read that should succeed.

    The ``org_id`` filter is the tenant boundary: without it a credential id from
    another tenant would resolve and every downstream check would then be comparing
    another organization's records. When ``service``/``label`` are supplied they must
    AGREE with the stored row — a reference whose id and metadata disagree is
    ambiguous about which credential it names, so it is refused rather than resolved
    in favour of the id.
    """
    if not org_id or not credential_id:
        return None
    row = await session.scalar(
        select(UserCredential)
        .where(
            UserCredential.id == credential_id,
            UserCredential.org_id == org_id,
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    if service is not None and row.service != service:
        return None
    if label is not None and row.label != label:
        return None
    return row


async def delegated_workspaces(
    session: AsyncSession,
    *,
    org_id: str,
    credential_id: str,
) -> frozenset[str]:
    """The workspaces this credential is currently delegated to.

    Revoked rows are excluded: they are retained so a refusal can distinguish
    "never delegated" from "withdrawn", but a withdrawn delegation must not admit
    work. Scoped by ``org_id`` as well as credential id — a delegation row is only
    meaningful within the tenant that owns the credential.
    """
    rows = await session.scalars(
        select(CredentialWorkspaceDelegation.workspace_id).where(
            CredentialWorkspaceDelegation.credential_id == credential_id,
            CredentialWorkspaceDelegation.org_id == org_id,
            CredentialWorkspaceDelegation.revoked_at.is_(None),
        )
    )
    return frozenset(rows.all())


async def _attestation(
    session: AsyncSession,
    *,
    org_id: str,
    workspace_id: str,
    credential_id: str,
    report_digest: str,
    current_version_id: str | None,
) -> tuple[str, datetime] | None:
    """Independently establish the caller's claimed attestation, or ``None``.

    The stored row is the Gateway's own copy of the provider readings. Its digest
    is recomputed here and compared to the caller's claim; a mismatch means the
    caller is asserting a validation result the Gateway does not hold.

    The provider's observation time is returned alongside because the consumer
    refuses evidence whose ``report_checked_at`` is absent, naive, or in the
    future — so a row with an unusable timestamp is no attestation at all.
    """
    row = await session.scalar(
        select(CredentialValidationEvidence)
        .where(
            CredentialValidationEvidence.credential_id == credential_id,
            CredentialValidationEvidence.workspace_id == workspace_id,
            CredentialValidationEvidence.org_id == org_id,
        )
        .execution_options(populate_existing=True)
    )
    if row is None or not current_version_id or row.validated_version_id != current_version_id:
        return None
    computed = validation_digest(
        credential_valid=row.credential_valid,
        permissions_sufficient=row.permissions_sufficient,
        quota_available=row.quota_available,
        observed_capacity=row.observed_capacity,
        detail=row.detail,
    )
    # Constant-time compare: the digest is an authorization token in this
    # comparison, and a short-circuiting == leaks a prefix oracle over retries.
    if not hmac.compare_digest(computed, report_digest):
        return None
    checked_at = _as_utc(row.checked_at)
    if checked_at is None or checked_at > datetime.now(UTC):
        # A measurement dated in the future was not a measurement.
        return None
    return computed, checked_at


async def read_credential_evidence(
    session: AsyncSession,
    sm: SecretsManagerHelper,
    *,
    org_id: str,
    workspace_id: str,
    credential_id: str,
    service: str | None,
    label: str | None,
    principal: str,
    report_digest: str | None,
) -> VaultCredentialEvidence | None:
    """Current vault evidence for one credential in one workspace, or ``None``.

    Returns ``None`` — never raises a refusal — for every unestablished condition:
    unknown or foreign credential, reference whose metadata disagrees with the
    stored row, unresolvable ownership, a principal with no relationship to the
    credential, or a claimed attestation that does not match the Gateway's own
    record. See the module docstring for why a raise would be wrong here.
    """
    if not workspace_id or not workspace_id.strip() or not principal or not principal.strip():
        return None

    credential = await resolve_exact_credential(
        session,
        org_id=org_id,
        credential_id=credential_id,
        service=service,
        label=label,
    )
    if credential is None:
        return None

    owner = _owner_principal(credential)
    if owner is None:
        return None

    workspaces = await delegated_workspaces(session, org_id=org_id, credential_id=credential.id)

    # The caller must have a vault-recorded relationship to this credential: either
    # it IS the owner, or the owner delegated the credential to the workspace being
    # asked about. `authorize_delegation` re-checks this consumer-side against the
    # ownership record returned here; checking it here too means a principal with no
    # relationship gets no evidence to reason about in the first place.
    if principal != owner and workspace_id not in workspaces:
        return None

    # Metadata read, never the value.
    try:
        version_id = sm.current_version_id(credential.secret_arn)
    except Exception:
        # An unreadable version is unknown, not fatal: ownership and delegation are
        # still established facts. Logged without the ARN's secret value (an ARN is
        # an identifier, but it is still not echoed to the caller).
        logger.warning("Could not determine current version for credential %s", credential.id)
        version_id = None

    attested_digest: str | None = None
    report_checked_at: datetime | None = None
    if report_digest is not None:
        established = await _attestation(
            session,
            org_id=org_id,
            workspace_id=workspace_id,
            credential_id=credential.id,
            report_digest=report_digest,
            current_version_id=version_id,
        )
        if established is None:
            # The caller asked for an attestation the Gateway cannot establish.
            # Returning evidence WITHOUT the digest would be worse than returning
            # nothing: the consumer requires the digests to match when it sent one,
            # so a partial answer reads as a vault malfunction rather than a refusal.
            return None
        attested_digest, report_checked_at = established

    return VaultCredentialEvidence(
        org_id=org_id,
        workspace_id=workspace_id,
        credential_id=credential.id,
        service=credential.service,
        label=credential.label,
        owner_principal=owner,
        owner_scope=credential.owner_scope,
        delegated_to_workspaces=workspaces,
        current_version_id=version_id,
        expires_at=_as_utc(credential.expires_at),
        attested_report_digest=attested_digest,
        report_checked_at=report_checked_at,
    )
