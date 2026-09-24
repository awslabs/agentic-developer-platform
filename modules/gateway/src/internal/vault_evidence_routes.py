"""Internal vault-evidence and operation-bound delivery endpoints — Issue #5528.

Wave 6 / w6-05. Three routes on the internal plane:

    POST /internal/v1/credential-evidence         — what the vault says about a credential
    POST /internal/v1/credential-revocation-state — does it still admit work?
    POST /internal/v1/credential-delivery         — hand the value to one bound executor

The decisions live in :mod:`src.auth.vault_evidence` and
:mod:`src.auth.vault_delivery`; this module is the transport. It authenticates,
parses, delegates, audits and shapes the HTTP answer, and re-implements no
authorization rule of its own — so the guarantees stay in one reviewable place.

The two planes these routes sit on
----------------------------------
The evidence and revocation-state routes return vault-held FACTS (ownership,
delegation, stored version id, expiry, an independently verified attestation
binding). They carry no secret material, so they authenticate as ordinary
service-to-service internal calls: ``verify_internal_or_irsa``, with ``org_id``
asserted by the caller. That is the documented contract of this whole plane —
``auth_deps.INTERNAL_PLANE_SCOPES`` exists precisely because "every /internal/*
route trusts its caller to assert org/tenant identity", and the scope allowlist is
what makes that trust non-self-assignable.

The delivery route returns SECRET MATERIAL, and for it that plane is not enough.

Why delivery cannot authenticate on the internal plane alone
------------------------------------------------------------
``agentauth/broker_identity`` opens with the fact that settles this: *"workers share
IRSA."* Every agent pod presents the same worker role, so ``token_context.user_id``
on the IRSA path names the shared *service* identity, not the individual executor.
A recipient check against it would pass for every worker in the fleet — the binding
would be recipient-LABELLED, not recipient-BOUND, which is the exact failure this
story exists to refuse.

The per-executor identity is the HMAC-verified run credential:
``RunCredential.principal`` is ``invocation_id#attempt``, minted by the gateway and
unforgeable without ``AGENT_RUN_CREDENTIAL_KEY``. Delivery therefore additionally
authenticates the individual run and pod through ``AgentRuntime.authenticate``, and
takes the recipient identity and the tenant from THAT, never from the body.

Two consequences are deliberate:

* ``binding.org_id`` must equal the verified ``tenant_id``. A body naming another
  tenant is refused before any vault read, so cross-tenant delivery is impossible
  even if the shared worker role were compromised.
* ``recipient`` must equal the verified ``principal``. Presenting another executor's
  id is refused, which is what makes the lease recipient-bound.

Why these are NOT in ``BROKER_PATHS``
------------------------------------
``BROKER_PATHS`` selects paths that run ``verify_broker_worker``, whose generic
branch requires ``body["user_id"]`` to equal the ``authorized_user_id`` on a
webhook-events row, and whose scope branch is keyed to the two legacy broker paths.
A Superplane workspace operation has no webhook invocation and no user-scoped row to
match, so adding these paths there would refuse every legitimate call. This is the
same reasoning ``agentauth/github_operation_routes.py`` records for its own
exclusion. Delivery reuses the part of that function that *does* apply — the run
credential and pod verification — and none of the parts that do not.

Why refusals are a uniform 403 and never a 503
----------------------------------------------
The domain's reader contract declares ``NONE_MEANS_UNVERIFIED`` and its consumer
maps a ``None`` read onto 403 and a raised exception onto 503. A refusal answered
with 503 would reach an operator as *the vault is broken* rather than *this was
denied*, sending them to diagnose an outage that is not happening. And the message
is identical for every denial class for the reason the services' own refusals are:
a response that distinguished "no such credential" from "another tenant's
credential" would let a caller map another tenant's vault by reading the
differences. 503 is reserved for the cases that genuinely ARE unavailability —
an unreachable authority store.

Activation status (read this before expecting delivery to serve anything)
------------------------------------------------------------------------
Delivery is gated on a server-held capability, :data:`DELIVERY_SCOPE`, resolved
from a strongly consistent read of the caller's ``agent_registry`` entry (
Issue #4131 — "the authoritative source for credential-scope decisions — never a
caller-supplied header"; the admin API constrains self-assignable scopes to
``^(shared|personal)$`` so this value is writable only by the Terraform seeds).

No seed grants that scope today. The endpoint is therefore INERT: it authenticates,
refuses, and audits the refusal. That is the intended fail-closed posture for an
unactivated capability — this story is implementation and offline verification only,
and granting the scope is a deployment action it does not authorize. The exact seed
change a live evaluator would need is named in the completion report.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStateError
from src.agentauth.run_credential import CredentialError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError
from src.auth.vault_delivery import (
    REVOCATION_LIMITATION,
    DeliveryRefusedError,
    OperationAuthorityUnavailableError,
    OperationBinding,
    deliver_credential,
    revocation_state,
)
from src.auth.vault_evidence import read_credential_evidence
from src.internal.auth_deps import verify_internal_or_irsa

# Imported rather than re-implemented. These are private to `credential_routes` by
# name, but they are the vault's established audit and last-used discipline, and a
# second copy here would be a second thing to keep in step with the audit schema.
from src.internal.credential_routes import _touch_last_used, _write_audit, get_secrets_manager
from src.shared.database import get_db
from src.shared.services.secrets_manager import SecretsManagerHelper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal-credentials"])

# The registry-granted capability required to receive credential material through
# this path. Gateway-side vocabulary: the Gateway has no workspace-permission model,
# so the route translates this server-held scope into the domain contract's
# `workspace:provision` permission at the boundary (see `_granted_permissions`).
DELIVERY_SCOPE = "credential:operation-delivery"

# One refusal message per route, deliberately coarse. See the module docstring.
_DENIED = "credential evidence could not be established"
_DELIVERY_DENIED = "credential delivery refused"


class EvidenceBody(BaseModel):
    """Request for vault evidence about one credential in one workspace.

    ``report_digest`` is the caller's CLAIMED provider-validation attestation. It is
    untrusted request context: the service recomputes the digest from the Gateway's
    own validation rows and refuses unless the two agree. It is accepted here only
    so that it can be checked — never so that it can be echoed.
    """

    org_id: str = Field(min_length=1, max_length=255)
    workspace_id: str = Field(min_length=1, max_length=255)
    credential_id: str = Field(min_length=1, max_length=64)
    principal: str = Field(min_length=1, max_length=255)
    service: str | None = Field(default=None, max_length=255)
    label: str | None = Field(default=None, max_length=255)
    # Length and charset pinned so a malformed claim is refused by the parser rather
    # than reaching a comparison.
    report_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class EvidenceResponse(BaseModel):
    """Vault-held facts only. Carries no secret value and no secret ARN.

    The ARN is absent even though it is an identifier rather than a value: the
    domain contract's ``CredentialReference`` refuses an ARN, because a reference
    complete enough to read the secret from is a disclosure in itself.
    """

    org_id: str
    workspace_id: str
    credential_id: str
    service: str
    label: str
    owner_principal: str
    owner_scope: str
    delegated_to_workspaces: list[str]
    current_version_id: str | None
    expires_at: datetime | None
    attested_report_digest: str | None
    report_checked_at: datetime | None


class DeliveryBody(BaseModel):
    """Request to deliver one credential to one executor for one operation.

    ``recipient`` is the identity the request NAMES. The authenticated identity comes
    from the verified run credential, and the handler refuses unless they match.
    Both exist so the mismatch is *detectable*: a request that could only ever name
    itself would make the recipient check unfalsifiable, and an unfalsifiable check
    is not evidence of anything.
    """

    operation_id: str = Field(min_length=1, max_length=255)
    attempt_id: str = Field(min_length=1, max_length=255)
    job_id: str = Field(min_length=1, max_length=255)
    org_id: str = Field(min_length=1, max_length=255)
    workspace_id: str = Field(min_length=1, max_length=255)
    credential_id: str = Field(min_length=1, max_length=64)
    recipient: str = Field(min_length=1, max_length=255)
    service: str | None = Field(default=None, max_length=255)
    label: str | None = Field(default=None, max_length=255)
    provider: str = Field(min_length=1, max_length=255)
    provider_account_id: str = Field(min_length=1, max_length=255)


class RevocationBody(BaseModel):
    """Request asking whether a credential still admits work for a workspace.

    A narrower body than :class:`DeliveryBody` on purpose: this route returns no
    material, so it needs the workspace binding but not an executor identity.
    Reusing the delivery body would have required callers to invent a recipient for
    a question that has no recipient.
    """

    operation_id: str = Field(min_length=1, max_length=255)
    attempt_id: str = Field(min_length=1, max_length=255)
    job_id: str = Field(min_length=1, max_length=255)
    org_id: str = Field(min_length=1, max_length=255)
    workspace_id: str = Field(min_length=1, max_length=255)
    credential_id: str = Field(min_length=1, max_length=64)


class DeliveryResponse(BaseModel):
    """The delivered value, plus the limitation of revoking it later.

    ``value`` is the one field in this module that carries secret material. It is
    returned over the protected internal transport to the authenticated recipient,
    and is never persisted, audited or logged — the audit call below records
    identifiers only.

    ``revocation_limitation`` travels on the SUCCESS path too, not only on refusals.
    The operator who needs it is the one who later believes disabling this delivery
    contained a leak, and by then they are not reading a refusal.
    """

    value: str
    credential_type: str
    credential_id: str
    provenance_id: str
    revocation_limitation: str = REVOCATION_LIMITATION


class RevocationResponse(BaseModel):
    admits_work: bool
    limitation: str


def _granted_permissions(request: Request) -> frozenset[str]:
    """Translate the caller's server-held scope into the contract's permission.

    The authenticated registry ID locates a strongly consistent current row.
    Cached scopes and caller-supplied headers cannot grant this capability. Returns the empty set when the scope is absent, which the
    delivery service treats as a refusal: an unresolved capability is not a grant.

    The translation is one-way and deliberately narrow. Holding
    :data:`DELIVERY_SCOPE` grants exactly ``workspace:provision`` (obtaining a
    credential for use) and never ``workspace:renew_credential`` (registering or
    rotating one) — the contract keeps those separate because managing a credential
    is not authority to use it, and this boundary must not quietly merge them.
    """
    context = getattr(request.state, "token_context", None)
    # Only API Gateway's IAM-authenticated identity can select this registry row.
    # A shared key or caller-supplied scope never grants delivery authority.
    from src.auth.agent_registry import get_agent_registry_service, parse_assumed_role_arn
    from src.internal.auth_deps import INTERNAL_PLANE_SCOPES

    role = parse_assumed_role_arn(request.headers.get("X-Caller-Identity", ""))
    registry_id = getattr(context, "agent_registry_id", "")
    if getattr(context, "auth_source", None) != "iam" or not role or not registry_id:
        return frozenset()
    try:
        current = get_agent_registry_service().get_current_agent(registry_id, role)
    except (ClientError, BotoCoreError):
        raise HTTPException(503, "agent registry unavailable") from None
    if current is None or current["scope"] not in INTERNAL_PLANE_SCOPES:
        return frozenset()
    scopes = current.get("credential_scopes", [])
    if DELIVERY_SCOPE not in scopes:
        return frozenset()
    from src.auth.vault_delivery import DELIVERY_PERMISSION

    return frozenset({DELIVERY_PERMISSION})


async def _verified_executor(request: Request) -> tuple[str, str]:
    """Authenticate the individual run and pod behind this request.

    Returns ``(principal, tenant_id)`` from the HMAC-verified run credential —
    ``principal`` being ``invocation_id#attempt``, the only per-executor identity
    available (the IRSA identity is shared fleet-wide; see the module docstring).

    Mirrors the credential/workload half of ``verify_broker_worker`` and nothing
    else: the pod is verified so a stolen credential cannot be replayed from another
    workload, the execution state is checked so a finished or superseded attempt
    cannot collect material, and the live grant is required so a revoked run cannot.

    Refusals are a uniform 403. A genuinely unreachable authority store is a 503,
    because that one IS unavailability rather than denial.
    """
    # Local import: `routes` composes the transport, and importing it at module
    # scope would close a cycle through this module's own registration.
    from src.agentauth.routes import get_agent_runtime
    from src.internal.domain_operation_runtime import maybe_domain_executor

    domain_executor = await maybe_domain_executor(request)
    if domain_executor is not None:
        return domain_executor

    try:
        runtime = get_agent_runtime()
        pod, caller, record, grant = await run_in_threadpool(
            runtime.authenticate,
            request.headers.get(CREDENTIAL_HEADER, ""),
            request.headers.get(WORKLOAD_HEADER, ""),
        )
        await runtime.validate_flow(record, grant)
    except HTTPException:
        # get_agent_runtime's own 503s ("agent authority is not enabled/configured")
        # are already the right answer and say nothing about any credential.
        raise
    except (BootstrapRefusedError, WorkloadRefusedError, CredentialError, ExecutionStateError, ValueError, KeyError):
        logger.info("Vault delivery refused: run identity not verified")
        raise HTTPException(status_code=403, detail={"error": "denied", "message": _DELIVERY_DENIED}) from None
    except (AuthorityStoreError, ClientError, BotoCoreError):
        raise HTTPException(status_code=503, detail={"error": "unavailable", "message": "agent authority unavailable"}) from None

    del pod  # verified above; this module needs no field from it
    return caller.principal, caller.tenant_id


@router.post(
    "/credential-evidence",
    response_model=EvidenceResponse,
    summary="Vault evidence for one credential in one workspace (internal)",
    description=(
        "Returns the vault's own record of ownership, delegation, current stored "
        "version and expiry, plus an independently verified provider-validation "
        "binding when report_digest is supplied. A supplied digest is recomputed "
        "from the Gateway's own validation rows and is never echoed. Secret values "
        "and ARNs are NEVER returned. Unestablished conditions return a uniform 403."
    ),
)
async def credential_evidence(
    body: EvidenceBody,
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
    _: None = Depends(verify_internal_or_irsa),
) -> EvidenceResponse:
    evidence = await read_credential_evidence(
        db,
        sm,
        org_id=body.org_id,
        workspace_id=body.workspace_id,
        credential_id=body.credential_id,
        service=body.service,
        label=body.label,
        principal=body.principal,
        report_digest=body.report_digest,
    )
    if evidence is None:
        # 403, never 503 — see the module docstring. No detail about WHICH condition
        # failed, so this cannot be used to enumerate another tenant's vault.
        raise HTTPException(status_code=403, detail={"error": "denied", "message": _DENIED})

    return EvidenceResponse(
        org_id=evidence.org_id,
        workspace_id=evidence.workspace_id,
        credential_id=evidence.credential_id,
        service=evidence.service,
        label=evidence.label,
        owner_principal=evidence.owner_principal,
        owner_scope=evidence.owner_scope,
        # Sorted for a stable response body; the service's frozenset has no order.
        delegated_to_workspaces=sorted(evidence.delegated_to_workspaces),
        current_version_id=evidence.current_version_id,
        expires_at=evidence.expires_at,
        attested_report_digest=evidence.attested_report_digest,
        report_checked_at=evidence.report_checked_at,
    )


@router.post(
    "/credential-revocation-state",
    response_model=RevocationResponse,
    summary="Whether a credential still admits work for a workspace (internal)",
    description=(
        "Reports whether this credential still admits work for the bound workspace. "
        "A negative answer always carries the limitation that expiring a delivery "
        "lease does not revoke an already-issued provider key."
    ),
)
async def credential_revocation_state(
    body: RevocationBody,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_or_irsa),
) -> RevocationResponse:
    try:
        binding = OperationBinding(
            operation_id=body.operation_id,
            attempt_id=body.attempt_id,
            job_id=body.job_id,
            org_id=body.org_id,
            workspace_id=body.workspace_id,
        )
    except DeliveryRefusedError:
        raise HTTPException(status_code=403, detail={"error": "denied", "message": _DELIVERY_DENIED}) from None

    answer = await revocation_state(
        db,
        binding=binding,
        credential_id=body.credential_id,
        now=datetime.now(UTC),
    )
    return RevocationResponse(admits_work=answer.admits_work, limitation=answer.limitation)


@router.post(
    "/credential-delivery",
    response_model=DeliveryResponse,
    summary="Deliver one credential to one bound executor (internal)",
    description=(
        "Delivers a credential value to the authenticated executor named as the "
        "recipient, for one job attempt in one workspace. Requires a verified run "
        "credential and workload token — the shared worker IRSA identity is not "
        "sufficient. Refuses missing, revoked, foreign, ambiguous and undelegated "
        "references, a tenant that differs from the verified one, a caller that is "
        "not the named recipient, and a caller without the delivery capability. "
        "Every outcome is audit-logged; the value is never logged or persisted."
    ),
)
async def credential_delivery(
    body: DeliveryBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
    _: None = Depends(verify_internal_or_irsa),
) -> DeliveryResponse:
    return await _executor_credential_request(body, request, db, sm, preflight_only=False)


@router.post("/credential-delivery/preflight", response_model=RevocationResponse)
async def credential_delivery_preflight(
    body: DeliveryBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
    _: None = Depends(verify_internal_or_irsa),
) -> RevocationResponse:
    """Verify current attempt authority without reading or returning material."""
    return await _executor_credential_request(body, request, db, sm, preflight_only=True)


async def _executor_credential_request(body, request, db, sm, *, preflight_only):
    provenance_id = str(uuid.uuid4())

    # Individual run + pod, before any vault read: an unverified caller must not be
    # able to learn anything from the shape or timing of a vault lookup.
    principal, tenant_id = await _verified_executor(request)

    async def refresh_executor():
        if await _verified_executor(request) != (principal, tenant_id):
            raise DeliveryRefusedError(_DELIVERY_DENIED)
        return await run_in_threadpool(_granted_permissions, request)

    # The verified tenant wins over the asserted one. Checked here rather than left
    # to the service because the service is given `binding.org_id` as the tenant to
    # scope every query by — a body naming a foreign tenant must never reach it.
    if body.org_id != tenant_id:
        logger.warning(
            "Vault delivery refused: asserted tenant does not match verified run identity provenance_id=%s",
            provenance_id,
        )
        await _audit_refusal(db, body, principal=principal, provenance_id=provenance_id, reason="tenant_mismatch")
        raise HTTPException(
            status_code=403,
            detail={"error": "denied", "message": _DELIVERY_DENIED, "provenance_id": provenance_id},
        )

    try:
        binding = OperationBinding(
            operation_id=body.operation_id,
            attempt_id=body.attempt_id,
            job_id=body.job_id,
            org_id=body.org_id,
            workspace_id=body.workspace_id,
            provider=body.provider,
            provider_account_id=body.provider_account_id,
        )
        from contextlib import AsyncExitStack

        from src.internal.domain_operation_store import operation_session

        domain_binding = getattr(request.state, "domain_operation_binding", None)
        async with AsyncExitStack() as stack:
            operation_db = await stack.enter_async_context(operation_session(domain_binding)) if domain_binding else db
            credential, secret = await deliver_credential(
                db,
                sm,
                binding=binding,
                credential_id=body.credential_id,
                service=body.service,
                label=body.label,
                recipient=body.recipient,
                authenticated_recipient=principal,
                granted_permissions=await run_in_threadpool(_granted_permissions, request),
                refresh_executor=refresh_executor,
                preflight_only=preflight_only,
                operation_session=operation_db,
                vault_org_id=domain_binding.adp_org_id if domain_binding else None,
            )
    except OperationAuthorityUnavailableError:
        raise HTTPException(status_code=503, detail={"error": "unavailable", "message": "operation authority unavailable"}) from None
    except DeliveryRefusedError:
        await _audit_refusal(db, body, principal=principal, provenance_id=provenance_id, reason="refused")
        raise HTTPException(
            status_code=403,
            detail={"error": "denied", "message": _DELIVERY_DENIED, "provenance_id": provenance_id},
        ) from None

    if preflight_only:
        return RevocationResponse(admits_work=True, limitation=REVOCATION_LIMITATION)

    assert secret is not None
    await _touch_last_used(credential.id, db)
    await _write_audit(
        db,
        event_type="vault_credential_delivered",
        org_id=body.org_id,
        actor_id=principal,
        details={
            "provenance_id": provenance_id,
            "workspace_id": body.workspace_id,
            "credential_id": credential.id,
            "credential_type": credential.credential_type,
            "service": credential.service,
            "label": credential.label,
            "operation_id": body.operation_id,
            "attempt_id": body.attempt_id,
            "job_id": body.job_id,
            "recipient": body.recipient,
            # Recorded so an operator reading this row learns that a lease is not
            # provider-side revocation without having to find the runbook first.
            "revocation_limitation": REVOCATION_LIMITATION,
        },
    )
    await db.commit()

    return DeliveryResponse(
        # reveal() at the transport boundary. The container exists to stop accidental
        # rendering into logs and transcripts; this is the one intended egress.
        value=secret.reveal(),
        credential_type=credential.credential_type,
        credential_id=credential.id,
        provenance_id=provenance_id,
    )


async def _audit_refusal(
    db: AsyncSession,
    body: DeliveryBody,
    *,
    principal: str,
    provenance_id: str,
    reason: str,
) -> None:
    """Record a refused delivery, then let the refusal proceed regardless.

    ``reason`` is a coarse internal label written to the audit row only — it is
    never returned to the caller, so the operator-facing record can be more specific
    than the uniform response without that specificity becoming an oracle.

    Identifiers only: the refusal exception's message is deliberately NOT recorded,
    so that if a future refusal message ever gained detail it could not reach a
    persisted row through here. Audit failure is logged and swallowed, matching the
    vault's existing denial-audit discipline — a failed write must not convert a
    security refusal into a 500 that the caller could provoke on purpose.
    """
    try:
        await _write_audit(
            db,
            event_type="vault_credential_delivery_denied",
            org_id=body.org_id,
            actor_id=principal,
            details={
                "provenance_id": provenance_id,
                "reason": reason,
                "workspace_id": body.workspace_id,
                "credential_id": body.credential_id,
                "operation_id": body.operation_id,
                "attempt_id": body.attempt_id,
                "job_id": body.job_id,
                "named_recipient": body.recipient,
                "authenticated_recipient": principal,
            },
        )
        await db.commit()
    except Exception:
        logger.warning("Delivery denial audit failed provenance_id=%s", provenance_id)
