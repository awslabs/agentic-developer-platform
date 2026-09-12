"""Reading the active V2 pricing generation, without a database driver (§4.1, S3).

Three deploy artifacts need the same answer to "what are the current rates?" while
reaching the database three different ways: the gateway through async SQLAlchemy,
the two Lambdas through psycopg2. This module owns everything that is not the
driver — the SQL text, the schema-gap classification, the last-known-good cache and
the estimate reasons a degraded read implies — so those decisions cannot drift
between the artifact that settles money and the one that displays an estimate.

The adapters own exactly two things: issuing the SQL, and translating their
driver's exception into one of the ``V2UnavailableError`` subclasses below. Everything
after that is here.

Why the cache is not a plain TTL dict
-------------------------------------
The rates in the database are strictly better than the ones bundled in the
snapshot: they are what the daily refresh verified against AWS publications. So a
read failure must never demote a generation that was already loaded. If the
database becomes unreachable for hours, the right behavior is to keep charging the
last known good rates and mark the decision estimated with a staleness reason —
NOT to fall back to the bundled snapshot, which is by definition older. Falling
back would silently reprice live traffic in the middle of an outage, which is the
class of bug #4969 was filed about.

The bundled snapshot bootstraps cold processes and provides compatibility when
an operator explicitly disables consumers. Disable is separate from read failure;
last good rows remain retained internally for later recovery.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .policy import (
    CURRENT_SNAPSHOT_VERSION,
    POLICY_VERSION,
    EstimateReason,
    RateRow,
    Snapshot,
    VariantKey,
    load_snapshot,
    staleness_reasons,
)

logger = logging.getLogger(__name__)

#: PostgreSQL SQLSTATEs that mean "the V2 schema is not here yet", and NOTHING else.
#:
#: ``42P01`` undefined_table covers a pre-044 database; ``42703`` undefined_column
#: covers a partially applied or hand-patched schema, which is the more dangerous
#: case because the tables exist and the query looks like it should work.
#:
#: Deliberately a two-element set rather than a bare ``except Exception``. A
#: connection error, a permission error, a serialization failure or a syntax
#: mistake in a future edit of the SQL below are all NOT "the schema is missing" —
#: swallowing them here would turn any transient database problem into a silent,
#: indefinite fallback to bundled rates while the real generation sat there
#: unread. Those propagate as ``V2QueryFailedError`` and keep the last known good rows.
MISSING_SCHEMA_SQLSTATES = frozenset({"42P01", "42703"})

#: How long a confirmed schema gap is trusted before probing again.
#:
#: Bounded at 60s because the gap is expected to be temporary: on a fresh deploy
#: the gateway can start before the migration job finishes, and the pods must pick
#: up real rates shortly after 044/045 land without a restart. Caching the gap
#: forever would leave a running fleet on bundled rates until someone noticed.
SCHEMA_REPROBE_SECONDS = 60.0

#: Normal refresh interval for a healthy read.
RATE_CACHE_TTL_SECONDS = 900.0
RETRY_MIN_SECONDS = 30.0
RETRY_MAX_SECONDS = 900.0

#: Columns the readers require. Selected explicitly — never ``SELECT *`` — so that
#: a column added by a later migration cannot change the shape of the tuples these
#: rows are built from, and so a MISSING one raises 42703 (a classified, handled
#: schema gap) instead of silently arriving as None and being parsed as a rate.
RATE_COLUMNS: tuple[str, ...] = (
    "model_id",
    "geography",
    "service_tier",
    "context_tier",
    "region",
    "max_input_tokens",
    "input_price_per_1k_tokens",
    "output_price_per_1k_tokens",
    "cache_read_price_per_1k_tokens",
    "cache_write_price_per_1k_tokens",
    "cache_write_policy",
    "source",
    "source_url",
    "source_content_sha256",
    "source_effective_at",
    "verified_at",
    "snapshot_version",
    "generation_id",
)

#: The active pointer, joined to its generation so one round trip decides whether
#: there is anything to read at all.
#:
#: Keep the pointer visible even when it is explicitly cleared/disabled. Its
#: revision is needed to prevent a stale in-flight read from reopening the gate.
#: Generation status and supported schema/policy are checked before adoption.
SQL_ACTIVE_POINTER = """
SELECT a.current_generation_id,
       a.pointer_revision,
       a.consumers_enabled,
       g.snapshot_version,
       g.schema_version,
       g.policy_version,
       g.status AS generation_status,
       g.validated_at
FROM model_pricing_active a
LEFT JOIN model_pricing_generations g
  ON g.generation_id = a.current_generation_id
WHERE a.singleton IS TRUE
"""

SQL_ACTIVE_RATES = f"""
SELECT {", ".join(RATE_COLUMNS)}
FROM model_pricing_rates_v2
WHERE generation_id = %(generation_id)s
"""


class V2UnavailableError(Exception):
    """The active V2 generation could not be read. Base class; never raised directly."""


class MissingV2SchemaError(V2UnavailableError):
    """The V2 tables or a required column are absent (SQLSTATE 42P01/42703).

    Expected on a database that has not run migration 044 yet. The caller must have
    rolled its transaction back before raising this: PostgreSQL aborts the whole
    transaction on such an error, so any subsequent statement on the same
    connection fails with ``25P02`` until a rollback happens. A pricing probe must
    never be the reason a ledger write on that connection fails.
    """


class V2QueryFailedError(V2UnavailableError):
    """The read failed for any other reason — connection, permission, timeout.

    Distinct from ``MissingV2SchemaError`` because the response differs: a schema gap is
    re-probed in a minute and legitimately means "use bundled rates"; this means
    "keep whatever we already had and say so".
    """


class V2DisabledError(V2UnavailableError):
    """The operator disabled/cleared the pointer, or no pointer exists yet.

    This is a successful observation of the compatibility gate, not an outage.
    Keep the last good generation internally but do not serve it while disabled.
    """

    def __init__(self, message: str, *, pointer_revision: int | None = None) -> None:
        super().__init__(message)
        self.pointer_revision = pointer_revision


@dataclass(frozen=True)
class ActiveGeneration:
    """A validated generation and its rows, as read from the database."""

    generation_id: int
    pointer_revision: int
    snapshot_version: str | None
    policy_version: int | None
    rows: tuple[RateRow, ...]
    #: When this process read it, ISO 8601. Not the generation's own timestamp:
    #: staleness of the cache is a different question from staleness of the source.
    loaded_at: str

    @property
    def by_variant(self) -> dict[VariantKey, RateRow]:
        return {row.variant_key: row for row in self.rows}

    def rows_for_model(self, model_id: str) -> tuple[RateRow, ...]:
        return tuple(row for row in self.rows if row.model_id == model_id)


@dataclass
class RateSourceState:
    """What the cache currently holds and why (design §4.1).

    ``reasons`` are the estimate reasons implied by the STATE of the read — a
    failing refresh or bundled compatibility. The caller must also union the
    selected row's source-age reason via ``staleness_reasons`` after selection;
    one stale row must not make an unrelated fresh model estimated.
    """

    rows: tuple[RateRow, ...]
    source: str
    generation_id: int | None
    pointer_revision: int | None
    reasons: tuple[str, ...] = ()
    #: True when rows came from the database at some point, even if the last read
    #: failed. Settlement may still be verified in that case, subject to the
    #: reasons above; bootstrap rows never can be.
    from_database: bool = False


def rate_row_from_db(row: dict[str, Any]) -> RateRow:
    """Build a ``RateRow`` from a database mapping, validating as ``RateRow`` does.

    Routed through ``RateRow.from_mapping`` deliberately, so the CHECK constraints
    in 044 and the in-memory invariants are enforced by the same code for a
    database row as for a bundled snapshot entry. A row that somehow violated
    them — an ``unpublished`` policy carrying a zero write price, say — raises here
    rather than quietly pricing cache writes at zero.
    """
    return RateRow.from_mapping(row)


class V2RateCache:
    """Last-known-good cache over the active generation.

    One instance per process. Bookkeeping is synchronous; the async adapter
    coalesces reads with an asyncio lock because requests can overlap at awaits.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = RATE_CACHE_TTL_SECONDS,
        reprobe_seconds: float = SCHEMA_REPROBE_SECONDS,
        snapshot: Snapshot | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._reprobe = reprobe_seconds
        self._snapshot = snapshot or load_snapshot()
        self._generation: ActiveGeneration | None = None
        self._disabled = False
        self._pointer_revision: int | None = None
        #: Monotonic stamps. Wall clock is used only for staleness arithmetic
        #: against a row's ``verified_at``; intervals use a monotonic source so an
        #: NTP correction cannot make the cache look fresh for hours.
        self._loaded_monotonic: float | None = None
        self._schema_gap_monotonic: float | None = None
        self._first_failure_monotonic: float | None = None
        #: When the last attempt finished, successful or not. Distinct from
        #: ``_loaded_monotonic``, which only moves on success: without this, a
        #: process that has never loaded a generation has nothing to rate-limit
        #: against and re-queries on EVERY request. That is the normal state of a
        #: deployment whose rollout gate is still closed, so it would be a query per
        #: priced request for as long as the operator left it that way.
        self._attempted_monotonic: float | None = None
        self._consecutive_failures = 0

    # -- state questions ---------------------------------------------------

    @property
    def generation(self) -> ActiveGeneration | None:
        return self._generation

    def needs_refresh(self, monotonic: float) -> bool:
        """Whether a database read is due.

        Public because the async adapter must decide whether to await a query
        BEFORE it can hand a result to ``record_*``. The synchronous adapter gets
        the same schedule via ``refresh``, which calls this. Two adapters reading
        one schedule is the point; a second copy of this arithmetic is how the
        gateway and the tracker would end up re-probing on different clocks.
        """
        if self._schema_gap_monotonic is not None:
            # A known schema gap is re-probed on its own, shorter clock.
            return (monotonic - self._schema_gap_monotonic) >= self._reprobe
        if self._first_failure_monotonic is not None and self._attempted_monotonic is not None:
            delay = min(RETRY_MAX_SECONDS, RETRY_MIN_SECONDS * (2 ** min(self._consecutive_failures - 1, 5)))
            return (monotonic - self._attempted_monotonic) >= delay
        if self._generation is not None and self._loaded_monotonic is not None:
            # The LAST ATTEMPT, not the last successful load. A read that was
            # rejected as stale (an older pointer revision from a lagging replica)
            # leaves the loaded stamp where it was, so keying off it alone would
            # re-query on every request for as long as the replica lagged.
            last = max(self._loaded_monotonic, self._attempted_monotonic or self._loaded_monotonic)
            return (monotonic - last) >= self._ttl
        if self._attempted_monotonic is None:
            # Nothing tried yet on this process.
            return True
        # A closed rollout gate uses the healthy polling interval. Connection
        # failures and schema gaps were handled above on their bounded schedules.
        return (monotonic - self._attempted_monotonic) >= self._ttl

    def cache_failure_minutes(self, monotonic: float) -> float | None:
        """How long refresh has been failing, or None if the last attempt worked."""
        if self._first_failure_monotonic is None:
            return None
        return (monotonic - self._first_failure_monotonic) / 60.0

    # -- refresh -----------------------------------------------------------

    def refresh(self, fetch: Callable[[], ActiveGeneration], *, monotonic: float, force: bool = False) -> None:
        """Attempt a refresh if due. Never raises for an expected unavailability.

        ``fetch`` is the adapter's callable: it issues the two queries and returns
        an ``ActiveGeneration``, or raises a ``V2UnavailableError`` subclass. Any other
        exception is treated as ``V2QueryFailedError`` rather than propagating — a
        pricing read must not be able to fail a request that has already been
        served, and the caller has no better recovery than "use what we had".
        """
        if not force and not self.needs_refresh(monotonic):
            return

        try:
            generation = fetch()
        except Exception as exc:
            self.record_failure(exc, monotonic=monotonic)
            return

        self.record_success(generation, monotonic=monotonic)

    def record_success(self, generation: ActiveGeneration, *, monotonic: float) -> None:
        """Adopt a generation an adapter has just read.

        Separate from ``refresh`` so the async adapter — which cannot call a
        synchronous ``fetch`` — reaches the identical bookkeeping instead of
        reimplementing it.
        """
        self._adopt(generation, monotonic)

    def record_failure(self, exc: BaseException, *, monotonic: float) -> None:
        """Classify a failed read and update the cache's state accordingly.

        Failures retain the active last good rows. Explicit disable hides them
        until a newer enabled pointer is observed, without deleting the cache.
        """
        if isinstance(exc, MissingV2SchemaError):
            self._schema_gap_monotonic = monotonic
            # A missing schema after a successful read is an outage, unlike a
            # cold process probing before migrations have installed the tables.
            self._note_failure(monotonic, expected=self._generation is None or self._disabled)
            return
        if isinstance(exc, V2DisabledError):
            self._schema_gap_monotonic = None
            revision = exc.pointer_revision
            if revision is not None and self._pointer_revision is not None and revision < self._pointer_revision:
                self._note_failure(monotonic, expected=False)
                return
            if revision is not None:
                self._pointer_revision = revision
            self._disabled = True
            self._schema_gap_monotonic = None
            self._note_failure(monotonic, expected=True)
            return
        # The last attempt no longer establishes a schema gap. In particular, a
        # connection error after the first 60s probe must obey retry backoff.
        self._schema_gap_monotonic = None
        if isinstance(exc, V2UnavailableError):
            logger.warning("V2 pricing refresh failed, retaining cached rates: %s", exc)
            self._note_failure(monotonic, expected=False)
            return

        # An unclassified exception is still not allowed to propagate: a pricing
        # read must not fail a request that has already been served. Logged with a
        # traceback because, unlike the branches above, this one is a bug.
        logger.exception("V2 pricing refresh raised unexpectedly, retaining cached rates", exc_info=exc)
        self._note_failure(monotonic, expected=False)

    def _adopt(self, generation: ActiveGeneration, monotonic: float) -> None:
        current = self._generation
        if self._pointer_revision is not None and (
            generation.pointer_revision < self._pointer_revision
            or (self._disabled and generation.pointer_revision <= self._pointer_revision)
            or (current is not None and generation.pointer_revision == self._pointer_revision and generation.generation_id != current.generation_id)
        ):
            logger.warning(
                "ignoring V2 generation %s at pointer revision %s: latest observed revision is %s",
                generation.generation_id,
                generation.pointer_revision,
                self._pointer_revision,
            )
            self._schema_gap_monotonic = None
            self._note_failure(monotonic, expected=False)
            return

        self._disabled = False
        self._pointer_revision = generation.pointer_revision
        self._generation = generation
        self._loaded_monotonic = monotonic
        self._attempted_monotonic = monotonic
        self._schema_gap_monotonic = None
        self._first_failure_monotonic = None
        self._consecutive_failures = 0

    def _note_failure(self, monotonic: float, *, expected: bool) -> None:
        self._consecutive_failures += 1
        self._attempted_monotonic = monotonic
        if self._first_failure_monotonic is None:
            self._first_failure_monotonic = monotonic
        if expected:
            # An absent schema or a disabled pointer is not a "failure" for the
            # purpose of the cache-refresh-failing signal: alarming on a
            # deliberately disabled rollout gate would train operators to ignore
            # the alarm that matters.
            self._first_failure_monotonic = None
            self._consecutive_failures = 0

    # -- resolution --------------------------------------------------------

    def state(self, *, monotonic: float, now_iso: str) -> RateSourceState:
        """The rows to price with, and the estimate reasons the read state implies."""
        failure_minutes = self.cache_failure_minutes(monotonic)

        if self._generation is None or self._disabled:
            # No enabled database state is available: bootstrap from the bundled snapshot. This
            # is always estimated — the snapshot is a floor, not a verification of
            # what AWS publishes today.
            reasons = {EstimateReason.BOOTSTRAP_FALLBACK}
            if failure_minutes is not None:
                reasons.update(staleness_reasons(row_verified_at=None, now_iso=now_iso, cache_failure_minutes=failure_minutes))
            return RateSourceState(
                rows=self._snapshot.rates,
                source=f"bundled_snapshot:{self._snapshot.snapshot_version}",
                generation_id=None,
                pointer_revision=None,
                reasons=tuple(sorted(reasons)),
                from_database=False,
            )

        generation = self._generation
        reasons: set[str] = set()
        if failure_minutes is not None:
            reasons.update(staleness_reasons(row_verified_at=None, now_iso=now_iso, cache_failure_minutes=failure_minutes))
        return RateSourceState(
            rows=generation.rows,
            source=f"v2_generation:{generation.generation_id}",
            generation_id=generation.generation_id,
            pointer_revision=generation.pointer_revision,
            reasons=tuple(sorted(reasons)),
            from_database=True,
        )

    def snapshot_for_selection(self, state: RateSourceState) -> Snapshot:
        """A ``Snapshot`` view over ``state.rows``, for the selection helpers.

        ``select_rate_row`` and the flat adapters take a ``Snapshot``. Wrapping the
        database rows in one — rather than giving those helpers a second code path
        for database rows — is what guarantees a variant is selected identically
        whether its rates came from the bundle or from a generation.

        The curated non-OpenAI section always comes from the bundled snapshot:
        those rates are hand-curated policy (#1486/#4592), not something the daily
        refresh publishes, so they are not carried in V2 rows.
        """
        if not state.from_database:
            return self._snapshot
        return replace(self._snapshot, rates=state.rows)


@dataclass
class ReaderMetrics:
    """Counters an adapter can surface without importing a metrics client.

    Kept here so both adapters report the same names; the gateway publishes them
    through its existing metrics surface and the Lambdas through CloudWatch.
    """

    schema_missing: int = 0
    disabled: int = 0
    query_failed: int = 0
    refreshed: int = 0
    served_from_cache: int = 0
    served_from_bundle: int = 0
    observed_generations: set[int] = field(default_factory=set)

    def as_dict(self) -> dict[str, int]:
        return {
            "schema_missing": self.schema_missing,
            "disabled": self.disabled,
            "query_failed": self.query_failed,
            "refreshed": self.refreshed,
            "served_from_cache": self.served_from_cache,
            "served_from_bundle": self.served_from_bundle,
            "distinct_generations": len(self.observed_generations),
        }


def classify_sqlstate(sqlstate: str | None) -> type[V2UnavailableError]:
    """Map a PostgreSQL SQLSTATE onto the right unavailability class.

    Centralised so both adapters cannot disagree about which codes mean "schema
    absent". Anything unrecognised — including None, which is what a connection
    failure produces — is a query failure, so the cache is retained rather than
    demoted to bundled rates.
    """
    if sqlstate in MISSING_SCHEMA_SQLSTATES:
        return MissingV2SchemaError
    return V2QueryFailedError


def build_active_generation(
    *,
    pointer: dict[str, Any] | None,
    rate_rows: list[dict[str, Any]],
    loaded_at: str,
) -> ActiveGeneration:
    """Assemble the read result, rejecting states a consumer must not serve.

    Explicitly disabled/cleared pointers select compatibility. A malformed or
    unsupported enabled generation is an operational error that retains last good
    rows; it must not masquerade as an operator closing the gate.
    """
    if not pointer:
        raise V2DisabledError("no active pricing pointer")
    revision = pointer.get("pointer_revision")
    if type(revision) is not int or revision < 0:
        raise V2QueryFailedError("active pointer has an invalid revision")
    if pointer.get("current_generation_id") is None:
        raise V2DisabledError("active pointer has no generation", pointer_revision=revision)
    if pointer.get("consumers_enabled") is False:
        raise V2DisabledError("consumers_enabled is false", pointer_revision=revision)
    if pointer.get("consumers_enabled") is not True:
        raise V2QueryFailedError("active pointer has an invalid consumers_enabled flag")
    if type(pointer["current_generation_id"]) is not int or pointer["current_generation_id"] <= 0 or revision <= 0:
        raise V2QueryFailedError("enabled pointer requires positive generation and revision")
    if pointer.get("generation_status") != "validated":
        raise V2QueryFailedError("active generation is not validated")
    if pointer.get("schema_version") != 2 or pointer.get("policy_version") != POLICY_VERSION:
        raise V2QueryFailedError("active generation has an unsupported schema or policy version")
    if not rate_rows:
        raise V2QueryFailedError(f"validated generation {pointer['current_generation_id']} has no rate rows")

    rows = tuple(rate_row_from_db(row) for row in rate_rows)
    if any(row.generation_id != pointer["current_generation_id"] for row in rows):
        raise V2QueryFailedError("rate rows do not belong to the active generation")
    if len({row.variant_key for row in rows}) != len(rows):
        raise V2QueryFailedError("active generation has duplicate variant keys")
    return ActiveGeneration(
        generation_id=int(pointer["current_generation_id"]),
        pointer_revision=revision,
        snapshot_version=pointer.get("snapshot_version"),
        policy_version=pointer.get("policy_version"),
        rows=rows,
        loaded_at=loaded_at,
    )


def utc_now_iso() -> str:
    """Current UTC time as ISO 8601, for staleness arithmetic."""
    from datetime import UTC

    return datetime.now(UTC).isoformat()


__all__ = [
    "CURRENT_SNAPSHOT_VERSION",
    "MISSING_SCHEMA_SQLSTATES",
    "RATE_CACHE_TTL_SECONDS",
    "RATE_COLUMNS",
    "SCHEMA_REPROBE_SECONDS",
    "SQL_ACTIVE_POINTER",
    "SQL_ACTIVE_RATES",
    "ActiveGeneration",
    "MissingV2SchemaError",
    "RateSourceState",
    "ReaderMetrics",
    "V2DisabledError",
    "V2QueryFailedError",
    "V2RateCache",
    "V2UnavailableError",
    "build_active_generation",
    "classify_sqlstate",
    "rate_row_from_db",
    "utc_now_iso",
]
