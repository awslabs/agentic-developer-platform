"""Durable admission and terminal receipts for privileged admin operations.

An intent is committed before authorization dependencies/handler effects. A
terminal receipt is committed before a successful response can leave the route.
An interrupted operation stays visible as unresolved; it is never automatically
retried. This deliberately does not claim a transaction across SQL and providers.
"""

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.organization import Organization, User

logger = logging.getLogger(__name__)
MUTATIONS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
NON_MUTATIONS = frozenset({"create_organization_gone", "preview_policies"})
CALLBACKS = frozenset({"github_install_callback", "github_app_register_callback"})


@dataclass
class Operation:
    action: str
    route: str
    operation_id: str = field(default_factory=lambda: str(uuid4()))
    actor: object = None
    db: object = None
    target_org: str | None = None
    terminal: dict | None = None
    started: bool = False
    effects_started: bool = False
    refusal: dict | None = None


current_operation: ContextVar[Operation | None] = ContextVar("admin_operation", default=None)


def mark_admin_effects():
    """Mark the effect boundary, after local authorization checks, without I/O."""
    operation = current_operation.get()
    if operation is not None:
        operation.effects_started = True


def callback_actor(user, org_id):
    """Called only after the existing nonce/provider authority has verified it."""
    operation = current_operation.get()
    if operation is not None:
        operation.actor = SimpleNamespace(user_id=user.cognito_sub or user.id, org_id=org_id)
        operation.target_org = org_id


def callback_target_org(org_id):
    """Record the target only after the existing service resolves its ownership."""
    operation = current_operation.get()
    if operation is not None:
        operation.target_org = org_id


def callback_result(*, action, target_id, complete=True):
    operation = current_operation.get()
    if operation is not None:
        operation.terminal = {
            "event_type": "admin_" + action,
            "outcome": "success" if complete else "reconciliation_required",
            "target_type": "github_connection",
            "target_id": str(target_id),
            "org_id": operation.target_org,
        }


async def begin_callback(request: Request, db=Depends(get_db)):
    operation = current_operation.get()
    if operation is None or operation.started:
        return
    operation.db = db
    # A redirect carries no browser JWT. Never turn its query parameters into
    # an actor. The existing service installs the verified nonce actor later.
    operation.actor = SimpleNamespace(user_id=None, org_id="platform")
    try:
        await persist(operation, event_type="admin_operation_started", outcome="pending")
    except Exception:
        raise HTTPException(503, detail={"error": "admin_audit_unavailable", "operation_id": operation.operation_id}) from None
    operation.started = True


async def persist(operation, *, event_type, outcome, target_type="route", target_id=None, org_id=None, extra=None):
    """Commit a receipt in a separate session; never commit caller mutations."""
    async with async_sessionmaker(operation.db.bind, expire_on_commit=False)() as audit_db:
        audit_db.add(
            AuditLog(
                org_id=org_id or operation.target_org or operation.actor.org_id,
                event_type=event_type,
                actor_id=operation.actor.user_id,
                details={
                    "operation_id": operation.operation_id,
                    "correlation_id": operation.operation_id,
                    "action": operation.action,
                    "route": operation.route,
                    "target_type": target_type,
                    "target_id": target_id or operation.route,
                    "target_tenant": org_id or operation.target_org,
                    "outcome": outcome,
                    **(extra or {}),
                },
            )
        )
        await audit_db.commit()


async def begin_operation(request: Request, actor=Depends(get_current_user), db=Depends(get_db)):
    operation = current_operation.get()
    if operation is None or operation.started:
        return
    operation.actor, operation.db = actor, db
    # Resolve target standing from stored records, never from an actor/tenant
    # field in the body. A create's target is not yet known; preserve that fact.
    org_id = request.path_params.get("org_id") or request.path_params.get("tenant_id")
    if org_id:
        organization = await db.get(Organization, org_id)
        operation.target_org = organization.id if organization else None
    elif request.path_params.get("user_id"):
        user = await db.get(User, request.path_params["user_id"])
        operation.target_org = user.org_id if user else None
    try:
        await persist(operation, event_type="admin_operation_started", outcome="pending")
    except Exception:
        logger.error("Admin operation admission audit unavailable", extra={"operation_id": operation.operation_id})
        raise HTTPException(503, detail={"error": "admin_audit_unavailable", "operation_id": operation.operation_id}) from None
    operation.started = True


class AuditedAdminRoute(APIRoute):
    """Audit before other dependencies, and finish before sending a response."""

    def __init__(self, *args, **kwargs):
        endpoint = kwargs.get("endpoint")
        self.audit_callback = endpoint.__name__ in CALLBACKS
        self.audit_mutation = self.audit_callback or (bool(set(kwargs.get("methods") or []) & MUTATIONS) and endpoint.__name__ not in NON_MUTATIONS)
        if self.audit_mutation:
            dependencies = list(kwargs.get("dependencies") or [])
            begin = begin_callback if self.audit_callback else begin_operation
            if not any(d.dependency is begin for d in dependencies):
                dependencies.insert(0, Depends(begin))
            kwargs["dependencies"] = dependencies
        super().__init__(*args, **kwargs)

    def get_route_handler(self):
        handler = super().get_route_handler()
        if not self.audit_mutation:
            return handler

        async def audited(request):
            operation = Operation(self.endpoint.__name__, self.path)
            token = current_operation.set(operation)
            try:
                try:
                    response = await handler(request)
                except Exception as exc:
                    if operation.started:
                        await operation.db.rollback()
                        code = getattr(exc, "status_code", 500)
                        refused = code in (401, 403, 404, 409, 422) or (operation.refusal is not None and not operation.effects_started)
                        try:
                            await persist(
                                operation,
                                event_type="admin_operation_refused" if refused else "admin_operation_failed",
                                outcome="denied"
                                if refused and not operation.effects_started
                                else "reconciliation_required",
                                extra={
                                    "status_code": code,
                                    "exception_type": type(exc).__name__,
                                    "effects_may_have_occurred": operation.effects_started,
                                    **(operation.refusal or {}),
                                },
                            )
                        except Exception:
                            # The durable pending intent remains an operator-visible
                            # reconciliation item; preserve the refusal response.
                            logger.error("Admin terminal audit unavailable", extra={"operation_id": operation.operation_id})
                    raise
                if self.audit_callback and operation.started and operation.terminal is None:
                    await operation.db.rollback()
                    operation.terminal = {
                        "event_type": "admin_operation_failed" if operation.effects_started else "admin_operation_refused",
                        "outcome": "reconciliation_required" if operation.effects_started else "denied",
                        "extra": {"callback_response_status": response.status_code, "effects_may_have_occurred": operation.effects_started},
                    }
                if not operation.started or operation.terminal is None:
                    raise HTTPException(503, detail={"error": "admin_audit_incomplete", "operation_id": operation.operation_id})
                try:
                    # Some handlers own their commits; others leave SQL pending.
                    # Complete it before recording success. If either commit is
                    # uncertain, the admitted operation remains unresolved.
                    await operation.db.commit()
                    await persist(operation, **operation.terminal)
                except Exception:
                    await operation.db.rollback()
                    logger.error("Admin completion requires reconciliation", extra={"operation_id": operation.operation_id})
                    raise HTTPException(
                        503, detail={"error": "admin_audit_reconciliation_required", "operation_id": operation.operation_id}
                    ) from None
                response.headers["X-Admin-Operation-Id"] = operation.operation_id
                return response
            finally:
                current_operation.reset(token)

        return audited
