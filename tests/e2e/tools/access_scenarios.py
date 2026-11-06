"""Bounded, independently observed access lifecycle for protected fixtures."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import monotonic
from uuid import uuid4

from tests.e2e.tools.scenarios import FixtureUnavailable, canonical_uuid


@dataclass(frozen=True)
class AccessFixture:
    identifier: str = "SCN-8065-04"
    actions: tuple[str, ...] = ("grant", "consent", "invoke", "revoke", "deny", "disconnect", "reconnect")
    ttl_seconds: int = 300
    cleanup_seconds: int = 600
    max_cost_usd: Decimal = Decimal("20")
    max_sessions: int = 1


ACCESS_LIVE_FIXTURE = AccessFixture()
READINESS = {"grant": False, "consent": True, "invoke": True, "revoke": False,
             "deny": False, "disconnect": False, "reconnect": False}


def cost(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValueError("test cost estimate unavailable") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid test cost estimate")
    return amount


def run_access_fixture(fixture, driver, *, clock=monotonic):
    if driver is None or not all(getattr(driver, field, False) is True for field in
                                  ("authorized", "isolated", "tenant_selected", "pricing_approved")):
        raise FixtureUnavailable("selected, isolated and priced live fixture unavailable")
    if fixture != ACCESS_LIVE_FIXTURE:
        raise ValueError("unapproved access fixture bounds")
    try:
        prices = {action: cost(driver.estimate_cost(action)) for action in ("start_owned", *fixture.actions)}
    except (ValueError, TypeError, AttributeError):
        raise FixtureUnavailable("approved provider pricing is unavailable") from None
    if sum(prices.values()) > fixture.max_cost_usd:
        raise FixtureUnavailable("approved test-run cost exceeds $20")
    start = clock()
    request_id = str(uuid4())
    evidence = {"execution_id": None, "run_id": None, "request_ids": [request_id],
                "audit_operation_ids": [], "diagnostics": [], "provider_request_count": 0,
                "estimated_cost_usd": "0", "cleanup": {"status": "not-run", "provider_termination": None}}
    spent = Decimal("0")
    try:
        owned = driver.start_owned(fixture.identifier, max_sessions=fixture.max_sessions,
                                   ttl_seconds=fixture.ttl_seconds, max_cost_usd=fixture.max_cost_usd)
        evidence["execution_id"] = canonical_uuid(owned["execution_id"])
        evidence["run_id"] = canonical_uuid(owned["run_id"])
        if owned.get("session_count") != 1 or owned.get("ttl_seconds") != fixture.ttl_seconds:
            raise ValueError("stateful session or TTL bound unverified")
        for action in fixture.actions:
            if clock() - start >= fixture.ttl_seconds:
                raise TimeoutError("stateful session TTL exceeded")
            step = driver.step(action, execution_id=evidence["execution_id"], request_id=request_id,
                               max_cost_usd=min(prices[action], fixture.max_cost_usd - spent))
            if step.get("ready") is not READINESS[action]:
                raise ValueError("access diagnostic disagrees with expected transition")
            calls = step.get("provider_request_count")
            if type(calls) is not int or calls != (1 if action == "invoke" else 0):
                raise ValueError("unexpected provider call count")
            observed_cost = cost(step.get("estimated_cost_usd"))
            if observed_cost > prices[action]:
                raise ValueError("observed cost exceeds approved per-action ceiling")
            spent += observed_cost
            if spent > fixture.max_cost_usd:
                raise ValueError("test cost bound exceeded")
            if action != "deny":
                evidence["audit_operation_ids"].append(canonical_uuid(step["audit_operation_id"]))
            evidence["diagnostics"].append({"action": action, "ready": step["ready"]})
            evidence["provider_request_count"] += calls
        evidence["estimated_cost_usd"] = str(spent)
        evidence["status"] = "pending-verification"
    except (FixtureUnavailable, ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
        evidence["status"], evidence["reason"] = "blocked", type(exc).__name__
    finally:
        try:
            remaining = max(1, fixture.cleanup_seconds - int(clock() - start))
            stopped = driver.stop_owned(evidence["execution_id"], timeout=remaining)
            evidence["cleanup"] = {"status": stopped.get("status", "unknown"),
                                   "provider_termination": stopped.get("provider_termination")}
            if clock() - start > fixture.cleanup_seconds:
                evidence["cleanup"]["status"] = "late"
        except (FixtureUnavailable, ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
            evidence["cleanup"] = {"status": "unknown", "provider_termination": None,
                                   "reason": type(exc).__name__}
    return evidence


def access_verdict(fixture, evidence, *, observer=None, live=False):
    if evidence["cleanup"]["status"] == "failed":
        return "fail", "owned cleanup failed"
    if evidence.get("status") != "pending-verification" or evidence["cleanup"]["status"] != "confirmed":
        return "blocked", "fixture execution or cleanup is unverified"
    if not live or observer is None:
        return "blocked", "protected independent observations unavailable"
    try:
        result = observer.observe(execution_id=evidence["execution_id"], run_id=evidence["run_id"],
                                  request_ids=tuple(evidence["request_ids"]),
                                  audit_operation_ids=tuple(evidence["audit_operation_ids"]),
                                  timeout=fixture.cleanup_seconds)
        observed_cost = cost(result["total_cost_usd"])
        observed_requests = set(result["request_ids"])
        observed_receipts = set(result["audit_operation_ids"])
    except (FixtureUnavailable, ValueError, TimeoutError, OSError, KeyError, TypeError, AttributeError, RuntimeError):
        return "blocked", "independent provider, audit or cost readback unavailable"
    if (result.get("provider_source") != "provider-control-plane" or result.get("audit_source") != "gateway-audit"
            or result.get("execution_id") != evidence["execution_id"] or result.get("run_id") != evidence["run_id"]
            or observed_requests != set(evidence["request_ids"])
            or observed_receipts != set(evidence["audit_operation_ids"])):
        return "blocked", "independent evidence does not match this owned run"
    if (result.get("session_count") != fixture.max_sessions or result.get("provider_request_count") != 1
            or result.get("audit_attributed") is not True or result.get("diagnostics_match") is not True
            or result.get("tenant_verified") is not True or result.get("owner_verified") is not True
            or observed_cost > fixture.max_cost_usd):
        return "fail", "independent access, audit or cost assertion failed"
    elapsed = result.get("cleanup_elapsed_seconds")
    if (result.get("termination") not in {"stopped", "expired"} or not result.get("termination_observed_at")
            or type(elapsed) not in (int, float) or not 0 <= elapsed <= fixture.cleanup_seconds
            or not evidence["cleanup"].get("provider_termination")):
        return "blocked", "independent cleanup deadline or termination is unverified"
    return "pass", "independent access, provider, audit and cleanup readback matched"
