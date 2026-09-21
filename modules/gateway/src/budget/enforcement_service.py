"""
Budget Enforcement Service.

This module provides cascading budget enforcement logic for the proxy path.
It traverses the entity hierarchy (user → team → department → org) and
checks budget constraints at each level.
"""

import os
import time
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
    emit_person_budget_layer_skipped,
    emit_run_binding_drift,
)
from src.shared.models.budget import BudgetConfig, BudgetUsage, PersonBudgetConfig, PersonBudgetDefault
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import (
    DenyReason,
    EnforcementMode,
    EnforcementResult,
    EntityType,
    PeriodType,
)

from .config import budget_config
from .enforcement_settings import BudgetAccountingGap, flow_key, read_enforcement
from .grace_window import GraceWindow

# `person_ledger` is a deliberate LEAF (its own docstring): models and shared
# schemas only, no router/service/auth, so a module-level import here cannot
# reintroduce a cycle. Only the TYPE is imported at module level — the ladder
# FUNCTIONS stay function-local, for symmetry with `person_anchor.py`'s own note
# and so the import list at each call site names exactly what that path reads.
from .person_ledger import PersonLimit
from .pricing import PricingService, pricing_service
from .reservations import ReservationStore, ReservationTarget
from .run_binding import RunBinding, RunBindingError, RunBindingResolver, resolve_run_binding
from .utils import (
    CALENDAR_PERIOD_TYPES,
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

# Issue #4344: the namespace prefix for a SERVICE-rooted root principal.
#
# The lineage plane writes both kinds of root principal into one field: a human
# chain carries a canonical `users.id`, while a service-rooted chain (EventBridge /
# scheduled / CI / alarm) carries a service identity key such as
# `eventbridge:adp-dev-high-error-rate` (webhook-ingress
# `lambda/eventbridge/handler.py`). Writing both verbatim as the ROOT_USER entity id
# puts two identifier namespaces in one column under a UniqueConstraint — the exact
# collision the EntityType comment in `shared/schemas/budget.py` splits entity values
# to avoid, reappearing INSIDE `root_user`.
#
# Only the service side is prefixed. Human ids stay BARE, which keeps every
# human-rooted key byte-identical to #4300 — the settled `root_user` ledger rows the
# tracker Lambda has already written stay addressable, so this needs no migration and
# no backfill. Collision-freedom does not depend on the prefix being unguessable: a
# canonical `users.id` is a generated UUID (`shared/models/organization.py`), which
# contains no colon, so no bare human id can ever equal a `service:`-qualified value.
_SERVICE_PRINCIPAL_PREFIX = "service:"

# How long the "any person caps exist?" verdict may be reused per process (review
# fix on #4689). Bounded staleness in both directions — see _any_person_caps_exist.
_PERSON_CAPS_EXISTENCE_TTL_SECONDS = 60.0


def _qualify_root_principal_id(root_principal_id: str, *, is_human_rooted: bool | None) -> str:
    """Namespace-qualify a root principal id by principal kind (Issue #4344).

    Args:
        root_principal_id: The raw ``root_human_id`` off the run's registry row.
            May be a canonical ``users.id`` or a service identity key.
        is_human_rooted: The row's flag. ``None`` means the row carried none.

    Returns:
        ``""`` for an empty input — empty stays empty, because a qualified empty
        (``"service:"``) would be a brand-new sentinel that collapses every
        unattributed request in a tenant into one shared bogus ledger line, which is
        the specific outcome #4300's empty-check exists to prevent.

        Otherwise the bare id when the principal is a human, or a
        ``service:``-prefixed id when it is not.

    ``None`` resolves to SERVICE, never human (D4b). That default is the whole
    safety property: the failure this function prevents is a service key being
    treated as a canonical ``users.id``, so an unknown kind must fall on the service
    side. Defaulting the other way would leave every row that predates the flag —
    and every writer that omits it — landing in exactly the namespace being
    protected, and the bug would look fixed.
    """
    if not root_principal_id:
        return ""
    if is_human_rooted is True:
        return root_principal_id
    return f"{_SERVICE_PRINCIPAL_PREFIX}{root_principal_id}"


def _unqualify_root_principal_id(entity_id: str) -> str:
    """Strip the service namespace back off, for comparison only (Issue #4344).

    Used by the ROOT_USER dedup guard, which asks "is the root principal the same
    party as the authenticated caller?". That question is about the PRINCIPAL, not
    about the namespace it was written in: for a service-rooted run the registry row
    puts the same service identity key in both ``user_id`` and ``root_human_id``
    (webhook-ingress `eventbridge/handler.py` passes it as both), so comparing the
    qualified id against a bare ``user_id`` would find them different and add a
    SECOND budget line for one principal — consuming its headroom at 2x rate, the
    exact defect the guard was added to prevent.
    """
    if entity_id.startswith(_SERVICE_PRINCIPAL_PREFIX):
        return entity_id[len(_SERVICE_PRINCIPAL_PREFIX) :]
    return entity_id


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
        run_bindings: RunBindingResolver | None = None,
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
            run_bindings: Optional injected run-identity resolver (Issue #4187,
                for testing). When omitted, one is built lazily from config.
        """
        self.db_session = db_session
        # ``((individual_caps_exist, defaults_exist), monotonic_stamp)`` — see
        # ``_person_limit_sources_exist``. A PAIR since #4690: a default rule is a
        # person limit too, but it gates strictly more work than an individual cap.
        self._person_caps_exist_cache: tuple[tuple[bool, bool], float] | None = None
        self._pricing = pricing or pricing_service
        self._grace_window = grace_window
        self._reservations = reservations
        self._run_bindings = run_bindings

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

    def _get_run_bindings(self) -> RunBindingResolver:
        """Get (or lazily build) the server-side run-identity resolver (#4187)."""
        if self._run_bindings is None:
            from src.shared.config import get_settings

            settings = get_settings()
            self._run_bindings = RunBindingResolver(
                table_name=settings.webhook_events_table,
                aws_region=settings.aws_region,
                redis_url=settings.redis_url,
                cache_ttl_seconds=budget_config.budget_run_cap_ttl_seconds,
            )
        return self._run_bindings

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

        Issue #4300: the root-human level applies the same reasoning one tier
        down. ``user_id`` for a hosted run is the agent's own service account, so
        without this entity a human can trigger a chain of N sub-agents that each
        sit inside their own cap and collectively blow the human's envelope, with
        the human's budget line never registering any of it.

        This entity belongs HERE rather than in ``_scope_targets`` because it is a
        CUMULATIVE per-period budget with a settled Postgres ledger behind it (the
        tracker Lambda writes a ``root_user`` row), so it needs
        ``headroom = cap - settled`` — which is exactly what this path's
        ``_check_entity_budget`` computes. ``_scope_targets`` exists for run/chain
        precisely because those have NO settled ledger and need the full cap.

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

        # Root-principal level (attribution — see docstring). Issue #4300.
        #
        # Gated on `!= user_id` because when the caller IS the initiating principal,
        # their spend is already covered by the USER/SERVICE_ACCOUNT line above.
        # Adding a second entity would reserve the same cost twice against one party
        # (the two entity types are different Redis keys, so nothing dedupes them)
        # and consume their headroom at 2x rate.
        #
        # Issue #4345 — WHICH callers this skip can actually fire for. It is live for
        # direct callers and INERT for hosted agent runs; do not read it as protecting
        # the hosted path:
        #   * hosted agent run (the dominant path): `user_id` is the shared registry
        #     identity `scaledjob-worker`, while `attributed_user_id` is a canonical
        #     `users.id` UUID resolved server-side off the run-binding row. The two can
        #     never be equal, so the skip is a no-op here and the ROOT_USER line is
        #     always added — which is the point of #4300.
        #   * direct human caller: `user_id` IS the initiating human's id, so the skip
        #     fires and prevents the 2x debit above.
        #   * service-rooted run (EventBridge): the registry row names the same service
        #     key as both `user_id` and `root_human_id`, so the skip fires — see #4344
        #     below for why the comparison has to unqualify first.
        # Keep the guard: the two direct-caller cases are real and exercise it.
        #
        # Issue #4344: the comparison runs on the UNQUALIFIED id. `attributed_user_id`
        # is namespace-qualified for a service root, while `user_id` never is, so
        # comparing the two verbatim would report "different" for a service-rooted run
        # whose row names the same service key as both the caller and the root — and
        # reintroduce the 2x debit above. The entity id itself stays qualified: that is
        # the key the ledger and the tracker Lambda agree on.
        if context.attributed_user_id and _unqualify_root_principal_id(context.attributed_user_id) != context.user_id:
            entities.append((EntityType.ROOT_USER, context.attributed_user_id))

        # Organization level (attribution — see docstring)
        if context.attributed_org_id:
            entities.append((EntityType.ORGANIZATION, context.attributed_org_id))

        return entities

    async def _resolve_scope_cap(
        self,
        session: AsyncSession,
        org_id: str,
        entity_type: EntityType,
        *,
        approved_platform_cap: Decimal | None = None,
    ) -> Decimal:
        """Resolve the effective run or chain cap for a tenant (Issue #4187).

        Precedence, and why:

        1. The platform default (``budget_run_cap_usd`` / ``budget_chain_cap_usd``)
           is the baseline **and a hard upper bound**.
        2. A ``budget_configs`` row for this tenant may LOWER it.

        The clamp is the security property. The issue requires that a tenant
        cannot raise its own cap, and tenant admins can already write
        ``budget_configs`` rows for their own org — so without ``min()`` the
        control would be self-service. Raising a ceiling stays a platform action
        via the internal plane.

        A missing row, an unparseable amount, or a non-positive one all resolve to
        the platform default. **Never to unlimited** — that is the specific
        failure this issue exists to prevent, so every error path here lands on a
        finite number.

        No migration is involved: this reuses ``budget_configs`` with
        ``entity_type`` of ``"run"``/``"chain"`` and ``period_type="run"``, all of
        which fit the existing ``String(20)``/``String(10)`` columns, and the table
        has only a ``UniqueConstraint`` — no CHECK pins the enum.
        """
        platform_default = budget_config.budget_run_cap_usd if entity_type == EntityType.RUN else budget_config.budget_chain_cap_usd
        # Only the authenticated shared-worker path supplies this value, from a
        # platform-admin receipt bound to its exact flow and accepted plan. It
        # replaces the platform ceiling, never a tenant-authored tighter limit.
        if approved_platform_cap is not None:
            if not approved_platform_cap.is_finite() or approved_platform_cap <= 0:
                raise ValueError("Invalid approved platform cap")
            platform_default = approved_platform_cap

        try:
            result = await session.execute(
                select(BudgetConfig).where(
                    and_(
                        BudgetConfig.org_id == org_id,
                        BudgetConfig.entity_type == entity_type.value,
                        # A tenant-wide override, not a per-run row: nobody
                        # provisions a budget row per run id.
                        BudgetConfig.entity_id == "*",
                        BudgetConfig.period_type == PeriodType.RUN.value,
                    )
                )
            )
            override = result.scalar_one_or_none()
        except _INFRASTRUCTURE_FAULTS:
            # Let the caller's fail-closed handler classify a ledger fault. It is
            # not this function's job to decide the outcome of an outage.
            raise

        if override is None:
            return platform_default

        try:
            configured = Decimal(override.budget_amount_usd)
        except (TypeError, ValueError, ArithmeticError):
            logger.warning(
                f"Unparseable {entity_type.value} cap for org {org_id} ({override.budget_amount_usd!r}) — using platform default ${platform_default}"
            )
            return platform_default

        if configured <= 0:
            logger.warning(f"Non-positive {entity_type.value} cap for org {org_id} (${configured}) — using platform default ${platform_default}")
            return platform_default

        # The clamp: a tenant may tighten its own ceiling, never loosen it.
        return min(configured, platform_default)

    async def _resolve_run_scope(self, context: TokenContext, run_id: str | None) -> RunBinding | None:
        """Bind the asserted run id to the authenticated caller (Issue #4187).

        Returns:
            The VERIFIED binding, in **both** modes, or ``None`` when there is no
            verified binding to report.

        Raises:
            RunBindingError: the run id is unknown or belongs to someone else, in
                ``enforce`` mode. The caller converts this into a 402.

        Issue #4591 — a verified binding is returned in shadow mode too. This used
        to end ``return binding if enforcing else None``, which conflated two
        separate questions: "did this run id verify?" and "may we deny on it yet?".
        Only the second is the #4337 rollout gate. The first also carries the
        #4300 root-human attribution, so discarding it in shadow starved the whole
        attribution chain (chat-log ``root_human_id`` → the tracker Lambda's
        ``root_user`` ledger row → every per-person cloud-agent budget authored via
        #4536) in every environment still in shadow — which is the shipped default.
        The caller now applies the mode gate to the run/chain CAP alone.

        ``None`` covers three cases, all of them deliberate:

        * the feature is off;
        * no run id was asserted AND the missing-header policy exempts this caller;
        * there is no verified binding to return — either the assertion failed
          verification in shadow mode (drift recorded, nothing denied) or the
          lookup faulted (DDB unreachable — degrade, do not deny).

        That last case is a hard boundary: attribution may only ever be published
        from a row this function VERIFIED. Returning an unverified or unresolved
        row here would let an agent pin its spend on an arbitrary human, which is
        exactly the forgery surface #4187/AD-1 closed.
        """
        if context.auth_source == "iam" and (
            context.user_id == "authority-worker" or (context.user_id == "scaledjob-worker" and context._protected_run_binding is not None)
        ):
            binding = context._protected_run_binding
            if binding is None or (run_id is not None and run_id != binding.run_id):
                raise RunBindingError("unverified_worker", "Protected worker identity is unavailable or mismatched.")
            return binding

        if not budget_config.budget_run_cap_enabled:
            # Issue #4591: the run-cap FEATURE being off must not starve
            # attribution — that is the same defect this issue fixes for shadow
            # mode, one flag over (the flag ships False in config.py, so an
            # unconditional `return None` here would make the shadow-mode fix a
            # no-op in any environment that never enabled run caps). A verified
            # binding is still resolved for its root_human_id; everything cap-
            # shaped stays off: no missing-id policy, no drift metrics (they
            # measure a rollout that is not happening), no denial ever.
            if not run_id:
                return None
            try:
                binding = await resolve_run_binding(
                    run_id=run_id,
                    caller_user_id=context.user_id,
                    caller_org_id=context.attributed_org_id,
                    resolver=self._get_run_bindings(),
                )
            except RunBindingError as exc:
                # Failed verification is never a source of attribution — see the
                # forgery boundary in the docstring. Logged (not drift-metered)
                # so an operator can still see refusals with the feature off.
                logger.info(f"Run id failed verification with run caps disabled (no attribution): run={run_id} reason={exc.reason}")
                return None
            # None here is a lookup fault: degrade to "no attribution".
            return binding

        enforcing = budget_config.budget_run_binding_mode.lower() == "enforce"

        if not run_id:
            # A missing run id is a DECLARED policy, never "absent -> unlimited".
            policy = budget_config.budget_run_id_required_mode.lower()
            require_all = policy == "require"
            # Issue #4337 D10a: the declared exemption for the audited no-row dispatch
            # paths (orchestration engine, GitLab; chat until its worker asserts an id).
            # All three authenticate as the IAM worker, so `exempt_human` does NOT
            # cover them — see the D10a table in `run_binding.py`.
            exempt_missing = policy == "exempt_missing"
            is_agent_caller = context.auth_source == "iam"
            deny = enforcing and not exempt_missing and (require_all or is_agent_caller)

            # Issue #4337 D10a: emitted in BOTH modes, and in shadow whether or not
            # the policy would have denied. Pre-#4337 this path returned before any
            # drift was recorded, so the no-row dispatch paths contributed ZERO drift
            # events in shadow and then 402'd on their first model call in enforce —
            # invisible in shadow, fatal in enforce. A shadow window reading "0 drift"
            # was fully consistent with those paths being wholly untested, which is
            # what made the original acceptance gate unable to detect its own failure.
            #
            # `outcome` distinguishes the declared dispositions, so an operator reading
            # the metric can tell "a path we exempted on purpose" from "a path that is
            # about to start failing". Emitting only the deny case would leave the
            # exemption itself unmeasured — and an exemption nobody can see is how a
            # path silently stops being capped.
            emit_run_binding_drift(
                reason="missing_run_id",
                environment=self._get_environment(),
                outcome="deny" if deny else "exempt",
            )

            if deny:
                raise RunBindingError(
                    "missing_run_id",
                    "A run id is required on this path; the request carried none.",
                )
            # Human/JWT callers are bounded by the per-user hierarchy caps, which
            # already ran. Nothing is uncapped here.
            return None

        try:
            binding = await resolve_run_binding(
                run_id=run_id,
                caller_user_id=context.user_id,
                caller_org_id=context.attributed_org_id,
                resolver=self._get_run_bindings(),
            )
        except RunBindingError as exc:
            # Shadow mode: record what a deny WOULD have rejected, deny nothing.
            #
            # Issue #4591: this returns ``None`` and must keep doing so. The row
            # behind this exception FAILED verification (unknown run id, or one
            # owned by another tenant/identity), so it is not a source of anything
            # — least of all attribution. Widening this to return the offending
            # row so shadow could "observe more" would hand an agent the ability
            # to name any human as the payer of its spend.
            if not enforcing:
                logger.warning(f"Run-binding drift (shadow mode, not denying): run={run_id} reason={exc.reason} caller={context.user_id}")
                emit_run_binding_drift(reason=exc.reason, environment=self._get_environment())
                return None
            emit_run_binding_drift(reason=exc.reason, environment=self._get_environment())
            raise

        if binding is None:
            # Lookup fault — the hierarchy caps still apply. Nothing verified, so
            # nothing to attribute either (Issue #4591): a registry outage must
            # degrade to "no attribution", never to a guessed one.
            return None

        # Verified. Returned in BOTH modes — see the docstring; the mode gate lives
        # on the caller's run/chain cap block, not here.
        return binding

    def _scope_targets(self, binding: RunBinding, run_cap: Decimal, chain_cap: Decimal) -> list[ReservationTarget]:
        """Build the run and chain reservation targets for a bound run (#4187).

        Both scopes are required and they fail differently: the run cap bounds one
        runaway loop, while the chain cap bounds the aggregate of a fan-out that
        can sit inside every per-run limit and still cost many times the intended
        total. An implementation with only the run scope has an obvious hole.

        ``headroom_usd`` is the FULL cap, not ``cap - settled_spend``: run and
        chain scopes have no settled Postgres ledger to subtract (the
        budget-usage-tracker Lambda writes no run rows), so the live reservation
        total in Redis *is* the whole denominator. That is exactly why this cap
        trips with no bridging job having run — and why a ``SUM(cost_usd)``
        implementation would read ~0 for the run that is currently overspending.

        Both targets carry the run-lifetime TTL rather than the #4287 default.

        Issue #4337 (B1): ``org_id`` is ``binding.tenant_id`` — the tenant
        webhook-ingress wrote on the run's row — and NOT
        ``context.attributed_org_id``, which is caller-influenced (#4132). The two
        are equal by the time we get here (``verify_row_matches_caller`` denies
        otherwise), so this is a provenance statement rather than a behaviour change:
        the ledger these keys address partitions on a server-written value, so no
        future relaxation of the assertion check can silently move a run's ledger to
        a caller-chosen tenant. There is no ``or "unknown"`` fallback because B1
        denies a row with no tenant, so an empty value cannot reach here — and a
        shared ``"unknown"`` partition would have merged unrelated tenants' runs into
        one ledger.
        """
        ttl = budget_config.budget_run_cap_ttl_seconds
        targets = [
            ReservationTarget(
                org_id=binding.tenant_id,
                entity_type=EntityType.RUN.value,
                entity_id=binding.run_id,
                period_type=PeriodType.RUN.value,
                period_start="lifetime",
                headroom_usd=run_cap,
                ttl_seconds=ttl,
            )
        ]

        # A run with no correlation id is not part of a chain, so there is no
        # aggregate to bound. Keying the chain scope on the run id as a fallback
        # would silently apply the (larger) chain cap a second time to a single
        # run, which is not the control anyone asked for.
        if binding.correlation_id:
            targets.append(
                ReservationTarget(
                    org_id=binding.tenant_id,
                    entity_type=EntityType.CHAIN.value,
                    entity_id=binding.correlation_id,
                    period_type=PeriodType.RUN.value,
                    period_start="lifetime",
                    headroom_usd=chain_cap,
                    ttl_seconds=ttl,
                )
            )

        return targets

    async def prepare_enforcement_context(self, context: TokenContext, run_id: str | None) -> EnforcementResult | None:
        """Read live controls after run authentication, before any financial check."""
        try:
            binding = await self._resolve_run_scope(context, run_id)
        except RunBindingError as exc:
            return EnforcementResult(
                allowed=False, deny_reason=DenyReason.BUDGET_EXCEEDED, blocked_reason=exc.message, enforcement_mode=EnforcementMode.HARD, scope="run"
            )
        try:
            async with self._get_session() as session:
                posture = await read_enforcement(session, org_id=binding.tenant_id if binding else None, flow_id=binding.flow_id if binding else None)
                context._budget_observation_scope = flow_key(binding.tenant_id, binding.flow_id) if binding and binding.flow_id else "global"
                context._budget_enforcement_enabled = posture.enabled
                context._budget_accounting_incomplete = posture.accounting_incomplete
        except Exception:
            return self._policy_budget_unavailable()
        return None

    async def check_budget_hierarchy(
        self,
        context: TokenContext,
        estimated_cost: Decimal,
        request_id: str | None = None,
        run_id: str | None = None,
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
            run_id: The caller's asserted run id (Issue #4187). Treated as an
                assertion to verify, never as an identity — see
                ``_resolve_run_scope``.

        Returns:
            EnforcementResult indicating if request is allowed
        """
        if context._budget_accounting_incomplete and context._budget_enforcement_enabled:
            return self._policy_budget_unavailable()
        if not context._budget_enforcement_enabled:
            try:
                binding = await self._resolve_run_scope(context, run_id)
                if binding and binding.root_human_id and context.auth_source == "iam":
                    object.__setattr__(
                        context, "attributed_user_id", _qualify_root_principal_id(binding.root_human_id, is_human_rooted=binding.is_human_rooted)
                    )
                async with self._get_session() as session:
                    return await self._observe_without_enforcement(session, context, binding, request_id)
            except RunBindingError as exc:
                return EnforcementResult(
                    allowed=False,
                    deny_reason=DenyReason.BUDGET_EXCEEDED,
                    blocked_reason=exc.message,
                    enforcement_mode=EnforcementMode.HARD,
                    scope="run",
                )
            except Exception:
                return self._policy_budget_unavailable()

        policy_target = context._policy_flow_target
        if policy_target is not None:
            # Issue #5225: the typed quote is required, not merely preferred. An
            # amount with no quote behind it is a number whose provenance is
            # gone — nothing says which bytes, which billing model or which
            # published revision produced it, which is exactly what this path
            # must not spend against.
            if context._policy_estimated_cost is None or context._policy_request_id is None or context._policy_quote is None:
                return self._policy_budget_unavailable()
            request_id = context._policy_request_id
            estimated_cost = max(estimated_cost, context._policy_estimated_cost)
        if not budget_config.budget_check_enabled:
            if policy_target is not None:
                return self._policy_budget_unavailable()
            return EnforcementResult(allowed=True)

        try:
            async with self._get_session() as session:
                all_warnings = []
                reservation_targets: list[ReservationTarget] = [policy_target] if policy_target is not None else []
                context._run_scope_reservations = list(reservation_targets)

                # Issue #4187: run and chain scopes go FIRST so the reservation
                # script attributes a denial to the most specific scope that ran
                # out — run, then chain, then the hierarchy below.
                try:
                    binding = await self._resolve_run_scope(context, run_id)
                except RunBindingError as exc:
                    # A forged or unknown run id is a denial, not a degrade.
                    # Deliberately not routed through _handle_check_failure: this
                    # is not a ledger fault, so it must not consume the grace
                    # window or ever become a 503.
                    logger.warning(f"Denying request: run id could not be bound to caller ({exc.reason})")
                    return EnforcementResult(
                        allowed=False,
                        deny_reason=DenyReason.BUDGET_EXCEEDED,
                        blocked_reason=exc.message,
                        enforcement_mode=EnforcementMode.HARD,
                        scope="run",
                    )

                if binding is not None:
                    # Issue #4591: the binding is VERIFIED in both modes, but the two
                    # things built from it below have different rollout gates.
                    #
                    #   * the run/chain CAP can deny a request, so it is gated on
                    #     `enforce` — that is the #4337 shadow-first rollout gate, and
                    #     shadow must continue to deny nothing;
                    #   * the #4300 ATTRIBUTION denies nothing. It labels the spend
                    #     with the human who set it in motion. Gating it on the same
                    #     flag was the #4591 defect: in shadow (the shipped default)
                    #     no cost record carried a root human, so every per-person
                    #     cloud-agent budget accrued nothing and enforced nothing.
                    #
                    # Deliberate and intended consequence: with attribution published
                    # in shadow, an authored `root_user` cap (the #4536 Budget
                    # Management surface) joins the entity hierarchy below and enforces
                    # like any user or org cap. That is HIERARCHY enforcement under
                    # `budget_check_enabled` — a cap a human explicitly authored, on a
                    # settled ledger, with `cap - settled` headroom — and it is
                    # independent of the run-binding rollout gate, which governs only
                    # the platform-default run/chain caps that deny with no ledger
                    # behind them. It is also exactly what that screen already
                    # promises the operator who set the number.
                    #
                    # Guarded on the feature flag too (Issue #4591): with run caps
                    # disabled, _resolve_run_scope now returns verified bindings
                    # for attribution, and a leftover mode=enforce setting must
                    # not switch the cap machinery on through this path.
                    enforcing = policy_target is not None or (
                        budget_config.budget_run_cap_enabled and budget_config.budget_run_binding_mode.lower() == "enforce"
                    )

                    if enforcing:
                        # Issue #4337 (B1): the cap is looked up for the tenant the ROW
                        # names, matching the partition `_scope_targets` keys on. Reading
                        # the caller-influenced `attributed_org_id` here would mean a
                        # tenant's per-run override could be addressed by a header, and
                        # would desync the cap from the ledger it is applied to.
                        approved = context._policy_scope_caps if policy_target is not None else None
                        run_cap = await self._resolve_scope_cap(
                            session, binding.tenant_id, EntityType.RUN, **({"approved_platform_cap": approved[0]} if approved else {})
                        )
                        chain_cap = await self._resolve_scope_cap(
                            session, binding.tenant_id, EntityType.CHAIN, **({"approved_platform_cap": approved[1]} if approved else {})
                        )
                        scope_targets = self._scope_targets(binding, run_cap, chain_cap)
                        reservation_targets.extend(scope_targets)

                        # Issue #4323: publish the run/chain targets so the reconcile
                        # on the way out can release them. The hierarchy targets are
                        # rebuilt from scratch at reconcile time (entity × period is
                        # derivable from the context alone), but these two are not:
                        # their keys need `binding.run_id` / `binding.correlation_id`,
                        # which only exist here. Re-resolving the binding at reconcile
                        # time would mean trusting `X-Agent-RunId` on a path that has
                        # no caller to deny — exactly the forgery surface AD-1 closed.
                        #
                        # The SAME target objects are carried, not the ids to rebuild
                        # them from, so the released key byte-matches the reserved key
                        # by construction. A mismatch here is not a loud failure, it is
                        # a silent no-op that looks exactly like the leak being fixed.
                        #
                        # Assigned even when reserving is later skipped or degrades:
                        # reconcile against a field that was never written is a no-op
                        # (the Lua only touches existing fields), which is cheaper than
                        # reasoning about which of the two paths ran.
                        #
                        # Stays inside the `enforcing` branch (Issue #4591): it exists
                        # to release the run/chain reservations, and in shadow none are
                        # ever taken. Publishing it unconditionally would name targets
                        # that were never reserved.
                        #
                        # Plain assignment, NOT the `object.__setattr__` the public
                        # attribution fields below use. That idiom exists to dodge
                        # validator re-entry, which private attributes never trigger —
                        # and pydantic keeps them in `__pydantic_private__`, so
                        # `object.__setattr__` would instead shadow a stale default
                        # there with a value in `__dict__`. Two homes for one value is
                        # a silent-divergence trap; this writes the one pydantic reads.
                        context._run_scope_reservations = ([policy_target] if policy_target is not None else []) + scope_targets

                    # Issue #4300: publish the server-resolved root human onto the
                    # context so (a) the hierarchy below can add its budget entity
                    # and (b) the proxy's chat-log write sites can carry it into the
                    # settled ledger. The binding is the ONLY forge-resistant source
                    # for this — it comes off the webhook-events row, and
                    # `verify_row_matches_caller` has already asserted the row
                    # belongs to this caller's tenant and identity.
                    #
                    # `object.__setattr__` follows the `_default_attributed_org_id`
                    # precedent: it dodges validator re-entry and keeps working if
                    # `validate_assignment` is ever enabled on TokenContext.
                    #
                    # Empty stays empty. `run_binding.py` normalizes a missing
                    # root_human_id to "", which is the common case (runs with no
                    # recorded root, and every row written before the lineage plane
                    # shipped).
                    #
                    # Issue #4344: the id is namespace-qualified by principal kind
                    # BEFORE it is published, not at the point it is read. Both
                    # consumers of this field read the same qualified value that way —
                    # the ROOT_USER entity below (the enforcement key) and the four
                    # chat-log write sites in `proxy/routes.py` (which feed the settled
                    # `root_user` ledger row via the budget-usage-tracker Lambda).
                    # Qualifying at only one of the two would silently desync the key
                    # enforcement reads from the key spend settles under, so the cap
                    # would read a ledger that never rises.
                    #
                    # A service-rooted run puts a SERVICE IDENTITY KEY here, not a
                    # `users.id`; see `_qualify_root_principal_id`.
                    #
                    # Issue #4591: UNCONDITIONAL on the binding mode — deliberately
                    # outside the `if enforcing:` block above. Attribution is a label,
                    # not a denial, and it is what makes cloud-agent spend visible and
                    # accruable at all. Re-gating it on `enforcing` puts every
                    # per-person budget back to displaying $0 forever in shadow.
                    #
                    # IAM callers only: a JWT human's spend is already accounted
                    # under (USER, sub), and verify_row_matches_caller deliberately
                    # never compares caller identity — so a signed-in human
                    # replaying a live run's X-Agent-RunId (debug replay, a client
                    # propagating agent headers) would otherwise be debited TWICE
                    # for one request: once as themselves, once as the run's root
                    # human. The ROOT_USER dedup guard in _get_entity_hierarchy
                    # cannot catch it — it compares a Cognito sub against a
                    # canonical users.id, disjoint namespaces that never match.
                    # Attribution exists to label AGENT spend; agents authenticate
                    # as IAM. (#4396 fused direct human spend into the per-person
                    # envelope on the READ side, over the `(USER, sub)` rows that
                    # already exist — so this guard stays exactly as narrow as it
                    # was, and must: widening it is the double-debit above.)
                    if binding.root_human_id and context.auth_source == "iam":
                        object.__setattr__(
                            context,
                            "attributed_user_id",
                            _qualify_root_principal_id(binding.root_human_id, is_human_rooted=binding.is_human_rooted),
                        )

                # ORDERING CONTRACT (Issue #4300): the hierarchy is built HERE,
                # strictly after the run binding has been resolved and
                # `attributed_user_id` published above. Building it earlier — where
                # this call used to live — means the ROOT_USER entity is never
                # added, and the per-human envelope silently enforces nothing while
                # every test that seeds the context directly still passes. If you
                # move this line back up, #4300 becomes inert.
                entities = self._get_entity_hierarchy(context)

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

                # Issue #4630 (#4620 · C4): the person layer, AFTER the per-org
                # hierarchy above. The ordering is a design requirement, not a
                # coincidence of where the line was typed: an org's own cap must
                # keep firing first, which is what "per-org caps remain
                # independently authoritative" means operationally (note §5.3).
                # With no person cap row authored, this returns None and the whole
                # path above is byte-identical to pre-#4630 behaviour.
                #
                # Contained rather than allowed to raise (review finding): a fault
                # in here must NOT reach the shared `except` below, whose
                # `_handle_check_failure` fails OPEN for code-level faults. That
                # would void the ENTIRE budget check — run, chain and every org cap
                # — while requests still returned 200. Not hypothetical: on an
                # environment where C3's migration has not landed, the cap read
                # raises for every request, so all enforcement platform-wide would
                # silently fail open. This new layer may only ever fail to enforce
                # ITSELF.
                person_result = await self._check_person_budget_contained(session, context, estimated_cost)
                if person_result is not None:
                    if not person_result.allowed:
                        # The ledger read succeeded, so this is a healthy check.
                        await self._note_check_succeeded()
                        return person_result
                    all_warnings.extend(person_result.warnings)

                # All checks passed — the ledger is readable, so reset the
                # consecutive-failure window and emit the healthy-path 0.
                await self._note_check_succeeded()

        except Exception as e:
            if policy_target is not None:
                return self._policy_budget_unavailable()
            return await self._handle_check_failure(e, "Budget check")

        # Issue #5225: confirm the quote at the boundary that actually spends.
        #
        # The middleware revalidated it before handing control inward, but
        # everything above this line is awaited work — a session, the run
        # binding, the scope caps, the entity hierarchy, the person layer. A
        # 120-second quote TTL can lapse inside that window and a published rate
        # generation can roll over inside it, and the hold below would then be
        # taken against a bound that no longer describes the request's cost.
        #
        # Deliberately the LAST thing before reserving, for the same reason the
        # middleware's own check sits immediately before minting the request id:
        # any awaited work placed after it would reopen the window it closes.
        #
        # Nothing has been reserved and nothing submitted upstream at this point,
        # so a refusal is a definite pre-submission failure: deny, take no hold,
        # and leave every prior hold untouched. It reports as CHECK_UNAVAILABLE
        # (503 + Retry-After) rather than a 402, because no cap was exceeded —
        # the bound is merely unconfirmed, and a retry requotes.
        if policy_target is not None:
            from src.orchestration.provider_quotes import confirm_quote_spendable

            refusal = await confirm_quote_spendable(context._policy_quote)
            if refusal is not None:
                logger.warning(
                    "Denying policy model request: quote no longer spendable at the reservation boundary "
                    f"(reason={refusal.reason}, capability={refusal.capability})"
                )
                return self._policy_budget_unavailable()

        # Issue #4287: the live-denominator gate. Deliberately OUTSIDE the try
        # above: a reservation fault is not a ledger-read fault, and routing it
        # into _handle_check_failure would let a Redis blip burn the DB grace
        # window and then deny all traffic. It degrades instead — see
        # _reserve_or_degrade.
        reservation_denial = await self._reserve_or_degrade(
            request_id, estimated_cost, reservation_targets, **({"strict": True} if policy_target is not None else {})
        )
        if reservation_denial is not None:
            return reservation_denial

        return EnforcementResult(allowed=True, warnings=all_warnings)

    async def _observe_without_enforcement(self, session, context, binding, request_id):
        """Usage writers still run. Maintain accumulators without spending gates."""
        from uuid import uuid4

        request_id = context._policy_request_id or request_id or str(uuid4())
        targets = [
            ReservationTarget(
                org_id=context.attributed_org_id,
                entity_type=entity.value,
                entity_id=entity_id,
                period_type=period.value,
                period_start=get_period_start_end(period)[0].isoformat(),
                headroom_usd=Decimal(0),
                # Do not lose an off-mode in-flight marker on the short live
                # counter TTL before a long provider response settles.
                ttl_seconds=172800,
            )
            for entity, entity_id in self._get_entity_hierarchy(context)
            for period in (PeriodType.DAILY, PeriodType.WEEKLY, PeriodType.MONTHLY)
        ]
        scopes = self._scope_targets(binding, Decimal(0), Decimal(0)) if binding else []
        if context._policy_flow_target is not None:
            scopes.append(context._policy_flow_target)
        context._run_scope_reservations = scopes
        store = self._get_reservations()
        if not store.enabled or not await store.observe(request_id, targets + scopes):
            # Persist missing observation so a later ON cannot trust stale totals.
            # This records no amount and changes no existing reservation.
            key = flow_key(binding.tenant_id, binding.flow_id) if binding and binding.flow_id else "global"
            session.add(BudgetAccountingGap(request_id=request_id, scope_key=key))
            await session.commit()
        return EnforcementResult(allowed=True)

    async def _reserve_or_degrade(
        self,
        request_id: str | None,
        estimated_cost: Decimal,
        targets: list[ReservationTarget],
        *,
        strict: bool = False,
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
            return self._policy_budget_unavailable() if strict else None

        store = self._get_reservations()
        if not store.enabled:
            return self._policy_budget_unavailable() if strict else None

        environment = self._get_environment()
        outcome = await store.reserve(request_id, estimated_cost, targets)

        if outcome is None:
            # Redis unavailable. Degrade to the settled-ledger check (lagged
            # denominator, still fail-closed on the ledger itself) and alarm.
            emit_budget_reservation_outcome(outcome="degraded", environment=environment)
            return self._policy_budget_unavailable() if strict else None

        if outcome.usage_unavailable:
            return self._policy_budget_unavailable()

        if outcome.admitted:
            emit_budget_reservation_outcome(outcome="reserved", environment=environment)
            return None

        exhausted = outcome.exhausted
        assert exhausted is not None  # denials always name the exhausted budget
        emit_budget_reservation_outcome(outcome="denied", environment=environment)
        if strict and outcome.awaiting_usage:
            # The Lua denied without writing any reservation. This request may
            # fit after current provider calls settle; the SDK can retry through
            # every identity, policy, quote and cap check without losing its run.
            logger.info("Policy model request waiting for in-flight usage", extra={"budget_scope": exhausted.entity_type})
            return EnforcementResult(
                allowed=False,
                deny_reason=DenyReason.RESERVATIONS_PENDING,
                blocked_reason="Current model requests are awaiting usage settlement. Retry shortly.",
                scope=exhausted.entity_type,
            )
        logger.warning(
            f"Budget exceeded (in-flight reservations): {exhausted.entity_type} {exhausted.entity_id} "
            f"- {exhausted.period_type} settled headroom ${exhausted.headroom_usd}, request estimate ${estimated_cost}"
        )

        # Issue #4187: only the new scopes carry a discriminator. Hierarchy
        # denials keep `scope=None`, which leaves every pre-#4187 402 body
        # byte-identical.
        #
        # Issue #4300 adds `root_user` so a personal-envelope stop is
        # distinguishable from an org/team cap. Without it the worker's regex
        # falls through to `hierarchy_cap_exceeded` and the operator is told to
        # raise an org budget when the real limit was one person's envelope.
        is_scope_denial = exhausted.entity_type in (
            EntityType.RUN.value,
            EntityType.CHAIN.value,
            EntityType.ROOT_USER.value,
            EntityType.FLOW.value,
        )
        return EnforcementResult(
            allowed=False,
            deny_reason=DenyReason.BUDGET_EXCEEDED,
            blocked_reason=f"Budget exceeded for {exhausted.entity_type} {exhausted.entity_id} (including in-flight spend)",
            exceeded_entity_type=EntityType(exhausted.entity_type),
            exceeded_entity_id=exhausted.entity_id,
            enforcement_mode=EnforcementMode.HARD,
            scope=exhausted.entity_type if is_scope_denial else None,
            # For run/chain the headroom IS the whole cap: there is no settled
            # ledger to subtract, so this is the cap, not a remainder. For
            # root_user (#4300) it IS a remainder — that line has a settled
            # ledger, so this is `cap - settled_spend`, the headroom the request
            # was denied against rather than the configured ceiling.
            scope_cap_usd=exhausted.headroom_usd if is_scope_denial else None,
        )

    @staticmethod
    def _policy_budget_unavailable() -> EnforcementResult:
        return EnforcementResult(
            allowed=False,
            deny_reason=DenyReason.CHECK_UNAVAILABLE,
            blocked_reason="Accepted policy budget is unavailable or has unresolved usage",
            scope="flow",
        )

    async def reconcile_reservation(
        self,
        context: TokenContext,
        request_id: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        actual_cost_usd: Decimal | None = None,
        usage_known: bool = True,
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

        Issue #4323: the run/chain (``lifetime``) reservations are released here
        too. They cannot be rebuilt from ``context`` the way the hierarchy grid
        can — their keys are derived from the server-resolved run binding — so the
        check path stashes the exact targets it reserved and this appends them
        verbatim. Before that they were never in this list at all, so a failed or
        aborted chain kept holding its run/chain headroom for the full 24h run TTL
        and fresh runs under the same cap were denied against spend that had
        already stopped.
        """
        if not context._budget_enforcement_enabled and (not usage_known or (context._policy_flow_target is not None and actual_cost_usd is None)):
            # Preserve all unbounded markers. No missing receipt becomes $0.
            async with self._get_session() as session:
                if await session.get(BudgetAccountingGap, request_id) is None:
                    session.add(BudgetAccountingGap(request_id=request_id, scope_key=context._budget_observation_scope or "global"))
                    await session.commit()
            return

        if budget_config.budget_reservation_enabled is False and context._budget_enforcement_enabled and not context._run_scope_reservations:
            return

        store = self._get_reservations()
        if not store.enabled:
            return

        actual_cost = actual_cost_usd if actual_cost_usd is not None else self._pricing.calculate_cost(model_id, input_tokens, output_tokens)

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

        # Issue #4323: plus whatever run/chain scopes the check reserved for THIS
        # request. Empty for every caller that reserved none, which keeps the
        # pre-#4323 key set byte-identical for those paths.
        #
        # Per-request by construction, and that is the no-over-release property:
        # a sibling run under the same chain cap carries its own targets, and the
        # reconcile script keys on `request_id` as the hash FIELD — so releasing
        # this request touches only this request's field, even in the chain key
        # the two siblings share.
        targets.extend(context._run_scope_reservations)

        if not usage_known or actual_cost_usd is None:
            # Policy totals require a trusted price receipt. A failed upstream
            # call or missing usage must not turn its estimate into zero spend.
            unresolved = [target for target in targets if target.require_initialization]
            for target in unresolved:
                await store.mark_unknown(request_id, target)
            targets = [target for target in targets if not target.require_initialization]

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
                        # Issue #4591: the settled-ledger ROOT_USER deny is the
                        # dominant deny path attribution newly activates, and the
                        # agent worker classifies the stop by matching `scope` in
                        # the 402 body (agent-worker.ts). Without it, a per-person
                        # cloud-agent cap misreports as hierarchy_cap_exceeded and
                        # the operator is told to raise the org budget — the wrong
                        # knob. Other hierarchy types keep scope=None: their 402
                        # bodies pre-date the field and consumers key off
                        # exceeded_entity_type for them.
                        scope="root_user" if entity_type == EntityType.ROOT_USER else None,
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

    async def _check_person_budget_contained(
        self,
        session: AsyncSession,
        context: TokenContext,
        estimated_cost: Decimal,
    ) -> EnforcementResult | None:
        """Run the person layer with its faults confined to itself (Issue #4630).

        The person layer is the newest and widest-reading part of this check: it
        resolves an identity, fans out over a partition set and reads one ledger row
        per (partition, fused id). Any of that can fault, and it sits inside
        ``check_budget_hierarchy``'s shared ``try`` — whose handler
        (``_handle_check_failure``) deliberately fails **open** for code-level
        faults so a deterministic bug cannot permanently down all inference.

        Composing those two gives a defect worth naming: a fault in *this* layer
        would return an allow for the whole request, discarding the run, chain and
        per-org verdicts that had already been computed. On an environment where
        C3's migration has not landed, that is every request — all budget
        enforcement platform-wide, silently off, with 200s.

        So the person layer is the only layer whose failure it may cause. A fault
        here skips the person cap, emits the dedicated ``PersonBudgetLayerSkipped``
        metric (alarmed in ``budget-alarms/main.tf`` once that infra is applied),
        and leaves every other verdict standing. It deliberately does NOT enter the
        shared grace window: escalation there fails CLOSED for the whole check, and
        a person-layer-only fault (e.g. the C3 migration missing) must never be
        able to down all inference.
        That is strictly safer than the alternative in both directions: the org that
        pays still has its ceiling enforced, and the person's own cap is advisory
        for the duration of the fault rather than the whole check being.

        Returns:
            The person layer's verdict, or ``None`` when there is no cap to apply
            (the overwhelmingly common case) or the layer faulted.
        """
        try:
            return await self._check_person_budget(session, context, estimated_cost)
        except Exception as exc:  # noqa: BLE001 — containment is the whole point; see docstring
            logger.error(
                f"Person-level budget check failed and was SKIPPED — every other budget verdict stands ({type(exc).__name__}): {exc}",
                exc_info=True,
            )
            fault_class = "infrastructure" if isinstance(exc, _INFRASTRUCTURE_FAULTS) else "unexpected"
            emit_budget_check_failure(
                fault_class=fault_class,
                # A distinct outcome, not one of `_handle_check_failure`'s: nothing
                # was allowed *because of* this fault, so reporting it as
                # `allowed_fail_open` would overstate the blast radius and hide the
                # one thing that did stop working.
                outcome="person_layer_skipped",
                environment=self._get_environment(),
            )
            # The alarmable signal (review fix on #4661): the outcome above lands
            # in a dimension set no alarm watches, and this failure mode — person
            # caps silently unenforced while everything else stays green — is
            # found on a bill unless something pages. Dedicated metric, plain
            # [Environment] rollup, single alarm in budget-alarms/main.tf.
            emit_person_budget_layer_skipped(
                fault_class=fault_class,
                environment=self._get_environment(),
            )
            return None

    async def _person_limit_sources_exist(self, session: AsyncSession) -> tuple[bool, bool]:
        """Short-TTL process-local gate: do ANY person limits exist, individual or default?

        Review fix on #4689, **widened to cover ``person_budget_defaults`` by #4690**.
        Both tables are empty on most installs, and without this gate every JWT
        model invoke pays several sequential identity/limit queries to learn "no
        limit". One round trip per TTL window per process answers the common case
        instead — and it stays ONE round trip after the widening, because the two
        existence questions are asked as two ``EXISTS`` subqueries in a single
        statement rather than a query each. Adding a hot-path round trip here is
        the #4689 lesson and the thing this method exists to prevent.

        The two answers are returned SEPARATELY rather than pre-``or``ed because
        they gate different amounts of work: with individual caps but no defaults,
        the person layer does exactly what it did before #4690 (one indexed cap
        read, fan-out only if it matched), whereas a default existing means the
        applicable-limit ladder must resolve the person's orgs and teams even when
        they have no personal row — that is the point of a default. Collapsing the
        pair would make every install with a personal cap pay the defaults path.

        Trade documented at the call site: a first-ever limit of either kind starts
        enforcing within the TTL, not instantly; deleting the last one wastes
        queries for one window. The cache is deliberately per-process and unlocked
        — a stale read is bounded by the TTL and both failure directions are benign.

        Returns:
            ``(individual_caps_exist, defaults_exist)``.
        """
        now = time.monotonic()
        cached = self._person_caps_exist_cache
        if cached is not None and now - cached[1] < _PERSON_CAPS_EXISTENCE_TTL_SECONDS:
            return cached[0]
        row = (
            await session.execute(
                select(
                    select(PersonBudgetConfig.id).exists(),
                    select(PersonBudgetDefault.id).exists(),
                )
            )
        ).one()
        exists = (bool(row[0]), bool(row[1]))
        self._person_caps_exist_cache = (exists, now)
        return exists

    async def _any_person_caps_exist(self, session: AsyncSession) -> bool:
        """Does ANY person limit exist — individual cap or default rule?

        The gate the person layer actually skips on. Since #4690 a default rule is
        a person limit too, so a gate that asked only about ``person_budget_configs``
        would skip the layer on precisely the install a platform admin had just
        bounded everybody on — the default would be authored, displayed, and govern
        nothing (the #4511 inert-cap class, at platform scale).
        """
        individual, defaults = await self._person_limit_sources_exist(session)
        return individual or defaults

    async def _resolve_person_limits(
        self,
        session: AsyncSession,
        context: TokenContext,
    ) -> tuple[dict[str, PersonLimit], str, list[str], list[str], list[str]] | None:
        """Who is this caller, which limits govern them, and what does the denominator span?

        Issue #4690. The shared preamble of ``_check_person_budget`` (the deny path)
        and ``_person_cap_headroom`` (the headers path). Extracted rather than
        duplicated because the ladder made the preamble long enough that two copies
        would drift, and drift here is precisely the FR-1.4 class the headers path
        was added to close: a ``X-Budget-Remaining`` computed against a different
        rung than the 402 enforces is a worse answer than no header at all.

        The order of operations is load-bearing and unchanged from #4661/#4689:

        1. **The existence gate first.** No person limit of either kind on the
           install → return immediately, zero identity work. This is the
           overwhelmingly common case platform-wide.
        2. **Then the person key**, from the run binding if attributed or the token
           identity otherwise (#4396), skipping service principals.
        3. **Then ONE deterministic anchor lookup**, and — only if a limit is
           actually going to be consumed — the fused-identity scan, partition
           derivation and sub projection that the denominator needs.

        **The #4690 addition costs nothing on an install with no defaults.** The
        existence gate reports the two tables separately; with the defaults table
        empty, this resolves only the top rung, by the same single indexed anchor
        read as before. The org/team resolution a default requires happens only
        where a default exists.

        Returns:
            ``(limits, anchor, person_user_ids, person_subs, partitions)``, or
            ``None`` when this caller has no person limit to apply — a service
            principal, a non-human account, an unprovisioned identity, or a person
            matched by neither an individual row nor any default rule. ``None`` is
            the only honest "unlimited", and it is now a strictly narrower set of
            callers than before #4690.

            ``anchor`` is the person key the limits were matched on, carried out so
            the denial can NAME the person (a #4630 review pin: ``scope="person"``
            alone does not say which person, and an operator needs the anchor to
            find the row). It is the fused anchor, so on a person with no linked
            GitHub identity it is ``resolve_person_identity``'s ``users:<id>``
            form — still a key that identifies them, which is what the message
            needs, even though no INDIVIDUAL row can be stored under it.
        """
        # `person_ledger` is a deliberate leaf (review fix on #4689): unlike the
        # previous me_routes underscore-privates, these carry a stability contract
        # for this second consumer and import no router — the anchor-prefix import
        # stays function-local only for symmetry with `person_anchor.py`'s own note.
        from src.shared.identity.person_anchor import format_person_anchor, is_authorable_person_anchor

        from .person_ledger import (
            resolve_applicable_person_limits,
            resolve_individual_person_limits,
            resolve_member_partitions,
            resolve_person_anchor_identity,
            resolve_person_identity,
            resolve_person_subs,
            resolve_person_team_keys,
        )

        # The overwhelmingly common case platform-wide is "no person limit exists at
        # all" — both tables are empty on most installs. This short-TTL
        # process-local gate keeps that case at ~zero cost instead of several
        # sequential identity/limit queries on EVERY JWT model invoke (review fix on
        # #4689), which landed squarely on the gateway latency path. Cost of the
        # cache: the first limit ever authored takes up to the TTL to start
        # enforcing, and deleting the last one wastes queries for one TTL window —
        # both bounded and documented in budget-ratelimit.md.
        individual_caps_exist, defaults_exist = await self._person_limit_sources_exist(session)
        if not (individual_caps_exist or defaults_exist):
            return None

        attributed_user_id = context.attributed_user_id or ""

        if attributed_user_id:
            # §7.3's double-count guard: a service principal (EventBridge / scheduled
            # / CI / alarm) is not a person, has no GitHub anchor, and must not be
            # charged against anybody's personal ceiling — nor against a default,
            # which is a rule about PEOPLE.
            if attributed_user_id.startswith(_SERVICE_PRINCIPAL_PREFIX):
                return None
            person_user_id = attributed_user_id
        else:
            # A direct (JWT) caller: there is no run binding, so the person key comes
            # from the token identity (#4396).
            # Not a person: unattended/service principals never carry a personal
            # ceiling (mirrors the read surface's not_applicable case).
            if context.account_type != "human":
                return None
            # STRICT lookup, deliberately NOT the read surface's resolver (review
            # fix on #4689): `resolve_canonical_user_id` swallows SQLAlchemyError
            # into a raw-sub fallback — tolerable for a degraded dollar figure on
            # a page, but HERE it turned a transient DB error into a silent
            # fail-open that never reached the containment wrapper, so the
            # PersonBudgetLayerSkipped pager stayed dark. A fault must propagate
            # to the wrapper; only a genuine no-row (unprovisioned identity, no
            # direct ledger to govern) skips quietly.
            person_user_id = await session.scalar(select(User.id).where(User.cognito_sub == context.user_id).limit(1))
            if not person_user_id:
                return None

        # Limit first, fan-out second — for real this time (review fix on #4661: the
        # previous shape ran the cross-tenant fused-id scan before the cap read,
        # paying it on every attributed request when the overwhelmingly common
        # outcome is "no limit"). Only the single deterministic anchor lookup runs
        # unconditionally; the fused-id scan and partition derivation are deferred
        # until a limit proves they will be consumed.
        if defaults_exist:
            # A default exists, so membership must be resolved BEFORE the limit is
            # known: which rule applies is a function of the person's orgs and
            # teams. This is the one place #4690 genuinely costs queries, and only
            # on installs that have chosen to author a default. The standalone
            # anchor pre-query is skipped on this branch (review fix on #4696) —
            # the fusion below derives the anchor anyway; one query, one authority.
            resolved_anchor, person_user_ids = await resolve_person_identity(session, person_user_id)
            # Asks the namespace registry whether a cap can be STORED under this
            # anchor, rather than testing for the `github:` prefix (#4843). The
            # prefix test excluded every namespace added after it from the
            # individual-cap read while the authoring side would happily store one
            # — a cap that displays a limit and governs nothing, the #4511 class.
            # Native users now have authorable keys too; the same resolver and
            # namespace registry are used by the authoring and read surfaces.
            anchor = resolved_anchor if is_authorable_person_anchor(resolved_anchor) else None

            # TWO org lists, deliberately different (review fix on #4696 — the
            # critical finding): the SPEND denominator may include the
            # caller-influenced attributed org (#4132) because widening it only
            # COUNTS MORE spend — the fail-safe direction. The DEFAULT LADDER must
            # not: which rule governs a person is authorization-adjacent, and
            # unioning the attributed org let an X-Agent-OrgId header select a
            # foreign org's more generous default over a tight platform ceiling.
            # Ladder candidates are server-derived memberships ONLY.
            ladder_org_ids = await resolve_member_partitions(session, person_user_ids, None)
            partitions = sorted(set(ladder_org_ids) | ({context.attributed_org_id} if context.attributed_org_id else set()))
            limits = await resolve_applicable_person_limits(
                session,
                person_anchor=anchor,
                org_ids=ladder_org_ids,
                team_keys=await resolve_person_team_keys(session, person_user_ids),
                # The person's own id, so a limit they authored on themselves is
                # named as theirs in the 402, not as an admin's grant (review fix
                # on #4696).
                self_authored_by=person_user_id,
            )
        else:
            # No default exists anywhere on the install, so the ladder provably
            # degenerates to its top rung. Taking it directly is what keeps #4690
            # free for these installs: resolving the person's orgs and teams only to
            # find no default would put that fan-out back on the hot path for every
            # request — the #4689 regression, reintroduced. Limit-first fast path:
            # one anchor lookup, and the fusion is paid only when a row exists.
            resolved = await resolve_person_anchor_identity(session, person_user_id)
            if not resolved:
                anchor, _ = await resolve_person_identity(session, person_user_id)
            else:
                anchor = format_person_anchor(resolved[1], resolved[0])
            # Composed through the single composer (#4843) — this was the third of
            # the three hand-rolled anchor f-strings the design note flagged. It is
            # the ENFORCEMENT side of the comparison a few lines below, so a
            # spelling that drifts from the authoring side's by one character makes
            # every cap on the install inert.
            limits = await resolve_individual_person_limits(session, anchor, self_authored_by=person_user_id)
            if not limits:
                return None

            # A limit exists: NOW pay for the denominator's identity fusion. The
            # resolver re-runs the anchor lookup internally (one extra single-row
            # query on the rare limited path) — reused rather than forked, so
            # authoring, the C1 read and this layer cannot drift on what "the same
            # person" means.
            resolved_anchor, person_user_ids = await resolve_person_identity(session, person_user_id)
            if resolved_anchor != anchor:
                # Only possible if identity rows changed between the two lookups
                # mid-request. The limit was matched on `anchor`; refusing to enforce
                # it against a denominator resolved for a DIFFERENT anchor beats
                # denying someone on another person's spend.
                logger.warning("Person anchor changed between cap lookup and identity fusion; skipping the person layer for this request")
                return None
            partitions = await resolve_member_partitions(session, person_user_ids, context.attributed_org_id)

        if not limits:
            return None

        # The direct half's key namespace (#4396). A projection of the fusion just
        # performed, not a second opinion on who the person is.
        person_subs = await resolve_person_subs(session, person_user_ids)
        return limits, resolved_anchor, person_user_ids, person_subs, partitions

    async def _check_person_budget(
        self,
        session: AsyncSession,
        context: TokenContext,
        estimated_cost: Decimal,
    ) -> EnforcementResult | None:
        """Check the person's platform-wide cap against a cross-org SETTLED denominator.

        Issue #4630 (#4620 · C4), design note
        ``docs/design-notes/4620-cross-org-person-budgets.md`` §5.3 + §5.5.

        This is the layer that makes a person's own ceiling real in every org their
        agents run in. The per-org hierarchy above cannot express it: every budget
        row is keyed ``org_id``-first, so a cap authored in one partition caps
        nothing that executes in another (#4620). ``person_budget_configs`` is
        partition-free, and this reads it.

        **The denominator is the settled ledger, and the overshoot bound is real
        (§5.5).** ``ReservationTarget.key()`` embeds ``{org_id}`` as a Redis Cluster
        hash tag so the multi-key atomic Lua stays single-slot
        (``reservations.py``); a person key cannot carry an ``org_id`` hash tag
        without re-partitioning the very thing this cap exists to span. The
        recorded ruling on #4620 is therefore explicit: **do not add a second,
        non-atomic reservation call for this layer.** So this method returns no
        ``ReservationTarget`` and takes no Redis reservation — that absence is the
        requirement, not an omission.

        The consequence, stated rather than implied away: **a person cap can be
        exceeded by up to the spend incurred but not yet settled at the moment of
        this check** — bounded in practice by the person's aggregate burn rate over
        the settlement lag (``/me/budget`` surfaces that lag as
        ``freshness.cost_backfill_lag``), plus in-flight concurrency. It is a
        bounded ceiling, **not** an atomic guarantee, and
        ``docs/budget-ratelimit.md`` says so in the 402 contract. Per-org hierarchy
        caps keep their live Redis denominator, so each org's own ceiling stays
        bounded exactly as tightly as it is today.

        **The denominator is the person's TOTAL spend — direct + cloud (#4396).**
        Widened from cloud-only by the operator ruling of 2026-09-05 (thread on
        #4669/#4685): *the person's limit governs total spend across all GitHub orgs,
        and the person sees ONE number tracked against it.* Two consequences, both
        deliberate:

        * **JWT (direct, interactive) callers now pass through this layer.** Before
          #4396 an unattributed request returned ``None`` immediately, so a person
          could sit inside their personal limit while spending freely from their own
          machine. The person key for such a caller is resolved from the token
          identity instead of from a run binding — see below.
        * **The figure enforced here is the figure ``/me/budget`` displays.** Both
          call the same ``_read_person_partition_spend``, so "displayed == enforced"
          is a shared code path rather than two derivations that happen to agree.
          That was the explicit requirement of the ruling.

        **No double-count, and it needs no offsetting skip.** The two ledgers summed
        are disjoint by construction: ``user`` rows are keyed by Cognito sub, all
        ``root_user`` rows by canonical ``users.id``, and ``budget_usage`` is uniquely
        keyed including ``entity_type``. A direct request writes only the ``user``
        row (the tracker's ``!= user_id`` gate suppresses the other); a hosted run's
        ``user`` row is keyed by the shared *worker* identity, never by this person's
        sub. ``me_routes``' fused-envelope section header states the full argument.

        **This is why the equality-skip in ``_get_entity_hierarchy`` is left
        untouched.** The issue asked for it to be revisited, and revisiting it
        concludes: leave it. It prevents a second *reservation* against one party
        inside one request — a live-Redis concern about one org's hierarchy — which is
        a different question from what this settled cross-org read sums. Relaxing it
        would reintroduce the 2x debit its own comment documents, without changing
        anything here.

        **Identity is C1's, reused verbatim.** ``_resolve_person_identity`` fuses
        the person's ``users.id`` rows through ``user_identities.provider_user_id``
        (§3.3 — one GitHub account legitimately holds one ``users.id`` *per tenant*,
        so summing by canonical id alone under-reports for exactly the multi-org
        population this ships for), ``_resolve_person_subs`` projects that same fusion
        onto the Cognito subs the direct ledger is keyed by, ``_resolve_member_partitions``
        derives the partition set server-side (§7.3, including the shadow-user union),
        and ``_read_settled_spend`` performs the same full 5-filter single-row read
        enforcement already compares against. The cross-org read is a **widened key
        and partition set over an unchanged predicate** — never a relaxed predicate
        and never a SQL aggregate. A second identity fusion in this module would be
        the #4511 class: an enforcement layer reading a different person key than C3
        writes is an inert cap.

        **The applicable limit may be a DEFAULT, not the person's own row (#4690).**
        When no individual ``person_budget_configs`` row matches, this layer no
        longer concludes "unlimited": ``resolve_applicable_person_limits`` walks
        individual row → team default → org default → platform default, so a
        platform admin's single "$1,000 each" rule governs everybody who has never
        authored a limit, including people who join later. The denial names which
        rung supplied the number, because "you are over your limit" is unactionable
        to somebody who never set one.

        **Cap first, denominator second.** At most two single-row indexed lookups
        (the person key, then the anchor) before the limit read; when that returns
        nothing — essentially every request on an install with no person limits at
        all — the partition fan-out and every spend read are skipped entirely.
        Direct callers pay one extra ``users`` lookup on ``cognito_sub`` (an indexed
        column) relative to pre-#4396, which is the irreducible cost of them being
        subject to the cap at all. #4690 adds NO query to an install with no
        defaults: the existence gate reports the two tables separately and the
        ladder degenerates to its top rung when the defaults table is empty.

        Returns:
            ``None`` when this caller has no person limit to apply. Otherwise an
            ``EnforcementResult``: a denial for a ``hard`` limit that is exceeded, or
            an allow (possibly carrying warnings) in every other case. Deliberately
            no ``ReservationTarget`` in the return — see above.
        """
        from .person_ledger import read_person_partition_spend

        resolved = await self._resolve_person_limits(session, context)
        if resolved is None:
            return None
        limits, anchor, person_user_ids, person_subs, partitions = resolved

        warnings: list[str] = []

        # Deterministic evaluation order, so which of several exceeded periods is
        # named in the denial is stable rather than an accident of dict order.
        for limit in sorted(limits.values(), key=lambda item: item.period_type):
            period_type = PeriodType(limit.period_type)
            period_start, _ = get_period_start_end(period_type)

            # The cross-org denominator: the SAME predicate, over a widened key
            # and partition set. `_read_person_partition_spend` is the read
            # surface's own function, called here rather than reimplemented, so
            # `/me/budget`'s figure and this one cannot drift — the ruling's
            # "displayed == enforced" is a shared code path, not a coincidence.
            # Summing cloud and direct cannot double-count: `root_user` rows are
            # keyed by canonical `users.id` and `user` rows by Cognito sub, in a
            # table uniquely keyed including `entity_type`, and no write path
            # produces both for one dollar (§7.3, the #4322 family — the argument
            # in full lives on `me_routes`' fused-envelope section header).
            # `organization`/`department`/`team` rows stay excluded: those re-count
            # the same dollar at a coarser grain.
            current_spend = Decimal("0")
            for org_id in partitions:
                cloud, direct = await read_person_partition_spend(
                    session,
                    org_id,
                    person_user_ids,
                    person_subs,
                    period_type,
                    period_start,
                )
                current_spend += cloud + direct

            projected_spend = current_spend + estimated_cost
            enforcement_mode = EnforcementMode(limit.enforcement_mode)

            if projected_spend > limit.amount:
                if enforcement_mode == EnforcementMode.HARD:
                    logger.warning(
                        f"Person budget exceeded (hard limit): {anchor} - {limit.period_type} limit "
                        f"${limit.amount} from {limit.scope_label} (source={limit.source}), settled "
                        f"${current_spend} across {len(partitions)} partition(s), projected ${projected_spend}"
                    )
                    return EnforcementResult(
                        allowed=False,
                        deny_reason=DenyReason.BUDGET_EXCEEDED,
                        # The anchor, the period AND (new in #4690) the source rung
                        # are all named here and in the WARN above, because they are
                        # what somebody needs to find the knob. `scope="person"`
                        # alone says neither which person nor which period (the
                        # #4630 pin), and since #4690 it does not say whose number
                        # it is either: a person stopped at a platform default they
                        # never authored, told only "personal spending limit
                        # exceeded", goes looking for a limit of their own that does
                        # not exist.
                        blocked_reason=(
                            f"Personal spending limit exceeded for {anchor} ({limit.period_type}, "
                            f"from {limit.scope_label}): ${projected_spend:.2f} of ${limit.amount:.2f} "
                            f"across all organizations"
                        ),
                        # `exceeded_entity_type`/`exceeded_entity_id` stay None
                        # DELIBERATELY. A person is not an `EntityType`, and minting
                        # one would make `entity_type="person"` authorable through
                        # `budget_configs` — an inert cap nothing enforces, which is
                        # the #4511 class this EPIC exists to remove.
                        budget_amount_usd=limit.amount,
                        current_spend_usd=current_spend,
                        enforcement_mode=enforcement_mode,
                        # The discriminator the worker classifies the stop by. A new
                        # scope that is not threaded end-to-end silently misreports
                        # as `hierarchy_cap_exceeded` and sends the operator to
                        # raise an ORG budget — the wrong knob, and (for a personal
                        # row) nobody can raise this one but the person themselves.
                        scope="person",
                        scope_cap_usd=limit.amount,
                    )

                # Soft means informational: the figure is reported and nothing is
                # denied — the same meaning `_check_entity_budget` gives the column
                # on `budget_configs`, so there is one enforcement semantic for
                # `enforcement_mode` rather than a per-table special case. Only an
                # individual row can be soft; defaults are written `hard` (#4690).
                logger.info(
                    f"Person budget exceeded (soft limit): {anchor} - {limit.period_type} limit ${limit.amount}, projected ${projected_spend}"
                )
                warnings.append(
                    f"Personal spending limit exceeded for {limit.period_type} (from {limit.scope_label}): "
                    f"${projected_spend:.2f} / ${limit.amount:.2f} across all organizations"
                )
                continue

            utilization = calculate_budget_utilization(limit.amount, projected_spend)
            if utilization >= budget_config.budget_critical_threshold_percent:
                warnings.append(f"Personal spending limit {limit.period_type} at {utilization:.1f}% (critical)")
            elif utilization >= budget_config.budget_warning_threshold_percent:
                warnings.append(f"Personal spending limit {limit.period_type} at {utilization:.1f}%")

        return EnforcementResult(allowed=True, warnings=warnings)

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

    async def _person_cap_headroom(self, session: AsyncSession, context: TokenContext):
        """The caller's tightest HARD person-limit headroom, or ``None``.

        Review fix on #4689, for the headers surface only — the deny path stays
        ``_check_person_budget``. Both now share ``_resolve_person_limits``, so the
        person key, the ladder rung and the fused denominator are literally the same
        computation; the headroom reported here is the headroom the 402 enforces.
        Evaluated per applicable limit over that limit's OWN period. Returns
        ``(remaining, limit, period_end)`` for the tightest. Faults propagate to the
        caller's fail-open handler ("unavailable"), which is the honest header
        answer for an unreadable person ledger.

        **Since #4690 the reported limit may be a default** the person never
        authored. That is the requirement, not a leak: these headers are read by
        exactly the population defaults exist to bound, and advertising headroom
        that enforcement will not honour is the FR-1.4 class of defect.

        ``soft`` limits are skipped: a non-enforcing ceiling in
        ``X-Budget-Remaining`` would be the opposite lie. Only an individual row can
        be soft — defaults are always written ``hard``.
        """
        from .person_ledger import read_person_partition_spend

        resolved = await self._resolve_person_limits(session, context)
        if resolved is None:
            return None
        # The anchor is dropped here deliberately: headers carry numbers, not a
        # person key, and the deny path is where naming the person matters.
        limits, _anchor, person_user_ids, person_subs, partitions = resolved

        tightest = None
        for limit in sorted(limits.values(), key=lambda item: item.period_type):
            if limit.enforcement_mode != EnforcementMode.HARD.value:
                continue
            period_type = PeriodType(limit.period_type)
            period_start, period_end = get_period_start_end(period_type)
            total = Decimal("0")
            for org_id in partitions:
                cloud, direct = await read_person_partition_spend(session, org_id, person_user_ids, person_subs, period_type, period_start)
                total += cloud + direct
            remaining = limit.amount - total
            if tightest is None or remaining < tightest[0]:
                tightest = (remaining, limit.amount, period_end)
        return tightest

    async def get_budget_status_for_headers(self, context: TokenContext) -> dict[str, Any]:
        """
        Get budget status info for response headers.

        Returns the most restrictive (lowest remaining) budget across the
        hierarchy, considering EVERY calendar period type (Issue #4392) — not
        just monthly. A tenant whose only cap is daily or weekly used to be
        reported as having no cap at all, which is the most reassuring possible
        answer and the wrong one.

        Args:
            context: Token context with user hierarchy info

        Returns:
            One of three self-describing shapes, distinguishable by "status":

              {"status": "ok", "budget_limit": float, "budget_remaining": float,
               "budget_reset": str}   a real cap was found
              {"status": "no_budget"}     nothing configured for any entity x
                                          calendar period
              {"status": "unavailable"}   the lookup FAILED; the caller must NOT
                                          render this as "no limit"

            The three used to be two, and "no budget" and "DB error" were
            byte-identical empty dicts (Issue #4392) — so an outage rendered as
            "you have no limit", reassuring the user exactly when it should not.
            The "ok" shape keeps its original keys so `format_budget_for_headers`
            (src/budget/headers.py) works unchanged; it emits no header for an
            absent key, so `no_budget` and `unavailable` both correctly produce
            NO X-Budget-* headers. Omission is the honest signal on the failure
            path — never a fabricated limit.

        The reported limit is the RAW configured `budget_amount_usd`, and
        `_resolve_scope_cap` is deliberately NOT called here. That is not an
        oversight — Issue #4392 was filed prescribing exactly that, and it would
        be actively harmful. `_resolve_scope_cap` is RUN/CHAIN-only: its platform
        default is a two-way branch that hands `budget_chain_cap_usd` (default
        $100) to every non-RUN entity type, and its override query pins
        `entity_id == "*"` / `period_type == "run"`, so it never matches a
        hierarchy row and returns that $100 unconditionally. An org with a
        configured $50,000 monthly cap would advertise $100. There is also
        nothing to clamp TO: `budget_run_cap_usd`/`budget_chain_cap_usd` are the
        only platform ceilings in the codebase and no hierarchy equivalent
        exists, so `min(configured, platform_default)` has no second operand.
        The raw value is already the truthful one — `_check_entity_budget`
        enforces against this same `budget.budget_amount_usd`, so what this
        function reports is what enforcement honours. See
        `test_hierarchy_cap_is_not_clamped_to_run_chain_ceiling`.

        Fail-OPEN is intentional and must stay: this is a reporting helper, not a
        request gate. Turning a failure here into a denial would be an
        enforcement change.
        """
        try:
            async with self._get_session() as session:
                entities = self._get_entity_hierarchy(context)

                lowest_remaining = None
                corresponding_limit = None
                corresponding_reset = None

                for entity_type, entity_id in entities:
                    # Every calendar-period cap for this entity, not just
                    # monthly (Issue #4392). Gated on the #4328 allowlist rather
                    # than `!= RUN`: `get_period_start_end` RAISES for anything
                    # it does not implement, so a denylist would let the next
                    # period type added to the enum (say QUARTERLY) reach it and
                    # turn this path into a 500 — and rows carry whatever string
                    # is in the column, not necessarily a known enum member.
                    #
                    # Issue #4132: the org_id predicate is the attributed-tenant
                    # partition the check/record paths use, so the headers
                    # describe the ledger actually being enforced. It MUST stay
                    # inside this loop body, pinned to every query below.
                    budget_result = await session.execute(
                        select(BudgetConfig)
                        .where(
                            and_(
                                BudgetConfig.org_id == context.attributed_org_id,
                                BudgetConfig.entity_type == entity_type.value,
                                BudgetConfig.entity_id == entity_id,
                                BudgetConfig.period_type.in_(CALENDAR_PERIOD_TYPES),
                            )
                        )
                        # Deterministic tie-break: with two periods on identical
                        # remaining, the reported reset date must not depend on
                        # row order.
                        .order_by(BudgetConfig.period_type)
                    )
                    budgets = budget_result.scalars().all()

                    for budget in budgets:
                        period_type = PeriodType(budget.period_type)
                        period_start, period_end = get_period_start_end(period_type)

                        # Get current usage for THIS period's window
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

                        # Track the most restrictive (lowest remaining) across
                        # the full entity x calendar-period cross product.
                        if lowest_remaining is None or remaining < lowest_remaining:
                            lowest_remaining = remaining
                            corresponding_limit = budget.budget_amount_usd
                            corresponding_reset = period_end

                # The PERSON layer (review fix on #4689): #4396 subjects every
                # JWT caller to the personal limit, and these headers are read by
                # exactly that population — omitting it advertised headroom
                # enforcement will not honour (the FR-1.4 class). Same primitives
                # as _check_person_budget, so the header figure IS the enforced
                # figure; gated on the same existence cache, so the empty-table
                # common case costs nothing. Soft rows are skipped: a
                # non-enforcing ceiling in X-Budget-Remaining would be the
                # opposite lie.
                person = await self._person_cap_headroom(session, context)
                if person is not None:
                    person_remaining, person_limit, person_reset = person
                    if lowest_remaining is None or person_remaining < lowest_remaining:
                        lowest_remaining = person_remaining
                        corresponding_limit = person_limit
                        corresponding_reset = person_reset

                if lowest_remaining is not None:
                    return {
                        "status": "ok",
                        "budget_limit": float(corresponding_limit),
                        "budget_remaining": float(max(Decimal("0"), lowest_remaining)),
                        "budget_reset": corresponding_reset.isoformat(),
                    }

                return {"status": "no_budget"}

        except Exception as e:
            logger.error(f"Failed to get budget status for headers: {e}")
            # NOT the same value as "no budget configured" — that ambiguity is
            # the defect (Issue #4392). A caller seeing "unavailable" knows the
            # ledger could not be read and must not claim the user is unlimited.
            return {"status": "unavailable"}

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
    actual_cost_usd: Decimal | None = None,
    usage_known: bool = True,
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
            **({"actual_cost_usd": actual_cost_usd} if actual_cost_usd is not None else {}),
            **({"usage_known": False} if not usage_known else {}),
        )
    except Exception as exc:
        logger.warning(f"Budget reservation reconcile failed: {exc}")
