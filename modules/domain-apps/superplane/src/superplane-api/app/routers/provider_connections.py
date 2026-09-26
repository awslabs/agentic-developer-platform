"""Provider-connection routes — U7's contract, served over HTTP.

Issue #5053 (U7b), EPIC #4910. R7 server half, acceptances 1, 2, 3 and 5.

## Why these routes carry the workspace in the path

Every route here is ``/workspaces/{workspace_id}/provider-connections/...``, and the
path parameter is named exactly ``workspace_id`` because that is the name
``app/domain_guard.py`` resolves (``WORKSPACE_PATH_PARAM``).

That is not cosmetic. The guard publishes the caller's server-held grant on
``request.state.grant`` **only** for ``Scope.WORKSPACE`` routes; an
``Scope.ORGANIZATION`` route gets none. ``authorize_delegation`` requires
``workspace:renew_credential`` from that grant, so a credential-keyed route
registered as organization-scoped would see an empty permission set and refuse
*every* caller, including a fully privileged one. A route that denies everybody still
passes every negative test, which is why this family is workspace-scoped and why
``tests/test_workspaces.py`` pins a positive case for each route as well as the
negative ones.

## What each route does at its boundary

``accept_connection_request`` runs the secret check over the **whole submitted
payload** before any field is read, which is why the registration handler takes the
raw body rather than a typed model. A Pydantic model would drop unknown keys under its
default ``extra="ignore"``, so a request carrying a valid reference *beside* a stray
``secret_access_key`` would be accepted for the former while the latter was quietly
discarded — the submitter believing they had sent a credential, and nothing reporting
that a secret had crossed the wire. Refusing the whole request is acceptance 1.

Responses are built by ``superplane_contracts.emission``, which emits from an
allowlist. A field added to ``ConnectionState`` later is therefore absent from the
wire until someone deliberately publishes it, rather than appearing by default and
relying on a reviewer to notice.

Error bodies are plain strings written here. FastAPI's default 422 handler echoes each
error's ``input`` verbatim, so a validation error on a field holding an ARN returns
the ARN — in the one response whose entire purpose is refusing secret material. This
was not theoretical: submitting an ARN to ``POST /vault/credentials`` returned the
full ARN, account id included, and that is now fixed by the scrubbing handler this
story adds in ``app/main.py``.

These handlers additionally never *generate* such a 422: they validate the reference
themselves and raise a 400 whose message names the rule rather than the value, so this
family does not depend on the backstop being correct.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from superplane_contracts.connections import (
    RENEW_CREDENTIAL_PERMISSION,
    ConnectionState,
    CredentialReference,
    ValidationReport,
    VaultOwnership,
    accept_connection_request,
    authorize_delegation,
)
from superplane_contracts.emission import connection_response, validation_response
from superplane_contracts.health import ContractViolation
from superplane_contracts.secrets import assert_no_secret_material

from app.auth import _grant_to_policy_object
from app.database import get_session
from app.middleware.auth import get_current_org
from app.models.provider_connection import ProviderConnection, ProviderConnectionBinding
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from app.services import provider_connections as service
from app.services.cli_lifecycle import connection_revision, require_connection_revision
from app.services.credential_evidence import (
    VerifiedCredentialEvidence,
    get_credential_evidence_reader,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["provider-connections"])

_CONNECTIONS = "/{workspace_id}/provider-connections"

# One refusal string per denial class, mirroring the contract's own choice to keep a
# single constant per class: a branch that became more informative than its siblings
# would turn the endpoint into an oracle for which half of the check failed.
_DENIED = "not authorized to delegate this credential for this workspace"
_NOT_BOUND = "credential is not bound to this workspace"
_NOT_FOUND = "provider connection not found"

# Said out loud, unlike the two above, and only to a caller who has already proved
# org scope, the workspace binding AND ownership of the credential. Without this
# branch a disabled connection failed `authorize_use`'s `admits_new_work` term and
# came back as `_NOT_BOUND` — telling the operator who had just disabled it that it
# belongs to another workspace, which is false and sends them looking in the wrong
# place. The contract collapses its denials into one string to avoid becoming an
# enumeration oracle; that reasoning does not apply to a caller who has cleared every
# check the oracle would be probing for, so the accurate answer is cheap here.
_DISABLED = (
    "connection is disabled: disablement blocks new admissions and credential "
    "renewals, including rotation"
)


def _caller_principal(request: Request, org_id: uuid.UUID) -> str:
    """Credential changes require a verified individual principal."""
    caller = getattr(request.state, "caller", None)
    if caller is None:
        raise HTTPException(
            status_code=403, detail="verified workspace identity is required"
        )
    principal = caller.principal.subject
    if not isinstance(principal, str) or not principal.strip() or len(principal) > 255:
        raise HTTPException(
            status_code=403, detail="verified workspace identity is required"
        )
    return principal


def _current(evidence: VerifiedCredentialEvidence) -> None:
    if evidence.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=403, detail="credential evidence expired")


async def _vault_evidence(request, org_id, workspace_id, reference, report=None):
    principal = _caller_principal(request, org_id)
    reader = get_credential_evidence_reader()
    if reader is None:
        raise HTTPException(status_code=503, detail="ADP vault evidence is unavailable")
    digest = None
    if report is not None:
        readings = asdict(report)
        readings.pop("checked_at")  # Local receipt time is not a provider measurement.
        digest = hashlib.sha256(
            json.dumps(readings, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    try:
        evidence = await reader.read(
            org_id=str(org_id),
            workspace_id=str(workspace_id),
            reference=reference,
            principal=principal,
            report_digest=digest,
        )
    except Exception:
        raise HTTPException(
            status_code=503, detail="ADP vault evidence is unavailable"
        ) from None
    if (
        not isinstance(evidence, VerifiedCredentialEvidence)
        or evidence.org_id != str(org_id)
        or evidence.workspace_id != str(workspace_id)
        or evidence.reference != reference
        or not isinstance(evidence.ownership, VaultOwnership)
        or not isinstance(evidence.expires_at, datetime)
        or evidence.expires_at.tzinfo is None
        or evidence.expires_at.utcoffset() is None
        or (digest is not None and evidence.attested_report_digest != digest)
    ):
        raise HTTPException(status_code=403, detail=_DENIED)
    _current(evidence)
    if digest is not None and (
        not isinstance(evidence.report_checked_at, datetime)
        or evidence.report_checked_at.tzinfo is None
        or evidence.report_checked_at.utcoffset() is None
        or evidence.report_checked_at > datetime.now(timezone.utc)
    ):
        raise HTTPException(
            status_code=403, detail="provider observation time is not verified"
        )
    decision = authorize_delegation(
        principal=principal,
        workspace_id=str(workspace_id),
        ownership=evidence.ownership,
        reference=reference,
        granted_permissions=_permissions(request),
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=_DENIED)
    return evidence


def _permissions(request: Request) -> frozenset[str]:
    """The caller's server-held permissions (see the service's ``granted_permissions``)."""
    return service.granted_permissions(getattr(request.state, "grant", None))


async def _registry_reference(
    db, org_id, reference, provider, *, lock=False, what="credential"
):
    try:
        await service.assert_credential_registered(
            db,
            org_id=org_id,
            credential_id=reference.credential_id,
            provider=provider,
            credential_service=reference.service,
            lock=lock,
        )
    except service.CredentialNotRegistered:
        raise HTTPException(
            status_code=404, detail=f"{what} is not registered for this organization"
        ) from None
    except service.CredentialProviderMismatch:
        raise HTTPException(
            status_code=400,
            detail="credential service does not match the connection provider",
        ) from None


async def _mutation_authority(db, request, org_id, workspace_id, provider, *evidence):
    """Re-read protected authority after waits and hold it through the commit."""
    principal = _caller_principal(request, org_id)
    caller = request.state.caller.principal
    if caller.org_id != str(org_id):
        raise HTTPException(status_code=403, detail=_DENIED)
    with db.no_autoflush:
        grant = (
            await db.execute(
                select(WorkspaceGrantRecord)
                .join(Workspace, Workspace.id == WorkspaceGrantRecord.workspace_id)
                .where(
                    Workspace.id == workspace_id,
                    Workspace.org_id == org_id,
                    WorkspaceGrantRecord.org_id == org_id,
                    WorkspaceGrantRecord.principal == principal,
                    WorkspaceGrantRecord.principal_type == caller.account_type,
                    WorkspaceGrantRecord.revoked_at.is_(None),
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            grant is None
            or RENEW_CREDENTIAL_PERMISSION
            not in service.granted_permissions(_grant_to_policy_object(grant))
        ):
            raise HTTPException(status_code=403, detail=_DENIED)
        for item in sorted(evidence, key=lambda value: value.reference.credential_id):
            await _registry_reference(db, org_id, item.reference, provider, lock=True)
        for item in evidence:
            _current(item)


def _state(
    connection: ProviderConnection, binding: ProviderConnectionBinding
) -> ConnectionState:
    """Build the contract state, turning a contract refusal into a 500-free 409.

    A stored row the contract refuses (an ACTIVE status with no passing report, a
    binding naming a different credential) is a server-side inconsistency, not a bad
    request. It is reported as a conflict carrying no detail rather than surfacing a
    contract message that names internal fields.
    """
    try:
        return service.to_state(connection, binding)
    except ContractViolation:
        logger.error(
            "stored provider connection %s does not satisfy the connection contract",
            connection.id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="stored connection state is inconsistent",
        ) from None


async def _load_or_404(
    db: AsyncSession, org_id: uuid.UUID, connection_id: uuid.UUID
) -> tuple[ProviderConnection, ProviderConnectionBinding]:
    try:
        return await service.load(db, org_id=org_id, connection_id=connection_id)
    except service.ConnectionNotFound:
        # Deliberately not distinguishing "another org's" from "does not exist":
        # either answer would confirm the existence of another tenant's connection.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND
        ) from None


def _require_binding(
    *,
    workspace_id: uuid.UUID,
    state: ConnectionState,
    binding: ProviderConnectionBinding,
) -> None:
    """Refuse a connection bound to a different workspace.

    Runs before the delegation check on every route, so a caller cannot use a
    connection id from a sibling workspace to learn whether they own its credential.
    """
    decision = service.check_binding(
        workspace_id=str(workspace_id), state=state, binding=binding
    )
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_NOT_BOUND)


async def _authorize(
    *,
    request: Request,
    workspace_id: uuid.UUID,
    connection: ProviderConnection,
    binding: ProviderConnectionBinding,
    org_id: uuid.UUID,
    require_renewal: bool,
) -> VerifiedCredentialEvidence:
    """Run BOTH independent checks, then the lifecycle term the operation needs.

    The two authorization checks answer different questions and neither implies the
    other: owning a credential does not make it usable in a workspace, and a workspace
    holding a binding does not make its members able to delegate. Both are required,
    and the binding check runs against the server's independently loaded row rather
    than against anything the caller sent.

    The lifecycle term is separate, asked with the contract method that fits the
    operation, and is the parameter rather than a constant because the contract draws
    the distinction deliberately: ``allows_renewal()`` is true for PENDING and false
    for DISABLED, while ``admits_new_work()`` is true only for ACTIVE. Guarding
    rotation with the admission gate — which the first version of this router did —
    would refuse to rotate a PENDING connection, stranding exactly the case rotation
    exists for: one registered against a credential that never validates, which
    therefore cannot be activated, and whose only remaining transition would be
    disablement.

    ``require_renewal=False`` is for an operation a disabled connection still permits:
    disablement itself, so ``DELETE`` stays idempotent rather than 409ing on a second
    call.
    """
    state = _state(connection, binding)
    _require_binding(workspace_id=workspace_id, state=state, binding=binding)

    evidence = await _vault_evidence(
        request, org_id, workspace_id, service.to_reference(connection)
    )

    # Disablement is reported as disablement, not as a binding failure. Said out loud
    # only here, after both checks have passed: a caller who has proved org scope, the
    # exact workspace binding AND ownership of the credential is not the caller the
    # contract's single-refusal-string rule protects against, and telling them their
    # credential belongs to another workspace would be plainly false — sending them to
    # look in the wrong place instead of at the disablement they can reverse.
    if require_renewal and not state.allows_renewal():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DISABLED)
    return evidence


def _reference_or_400(payload: Any, *, field: str = "payload") -> CredentialReference:
    """Accept a reference, refusing a payload that carries a value (acceptance 1).

    The contract checks the whole mapping before reading a field, so a stray secret
    beside a well-formed reference fails the request instead of being dropped.

    The refusal message is the contract's, which is written not to echo the offending
    value: ``assert_no_secret_material`` names the *key* that carried secret material,
    and the ARN rule reports that the value must not be an ARN without reproducing it.
    Returning a 400 built here rather than letting a Pydantic validator raise keeps
    FastAPI's ``input``-echoing 422 handler off this path entirely.
    """
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be a JSON object",
        )
    try:
        reference = accept_connection_request(payload)
        if any(
            len(value) > maximum
            for value, maximum in (
                (reference.credential_id, 255),
                (reference.service, 100),
                (reference.label, 255),
            )
        ):
            raise ContractViolation("credential reference fields exceed storage limits")
        return reference
    except ContractViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None


def _report_or_400(payload: Any, *, field: str) -> ValidationReport:
    """Build a validation report from a submitted reading set.

    ``observed_capacity`` is read with a sentinel rather than ``payload.get(...)`` so
    an omitted key stays ``None`` — "not measured" — and an explicit ``0`` stays ``0``.
    Collapsing those two is the conflation acceptance 3 exists to prevent, and it
    would happen here at the wire boundary if omission and zero arrived the same way.

    Caller readings are untrusted until the vault integration independently
    verifies an attestation of their exact digest and credential/workspace binding.
    """
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be a JSON object",
        )
    capacity = payload.get("observed_capacity", None)
    if capacity is not None and not isinstance(capacity, int):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="observed_capacity must be an integer or omitted",
        )
    if isinstance(capacity, bool):
        # `bool` is an `int` in Python; the contract's report would accept True as a
        # capacity of 1. Refused so a truthy flag cannot masquerade as a measurement.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="observed_capacity must be an integer or omitted",
        )
    if capacity is not None and not 0 <= capacity <= 2147483647:
        raise HTTPException(
            status_code=400, detail="observed_capacity is outside the supported range"
        )
    detail = payload.get("detail", "")
    if not isinstance(detail, str) or len(detail) > 1024:
        raise HTTPException(
            status_code=400,
            detail="validation detail must be text of at most 1024 characters",
        )
    try:
        return ValidationReport(
            credential_valid=payload.get("credential_valid", False),
            permissions_sufficient=payload.get("permissions_sufficient", False),
            quota_available=payload.get("quota_available", False),
            observed_capacity=capacity,
            checked_at=datetime.now(timezone.utc),
            detail=detail,
        )
    except ContractViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None


def _check_request_material(value):
    if isinstance(value, dict):
        for key, item in value.items():
            # This protocol field is a boolean reading, not a credential value.
            # Its value still undergoes the same secret scan and strict type check.
            if key != "credential_valid":
                assert_no_secret_material({key: None}, what="connection request")
            _check_request_material(item)
    elif isinstance(value, list):
        for item in value:
            _check_request_material(item)
    else:
        assert_no_secret_material(value, what="connection request")


async def _body(request: Request) -> Any:
    """The raw parsed JSON body.

    Raw rather than a typed model so the contract's whole-payload secret check sees
    every key the caller sent, including ones no field of ours declares.
    """
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            raise HTTPException(
                status_code=413, detail="provider connection request is too large"
            )
    try:
        payload = json.loads(raw)
        _check_request_material(payload)
        return payload
    except ContractViolation as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except (ValueError, RecursionError):
        raise HTTPException(
            status_code=400,
            detail="body must be valid JSON",
        ) from None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(_CONNECTIONS, status_code=status.HTTP_201_CREATED)
async def register_connection(
    request: Request,
    workspace_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Bind an ADP credential reference to this workspace.

    Refuses a payload carrying credential material, requires the workspace to belong
    to the caller's organization, and requires the reference to already exist in that
    organization's credential registry — server-held evidence the caller did not
    write, so an arbitrary credential id cannot be bound.

    Created PENDING: an unvalidated reference admits no work.
    """
    payload = await _body(request)
    reference = _reference_or_400(payload)
    operation_id = None
    if "operation_id" in payload:
        try:
            operation_id = uuid.UUID(payload["operation_id"])
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(400, "operation_id must be a UUID") from None

    if RENEW_CREDENTIAL_PERMISSION not in _permissions(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)

    if not await service.workspace_in_org(db, org_id=org_id, workspace_id=workspace_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
        )

    provider = payload.get("provider")
    if not isinstance(provider, str) or not provider.strip() or len(provider) > 50:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="provider is required"
        )

    await _registry_reference(db, org_id, reference, provider)
    evidence = await _vault_evidence(request, org_id, workspace_id, reference)
    registration = {
        "org_id": org_id,
        "workspace_id": workspace_id,
        "reference": reference,
        "provider": provider,
        "owner_principal": evidence.ownership.owner_principal,
        "bound_by": _caller_principal(request, org_id),
        "verify_authority": lambda: _mutation_authority(
            db, request, org_id, workspace_id, provider, evidence
        ),
    }
    try:
        try:
            connection, binding = await service.register(
                db, operation_id=operation_id, **registration
            )
        except IntegrityError:
            await db.rollback()
            # Concurrent exact registration may have committed while our insert
            # waited. Recheck current authority and the entire immutable binding.
            recovered = (
                await service.replay_registration(
                    db, operation_id=operation_id, **registration
                )
                if operation_id is not None
                else None
            )
            if recovered is None:
                raise service.RegistrationConflict() from None
            connection, binding = recovered
    except service.RegistrationConflict:
        await db.rollback()
        raise HTTPException(
            409,
            "registration identity conflicts with an existing connection or binding",
        ) from None
    # The service verified authority before and after flush. A successful commit
    # is durable; later expiry cannot turn its outcome into a refusal.
    return connection_response(_state(connection, binding))


@router.get(_CONNECTIONS + "/{connection_id}")
async def get_connection(
    request: Request,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Read a connection, including its four separate validation readings.

    The binding half is enforced here too: a connection bound to another workspace is
    refused rather than returned, so this read cannot be used to enumerate a sibling
    workspace's connections.
    """
    _caller_principal(request, org_id)
    if "workspace:read" not in _permissions(request):
        raise HTTPException(status_code=403, detail="workspace read grant is required")
    connection, binding = await _load_or_404(db, org_id, connection_id)
    state = _state(connection, binding)
    _require_binding(workspace_id=workspace_id, state=state, binding=binding)
    return {
        **connection_response(state),
        "revision": connection_revision(connection, binding),
    }


@router.post(_CONNECTIONS + "/{connection_id}/validation")
async def record_validation(
    request: Request,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
    expected_revision: Annotated[str | None, Query(pattern=r"^[a-f0-9]{64}$")] = None,
) -> dict[str, Any]:
    """Record a validation reading and activate the connection if it passed.

    Reports four readings separately and computes no aggregate. Activation goes
    through the contract, which refuses a report that did not establish a valid,
    permitted, in-quota credential — so ACTIVE cannot be reached by asserting it.

    Validating a DISABLED connection is refused: ``activate`` raises on one, and
    recording a fresh passing reading against a connection that cannot use it would
    leave storage asserting a healthy credential on a connection admitting nothing.
    """
    connection, binding = await _load_or_404(db, org_id, connection_id)
    # Authorized before the body is parsed, so an unauthorized caller's payload is
    # never examined — and the state is built first, so a stored row the contract
    # refuses is a 409 rather than a partially-applied update.
    evidence = await _authorize(
        request=request,
        workspace_id=workspace_id,
        connection=connection,
        binding=binding,
        org_id=org_id,
        require_renewal=True,
    )

    require_connection_revision(expected_revision, connection, binding)

    report = _report_or_400(await _body(request), field="validation")
    evidence = await _vault_evidence(
        request, org_id, workspace_id, service.to_reference(connection), report
    )
    report = replace(report, checked_at=evidence.report_checked_at)
    if not report.validated:
        # A failing report is recorded, not discarded, and the connection stays where
        # it is. Returning the four readings lets the caller see WHICH reading failed;
        # calling `activate` would raise instead, which would lose the readings.
        await service.record_failed_validation(
            db,
            connection=connection,
            report=report,
            verify_authority=lambda: _mutation_authority(
                db, request, org_id, workspace_id, connection.provider, evidence
            ),
        )
        return {
            "connection_id": str(connection.id),
            "status": connection.status,
            "validation": validation_response(report),
        }

    try:
        activated = await service.record_activation(
            db,
            connection=connection,
            binding=binding,
            report=report,
            verify_authority=lambda: _mutation_authority(
                db, request, org_id, workspace_id, connection.provider, evidence
            ),
        )
    except ContractViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from None
    return connection_response(activated)


@router.post(_CONNECTIONS + "/{connection_id}/rotation")
async def rotate_connection(
    request: Request,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
    expected_revision: Annotated[str | None, Query(pattern=r"^[a-f0-9]{64}$")] = None,
) -> dict[str, Any]:
    """Switch onto an already-validated replacement, atomically (acceptance 5).

    The replacement's reference and its validation reading are both required: the
    contract's ``rotate`` cannot be called without a passing report, so there is no
    way to rotate onto an unchecked credential.

    The superseded reference is **reported, not deleted**. Revoking it is a separate,
    deliberate step once traffic is confirmed on the replacement; a sequence that
    deleted first would leave the connection dead for the width of that window.

    A PENDING connection may be rotated (``allows_renewal()``, not
    ``admits_new_work()``): a connection registered against a credential that never
    validated is exactly the one whose reference needs replacing, and gating this on
    ACTIVE would leave disablement as its only remaining transition. A DISABLED one may
    not — acceptance 5 is that disablement blocks renewals.
    """
    connection, binding = await _load_or_404(db, org_id, connection_id)
    evidence = await _authorize(
        request=request,
        workspace_id=workspace_id,
        connection=connection,
        binding=binding,
        org_id=org_id,
        require_renewal=True,
    )

    require_connection_revision(expected_revision, connection, binding)

    payload = await _body(request)
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="body must be a JSON object"
        )
    replacement = _reference_or_400(payload.get("replacement"), field="replacement")
    report = _report_or_400(payload.get("validation"), field="validation")

    await _registry_reference(
        db, org_id, replacement, connection.provider, what="replacement credential"
    )

    replacement_evidence = await _vault_evidence(
        request, org_id, workspace_id, replacement, report
    )
    report = replace(report, checked_at=replacement_evidence.report_checked_at)
    try:
        result = await service.record_rotation(
            db,
            connection=connection,
            binding=binding,
            replacement=replacement,
            replacement_validation=report,
            rotated_at=datetime.now(timezone.utc),
            rotated_by=_caller_principal(request, org_id),
            verify_authority=lambda: _mutation_authority(
                db,
                request,
                org_id,
                workspace_id,
                connection.provider,
                evidence,
                replacement_evidence,
            ),
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="replacement credential already has a connection or binding",
        ) from None
    except ContractViolation as exc:
        # The contract refuses an unvalidated replacement and a rotation onto the
        # same credential. Both are caller errors about the submitted replacement,
        # and the message names neither a secret nor an ARN.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None

    body = connection_response(result.connection)
    body["superseded_credential"] = {
        "credential_id": result.superseded_reference.credential_id,
        "service": result.superseded_reference.service,
        "label": result.superseded_reference.label,
        # Stated in the response because the operator's next action depends on it:
        # the old credential is still live and must be revoked at the vault.
        "still_registered": result.old_credential_still_registered,
        "next_step": (
            "The superseded credential remains registered and usable. Revoke it at "
            "the vault once traffic is confirmed on the replacement."
        ),
    }
    return body


@router.delete(_CONNECTIONS + "/{connection_id}")
async def disable_connection(
    request: Request,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
    expected_revision: Annotated[str | None, Query(pattern=r"^[a-f0-9]{64}$")] = None,
) -> dict[str, Any]:
    """Block new admissions and renewals, and say what that does not accomplish.

    The response carries the contract's disablement limitation: credentials already
    delivered to running workloads may remain usable until they are revoked at the
    provider. An operator who reads "disabled" as "revoked" skips the provider-side
    revocation that actually contains the credential, so the limitation is in the
    body rather than only in a log or a docstring.

    ``require_renewal=False``: disabling an already-disabled connection is allowed and
    returns the same body. Refusing the second call would make containment depend on
    the caller knowing the current status, and an operator retrying because they are
    unsure whether the first attempt landed would get an error that reads like a
    failure to disable.
    """
    connection, binding = await _load_or_404(db, org_id, connection_id)
    evidence = await _authorize(
        request=request,
        workspace_id=workspace_id,
        connection=connection,
        binding=binding,
        org_id=org_id,
        require_renewal=False,
    )

    require_connection_revision(expected_revision, connection, binding)

    state = await service.record_disablement(
        db,
        connection=connection,
        binding=binding,
        verify_authority=lambda: _mutation_authority(
            db, request, org_id, workspace_id, connection.provider, evidence
        ),
    )
    body = connection_response(state)
    # `connection_response` already emits `limitation`; asserted here so a future
    # change to the allowlist cannot silently drop the one field acceptance 5 is about.
    if not body.get("limitation"):
        logger.error("disable response for %s carried no limitation", connection.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="disablement limitation could not be surfaced",
        )
    return body
