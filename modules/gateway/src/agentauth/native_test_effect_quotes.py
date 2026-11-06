"""Complete server-owned paid-effect quotes; missing terms are never zero.

There is deliberately no production tariff registration here. AgentCore resource
bounds and public rates support individual formulas, not the whole lifecycle.
The source adapter must also prove attempts, INIT, storage, network, observation,
shared allocation, setup and original-owner cleanup before returning a quote.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_UP, Decimal, localcontext
from typing import Protocol

from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest

QUANTUM = Decimal("0.000001")
COMPONENTS = frozenset(
    {
        "provider_compute",
        "provider_requests",
        "model_setup",
        "lambda_duration",
        "lambda_init",
        "lambda_requests",
        "lambda_storage",
        "kms",
        "dynamodb_requests",
        "dynamodb_storage",
        "sqs",
        "network",
        "observability",
        "shared_allocation",
        "runner",
        "evidence",
        "cleanup",
    }
)


def sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def amount(value):
    if not isinstance(value, str) or len(value) > 80:
        raise AdmissionRefusedError("native_test_cost_term_unavailable")
    try:
        result = Decimal(value)
        if not result.is_finite() or result < 0 or abs(result.as_tuple().exponent) > 30 or result > Decimal("1e18"):
            raise ValueError
        return result
    except (ValueError, ArithmeticError):
        raise AdmissionRefusedError("native_test_cost_term_unavailable") from None


@dataclass(frozen=True)
class CostBound:
    """A priced, enforced quantity from a trusted source adapter.

    Even a zero quantity needs its own limit evidence. Policy ceilings and idle
    headroom do not establish a quantity or a tariff. Every field is mandatory.
    """

    component: str
    unit: str
    quantity: str
    usd_per_unit: str
    pricing_evidence_sha256: str
    enforced_limit_sha256: str

    def __post_init__(self):
        if (
            self.component not in COMPONENTS
            or not isinstance(self.unit, str)
            or not 0 < len(self.unit) <= 128
            or not sha256(self.pricing_evidence_sha256)
            or not sha256(self.enforced_limit_sha256)
        ):
            raise AdmissionRefusedError("native_test_cost_term_unavailable")
        amount(self.quantity)
        amount(self.usd_per_unit)

    @property
    def total(self):
        with localcontext() as precision:
            precision.prec = 200
            return (amount(self.quantity) * amount(self.usd_per_unit)).quantize(QUANTUM, rounding=ROUND_UP)


def component_total(components):
    with localcontext() as precision:
        precision.prec = 200
        return sum((term.total for term in components), Decimal(0))


@dataclass(frozen=True)
class NativeEffectQuote:
    effect_key: str
    request_sha256: str
    authority_sha256: str
    source_contract_sha256: str
    pricing_revision: str
    issued_at: datetime
    expires_at: datetime
    session_seconds: int
    cleanup_seconds: int
    cleanup_attempts: int
    components: tuple[CostBound, ...]
    total_usd: Decimal

    def __post_init__(self):
        if (
            not isinstance(self.effect_key, str)
            or re.fullmatch(r"native-effect:[a-z][a-z0-9_.-]{1,127}", self.effect_key) is None
            or not all(sha256(value) for value in (self.request_sha256, self.authority_sha256, self.source_contract_sha256, self.pricing_revision))
            or self.issued_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or self.expires_at <= self.issued_at
            or type(self.session_seconds) is not int
            or not 1 <= self.session_seconds <= 300
            or type(self.cleanup_seconds) is not int
            or not self.session_seconds <= self.cleanup_seconds <= 600
            or type(self.cleanup_attempts) is not int
            or not 1 <= self.cleanup_attempts <= 32
            or type(self.components) is not tuple
            or not 1 <= len(self.components) <= 128
            or any(type(term) is not CostBound for term in self.components)
            or {term.component for term in self.components} != COMPONENTS
            or type(self.total_usd) is not Decimal
            or not self.total_usd.is_finite()
            or self.total_usd <= 0
            or component_total(self.components) != self.total_usd
        ):
            raise AdmissionRefusedError("native_test_all_in_quote_incomplete")

    def expired(self, now):
        return now >= self.expires_at or now < self.issued_at

    def require_binding(self, body, effect_key, authority, now):
        if (
            self.expired(now)
            or self.effect_key != effect_key
            or self.request_sha256 != hashlib.sha256(body).hexdigest()
            or self.authority_sha256 != digest(authority)
        ):
            raise AdmissionRefusedError("native_test_effect_quote_changed")

    @classmethod
    def from_evidence(cls, evidence):
        return cls(
            **{
                **evidence,
                "issued_at": datetime.fromisoformat(evidence["issued_at"]),
                "expires_at": datetime.fromisoformat(evidence["expires_at"]),
                "total_usd": Decimal(evidence["total_usd"]),
                "components": tuple(CostBound(**term) for term in evidence["components"]),
            }
        )

    @property
    def components_digest(self):
        return digest({"components": [asdict(term) for term in self.components]})


class NativeEffectQuoteAdapter(Protocol):
    """Trusted server composition; never implemented by request input or a CLI."""

    async def quote(self, body: bytes, effect_key: str, authority: dict, *, now: datetime) -> NativeEffectQuote: ...

    async def revalidate(self, quote: NativeEffectQuote, body: bytes, authority: dict, *, now: datetime) -> None: ...

    async def confirm(self, quote: NativeEffectQuote, *, now: datetime) -> None: ...


class UnavailableEffectQuoteAdapter:
    async def quote(self, *args, **kwargs):
        raise AdmissionRefusedError("native_test_all_in_quote_unavailable")

    async def revalidate(self, *args, **kwargs):
        raise AdmissionRefusedError("native_test_all_in_quote_unavailable")

    async def confirm(self, *args, **kwargs):
        raise AdmissionRefusedError("native_test_all_in_quote_unavailable")


def agentcore_compute_components(*, family, billable_seconds, cpu_usd_per_hour, memory_usd_per_gb_hour, pricing_sha256, bound_sha256):
    """Official fixed hardware quantities, with separately proven billed duration.

    This component formula alone is never a complete quote. A requested session
    timeout or delayed usage-log observation is not a billed-duration bound.
    """
    quantities = {"browser": (1, 4), "code_interpreter": (2, 8)}
    if family not in quantities or type(billable_seconds) is not int or not 0 < billable_seconds <= 31_536_000:
        raise AdmissionRefusedError("native_test_provider_bound_unavailable")
    cpu, memory = quantities[family]
    hours = Decimal(billable_seconds) / Decimal(3600)
    return (
        CostBound("provider_compute", "vcpu-hour", str(hours * cpu), cpu_usd_per_hour, pricing_sha256, bound_sha256),
        CostBound("provider_compute", "gb-hour", str(hours * memory), memory_usd_per_gb_hour, pricing_sha256, bound_sha256),
    )
