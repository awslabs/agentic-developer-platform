from decimal import Decimal

from pydantic_settings import BaseSettings


class BudgetConfig(BaseSettings):
    """Configuration for budget service including model pricing."""

    # Enforcement settings
    default_enforcement_mode: str = "hard"
    budget_check_enabled: bool = True
    cost_calculation_enabled: bool = True

    # Fail mode (Issue #4075): "closed" (default) blocks requests when the
    # budget check itself fails, "open" allows them.
    #
    # The default used to be "open", which meant any DB/IAM error on the ledger
    # read admitted the request — so a transient fault was an open window of
    # uncapped model spend that no cap would stop. Fail-closed removes that
    # window; budget_fail_open_grace_seconds below keeps a brief blip from
    # hard-downing all inference instead.
    #
    # Set via BG_BUDGET_BUDGET_FAIL_MODE (note the doubled BUDGET — env_prefix
    # is "BG_BUDGET_"), stamped into the ConfigMap from SSM at deploy time.
    # Reverting to "open" is the rollback lever; it requires an SSM put plus a
    # redeploy/rollout-restart, since this is read once at container start.
    budget_fail_mode: str = "closed"

    # How long CONSECUTIVE budget-check failures may be tolerated before
    # requests are denied (Issue #4075). Sized to absorb a realistic RDS
    # IAM-token-expiry blip; state is shared via Redis so this is one window
    # cluster-wide rather than one per process. Set to 0 to deny on the first
    # failure.
    budget_fail_open_grace_seconds: int = 30

    # Where the grace window keeps its state: "redis" (shared, correct across
    # the 8 replica×worker processes) or "process" (per-process, approximate —
    # single-process local dev only).
    budget_grace_window_backend: str = "redis"

    # Live-denominator reservations (Issue #4287). The settled ledger only
    # materializes minutes after a request, so concurrent requests otherwise all
    # read the same stale spend and collectively exceed a cap each one passed.
    # Setting this False is the rollback lever: enforcement reverts to the Wave 1
    # settled-ledger check with no other behavior change. Same operational shape
    # as budget_fail_mode — read once at container start, so flipping it needs an
    # SSM put plus a rollout restart.
    budget_reservation_enabled: bool = True

    # How long an un-reconciled reservation keeps consuming cap headroom.
    #
    # This is the SIGKILL backstop, not the primary release: a pod killed between
    # reserving and logging usage never runs its release, so only this expiry
    # returns the headroom. Sized at roughly 2x p99 request latency — long enough
    # that a slow-but-live streaming request is not double-counted against
    # itself, short enough that a lost reservation is not a lasting
    # denial-of-service against the tenant's own cap.
    budget_reservation_ttl_seconds: int = 120

    # Where reservations live: "redis" (shared, the only correct setting for a
    # multi-process deployment) or anything else to disable them. A per-process
    # counter would be 8 independent denominators, which is not a live figure at
    # all, so there is deliberately no in-process fallback here — see
    # reservations.py on why unavailability degrades rather than approximates.
    budget_reservation_backend: str = "redis"

    # Grace period settings (in seconds)
    soft_enforcement_grace_period: int = 300  # 5 minutes
    budget_exceeded_notification_cooldown: int = 3600  # 1 hour

    # Alert thresholds
    budget_warning_threshold_percent: float = 80.0
    budget_critical_threshold_percent: float = 95.0

    # Model pricing (cost per 1000 tokens in USD)
    model_pricing: dict[str, dict[str, Decimal]] = {
        # Claude models
        "claude-3-5-sonnet-20241022": {
            "input": Decimal("0.003"),
            "output": Decimal("0.015"),
        },
        "claude-3-5-sonnet-20240620": {
            "input": Decimal("0.003"),
            "output": Decimal("0.015"),
        },
        "claude-3-5-haiku-20241022": {
            "input": Decimal("0.0008"),
            "output": Decimal("0.004"),
        },
        "claude-3-opus-20240229": {
            "input": Decimal("0.015"),
            "output": Decimal("0.075"),
        },
        "claude-3-sonnet-20240229": {
            "input": Decimal("0.003"),
            "output": Decimal("0.015"),
        },
        "claude-3-haiku-20240307": {
            "input": Decimal("0.00025"),
            "output": Decimal("0.00125"),
        },
        # Legacy Claude models
        "claude-2.1": {
            "input": Decimal("0.008"),
            "output": Decimal("0.024"),
        },
        "claude-2.0": {
            "input": Decimal("0.008"),
            "output": Decimal("0.024"),
        },
        "claude-instant-1.2": {
            "input": Decimal("0.0008"),
            "output": Decimal("0.0024"),
        },
        # Default pricing for unknown models
        "default": {
            "input": Decimal("0.003"),
            "output": Decimal("0.015"),
        },
    }

    # Database settings
    budget_usage_batch_size: int = 100
    budget_cleanup_days: int = 90  # Keep budget usage data for 90 days

    class Config:
        env_prefix = "BG_BUDGET_"
        case_sensitive = False


# Global config instance
budget_config = BudgetConfig()
