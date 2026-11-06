"""Durable preparation and acknowledged admission before a priced model call."""

import asyncio
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal
from threading import Lock
from time import monotonic

import rfc8785
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pricing_policy import (
    RateRow,
    RoutingEvidence,
    build_pricing_decision,
    load_snapshot,
    normalize_billing_model_id,
    normalize_usage,
    verify_pricing_decision,
)
from pricing_policy.policy import model_rate_candidates
from src.budget.config import budget_config
from src.budget.metering_policy_gates import lock_admission_policy, lock_person_binding
from src.budget.person_ledger import (
    read_person_partition_spend,
    resolve_applicable_person_limits,
    resolve_member_partitions,
    resolve_person_subs,
    resolve_person_team_keys,
)
from src.budget.pricing_v2_reader import cached_rate_state
from src.budget.utils import CALENDAR_PERIOD_TYPES, get_period_start_end
from src.orchestration.provider_quotes import ProviderQuote, TrustedUsage, _pricing_revision, quote_request, request_digest
from src.shared.identity.person_anchor import is_authorable_person_anchor
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.metering import MeteringAttempt, MeteringAttemptScope, MeteringScopeAlias, MeteringScopeGate
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import PeriodType
from src.usage.metering_journal import ImmutableMeteringJournal, MeteringObjectReceipt


class MeteringAdmissionError(Exception):
    pass


class MeteringBudgetExceededError(MeteringAdmissionError):
    pass


class MeteringAccountingUnresolvedError(MeteringAdmissionError):
    pass


_ACTIVE_STATES = ("prepared", "dispatch_armed", "unknown", "quarantined")
_BUDGET_ENTITY_TYPES = {
    "org": "org",
    "user": "service_account",
    "agent": "agent",
    "team": "team",
    "department": "department",
    "root_user": "root_user",
}


@dataclass(frozen=True)
class PreparedMeteringAttempt:
    receipt: MeteringObjectReceipt
    quote: ProviderQuote
    deadline: float
    scope_revisions: dict[str, int]
    scope_bindings: dict[str, tuple[str, str]]
    submitted_sha256: str
    quote_sha256: str
    producer_epoch: int
    pricing_basis: dict


@dataclass(frozen=True)
class MeteringDispatchPermit:
    """One-use accounting permission; #7033 must consume it at physical handoff.

    The transport must acquire its connection and credentials first, read the
    process-local published pricing revision, then call consume synchronously
    immediately before submitting the bytes. An HTTPX hook or an awaited call
    between consume and submission does not implement this contract.
    """

    tenant_id: str
    request_id: str
    revision: int
    producer_epoch: int
    quote: ProviderQuote
    submitted_sha256: str
    destination_sha256: str
    expires_at: datetime
    monotonic_deadline: float
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _used: bool = field(default=False, init=False, repr=False)
    _physical_submitted: bool = field(default=False, init=False, repr=False)
    _revoked: bool = field(default=False, init=False, repr=False)

    def mark_physical_submission(self) -> None:
        with self._lock:
            if not self._used or self._physical_submitted or self._revoked:
                raise MeteringAdmissionError("dispatch permit cannot submit twice")
            object.__setattr__(self, "_physical_submitted", True)

    def revoke_unsent(self) -> None:
        with self._lock:
            if self._physical_submitted:
                raise MeteringAdmissionError("armed cancellation cannot be proved")
            object.__setattr__(self, "_used", True)
            object.__setattr__(self, "_revoked", True)

    def proven_unsent(self) -> bool:
        with self._lock:
            return not self._physical_submitted

    def consume(self, *, submitted: bytes, model: str, destination: str, pricing_revision: str) -> None:
        with self._lock:
            if self._used:
                raise MeteringAdmissionError("dispatch permit already consumed")
            object.__setattr__(self, "_used", True)
            if (
                monotonic() >= self.monotonic_deadline
                or datetime.now(UTC) >= self.expires_at
                or request_digest(submitted) != self.submitted_sha256
                or request_digest(destination.encode()) != self.destination_sha256
                or self.quote.binds(
                    request_sha256=self.quote.request_sha256,
                    billing_model_id=normalize_billing_model_id(model),
                    pricing_revision=pricing_revision,
                )
                is not None
            ):
                raise MeteringAdmissionError("dispatch permit expired or binding changed")


class MeteringAdmission:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        journal: ImmutableMeteringJournal,
        *,
        settlement_session_factory: Callable[[], AsyncSession] | None = None,
        max_open_per_scope: int = 1000,
    ) -> None:
        if type(max_open_per_scope) is not int or max_open_per_scope < 1:
            raise ValueError("max_open_per_scope must be positive")
        self._session_factory = session_factory
        self._settlement_session_factory = settlement_session_factory or session_factory
        self._journal = journal
        self._max_open_per_scope = max_open_per_scope

    async def _check_scope(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        scope_key: str,
        scope_kind: str,
        identity: str,
        bound: Decimal,
        now: datetime,
        exclude_request_id: str | None = None,
        enforce_caps: bool = True,
    ) -> None:
        exposure = (
            select(MeteringAttempt.state, MeteringAttempt.prepare_deadline, MeteringAttempt.completion_deadline, MeteringAttemptScope.bound_usd)
            .join(
                MeteringAttemptScope,
                and_(
                    MeteringAttempt.tenant_id == MeteringAttemptScope.tenant_id,
                    MeteringAttempt.request_id == MeteringAttemptScope.request_id,
                ),
            )
            .where(
                MeteringAttemptScope.scope_key == scope_key,
                MeteringAttemptScope.allocation_period == "all",
                MeteringAttempt.state.in_(_ACTIVE_STATES),
            )
        )
        if exclude_request_id is not None:
            exposure = exposure.where(~and_(MeteringAttempt.tenant_id == tenant_id, MeteringAttempt.request_id == exclude_request_id))
        result = await session.execute(exposure.limit(self._max_open_per_scope + 1))
        open_attempts = result.all()
        for state, prepare_deadline, completion_deadline, _ in open_attempts:
            if (
                state in {"unknown", "quarantined"}
                or (state == "prepared" and prepare_deadline <= now)
                or (state == "dispatch_armed" and completion_deadline <= now)
            ):
                raise MeteringAccountingUnresolvedError("unresolved metering exposure")
        if len(open_attempts) >= self._max_open_per_scope:
            raise MeteringAccountingUnresolvedError("metering scope backlog exceeded")
        open_bound = sum((row.bound_usd for row in open_attempts), Decimal("0"))
        if scope_kind == "person" or not enforce_caps:
            return
        configs = await session.scalars(
            select(BudgetConfig).where(
                BudgetConfig.org_id == tenant_id,
                BudgetConfig.entity_type == _BUDGET_ENTITY_TYPES[scope_kind],
                BudgetConfig.entity_id == identity,
                BudgetConfig.period_type.in_(CALENDAR_PERIOD_TYPES),
                BudgetConfig.enforcement_mode == "hard",
            )
        )
        for config in configs:
            period_start, _ = get_period_start_end(PeriodType(config.period_type), reference_date=now.date())
            settled = await session.scalar(
                select(BudgetUsage.total_cost_usd).where(
                    BudgetUsage.org_id == tenant_id,
                    BudgetUsage.entity_type == ("user" if config.entity_type == "service_account" else config.entity_type),
                    BudgetUsage.entity_id == identity,
                    BudgetUsage.period_type == config.period_type,
                    BudgetUsage.period_start == period_start,
                )
            )
            cap = config.budget_amount_usd
            if budget_config.budget_period_cap_usd is not None:
                cap = min(cap, budget_config.budget_period_cap_usd)
            if (settled or Decimal("0")) + open_bound + bound > cap:
                raise MeteringBudgetExceededError("metering scope budget exhausted")

    async def _person_scopes(self, session: AsyncSession, root_user_id: str):
        try:
            anchor, person_user_ids = await lock_person_binding(session, root_user_id)
        except ValueError as exc:
            raise MeteringAdmissionError("person binding changed during admission") from exc
        if anchor is None:
            raise MeteringAdmissionError("human root identity is unresolved")
        aliases = (
            await session.scalars(
                select(MeteringScopeAlias).where(MeteringScopeAlias.alias_kind == "person_user", MeteringScopeAlias.stable_alias.in_(person_user_ids))
            )
        ).all()
        anchors = {anchor, *(alias.identity_revision for alias in aliases)}
        bindings = {f"v1:person:{request_digest(rfc8785.dumps(['person', value]))}": ("person", value) for value in anchors}
        if any(alias.scope_key not in bindings for alias in aliases):
            raise MeteringAdmissionError("person alias binding cannot be verified")
        existing = {(alias.stable_alias, alias.scope_key) for alias in aliases}
        new_aliases = [
            MeteringScopeAlias(alias_kind="person_user", stable_alias=user_id, scope_key=key, identity_revision=value)
            for user_id in person_user_ids
            for key, (_, value) in bindings.items()
            if (user_id, key) not in existing
        ]
        return bindings, new_aliases, anchor, person_user_ids

    async def _check_person_cap(
        self,
        session: AsyncSession,
        *,
        anchors: set[str],
        person_user_ids: list[str],
        person_scope_keys: list[str],
        tenant_id: str,
        request_id: str,
        bound: Decimal,
        now: datetime,
    ) -> None:
        partitions = await resolve_member_partitions(session, person_user_ids, tenant_id)
        defaults = await resolve_member_partitions(session, person_user_ids, None)
        teams = await resolve_person_team_keys(session, person_user_ids)
        subs = await resolve_person_subs(session, person_user_ids)
        attempts = (
            await session.execute(
                select(MeteringAttempt.tenant_id, MeteringAttempt.request_id, MeteringAttempt.bound_usd)
                .join(
                    MeteringAttemptScope,
                    and_(MeteringAttempt.tenant_id == MeteringAttemptScope.tenant_id, MeteringAttempt.request_id == MeteringAttemptScope.request_id),
                )
                .where(
                    MeteringAttemptScope.scope_key.in_(person_scope_keys),
                    MeteringAttemptScope.allocation_period == "all",
                    MeteringAttempt.state.in_(_ACTIVE_STATES),
                    ~and_(MeteringAttempt.tenant_id == tenant_id, MeteringAttempt.request_id == request_id),
                )
                .distinct()
            )
        ).all()
        open_bound = sum((row.bound_usd for row in attempts), Decimal("0"))
        for anchor in sorted(anchors):
            limits = await resolve_applicable_person_limits(
                session,
                person_anchor=anchor if is_authorable_person_anchor(anchor) else None,
                org_ids=defaults,
                team_keys=teams,
            )
            for limit in limits.values():
                if limit.enforcement_mode != "hard":
                    continue
                period = PeriodType(limit.period_type)
                start, _ = get_period_start_end(period, reference_date=now.date())
                spent = Decimal("0")
                for partition in partitions:
                    cloud, direct = await read_person_partition_spend(session, partition, person_user_ids, subs, period, start)
                    spent += cloud + direct
                if spent + open_bound + bound > limit.amount:
                    raise MeteringBudgetExceededError("metering person budget exhausted")

    async def prepare(
        self,
        *,
        context: TokenContext,
        request_id: str,
        model: str,
        original: bytes,
        submitted: bytes,
        deadline: float,
        completion_timeout_seconds: float = 728.0,
    ) -> PreparedMeteringAttempt:
        binding = context._protected_run_binding
        if context.account_type != "service" or context.attributed_org_id != context.org_id:
            raise MeteringAdmissionError("delegated attribution requires active scope resolution")
        if binding is not None and binding.tenant_id != context.org_id:
            raise MeteringAdmissionError("run tenant differs from authenticated tenant")
        root_user_id = binding.root_human_id if binding is not None and binding.is_human_rooted is True else None
        if context.attributed_user_id != (root_user_id or context.user_id):
            raise MeteringAdmissionError("delegated attribution requires active scope resolution")
        if context._policy_flow_target is not None or (binding is not None and binding.flow_id):
            raise MeteringAdmissionError("strict policy scope is unavailable for durable metering")
        if any(target.require_initialization and target.entity_type != "project" for target in context._run_scope_reservations):
            raise MeteringAdmissionError("delegated attribution requires active scope resolution")
        quote = context._policy_quote or await quote_request(original, "/openai/v1/responses")
        if (
            quote.binds(
                request_sha256=request_digest(original),
                billing_model_id=normalize_billing_model_id(model),
                pricing_revision=quote.pricing_revision,
            )
            is not None
        ):
            raise MeteringAdmissionError("request quote binding is unavailable")
        if not math.isfinite(completion_timeout_seconds) or completion_timeout_seconds <= 0:
            raise MeteringAdmissionError("completion timeout must be finite and positive")
        now = datetime.now(UTC)
        bound = quote.total_usd.quantize(Decimal("0.000001"), rounding=ROUND_UP)
        if not bound.is_finite() or bound <= 0 or deadline <= asyncio.get_running_loop().time():
            raise MeteringAdmissionError("metering admission deadline or bound is invalid")
        tenant_id = context.org_id
        scopes = [("org", tenant_id), ("user", context.user_id)]
        if context.team_id:
            scopes.append(("team", context.team_id))
        if context.department_id:
            scopes.append(("department", context.department_id))
        if context.account_type == "service":
            scopes.append(("agent", context.user_id))
        scopes = sorted({(kind, identity) for kind, identity in scopes})
        if root_user_id and root_user_id != context.user_id:
            scopes.append(("root_user", root_user_id))
        bindings = {f"v1:tenant:{request_digest(rfc8785.dumps([tenant_id, kind, identity]))}": (kind, identity) for kind, identity in scopes}
        scope_keys = sorted(bindings)
        admission_key, final_key = self._journal.object_keys(tenant_id, request_id)
        submitted_sha256 = request_digest(submitted)
        snapshot = load_snapshot()
        state = cached_rate_state()
        if quote.pricing_revision != _pricing_revision(snapshot.snapshot_version, state.source, state.generation_id, state.pointer_revision):
            raise MeteringAdmissionError("pricing generation changed before durable admission")
        published = model_rate_candidates(state.rows, quote.billing_model_id) if state.from_database else ()
        rows = published or model_rate_candidates(snapshot.rates, quote.billing_model_id)
        if not rows:
            raise MeteringAdmissionError("historical pricing rows unavailable")
        pricing_basis = {
            "snapshot_version": snapshot.snapshot_version,
            "source_kind": "database" if published else "bundled_snapshot",
            "generation_id": state.generation_id if published else None,
            "pointer_revision": state.pointer_revision if published else None,
            "rate_rows": [{key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(row).items()} for row in rows],
        }
        quote_snapshot = asdict(quote)
        quote_snapshot["pricing_basis"] = pricing_basis
        quote_snapshot["total_usd"] = str(quote.total_usd)
        quote_snapshot["upper_bound_usd"] = str(quote.maximum_cost_usd)
        quote_snapshot["issued_at"] = quote.issued_at.isoformat()
        quote_snapshot["expires_at"] = quote.expires_at.isoformat()
        quote_sha256 = request_digest(rfc8785.dumps(quote_snapshot))
        scope_revisions = {}
        async with self._session_factory() as session:
            await lock_admission_policy(session, tenant_id=tenant_id)
            person_aliases = []
            person_anchors = set()
            person_user_ids = []
            if root_user_id:
                person_bindings, person_aliases, _, person_user_ids = await self._person_scopes(session, root_user_id)
                person_anchors = {identity for _, identity in person_bindings.values()}
                bindings.update(person_bindings)
                scope_keys = sorted(bindings)
                await lock_admission_policy(
                    session,
                    tenant_id=tenant_id,
                    person_anchors=person_anchors,
                    org_ids=await resolve_member_partitions(session, person_user_ids, None),
                    team_keys=await resolve_person_team_keys(session, person_user_ids),
                )
            for scope_key in scope_keys:
                gate = await session.scalar(select(MeteringScopeGate).where(MeteringScopeGate.scope_key == scope_key).with_for_update())
                if (
                    gate is None
                    or gate.tenant_id != (None if bindings[scope_key][0] == "person" else tenant_id)
                    or gate.scope_kind != bindings[scope_key][0]
                    or gate.activation != "active"
                    or gate.readiness != "ready"
                ):
                    raise MeteringAdmissionError("metering scope is not activated")
                scope_revisions[scope_key] = gate.revision
            if person_aliases:
                registered = {
                    (alias.stable_alias, alias.scope_key)
                    for alias in await session.scalars(
                        select(MeteringScopeAlias).where(
                            MeteringScopeAlias.alias_kind == "person_user",
                            MeteringScopeAlias.stable_alias.in_(person_user_ids),
                            MeteringScopeAlias.scope_key.in_([key for key in scope_keys if bindings[key][0] == "person"]),
                        )
                    )
                }
                pending = [alias for alias in person_aliases if (alias.stable_alias, alias.scope_key) not in registered]
                session.add_all(pending)
                await session.flush()
            now = datetime.now(UTC)
            try:
                for scope_key in scope_keys:
                    await self._check_scope(
                        session,
                        tenant_id=tenant_id,
                        scope_key=scope_key,
                        scope_kind=bindings[scope_key][0],
                        identity=bindings[scope_key][1],
                        bound=bound,
                        now=now,
                        enforce_caps=context._budget_enforcement_enabled,
                    )
                if person_anchors and context._budget_enforcement_enabled:
                    await self._check_person_cap(
                        session,
                        anchors=person_anchors,
                        person_user_ids=person_user_ids,
                        person_scope_keys=[key for key in scope_keys if bindings[key][0] == "person"],
                        tenant_id=tenant_id,
                        request_id=request_id,
                        bound=bound,
                        now=now,
                    )
            except (MeteringAccountingUnresolvedError, MeteringBudgetExceededError):
                if person_aliases:
                    await session.commit()
                raise
            session.add(
                MeteringAttempt(
                    tenant_id=tenant_id,
                    request_id=request_id,
                    owner_id=context.user_id,
                    model_id=model,
                    request_sha256=quote.request_sha256,
                    submitted_sha256=submitted_sha256,
                    pricing_revision=quote.pricing_revision,
                    quote_snapshot=quote_snapshot,
                    attribution_snapshot={
                        "tenant_id": tenant_id,
                        "owner_id": context.user_id,
                        "team_id": context.team_id,
                        "root_user_id": root_user_id or "",
                        "department_id": context.department_id,
                        "account_type": context.account_type,
                        "scopes": scope_keys,
                        "scope_revisions": scope_revisions,
                        "scope_bindings": {key: list(value) for key, value in bindings.items()},
                        "enforce_caps": context._budget_enforcement_enabled,
                        **({"project_attribution": asdict(context._project_attribution)} if context._project_attribution is not None else {}),
                    },
                    bound_usd=bound,
                    state="prepared",
                    prepare_deadline=now + timedelta(seconds=max(0, deadline - asyncio.get_running_loop().time())),
                    completion_deadline=now + timedelta(seconds=completion_timeout_seconds),
                    admission_key=admission_key,
                    final_key=final_key,
                )
            )
            for scope_key in scope_keys:
                session.add(
                    MeteringAttemptScope(
                        tenant_id=tenant_id,
                        request_id=request_id,
                        scope_key=scope_key,
                        allocation_period="all",
                        bound_usd=bound,
                        captured_allocation={"tenant_id": tenant_id, "owner_id": context.user_id},
                    )
                )
            await session.commit()
        receipt = await self._journal.write_admission(
            tenant_id=tenant_id,
            request_id=request_id,
            owner_id=context.user_id,
            quote=quote,
            submitted_sha256=submitted_sha256,
            deadline=deadline,
        )
        async with self._session_factory() as session:
            updated = await session.execute(
                update(MeteringAttempt)
                .where(
                    MeteringAttempt.tenant_id == tenant_id,
                    MeteringAttempt.request_id == request_id,
                    MeteringAttempt.owner_id == context.user_id,
                    MeteringAttempt.state == "prepared",
                    MeteringAttempt.revision == 1,
                )
                .values(admission_version=receipt.version_id, admission_sha256=receipt.sha256, revision=2)
            )
            if updated.rowcount != 1:
                await session.rollback()
                raise MeteringAdmissionError("admission acknowledgment could not be recorded")
            await session.commit()
        return PreparedMeteringAttempt(
            receipt=receipt,
            quote=quote,
            deadline=deadline,
            scope_revisions=scope_revisions,
            scope_bindings=bindings,
            submitted_sha256=submitted_sha256,
            quote_sha256=quote_sha256,
            producer_epoch=1,
            pricing_basis=pricing_basis,
        )

    async def arm(self, attempt: PreparedMeteringAttempt, *, submitted: bytes, model: str, destination: str) -> MeteringDispatchPermit:
        if (
            request_digest(submitted) != attempt.submitted_sha256
            or normalize_billing_model_id(model) != attempt.quote.billing_model_id
            or not destination.startswith("https://")
        ):
            raise MeteringAdmissionError("dispatch request binding changed")
        if attempt.quote.expired() or asyncio.get_running_loop().time() >= attempt.deadline:
            raise MeteringAdmissionError("dispatch permit expired before submission")
        async with self._session_factory() as session:
            await lock_admission_policy(session, tenant_id=attempt.receipt.tenant_id)
            person_scope_keys = [key for key, (kind, _) in attempt.scope_bindings.items() if kind == "person"]
            root_user_id = next(
                (identity for kind, identity in attempt.scope_bindings.values() if kind == "root_user"),
                attempt.receipt.owner_id if person_scope_keys else None,
            )
            person_anchors = set()
            person_user_ids = []
            if root_user_id:
                person_bindings, _, _, person_user_ids = await self._person_scopes(session, root_user_id)
                person_anchors = {identity for kind, identity in person_bindings.values() if kind == "person"}
                await lock_admission_policy(
                    session,
                    tenant_id=attempt.receipt.tenant_id,
                    person_anchors=person_anchors,
                    org_ids=await resolve_member_partitions(session, person_user_ids, None),
                    team_keys=await resolve_person_team_keys(session, person_user_ids),
                )
                if set(person_bindings) != set(person_scope_keys):
                    raise MeteringAdmissionError("person binding changed before dispatch")
            for scope_key in sorted(attempt.scope_revisions):
                gate = await session.scalar(select(MeteringScopeGate).where(MeteringScopeGate.scope_key == scope_key).with_for_update())
                kind, _ = attempt.scope_bindings[scope_key]
                if (
                    gate is None
                    or gate.activation != "active"
                    or gate.readiness != "ready"
                    or gate.revision != attempt.scope_revisions[scope_key]
                    or gate.scope_kind != kind
                    or gate.tenant_id != (None if kind == "person" else attempt.receipt.tenant_id)
                ):
                    raise MeteringAdmissionError("metering policy changed before dispatch")
            row = await session.scalar(
                select(MeteringAttempt)
                .where(
                    MeteringAttempt.tenant_id == attempt.receipt.tenant_id,
                    MeteringAttempt.request_id == attempt.receipt.request_id,
                )
                .with_for_update()
            )
            if (
                row is None
                or row.owner_id != attempt.receipt.owner_id
                or row.state != "prepared"
                or row.revision != 2
                or row.producer_epoch != attempt.producer_epoch
                or row.admission_version != attempt.receipt.version_id
                or row.admission_sha256 != attempt.receipt.sha256
                or row.request_sha256 != attempt.quote.request_sha256
                or row.submitted_sha256 != attempt.submitted_sha256
                or row.pricing_revision != attempt.quote.pricing_revision
                or row.model_id != model
                or request_digest(rfc8785.dumps(row.quote_snapshot)) != attempt.quote_sha256
                or row.bound_usd != attempt.quote.total_usd.quantize(Decimal("0.000001"), rounding=ROUND_UP)
                or row.attribution_snapshot.get("scope_revisions") != attempt.scope_revisions
                or row.attribution_snapshot.get("scope_bindings") != {key: list(value) for key, value in attempt.scope_bindings.items()}
                or row.attribution_snapshot.get("root_user_id") != (root_user_id or "")
            ):
                raise MeteringAdmissionError("admission cannot be armed")
            now = datetime.now(UTC)
            if now >= row.prepare_deadline or asyncio.get_running_loop().time() >= attempt.deadline or attempt.quote.expired(now):
                raise MeteringAdmissionError("dispatch permit expired before submission")
            for scope_key in sorted(attempt.scope_revisions):
                kind, identity = attempt.scope_bindings[scope_key]
                await self._check_scope(
                    session,
                    tenant_id=row.tenant_id,
                    scope_key=scope_key,
                    scope_kind=kind,
                    identity=identity,
                    bound=row.bound_usd,
                    now=now,
                    exclude_request_id=row.request_id,
                    enforce_caps=row.attribution_snapshot.get("enforce_caps", True),
                )
            if person_anchors and row.attribution_snapshot.get("enforce_caps", True):
                await self._check_person_cap(
                    session,
                    anchors=person_anchors,
                    person_user_ids=person_user_ids,
                    person_scope_keys=person_scope_keys,
                    tenant_id=row.tenant_id,
                    request_id=row.request_id,
                    bound=row.bound_usd,
                    now=now,
                )
            if row.attribution_snapshot.get("project_attribution"):
                from src.budget.enforcement_service import budget_enforcement_service
                from src.budget.project_budget import prepare_project_budget
                from src.products.work_scope import ProjectAttribution

                scope = ProjectAttribution(**row.attribution_snapshot["project_attribution"])
                if scope.tenant_id != row.tenant_id:
                    raise MeteringAdmissionError("Project tenant changed before dispatch")
                store = budget_enforcement_service._get_reservations()
                target = await prepare_project_budget(session, store, scope, enforce=row.attribution_snapshot.get("enforce_caps", True))
                # The prepared SQL attempt already exists, so an interrupted arm
                # has durable unsent recovery evidence for its retained hold.
                target = replace(target, pending_marker_seconds=1000000000000)
                outcome = await store.reserve(row.request_id, attempt.quote.maximum_cost_usd, [target])
                if outcome is None or outcome.usage_unavailable:
                    raise MeteringAccountingUnresolvedError("Project accounting unavailable")
                if not outcome.admitted:
                    raise MeteringBudgetExceededError("Project budget exhausted")
            row.state = "dispatch_armed"
            row.revision = 3
            row.dispatch_deadline = min(
                attempt.quote.expires_at,
                now + timedelta(seconds=min(1, max(0, attempt.deadline - asyncio.get_running_loop().time()))),
            )
            await session.commit()
            return MeteringDispatchPermit(
                tenant_id=row.tenant_id,
                request_id=row.request_id,
                revision=row.revision,
                producer_epoch=row.producer_epoch,
                quote=attempt.quote,
                submitted_sha256=attempt.submitted_sha256,
                destination_sha256=request_digest(destination.encode()),
                expires_at=row.dispatch_deadline,
                monotonic_deadline=min(attempt.deadline, monotonic() + 1),
            )

    async def cancel_unarmed(self, attempt: PreparedMeteringAttempt) -> None:
        """Release only a still-prepared attempt; an armed send is ambiguous."""
        async with self._session_factory() as session:
            for scope_key in sorted(attempt.scope_revisions):
                await session.scalar(select(MeteringScopeGate).where(MeteringScopeGate.scope_key == scope_key).with_for_update())
            row = await session.scalar(
                select(MeteringAttempt)
                .where(MeteringAttempt.tenant_id == attempt.receipt.tenant_id, MeteringAttempt.request_id == attempt.receipt.request_id)
                .with_for_update()
            )
            if (
                row is None
                or row.state != "prepared"
                or row.revision != 2
                or row.producer_epoch != attempt.producer_epoch
                or row.admission_version != attempt.receipt.version_id
                or row.admission_sha256 != attempt.receipt.sha256
            ):
                raise MeteringAdmissionError("unarmed cancellation cannot be proved")
            row.state = "cancelled_unsubmitted"
            row.revision += 1
            await session.commit()

    async def cancel_armed_unsent(self, attempt: PreparedMeteringAttempt, proof: object) -> None:
        from src.proxy.metered_transport import MeteredProvenUnsentError, MeteredUnsentCancelled

        if not isinstance(proof, MeteredProvenUnsentError | MeteredUnsentCancelled):
            raise MeteringAdmissionError("armed cancellation requires transport proof")
        permit = proof.permit
        if (
            permit.tenant_id != attempt.receipt.tenant_id
            or permit.request_id != attempt.receipt.request_id
            or permit.producer_epoch != attempt.producer_epoch
            or not permit.proven_unsent()
        ):
            raise MeteringAdmissionError("armed cancellation cannot be proved")
        permit.revoke_unsent()
        deadline = asyncio.get_running_loop().time() + 2
        receipt = await self._journal.write_unsent(
            attempt.receipt,
            revision=permit.revision,
            producer_epoch=permit.producer_epoch,
            request_sha256=attempt.quote.request_sha256,
            submitted_sha256=attempt.submitted_sha256,
            quote_sha256=attempt.quote_sha256,
            destination_sha256=permit.destination_sha256,
            deadline=deadline,
        )
        evidence = await self._journal.read_unsent(receipt, deadline=deadline)
        if (
            evidence["admission_sha256"] != attempt.receipt.sha256
            or evidence["admission_version_id"] != attempt.receipt.version_id
            or evidence["revision"] != permit.revision
            or evidence["producer_epoch"] != permit.producer_epoch
            or evidence["request_sha256"] != attempt.quote.request_sha256
            or evidence["submitted_sha256"] != attempt.submitted_sha256
            or evidence["quote_sha256"] != attempt.quote_sha256
            or evidence["destination_sha256"] != permit.destination_sha256
            or await self._journal.discover_final(attempt.receipt, deadline=deadline) is not None
        ):
            raise MeteringAdmissionError("armed no-send evidence conflicts with admitted request")
        async with self._session_factory() as session:
            for scope_key in sorted(attempt.scope_revisions):
                await session.scalar(select(MeteringScopeGate).where(MeteringScopeGate.scope_key == scope_key).with_for_update())
            row = await session.scalar(
                select(MeteringAttempt)
                .where(MeteringAttempt.tenant_id == permit.tenant_id, MeteringAttempt.request_id == permit.request_id)
                .with_for_update()
            )
            if (
                row is None
                or row.state != "dispatch_armed"
                or row.owner_id != attempt.receipt.owner_id
                or row.request_sha256 != attempt.quote.request_sha256
                or row.submitted_sha256 != attempt.submitted_sha256
                or request_digest(rfc8785.dumps(row.quote_snapshot)) != attempt.quote_sha256
                or row.revision != permit.revision
                or row.producer_epoch != permit.producer_epoch
                or row.admission_version != attempt.receipt.version_id
                or row.admission_sha256 != attempt.receipt.sha256
                or row.final_version is not None
                or not permit.proven_unsent()
            ):
                raise MeteringAdmissionError("armed cancellation cannot be proved")
            row.state = "cancelled_unsubmitted"
            row.reconciliation_status = "proven_unsubmitted"
            row.revision += 1
            await session.commit()

    @staticmethod
    def price_final(*, pricing_basis: dict, usage: dict, routing: RoutingEvidence, request_id: str, tenant_id: str, bound: Decimal):
        snapshot = load_snapshot(pricing_basis["snapshot_version"])
        rows = tuple(RateRow.from_mapping(row) for row in pricing_basis["rate_rows"])
        decision = build_pricing_decision(
            request_id=request_id,
            org_id=tenant_id,
            usage=normalize_usage(usage, api_format="openai"),
            evidence=routing,
            rows=rows,
            snapshot=snapshot,
            source_kind=pricing_basis["source_kind"],
            generation_id=pricing_basis["generation_id"],
            pointer_revision=pricing_basis["pointer_revision"],
        )
        amount = verify_pricing_decision(decision.to_dict(), request_id=request_id, org_id=tenant_id)
        if amount > bound or routing.billing_model_id not in {row.model_id for row in rows}:
            raise MeteringAdmissionError("measured usage exceeds immutable pricing bound")
        return decision

    async def settle_receipt(self, attempt: PreparedMeteringAttempt, receipt: MeteringObjectReceipt) -> bool:
        from src.budget.metering_settlement import settle_metering_receipt

        return await settle_metering_receipt(
            self._settlement_session_factory,
            self._journal,
            receipt=receipt,
            admission=attempt.receipt,
            scope_keys=tuple(sorted(attempt.scope_revisions)),
        )

    async def mark_unknown(self, attempt: PreparedMeteringAttempt) -> bool:
        async with self._settlement_session_factory() as session:
            for scope_key in sorted(attempt.scope_revisions):
                gate = await session.scalar(select(MeteringScopeGate).where(MeteringScopeGate.scope_key == scope_key).with_for_update())
                if gate is None:
                    raise MeteringAdmissionError("metering scope gate missing during unknown containment")
            updated = await session.execute(
                update(MeteringAttempt)
                .where(
                    MeteringAttempt.tenant_id == attempt.receipt.tenant_id,
                    MeteringAttempt.request_id == attempt.receipt.request_id,
                    MeteringAttempt.owner_id == attempt.receipt.owner_id,
                    MeteringAttempt.state == "dispatch_armed",
                    MeteringAttempt.revision == 3,
                    MeteringAttempt.producer_epoch == attempt.producer_epoch,
                    MeteringAttempt.admission_version == attempt.receipt.version_id,
                    MeteringAttempt.admission_sha256 == attempt.receipt.sha256,
                    MeteringAttempt.final_version.is_(None),
                )
                .values(state="unknown", revision=MeteringAttempt.revision + 1)
            )
            await session.commit()
            return updated.rowcount == 1

    async def write_final(
        self,
        *,
        attempt: PreparedMeteringAttempt,
        usage: dict,
        trusted: TrustedUsage,
        provider_request_id: str | None,
        decision,
        latency_ms: int,
    ) -> MeteringObjectReceipt:
        receipt = await self._journal.write_final(
            admission=attempt.receipt,
            usage=usage,
            trusted=trusted,
            provider_request_id=provider_request_id,
            deadline=asyncio.get_running_loop().time() + 3,
            pricing_decision=decision.to_dict(),
            completed_at=datetime.now(UTC).isoformat(),
            latency_ms=latency_ms,
        )
        async with self._settlement_session_factory() as session:
            updated = await session.execute(
                update(MeteringAttempt)
                .where(
                    MeteringAttempt.tenant_id == receipt.tenant_id,
                    MeteringAttempt.request_id == receipt.request_id,
                    MeteringAttempt.owner_id == receipt.owner_id,
                    MeteringAttempt.state == "dispatch_armed",
                    MeteringAttempt.admission_version == attempt.receipt.version_id,
                    MeteringAttempt.admission_sha256 == attempt.receipt.sha256,
                    MeteringAttempt.final_version.is_(None),
                )
                .values(final_version=receipt.version_id, final_sha256=receipt.sha256)
            )
            if updated.rowcount != 1:
                await session.rollback()
                raise MeteringAdmissionError("final receipt cannot be attached to attempt")
            await session.commit()
        return receipt
