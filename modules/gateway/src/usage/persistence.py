"""Retry a receipted usage transaction without repeating provider inference."""

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict

from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

logger = logging.getLogger(__name__)
_RETRY_DELAYS = (0.1, 0.3, 0.9)
_TRANSIENT_SQLSTATES = {"40001", "40P01", "55P03", "57014", "57P01", "57P02", "57P03", "53300"}


def usage_recovery_record(context, agent_run_id: str | None, status_code: int) -> dict:
    """Capture only verified attribution; callers must first validate provider usage."""
    from src.usage.service import UsageService

    scope = context._project_attribution
    if scope and (scope.tenant_id != context.attributed_org_id or (agent_run_id and scope.run_id != agent_run_id)):
        raise ValueError("Project usage attribution does not match the request")

    return {
        "version": 1,
        "agent_run_id": agent_run_id or (context._project_attribution.run_id if context._project_attribution else None),
        "graph_address": UsageService._graph_address_for(context, agent_run_id),
        "status_code": status_code,
        **({"project_attribution": asdict(context._project_attribution)} if context._project_attribution else {}),
        "reservation_scope_keys": [
            target.key()
            for target in (context._budget_admission_targets or context._run_scope_reservations)
            if target.require_initialization or target.entity_type in {"run", "chain"}
        ],
    }


def usage_write_error_details(error: Exception) -> dict[str, str | bool | None]:
    """Expose driver codes, never SQL text, bound parameters or exception text."""
    code = None
    for candidate in (getattr(error, "orig", None), error.__cause__, error):
        value = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if isinstance(value, str) and re.fullmatch(r"[A-Z0-9]{5}", value):
            code = value
            break
    return {
        "error_type": type(error).__name__,
        "sqlstate": code,
        "connection_invalidated": bool(getattr(error, "connection_invalidated", False)),
    }


def _transient(error: Exception) -> bool:
    if isinstance(error, PoolTimeoutError | TimeoutError | ConnectionError | OSError):
        return True
    if not isinstance(error, DBAPIError):
        return False
    code = usage_write_error_details(error)["sqlstate"]
    # asyncpg invalidates cached statements after a concurrent schema change.
    # Only retry that specific 0A000 case, not arbitrary unsupported SQL.
    stale_plan = any(type(candidate).__name__ == "InvalidCachedStatementError" for candidate in (getattr(error, "orig", None), error.__cause__))
    return bool(error.connection_invalidated or stale_plan or code in _TRANSIENT_SQLSTATES or (code and code.startswith("08")))


async def persist_usage_with_retry(write: Callable[[], Awaitable[None]], *, request_id: str | None, retry_safe: bool) -> None:
    """Each attempt opens its own session; the receipt makes commit ambiguity safe.

    ``write`` must persist the same server request and pricing decision on every
    attempt. Unreceipted diagnostic rows are never retried. Persistent failure
    propagates to the caller, which retains the reservation and recovery evidence.
    The caller's finalizer deadline and cancellation still bound the whole write.
    """
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            await write()
            return
        except Exception as error:
            if not retry_safe or not request_id or attempt == len(_RETRY_DELAYS) or not _transient(error):
                raise
            logger.warning(
                "Retrying usage settlement transaction",
                extra={"request_id": request_id, "attempt": attempt + 1, **usage_write_error_details(error)},
            )
            await asyncio.sleep(_RETRY_DELAYS[attempt] * random.uniform(0.8, 1.2))
