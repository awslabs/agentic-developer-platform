"""
Budget Enforcement Service.

This module provides cascading budget enforcement logic for the proxy path.
It traverses the entity hierarchy (user → team → department → org) and
checks budget constraints at each level.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import botocore.exceptions
from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database import get_session_factory, reset_engine
from src.shared.logging import get_logger
from src.shared.metrics import (
    emit_budget_check_failure,
    emit_budget_grace_engaged,
    emit_budget_reservation_outcome,
)
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import (
    DenyReason,
    EnforcementMode,
    EnforcementResult,
    EntityType,
    PeriodType,
)

from .config import budget_config
from .grace_window import GraceWindow
from .pricing import PricingService, pricing_service
from .reservations import ReservationStore, ReservationTarget
from .utils import (
    calculate_budget_utilization,
    get_period_start_end,
)

logger = get_logger(__name__)

# Exception types that mean "the ledger is temporarily unreadable" rather than
# "this code is broken" (Issue #4075).
#
# Only these get grace-window treatment. The distinction is load-bearing: a
# TypeError/AttributeError in the check is DETERMINISTIC — it recurs on every
# request forever — so a grace window is precisely the wrong medicine. It would
# expire and then deny 100% of traffic on every enforced path, permanently. A
# code bug must not be able to do that, so unexpected classes fail open with a
# distinct high-severity signal instead.
# Note: TimeoutError and ConnectionError are both subclasses of OSError, so
# OSError alone would suffice. They are listed explicitly because they are the
# two faults this path actually sees in production (RDS proxy timeouts, socket
# resets) and a future narrowing of OSError must not silently drop them.
_INFRASTRUCTURE_FAULTS: tuple[type[BaseException], ...] = (
    SQLAlchemyError,
    TimeoutError,
    ConnectionError,
    OSError,
    botocore.exceptions.BotoCoreError,
    botocore.exceptions.ClientError,
)


class BudgetEnforcementService:
    """
    Service for cascading budget enforcement.

    Provides:
    - Hierarchical budget checking (user → team → dept → org)
    - Pre-request cost estimation
    - Post-request usage recording
    - Soft/hard enforcement mode support
    """

    def __init__(
        self,
        db_session: AsyncSession | None = None,
        pricing: PricingService | None = None,
        grace_window: GraceWindow | None = None,
        reservations: ReservationStore | None = None,
    ):
        """
        Initialize the budget enforcement service.

        Args:
            db_session: Optional injected database session
            pricing: Optional custom pricing service (for testing)
            grace_window: Optional injected grace window (for testing). When
                omitted, one is built lazily from config so the Redis client is
                not created until a failure actually occurs.
            reservations: Optional injected reservation store (Issue #4287, for
                testing). When omitted, one is built lazily from config.
        """
        self.db_session = db_session
        self._pricing = pricing or pricing_service
        self._grace_window = grace_window
        self._reservations = reservations

    def _get_grace_window(self) -> GraceWindow:
        """Get (or lazily build) the grace window.

        Built lazily so the healthy path never touches Redis — the window is
        only consulted when a ledger read has already failed.
        """
        if self._grace_window is None:
            from src.shared.config import get_settings

            redis_url = None
            if budget_config.budget_grace_window_backend == "redis":
                redis_url = get_settings().redis_url

            self._grace_window = GraceWindow(
                grace_seconds=budget_config.budget_fail_open_grace_seconds,
                redis_url=redis_url,
            )
        return self._grace_window

    def _get_reservations(self) -> ReservationStore:
        """Get (or lazily build) the live-denominator reservation store (#4287)."""
        if self._reservations is None:
            from src.shared.config import get_settings

            redis_url = None
            if budget_config.budget_reservation_backend == "redis":
                redis_url = get_settings().redis_url

            self._reservations = ReservationStore(
                redis_url=redis_url,
                ttl_seconds=budget_config.budget_reservation_ttl_seconds,
            )
        return self._reservations

    async def _handle_check_failure(self, exc: Exception, check_name: str) -> EnforcementResult:
        """Decide what to do when the budget check itself failed (Issue #4075).

        Three outcomes:

        * fail mode is explicitly ``open`` → allow (this is the rollback lever)
        * unexpected exception class → allow + high-severity signal, because a
          deterministic code bug must not be able to permanently down all
          inference (it would recur on every request and outlive any window)
        * infrastructure fault → allow while inside the bounded grace window,
          deny once it expires

        Args:
            exc: The exception raised by the check
            check_name: Which check failed, for log attribution

        Returns:
            EnforcementResult carrying the decision and its reason
        """
        environment = self._get_environment()

        # Explicit fail-open: the documented rollback path. Kept honest — this
        # is NOT reported as a grace-window allow.
        if budget_config.budget_fail_mode.lower() == "open":
            logger.error(f"{check_name} failed (fail_mode=open, allowing): {exc}", exc_info=True)
            emit_budget_check_failure(
                fault_class="infrastructure" if isinstance(exc, _INFRASTRUCTURE_FAULTS) else "unexpected",
                outcome="allowed_fail_open",
                environment=environment,
            )
            return EnforcementResult(
                allowed=True,
                warnings=[f"Budget check failed: {str(exc)}"],
            )

        # A code bug, not an outage. Failing closed here would be a permanent
        # total outage that no grace window rescues, so allow and shout.
        if not isinstance(exc, _INFRASTRUCTURE_FAULTS):
            logger.error(
                f"{check_name} raised an unexpected {type(exc).__name__} — allowing request and alarming. "
                f"This is a code defect, not an outage: {exc}",
                exc_info=True,
            )
            emit_budget_check_failure(
                fault_class="unexpected",
                outcome="allowed_fail_open",
                environment=environment,
            )
            return EnforcementResult(
                allowed=True,
                warnings=[f"Budget check error ({type(exc).__name__}): {str(exc)}"],
            )

        # Transient infrastructure fault — consult the bounded window.
        within_grace = await self._get_grace_window().register_failure()

        if within_grace:
            logger.error(
                f"{check_name} failed (infrastructure fault, allowing under grace window): {exc}",
                exc_info=True,
            )
            emit_budget_grace_engaged(1, environment=environment)
            emit_budget_check_failure(
                fault_class="infrastructure",
                outcome="allowed_under_grace",
                environment=environment,
            )
            return EnforcementResult(
                allowed=True,
                grace_engaged=True,
                warnings=[f"Budget check unavailable, allowed under grace window: {str(exc)}"],
            )

        logger.error(
            f"{check_name} failed and the grace window has expired — denying: {exc}",
            exc_info=True,
        )
        emit_budget_grace_engaged(1, environment=environment)
        emit_budget_check_failure(
            fault_class="infrastructure",
            outcome="denied",
            environment=environment,
        )
        return EnforcementResult(
            allowed=False,
            deny_reason=DenyReason.CHECK_UNAVAILABLE,
            blocked_reason=f"Budget check failed: {str(exc)}",
        )

    async def _note_check_succeeded(self) -> None:
        """Reset the grace window after a healthy ledger read.

        The window tracks CONSECUTIVE failures. Without this reset, unrelated
        blips hours apart would accumulate into one long-expired streak and the
        first failure after a healthy week would deny outright. Also emits the
        healthy-path 0 so the alarm can transition (see emit_budget_grace_engaged).
        """
        emit_budget_grace_engaged(0, environment=self._get_environment())

        window = self._grace_window
        if window is not None:
            await window.clear()

    @staticmethod
    def _get_environment() -> str:
        """Environment name for metric dimensions.

        Read straight from the env var (same pattern as shared/tracing.py) —
        Settings has no environment field.
        """
        return os.environ.get("BG_ENVIRONMENT", "dev")

    @asynccontextmanager
    async def _get_session(self) -> AsyncIterator[AsyncSession]:
        """Get database session as async context manager.

        For IAM auth, resets the engine before each session to ensure
        a fresh IAM token — same pattern as get_db() in the main app.
        Without this, pooled connections use stale tokens and fail with
        'PAM authentication failed'.
        """
        if self.db_session:
            yield self.db_session
        else:
            from src.shared.config import get_settings

            settings = get_settings()
            if settings.rds_iam_auth and settings.rds_host:
                reset_engine()
            factory = get_session_factory()
            async with factory() as session:
                yield session

    def _get_entity_hierarchy(self, context: TokenContext) -> list[tuple[EntityType, str]]:
        """
        Get entity hierarchy for budget checking.

        Returns entities in order from most specific to most general:
        user/service_account → team → department → organization

        Issue #4132: the org level uses attributed_org_id. Budget is a spend
        ledger, so its denominator must follow attribution — a hosted run's
        spend belongs to the tenant that triggered it. Leaving this on the
        authenticated org_id would charge every hosted run to __platform__ and
        leave per-tenant caps unenforced.

        Args:
            context: Token context with user hierarchy info

        Returns:
            List of (EntityType, entity_id) tuples
        """
        entities = []

        # User or service account level
        if context.account_type == "service":
            entities.append((EntityType.SERVICE_ACCOUNT, context.user_id))
        else:
            entities.append((EntityType.USER, context.user_id))

        # Team level
        if context.team_id:
            entities.append((EntityType.TEAM, context.team_id))

        # Department level
        if context.department_id:
            entities.append((EntityType.DEPARTMENT, context.department_id))

        # Organization level (attribution — see docstring)
        if context.attributed_org_id:
            entities.append((EntityType.ORGANIZATION, context.attributed_org_id))

        return entities

    async def check_budget_hierarchy(
        self,
        context: TokenContext,
        estimated_cost: Decimal,
        request_id: str | None = None,
    ) -> EnforcementResult:
        """
        Check budget constraints across the entire hierarchy.

        Traverses user → team → department → org, checking each level.
        Returns immediately if a hard limit is exceeded.
        Accumulates warnings for soft limit breaches.

        On failure (Issue #4075) this fails CLOSED by default: if the ledger
        cannot be read, the request is denied rather than admitted, because
        admitting it means uncapped spend no cap will stop. A bounded,
        alarmed grace window keeps a transient DB/IAM blip from hard-downing
        all inference — see _handle_check_failure for the full policy.

        Issue #4287: the settled ledger is eventually consistent — spend only
        materializes once the budget-usage-tracker Lambda processes the chat log,
        minutes later. So passing every DB check is necessary but not sufficient:
        a burst of concurrent requests all read the same stale total and
        collectively exceed a cap each one individually passed. Once the DB checks
        pass, this takes an atomic reservation against the live in-flight counter
        so concurrent requests contend on a current figure. See reservations.py.

        Args:
            context: Token context with user hierarchy info
            estimated_cost: Estimated cost for this request
            request_id: Request identifier, used as the reservation's idempotency
                key so completion can adjust this request's own reservation.

        Returns:
            EnforcementResult indicating if request is allowed
        """
        if not budget_config.budget_check_enabled:
            return EnforcementResult(allowed=True)

        try:
            async with self._get_session() as session:
                entities = self._get_entity_hierarchy(context)
                all_warnings = []
                reservation_targets: list[ReservationTarget] = []

                # Check each entity in the hierarchy
                for entity_type, entity_id in entities:
                    # Check all period types (daily, weekly, monthly)
                    for period_type in [
                        PeriodType.DAILY,
                        PeriodType.WEEKLY,
                        PeriodType.MONTHLY,
                    ]:
                        result, target = await self._check_entity_budget(
                            session,
                            entity_type,
                            entity_id,
                            period_type,
                            estimated_cost,
                            # Issue #4132: ledger partition must match the
                            # attributed tenant whose rows we are checking.
                            context.attributed_org_id,
                        )

                        if not result.allowed:
                            # Hard limit exceeded - block immediately. The
                            # ledger read itself succeeded, so this counts as a
                            # healthy check for grace-window purposes.
                            await self._note_check_succeeded()
                            return result

                        if target is not None:
                            reservation_targets.append(target)

                        # Accumulate warnings from soft limits
                        if result.warnings:
                            all_warnings.extend(result.warnings)

                # All checks passed — the ledger is readable, so reset the
                # consecutive-failure window and emit the healthy-path 0.
                await self._note_check_succeeded()

        except Exception as e:
            return await self._handle_check_failure(e, "Budget check")

        # Issue #4287: the live-denominator gate. Deliberately OUTSIDE the try
        # above: a reservation fault is not a ledger-read fault, and routing it
        # into _handle_check_failure would let a Redis blip burn the DB grace
        # window and then deny all traffic. It degrades instead — see
        # _reserve_or_degrade.
        reservation_denial = await self._reserve_or_degrade(request_id, estimated_cost, reservation_targets)
        if reservation_denial is not None:
            return reservation_denial

        return EnforcementResult(allowed=True, warnings=all_warnings)

    async def _reserve_or_degrade(
        self,
        request_id: str | None,
        estimated_cost: Decimal,
        targets: list[ReservationTarget],
    ) -> EnforcementResult | None:
        """Take a live reservation, or degrade to the settled-ledger verdict.

        Issue #4287. Three outcomes:

        * reservations disabled / no capped budget / Redis unreachable → ``None``
          (the Wave 1 DB verdict stands unchanged). Redis is a NEW hot-path
          dependency here, and a blip on it must never 503 inference nor consume
          the DB grace window.
        * live denominator has room → ``None`` (allow)
        * live denominator exhausted → a 402 ``BUDGET_EXCEEDED`` denial

        Returns:
            A denial result, or ``None`` to leave the caller's verdict alone.
        """
        if not budget_config.budget_reservation_enabled or not targets or request_id is None:
            return None

        store = self._get_reservations()
        if not store.enabled:
            return None

        environment = self._get_environment()
        outcome = await store.reserve(request_id, estimated_cost, targets)

        if outcome is None:
            # Redis unavailable. Degrade to the settled-ledger check (lagged
            # denominator, still fail-closed on the ledger itself) and alarm.
            emit_budget_reservation_outcome(outcome="degraded", environment=environment)
            return None

        if outcome.admitted:
            emit_budget_reservation_outcome(outcome="reserved", environment=environment)
            return None

        exhausted = outcome.exhausted
        assert exhausted is not None  # denials always name the exhausted budget
        emit_budget_reservation_outcome(outcome="denied", environment=environment)
        logger.warning(
            f"Budget exceeded (in-flight reservations): {exhausted.entity_type} {exhausted.entity_id} "
            f"- {exhausted.period_type} settled headroom ${exhausted.headroom_usd}, request estimate ${estimated_cost}"
        )
        return EnforcementResult(
            allowed=False,
            deny_reason=DenyReason.BUDGET_EXCEEDED,
            blocked_reason=f"Budget exceeded for {exhausted.entity_type} {exhausted.entity_id} (including in-flight spend)",
            exceeded_entity_type=EntityType(exhausted.entity_type),
            exceeded_entity_id=exhausted.entity_id,
            enforcement_mode=EnforcementMode.HARD,
        )

    async def reconcile_reservation(
        self,
        context: TokenContext,
        request_id: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Adjust this request's reservation from estimate to settled actual (#4287).

        Called from the proxy's ``_log_usage``, which runs in a ``finally`` on
        every path — so this fires on success AND on failure. A request that
        raised logs ~zero tokens, so its reservation adjusts to ~zero and the
        headroom it was holding is returned.

        Idempotent on ``request_id``: rerunning it overwrites the same value
        rather than debiting twice.

        No DB read is needed. The reconcile script only touches hash fields that
        already exist, so enumerating the full hierarchy × period grid is safe —
        entities without a reservation are no-ops.
        """
        if not budget_config.budget_reservation_enabled:
            return

        store = self._get_reservations()
        if not store.enabled:
            return

        actual_cost = self._pricing.calculate_cost(model_id, input_tokens, output_tokens)

        targets = [
            ReservationTarget(
                org_id=context.attributed_org_id,
                entity_type=entity_type.value,
                entity_id=entity_id,
                period_type=period_type.value,
                period_start=get_period_start_end(period_type)[0].isoformat(),
                # Unused on the reconcile path: the script overwrites an existing
                # amount and never re-evaluates headroom.
                headroom_usd=Decimal("0"),
            )
            for entity_type, entity_id in self._get_entity_hierarchy(context)
            for period_type in (PeriodType.DAILY, PeriodType.WEEKLY, PeriodType.MONTHLY)
        ]

        await store.reconcile(request_id, actual_cost, targets)

    async def _check_entity_budget(
        self,
        session: AsyncSession,
        entity_type: EntityType,
        entity_id: str,
        period_type: PeriodType,
        estimated_cost: Decimal,
        org_id: str,
    ) -> tuple[EnforcementResult, ReservationTarget | None]:
        """
        Check budget for a specific entity and period.

        Args:
            session: Database session
            entity_type: Type of entity (user, team, dept, org)
            entity_id: Entity identifier
            period_type: Budget period type
            estimated_cost: Estimated cost for request
            org_id: Organization ID for tenant isolation

        Returns:
            Tuple of (EnforcementResult for this entity/period, reservation
            target). Issue #4287: the second element is the live-denominator
            reservation this budget needs, carrying the settled headroom this
            read just observed — or ``None`` when there is nothing to reserve
            against (no budget row, or a soft limit, which by definition does
            not block).
        """
        # Get budget configuration
        budget_result = await session.execute(
            select(BudgetConfig).where(
                and_(
                    BudgetConfig.org_id == org_id,
                    BudgetConfig.entity_type == entity_type.value,
                    BudgetConfig.entity_id == entity_id,
                    BudgetConfig.period_type == period_type.value,
                )
            )
        )
        budget = budget_result.scalar_one_or_none()

        if not budget:
            # No budget configured for this entity/period - allow
            return EnforcementResult(allowed=True), None

        # Get current usage
        period_start, period_end = get_period_start_end(period_type)
        usage_result = await session.execute(
            select(BudgetUsage).where(
                and_(
                    BudgetUsage.org_id == org_id,
                    BudgetUsage.entity_type == entity_type.value,
                    BudgetUsage.entity_id == entity_id,
                    BudgetUsage.period_type == period_type.value,
                    BudgetUsage.period_start == period_start,
                )
            )
        )
        usage = usage_result.scalar_one_or_none()

        current_spend = usage.total_cost_usd if usage else Decimal("0")
        projected_spend = current_spend + estimated_cost
        enforcement_mode = EnforcementMode(budget.enforcement_mode)

        # Check if budget would be exceeded
        if projected_spend > budget.budget_amount_usd:
            if enforcement_mode == EnforcementMode.HARD:
                # Hard limit - block request
                logger.warning(
                    f"Budget exceeded (hard limit): {entity_type.value} {entity_id} "
                    f"- {period_type.value} budget ${budget.budget_amount_usd}, "
                    f"current ${current_spend}, projected ${projected_spend}"
                )
                return (
                    EnforcementResult(
                        allowed=False,
                        deny_reason=DenyReason.BUDGET_EXCEEDED,
                        blocked_reason=f"Budget exceeded for {entity_type.value} {entity_id}",
                        exceeded_entity_type=entity_type,
                        exceeded_entity_id=entity_id,
                        budget_amount_usd=budget.budget_amount_usd,
                        current_spend_usd=current_spend,
                        enforcement_mode=enforcement_mode,
                    ),
                    None,
                )
            else:
                # Soft limit - warn and continue
                logger.info(
                    f"Budget exceeded (soft limit): {entity_type.value} {entity_id} "
                    f"- {period_type.value} budget ${budget.budget_amount_usd}, "
                    f"current ${current_spend}, projected ${projected_spend}"
                )
                return (
                    EnforcementResult(
                        allowed=True,
                        warnings=[
                            f"Budget exceeded for {entity_type.value} {entity_id} "
                            f"({period_type.value}): ${projected_spend:.2f} / ${budget.budget_amount_usd:.2f}"
                        ],
                    ),
                    None,
                )

        # Issue #4287: this budget passed against the SETTLED total, so it is a
        # candidate for the live-denominator gate. Only hard limits get one — a
        # soft limit never blocks, so reserving against it would consume headroom
        # nothing is ever going to enforce.
        target = None
        if enforcement_mode == EnforcementMode.HARD:
            target = ReservationTarget(
                org_id=org_id,
                entity_type=entity_type.value,
                entity_id=entity_id,
                period_type=period_type.value,
                period_start=period_start.isoformat(),
                headroom_usd=budget.budget_amount_usd - current_spend,
            )

        # Check for warning threshold
        utilization = calculate_budget_utilization(budget.budget_amount_usd, projected_spend)
        warnings = []

        if utilization >= budget_config.budget_critical_threshold_percent:
            warnings.append(f"{entity_type.value} {entity_id} {period_type.value} budget at {utilization:.1f}% (critical)")
        elif utilization >= budget_config.budget_warning_threshold_percent:
            warnings.append(f"{entity_type.value} {entity_id} {period_type.value} budget at {utilization:.1f}%")

        return EnforcementResult(allowed=True, warnings=warnings), target

    async def record_usage(
        self,
        context: TokenContext,
        input_tokens: int,
        output_tokens: int,
        model_id: str,
    ) -> None:
        """
        Record usage to all hierarchy levels after request completion.

        Args:
            context: Token context with user hierarchy info
            input_tokens: Number of input tokens used
            output_tokens: Number of output tokens generated
            model_id: Model ID used for the request
        """
        if not budget_config.cost_calculation_enabled:
            return

        try:
            # Calculate actual cost
            cost = self._pricing.calculate_cost(model_id, input_tokens, output_tokens)

            logger.debug(f"Recording usage: user={context.user_id}, tokens_in={input_tokens}, tokens_out={output_tokens}, cost=${cost:.6f}")

            async with self._get_session() as session:
                entities = self._get_entity_hierarchy(context)

                # Record to each entity in the hierarchy
                for entity_type, entity_id in entities:
                    await self._record_entity_usage(
                        session,
                        entity_type,
                        entity_id,
                        # Issue #4132: spend is recorded against the attributed
                        # tenant, matching _get_entity_hierarchy's org level.
                        context.attributed_org_id,
                        input_tokens,
                        output_tokens,
                        cost,
                    )

                await session.commit()

        except Exception as e:
            logger.error(f"Failed to record usage: {e}", exc_info=True)
            # Don't raise - usage recording failure shouldn't block the response

    async def _record_entity_usage(
        self,
        session: AsyncSession,
        entity_type: EntityType,
        entity_id: str,
        org_id: str,
        input_tokens: int,
        output_tokens: int,
        cost: Decimal,
    ) -> None:
        """
        Record usage for a single entity across all period types.

        Args:
            session: Database session
            entity_type: Type of entity
            entity_id: Entity identifier
            org_id: Organization ID
            input_tokens: Input tokens used
            output_tokens: Output tokens generated
            cost: Total cost for this request
        """
        total_tokens = input_tokens + output_tokens

        for period_type in [PeriodType.DAILY, PeriodType.WEEKLY, PeriodType.MONTHLY]:
            period_start, _ = get_period_start_end(period_type)

            # Get or create usage record
            result = await session.execute(
                select(BudgetUsage).where(
                    and_(
                        BudgetUsage.org_id == org_id,
                        BudgetUsage.entity_type == entity_type.value,
                        BudgetUsage.entity_id == entity_id,
                        BudgetUsage.period_type == period_type.value,
                        BudgetUsage.period_start == period_start,
                    )
                )
            )
            usage = result.scalar_one_or_none()

            if not usage:
                usage = BudgetUsage(
                    org_id=org_id,
                    entity_type=entity_type.value,
                    entity_id=entity_id,
                    period_start=period_start,
                    period_type=period_type.value,
                    total_cost_usd=Decimal("0"),
                    total_tokens=0,
                    request_count=0,
                )
                session.add(usage)

            # Update usage
            usage.total_cost_usd += cost
            usage.total_tokens += total_tokens
            usage.request_count += 1

    async def get_budget_status_for_headers(self, context: TokenContext) -> dict[str, Any]:
        """
        Get budget status info for response headers.

        Returns the most restrictive (lowest remaining) budget across the hierarchy.

        Args:
            context: Token context with user hierarchy info

        Returns:
            Dict with budget_limit, budget_remaining, budget_reset
        """
        try:
            async with self._get_session() as session:
                entities = self._get_entity_hierarchy(context)

                lowest_remaining = None
                corresponding_limit = None
                corresponding_reset = None

                for entity_type, entity_id in entities:
                    # Check monthly budget (most common)
                    period_type = PeriodType.MONTHLY
                    period_start, period_end = get_period_start_end(period_type)

                    # Get budget config. Issue #4132: same attributed-tenant
                    # partition as the check/record paths, so the headers
                    # describe the ledger actually being enforced.
                    budget_result = await session.execute(
                        select(BudgetConfig).where(
                            and_(
                                BudgetConfig.org_id == context.attributed_org_id,
                                BudgetConfig.entity_type == entity_type.value,
                                BudgetConfig.entity_id == entity_id,
                                BudgetConfig.period_type == period_type.value,
                            )
                        )
                    )
                    budget = budget_result.scalar_one_or_none()

                    if not budget:
                        continue

                    # Get current usage
                    usage_result = await session.execute(
                        select(BudgetUsage).where(
                            and_(
                                BudgetUsage.org_id == context.attributed_org_id,
                                BudgetUsage.entity_type == entity_type.value,
                                BudgetUsage.entity_id == entity_id,
                                BudgetUsage.period_type == period_type.value,
                                BudgetUsage.period_start == period_start,
                            )
                        )
                    )
                    usage = usage_result.scalar_one_or_none()

                    current_spend = usage.total_cost_usd if usage else Decimal("0")
                    remaining = budget.budget_amount_usd - current_spend

                    # Track the most restrictive (lowest remaining)
                    if lowest_remaining is None or remaining < lowest_remaining:
                        lowest_remaining = remaining
                        corresponding_limit = budget.budget_amount_usd
                        corresponding_reset = period_end

                if lowest_remaining is not None:
                    return {
                        "budget_limit": float(corresponding_limit),
                        "budget_remaining": float(max(Decimal("0"), lowest_remaining)),
                        "budget_reset": corresponding_reset.isoformat(),
                    }

                return {}

        except Exception as e:
            logger.error(f"Failed to get budget status for headers: {e}")
            return {}

    def estimate_request_cost(self, model_id: str, request_body: dict[str, Any]) -> Decimal:
        """
        Estimate the cost of a request before execution.

        Args:
            model_id: Model ID for the request
            request_body: Request body dictionary

        Returns:
            Estimated cost in USD
        """
        return self._pricing.estimate_request_cost(model_id, request_body)

    def estimate_cost_from_payload_size(self, model_id: str, content_length: int) -> Decimal:
        """
        Estimate the cost of a request from its model and serialized size.

        Issue #4287: the pre-request estimate for the enforced proxy paths, where
        the middleware cannot read the body. See
        ``PricingService.estimate_cost_from_payload_size``.

        Args:
            model_id: Model ID for the request
            content_length: Value of the request's ``content-length`` header

        Returns:
            Estimated cost in USD
        """
        return self._pricing.estimate_cost_from_payload_size(model_id, content_length)

    async def check_agent_budget(
        self,
        budget_config_id: str,
        estimated_cost: Decimal,
    ) -> EnforcementResult:
        """
        Check agent-level budget by budget_config_id.

        Issue #249: Agent budgets are checked directly by budget_config_id
        (passed from Lambda authorizer via X-Agent-BudgetConfigId header).
        This allows agent-level enforcement BEFORE the team/org hierarchy.

        Args:
            budget_config_id: Budget config ID from agent registry
            estimated_cost: Estimated cost for this request

        Returns:
            EnforcementResult indicating if request is allowed
        """
        if not budget_config.budget_check_enabled:
            return EnforcementResult(allowed=True)

        try:
            async with self._get_session() as session:
                # Get budget config directly by ID
                budget_result = await session.execute(select(BudgetConfig).where(BudgetConfig.id == budget_config_id))
                budget = budget_result.scalar_one_or_none()

                if not budget:
                    # No budget config found - allow request
                    logger.warning(f"Budget config not found: {budget_config_id}")
                    return EnforcementResult(allowed=True)

                # Get current usage for the budget period
                period_type = PeriodType(budget.period_type)
                period_start, _ = get_period_start_end(period_type)

                usage_result = await session.execute(
                    select(BudgetUsage).where(
                        and_(
                            BudgetUsage.org_id == budget.org_id,
                            BudgetUsage.entity_type == budget.entity_type,
                            BudgetUsage.entity_id == budget.entity_id,
                            BudgetUsage.period_type == budget.period_type,
                            BudgetUsage.period_start == period_start,
                        )
                    )
                )
                usage = usage_result.scalar_one_or_none()

                current_spend = usage.total_cost_usd if usage else Decimal("0")
                projected_spend = current_spend + estimated_cost

                # Check if budget would be exceeded
                if projected_spend > budget.budget_amount_usd:
                    enforcement_mode = EnforcementMode(budget.enforcement_mode)

                    if enforcement_mode == EnforcementMode.HARD:
                        logger.warning(
                            f"Agent budget exceeded (hard limit): config_id={budget_config_id}, "
                            f"budget=${budget.budget_amount_usd}, current=${current_spend}, projected=${projected_spend}"
                        )
                        return EnforcementResult(
                            allowed=False,
                            deny_reason=DenyReason.BUDGET_EXCEEDED,
                            blocked_reason=f"Agent budget exceeded (config_id={budget_config_id})",
                            exceeded_entity_type=EntityType(budget.entity_type),
                            exceeded_entity_id=budget.entity_id,
                            budget_amount_usd=budget.budget_amount_usd,
                            current_spend_usd=current_spend,
                            enforcement_mode=enforcement_mode,
                        )
                    else:
                        logger.info(
                            f"Agent budget exceeded (soft limit): config_id={budget_config_id}, "
                            f"budget=${budget.budget_amount_usd}, current=${current_spend}, projected=${projected_spend}"
                        )
                        return EnforcementResult(
                            allowed=True,
                            warnings=[
                                f"Agent budget exceeded (config_id={budget_config_id}): ${projected_spend:.2f} / ${budget.budget_amount_usd:.2f}"
                            ],
                        )

                # Check warning threshold
                utilization = calculate_budget_utilization(budget.budget_amount_usd, projected_spend)
                warnings = []

                if utilization >= budget_config.budget_critical_threshold_percent:
                    warnings.append(f"Agent budget at {utilization:.1f}% (critical)")
                elif utilization >= budget_config.budget_warning_threshold_percent:
                    warnings.append(f"Agent budget at {utilization:.1f}%")

                await self._note_check_succeeded()
                return EnforcementResult(allowed=True, warnings=warnings)

        except Exception as e:
            # Issue #4075 (D7): this carried an identical fail-open defect to
            # check_budget_hierarchy. It is unreachable today (#3985 removed
            # the header that drove it) but it is live code the per-agent
            # budget work will re-wire, so it gets the same policy rather than
            # being left as a second silent hole.
            return await self._handle_check_failure(e, "Agent budget check")


# Global enforcement service instance
budget_enforcement_service = BudgetEnforcementService()


async def reconcile_budget_reservation(
    context: TokenContext,
    request_id: str | None,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Metering-side entry point for reservation reconciliation (Issue #4287).

    Called from the proxy's usage-logging ``finally`` blocks. Kept as a
    module-level function taking the global service so the metering path does not
    have to own a service instance, and so it can be patched in one place.

    Swallows everything. This runs after the response has been produced: a
    reconcile failure must never surface to the caller, and the reservation's own
    expiry already bounds the cost of losing one.
    """
    if request_id is None:
        # Without an idempotency key there is no reservation to find. The check
        # skips reserving in this case too, so there is nothing to release.
        return

    try:
        await budget_enforcement_service.reconcile_reservation(
            context=context,
            request_id=request_id,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except Exception as exc:
        logger.warning(f"Budget reservation reconcile failed: {exc}")
