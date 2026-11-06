"""Dormant, model-only tools-test admission primitive (#8078).

No route, credential issuer, run enrollment, meter initialization, provider call,
or settlement/release is provided here. Native identity is unavailable by default.
Only a trusted server adapter may implement NativeTestIdentityVerifier. Calling
this service never turns a caller's quote, fixture flag or header into authority.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from starlette.concurrency import run_in_threadpool

from src.agentauth.test_run_admission_store import AdmissionRefusedError, TestAdmissionStore, digest
from src.budget.config import budget_config
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.reservations import ReservationTarget
from src.orchestration import provider_quotes
from src.orchestration.provider_quotes import ProviderQuote
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EntityType, PeriodType

CAP = Decimal("20")
MODEL_ROUTES = frozenset({"/v1/messages", "/openai/v1/responses"})


class StrictTestBudgetEnforcementService(BudgetEnforcementService):
    """Only test admission refuses person-layer faults; shared behavior is intact."""

    async def _check_person_budget_contained(self, session, context, estimated_cost):
        # The parent policy-target exception boundary converts failures to a
        # refusal before reservation. Do not use the legacy skip-on-fault path.
        return await self._check_person_budget(session, context, estimated_cost)

    async def _confirm_policy_quote(self, context):
        if context._native_test_effect_quote_guard is not None:
            from src.agentauth.native_test_effect_admission import NativeEffectQuoteGuard

            guard = context._native_test_effect_quote_guard
            if type(guard) is not NativeEffectQuoteGuard:
                raise AdmissionRefusedError("native_test_effect_quote_unavailable")
            return await guard.confirm(context, self)
        return await super()._confirm_policy_quote(context)

    def _retained_reservation_targets(self, context, targets):
        original = context._policy_flow_target
        if (
            original is None
            or not original.require_initialization
            or not original.entity_id.startswith("tools-test:")
            or type(original.ttl_seconds) is not int
            or original.ttl_seconds <= 0
        ):
            raise AdmissionRefusedError("test_reservation_lifetime_unavailable")
        # Cleanup and delayed settlement share the original hold. The ordinary
        # 120-second hierarchy TTL cannot erase those reserved dollars first.
        return [
            replace(
                target,
                ttl_seconds=max(target.ttl_seconds or 0, original.ttl_seconds),
                retain_until_settlement=original.retain_until_settlement,
            )
            for target in targets
        ]


@dataclass(frozen=True)
class VerifiedNativeTestIdentity:
    """Internal verifier output, never a public request model.

    The adapter must verify mandatory scoped credential identity, canonical DB
    membership and explicit scoped runtime authority. The dormant CI adapter
    proves an approved job/source, not an arbitrary local executable. Raw JWT
    plus asserted run/source/runtime headers cannot implement this contract.
    """

    tenant_id: str
    subject_id: str
    user_id: str
    team_id: str
    department_id: str
    run_id: str
    credential_id: str
    source_sha256: str
    runtime_id: str


class NativeTestIdentityVerifier(Protocol):
    async def verify(self, context: TokenContext, credential: str) -> VerifiedNativeTestIdentity: ...


class UnavailableNativeTestIdentity:
    async def verify(self, context: TokenContext, credential: str) -> VerifiedNativeTestIdentity:
        raise AdmissionRefusedError("mandatory_native_test_identity_unavailable")


@dataclass(frozen=True)
class AdmissionReceipt:
    operation_id: str
    status: str
    reservation_id: str | None
    quoted_usd: str | None
    reserved_usd: str | None
    replay: bool


@dataclass(frozen=True)
class PreparedModelAdmission:
    """In-process handoff only; do not serialize this object to clients."""

    receipt: AdmissionReceipt
    _authority: dict = field(repr=False)
    _operation: dict = field(repr=False)
    _context: TokenContext = field(repr=False)
    _quote: ProviderQuote = field(repr=False)


def _uuid(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise AdmissionRefusedError("invalid_test_operation_identity") from None
    return value


def _receipt(operation: dict, *, replay: bool) -> AdmissionReceipt:
    quoted = operation.get("quote", {}).get("total_usd")
    return AdmissionReceipt(
        operation["operation_id"],
        operation["status"],
        operation.get("reservation_id"),
        quoted,
        quoted if operation.get("reservation_confirmed") is True else None,
        replay,
    )


def _quote_evidence(quote: ProviderQuote) -> dict:
    evidence = {
        **asdict(quote),
        "total_usd": str(quote.total_usd),
        "issued_at": quote.issued_at.isoformat(),
        "expires_at": quote.expires_at.isoformat(),
    }
    if isinstance(quote, ProviderQuote):
        evidence["upper_bound_usd"] = str(quote.upper_bound_usd) if quote.upper_bound_usd is not None else None
    return evidence


def _target_evidence(target: ReservationTarget) -> dict:
    return {**asdict(target), "headroom_usd": str(target.headroom_usd)}


def _reservation_id(tenant_id: str, run_id: str, operation_id: str) -> str:
    return "tools-test-model:" + digest({"tenant_id": tenant_id, "run_id": run_id, "operation_id": operation_id})


class TestRunAdmissionService:
    __test__ = False
    _authority_state = "active"

    def __init__(
        self,
        *,
        store: TestAdmissionStore,
        budget: StrictTestBudgetEnforcementService | None = None,
        identity_verifier: NativeTestIdentityVerifier | None = None,
        clock=None,
    ):
        self.store = store
        if budget is not None and type(budget) is not StrictTestBudgetEnforcementService:
            raise AdmissionRefusedError("strict_test_budget_service_required")
        self.budget = budget or StrictTestBudgetEnforcementService()
        self.identity_verifier = identity_verifier or UnavailableNativeTestIdentity()
        self.clock = clock or (lambda: datetime.now(UTC))

    async def _authority(self, context: TokenContext, credential: str) -> tuple[VerifiedNativeTestIdentity, dict]:
        if (
            not isinstance(credential, str)
            or not credential
            or context.account_type != "human"
            or context.auth_source != "jwt"
            or context.expires_at <= self.clock()
            or context._policy_flow_target is not None
            or context._protected_run_binding is not None
            or context._run_scope_reservations
            or context._budget_admission_targets is not None
            or context.attributed_org_id != context.org_id
        ):
            raise AdmissionRefusedError("incompatible_test_identity_context")
        identity = await self.identity_verifier.verify(context, credential)
        if (
            not isinstance(identity, VerifiedNativeTestIdentity)
            or any(not isinstance(v, str) or (not v and k != "department_id") for k, v in asdict(identity).items())
            or identity.tenant_id != context.org_id
            or identity.subject_id != context.user_id
            or not re.fullmatch(r"[a-f0-9]{64}", identity.source_sha256)
        ):
            raise AdmissionRefusedError("invalid_native_test_identity")
        _uuid(identity.run_id)
        authority = await run_in_threadpool(self.store.authority, identity.tenant_id, identity.run_id)
        expected = set(asdict(identity)) | {
            "schema_version",
            "cap_usd",
            "meter_epoch",
            "meter_expires_at",
            "dispatch_expires_at",
            "cleanup_expires_at",
            "state",
            "allowed_routes",
            "allowed_models",
            "revision",
        }
        if "scope" in authority:
            from src.agentauth.native_test_enrollment import EffectRegistration

            try:
                effects = [EffectRegistration.model_validate(effect) for effect in authority["effects"]]
                if (
                    authority["scope"] != "all_in"
                    or type(authority.get("started_at")) is not int
                    or authority["started_at"] <= 0
                    or not isinstance(authority.get("deployment_id"), str)
                    or re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", authority["deployment_id"]) is None
                    or not effects
                    or len(effects) > 64
                    or len({effect.effect_key for effect in effects}) != len(effects)
                    or [effect.model_dump() for effect in effects] != authority["effects"]
                ):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise AdmissionRefusedError("invalid_test_run_authority") from None
            expected |= {"scope", "effects", "deployment_id", "started_at"}
        now = int(self.clock().timestamp())
        if (
            set(authority) != expected
            or any(authority.get(k) != v for k, v in asdict(identity).items())
            or authority.get("schema_version") != "1"
            or authority.get("state") != self._authority_state
            or authority.get("cap_usd") != "20"
            or not isinstance(authority.get("revision"), str)
            or not authority["revision"]
            or any(type(authority.get(k)) is not int for k in ("meter_expires_at", "dispatch_expires_at", "cleanup_expires_at"))
            or not now < authority["dispatch_expires_at"] <= authority["cleanup_expires_at"] < authority["meter_expires_at"]
            or not isinstance(authority.get("allowed_routes"), list)
            or (not authority["allowed_routes"] and authority.get("scope") != "all_in")
            or any(not isinstance(route, str) or route not in MODEL_ROUTES for route in authority["allowed_routes"])
            or not isinstance(authority.get("allowed_models"), list)
            or (not authority["allowed_models"] and authority.get("scope") != "all_in")
            or any(not isinstance(model, str) or not model for model in authority["allowed_models"])
        ):
            raise AdmissionRefusedError("invalid_test_run_authority")
        _uuid(authority["meter_epoch"])
        return identity, authority

    def _target(self, authority: dict) -> ReservationTarget:
        return ReservationTarget(
            org_id=authority["tenant_id"],
            entity_type=EntityType.RUN.value,
            entity_id="tools-test:" + authority["run_id"],
            period_type=PeriodType.RUN.value,
            period_start="test-run-v1:" + authority["meter_epoch"],
            headroom_usd=CAP,
            ttl_seconds=authority["meter_expires_at"] - int(self.clock().timestamp()),
            require_initialization=True,
            retain_until_settlement=authority.get("scope") == "all_in",
        )

    def _supports_request(self, route, authority):
        return route in MODEL_ROUTES and route in authority["allowed_routes"]

    def _reservation_id(self, tenant_id, run_id, operation_id):
        return _reservation_id(tenant_id, run_id, operation_id)

    async def _fetch_quote(self, body, route, authority):
        return await provider_quotes.quote_request(body, route, now=self.clock())

    def _quote_permitted(self, quote, authority):
        return isinstance(quote, ProviderQuote) and quote.billing_model_id in authority["allowed_models"]

    async def _validate_quote(self, quote, body, route, authority):
        await provider_quotes.revalidate_quote(quote, body, route, now=self.clock())

    async def _confirm_quote(self, quote):
        return await provider_quotes.confirm_quote_spendable(quote, now=self.clock())

    def _restore_quote(self, evidence):
        return ProviderQuote(
            **{
                **evidence,
                "total_usd": Decimal(evidence["total_usd"]),
                "upper_bound_usd": Decimal(evidence["upper_bound_usd"]) if evidence.get("upper_bound_usd") is not None else None,
                "issued_at": datetime.fromisoformat(evidence["issued_at"]),
                "expires_at": datetime.fromisoformat(evidence["expires_at"]),
            }
        )

    def _quote_evidence(self, quote):
        return _quote_evidence(quote)

    def _bind_quote(self, context, quote, body, route, authority):
        context._policy_quote = quote

    def _transition(self, authority, operation, **kwargs):
        return self.store.transition(authority, operation, **kwargs)

    def _new_operation(self, operation, authority, body):
        return operation

    def _claim_operation(self, authority, operation, now):
        return self.store.claim(authority, operation, now)

    async def prepare_model(
        self, *, context: TokenContext, credential: str, operation_id: str, body: bytes, route: str
    ) -> PreparedModelAdmission | AdmissionReceipt:
        """Claim and reserve once; replay returns status without dispatch authority."""
        identity, authority = await self._authority(context, credential)
        _uuid(operation_id)
        if not self._supports_request(route, authority) or type(body) is not bytes or not 0 < len(body) <= 16 * 1024 * 1024:
            raise AdmissionRefusedError("unsupported_test_provider_quote")
        binding = {
            **asdict(identity),
            "authority_revision": authority["revision"],
            "meter_epoch": authority["meter_epoch"],
            "route": route,
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "operation_id": operation_id,
        }
        reservation_id = self._reservation_id(identity.tenant_id, identity.run_id, operation_id)
        operation = {
            "tenant_id": identity.tenant_id,
            "run_id": identity.run_id,
            "operation_id": operation_id,
            "binding_digest": digest(binding),
            "binding": binding,
            "owner_token": str(uuid4()),
            "version": 1,
            "status": "preparing",
            "reservation_id": reservation_id,
            "reservation_confirmed": False,
        }
        operation = self._new_operation(operation, authority, body)
        operation, fresh = await run_in_threadpool(self._claim_operation, authority, operation, int(self.clock().timestamp()))
        if not fresh:
            return _receipt(operation, replay=True)
        target = self._target(authority)
        # Never overwrite a delivery-flow context. Human model USER ledgers and
        # person-cap resolution use the JWT subject, not canonical users.id.
        # Preserve that namespace; bind the canonical owner in the journal and
        # use the verifier's current team/department membership for hierarchy.
        budget_context = TokenContext(
            user_id=identity.subject_id,
            org_id=identity.tenant_id,
            team_id=identity.team_id,
            department_id=identity.department_id,
            account_type="human",
            expires_at=context.expires_at,
        )
        try:
            posture = await self.budget.prepare_enforcement_context(budget_context, None)
            if (
                posture is not None
                or not budget_context._budget_enforcement_enabled
                or budget_context._budget_accounting_incomplete
                or not budget_config.budget_check_enabled
                or not budget_config.budget_reservation_enabled
            ):
                raise AdmissionRefusedError("strict_test_budget_unavailable")
            if await self.budget._get_reservations().snapshot(target) is None:
                raise AdmissionRefusedError("test_accumulator_unavailable")
            # Quotes originate only in registered server adapters; no quote or
            # caller cost argument exists in this API.
            quote = await self._fetch_quote(body, route, authority)
            if not self._quote_permitted(quote, authority):
                raise AdmissionRefusedError("unsupported_test_provider_quote")
            await self._validate_quote(quote, body, route, authority)
            _, current = await self._authority(context, credential)
            if current != authority:
                raise AdmissionRefusedError("test_authority_changed")
            operation = await run_in_threadpool(
                self._transition, authority, operation, status="quoted", now=int(self.clock().timestamp()), quote=self._quote_evidence(quote)
            )
            budget_context._policy_flow_target = target
            self._bind_quote(budget_context, quote, body, route, authority)
            budget_context._policy_estimated_cost = quote.total_usd
            budget_context._policy_request_id = reservation_id
            result = await self.budget.check_budget_hierarchy(budget_context, quote.total_usd, request_id=reservation_id)
            if not result.allowed or budget_context._budget_accounting_incomplete or not budget_context._budget_enforcement_enabled:
                raise AdmissionRefusedError("test_budget_refused")
            targets = budget_context._budget_admission_targets
            if (
                not targets
                or target not in targets
                or len({t.key() for t in targets}) != len(targets)
                or any(t.org_id != identity.tenant_id for t in targets)
            ):
                raise AdmissionRefusedError("test_reservation_unconfirmed")
            for admitted_target in targets:
                await self._verify_hold(reservation_id, quote.total_usd, admitted_target)
            operation = await run_in_threadpool(
                self._transition,
                authority,
                operation,
                status="admitted",
                now=int(self.clock().timestamp()),
                target_keys=[t.key() for t in targets],
                targets=[_target_evidence(t) for t in targets],
            )
            return PreparedModelAdmission(_receipt(operation, replay=False), authority, operation, budget_context, quote)
        except Exception as exc:
            # Failure after a reservation response may be ambiguous. Never zero
            # or resize any own, sibling or cleanup hold on this path.
            await self.budget._get_reservations().mark_unknown(reservation_id, target)
            try:
                await run_in_threadpool(
                    self._transition,
                    authority,
                    operation,
                    status="unknown",
                    now=int(self.clock().timestamp()),
                    target_keys=[t.key() for t in budget_context._budget_admission_targets or []],
                )
            except AdmissionRefusedError:
                pass
            if isinstance(exc, AdmissionRefusedError):
                raise
            raise AdmissionRefusedError("test_admission_unavailable") from None

    async def _verify_hold(self, reservation_id: str, amount: Decimal, target: ReservationTarget) -> float:
        reservations = self.budget._get_reservations()
        try:
            # A single aggregate read sees own and sibling state together. An
            # unbounded sibling invalidates ordinary hierarchy ledgers too.
            raw = await (await reservations._get_client()).hgetall(target.key())
            entries = {
                key.decode() if isinstance(key, bytes) else key: value.decode() if isinstance(value, bytes) else value for key, value in raw.items()
            }
            if any(key.startswith("unbounded:") for key in entries):
                raise ValueError
            parsed = {}
            now = self.clock().timestamp()
            unsettled = {key.removeprefix("pending:") for key in entries if key.startswith("pending:")}
            for key, value in entries.items():
                held, expires = value.split(":")
                cost, deadline = Decimal(held), float(expires)
                if not cost.is_finite() or cost < 0 or not math.isfinite(deadline):
                    raise ValueError
                if key.startswith(("pending:", "bounded:", "retained:")) and cost != 0:
                    raise ValueError
                if (key.startswith("pending:") or key in unsettled) and deadline <= now:
                    raise ValueError
                parsed[key] = (cost, deadline)
            if any(key not in parsed for key in unsettled):
                raise ValueError
            anchor = parsed.get("__initialized__")
            if target.require_initialization and (anchor is None or anchor[0] != 0):
                raise ValueError
            own = parsed.get(reservation_id)
            if (
                own is None
                or own[0] != amount
                or own[1] <= now
                or "bounded:" + reservation_id in parsed
                or (target.require_initialization and "pending:" + reservation_id not in parsed)
                or (target.retain_until_settlement and "retained:" + reservation_id not in parsed)
            ):
                raise ValueError
            # Later awaits must not outlive this read's validity. Pending
            # siblings becoming expired also turn the whole ledger unknown.
            return min([own[1], *(parsed[key][1] for key in parsed if key.startswith("pending:") or key in unsettled)])
        except Exception:
            raise AdmissionRefusedError("test_reservation_unconfirmed") from None

    def _dispatch_freshness(self, quote: ProviderQuote, authority: dict, context: TokenContext, hold_deadline: float) -> datetime:
        now = self.clock()
        if quote.expired(now):
            raise AdmissionRefusedError("test_quote_unavailable")
        dispatch_expires_at = min(authority["dispatch_expires_at"], authority.get("started_at", authority["dispatch_expires_at"]) + 300)
        if context.expires_at <= now or now.timestamp() >= dispatch_expires_at:
            raise AdmissionRefusedError("test_dispatch_binding_changed")
        if not math.isfinite(hold_deadline) or now.timestamp() >= hold_deadline:
            raise AdmissionRefusedError("test_reservation_unconfirmed")
        return now

    async def begin_model_dispatch(
        self, prepared: PreparedModelAdmission, *, context: TokenContext, credential: str, body: bytes, route: str
    ) -> AdmissionReceipt:
        """Single-use intent CAS immediately before handoff; performs no provider call.

        A caller must retain the original bytes/context and obtain trusted usage
        settlement in a later integration slice. A lost acknowledgement is unknown
        and cannot be retried into another provider effect.
        """
        if not isinstance(prepared, PreparedModelAdmission):
            raise AdmissionRefusedError("test_dispatch_requires_fresh_admission")
        try:
            identity, authority = await self._authority(context, credential)
            if authority != prepared._authority:
                raise AdmissionRefusedError("test_dispatch_binding_changed")
            operation_id = _uuid(prepared._operation["operation_id"])
            operation = await run_in_threadpool(self.store.read_operation, authority["tenant_id"], authority["run_id"], operation_id)
            if operation != prepared._operation or operation.get("status") != "admitted":
                raise AdmissionRefusedError("test_operation_transition_unconfirmed")
            binding = {
                **asdict(identity),
                "authority_revision": authority["revision"],
                "meter_epoch": authority["meter_epoch"],
                "route": route,
                "request_sha256": hashlib.sha256(body).hexdigest(),
                "operation_id": operation_id,
            }
            reservation_id = self._reservation_id(authority["tenant_id"], authority["run_id"], operation_id)
            if (
                operation["binding"] != binding
                or operation["reservation_id"] != reservation_id
                or prepared.receipt != _receipt(operation, replay=False)
            ):
                raise AdmissionRefusedError("test_dispatch_binding_changed")
            # The journal owns the quote and complete target guards. In-process
            # objects must still match, but never supply reservation authority.
            evidence = operation["quote"]
            quote = self._restore_quote(evidence)
            if self._quote_evidence(prepared._quote) != evidence:
                raise AdmissionRefusedError("test_dispatch_binding_changed")
            targets = [ReservationTarget(**{**t, "headroom_usd": Decimal(t["headroom_usd"])}) for t in operation["targets"]]
            expected_keys = operation.get("target_keys")
            mandatory = self._target(authority)
            mandatory_evidence = _target_evidence(mandatory)
            mandatory_evidence.pop("ttl_seconds")
            bound_mandatory = next((target for target in targets if target.key() == mandatory.key()), None)
            bound_evidence = _target_evidence(bound_mandatory) if bound_mandatory is not None else {}
            bound_evidence.pop("ttl_seconds", None)
            if (
                not targets
                or not isinstance(expected_keys, list)
                or not expected_keys
                or [target.key() for target in targets] != expected_keys
                or len(set(expected_keys)) != len(expected_keys)
                or bound_evidence != mandatory_evidence
                or any(target.org_id != authority["tenant_id"] for target in targets)
                or operation["targets"] != [_target_evidence(t) for t in prepared._context._budget_admission_targets or []]
                or prepared._context._policy_flow_target != bound_mandatory
                or prepared._context._policy_request_id != reservation_id
                or prepared._context._policy_quote != quote
                or prepared._context._policy_estimated_cost != quote.total_usd
                or prepared._context.user_id != identity.subject_id
                or prepared._context.org_id != identity.tenant_id
                or prepared._context.team_id != identity.team_id
                or prepared._context.department_id != identity.department_id
            ):
                raise AdmissionRefusedError("test_reservation_unconfirmed")
            posture = await self.budget.prepare_enforcement_context(prepared._context, None)
            if (
                posture is not None
                or prepared._context._budget_accounting_incomplete
                or not prepared._context._budget_enforcement_enabled
                or not budget_config.budget_check_enabled
                or not budget_config.budget_reservation_enabled
            ):
                raise AdmissionRefusedError("strict_test_budget_unavailable")
            await self._validate_quote(quote, body, route, authority)
            hold_deadline = min([await self._verify_hold(reservation_id, quote.total_usd, target) for target in targets])
            if await self._confirm_quote(quote) is not None:
                raise AdmissionRefusedError("test_quote_unavailable")

            def consume_intent():
                # Run after the thread-pool queue, immediately before the CAS.
                # The awaited pricing check can itself outlive the quote.
                now = self._dispatch_freshness(quote, authority, context, hold_deadline)
                return self._transition(authority, operation, status="dispatch_started", now=now.timestamp())

            operation = await run_in_threadpool(consume_intent)
            # A late successful response consumes the intent but cannot grant a
            # provider handoff with an expired quote/authority. Keep all holds.
            self._dispatch_freshness(quote, authority, context, hold_deadline)
            prepared._context._native_test_dispatch_deadline = min(
                quote.expires_at.timestamp(),
                authority["dispatch_expires_at"],
                context.expires_at.timestamp(),
                hold_deadline,
                authority.get("started_at", authority["dispatch_expires_at"]) + 300,
            )
        except Exception as exc:
            if isinstance(exc, AdmissionRefusedError):
                raise
            raise AdmissionRefusedError("test_dispatch_unavailable") from None
        prepared._context._budget_provider_started = True
        return _receipt(operation, replay=False)
