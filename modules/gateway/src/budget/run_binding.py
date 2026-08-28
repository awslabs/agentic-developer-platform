"""Server-side run identity for the per-run / per-chain spend cap (Issue #4187).

## Why this exists — the cap key cannot come from the client

``X-Agent-RunId`` is a request header (``src/proxy/routes.py``), written by the
worker's sigv4 proxy and validated by nothing. A cap keyed on it is not a cap: a
caller that rotates the value every N calls mints a fresh, unspent ledger every N
calls, and a caller that omits it entirely has no ledger at all.

That is not a hypothetical — it is the **exact** defect Issue #3985 removed for
``X-Agent-BudgetConfigId``, whose removal note still sits in
``enforcement_middleware.py``:

    Re-adding per-agent budget enforcement requires resolving the config id from
    the agent registry entry (server-side, keyed off the authenticated identity),
    not from a request header.

So this module treats the header as an *assertion to be verified*, never as an
identity. The run id is looked up in the ``webhook-events`` registry table — the
row written at ingress, before any agent code ran — and the row's own
``user_id`` / ``root_human_id`` / ``tenant_id`` must agree with the authenticated
``TokenContext``. The ``correlation_id`` for the chain scope is read from that
same row, so the chain ledger is equally unforgeable (the worker does send
``x-agent-correlationid``, but nothing here reads it).

## Fail closed on IDENTITY, not just on spend

Issue #4075 made the check fail closed when *spend* is unknown. This adds the
other half: when the run's *identity* is unknown — no such row, or a row
belonging to someone else — the request is denied. An unverifiable run id must
not resolve to "no cap applies", because that is the bypass restated.

The one deliberate exception is a **lookup fault** (DDB unreachable). See
:func:`resolve_run_binding`: that degrades rather than denies, because the
alternative is a DynamoDB blip taking down all inference — the same reasoning
``reservations.py`` applies to Redis. A fault is not a forgery.

## Why a cache, and why it is safe

The binding lookup is a DDB Query on the model-call hot path, which the issue
explicitly warns against. But a run makes hundreds of model calls under one run
id, so the result is cached in Redis under ``runbind:{run_id}`` for the run
lifetime: one Query per run, not per call.

Caching an *authorization* decision is normally a smell. It is safe here because
the cached value is immutable — a webhook-events row's ``user_id`` / ``tenant_id``
/ ``correlation_id`` are written once at ingress and never updated — and because
the identity comparison is re-done against the live ``TokenContext`` on every
call. Only the row lookup is cached, never the verdict.

## Shadow first (#3175)

``budget_run_binding_mode`` defaults to ``"shadow"``: the binding is resolved and
every mismatch is logged and counted, but nothing is denied and no cap is
enforced. That is how #3175 shipped credential binding, and the reason is the
same — the drift metric tells you how much real traffic a new deny rule would
have rejected *before* it rejects any. Flip to ``"enforce"`` once drift is zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import boto3
import redis.asyncio as redis
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from src.shared.logging import get_logger
from src.shared.redis_client import create_redis_client

logger = get_logger(__name__)

_CACHE_PREFIX = "runbind"

# Attributes the binding needs off the registry row. Projected explicitly (rather
# than fetching the whole item) because these rows carry the full webhook payload
# and the delivery body is large, hot-path irrelevant, and often sensitive.
#
# Issue #4344 adds ``is_human_rooted``: ``root_human_id`` holds a canonical
# ``users.id`` for a human-rooted chain but a SERVICE IDENTITY KEY for a
# service-rooted one (EventBridge / scheduled / CI / alarm), and the two are
# indistinguishable from the id alone. Budget attribution needs the kind to
# namespace-qualify the id, so the flag has to come off the same row.
_PROJECTION = "user_id, tenant_id, root_human_id, is_human_rooted, correlation_id, arrived_at"


@dataclass(frozen=True)
class RunBinding:
    """A run's server-authoritative identity.

    Every field comes from the ``webhook-events`` row written at ingress. None of
    it is caller-supplied, which is the entire point of the type.
    """

    run_id: str
    correlation_id: str
    tenant_id: str
    user_id: str
    root_human_id: str
    # Issue #4344: whether ``root_human_id`` names a HUMAN (canonical ``users.id``)
    # or a SERVICE identity key (``eventbridge:<rule>`` and friends). ``None`` means
    # the row carried no flag — rows written before the lineage plane shipped, and
    # every row from a writer that omits it. ``None`` is NOT "human": absent is
    # resolved as service by the budget layer, mirroring
    # ``correlation_store.py:120-124``'s deliberate no-True-default.
    #
    # Attribution only, exactly like ``root_human_id`` itself. Nothing in
    # ``verify_row_matches_caller``'s identity comparison reads it.
    is_human_rooted: bool | None = None


class RunBindingError(Exception):
    """The asserted run id could not be bound to the authenticated caller.

    Raised for *forgery-shaped* failures only — unknown run, or a run owned by
    someone else. A lookup fault is not this: it returns ``None`` so the caller
    can degrade instead of denying (see the module docstring).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _normalize_is_human_rooted(raw: object) -> bool | None:
    """Coerce a registry row's ``is_human_rooted`` attribute to ``bool | None``.

    Issue #4344. ``None`` in means ``None`` out: an absent attribute is a real,
    common state (rows predating the lineage plane) and must stay distinguishable
    from an explicit ``false`` for logging.

    Everything else resolves to ``True`` only for an unambiguous true — the bool
    ``True`` or the string ``"true"``. Every other shape reads ``False``. The
    asymmetry is deliberate and is the safe direction: the row is written by
    several producers, and a value this function cannot confidently read as human
    must not be *treated* as human, because that is what would put a service
    identity key into the canonical-``users.id`` namespace (D4b).
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() == "true"


def _get_dynamodb_table(table_name: str, aws_region: str):
    """Get a DynamoDB Table resource. Separated for testability.

    Mirrors ``internal/credential_binding._get_dynamodb_table`` so both binding
    paths stub the same way in tests.
    """
    dynamodb = boto3.resource("dynamodb", region_name=aws_region)
    return dynamodb.Table(table_name)


class RunBindingResolver:
    """Resolves and caches server-side run identity.

    Args:
        table_name: The ``webhook-events`` DDB table.
        aws_region: Region for the DDB client.
        redis_url: Cache backend. ``None`` disables caching (every call does a
            Query) rather than disabling the binding — correctness does not
            depend on the cache.
        cache_ttl_seconds: How long a resolved row is cached. Should cover a run
            lifetime; the values are immutable, so over-caching is harmless.
        client: Injectable Redis client, for tests.
        table: Injectable DDB table, for tests.
    """

    def __init__(
        self,
        table_name: str,
        aws_region: str,
        redis_url: str | None = None,
        cache_ttl_seconds: int = 86_400,
        client: redis.Redis | None = None,
        table=None,
    ) -> None:
        self._table_name = table_name
        self._aws_region = aws_region
        self._redis_url = redis_url
        self._cache_ttl_seconds = cache_ttl_seconds
        self._client = client
        self._table = table

    async def _get_cache(self) -> redis.Redis | None:
        """Get the cache client, or ``None`` when caching is unconfigured."""
        if self._client is not None:
            return self._client
        if not self._redis_url:
            return None
        self._client = create_redis_client(self._redis_url, encoding="utf-8", decode_responses=True)
        return self._client

    def _get_table(self):
        if self._table is None:
            self._table = _get_dynamodb_table(self._table_name, self._aws_region)
        return self._table

    async def _cached_row(self, run_id: str) -> dict | None:
        """Read a cached registry row. A cache fault is never fatal."""
        try:
            client = await self._get_cache()
            if client is None:
                return None
            raw = await client.get(f"{_CACHE_PREFIX}:{run_id}")
        except Exception as exc:
            logger.warning(f"Run-binding cache read failed, falling back to DDB: {exc}")
            return None

        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # Corrupt entry — treat as a miss and let the DDB read overwrite it.
            return None

    async def _cache_row(self, run_id: str, row: dict) -> None:
        """Cache a resolved registry row. Best-effort; failures are ignored."""
        try:
            client = await self._get_cache()
            if client is None:
                return
            await client.set(f"{_CACHE_PREFIX}:{run_id}", json.dumps(row), ex=self._cache_ttl_seconds)
        except Exception as exc:
            logger.warning(f"Run-binding cache write failed (continuing): {exc}")

    def _query_row(self, run_id: str) -> dict | None:
        """Fetch the latest ``webhook-events`` row for ``run_id``.

        ``Query``, not ``GetItem``: the table has a COMPOSITE key (``event_id``
        HASH + ``arrived_at`` RANGE), so ``GetItem`` would require a sort key the
        caller does not have. This is the Issue #3376 lesson, learned the hard way
        in ``credential_binding._lookup_authorized_user``.

        Newest-first with ``Limit=1`` handles GitHub re-delivery, which writes
        several rows under one ``event_id``.

        Returns:
            The row, or ``None`` when it genuinely does not exist.

        Raises:
            ClientError / BotoCoreError: propagated so the caller can tell a
                lookup FAULT (degrade) from an absent row (deny).
        """
        response = self._get_table().query(
            KeyConditionExpression=Key("event_id").eq(run_id),
            ProjectionExpression=_PROJECTION,
            ScanIndexForward=False,  # descending arrived_at -> latest first
            Limit=1,
        )
        items = response.get("Items", [])
        return items[0] if items else None

    async def resolve(self, run_id: str) -> dict | None:
        """Resolve the registry row for ``run_id``, cache-first.

        Returns:
            The row, or ``None`` when no such run exists.

        Raises:
            ClientError / BotoCoreError: on a lookup fault.
        """
        cached = await self._cached_row(run_id)
        if cached is not None:
            return cached

        row = self._query_row(run_id)
        if row is None:
            # Deliberately NOT cached. A negative cache would let a lookup during
            # the ingress-write race pin "unknown" for the whole run lifetime,
            # turning a millisecond race into a run-long outage.
            return None

        # Issue #4346: `correlation_id` normalizes to "" for chat-originated runs —
        # the chat row writer (agent-factory ingest `invocation_logger`) never writes
        # the attribute at all. That empty string must never key a CHAIN ledger, or
        # every chat run under one org shares one chain budget. The guard lives
        # downstream in `enforcement_service._scope_targets` (`if
        # binding.correlation_id:`) and is pinned by
        # `test_run_spend_cap.TestChainScopeRequiresAChainId` — do not "simplify"
        # either away on the assumption that an empty chain id cannot occur here.
        normalized = {
            "user_id": str(row.get("user_id") or ""),
            "tenant_id": str(row.get("tenant_id") or ""),
            "root_human_id": str(row.get("root_human_id") or ""),
            "correlation_id": str(row.get("correlation_id") or ""),
            # Issue #4344: normalized to bool/None here rather than left raw so the
            # value that goes into the cache is the value that comes back out.
            # `json.dumps` round-trips bool and None losslessly; a DDB Decimal or a
            # numeric string would not, and the cached read would then disagree with
            # the uncached one about a principal's KIND.
            "is_human_rooted": _normalize_is_human_rooted(row.get("is_human_rooted")),
        }
        await self._cache_row(run_id, normalized)
        return normalized

    async def close(self) -> None:
        """Close the cache client."""
        if self._client:
            await self._client.aclose()
            self._client = None


def verify_row_matches_caller(
    *,
    run_id: str,
    row: dict,
    caller_user_id: str,
    caller_org_id: str,
) -> RunBinding:
    """Assert a registry row belongs to the authenticated caller.

    This is the check that makes the run id unforgeable. Without it the lookup
    would merely confirm that *some* run has that id — which a caller can satisfy
    by naming any run they have ever seen, including another tenant's.

    A caller matches when EITHER identity on the row agrees:

    * ``user_id`` — the direct case: the run was dispatched for this caller.
    * ``root_human_id`` — the chain case: an agent-spawned child run carries the
      bot as ``user_id`` but the originating human as ``root_human_id`` (Issue
      #3705). Both must be accepted or every chain run would be denied.

    ``tenant_id`` is checked independently and is non-negotiable: a run whose
    tenant disagrees with the caller's org is a cross-tenant reference, and
    admitting it would let one tenant spend against another's cap.

    Args:
        run_id: The asserted run id.
        row: The registry row from :meth:`RunBindingResolver.resolve`.
        caller_user_id: ``TokenContext.user_id`` — authenticated, never a header.
        caller_org_id: ``TokenContext.attributed_org_id`` — the tenant whose
            ledger is being enforced (#4132).

    Returns:
        The verified :class:`RunBinding`.

    Raises:
        RunBindingError: on any mismatch.
    """
    row_tenant = row.get("tenant_id") or ""
    row_user = row.get("user_id") or ""
    row_root_human = row.get("root_human_id") or ""

    if row_tenant and caller_org_id and row_tenant != caller_org_id:
        raise RunBindingError(
            "tenant_mismatch",
            f"Run {run_id} belongs to a different tenant than the authenticated caller.",
        )

    identity_matches = caller_user_id and caller_user_id in (row_user, row_root_human)
    if not identity_matches:
        raise RunBindingError(
            "identity_mismatch",
            f"Run {run_id} was not dispatched for the authenticated caller.",
        )

    return RunBinding(
        run_id=run_id,
        correlation_id=str(row.get("correlation_id") or ""),
        tenant_id=str(row_tenant),
        user_id=str(row_user),
        root_human_id=str(row_root_human),
        # Issue #4344: carried through so budget attribution can tell a human root
        # from a service root. Read AFTER the identity checks above, never as part
        # of them — a row does not become admissible or inadmissible by claiming a
        # principal kind.
        is_human_rooted=_normalize_is_human_rooted(row.get("is_human_rooted")),
    )


async def resolve_run_binding(
    *,
    run_id: str,
    caller_user_id: str,
    caller_org_id: str,
    resolver: RunBindingResolver,
) -> RunBinding | None:
    """Bind an asserted run id to the authenticated caller, or refuse.

    Returns:
        The verified binding; or ``None`` when the binding could not be
        *determined* because the registry lookup itself faulted.

    ``None`` is a degrade signal, NOT an allow-all: the caller applies the
    hierarchy caps as normal and simply cannot apply the run/chain caps for this
    request. That asymmetry is deliberate and worth being explicit about, since
    the issue rightly insists on failing closed:

    * An unknown or mismatched run is a **forgery shape** → ``RunBindingError``
      → deny. This is the bypass the cap exists to prevent.
    * An unreachable DynamoDB is an **outage shape** → degrade. Denying here
      would convert a DDB blip into a total inference outage, and it buys nothing:
      a caller cannot induce it selectively to escape their cap, and the
      hierarchy caps still apply. Same policy ``reservations.py`` applies to
      Redis, for the same reason.

    Raises:
        RunBindingError: unknown run, or a run belonging to someone else.
    """
    try:
        row = await resolver.resolve(run_id)
    except (ClientError, BotoCoreError) as exc:
        logger.warning(f"Run-binding lookup faulted for run {run_id}, degrading to hierarchy caps only: {exc}")
        return None

    if row is None:
        raise RunBindingError(
            "unknown_run",
            f"Run {run_id} has no authorization record.",
        )

    return verify_row_matches_caller(
        run_id=run_id,
        row=row,
        caller_user_id=caller_user_id,
        caller_org_id=caller_org_id,
    )
