"""The observation payload: what a controller or monitor submits, and about what.

Issue #5043 (U8), EPIC #4910. R11, contracts half.

## What this replaces, and the one thing it does not reuse

The story's evidence is that the only fleet contract in the system today runs the
opposite way and takes no identity: the controller POSTs to `/internal/heartbeat`
with `Content-Type` as its only header, and the receiver has no auth dependency.
So the *payload* is a reasonable starting point — it is the set of facts a
controller genuinely knows about a cluster — while the **transport is not reused**,
because a transport whose only header is a content type has nowhere to put a
caller identity.

That split is the reason this module holds no URL, no client and no HTTP verb. It
holds the facts and their well-formedness rules; `auth.py` and `scoping.py` hold
who may submit them, and the receiver is U15's, upstream.

## Why the cluster reference is not just a UUID

`ClusterRef` pairs a cluster identifier with the workspace that owns it, and both
are required. A payload identifying its subject by UUID alone is precisely what
makes the current endpoint forgeable: the UUID is the entire claim, so knowing one
is indistinguishable from being authorized for it. The workspace is a caller assertion. U15 must independently resolve the cluster's
stored workspace and pass it to the scope check. An absent or mismatched ownership
record is a refusal, never a fallback to the payload.

The UUID is still what identifies the cluster. The workspace is not a second
identifier; it is the assertion whose agreement with the submitter's grant is
checked, and a disagreement is a refusal rather than a correction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite

from .health import CheckResult, CheckStatus, ContractViolation, aggregate_status
from .version import CONTRACT_VERSION, VERSION_FIELD, check_version

# Observation kinds this contract carries. Fleet health and budget *observation*
# are both reports of what was seen; neither is an enforcement decision. Budget
# enforcement is explicitly out of scope for this unit (M6, upstream/B) — a
# submitted budget observation states spend observed, and grants no authority to
# act on it.
_FLEET_HEALTH = "fleet_health"
_BUDGET_USAGE = "budget_usage"

OBSERVATION_KINDS: frozenset[str] = frozenset({_FLEET_HEALTH, _BUDGET_USAGE})


def _wire_string(value: object, *, nullable: bool = False) -> str | None:
    if isinstance(value, str) or (nullable and value is None):
        return value
    raise ContractViolation("expected a string field")


def _wire_time(value: object) -> datetime:
    timestamp = datetime.fromisoformat(_wire_string(value))
    if timestamp.utcoffset() is None:
        raise ContractViolation("wire timestamps must be timezone-aware")
    return timestamp


@dataclass(frozen=True)
class ClusterRef:
    """The subject of an observation: which cluster, in which workspace.

    Both fields required — see the module docstring on why a bare UUID is not a
    sufficient subject for an authenticated write.
    """

    cluster_id: str
    workspace: str

    def __post_init__(self) -> None:
        if not self.cluster_id or not self.cluster_id.strip():
            raise ContractViolation("cluster_id must be a non-empty string")
        if not self.workspace or not self.workspace.strip():
            # A blank workspace would make the scoping comparison in scoping.py
            # trivially satisfiable by an empty grant, so it is refused at
            # construction rather than left for the authorization layer.
            raise ContractViolation("workspace must be a non-empty string")


@dataclass(frozen=True)
class BudgetUsage:
    """Observed spend for a workspace over a window.

    Deliberately has **no** `budget_exceeded`, `enforce` or `limit` field. This
    contract reports what was observed; it confers no local budget authority, and
    a receiver of this payload is not thereby entitled to block provisioning.
    Admission-time enforcement is B's, and the story is explicit that this unit
    adds no local budget authority even behind a flag. A boolean verdict field
    here would be the first step to one.
    """

    workspace: str
    window_start: datetime
    window_end: datetime
    observed_spend_usd: float
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not self.workspace or not self.workspace.strip():
            raise ContractViolation("workspace must be a non-empty string")
        if self.window_start.tzinfo is None or self.window_end.tzinfo is None:
            raise ContractViolation("budget window bounds must be timezone-aware")
        if self.window_end <= self.window_start:
            raise ContractViolation("budget window end must be after its start")
        if not isfinite(self.observed_spend_usd) or self.observed_spend_usd < 0:
            raise ContractViolation(
                "observed spend must be finite and cannot be negative"
            )


@dataclass(frozen=True)
class Observation:
    """One versioned observation submission about one subject.

    The version is carried in the payload as well as the header (see
    `version.py`) so it survives being logged, queued or replayed. It defaults to
    the version this package defines, which is safe here because constructing
    this object *is* writing against that version — the case version discipline
    guards against is a submission arriving with a version this code does not
    implement, and that arrives as wire data, not as a constructor call.
    """

    kind: str
    subject: ClusterRef
    reported_at: datetime
    # Who is reporting. A stable submitter name (e.g. a monitor or controller
    # identity), recorded so a fleet surface can say which reporter last spoke.
    # This is NOT the authentication claim — `auth.py` owns that. A submitter
    # naming itself here proves nothing, which is exactly why the two are
    # separate objects.
    reporter: str
    checks: tuple[CheckResult, ...] = ()
    budget: BudgetUsage | None = None
    contract_version: str = CONTRACT_VERSION
    labels: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.kind not in OBSERVATION_KINDS:
            # Fail-closed on kind: an unknown kind is refused rather than stored
            # as an opaque blob, because a receiver cannot scope or interpret
            # what it cannot name.
            raise ContractViolation(f"unknown observation kind: {self.kind!r}")
        if not self.reporter or not self.reporter.strip():
            raise ContractViolation("reporter must be a non-empty string")
        if self.reported_at.tzinfo is None:
            raise ContractViolation("reported_at must be timezone-aware")

        if self.kind == _FLEET_HEALTH:
            if not self.checks:
                # A fleet-health observation with no checks would aggregate to
                # NOT_CHECKED and occupy the "last known state" slot for a
                # cluster while establishing nothing. Refused so a reporter
                # cannot overwrite a real reading with an empty one.
                raise ContractViolation(
                    "a fleet_health observation must carry at least one check"
                )
            if self.budget is not None:
                raise ContractViolation(
                    "a fleet_health observation cannot carry budget usage"
                )

        if self.kind == _BUDGET_USAGE:
            if self.budget is None:
                raise ContractViolation(
                    "a budget_usage observation must carry budget usage"
                )
            if self.checks:
                raise ContractViolation(
                    "a budget_usage observation cannot carry health checks"
                )
            if self.budget.workspace != self.subject.workspace:
                # Two workspace assertions in one payload that disagree. Refused
                # rather than reconciled: whichever one a receiver preferred
                # would become the tamperable field.
                raise ContractViolation(
                    "budget usage workspace does not match the observation subject's workspace"
                )

    @property
    def status(self) -> CheckStatus:
        """The single most severe status across this observation's checks.

        Empty checks reduce to `NOT_CHECKED`, never `HEALTHY` — see
        `health.aggregate_status`.
        """
        return aggregate_status(self.checks)

    @classmethod
    def from_wire(cls, payload: dict) -> Observation:
        """Validate received fields through the same guards as sender objects.

        Authentication must precede this parser. A signature authenticates bytes,
        not probe honesty. Never persist raw JSON in place of this validated object.
        """
        try:
            if (
                not isinstance(payload, dict)
                or not check_version(
                    CONTRACT_VERSION, payload.get(VERSION_FIELD)
                ).accepted
            ):
                raise ContractViolation("invalid observation version")
            subject = payload["subject"]
            raw_checks = payload.get("checks", [])
            labels = payload.get("labels", {})
            if not isinstance(raw_checks, list) or not isinstance(labels, dict):
                raise ContractViolation("invalid checks or labels shape")
            checks = tuple(
                CheckResult(
                    name=_wire_string(c["name"]),
                    status=CheckStatus(c["status"]),
                    observed_at=(
                        _wire_time(c["observed_at"])
                        if c.get("observed_at") is not None
                        else None
                    ),
                    detail=_wire_string(c.get("detail"), nullable=True),
                    error=_wire_string(c.get("error"), nullable=True),
                    reason=_wire_string(c.get("reason"), nullable=True),
                )
                for c in raw_checks
            )
            budget = None
            if "budget" in payload:
                raw = payload["budget"]
                amount = raw["observed_spend_usd"]
                if type(amount) not in (int, float) or raw["currency"] != "USD":
                    raise ContractViolation("invalid budget amount or currency")
                budget = BudgetUsage(
                    workspace=_wire_string(raw["workspace"]),
                    window_start=_wire_time(raw["window_start"]),
                    window_end=_wire_time(raw["window_end"]),
                    observed_spend_usd=amount,
                    currency=raw["currency"],
                )
            result = cls(
                kind=_wire_string(payload["kind"]),
                subject=ClusterRef(
                    _wire_string(subject["cluster_id"]),
                    _wire_string(subject["workspace"]),
                ),
                reported_at=_wire_time(payload["reported_at"]),
                reporter=_wire_string(payload["reporter"]),
                checks=checks,
                budget=budget,
                contract_version=payload[VERSION_FIELD],
                labels=tuple(
                    (_wire_string(k), _wire_string(v)) for k, v in labels.items()
                ),
            )
            if payload["status"] != result.status.value:
                raise ContractViolation("reported aggregate disagrees with checks")
            return result
        except ContractViolation:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise ContractViolation("invalid observation body") from exc

    def to_wire(self) -> dict:
        """Serialize to the wire shape a receiver parses.

        Written by hand rather than by `dataclasses.asdict` so the wire contract
        is visible in one place and cannot change as a side effect of renaming a
        field. `asdict` would make every attribute name part of the public
        contract implicitly, which is how a refactor becomes a breaking change
        without a version bump.
        """
        payload: dict = {
            VERSION_FIELD: self.contract_version,
            "kind": self.kind,
            "subject": {
                "cluster_id": self.subject.cluster_id,
                "workspace": self.subject.workspace,
            },
            "reported_at": self.reported_at.isoformat(),
            "reporter": self.reporter,
            "status": self.status.value,
        }
        if self.checks:
            payload["checks"] = [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "observed_at": c.observed_at.isoformat() if c.observed_at else None,
                    "detail": c.detail,
                    "error": c.error,
                    "reason": c.reason,
                }
                for c in self.checks
            ]
        if self.budget is not None:
            payload["budget"] = {
                "workspace": self.budget.workspace,
                "window_start": self.budget.window_start.isoformat(),
                "window_end": self.budget.window_end.isoformat(),
                "observed_spend_usd": self.budget.observed_spend_usd,
                "currency": self.budget.currency,
            }
        if self.labels:
            payload["labels"] = dict(self.labels)
        return payload
