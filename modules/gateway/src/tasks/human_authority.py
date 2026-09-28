"""Explicit human Task ownership; never resolved through service aliases."""

from __future__ import annotations

import os
import uuid

from starlette.concurrency import run_in_threadpool

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.human_control import authorize_human_session, require_live_human_membership
from src.agentauth.task_service_policy import TASK_SCOPES, TaskServicePolicyError, TaskServicePolicyStore
from src.tasks import errors

PREFIX = "human:"


def human_locator(user_id: str) -> str:
    return PREFIX + str(uuid.UUID(user_id))


def principal_owner(locator: str) -> tuple[str, str]:
    if locator.startswith(PREFIX):
        try:
            return "human", str(uuid.UUID(locator[len(PREFIX) :]))
        except ValueError:
            raise errors.disallowed_scope("Invalid human Task owner.") from None
    return "service_account", locator


async def resolve_human(context, db):
    if os.environ.get("ADP_TASK_API_HUMAN_ENABLED", "false").lower() != "true":
        raise errors.disallowed_scope("Human Task admission is not enabled.")
    try:
        session = await authorize_human_session(context, db)
        locator = human_locator(session.user_id)
        policy = await run_in_threadpool(TaskServicePolicyStore().get, tenant_id=session.tenant_id, canonical_principal_id=locator)
    except BootstrapRefusedError:
        raise errors.disallowed_scope("Human Task membership is unavailable.") from None
    except TaskServicePolicyError:
        raise errors.prerequisite_unavailable("Human Task policy is unavailable.") from None
    if not policy or policy.get("status") != "active":
        raise errors.disallowed_scope("This human is not enrolled for Task access.")
    scopes = policy.get("task_scopes")
    if not isinstance(scopes, list) or not scopes or any(scope not in TASK_SCOPES for scope in scopes):
        raise errors.disallowed_scope("Human Task policy has invalid scopes.")
    return locator, session.tenant_id, frozenset("adp-tasks/" + scope for scope in scopes)


async def require_current_owner(db, *, tenant, principal):
    kind, user_id = principal_owner(principal)
    if kind == "human":
        if os.environ.get("ADP_TASK_API_HUMAN_ENABLED", "false").lower() != "true":
            raise errors.disallowed_scope("Human Task access is not enabled.")
        try:
            await require_live_human_membership(db, user_id=user_id, tenant_id=tenant)
        except BootstrapRefusedError:
            raise errors.disallowed_scope("Human Task membership is unavailable.") from None
    return kind, user_id


async def require_admission_headroom(context, max_usd, *, enforcement=None):
    """Reject exhausted human hierarchy before dispatch; paid calls reserve again.

    This is a preflight, not a new budget ledger or a spend reservation. Concurrent
    launch races remain bounded by the existing paid-call reservations and Task
    per-run limits. The Task pilot admission hold remains separate.
    """
    from decimal import Decimal

    from src.budget.enforcement_service import BudgetEnforcementService

    context._budget_enforcement_enabled = True
    verdict = await (enforcement or BudgetEnforcementService()).check_budget_hierarchy(context, Decimal(str(max_usd)), request_id=None)
    if not verdict.allowed:
        raise errors.TaskApiError(402, "budget_exceeded", "Human Task budget headroom is unavailable; no task was dispatched.")
