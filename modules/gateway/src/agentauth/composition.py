"""Assembles the agent authorization stack from real dependencies (#5028).

## Why a composition root exists at all

Until now every collaborator of :class:`~src.agentauth.policy.AgentAuthorizationService`
was injected, and the only code that injected them was a test. That was correct
for the policy — it names no table, which is what let the protected store be
introduced without rewriting it — but it left nothing in ``src/`` that could
actually build the thing. This module is that one place.

It is deliberately the *only* place that reads environment configuration for this
stack. The policy, the store and the resolver all take their dependencies as
arguments, so a second assembly site would be a second set of decisions about
which table and which reader to use, and those two would eventually disagree.

## What this module refuses to do

It does not authorize anything, and it holds no request-derived state. It also
does not read the *human* control path's authorization: a delegated agent caller
satisfies neither of ``ControlService.resolve_target``'s checks (tenant AND human
owner) and must not — its authority comes from a grant, not from owning a run.
:class:`AgentRunStateReader` therefore reads the control row *without* an
authorization check, which is safe only because the policy has already authorized
the caller for this exact target before the reader is ever called. That ordering
is the reader's precondition and the reason it is not exported for general use.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.adapter import AgentControlAdapter, AgentStatusView
from src.agentauth.policy import AgentAuthorizationService
from src.agentauth.store import AgentAuthorityStore
from src.agentauth.targets import AuthorityTargetResolver

logger = logging.getLogger("bedrockgateway.agentauth.composition")

# The table holding worker-registered control state and run status. The same table
# the human control path reads, and the same env var name — this is a *read* of
# state the worker legitimately owns, not the authority store.
WEBHOOK_EVENTS_TABLE_ENV = "WEBHOOK_EVENTS_TABLE"
_DEFAULT_WEBHOOK_EVENTS_TABLE = "adp-dev-webhook-events"

# Row statuses after which no command can be delivered, so a leftover control
# registration must not be reported as available. Terminal cleanup is best-effort
# (the pod may be killed first) and pod IPs are reused, so the status is checked
# independently of whether the fields were actually removed.
# Issue #3964 adds `aborted`: a run an operator stopped on purpose can accept no
# further command, so a control registration left behind by best-effort teardown
# must not be reported as available on it.
_TERMINAL_STATUSES: frozenset[str] = frozenset({"complete", "failed", "skipped", "budget_stopped", "cancelled", "aborted"})


class ExecutionLocator(Protocol):
    """Supplies the authoritative ``(tenant_id, arrived_at)`` for a run.

    Narrower than the store's ``load_execution`` on purpose: the readers below need
    exactly the row key and the tenant, and a protocol that handed them a whole
    :class:`~src.agentauth.execution.ExecutionRecord` would invite one of them to
    start making authorization decisions from a record the *policy* is supposed to
    judge.
    """

    def locate(self, *, run_id: str, tenant_id: str | None = None) -> tuple[str, str] | None: ...


class StoreExecutionLocator:
    """Locates a run's events-row key in the protected authority table.

    ``tenant_id`` is optional because the two readers arrive with different
    knowledge: the resolver's generation lookup already has a caller-scoped tenant,
    while the status projection is called with a run ID alone. When absent the
    tenant is resolved from the protected global ``INVOCATION#<id>/DISPATCH``
    pointer that trusted dispatch writes, never from the events row — the events
    row's own ``tenant_id`` is worker-writable today, so trusting it here would let
    a run relabel which tenant's state it appears to be.
    """

    def __init__(self, *, store: AgentAuthorityStore, dynamodb_client=None) -> None:
        self._store = store
        self._client = dynamodb_client or boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))

    def locate(self, *, run_id: str, tenant_id: str | None = None) -> tuple[str, str] | None:
        resolved = tenant_id or self._tenant(run_id)
        if not resolved:
            return None
        record = self._store.load_execution(invocation_id=run_id, tenant_id=resolved)
        if record is None or not record.arrived_at or record.tenant_id != resolved:
            return None
        return record.tenant_id, record.arrived_at

    def _tenant(self, run_id: str) -> str | None:
        try:
            response = self._client.get_item(
                TableName=self._store.table_name,
                Key={"pk": {"S": f"INVOCATION#{run_id}"}, "sk": {"S": "DISPATCH"}},
                ConsistentRead=True,
            )
        except (ClientError, BotoCoreError):
            return None
        return (response.get("Item") or {}).get("tenant_id", {}).get("S") or None


class AgentRunStateReader:
    """Reads a run's control state for an already-authorized agent caller.

    **Precondition: the caller has been authorized for this exact run.** This
    class performs no authorization. It is separate from
    ``ControlService.resolve_target`` rather than reusing it because that method
    enforces the *human* model — tenant AND (owner OR root human) — which a
    delegated agent caller cannot satisfy and should not: a coordinator's
    authority comes from its grant. Reusing it would mean either loosening the
    human path or giving every coordinator a fake ownership claim.

    What it returns is a **projection**, never the row. The row carries
    ``control_token``, and a coordinator that could read one would be able to
    command the pod directly, bypassing the policy entirely.
    """

    def __init__(
        self,
        *,
        table_name: str | None = None,
        dynamodb_resource=None,
        executions: ExecutionLocator | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        resource = dynamodb_resource or boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        self._table = resource.Table(table_name or os.environ.get(WEBHOOK_EVENTS_TABLE_ENV, _DEFAULT_WEBHOOK_EVENTS_TABLE))
        # The protected record supplies the authoritative sort key. Required in
        # practice; optional in the signature only so the adapter's protocol stays
        # unchanged, and a missing locator refuses rather than falling back.
        self._executions = executions
        self._now = now or (lambda: datetime.now(UTC))

    def read_state(self, *, run_id: str, generation: int) -> AgentStatusView | None:
        """Return a projection of ``run_id``'s state, or None if unreadable.

        None means "could not read", which the adapter reports as *unavailable*
        rather than as not-found — the policy already established that the target
        exists, so answering "no such run" to an authorized coordinator would be a
        lie it cannot act on.

        The row is fetched by its **exact key**, strongly consistent. An earlier
        revision queried ``event_id`` with ``Limit=1, ScanIndexForward=False`` and
        took the newest row, which made a second row under one ``event_id`` decide
        what an authorized coordinator is told; and an eventually-consistent read
        can serve a registration or terminal status from before the last write.
        """
        row = self._exact_row(run_id)
        if row is None:
            return None

        # The registered generation, compared against the generation the policy
        # resolved. A mismatch is reported rather than silently answered, because
        # answering about a different attempt of the same run is how a coordinator
        # would act on state belonging to a pod that no longer exists.
        registered = _optional_int(row.get("control_generation"))
        registered_generation = registered or 0
        if generation and registered_generation and registered_generation != generation:
            return AgentStatusView(
                run_id=run_id,
                generation=registered_generation,
                state="unavailable",
                available=False,
                reason="run generation has advanced",
            )

        status = str(row.get("status", "")) or "unknown"
        # Availability is about whether a *command* could be delivered, which needs
        # a registered address. Deliberately not about whether the status looks
        # healthy: a coordinator asking "can I steer this?" needs the transport
        # answer, and no verb is supported in this deployment anyway.
        #
        # A registration whose token has expired, or a run that has reached a
        # terminal status, is reported unavailable even though the fields are still
        # present. Terminal cleanup is best-effort — the pod may be killed before it
        # runs — so a leftover address on a finished run is expected, and pod IPs are
        # reused. Reporting it as available would have a coordinator plan work
        # against whatever pod now holds that IP.
        registered = bool(row.get("control_address")) and bool(row.get("control_token"))
        expiry = _parse_iso(_optional_str(row.get("control_token_expires_at")))
        expired = expiry is None or expiry <= self._now()
        terminal = status in _TERMINAL_STATUSES
        available = registered and not expired and not terminal

        return AgentStatusView(
            run_id=run_id,
            generation=registered_generation,
            state=status,
            available=available,
            reason=None if available else _unavailable_reason(registered=registered, expired=expired, terminal=terminal),
            # Deliberately empty, and NOT derived from ``SUPPORTED_AGENT_ACTIONS``.
            #
            # The original reason was that the set held MONITOR alone, so there was
            # nothing to advertise. That stopped being true with #5222 (pause,
            # resume) and #3963 (abort), and the field is still empty — on purpose,
            # because deployment support is only one of the three inputs a
            # capability claim needs. The other two are the target pod's own
            # advertised verbs and its current availability, and this reader has
            # neither: it projects one events row and never speaks to the pod.
            # Reporting ``{"abort": True}`` from deployment support alone would tell
            # a coordinator a specific run can be aborted on the strength of a
            # global constant, which is exactly the over-advertisement the human
            # path's three-way intersection in ``control_service`` exists to
            # prevent. An empty map reads as "ask, and find out", which is honest.
            capabilities={},
            updated_at=_optional_str(row.get("updated_at")) or _optional_str(row.get("arrived_at")),
        )

    # -- exact-key access -------------------------------------------------

    def _exact_row(self, run_id: str, tenant_id: str | None = None) -> dict | None:
        """Fetch the events row for ``run_id`` by its authoritative exact key.

        The events table is keyed ``(event_id, arrived_at)``, and ``arrived_at``
        comes from the protected execution record — captured by trusted dispatch,
        unwritable by the worker. Without it there is no single row this method may
        claim to be "the" row for a run, which is why a missing locator or a record
        with no ``arrived_at`` refuses instead of degrading to a newest-row query.

        The tenant is verified from the row as well: this is not the protected
        table, so a row here must not be trusted to be in the tenant the protected
        record names.
        """
        if not run_id or self._executions is None:
            return None
        try:
            located = self._executions.locate(run_id=run_id, tenant_id=tenant_id)
        except Exception as exc:
            logger.warning(
                "Could not locate the protected execution record",
                extra={"run_id": run_id, "reason": str(exc)},
            )
            return None
        if located is None:
            return None
        located_tenant, arrived_at = located
        try:
            response = self._table.get_item(
                Key={"event_id": run_id, "arrived_at": arrived_at},
                # An eventually-consistent read can serve a registration or a
                # terminal status from before the most recent write.
                ConsistentRead=True,
            )
        except ClientError as exc:
            # Matches the human path's handling: a deploy-order gap or a missing
            # table is reported as unreadable rather than raising, so it cannot be
            # distinguished from a run with no control state.
            logger.warning(
                "Agent status row lookup failed",
                extra={"run_id": run_id, "code": exc.response.get("Error", {}).get("Code", "")},
            )
            return None
        row = response.get("Item")
        if row is None or str(row.get("tenant_id", "")) != located_tenant:
            return None
        return row


class ControlGenerationReader(AgentRunStateReader):
    """Supplies the target's current control generation to the resolver.

    Reads the counter the worker's registration assigns (the atomic
    ``ADD control_generation``, now performed by
    :mod:`src.agentauth.registration` on the worker's behalf) rather than
    duplicating it into the protected table. Two counters would be two values that
    agree on the day they are written; and unlike lineage, the generation is not an
    authority claim — the listener re-checks its own generation against the
    envelope, so a wrong value here fails closed at the pod.

    Subclasses the state reader for its exact-key lookup: both readers need the
    same authoritative ``(event_id, arrived_at)`` derivation, and two copies of it
    is how one of them keeps the newest-row query.
    """

    def read_generation(self, *, run_id: str, tenant_id: str) -> int | None:
        row = self._exact_row(run_id, tenant_id)
        if row is None:
            # The resolver treats this as generation 0, which matches no registered
            # generation and so refuses at the listener rather than binding an
            # envelope to a guess.
            return None
        return _optional_int(row.get("control_generation"))


def build_authorization_service(
    *,
    authority_table: str | None = None,
    events_table: str | None = None,
    dynamodb_client=None,
    dynamodb_resource=None,
    now: Callable[[], datetime] | None = None,
    env: dict[str, str] | None = None,
) -> AgentAuthorizationService:
    """Assemble the shared policy from the protected store and a real resolver.

    One store instance backs both the grant and execution protocols: they are
    different questions but the same protected table, and two clients would be two
    connection pools reading one table for no benefit.
    """
    store = AgentAuthorityStore(table_name=authority_table, dynamodb_client=dynamodb_client)
    resolver = AuthorityTargetResolver(
        executions=store,
        generation_reader=ControlGenerationReader(
            table_name=events_table,
            dynamodb_resource=dynamodb_resource,
            executions=StoreExecutionLocator(store=store, dynamodb_client=dynamodb_client),
        ),
    )
    return AgentAuthorizationService(
        grant_store=store,
        target_resolver=resolver,
        execution_store=store,
        now=now,
        env=env,
    )


def build_control_adapter(
    *,
    authority_table: str | None = None,
    events_table: str | None = None,
    dynamodb_client=None,
    dynamodb_resource=None,
    now: Callable[[], datetime] | None = None,
    env: dict[str, str] | None = None,
) -> AgentControlAdapter:
    """Assemble the agent-facing adapter over the shared policy."""
    return AgentControlAdapter(
        policy=build_authorization_service(
            authority_table=authority_table,
            events_table=events_table,
            dynamodb_client=dynamodb_client,
            dynamodb_resource=dynamodb_resource,
            now=now,
            env=env,
        ),
        state_reader=AgentRunStateReader(
            table_name=events_table,
            dynamodb_resource=dynamodb_resource,
            executions=StoreExecutionLocator(
                store=AgentAuthorityStore(table_name=authority_table, dynamodb_client=dynamodb_client),
                dynamodb_client=dynamodb_client,
            ),
            now=now,
        ),
        now=now,
        env=env,
    )


@lru_cache(maxsize=1)
def get_control_adapter() -> AgentControlAdapter:
    """Process-wide adapter for the request path.

    Cached because the boto3 clients underneath are expensive to build and safe to
    share; the adapter itself holds no per-request state. Nothing authorization-
    relevant is cached — grants and execution state are read fresh on every
    request inside the policy, which is what keeps revocation bounded by the
    envelope TTL rather than by the process lifetime.
    """
    return build_control_adapter()


def _unavailable_reason(*, registered: bool, expired: bool, terminal: bool) -> str:
    """Say which condition made a run uncontrollable.

    Distinguished because they call for different operator responses: a missing
    registration is a startup or permission problem, an expired token means the
    pod outlived its own credential, and a terminal run is working as intended.
    """
    if terminal:
        return "run has reached a terminal status"
    if not registered:
        return "run has no live control registration"
    if expired:
        return "control registration has expired"
    return "run has no live control registration"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        # An unreadable expiry is treated by the caller as expired: a registration
        # whose deadline cannot be checked has no enforceable deadline.
        return None


def _optional_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value) -> int | None:
    """Coerce a DynamoDB numeric attribute to ``int``, or ``None``.

    Mirrors ``activity.control_service._optional_int`` deliberately. DynamoDB
    numbers arrive as ``Decimal`` through the resource API, so an ``isinstance``
    check against ``(int, float)`` rejects every real row and silently reports
    generation 0 — which matches no registered generation and would have made the
    generation binding inert while appearing implemented. ``int()`` is the check.

    ``bool`` is excluded because ``True`` is an ``int`` in Python and would
    otherwise coerce to generation 1, i.e. a plausible-looking wrong answer.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
