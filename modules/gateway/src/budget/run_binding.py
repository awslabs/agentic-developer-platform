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
row written at ingress, before any agent code ran — and every value the cap keys
on is read off that row. The ``correlation_id`` for the chain scope comes from
there too, so the chain ledger is equally unforgeable (the worker does send
``x-agent-correlationid``, but nothing here reads it).

## What the binding actually proves — a CAPABILITY, not an identity (Issue #4337)

This module shipped comparing the caller's ``TokenContext.user_id`` against the
row's ``user_id`` / ``root_human_id``. **That comparison could never succeed**,
and #4337 removed it. The two sides are drawn from disjoint identifier
namespaces:

* the caller side is an agent-registry ``agent_name`` (``auth/agent_registry.py``
  sets ``TokenContext.user_id = entry["agent_name"]``), and **every** hosted agent
  run on the platform authenticates as the single shared registry row
  ``scaledjob-worker`` (agent-factory ``infra/agent-registry-seed.tf``, whose own
  comment says all workers share one entry);
* the row side is a canonical ``users.id`` (webhook-ingress
  ``lambda/common/spawn_persona.py``'s ``effective_user_id``) or a service identity
  key such as ``eventbridge:<rule>`` (``lambda/eventbridge/handler.py``, whose
  ``service_identity`` comes from the rule's own InputTransformer in
  ``infra/eventbridge.tf``).

No value legitimately lives in both, so the equality test failed on 100% of
legitimate traffic — a total outage the moment #4187 flipped to enforce. Nor could
it be *repaired* by recording the executing agent identity on the row: the value
recorded would be the constant ``scaledjob-worker`` for every run, so the check
would reduce to "is the caller the shared worker", which every caller satisfies.
That is the worst outcome available — zero drift over a disabled guard.

So the property is **stated instead of pretended**, following the treatment
``internal/credential_binding.py`` already gives the same table and the same
``event_id``: the run id is an **unguessable bearer capability**, not a claim of
identity. This is a deliberate reduction from *"the caller owns this run"* to
*"the caller holds a live, tenant-consistent capability for this run"*, and its
resistance rests on four independently-tested properties:

1. **Unguessable** — the run id is the envelope ``message_id``, a fresh ``uuid4``
   (``spawn_persona.py``). An invented id has no row: ``unknown_run``.
2. **Tenant-scoped** — :func:`verify_row_matches_caller` derives the enforced
   tenant from the row's server-written ``tenant_id`` and denies when the caller's
   attributed org disagrees. **This is the load-bearing property**, and it is why
   B1 below is not optional.
3. **Non-terminal** — a finished run's id mints no fresh headroom. Without this,
   rotating across one's OWN completed runs is an unbounded bypass, and it is the
   residual risk the (impossible) equality check was nominally covering.
4. **Not negative-cached** — an ``unknown_run`` verdict is never cached, so the
   ingress-write race cannot pin "unknown" for a whole run lifetime.

## The tenant guard is the guard (Issue #4337, decision B1)

``attributed_org_id`` is **caller-influenced**: ``auth/middleware.py`` writes it
from the pod's own ``X-Agent-OrgId`` header for any internal-scope agent, and
``shared/schemas/auth.py`` states the #4132 invariant in as many words — it "MUST
NEVER gate access". The pre-#4337 code gated on it anyway, which was a standing
violation masked only because the identity check above fired first on all
traffic. Relax identity while that stands and the result is worse than the
outage it fixes: a worker that sets ``X-Agent-OrgId`` to another tenant binds
that tenant's run and spends its cap.

Using the *authenticated* ``org_id`` instead does not work either — the shared
worker's registry ``org_id`` is the literal ``__platform__``, which equals no
real tenant, so every legitimate run would fail instead. That is the same
namespace disjunction, one field over.

**B1 inverts the direction of trust.** The row's ``tenant_id`` is written at
ingress by webhook-ingress (``lambda/common/webhook_events.py``) and is the
authority; the caller's ``attributed_org_id`` is an *assertion verified against
it*, exactly as the run id itself is. Forging the header becomes self-defeating
rather than profitable, and the run/chain ledger partitions on a server-written
value. This **resolves** the #4132 violation rather than inheriting it.

Fail-closed means both directions: a disagreement denies, and so does an
*unresolvable* authority (a row with no ``tenant_id``). The pre-#4337 guard was a
three-way conjunction — ``row_tenant and caller_org_id and row_tenant !=
caller_org_id`` — so either side being blank skipped the tenant check silently.
Under the capability model that blank-skip IS the cross-tenant bypass, so an
unusable authority is now a denial.

## Fail closed on the CAPABILITY, not just on spend

Issue #4075 made the check fail closed when *spend* is unknown. This adds the
other half: when the run's capability cannot be verified — no such row, a row
belonging to another tenant, or a row whose run has already finished — the
request is denied. An unverifiable run id must not resolve to "no cap applies",
because that is the bypass restated.

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
have rejected *before* it rejects any.

"Flip once drift is zero" is necessary but **not sufficient** — see the hardened
gate on #4337. Zero ``identity_mismatch`` was specifically NOT evidence of
anything, because drift measures the binding and not the reservation: with the
gateway's Redis auth broken (#4342) every run/chain reservation degraded to
*allow*, so the cap could read as live and enforce nothing while the gate went
green. The gate therefore also requires ``budget_reservation_outcome=reserved``
rather than ``degraded``, and positive controls (a forged run id and a forged
``X-Agent-OrgId`` must both still drift in the same window) so a window with zero
drift is distinguishable from a guard that has stopped checking.

## Dispatch paths that reach the gateway with NO bindable run id (Issue #4337 D10a)

The binding can only run if a row exists under the asserted id, and not every
dispatch path produces one. Audited in code, with the recorded disposition each
path must be held to during a shadow window — a GitHub-only window does not
qualify as a pre-enforce gate, because the paths below cannot generate drift at
all unless they are exercised:

All three bindable paths are bindable for the SAME reason — they route through
``lambda/common/spawn_persona.py``, which is the only module on any of them that
both stamps an envelope ``message_id`` and writes the row keyed on it. The
EventBridge and ``/agent/trigger`` handlers write no row of their own; they
delegate. That single chokepoint is why the table below has only three
dispositions and not one per handler.

===================== ====== ============ =================================================
Path                  Row?   Sends id?    Disposition
===================== ====== ============ =================================================
GitHub webhook        yes    yes          **bindable** — via ``common/spawn_persona``
EventBridge/cron/CI   yes    yes          **bindable**, service-rooted;
                                          ``eventbridge/handler.py`` delegates to
                                          ``spawn_persona`` for both row and envelope
``POST /agent/trigger`` yes  yes          **bindable** — same delegation
Orchestration engine  no     no           **exempt** — ``orchestration/dispatch_pass.py``'s
                                          ``_build_envelope`` has no ``message_id`` key and
                                          the module makes no DDB write at all (deliberate:
                                          it does not call ``spawn_persona``).
GitLab webhook        no     no           **exempt** — ``lambda/gitlab/handler.py`` imports
                                          no row writer; its uuid becomes the
                                          ``correlation_id``, never a ``message_id``. The
                                          ``message_id`` in its HTTP response is the SQS
                                          one (trap #2 above), not an envelope field.
Slack / WebChat       yes    **no**       **exempt for now** — a row exists, but from a
                                          narrower second writer (agent-factory
                                          ``gateway/lambdas/ingest/invocation_logger.py``,
                                          same table, fewer attributes, written AFTER the
                                          SQS publish rather than before), and
                                          ``agent/k8s/chat-scaledjob.yaml`` sets no
                                          ``ADP_MESSAGE_ID`` — the var is exported only by
                                          the GitHub worker's ``entrypoint.py``. Bindable
                                          once the chat worker asserts an id.
===================== ====== ============ =================================================

Every one of these authenticates as the shared IAM worker, so the
``exempt_human`` policy does **not** cover them (it keys on
``auth_source == "iam"``, which is true for all of them). The exemption is
declared explicitly via ``budget_run_id_required_mode="exempt_missing"`` rather
than left to be discovered at enforce time as a 402 on the first model call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import boto3
import redis.asyncio as redis
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from src.activity.liveness import OBSERVED_TERMINAL_STATUSES
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
#
# ---------------------------------------------------------------------------
# Issue #4348 (D11): TWO name collisions, both of which look like the run id.
# ---------------------------------------------------------------------------
#
# **1. The row attribute named ``run_id`` is NOT the run id.** It is the KEDA
# job/pod name, written by the worker's status updater
# (``agent-worker-image/lib/invocation_status.py`` — "run_id: KEDA job/pod name
# (set at in_progress)"), and the UI labels it "Run / Job ID". Real values look
# like ``agent-gateway-worker-abc12`` / ``chat-agent-worker-xyz98``.
#
# The id this module binds on — the one ``X-Agent-RunId`` carries — is the
# ``event_id``, i.e. the table's partition key, which is why ``_query_row``
# keys on ``Key("event_id")``. ``run_id`` is deliberately ABSENT from the
# projection below and must stay absent: binding on the pod name would compare
# against a value no caller ever sends, denying legitimate runs as
# ``unknown_run`` (or, worse, binding the wrong run). The identical trap on the
# cost path already has a guard — ``orchestration/cost.py``'s
# ``assert_join_key_is_event_id`` — and ``tests/budget/test_run_binding.py``
# reuses it here so re-adding ``run_id`` fails CI rather than production.
#
# **2. ``SpawnResult.message_id`` is NOT the run id either.** It is the SQS
# ``MessageId`` returned by ``publish_envelope``
# (``webhook-ingress/lambda/common/sqs_publisher.py`` →
# ``spawn_persona.py:258``). Only the ENVELOPE ``message_id``
# (``spawn_persona.py:543``, a fresh uuid4) is the run id — it is what becomes
# the ``event_id`` PK of this very row (``spawn_persona.py:696``) and what the
# worker exports as ``ADP_MESSAGE_ID`` for the header. Asserting the SQS id
# would make every call ``unknown_run``.
#
# Issue #4337 adds ``status``: capability property 3 (non-terminal). A finished
# run's id must mint no fresh headroom, which is the residual bypass the removed
# identity equality was nominally covering — rotation across one's OWN completed
# runs. The attribute is written at ingress (webhook-ingress
# ``lambda/common/webhook_events.py``) and advanced on every transition by the
# worker's status updater (``agent-worker-image/lib/invocation_status.py``).
_PROJECTION = "user_id, tenant_id, root_human_id, is_human_rooted, correlation_id, status, arrived_at"

# The two principal kinds ``root_human_id`` can name. Issue #4337 D4: DERIVED from
# the row's ``is_human_rooted`` flag, never a new column and never a value any
# caller supplies.
ROOT_PRINCIPAL_HUMAN = "human"
ROOT_PRINCIPAL_SERVICE = "service"


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
    # ``verify_row_matches_caller``'s admissibility checks reads it — a row does
    # not become bindable or unbindable by claiming a principal kind.
    is_human_rooted: bool | None = None

    @property
    def root_principal_type(self) -> str:
        """Whether ``root_human_id`` names a human or a service (Issue #4337 D4c).

        ``"human"`` only for an explicit ``is_human_rooted is True``; ``"service"``
        for an explicit false AND for absent (D4b — mirroring
        ``correlation_store.py``'s deliberate no-True-default, and
        ``_qualify_root_principal_id``'s). Absent must not read as human: that is
        what would attribute machine spend to a person's envelope and put a service
        identity key into the canonical-``users.id`` namespace.

        DERIVED rather than stored, deliberately. The kind is already fully
        determined by ``is_human_rooted``, and a second field holding the same fact
        is two homes for one value — the divergence trap the cache-normalization
        note on ``resolve`` describes, where a cached read and an uncached read can
        disagree about a principal's KIND. A property cannot desync.

        Consumers must read the TYPE from here and the ID from ``root_human_id``,
        and must never flatten a service key through a field typed as a canonical
        ``users.id``. ``enforcement_service._qualify_root_principal_id`` is what
        keeps the two namespaces apart downstream (Issue #4344).
        """
        return ROOT_PRINCIPAL_HUMAN if self.is_human_rooted is True else ROOT_PRINCIPAL_SERVICE


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
            # Issue #4337: capability property 3. Normalized to "" when absent so the
            # non-terminal check below sees a consistent shape from both the cached
            # and the uncached path.
            #
            # NOTE the cache TTL interaction, which is the one place this attribute
            # differs from every other value here: the rest are immutable (written
            # once at ingress), while `status` ADVANCES over a run's life. So a row
            # cached while `in_progress` keeps binding after the run reaches a
            # terminal status, for up to `cache_ttl_seconds`. That is acceptable and
            # is the right direction of error: the window is bounded by the run's own
            # cache entry, the reservation TTL bounds the headroom it could mint, and
            # the alternative — re-querying DDB per model call to catch a transition —
            # is exactly the hot-path cost the cache exists to remove. What the check
            # closes is the unbounded case: rotating across ids from runs that
            # finished long ago, whose rows are read cold and read terminal.
            "status": str(row.get("status") or ""),
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
    """Verify the caller holds a live, tenant-consistent capability for this run.

    This is the check that makes the run id unforgeable. Without it the lookup
    would merely confirm that *some* run has that id — which a caller can satisfy
    by naming any run they have ever seen, including another tenant's.

    Issue #4337 replaced a caller-identity equality test that could never succeed
    with the bearer-capability model the module docstring states in full. Read that
    section before changing anything here; the short version is that the caller side
    of the old comparison was always the shared registry name ``scaledjob-worker``
    while the row side was a canonical ``users.id``, so the test denied 100% of
    legitimate traffic and could not be repaired without reducing to "is the caller
    the shared worker" — a check every caller passes.

    Two properties are asserted here. The other two live elsewhere by construction:
    unguessability is the ``uuid4`` run id (an invented one has no row, so
    :func:`resolve_run_binding` raises ``unknown_run``), and not-negative-caching is
    :meth:`RunBindingResolver.resolve` declining to cache a miss.

    **1. Tenant-scoped (B1) — the load-bearing property.** The enforced tenant is
    DERIVED from the row's server-written ``tenant_id``; ``caller_org_id`` is only
    an assertion checked against it. Both failure directions deny:

    * the caller asserts a tenant that disagrees with the row → ``tenant_mismatch``.
      Forging ``X-Agent-OrgId`` to reach another tenant's run is therefore
      self-defeating: the row it names is the row that convicts it.
    * the row carries no ``tenant_id`` → ``tenant_mismatch``. There is no authority
      to enforce against, and a capability with no tenant scope is not tenant-scoped.
      The pre-#4337 conjunction skipped the check when either side was blank, which
      under this model is the cross-tenant bypass itself.

    A blank ``caller_org_id`` is NOT special-cased into an allow, for the same
    reason: an unasserted tenant cannot equal a real one, so it denies.

    **2. Non-terminal.** A run whose row reports an observed-terminal status is
    finished, and its id must mint no fresh headroom. Terminality is read from
    ``activity.liveness.OBSERVED_TERMINAL_STATUSES`` — the platform's existing
    single definition, reused rather than restated so the binding and the activity
    read path cannot drift about whether a run is over.

    An absent or unrecognised status is **not** terminal. That follows the same
    module's rule — "loss of contact is not evidence of exit" — and is the safe
    direction here too: denying on absent would deny every row whose writer never
    advanced it (the chat writer leaves rows at ``webhook_received``) and every row
    from a future producer using a status this build has not heard of.

    Args:
        run_id: The asserted run id (the row's ``event_id``, never its ``run_id``
            attribute — see the projection comment and Issue #4348).
        row: The registry row from :meth:`RunBindingResolver.resolve`.
        caller_user_id: ``TokenContext.user_id``. Authenticated, but on the hosted
            path it is the shared worker name for every run, so it is recorded for
            the drift log and is deliberately NOT compared against the row. See the
            module docstring.
        caller_org_id: ``TokenContext.attributed_org_id`` — caller-influenced
            (#4132), so it is verified against the row rather than trusted.

    Returns:
        The verified :class:`RunBinding`. Its ``tenant_id`` is the ROW's value,
        which is what the run/chain ledger must partition on.

    Raises:
        RunBindingError: ``tenant_mismatch`` or ``terminal_run``.
    """
    row_tenant = str(row.get("tenant_id") or "")
    row_user = str(row.get("user_id") or "")
    row_root_human = str(row.get("root_human_id") or "")
    row_status = str(row.get("status") or "")

    # Property 2 (B1): the row is the authority, the caller merely asserts.
    if not row_tenant or row_tenant != caller_org_id:
        raise RunBindingError(
            "tenant_mismatch",
            f"Run {run_id} could not be bound to the caller's attributed tenant.",
        )

    # Property 3: a finished run's capability is spent.
    if row_status in OBSERVED_TERMINAL_STATUSES:
        raise RunBindingError(
            "terminal_run",
            f"Run {run_id} has already finished; its id mints no further headroom.",
        )

    return RunBinding(
        run_id=run_id,
        correlation_id=str(row.get("correlation_id") or ""),
        # The SERVER-WRITTEN tenant, not the asserted one. They are equal by the
        # check above, so this is not a behavioural difference today — it is the
        # provenance that matters: everything downstream (the run/chain reservation
        # keys, the scope-cap lookup) partitions on a value webhook-ingress wrote,
        # so a future relaxation of the assertion check cannot silently move a
        # ledger to a caller-chosen tenant.
        tenant_id=row_tenant,
        user_id=row_user,
        root_human_id=row_root_human,
        # Issue #4344 / #4337: carried through so budget attribution can tell a human
        # root from a service root (see ``RunBinding.root_principal_type``). Read
        # AFTER the checks above, never as part of them — a row does not become
        # admissible or inadmissible by claiming a principal kind.
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

    * An unknown run, a cross-tenant one, or a finished one is a **forgery shape**
      → ``RunBindingError`` → deny. This is the bypass the cap exists to prevent.
    * An unreachable DynamoDB is an **outage shape** → degrade. Denying here
      would convert a DDB blip into a total inference outage, and it buys nothing:
      a caller cannot induce it selectively to escape their cap, and the
      hierarchy caps still apply. Same policy ``reservations.py`` applies to
      Redis, for the same reason.

    Raises:
        RunBindingError: ``unknown_run``, ``tenant_mismatch``, or ``terminal_run``.
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
