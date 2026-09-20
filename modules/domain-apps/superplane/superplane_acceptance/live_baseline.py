"""Capture U12's R17 live baseline and serving evidence from a real environment.

Issue #5289, follow-up to #5040 (U12) under EPIC #4910, evaluated by #5067.

## What this module is for

U12's merged `spike/` harness re-implements the baseline's decision rules against
fixtures derived from upstream source. That is useful and it is deliberately not
live evidence: it has never reached a provider, a cluster or an API server, so it
cannot say whether a machine is really rented, really joins EKS, really runs a
workload, or whether teardown really released capacity rather than dropping a
local handle. #5067 defers two criteria on exactly that gap:

* **U12-L1 / R17 baseline** -- provider selection and provisioning, EKS node
  registration and readiness, Kubernetes scheduling, status and logs,
  cancellation and controller lifecycle, cost observation, and *provider-verified*
  cleanup.
* **U12-L2 / R17 serving** -- endpoint reachability, authenticated and
  unauthorized behavior, status, and owning-controller stop/cleanup, for serving
  scenarios **actually present** in the selected baseline.

This module is the observer contract and the evidence rules for those two
criteria. It reuses U12's existing scenario inventory, provenance and parity
dimensions rather than restating them, so the offline harness and this capture
describe the same eight dimensions.

## What an observation has to say, not merely that one exists

The first review of this module drove it with deliberately negative and foreign
observations and it reported both criteria satisfied. The cause was that a check
passed whenever *any* observation existed for it, so "the node never became
Ready", "the unauthenticated request was allowed through" and "scheduling
failed" were recorded as successes. Two rules close that:

* :class:`Outcome` is a required field of every :class:`ObservedFact`. An
  observer must state whether the expected behavior was **satisfied**, was
  **refuted**, or could not be established (**indeterminate**). Only a fact
  set that is entirely satisfied passes.
* A refutation is retained as refuted *live* evidence rather than collapsed into
  ``NOT_RUN``. "We watched the node fail to join" and "nobody looked" are
  different findings, and a later migration comparison needs to tell them apart.
  Where the baseline genuinely does not do something, that outcome survives into
  the record instead of being massaged into a success.

## Evidence has to be about the selected machine

Identity is checked against the reviewed target's recorded configuration and
correlated across the lifecycle by :class:`_Correlation`: the instance that was
provisioned must be the one that joined the cluster, ran the workload and was
later confirmed gone, and one handle may not be bound to two different nodes,
clusters or controllers. Wrong resource identity is the specific way a baseline
capture becomes unsound -- U19 would compare its migrated behavior against a
measurement of something else -- so mismatches are refused before anything
aggregates rather than noted in the record afterwards.

## Scenario coverage, and serving as its own branch

Every parity check already declares the baseline scenarios it compares against.
That mapping is enforced here: a check may only be satisfied if one of its
scenarios was selected for observation, evidence for an unselected scenario is
refused, and a partial selection is reported as incomplete coverage that cannot
satisfy a criterion. U12-L2 branches on *observed* inventory -- the full serving
lifecycle when services exist, an independently evidenced absence when they do
not -- and serving facts arriving alongside an empty inventory are rejected as
contradictory rather than accepted.

## Why ``live_verified`` and not ``supports_parity_claim``

``ParityResult.supports_parity_claim`` additionally requires a *captured
baseline* to compare against, because a live ADP-side run with no baseline
observation measures one system instead of comparing two. That is U19's gate, not
this one. U12 is the run that **produces** the baseline side, so its gate is
``live_verified``. Using ``supports_parity_claim`` here would be unsatisfiable by
construction: every check this capture most needs to fill
(``node.join-produces-ready-node``, ``cleanup.provider-side-absence-verified``,
the four serving checks) is marked ``baseline_evidence=NOT_RUN`` precisely
because nobody has captured it yet.

## What running this module cannot do

It observes; it never operates. Every outside contact goes through
:class:`BaselineObserver`, which has no launch, stop, down, purge, delete, drain
or apply method -- so there is no path by which a capture run creates, mutates or
bills a resource. Any provisioning or cancellation needed to produce something to
observe is the operator's separately authorized action, taken before this runs,
under its own retained spend limit, deadline and cleanup owner. Authoring and CI
perform none of it.

It also does not execute U19's handover. It records the existing-state inventory
U19 needs for its adopt / drain-relaunch / no-existing-state decision, requiring
every recorded class to be enumerated explicitly and leaving every decision
``undecided``.

## Why ``BASELINE_TARGETS`` is empty while the observer ships

These are two different things, and the first review was right that conflating
them left operations unable to run the check at all.

The **observer** is client code. It ships: see
:mod:`superplane_acceptance.live_observer`, whose endpoints, cluster and read
commands are validated configuration rather than a guess about any particular
environment, and which is the only type whose output may be published.

The **target registry** is authorization. It stays empty, because no baseline
Superplane environment has been reviewed and mapped and the selection remains the
EPIC A supervisor's decision. Registering one means supplying that environment's
provider/cluster configuration, its SkyPilot API and runtime, its onboarding
entry points and its state stores -- an authorized change, not a default -- and
until then :func:`settings` fails ``BLOCKED`` rather than observing an invented
target.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from spike.baseline_inventory import (
    EXISTING_STATE_CLASSES,
    HANDOVER_DECISIONS,
    SCENARIOS,
)
from spike.parity_matrix import (
    Dimension,
    EvidenceKind,
    ParityResult,
    check_by_id,
    dimension_by_name,
)

from .cli_delivery import EvidenceError, require

# Reviewed baseline environments. Deliberately empty -- see the module docstring
# on why this is authorization and the observer is not. An entry needs the
# selected environment's provider/cluster configuration, its SkyPilot API and
# runtime, its onboarding entry points and its state stores.
BASELINE_TARGETS: dict[str, dict[str, str]] = {}

# Target-metadata keys a registered entry must supply. Observations are checked
# against these, so a target cannot be registered without the identity facts that
# make "is this evidence about the selected machine?" answerable.
#
# ``workspace_cluster_arn`` and ``workspace_api_endpoint`` exist because a cluster
# *name* is not an identity. The second review reproduced a foreign context named
# ``.../workspace-eks-attacker`` passing a containment test against
# ``workspace-eks``, after which its node facts were labelled as the selected
# cluster's. An EKS ARN carries account, region and name and is compared whole, so
# a prefix, a suffix and a lookalike are all simply different strings.
#
# ``skypilot_runtime_version`` is the expectation the *observed* runtime is checked
# against. Without a recorded expectation there is nothing for the observed value
# to disagree with, and "correlate the runtime" collapses into "write down
# whatever answered".
REQUIRED_TARGET_KEYS = (
    "provider",
    "region",
    "workspace_cluster",
    "workspace_cluster_arn",
    "workspace_api_endpoint",
    "skypilot_api",
    "skypilot_runtime_version",
    "controller",
)

# An EKS cluster ARN, parsed strictly so account, region and name are all present
# and can be compared as a whole rather than by substring.
_CLUSTER_ARN = re.compile(
    r"arn:aws[a-z-]*:eks:(?P<region>[a-z0-9-]{1,32}):(?P<account>\d{12}):"
    r"cluster/(?P<name>[A-Za-z0-9][A-Za-z0-9_-]{0,99})"
)

# Observer classes whose output may be published as live evidence. Membership is
# by exact type, not by subclassing or duck typing: a fake that merely satisfies
# the Protocol must not become publishable, which is the mock-results-reported-as
# -evidence failure the issue names. Registered lazily by live_observer to keep
# this module free of an import cycle.
LIVE_OBSERVERS: tuple[type, ...] = ()


def register_live_observer(observer_type: type) -> None:
    """Record a reviewed read-only observer as publishable.

    Called once by :mod:`superplane_acceptance.live_observer` at import. Being
    registered is necessary but not sufficient: :func:`capture` additionally
    requires the instance to report live transports, so the reviewed adapter
    driven by an offline transport still yields fixture evidence.
    """
    global LIVE_OBSERVERS
    if observer_type not in LIVE_OBSERVERS:
        LIVE_OBSERVERS = (*LIVE_OBSERVERS, observer_type)


class Outcome(str, Enum):
    """What an observation established about its check.

    ``str``-valued for the same reason as the contracts package's statuses: the
    record serializes to a stable wire string rather than an ordinal.
    """

    # The expected behavior was observed to happen.
    SATISFIED = "satisfied"
    # The expected behavior was observed NOT to happen. Retained as a finding,
    # never conflated with "not run" and never averaged away by a neighbouring
    # success.
    REFUTED = "refuted"
    # Looked at, could not be established either way. Fails the check, because an
    # unestablished behavior is not an observed one.
    INDETERMINATE = "indeterminate"


# U12-L1 covers everything except serving; U12-L2 covers serving alone. They are
# separate criteria because a batch result cannot establish endpoint
# reachability, unauthenticated refusal or owning-controller teardown.
BASELINE_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension.PROVIDER_SELECTION,
    Dimension.NODE_REGISTRATION,
    Dimension.BATCH_WORKLOAD,
    Dimension.STATUS_AND_LOGS,
    Dimension.STOP_CANCELLATION,
    Dimension.CONTROLLER_LIFECYCLE,
    Dimension.COST_AND_CLEANUP,
)
SERVING_DIMENSIONS: tuple[Dimension, ...] = (Dimension.SERVING_WORKLOAD,)

# The dimension a resource's life starts in. Anything observed about a machine in
# a later dimension must concern a machine this one saw provisioned.
ORIGIN_DIMENSION = Dimension.PROVIDER_SELECTION

# Checks whose satisfaction needs more than "a satisfied live observation".
# Cleanup: a successful Down/purge only means SkyPilot dropped its handle, so
# only independent provider confirmation counts.
PROVIDER_ABSENCE_CHECK = "cleanup.provider-side-absence-verified"
# Cost: a missing figure is recorded as unknown. Unknown is not zero spend.
COST_CHECK = "cost.hourly-and-daily-aggregation"

# Environment variables carrying the operator's explicit selection.
INPUTS = (
    "SUPERPLANE_LIVE_BASELINE_ENVIRONMENT",
    "SUPERPLANE_LIVE_BASELINE_REVISION",
    "SUPERPLANE_LIVE_BASELINE_SOURCE_REVISION",
    "SUPERPLANE_LIVE_BASELINE_SCENARIOS",
    "SUPERPLANE_LIVE_BASELINE_AUTHORIZATION",
    "SUPERPLANE_LIVE_BASELINE_WINDOW_START",
    "SUPERPLANE_LIVE_BASELINE_EVIDENCE_FILE",
)

# Where the authorized operation's retained records live. Optional as an input,
# because a capture with none supplied must still run and report precisely which
# checks are therefore unestablished -- but without it the behaviors that exist
# only while an operation is in flight can never be satisfied, which is what the
# second review rejected. See :mod:`superplane_acceptance.live_observer` for the
# record format and the validation applied to each entry.
RECEIPTS_VARIABLE = "SUPERPLANE_LIVE_BASELINE_RECEIPTS_DIR"

# Whether an executing checkout with uncommitted changes may publish. Default is
# refusal: a dirty tree means the recorded source revision does not describe the
# code that produced the evidence, so a later reader cannot reproduce the finding
# from that revision. An operator who accepts that gap must say so explicitly.
ALLOW_DIRTY_VARIABLE = "SUPERPLANE_LIVE_BASELINE_ALLOW_DIRTY_SOURCE"

# How far before the capture an authorized execution window may have opened.
# The window exists to reject a receipt replayed from an earlier session, so it
# needs a real lower bound; without one, "observed at some point in history"
# would satisfy it. Evidence for an already-finished operation -- a cancellation,
# a teardown, a provider confirming absence -- is necessarily observed BEFORE the
# capture runs, so the bound cannot be the capture's own start.
MAX_WINDOW_HOURS = 24

KNOWN_SCENARIOS = frozenset(scenario.scenario_id for scenario in SCENARIOS)
SERVING_SCENARIO = "serving-via-sky-serve-yaml"
KNOWN_STATE_KINDS = frozenset(entry.kind for entry in EXISTING_STATE_CLASSES)

# --------------------------------------------------------------------------
# Published-output sanitation.
#
# Every string that reaches the evidence file passes _safe(), whether the
# observer produced it or the operator typed it. Private file permissions do not
# satisfy "keep credentials and sensitive response bodies out of published
# artifacts": the artifact is meant to be shared with #5067's evaluator and U19,
# so anything unsafe has to be refused before it is written, not protected in
# place.
#
# The screen is deliberately a rejection rather than a redaction. A redacted
# record invites the reader to assume the rest is intact; a refused capture makes
# the observer supply a reference instead of a body, which is what the acceptance
# rules ask for.
# --------------------------------------------------------------------------
MAX_DETAIL = 400
MAX_IDENTIFIER = 200

_SECRET_MARKERS = re.compile(
    r"""
    -----BEGIN|                       # PEM private key / certificate block
    \bBearer\s|\bBasic\s|             # Authorization header values
    \bAKIA[0-9A-Z]{16}\b|             # AWS access key id
    \bASIA[0-9A-Z]{16}\b|             # AWS temporary access key id
    \beyJ[A-Za-z0-9_-]{10,}|          # JWT
    \bxox[baprs]-|                    # Slack token
    \bgh[pousr]_[A-Za-z0-9]{20,}|     # GitHub token
    (?:password|passwd|secret|token|api[_-]?key|credential|activation[_-]?code)
        \s*[:=]                       # a credential assigned inline
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A long unbroken high-entropy run: an opaque token pasted where a human-readable
# reference belongs. The comment has always said "or contain separators", but `-`
# and `_` were themselves inside the class, so a readable kebab-case reference
# long enough to cross the threshold -- which the retained-receipt references are,
# since they carry the check id -- was read as a token. Separators now break a run,
# which is what makes this a test for opacity rather than for length. Base64
# padding and `+`/`/` stay inside the class: they are alphabet, not separators.
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9+/=]{40,}")

_RAW_BODY_MARKERS = re.compile(
    r"""
    \{\s*"|\[\s*\{|                   # a JSON object/array body
    <\?xml|<html|<!DOCTYPE|           # a markup body
    HTTP/[0-9]\.[0-9]|                # a raw status line
    ^\s*(?:set-cookie|authorization)\s*:  # a raw header
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)


def _safe(value: object, label: str, limit: int = MAX_DETAIL) -> str:
    """Return a published-safe string, or refuse the capture.

    Bounded, single-line, free of credential and raw-body shapes. Applied to
    observer-controlled and operator-controlled text alike, because the threat is
    the content of the field rather than who supplied it.
    """
    __tracebackhide__ = True
    require(isinstance(value, str), f"{label} must be text")
    assert isinstance(value, str)  # narrowed by the require above
    require(bool(value.strip()), f"{label} must not be empty")
    require(
        len(value) <= limit,
        f"{label} exceeds the {limit}-character published limit; reference the "
        "retained evidence instead of inlining it",
    )
    require(
        value.isprintable(),
        f"{label} contains control characters or newlines; publish a reference, "
        "not a captured body",
    )
    require(
        _SECRET_MARKERS.search(value) is None,
        f"{label} looks like a credential; publish a non-secret reference",
    )
    require(
        _OPAQUE_RUN.search(value) is None,
        f"{label} contains an opaque high-entropy run; publish a readable "
        "reference rather than a token",
    )
    require(
        _RAW_BODY_MARKERS.search(value) is None,
        f"{label} looks like a raw API or provider response body; publish a "
        "reference and hash instead",
    )
    return value


def _safe_hash(value: object, label: str) -> str:
    """Require a sha256 of the retained raw evidence.

    Hashes are exempt from the opaque-run screen because they are the one field
    whose whole purpose is an opaque digest -- and being exactly 64 hex digits is
    checked here instead.
    """
    __tracebackhide__ = True
    require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        f"{label} must be the sha256 of the retained raw evidence",
    )
    assert isinstance(value, str)  # narrowed by the require above
    return value


def _checkout_revision() -> tuple[str, bool]:
    """The revision of the checkout actually executing, and whether it is dirty.

    The operator types the source revision, so on its own it is a claim about
    which code ran rather than a fact. Reading it from the executing tree makes
    the two comparable, which is the point: a capture published under a revision
    that does not describe the code that produced it cannot be reproduced.

    Returns empty when this is not a git checkout at all -- vendored into an image,
    for instance. That is reported to the caller rather than guessed at, because
    "no checkout to compare against" and "the checkout disagrees" are different
    findings.
    """
    __tracebackhide__ = True
    repository = Path(__file__).resolve().parent
    try:
        head = subprocess.run(  # fixed argv, no shell
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        if head.returncode != 0:
            return "", False
        revision = head.stdout.decode("ascii", "replace").strip()
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            return "", False
        # Only this domain module's tree is considered: an unrelated edit
        # elsewhere in the monorepo does not change the code that observed.
        status = subprocess.run(  # fixed argv, no shell
            ["git", "-C", str(repository), "status", "--porcelain", "--", "."],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        dirty = status.returncode != 0 or bool(status.stdout.strip())
        return revision, dirty
    except (OSError, subprocess.TimeoutExpired):
        return "", False


def _verify_source_revision(claimed: str, environment) -> dict[str, object]:
    """Require the operator's claimed source revision to be the one executing.

    The second review's finding was that any SHA-shaped string was accepted. A
    capture is supposed to be reproducible from the revision it names, so the
    claim is checked against the checkout and a disagreement is refused rather
    than recorded.
    """
    __tracebackhide__ = True
    observed, dirty = _checkout_revision()
    if not observed:
        # Not a checkout. Recorded as unverified rather than silently accepted, so
        # a reader can see that the provenance rests on the operator's word.
        return {
            "claimed": claimed,
            "verified_against_checkout": False,
            "checkout_dirty": False,
            "note": (
                "The executing tree is not a git checkout, so the source revision "
                "could not be verified against it"
            ),
        }
    require(
        observed.startswith(claimed) or claimed.startswith(observed),
        "BLOCKED: the recorded maintained-source revision is not the revision of "
        "the checkout performing this capture; publish the revision that actually "
        "observed",
    )
    if dirty:
        require(
            str(environment.get(ALLOW_DIRTY_VARIABLE, "")).strip().lower() == "true",
            "BLOCKED: the executing checkout has uncommitted changes, so the "
            "recorded source revision does not describe the code that observed; "
            f"set {ALLOW_DIRTY_VARIABLE}=true to publish with that gap recorded",
        )
    return {
        "claimed": claimed,
        "observed_in_checkout": observed,
        "verified_against_checkout": True,
        "checkout_dirty": dirty,
    }


def _require_aware(moment: object, label: str) -> None:
    """Require a timezone-aware instant; a naive one cannot be placed in a window."""
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")


@dataclass(frozen=True)
class ResourceIdentity:
    """Which actual resource or controller an observation is about.

    Wrong resource identity is the specific way a baseline capture becomes
    unsound: U19 would later compare its migrated behavior against a measurement
    of something else. At least one concrete handle is required, so a fact cannot
    be recorded about nothing, and every field is published-safe so a handle
    cannot smuggle a body or a token into the record.
    """

    provider: str = ""
    provider_resource_id: str = ""
    cluster: str = ""
    kubernetes_node: str = ""
    controller: str = ""
    skypilot_cluster: str = ""

    def __post_init__(self) -> None:
        if not any(
            (
                self.provider_resource_id,
                self.kubernetes_node,
                self.controller,
                self.skypilot_cluster,
            )
        ):
            raise ValueError(
                "a resource identity needs a provider resource, Kubernetes node, "
                "controller or SkyPilot cluster handle"
            )
        for name, value in self.as_record().items():
            _safe(value, f"resource {name}", MAX_IDENTIFIER)

    def as_record(self) -> dict[str, str]:
        return {
            name: value
            for name, value in (
                ("provider", self.provider),
                ("provider_resource_id", self.provider_resource_id),
                ("cluster", self.cluster),
                ("kubernetes_node", self.kubernetes_node),
                ("controller", self.controller),
                ("skypilot_cluster", self.skypilot_cluster),
            )
            if value
        }

    # Handles that identify one machine across dimensions, used to correlate a
    # provisioning observation with the node, workload and cleanup observations
    # that must concern the same machine.
    def correlation_handles(self) -> tuple[str, ...]:
        return tuple(
            handle
            for handle in (
                self.provider_resource_id,
                self.kubernetes_node,
                self.skypilot_cluster,
            )
            if handle
        )


@dataclass(frozen=True)
class ObservedFact:
    """One external observation, with its outcome, identity, evidence and instant.

    ``outcome`` is required and is the only thing that can satisfy a check:
    recording that an observation happened is not recording that the expected
    behavior happened. ``evidence_reference``/``evidence_sha256`` locate and pin
    the retained raw evidence so a later reader can re-derive the finding without
    the raw body being republished here. ``observed_at`` is checked against the
    run's authorized window, so a stale receipt from an earlier session cannot be
    replayed into this capture.
    """

    check_id: str
    environment: str
    revision: str
    observed_at: datetime
    resource: ResourceIdentity
    outcome: Outcome
    detail: str
    # Where the retained raw observation lives, and its digest.
    evidence_reference: str
    evidence_sha256: str
    # The launch request, workload specification or probe this fact concerns.
    request_reference: str = ""
    # Independent provider confirmation that an instance is gone. A SkyPilot
    # Down or purge succeeding is explicitly not this.
    provider_absence_confirmed: bool = False
    # None means the baseline reported no figure: unknown, which is not zero.
    hourly_cost: float | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, f"{self.check_id}: observed_at")
        if not isinstance(self.resource, ResourceIdentity):
            raise TypeError(f"{self.check_id}: needs a ResourceIdentity")
        if not isinstance(self.outcome, Outcome):
            raise TypeError(
                f"{self.check_id}: outcome must be an Outcome; an observation has "
                "to state whether the behavior was satisfied, refuted or "
                "indeterminate"
            )
        _safe(self.detail, f"{self.check_id}: detail")
        _safe(self.evidence_reference, f"{self.check_id}: evidence_reference")
        _safe_hash(self.evidence_sha256, f"{self.check_id}: evidence_sha256")
        if self.request_reference:
            _safe(self.request_reference, f"{self.check_id}: request_reference")
        if self.hourly_cost is not None and (
            not isinstance(self.hourly_cost, int | float)
            or isinstance(self.hourly_cost, bool)
            or self.hourly_cost < 0
        ):
            raise ValueError(
                f"{self.check_id}: hourly_cost must be a non-negative number"
            )

    def as_receipt(self) -> dict:
        """The auditable per-observation record kept in the evidence file."""
        receipt = {
            "outcome": self.outcome.value,
            "observed_at": self.observed_at.isoformat(),
            "environment": self.environment,
            "deployed_revision": self.revision,
            "resource": self.resource.as_record(),
            "evidence_reference": self.evidence_reference,
            "evidence_sha256": self.evidence_sha256,
            "detail": self.detail,
        }
        if self.request_reference:
            receipt["request_reference"] = self.request_reference
        if self.provider_absence_confirmed:
            receipt["provider_side_absence_confirmed"] = True
        if self.hourly_cost is not None:
            receipt["hourly_cost"] = self.hourly_cost
        return receipt


@dataclass(frozen=True)
class ObservedEnvironmentIdentity:
    """Provider, region, controller and runtime as the environment itself reported.

    This type exists because the second review found the previous code filling
    those fields from the operator's own configuration and then comparing them back
    to it -- a check that cannot fail and therefore establishes nothing. Keeping
    the observed identity in its own record, built only from live responses and
    each part pinned to the read it came from, makes
    :func:`_confirm_environment_identity` a real comparison: a run against a
    different provider, region, controller or runtime version is refused instead of
    being labelled as the selected baseline.
    """

    provider: str
    region: str
    controller: str
    runtime_version: str
    cluster_arn: str
    api_endpoint: str
    observed_at: datetime
    evidence_reference: str
    evidence_sha256: str
    # Fields the environment did not report, named rather than defaulted: an
    # unreported identity is an unverified one, not a matching one.
    unreported: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed identity: observed_at")
        _safe(self.evidence_reference, "observed identity: evidence_reference")
        _safe_hash(self.evidence_sha256, "observed identity: evidence_sha256")
        for name, value in self.as_record().items():
            if value:
                _safe(value, f"observed identity: {name}", MAX_IDENTIFIER)
        for name in self.unreported:
            require(
                name in _IDENTITY_FIELDS,
                f"{name!r} is not an environment identity field",
            )
            require(
                not getattr(self, name),
                f"observed identity: {name} is reported and cannot also be listed "
                "as unreported",
            )

    def as_record(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in _IDENTITY_FIELDS}


_IDENTITY_FIELDS = (
    "provider",
    "region",
    "controller",
    "runtime_version",
    "cluster_arn",
    "api_endpoint",
)

# How each observed identity field is compared against the registered target.
_IDENTITY_EXPECTATIONS = {
    "provider": "provider",
    "region": "region",
    "controller": "controller",
    "runtime_version": "skypilot_runtime_version",
    "cluster_arn": "workspace_cluster_arn",
    "api_endpoint": "workspace_api_endpoint",
}


def _confirm_environment_identity(
    config: dict,
    identity: object,
    window: _Window,
) -> ObservedEnvironmentIdentity:
    """Require what the environment reported to be what was selected.

    Each field is compared whole. An unreported field cannot satisfy its
    expectation: it leaves the identity partially unverified, which is recorded and
    -- for the fields that decide whether this is the right system at all -- blocks
    the capture.
    """
    __tracebackhide__ = True
    require(
        isinstance(identity, ObservedEnvironmentIdentity),
        "BLOCKED: the environment's own provider, region, controller and runtime "
        "must be observed, not copied from the selection",
    )
    assert isinstance(identity, ObservedEnvironmentIdentity)
    require(
        window.contains(identity.observed_at),
        "The environment identity was observed outside this run's window",
    )
    metadata = config["target_metadata"]
    for name, expectation in _IDENTITY_EXPECTATIONS.items():
        observed = getattr(identity, name)
        if not observed:
            require(
                name in identity.unreported,
                f"observed identity: {name} is empty and not declared unreported",
            )
            continue
        require(
            observed == metadata[expectation],
            f"BLOCKED: the environment reported {name} {observed!r}, which is not "
            f"the selected target's {expectation}; the evidence concerns another "
            "system",
        )
    # Provider, region and cluster decide whether this is the selected system at
    # all, so leaving them unreported is not a partial result -- there would be
    # nothing binding the observations to the baseline.
    essential = sorted(
        name
        for name in ("provider", "region", "cluster_arn")
        if not getattr(identity, name)
    )
    require(
        not essential,
        "BLOCKED: the environment did not report the identity fields that bind "
        "evidence to the selected baseline: " + ", ".join(essential),
    )
    return identity


@dataclass(frozen=True)
class ServingInventory:
    """What serving the selected baseline actually has, as observed.

    Absence of a serving scenario has to rest on this. An operator selecting an
    empty scenario list is a statement of intent, not evidence that no service is
    running -- and the baseline's SkyServe specs are operator-run CLI artifacts
    with no owning controller, so a service would appear in no CR listing at all.
    """

    enumerated_via: str
    environment: str
    revision: str
    observed_at: datetime
    evidence_reference: str
    evidence_sha256: str
    services: tuple[str, ...] = ()
    # Whether an independent producer vouched for the listing's bytes. Required
    # before absence may be concluded: a hand-written empty listing is not an
    # observation that nothing is serving, and treating it as one is how U12-L2
    # could pass with no serving evidence at all.
    authenticated: bool = False

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "serving inventory: observed_at")
        _safe(self.enumerated_via, "serving inventory: enumerated_via")
        _safe(self.evidence_reference, "serving inventory: evidence_reference")
        _safe_hash(self.evidence_sha256, "serving inventory: evidence_sha256")
        for name in self.services:
            _safe(name, "serving inventory: service name", MAX_IDENTIFIER)


@dataclass(frozen=True)
class ExistingStateRecord:
    """Live state U19 must account for, with its decision left open.

    ``kind`` must be one of U12's recorded classes so this capture feeds U19's
    adopt / drain-relaunch / no-existing-state decision instead of inventing a
    parallel taxonomy, and :func:`_existing_state` requires *every* class to be
    enumerated: omitting one is how a cutover discovers a running cluster it
    never planned for. ``empty_verified`` is why the absence of handles is a
    finding rather than a silence -- "no existing state" is a claim to verify.
    """

    kind: str
    enumerated_via: str
    environment: str
    revision: str
    observed_at: datetime
    evidence_reference: str
    evidence_sha256: str
    handles: tuple[str, ...] = ()
    empty_verified: bool = False
    decision: str = "undecided"
    # Why this class could not be settled from its authoritative source. A third
    # state alongside "here are the handles" and "verified empty", because the
    # second review found an unsettled class being filled with whatever was to
    # hand: every Kubernetes node became migration state regardless of whether it
    # belonged to this system. An unresolved class is BLOCKED and says so.
    unresolved_reason: str = ""
    # Operations in flight at enumeration time. A cutover that happens mid-launch
    # or mid-teardown is exactly the case a handover plan has to cover, so these
    # are recorded rather than left to the cluster list to imply.
    active_operations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, f"{self.kind}: observed_at")
        if self.kind not in KNOWN_STATE_KINDS:
            raise ValueError(f"{self.kind!r} is not a recorded existing-state class")
        if self.decision not in HANDOVER_DECISIONS:
            raise ValueError(f"{self.decision!r} is not a permitted handover decision")
        states = (
            bool(self.handles),
            bool(self.empty_verified),
            bool(self.unresolved_reason),
        )
        if sum(states) != 1:
            raise ValueError(
                f"{self.kind}: record exactly one of enumerated handles, a verified "
                "empty result, or the reason the class is unresolved"
            )
        if self.unresolved_reason:
            _safe(self.unresolved_reason, f"{self.kind}: unresolved_reason")
            if self.active_operations:
                raise ValueError(
                    f"{self.kind}: an unresolved class cannot also report active "
                    "operations"
                )
        _safe(self.enumerated_via, f"{self.kind}: enumerated_via")
        _safe(self.evidence_reference, f"{self.kind}: evidence_reference")
        _safe_hash(self.evidence_sha256, f"{self.kind}: evidence_sha256")
        for handle in self.handles:
            _safe(handle, f"{self.kind}: handle", MAX_IDENTIFIER)
        for operation in self.active_operations:
            _safe(operation, f"{self.kind}: active operation", MAX_IDENTIFIER)

    @property
    def resolved(self) -> bool:
        """Whether this class was settled from its authoritative source."""
        return not self.unresolved_reason


@runtime_checkable
class BaselineObserver(Protocol):
    """Read-only access to a selected baseline environment.

    Every method observes. None launches, stops, tears down, purges, deletes,
    drains, scales or applies anything, so a capture run has no code path by
    which it creates, mutates or bills a resource. Generating something to
    observe is the operator's separately authorized action.
    """

    def observe(self, dimension: Dimension) -> tuple[ObservedFact, ...]:
        """Facts for one parity dimension of the selected environment."""
        ...

    def environment_identity(self) -> ObservedEnvironmentIdentity | None:
        """Provider, region, controller and runtime as the environment reported them.

        Read from live responses and deployment metadata. Returning the configured
        expectations here would make :func:`_confirm_environment_identity` compare
        the selection against itself, which is the defect this method exists to
        close.
        """
        ...

    def serving_inventory(self) -> ServingInventory | None:
        """What serving exists in the selected baseline, as enumerated."""
        ...

    def existing_state(self) -> tuple[ExistingStateRecord, ...]:
        """Live clusters, nodes, services, state stores and handles for U19."""
        ...

    def transport_is_live(self) -> bool:
        """Whether this instance reached the real environment.

        A reviewed observer driven by an offline transport must report False, so
        its output stays fixture evidence. Checked in addition to the type being
        registered, never instead of it.
        """
        ...


def observer_for(config: dict) -> BaselineObserver:
    """Build the reviewed read-only observer for the selected target.

    Delegates to :mod:`superplane_acceptance.live_observer`, imported here rather
    than at module scope because that module registers itself back into
    :data:`LIVE_OBSERVERS`.
    """
    __tracebackhide__ = True
    from .live_observer import build_observer

    return build_observer(config)


def _scenario_selection(selection: str) -> tuple[str, ...]:
    __tracebackhide__ = True
    scenarios = tuple(
        dict.fromkeys(
            part for part in (n.strip() for n in selection.split(",")) if part
        )
    )
    require(
        bool(scenarios), "BLOCKED: select at least one baseline scenario to observe"
    )
    unknown = [name for name in scenarios if name not in KNOWN_SCENARIOS]
    require(
        not unknown,
        "Selected scenarios are not in U12's recorded inventory: "
        + ", ".join(sorted(unknown)),
    )
    return scenarios


def settings(environment) -> dict:
    """Validate the operator's explicit selection, or fail BLOCKED.

    Nothing is inferred and nothing defaults. A missing environment, revision,
    source revision, scenario selection, authorization reference, execution
    window or evidence path is a blocked run, never a skip and never a pass.
    """
    __tracebackhide__ = True
    missing = [name for name in INPUTS if not environment.get(name)]
    require(
        not missing,
        "BLOCKED: missing explicit baseline acceptance inputs: " + ", ".join(missing),
    )
    (
        target,
        revision,
        source_revision,
        selection,
        authorization,
        window_start,
        output,
    ) = (environment[name].strip() for name in INPUTS)
    require(
        target in BASELINE_TARGETS,
        "BLOCKED: no reviewed baseline environment is registered; the selected "
        "Superplane environment, its access and its observer mapping remain the "
        "EPIC A supervisor's decision",
    )
    metadata = dict(BASELINE_TARGETS[target])
    absent = [key for key in REQUIRED_TARGET_KEYS if not metadata.get(key)]
    require(
        not absent,
        "BLOCKED: the registered target is missing the configuration observations "
        "are checked against: " + ", ".join(absent),
    )
    for key, value in metadata.items():
        _safe(value, f"target {key}", MAX_IDENTIFIER)
    # A registered target must carry an identity that can be compared whole. A
    # name alone admits the lookalike the second review reproduced.
    arn = _CLUSTER_ARN.fullmatch(metadata["workspace_cluster_arn"])
    require(
        arn is not None,
        "BLOCKED: the registered target's workspace cluster must be recorded as a "
        "full EKS ARN including account, region and name",
    )
    assert arn is not None  # narrowed by the require above
    require(
        arn.group("name") == metadata["workspace_cluster"]
        and arn.group("region") == metadata["region"],
        "BLOCKED: the registered target's cluster ARN disagrees with its recorded "
        "cluster name or region",
    )
    require(
        metadata["workspace_api_endpoint"].startswith("https://"),
        "BLOCKED: the registered target's Kubernetes API endpoint must be an "
        "https origin, so the observed kubeconfig server can be compared to it",
    )
    # Exact, for the same reason the cluster must be an ARN: this revision is
    # compared whole against what an independent producer reports the evidence
    # attempt actually executed (`head_sha`, always a full commit). An
    # abbreviation cannot be compared whole -- it would either be rejected against
    # every genuine producer record, or invite a prefix comparison that admits a
    # different commit sharing the prefix. The operator knows the deployed commit
    # exactly, so asking for it exactly costs nothing.
    require(
        re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
        "BLOCKED: record the deployed revision actually running in the baseline "
        "as an exact 40-hex commit, so it can be compared whole against the "
        "revision the evidence-producing attempt reports it executed",
    )
    # The maintained-source revision this check ran from, kept separately from the
    # revision deployed in the baseline: they are different provenance facts and a
    # reader needs both to reproduce a finding.
    require(
        re.fullmatch(r"[0-9a-f]{7,40}", source_revision) is not None,
        "BLOCKED: record the maintained-source revision this capture ran from",
    )
    # Retained authorization for the operations being observed: spend limit,
    # deadline and cleanup owner. This check neither grants nor acquires it, and
    # it must be a readable reference -- a pasted credential is refused, not kept.
    require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:#/@-]{7,180}", authorization)
        is not None,
        "BLOCKED: reference the retained access/spend/deadline/cleanup "
        "authorization record; supply a readable reference, never a credential",
    )
    _safe(authorization, "authorization reference", MAX_IDENTIFIER)
    scenarios = _scenario_selection(selection)
    # The authorized execution window the observed operations ran in. Required as
    # an input because the operator knows it and this check cannot infer it.
    try:
        opened = datetime.fromisoformat(window_start)
    except ValueError:
        raise EvidenceError(
            "BLOCKED: give the authorized execution window start as an ISO-8601 "
            "instant with a UTC offset"
        ) from None
    require(
        opened.tzinfo is not None,
        "BLOCKED: the execution window start needs an explicit UTC offset",
    )
    now = datetime.now(timezone.utc)
    require(
        opened <= now,
        "The execution window cannot open in the future",
    )
    require(
        now - opened <= timedelta(hours=MAX_WINDOW_HOURS),
        f"The execution window opened over {MAX_WINDOW_HOURS}h ago; capture "
        "evidence within its window rather than replaying an older session",
    )
    path = Path(output)
    require(
        path.is_absolute() and path.parent.is_dir() and not os.path.lexists(path),
        "BLOCKED: evidence needs an absolute new filename in an existing directory",
    )
    # The retained records of the authorized operation, if the operator supplied
    # them. Absent, the in-flight behaviors stay unestablished and the run reports
    # which ones -- present, they must be a readable directory, checked here so a
    # typo surfaces as a blocked input rather than as silently missing evidence.
    receipts = str(environment.get(RECEIPTS_VARIABLE, "")).strip()
    if receipts:
        receipts_path = Path(receipts)
        require(
            receipts_path.is_absolute() and receipts_path.is_dir(),
            f"BLOCKED: {RECEIPTS_VARIABLE} must be an absolute existing directory "
            "of retained operation records",
        )
    provenance = _verify_source_revision(source_revision, environment)
    # Never retain a token or the rest of the process environment in the config.
    return {
        "environment": target,
        "revision": revision,
        "source_revision": source_revision,
        "source_provenance": provenance,
        "scenarios": scenarios,
        "authorization": authorization,
        "window_start": opened,
        "evidence_file": output,
        "receipts_dir": receipts,
        "target_metadata": metadata,
    }


class _Window:
    """The authorized execution window evidence must fall inside.

    It opens when the operator's authorized window opened -- not when this capture
    started -- because evidence for an operation that has already finished is
    necessarily observed beforehand: a cancellation, a teardown, or a provider
    confirming an instance is gone. Requiring observation after the capture's own
    start would have made exactly the lifecycle criteria R17 cares about
    impossible to satisfy.

    It closes at validation time, so nothing can be dated into the future, and it
    is checked per fact rather than once at the end so a late foreign record
    cannot slip in.
    """

    def __init__(self, opened: datetime, started: datetime) -> None:
        self.opened = opened
        self.started = started

    def contains(self, moment: datetime) -> bool:
        return self.opened <= moment <= datetime.now(timezone.utc)


@dataclass
class _Correlation:
    """Binds every observation to one coherent machine and controller.

    Two rules, both about the same failure: evidence that is well-formed and
    in-window but concerns something other than the selected operation.

    * A handle may not be bound to two different identities. If
      ``i-0baseline`` appears once as node ``sky-node-1`` and once as
      ``other-node``, one of the two observations is about a different machine.
    * A machine observed joining, running a workload or being cleaned up must be
      a machine this capture also saw provisioned. Otherwise the lifecycle
      evidence describes a resource whose origin is unrecorded, and U19 cannot
      tell which of its own resources to compare against.

    The second rule is only applied when provisioning evidence exists at all;
    without it the criterion is already unsatisfied for missing coverage, and
    raising instead would hide that more specific finding.
    """

    bindings: dict[str, dict[str, str]] = field(default_factory=dict)
    origin_handles: set[str] = field(default_factory=set)
    downstream: dict[str, str] = field(default_factory=dict)

    def record(self, fact: ObservedFact, dimension: Dimension) -> None:
        __tracebackhide__ = True
        identity = fact.resource.as_record()
        for handle in fact.resource.correlation_handles():
            known = self.bindings.setdefault(handle, {})
            for name, value in identity.items():
                previous = known.get(name)
                require(
                    previous is None or previous == value,
                    f"{fact.check_id}: {handle!r} was already observed with a "
                    f"different {name}; the evidence concerns two resources",
                )
                known[name] = value
            if dimension is ORIGIN_DIMENSION:
                self.origin_handles.add(handle)
            else:
                self.downstream.setdefault(handle, fact.check_id)

    def finalize(self) -> None:
        __tracebackhide__ = True
        if not self.origin_handles:
            return
        for handle, check_id in sorted(self.downstream.items()):
            require(
                handle in self.origin_handles,
                f"{check_id}: {handle!r} was never observed being provisioned; "
                "lifecycle evidence must concern the selected operation's resource",
            )


def _validated(
    fact: object,
    config: dict,
    check_ids: frozenset[str],
    dimension: Dimension,
    window: _Window,
    correlation: _Correlation,
) -> ObservedFact:
    """Reject foreign-target, foreign-resource, unselected, stale and off-dimension
    evidence."""
    __tracebackhide__ = True
    require(
        isinstance(fact, ObservedFact),
        f"{dimension.value}: observations must be ObservedFact records",
    )
    assert isinstance(fact, ObservedFact)  # narrowed by the require above
    require(
        fact.check_id in check_ids,
        f"{fact.check_id!r} is not a check of dimension {dimension.value}",
    )
    require(
        _in_scope(fact.check_id, config["scenarios"]),
        f"{fact.check_id}: evidence belongs to a scenario that was not selected "
        "for observation",
    )
    require(
        fact.environment == config["environment"],
        f"{fact.check_id}: evidence belongs to another environment",
    )
    require(
        fact.revision == config["revision"],
        f"{fact.check_id}: evidence was observed on another deployed revision",
    )
    require(
        window.contains(fact.observed_at),
        f"{fact.check_id}: observation falls outside this run's window",
    )
    _bound_to_target(fact, config["target_metadata"])
    correlation.record(fact, dimension)
    return fact


def _bound_to_target(fact: ObservedFact, metadata: dict[str, str]) -> None:
    """Require the observed provider, cluster and controller to be the selected ones.

    The reviewers' reproduction kept the expected environment and revision labels
    while naming a foreign provider, cluster, controller and instance. Labels are
    what the operator typed; these are what was actually observed, so they are
    the ones that have to match the reviewed target's recorded configuration.
    """
    __tracebackhide__ = True
    for observed, expected, name in (
        (fact.resource.provider, metadata["provider"], "provider"),
        # The canonical ARN, not the bare name: a name alone cannot distinguish the
        # selected cluster from a lookalike or from a same-named cluster in another
        # account.
        (fact.resource.cluster, metadata["workspace_cluster_arn"], "cluster"),
        (fact.resource.controller, metadata["controller"], "controller"),
    ):
        require(
            not observed or observed == expected,
            f"{fact.check_id}: observed {name} {observed!r} is not the selected "
            f"target's {name}",
        )


def _in_scope(check_id: str, selected: tuple[str, ...]) -> bool:
    """Whether a check's baseline scenario was selected for observation."""
    scenarios = check_by_id(check_id).baseline_scenarios
    return any(scenario in selected for scenario in scenarios)


@dataclass(frozen=True)
class _CheckCapture:
    """One check's result together with the observations behind it."""

    result: ParityResult
    facts: tuple[ObservedFact, ...]
    in_scope: bool


def _result(
    check_id: str,
    facts: tuple[ObservedFact, ...],
    kind: EvidenceKind,
    in_scope: bool,
) -> ParityResult:
    """Turn observations of one check into a result.

    The ordering matters: a refutation outranks a success for the same check, so
    contradictory evidence fails rather than being satisfied by its optimistic
    half. Both are kept in the record's receipts.
    """
    __tracebackhide__ = True
    if not in_scope:
        return ParityResult(
            check_id=check_id,
            passed=False,
            evidence=EvidenceKind.NOT_RUN,
            detail=(
                "This check's baseline scenario was not selected for observation, "
                "so the criterion's coverage is incomplete."
            ),
        )
    if not facts:
        # Untested or missing behavior stays explicit. It is never inferred from
        # a neighbouring success and never quietly dropped.
        return ParityResult(
            check_id=check_id,
            passed=False,
            evidence=EvidenceKind.NOT_RUN,
            detail="No observation was captured for this check.",
        )
    absence = any(fact.provider_absence_confirmed for fact in facts)
    refuted = [f for f in facts if f.outcome is Outcome.REFUTED]
    unresolved = [f for f in facts if f.outcome is Outcome.INDETERMINATE]
    observations = "; ".join(f"{f.outcome.value}: {f.detail}" for f in facts)
    passed = not refuted and not unresolved
    if refuted:
        # Retained as a live refutation, not downgraded to NOT_RUN: "observed not
        # to happen" is a finding U19 needs, distinct from "nobody looked".
        detail = f"Observation refuted this check. {observations}"
    elif unresolved:
        detail = f"Observation could not establish this check. {observations}"
    else:
        detail = observations
    if check_id == PROVIDER_ABSENCE_CHECK and not absence:
        # A successful Down or purge drops local state regardless of provider
        # outcome, so it cannot establish that capacity was released.
        passed = False
        detail = "Provider did not independently confirm absence. " + detail
    if check_id == COST_CHECK and all(fact.hourly_cost is None for fact in facts):
        passed = False
        detail = "Cost was not reported; unknown cost is not zero spend. " + detail
    return ParityResult(
        check_id=check_id,
        passed=passed,
        evidence=kind,
        detail=_safe(detail, f"{check_id}: aggregated detail", MAX_DETAIL * 8),
        provider_side_absence_confirmed=absence,
    )


def _capture_dimensions(
    config: dict,
    observer: BaselineObserver,
    dimensions: tuple[Dimension, ...],
    kind: EvidenceKind,
    window: _Window,
    correlation: _Correlation,
) -> dict[str, _CheckCapture]:
    """Collect and validate one criterion's dimensions into per-check captures."""
    __tracebackhide__ = True
    captures: dict[str, _CheckCapture] = {}
    for dimension in dimensions:
        entry = dimension_by_name(dimension)
        check_ids = frozenset(check.check_id for check in entry.checks)
        observed = observer.observe(dimension)
        require(
            isinstance(observed, tuple),
            f"{dimension.value}: observations must be returned as a tuple",
        )
        validated = [
            _validated(fact, config, check_ids, dimension, window, correlation)
            for fact in observed
        ]
        for check in entry.checks:
            facts = tuple(f for f in validated if f.check_id == check.check_id)
            in_scope = _in_scope(check.check_id, config["scenarios"])
            captures[check.check_id] = _CheckCapture(
                result=_result(check.check_id, facts, kind, in_scope),
                facts=facts,
                in_scope=in_scope,
            )
    return captures


def _serving_presence(
    config: dict,
    observer: BaselineObserver,
    window: _Window,
) -> tuple[ServingInventory, bool]:
    """Establish whether the selected baseline actually has serving.

    Absence is acceptable only on observed inventory evidence. Without an
    inventory observation the serving criterion is blocked rather than passed as
    "nothing to check".
    """
    __tracebackhide__ = True
    inventory = observer.serving_inventory()
    require(
        isinstance(inventory, ServingInventory),
        "BLOCKED: serving absence needs observed baseline inventory evidence; an "
        "operator selecting an empty list is insufficient",
    )
    assert isinstance(inventory, ServingInventory)  # narrowed by the require above
    require(
        inventory.environment == config["environment"]
        and inventory.revision == config["revision"],
        "Serving inventory was observed against another environment or revision",
    )
    require(
        window.contains(inventory.observed_at),
        "Serving inventory falls outside this run's observation window",
    )
    # Absence is the branch that needs authentication most. Concluding "this
    # baseline serves nothing" from a listing nobody independent vouched for is
    # indistinguishable from concluding it from a hand-written empty file, which
    # an offline reproduction did. A listing that *names* services is not relied
    # on for absence, but it still cannot satisfy the serving checks: those facts
    # carry their own authentication and are downgraded individually.
    require(
        inventory.services or inventory.authenticated,
        "BLOCKED: the serving inventory is unauthenticated, so it cannot establish "
        "that this baseline runs no service; supply a listing an independent "
        "producer vouches for",
    )
    return inventory, bool(inventory.services)


def _existing_state(
    config: dict,
    observer: BaselineObserver,
    window: _Window,
) -> tuple[ExistingStateRecord, ...]:
    """Record U19's handover inputs completely, without executing any handover."""
    __tracebackhide__ = True
    records = observer.existing_state()
    require(
        isinstance(records, tuple) and bool(records),
        "BLOCKED: record the baseline's live clusters, nodes, services, state "
        "stores and handles for U19's later handover decision",
    )
    seen: dict[str, ExistingStateRecord] = {}
    for record in records:
        require(
            isinstance(record, ExistingStateRecord),
            "Existing-state entries must be ExistingStateRecord records",
        )
        require(
            record.kind not in seen,
            f"{record.kind}: enumerated twice; one result per state class",
        )
        require(
            record.environment == config["environment"]
            and record.revision == config["revision"],
            f"{record.kind}: inventory was enumerated against another environment "
            "or revision",
        )
        require(
            window.contains(record.observed_at),
            f"{record.kind}: enumeration falls outside this run's window",
        )
        # U19 owns the adopt / drain-relaunch / no-existing-state decision. This
        # capture records its inputs; encoding a decision here would pre-empt it.
        require(
            record.decision == "undecided",
            f"{record.kind}: this capture must leave U19's handover decision "
            f"undecided, not {record.decision!r}",
        )
        seen[record.kind] = record
    omitted = sorted(KNOWN_STATE_KINDS - seen.keys())
    require(
        not omitted,
        "BLOCKED: every existing-state class must be enumerated explicitly, "
        "including a verified empty result; omitted: " + ", ".join(omitted),
    )
    return tuple(seen[kind] for kind in sorted(seen))


def capture(config: dict, observer: BaselineObserver) -> dict:
    """Observe the selected baseline and report both criteria separately.

    ``evidence_kind`` follows the observer's exact type against
    :data:`LIVE_OBSERVERS` *and* that instance reporting a live transport, not a
    caller-supplied flag -- so neither an injected fake nor the reviewed adapter
    driven by an offline transport can present itself as a live capture.
    """
    __tracebackhide__ = True
    require(
        config.get("environment") in BASELINE_TARGETS,
        "Selected target differs from the reviewed baseline registry",
    )
    live = type(observer) in LIVE_OBSERVERS and observer.transport_is_live() is True
    kind = EvidenceKind.LIVE_CAPTURE if live else EvidenceKind.SOURCE_FIXTURE

    window = _Window(config["window_start"], datetime.now(timezone.utc))
    # Confirm the environment is the selected one before gathering anything from
    # it. Observing first and checking afterwards would mean the refusal message
    # arrives after a foreign system has already been read.
    identity = _confirm_environment_identity(
        config, observer.environment_identity(), window
    )
    correlation = _Correlation()
    baseline = _capture_dimensions(
        config, observer, BASELINE_DIMENSIONS, kind, window, correlation
    )
    serving = _capture_dimensions(
        config, observer, SERVING_DIMENSIONS, kind, window, correlation
    )
    correlation.finalize()
    inventory, serving_present = _serving_presence(config, observer, window)
    _serving_consistency(serving, inventory, serving_present)
    state = _existing_state(config, observer, window)

    provenance = {
        "environment": config["environment"],
        "deployed_revision": config["revision"],
        "maintained_source_revision": config["source_revision"],
        "source_provenance": config["source_provenance"],
        "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "selected": dict(config["target_metadata"]),
        # Kept beside the selection rather than merged into it, so a reader can see
        # that these were read from the environment and compared, not assumed.
        "observed": {
            **identity.as_record(),
            "unreported_fields": list(identity.unreported),
            "observed_at": identity.observed_at.isoformat(),
            "evidence_reference": identity.evidence_reference,
            "evidence_sha256": identity.evidence_sha256,
        },
    }
    return {
        "schema_version": 2,
        "scope": "U12 R17 baseline and serving capture; no operation performed",
        "evidence_kind": "live" if live else "offline-fixture",
        "execution_window_start": window.opened.isoformat(),
        "started_at": window.started.isoformat(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "target": provenance,
        "selected_scenarios": list(config["scenarios"]),
        "scenario_coverage": _scenario_coverage(config, baseline, serving),
        "authorization_reference": config["authorization"],
        "criteria": {
            "U12-L1": _baseline_criterion(config, baseline, live),
            "U12-L2": _serving_criterion(config, serving, live, serving_present),
        },
        "serving_inventory": {
            "enumerated_via": inventory.enumerated_via,
            "observed_at": inventory.observed_at.isoformat(),
            "evidence_reference": inventory.evidence_reference,
            "evidence_sha256": inventory.evidence_sha256,
            "services": list(inventory.services),
            "serving_present_in_baseline": serving_present,
        },
        "existing_state_for_u19": [
            {
                "kind": record.kind,
                "enumerated_via": record.enumerated_via,
                "observed_at": record.observed_at.isoformat(),
                "evidence_reference": record.evidence_reference,
                "evidence_sha256": record.evidence_sha256,
                "handles": list(record.handles),
                "verified_empty": record.empty_verified,
                "resolved": record.resolved,
                "unresolved_reason": record.unresolved_reason,
                "active_operations": list(record.active_operations),
                "decision": record.decision,
            }
            for record in state
        ],
        # An unresolved state class blocks U19's handover decision rather than
        # leaving it to notice the gap, so it is named at the top level too.
        "unresolved_state_classes": [
            record.kind for record in state if not record.resolved
        ],
        "observed_resources": _observed_resources(baseline, serving),
        "not_established": [
            "U19 migration or state handover, which this capture does not perform",
            "any behavior whose check reports not_run or is left unsatisfied",
            "any behavior whose observation is recorded as refuted",
        ],
    }


def _serving_consistency(
    serving: dict[str, _CheckCapture],
    inventory: ServingInventory,
    serving_present: bool,
) -> None:
    """Refuse serving results that contradict the observed inventory.

    Serving facts with an empty inventory are not a partial pass, they are two
    observations that cannot both be true. Accepting them was how a run with no
    services could still satisfy U12-L2.
    """
    __tracebackhide__ = True
    if serving_present:
        return
    observed = sorted(
        check_id for check_id, capture in serving.items() if capture.facts
    )
    require(
        not observed,
        "Serving results were observed although the inventory enumerated no "
        "service via "
        f"{inventory.enumerated_via!r}; resolve the contradiction before "
        "publishing either: " + ", ".join(observed),
    )


def _scenario_coverage(
    config: dict,
    baseline: dict[str, _CheckCapture],
    serving: dict[str, _CheckCapture],
) -> dict:
    """Per-scenario coverage, so a partial selection is visible rather than implied."""
    captures = {**baseline, **serving}
    coverage = {}
    for scenario in sorted(KNOWN_SCENARIOS):
        checks = sorted(
            check_id
            for check_id in captures
            if scenario in check_by_id(check_id).baseline_scenarios
        )
        selected = scenario in config["scenarios"]
        coverage[scenario] = {
            "selected": selected,
            "checks": checks,
            "satisfied_checks": sorted(
                check_id for check_id in checks if captures[check_id].result.passed
            ),
        }
    return coverage


def _criterion_checks(captures: dict[str, _CheckCapture]) -> dict:
    """Per-check records, each retaining the receipts behind its outcome."""
    return {
        check_id: {
            "passed": capture.result.passed,
            "evidence": capture.result.evidence.value,
            "live_verified": capture.result.live_verified,
            "scenario_selected": capture.in_scope,
            "baseline_scenarios": list(check_by_id(check_id).baseline_scenarios),
            "provider_side_absence_confirmed": (
                capture.result.provider_side_absence_confirmed
            ),
            "detail": capture.result.detail,
            "observations": [fact.as_receipt() for fact in capture.facts],
        }
        for check_id, capture in sorted(captures.items())
    }


def _outstanding(captures: dict[str, _CheckCapture]) -> list[str]:
    return sorted(
        check_id
        for check_id, capture in captures.items()
        if not capture.result.live_verified
    )


def _uncovered(captures: dict[str, _CheckCapture]) -> list[str]:
    return sorted(
        check_id for check_id, capture in captures.items() if not capture.in_scope
    )


def _baseline_criterion(
    config: dict, captures: dict[str, _CheckCapture], live: bool
) -> dict:
    """U12-L1. Satisfied only on complete scenario coverage and live observation."""
    outstanding = _outstanding(captures)
    uncovered = _uncovered(captures)
    return {
        "title": "R17 live baseline",
        # U12 captures the baseline; U19 compares. See the module docstring on
        # why the gate is live_verified rather than supports_parity_claim.
        "satisfied": live and not outstanding and not uncovered,
        "coverage_complete": not uncovered,
        "outstanding_checks": outstanding,
        "unselected_checks": uncovered,
        "checks": _criterion_checks(captures),
    }


def _serving_criterion(
    config: dict,
    captures: dict[str, _CheckCapture],
    live: bool,
    serving_present: bool,
) -> dict:
    """U12-L2, which takes one of two consistent shapes.

    With services observed, the full serving lifecycle must be evidenced: a batch
    result cannot establish reachability, unauthenticated refusal or
    owning-controller teardown. With none observed, the criterion is satisfied by
    the *independently evidenced absence* recorded in the inventory -- and only if
    the serving scenario was actually selected, so "we never looked at serving"
    cannot pass as "serving is absent".
    """
    outstanding = _outstanding(captures)
    uncovered = _uncovered(captures)
    selected = SERVING_SCENARIO in config["scenarios"]
    if serving_present:
        satisfied = live and selected and not outstanding and not uncovered
        basis = "serving_lifecycle_observed"
    else:
        satisfied = live and selected
        basis = "serving_absent_by_observed_inventory"
    return {
        "title": "R17 serving baseline",
        "satisfied": satisfied,
        "basis": basis,
        "serving_present_in_baseline": serving_present,
        "serving_scenario_selected": selected,
        "coverage_complete": not uncovered,
        "outstanding_checks": outstanding,
        "unselected_checks": uncovered,
        "checks": _criterion_checks(captures),
    }


def _observed_resources(
    baseline: dict[str, _CheckCapture], serving: dict[str, _CheckCapture]
) -> list[dict]:
    """Every distinct resource and controller identity this capture observed.

    U19 reads this to confirm the evidence concerns the operation it is migrating
    from; the earlier record dropped it entirely, so a foreign instance id left
    no trace in the artifact.
    """
    identities: dict[str, dict] = {}
    for captures in (baseline, serving):
        for capture in captures.values():
            for fact in capture.facts:
                record = fact.resource.as_record()
                identities[json.dumps(record, sort_keys=True)] = record
    return [identities[key] for key in sorted(identities)]


def unsatisfied_criteria(report: dict) -> tuple[str, ...]:
    """Criteria in a report that observation did not satisfy."""
    return tuple(
        sorted(
            name
            for name, record in report["criteria"].items()
            if not record["satisfied"]
        )
    )


def publish(config: dict, report: dict) -> dict:
    """Write a live record once, to a new path, with private permissions.

    A fixture record is refused here rather than filtered later, and nothing is
    written unless every assertion already succeeded -- so a failed run leaves no
    file that could later be mistaken for a pass.
    """
    __tracebackhide__ = True
    require(
        report.get("evidence_kind") == "live",
        "Offline fixture evidence cannot be published as a live baseline result",
    )
    output = Path(config["evidence_file"])
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=output.parent, prefix=".u12-baseline-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(stream.fileno(), 0o600)
            stream.write(json.dumps(report, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # An exclusive atomic link refuses an existing file or symlink, races included.
        os.link(temporary, output)
    except OSError:
        raise EvidenceError(
            "Baseline evidence could not be published to a new file"
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return report


def run_live(environment) -> dict:
    """The explicit live entry point: no fixture injection is possible here."""
    __tracebackhide__ = True
    config = settings(environment)
    report = capture(config, observer_for(config))
    unsatisfied = unsatisfied_criteria(report)
    require(
        not unsatisfied,
        "Baseline criteria are not satisfied by observation: " + ", ".join(unsatisfied),
    )
    return publish(config, report)


# Scenario-by-scenario expected evidence for #5067's evaluator. Kept beside the
# checks it describes so a scenario cannot be added without saying what would
# attest it.
EXPECTED_EVIDENCE: dict[str, tuple[str, ...]] = {
    "provider-selection-cheapest-first": (
        (
            "The provider option actually chosen, its cost, and the order "
            "offered, bound to the selected environment and revision."
        ),
    ),
    "skypilot-launch-and-stream": (
        (
            "The launch request as sent, the streamed progress lines in arrival "
            "order, and the terminal event that ended the stream."
        ),
    ),
    "autostop-and-spot-defaults": (
        (
            "The effective idle-autostop and disk defaults observed on the "
            "running cluster, not read from source."
        ),
    ),
    "eks-join-via-onboarding-scripts": (
        (
            "A Kubernetes Node in the workspace cluster reaching "
            "NodeReady=True, correlated to the provider instance."
        ),
        "Allocatable nvidia.com/gpu on that node matching the request.",
        "No SSM activation id or code present in any captured output.",
    ),
    "node-health-monitoring": (
        "The node record's resolved Kubernetes node name, or its absence.",
    ),
    "cost-aggregation-per-nodepool": (
        (
            "The hourly and daily figures the baseline reported, labelled "
            "estimate; no figure means unknown, not zero."
        ),
    ),
    "teardown-via-down-then-purge": (
        (
            "The teardown calls issued, and separately the provider reporting "
            "no running instance afterwards."
        ),
    ),
    SERVING_SCENARIO: (
        "Observed serving inventory establishing presence or absence.",
        "An authorized request answered on the declared port.",
        "An unauthenticated request refused.",
        (
            "Exactly one owning controller, and teardown removing every "
            "replica with provider-side confirmation."
        ),
    ),
}


def unmapped_scenarios() -> tuple[str, ...]:
    """Inventory scenarios with no expected-evidence mapping."""
    return tuple(sorted(KNOWN_SCENARIOS - EXPECTED_EVIDENCE.keys()))
