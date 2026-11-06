"""Explicit owner renewal for an expired DevOps setup that never started."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, Response
from l5_services.api.bindings import _intent_hash
from l5_services.api.configuration import view as configuration_view
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.setup import BindingOperation, SetupAuthorityContinuation, SetupIntent, SetupResourceReceipt, SetupWork
from l5_services.setup_authority import continuation_error, continuation_rows, digest, setup_authorization
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from src.auth.aws_connection_authority import owned_aws_connection
from src.auth.l5_devops_setup_scope import current_devops_setup_scope
from src.auth.l5_owner_delegation import _aware, _owner, delegation_scope
from src.shared.models.vault import CredentialServiceDelegation


class ContinueSetup(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["continue_unstarted_setup"]
    intent_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    delegation_id: str = Field(min_length=1, max_length=36)
    delegation_revision: int = Field(ge=1)
    scope_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=1000)


def view(row):
    return {**row.receipt, "receipt_hash": row.receipt_hash, "active_until": row.receipt["expires_at"], "continuation_provider_effects": 0}


async def selected_intent(backend, product_id, project_id, operation_id):
    configuration, _ = await _owner(backend, product_id, project_id, "devops")
    intent = await backend.db.get(SetupIntent, (backend.actor.org_id, operation_id), populate_existing=True)
    if (
        intent is None
        or intent.configuration_id != configuration.configuration_id
        or (intent.product_id, intent.project_id, intent.actor_id, intent.service_id) != (product_id, project_id, backend.actor.user_id, "devops")
    ):
        raise continuation_error("not_found", "Setup operation not found.", 404)
    return configuration, intent


async def continue_setup(backend, product_id, project_id, operation_id, body, key, etag):
    db = backend.db
    configuration, intent = await selected_intent(backend, product_id, project_id, operation_id)
    # Lock order: configuration -> original work -> operation -> grants (sorted)
    # -> credential. The worker claims and commits the same work row before any
    # authorization/provider step. A racing worker therefore wins or is observed
    # as started; no continuation can cross an already-claimed work boundary.
    await db.scalar(
        select(Configuration)
        .where(
            Configuration.tenant_id == intent.tenant_id,
            Configuration.configuration_id == intent.configuration_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    works = (
        await db.scalars(
            select(SetupWork)
            .where(
                SetupWork.tenant_id == intent.tenant_id,
                SetupWork.operation_id == operation_id,
            )
            .order_by(SetupWork.generation)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    operation = await db.scalar(
        select(BindingOperation)
        .where(
            BindingOperation.tenant_id == intent.tenant_id,
            BindingOperation.operation_id == operation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    await db.refresh(intent)
    rows = await continuation_rows(db, intent)
    request_hash = digest({"body": body.model_dump(), "operation_etag": etag})
    prior = next((row for row in rows if row.idempotency_key == key), None)
    if prior is not None:
        if prior.request_hash != request_hash:
            raise continuation_error("idempotency_conflict", "This continuation key already records a different request. Reuse its original payload.")
        await setup_authorization(db, intent, require_active=False)
        return prior, False
    if etag is None:
        raise continuation_error("precondition_required", "Supply the operation revision in If-Match.", 428)
    if operation is None or etag != f'"{operation.revision}"':
        raise continuation_error("stale_revision", "Read the operation again before continuing its consent.", 412)
    version = await db.get(ConfigurationVersion, (intent.tenant_id, intent.configuration_id, configuration.current_revision), populate_existing=True)
    from src.auth.l5_devops_setup_scope import setup_target

    target = setup_target(version.settings) if version else None
    expected = {"action": target["setup_action"], "configuration_revision": configuration.current_revision} if target else None
    if target and target["setup_action"] == "link":
        expected["native_resource_ref"] = target["native_resource_ref"]
    if (
        intent.request != expected
        or version is None
        or intent.scope != configuration_view(configuration, version)["scope"]
        or intent.binding_generation != 1
        or intent.authorization.get("purpose") != "devops_setup"
        or body.intent_hash != intent.intent_hash
        or intent.intent_hash != _intent_hash(intent)
    ):
        raise continuation_error(
            "setup_continuation_scope_changed", "Only the unchanged retained DevOps create or exact-space link request can be continued.", 412
        )
    if (
        operation.state != "accepted"
        or operation.action != expected["action"]
        or operation.attempts != 0
        or operation.reconcile_checks != 0
        or any(
            getattr(operation, field) is not None
            for field in ("binding_id", "native_resource_ref", "native_candidate_id", "creation_receipt", "cleanup_receipt", "readback_ref")
        )
        or len(works) != 1
        or works[0].generation != 1
        or works[0].fence != 0
        or any(getattr(works[0], field) is not None for field in ("lease_owner", "lease_until", "completed_at", "blocked_at"))
        or await db.scalar(
            select(Binding.binding_id).where(Binding.tenant_id == intent.tenant_id, Binding.configuration_id == intent.configuration_id).limit(1)
        )
        or await db.scalar(
            select(SetupResourceReceipt.operation_id)
            .where(SetupResourceReceipt.tenant_id == intent.tenant_id, SetupResourceReceipt.operation_id == operation_id)
            .limit(1)
        )
    ):
        raise continuation_error(
            "setup_already_started",
            "This setup has started or has native evidence. Reconcile its existing outcome; consent continuation cannot restart it.",
        )
    authority = await setup_authorization(db, intent, require_active=False)
    ids = sorted({authority["delegation_id"], body.delegation_id})
    grants = (
        await db.scalars(
            select(CredentialServiceDelegation)
            .where(
                CredentialServiceDelegation.id.in_(ids),
            )
            .order_by(CredentialServiceDelegation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    previous = next((grant for grant in grants if grant.id == authority["delegation_id"]), None)
    renewed = next((grant for grant in grants if grant.id == body.delegation_id), None)
    now = datetime.now(UTC)
    if previous is None or previous.revoked_at is not None or previous.revision != authority["delegation_revision"]:
        raise continuation_error(
            "setup_prior_consent_revoked", "Revoked or changed consent cannot be continued. Inspect the retained operation and its authority.", 403
        )
    if _aware(previous.expires_at) > now:
        raise continuation_error("setup_consent_still_active", "The current setup consent is still active. Reuse the retained operation.")
    if (
        renewed is None
        or renewed.id == previous.id
        or renewed.revision != body.delegation_revision
        or renewed.revoked_at is not None
        or _aware(renewed.expires_at) <= now + timedelta(minutes=1)
    ):
        raise continuation_error(
            "setup_fresh_consent_required",
            "Review and grant fresh setup consent with at least one minute remaining, then explicitly continue this operation.",
            403,
        )
    columns = (
        "tenant_id",
        "product_id",
        "project_id",
        "created_by",
        "service_id",
        "credential_id",
        "installation_id",
        "binding_generation",
        "operations",
        "capabilities",
        "sources",
        "visibility",
        "execution_scope",
    )
    if any(getattr(previous, field) != getattr(renewed, field) for field in columns):
        raise continuation_error(
            "setup_continuation_scope_changed",
            "Fresh consent must match the original setup scope exactly; no source, credential, owner or configuration changes are allowed.",
            412,
        )
    await owned_aws_connection(db, renewed.credential_id, backend.actor.user_id, backend.actor.org_id, lock=True)
    preview = await delegation_scope(backend, product_id, project_id, "devops", for_control=True, control_action="devops_setup")
    if (
        preview["scope"] != renewed.execution_scope
        or preview["scope_digest"] != body.scope_digest
        or not await current_devops_setup_scope(db, renewed)
    ):
        raise continuation_error(
            "setup_continuation_scope_changed",
            "The owner, configuration or credential revision changed. Read the current scope before taking another action.",
            412,
        )
    work = works[0]
    authorization = {**intent.authorization, "delegation_id": renewed.id, "delegation_revision": renewed.revision, "authorized_at": now.isoformat()}
    receipt = {
        "schema_version": 1,
        "sequence": len(rows) + 1,
        "operation_id": operation_id,
        "intent_hash": intent.intent_hash,
        "work_id": work.work_id,
        "work_generation": work.generation,
        "work_fence": work.fence,
        "prior_authorization": authority,
        "authorization": authorization,
        "scope": preview["scope"],
        "scope_digest": preview["scope_digest"],
        "expires_at": _aware(renewed.expires_at).isoformat(),
        "reason": body.reason,
        "previous_receipt_hash": rows[-1].receipt_hash if rows else None,
    }
    row = SetupAuthorityContinuation(
        tenant_id=intent.tenant_id,
        operation_id=operation_id,
        sequence=len(rows) + 1,
        idempotency_key=key,
        request_hash=request_hash,
        receipt=receipt,
        receipt_hash=digest(receipt),
    )
    db.add(row)
    await db.commit()
    return row, True


def create_router(backend_dependency):
    router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}", tags=["l5-devops"])

    @router.get("/binding-operations/{operation_id}/authority-continuations")
    async def history(product_id: str, project_id: str, service_id: str, operation_id: str, response: Response, backend=Depends(backend_dependency)):
        if service_id != "devops":
            raise continuation_error("not_found", "Setup continuation not found.", 404)
        _, intent = await selected_intent(backend, product_id, project_id, operation_id)
        if intent.intent_hash != _intent_hash(intent):
            raise continuation_error()
        await setup_authorization(backend.db, intent, require_active=False)
        response.headers["Cache-Control"] = "no-store"
        return {
            "operation_id": operation_id,
            "intent_hash": intent.intent_hash,
            "original_request": intent.request,
            "original_idempotency_key": intent.idempotency_key,
            "items": [view(row) for row in await continuation_rows(backend.db, intent)],
        }

    @router.post("/binding-operations/{operation_id}/authority-continuations")
    async def continue_authority(
        product_id: str,
        project_id: str,
        service_id: str,
        operation_id: str,
        body: ContinueSetup,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        if_match: str | None = Header(default=None),
        backend=Depends(backend_dependency),
    ):
        if service_id != "devops":
            raise continuation_error("not_found", "Setup continuation not found.", 404)
        row, created = await continue_setup(backend, product_id, project_id, operation_id, body, idempotency_key, if_match)
        response.status_code = 201 if created else 200
        response.headers.update({"Cache-Control": "no-store", "ETag": '"' + row.receipt_hash + '"'})
        return view(row)

    return router
