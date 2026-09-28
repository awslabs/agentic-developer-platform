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

    # ------------------------------------------------------------------
    # Per-run / per-chain spend caps (Issue #4187)
    # ------------------------------------------------------------------

    # Master switch. Ships as False on purpose: the issue asks for a warn-only
    # observation period so the platform defaults below can be calibrated against
    # real run costs rather than guessed, and a cap that surprises operators on
    # day one is a self-inflicted incident. Flip via SSM + rollout restart, same
    # operational shape as budget_fail_mode.
    budget_run_cap_enabled: bool = False

    # Platform default ceilings, in USD, for ONE run and for one chain (a run
    # plus every run it spawned).
    #
    # These are also a HARD UPPER BOUND, not just a default: a per-tenant
    # override may lower them but never raise them (see
    # BudgetEnforcementService._resolve_scope_cap). Otherwise a tenant admin
    # could raise their own cap, which defeats the control.
    #
    # The chain default is deliberately several times the run default rather than
    # equal to it: a chain is expected to contain multiple runs, so an equal value
    # would make the chain cap fire on the second run of every normal fan-out and
    # the run cap would never be the thing that fires.
    budget_run_cap_usd: Decimal = Decimal("25.00")
    budget_chain_cap_usd: Decimal = Decimal("100.00")

    # How long a run's spend accumulator lives. This is the run/chain reservation
    # TTL and it is NOT the #4287 backstop TTL — see ReservationTarget.ttl_seconds.
    # It must comfortably exceed the longest expected run, because when it expires
    # the run's accumulated spend resets to zero and the cap stops applying.
    # 24h matches the activity read path's ACTIVE_STALENESS_HOURS.
    budget_run_cap_ttl_seconds: int = 86_400

    # Run-identity binding mode (Issue #4187 / the #3175 pattern):
    #   "shadow"  - resolve the binding, log + count drift, deny nothing, and
    #               enforce no run/chain cap. The safe default: the drift metric
    #               tells you what a deny rule WOULD have rejected first.
    #   "enforce" - an unbindable run id (unknown, cross-tenant, or already
    #               finished) is a 402 denial, and the caps apply.
    # A DDB lookup FAULT degrades in both modes — a forgery is denied, an outage
    # is not (see run_binding.resolve_run_binding).
    #
    # Issue #4337: flipping this is gated on more than "drift is zero" — the run
    # cap enforces purely through Redis reservations that fail OPEN, so it also
    # needs BudgetReservationOutcome reading `reserved` rather than `degraded`
    # (i.e. #4342 live in this environment), every dispatch path in the D10a table
    # exercised, and both forgery positive controls still drifting. Otherwise a
    # green window is over an inert cap. Per environment; the flip stays manual.
    budget_run_binding_mode: str = "shadow"

    # Policy for a request on an enforced path that carries NO run id.
    # Explicitly a declared policy, never "absent -> unlimited":
    #   "exempt_human"   - IAM-authenticated callers MUST carry a run id (they are
    #                      agents, and the GitHub-dispatched worker always sends
    #                      one); human/JWT callers are exempt because the per-user
    #                      hierarchy caps already bound them.
    #   "require"        - every caller on an enforced path must carry one.
    #   "exempt_missing" - nobody is denied merely for carrying no run id; a
    #                      request that DOES carry one is still bound and capped
    #                      exactly as before.
    #
    # Issue #4337 D10a adds "exempt_missing" as the recorded lever for the three
    # audited no-row dispatch paths (orchestration engine and GitLab write no row
    # at all; chat has a row but its worker asserts no id — see the D10a table in
    # run_binding.py). All three authenticate as the IAM worker, so `exempt_human`
    # does NOT cover them: under "exempt_human" + enforce they 402 on their first
    # model call. Either set this to "exempt_missing" for an environment carrying
    # that traffic, or give those paths rows and ids — but it must be a decision,
    # not a discovery. Both dispositions are now visible in shadow via the
    # `missing_run_id` drift metric's `outcome` dimension.
    #
    # Default unchanged: the GitHub path (the dominant one) does send an id, and
    # weakening the default would quietly widen the exemption for everyone.
    budget_run_id_required_mode: str = "exempt_human"

    # ------------------------------------------------------------------
    # Platform ceiling for CALENDAR caps (Issue #4397)
    # ------------------------------------------------------------------

    # The platform-wide upper bound on a daily/weekly/monthly `budget_configs`
    # cap, mirroring what budget_run_cap_usd / budget_chain_cap_usd are for
    # run/chain scopes: a tenant override may LOWER it, never raise it. The read
    # path resolves the effective cap as min(configured, this) — see
    # `_resolve_effective_period_cap` in me_routes.py.
    #
    # Ships as None (= no platform ceiling) DELIBERATELY, and that default is the
    # safe one for a specific reason. Calendar enforcement
    # (`_check_entity_budget`) compares spend against the RAW `budget_configs`
    # row with no clamp of its own. So while this is None, the effective cap the
    # read path reports is byte-identical to the cap enforcement honours, and
    # screen and enforcer agree — which is the property EPIC #4324 exists to
    # establish.
    #
    # Setting it introduces a real read/enforce divergence in the OTHER
    # direction: the read path would report the clamped (lower) figure while
    # calendar enforcement kept honouring the higher configured row, so a user
    # would be shown less headroom than they actually have. That is the honest
    # direction to err (under-promising, never over-promising), but it is still a
    # divergence and it is why this ships off rather than at a guessed number.
    # Closing it means teaching `_check_entity_budget` the same clamp, which is an
    # ENFORCEMENT change and out of scope for this read-only unit (NFR-2) —
    # tracked as a follow-up.
    #
    # Same operational shape as budget_fail_mode: read once at container start,
    # so changing it needs an SSM put plus a rollout restart.
    budget_period_cap_usd: Decimal | None = None

    # Grace period settings (in seconds)
    soft_enforcement_grace_period: int = 300  # 5 minutes
    budget_exceeded_notification_cooldown: int = 3600  # 1 hour

    # Alert thresholds
    budget_warning_threshold_percent: float = 80.0
    budget_critical_threshold_percent: float = 95.0

    # Model pricing: REMOVED in issue #4969.
    #
    # This was the third independently hand-maintained rate table in the module
    # (with src/budget/pricing.py and lambda/shared/pricing_fallback.py), and the
    # most stale of the three: it was keyed by short names like
    # "claude-3-5-sonnet-20241022" rather than Bedrock ids, so every modern
    # caller missed it and silently took its "default" row — a $3/$15 estimate
    # for traffic that might be Opus at $5/$25.
    #
    # It also sat on a pydantic BaseSettings with env_prefix BG_BUDGET_, which
    # made the whole nested rate table settable from one environment variable.
    # A rate is published by AWS, not configured per deployment.
    #
    # Its only reader was src/budget/utils.py::calculate_model_cost, which now
    # calls the shared pricing_policy resolver directly.

    # Database settings
    budget_usage_batch_size: int = 100
    budget_cleanup_days: int = 90  # Keep budget usage data for 90 days

    class Config:
        env_prefix = "BG_BUDGET_"
        case_sensitive = False


# Global config instance
budget_config = BudgetConfig()
