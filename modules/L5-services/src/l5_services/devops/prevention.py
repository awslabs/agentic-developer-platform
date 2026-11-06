"""Desired prevention policy and independently qualified native readback."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re


@dataclass(frozen=True)
class PreventionPolicy:
    enabled: bool = False
    goal_id: str | None = None
    expression: str | None = None
    timezone: str = "UTC"
    missed_run_policy: str = "skip"

    def __post_init__(self):
        if type(self.enabled) is not bool or self.timezone != "UTC" or self.missed_run_policy != "skip":
            raise ValueError("unsupported native prevention cadence or missed-run policy")
        if (self.enabled or self.goal_id is not None or self.expression is not None) and (
            not isinstance(self.goal_id, str) or not self.goal_id
            or not isinstance(self.expression, str)
            or re.fullmatch(r"rate\(([1-9]|[12][0-9]|30) days\)", self.expression) is None
        ):
            raise ValueError("enablement requires a selected goal and provider cadence")

    def as_dict(self):
        return {"enabled": self.enabled, "goal_id": self.goal_id,
                "expression": self.expression, "timezone": self.timezone,
                "missed_run_policy": self.missed_run_policy}

    @classmethod
    def from_configuration(cls, settings: dict):
        devops = settings.get("devops") or {}
        desired = devops.get("prevention_enabled", False)
        if type(desired) is not bool:
            raise ValueError("prevention enablement must be explicit")
        selected = devops.get("prevention_policy") or {}
        if not isinstance(selected, dict) or set(selected) - {
            "goal_id", "expression", "timezone", "missed_run_policy"
        }:
            raise ValueError("unsupported prevention policy")
        return cls(enabled=desired, **selected)


def require_native_budget(settings: dict, budget: dict | None, scope: dict) -> None:
    """Provider goal evaluations cannot be intercepted by L5 run admission."""
    limits = settings.get("limits") or {}
    try:
        cost = Decimal(limits["max_cost"])
    except (KeyError, InvalidOperation, TypeError):
        raise ValueError("native prevention needs a valid project budget") from None
    if (not isinstance(budget, dict) or budget.get("enforcement_mode") != "soft"
            or budget.get("tenant_id") != scope["tenant_id"]
            or budget.get("project_id") != scope["project_id"]
            or limits.get("policy_ref") != budget.get("policy_ref")
            or not isinstance(limits.get("policy_ref"), dict)
            or limits["policy_ref"].get("record_type") != "policy"
            or limits["policy_ref"].get("tenant_id") != scope["tenant_id"]
            or limits["policy_ref"].get("project_id") != scope["project_id"]
            or cost <= 0 or not cost.is_finite() or limits.get("currency") != "USD"
            or type(limits.get("deadline_seconds")) is not int
            or not 60 <= limits["deadline_seconds"] <= 86400
            or settings.get("exclusions") not in (None, [])
            or (settings.get("devops") or {}).get("paused") is True):
        raise ValueError("native prevention cannot honor strict admission or project exclusions")


def prevention_readiness(policy: PreventionPolicy, observed: dict | None, *,
                         configuration_revision: int, scope_fingerprint: str) -> dict:
    """Never infer a disabled native schedule from inbound trigger state."""
    if (not observed or observed.get("scope_fingerprint") != scope_fingerprint
            or observed.get("configuration_revision") != configuration_revision):
        return {"desired": {"enabled": policy.enabled}, "observed": None,
                "status": "blocked", "reason": "prevention_readback_missing"}
    native = observed.get("native_schedule")
    if not isinstance(native, dict) or type(native.get("enabled")) is not bool:
        return {"desired": {"enabled": policy.enabled}, "observed": native,
                "status": "blocked", "reason": "native_schedule_unqualified"}
    if native["enabled"] != policy.enabled:
        return {"desired": {"enabled": policy.enabled}, "observed": native,
                "status": "blocked", "reason": "prevention_state_mismatch"}
    if policy.enabled and not any(goal.get("goal_id") == policy.goal_id
                                  and goal.get("expression") == policy.expression
                                  and goal.get("state") == "ENABLED"
                                  for goal in native.get("goals", [])):
        return {"desired": {"enabled": policy.enabled}, "observed": native,
                "status": "blocked", "reason": "prevention_cadence_mismatch"}
    return {"desired": {"enabled": policy.enabled}, "observed": native,
            "status": "ready", "reason": None}
