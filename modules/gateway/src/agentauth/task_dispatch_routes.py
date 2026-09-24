"""Protected dispatch and recovery adapters: claim, settle, sweep.

These four routes are the only way the publication protocol advances. They are
internal adapters, not public API: the public Task API (T2) accepts a task and
returns 202; everything here runs on behalf of the platform's own publisher and
scheduled reconciler.

## Why recovery has its own allowlist

The publisher and the reconciler are both "producers", but they are not the same
principal and must not be interchangeable. Recovery is the more powerful of the
two -- it can discover any tenant's outstanding work in a shard and lease it,
which is exactly the capability a compromised publisher should not gain. So the
recovery routes check a separate role allowlist
(``ADP_TASK_RECOVERY_PRODUCER_ROLES``) rather than the dispatch one, and a caller
holding only the publisher role is refused with 403 even with a valid STS proof.

That is the gateway half of the recovery-authentication requirement. The other
half lives in the Lambda: it verifies the alias it was actually invoked through,
so a request *body* claiming to be recovery is never sufficient. Neither half is
load-bearing alone -- the body never selects authority, and the identity is
checked twice by two mechanisms that fail independently.

## Why the shard is validated against a fixed pattern

``shard`` reaches a DynamoDB partition key. It is constrained to the contract's
16 literal values (``v1#00``..``v1#15``) by pattern, so a caller cannot use it to
address a partition outside the recovery namespace or to probe for one. The same
reasoning applies to ``limit``: bounded at the contract's 100 so one request
cannot consume the whole invocation budget.

## What these routes deliberately do not do

They do not accept a tenant, a task ID, an envelope, a persona or a model from
the request body. A claim returns the envelope that was committed at acceptance;
the caller publishes that and nothing else. If the body could contribute to the
envelope, the publisher could rewrite the task it was asked to dispatch, and the
digest committed at acceptance would be the only thing standing between that and
an unauthorized model call.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.routes import AgentRuntime, get_agent_runtime
from src.agentauth.task_work import (
    DISPATCH_SORT_PREFIX,
    MAX_WORK_RECORDS_PER_INVOCATION,
    TaskWorkError,
    TaskWorkStore,
    TaskWorkUnavailableError,
)
from src.agentauth.work_routes import verify_producer

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"

# Rollout flags, both default-off (design section 11). Admission gates the
# publish path; recovery gates the sweep. Separate because recovery must be
# enableable for reconciliation while admission stays closed.
ADMISSION_FLAG = "ADP_TASK_API_ADMISSION_ENABLED"
RECOVERY_FLAG = "ADP_TASK_API_RECOVERY_ENABLED"

DISPATCH_ROLES_ENV = "ADP_TASK_DISPATCH_PRODUCER_ROLES"
RECOVERY_ROLES_ENV = "ADP_TASK_RECOVERY_PRODUCER_ROLES"

UUID4 = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
SHARD_PATTERN = r"^v1#(0[0-9]|1[0-5])$"
TASK_ID_PATTERN = r"^tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


def _flag(env, name: str) -> bool:
    """Default-off: anything but an explicit "true" leaves the path closed.

    A missing or misspelled value must not enable a path that can spend money.
    """
    return str(env.get(name, "")).strip().lower() == "true"


def _roles(env, name: str) -> set[str]:
    return {role for role in (r.strip() for r in env.get(name, "").split(",")) if role}


class DispatchClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    dispatch_id: str = Field(pattern=UUID4)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    producer_proof: str = Field(min_length=1, max_length=12000)


class DispatchSettleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    dispatch_id: str = Field(pattern=UUID4)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    lease_token: str = Field(min_length=1, max_length=256)
    publication_outcome: str = Field(pattern=r"^(confirmed|unknown|failed)$")
    sqs_message_id: str | None = Field(default=None, max_length=256)
    producer_proof: str = Field(min_length=1, max_length=12000)


class RecoveryClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    shard: str = Field(pattern=SHARD_PATTERN)
    cursor: str | None = Field(default=None, max_length=4096)
    limit: int = Field(ge=1, le=MAX_WORK_RECORDS_PER_INVOCATION)
    producer_proof: str = Field(min_length=1, max_length=12000)


def require_dispatch_enabled(runtime: AgentRuntime = Depends(get_agent_runtime)) -> None:
    """Gate every route on a rollout flag being explicitly on.

    A separate dependency from ``work_store`` on purpose: the flag check is the
    thing that must not be bypassable, and keeping it out of the store
    constructor means a test that substitutes the store still runs this.
    """
    env = os.environ if runtime.env is None else runtime.env
    if not _flag(env, ADMISSION_FLAG) and not _flag(env, RECOVERY_FLAG):
        raise HTTPException(503, "task dispatch unavailable")


def work_store(runtime: AgentRuntime = Depends(get_agent_runtime)) -> TaskWorkStore:
    env = os.environ if runtime.env is None else runtime.env
    return TaskWorkStore(
        dynamodb_client=runtime.store.client,
        table_name=env.get("WEBHOOK_EVENTS_TABLE") or None,
    )


# Declared after its dependency so the flag gate applies to every route below,
# including any added later -- a per-route decorator would be easy to forget.
router = APIRouter(
    prefix="/internal/v1/agent/task-dispatch",
    tags=["task-dispatch"],
    dependencies=[Depends(require_dispatch_enabled)],
)


def _refusal(exc: TaskWorkError) -> HTTPException:
    """Map a refusal to a status the caller can act on without leaking state.

    409 for "someone else owns this or it is already done" -- the caller should
    move on, not retry. 429 for a spent try budget, which IS retryable later.
    404 for absent work. The distinction matters because the sweep uses it to
    decide whether to look at this record again.
    """
    if exc.code in ("not_found",):
        return HTTPException(404, "not found")
    if exc.code == "throttled":
        return HTTPException(429, "publication try budget spent")
    if exc.code == "exhausted":
        return HTTPException(409, "recovery exhausted")
    return HTTPException(409, exc.code)


async def _authenticate(
    *,
    proof: str,
    identity: str,
    roles_env: str,
    runtime: AgentRuntime,
) -> str:
    """Verify the caller's STS proof against the allowlist for THIS route.

    `identity` is the value bound into the signed proof, so a proof captured for
    one dispatch cannot be replayed to claim another.
    """
    env = os.environ if runtime.env is None else runtime.env
    allowed = _roles(env, roles_env)
    if not allowed:
        # No configured principal means the path is not deployed. Refusing is the
        # only safe reading; an empty allowlist must never mean "anyone".
        raise HTTPException(503, "task dispatch unavailable")
    return await verify_producer(proof, identity, allowed_roles=allowed)


@router.post("/claim")
async def dispatch_claim(
    body: DispatchClaimRequest,
    runtime: AgentRuntime = Depends(get_agent_runtime),
    store: TaskWorkStore = Depends(work_store),
) -> JSONResponse:
    """Lease one committed dispatch and return the envelope to publish.

    The envelope comes from the record committed at acceptance, never from the
    request: the publisher is a courier, not an author.
    """
    await _authenticate(
        proof=body.producer_proof,
        identity=body.dispatch_id,
        roles_env=DISPATCH_ROLES_ENV,
        runtime=runtime,
    )
    sort_key = f"{DISPATCH_SORT_PREFIX}{body.dispatch_id}"
    try:
        claimed = await run_in_threadpool(store.claim, task_id=body.task_id, sort_key=sort_key)
    except TaskWorkError as exc:
        logger.info(
            "task dispatch claim refused task=%s dispatch=%s reason=%s",
            body.task_id,
            body.dispatch_id,
            exc.code,
        )
        raise _refusal(exc) from None
    except TaskWorkUnavailableError:
        raise HTTPException(503, "task dispatch unavailable") from None

    return JSONResponse(
        {
            "schema_version": SCHEMA_VERSION,
            "task_id": body.task_id,
            "dispatch_id": body.dispatch_id,
            "envelope_digest": claimed.get("envelope_digest", {}).get("S", ""),
            "lease_token": claimed["lease_token"]["S"],
            "lease_expires_at": claimed["lease_expires_at"]["S"],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/settle")
async def dispatch_settle(
    body: DispatchSettleRequest,
    runtime: AgentRuntime = Depends(get_agent_runtime),
    store: TaskWorkStore = Depends(work_store),
) -> JSONResponse:
    """Record the publication outcome the publisher actually observed.

    `unknown` is accepted as a normal answer and leaves the work due. The
    contract requires a transport ID with `confirmed`; that is enforced in the
    store so the rule holds for every caller, not just this route.
    """
    await _authenticate(
        proof=body.producer_proof,
        identity=body.dispatch_id,
        roles_env=DISPATCH_ROLES_ENV,
        runtime=runtime,
    )
    try:
        settled = await run_in_threadpool(
            store.settle_publication,
            task_id=body.task_id,
            dispatch_id=body.dispatch_id,
            lease_token=body.lease_token,
            publication_outcome=body.publication_outcome,
            sqs_message_id=body.sqs_message_id,
        )
    except TaskWorkError as exc:
        logger.info(
            "task dispatch settle refused task=%s dispatch=%s reason=%s",
            body.task_id,
            body.dispatch_id,
            exc.code,
        )
        raise _refusal(exc) from None
    except TaskWorkUnavailableError:
        raise HTTPException(503, "task dispatch unavailable") from None

    return JSONResponse(
        {
            "schema_version": SCHEMA_VERSION,
            "dispatch_id": body.dispatch_id,
            "queue_ack_status": settled.get("queue_ack_status", {}).get("S", "pending"),
            "publication_outcome": settled["publication_outcome"]["S"],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/recovery/claim")
async def recovery_claim(
    body: RecoveryClaimRequest,
    runtime: AgentRuntime = Depends(get_agent_runtime),
    store: TaskWorkStore = Depends(work_store),
) -> JSONResponse:
    """Return one bounded page of due work for a shard.

    Authenticated against the RECOVERY allowlist, not the dispatch one: this
    route can enumerate outstanding work across tenants in a shard, so the
    publisher's credential must not reach it.

    Discovery only. Nothing here is claimed or mutated -- the caller claims each
    record individually through /claim, where the version CAS applies. Listing
    and leasing are separate so a sweep that dies mid-page has not leased work it
    will never touch.
    """
    env = os.environ if runtime.env is None else runtime.env
    if not _flag(env, RECOVERY_FLAG):
        raise HTTPException(503, "task recovery unavailable")
    await _authenticate(
        proof=body.producer_proof,
        identity=body.shard,
        roles_env=RECOVERY_ROLES_ENV,
        runtime=runtime,
    )
    try:
        items, next_cursor = await run_in_threadpool(store.due_work, shard=body.shard, cursor=body.cursor, limit=body.limit)
    except TaskWorkError as exc:
        raise _refusal(exc) from None
    except TaskWorkUnavailableError:
        raise HTTPException(503, "task recovery unavailable") from None

    return JSONResponse(
        {
            "schema_version": SCHEMA_VERSION,
            "work": [
                {
                    "work_id": item["work_id"]["S"],
                    "task_id": item["task_id"]["S"],
                    "kind": item["kind"]["S"],
                    "dispatch_id": item.get("dispatch_id", {}).get("S"),
                    "publication_outcome": item.get("publication_outcome", {}).get("S"),
                    "tries": int(item.get("tries", {}).get("N", "0")),
                }
                for item in items
            ],
            "next_cursor": next_cursor,
        },
        headers={"Cache-Control": "no-store"},
    )
