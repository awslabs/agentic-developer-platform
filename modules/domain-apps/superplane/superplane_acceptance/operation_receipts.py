"""Bounded read-only ingestion of the retained records of an authorized operation.

Issue #5289. This module exists because of a specific, correct objection to the
first version of the R17 capture: around a third of the behaviours R17 requires
cannot be seen by reading a system afterwards, and the observer reported every
one of them ``INDETERMINATE`` **unconditionally**, with no input that could ever
change that. A fully authorized operator with a real operation could therefore
never satisfy U12-L1. "We have no authorization to operate" does not justify
omitting the code that reads the evidence -- the operator *retains* the records
of the authorized operation, and the check has to be able to read them.

## What a retained record is, and what it is not

It is a file the authorized operation left behind: the provider options the
controller was offered at launch, the streamed progress and its terminal event,
what a cancellation did, what a teardown called. It is **not** a claim about
whether a check passed.

That distinction is the whole design. A record supplies *observations*; this
module derives the *outcome*. A record declaring "cheapest-first ordering:
satisfied" while listing one option, or two options priced high-then-low, is
refuted -- the declared verdict is never read, because a check that trusts a
verdict written beside the evidence is not checking anything.
:data:`_DERIVATIONS` is the entire set of rules, one per check, each reading only
observations.

## Why the observations live inside the attested body

The first version of this module got that distinction right and still accepted
forged evidence, because of *where* it read the observations from. A submission
carried a pointer to a body file plus a separate, hand-written ``facts`` object,
and only the hand-written object was ever read. The digest therefore covered
bytes nobody consulted. An offline reproduction supplied an arbitrary body,
labelled the submission ``controller.launch-decision``, and obtained SATISFIED
for cheapest-first ordering; reversing only the prices in the hand-written field
flipped the verdict to REFUTED **with a byte-identical reported digest**. The
same unread path decided whether the baseline serves anything, so a hand-written
empty listing could establish serving absence.

So the body *is* the evidence, and everything a verdict depends on is parsed out
of it: which check, which authority, which environment and deployed revision,
when it was observed, which resource, which run and attempt, and the observations
themselves. Editing an observation to change an outcome changes the bytes, which
changes their digest, which breaks authentication. The submission file beside it
is a pointer and nothing more, and a submission still carrying any of the old
top-level fields is refused by name so no caller keeps the old behaviour by
accident.

## Why a local digest cannot authenticate its own body

A digest only attests to something if it comes from somewhere other than the
material it describes. Recomputing a digest that the same local file declared
proves the two agree -- it does not establish who produced either. Authentication
is therefore asked of a **registered read-only producer client**: the producing
system is asked, through its own API, what digest it recorded for the identified
run and attempt. That answer must match the bytes on disk, and the run, attempt,
environment, deployed revision and resource handles it names must be the selected
ones.

Material no registered producer can vouch for is still read and retained -- a
malformed submission is refused either way -- but it is reported
``INDETERMINATE`` as unauthenticated diagnostics. It cannot satisfy a check, and
it cannot establish an absence. A producer that *contradicts* the submission is a
different matter and refuses the capture outright.

## What every record must survive before it is read at all

* **Authentication.** An independent producer's record of what it published,
  matched to the body's bytes, run and attempt. Without it nothing is
  established; against it, nothing forged survives.
* **Hash.** The digest is recomputed from the bytes on disk and compared both to
  the submission's declared value and to the producer's. A body edited after the
  fact is refused, not downgraded.
* **Time.** ``observed_at`` must be inside the operator's authorized execution
  window, so a record replayed from an earlier session cannot be presented as
  this operation's.
* **Target and revision.** The record's environment and deployed revision must be
  the selected ones. Evidence from a neighbouring environment is refused.
* **Resource.** The record's handles must intersect a machine this capture
  *itself* observed. A record about some other cluster cannot settle a check
  about this one.
* **Authority.** Each check names which authority may speak for it, so a record
  written by the tool that performed an operation cannot answer a question about
  that operation's effect on the provider.

Provider-side absence is deliberately **not** settleable from a retained record
at all. Whether a rented machine stopped existing is the one fact an operator's
own file must not be able to assert, so it is read live from a registered
read-only provider client instead -- see
:data:`~superplane_acceptance.live_observer.PROVIDER_READERS`. A retained teardown
record establishes that teardown was called; the provider establishes what
happened to the machine.

Nothing here operates. It opens files read-only inside a directory the operator
names, with a bounded file count and bounded sizes, and it has no network or
subprocess path at all.

## Why absent records still block

Supplying no directory is a supported run: every affected check stays
unsatisfied and the report names the exact record that is missing. The difference
from before is only that a real path now exists. This module never manufactures a
pass, and a record it cannot fully validate is refused rather than accepted with
a caveat.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .cli_delivery import EvidenceError, require
from .live_baseline import MAX_IDENTIFIER, Outcome, _safe, _safe_hash

# Bounds, because the directory is operator-supplied. A capture must fail on a
# directory that is too large to read rather than reading it anyway.
MAX_RECORD_FILES = 200
MAX_RECORD_BYTES = 256 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024

# The two record kinds. Anything else in the directory is refused by name rather
# than skipped, so a misspelled kind surfaces instead of silently contributing
# nothing. There is deliberately no "provider inventory" record: what the provider
# says is read live from a registered reader, never from a file.
CHECK_EVIDENCE = "check_evidence"
SERVICE_INVENTORY = "service_inventory"
RECORD_KINDS = (CHECK_EVIDENCE, SERVICE_INVENTORY)

# Authorities, named for the system that produced the record. A check may only be
# evidenced by an authority able to speak for it: the SkyPilot API server can
# report what it streamed, and it cannot report whether a rented machine stopped
# billing.
CONTROLLER_LAUNCH = "controller.launch-decision"
CONTROLLER_ONBOARDING = "controller.onboarding"
CONTROLLER_CANCELLATION = "controller.cancellation"
CONTROLLER_RESTART = "controller.restart"
CONTROLLER_SPEND = "controller.spend-observation"
SKYPILOT_STREAM = "skypilot.api-stream"
SKYPILOT_STATE = "skypilot.api-state"
SKYPILOT_TEARDOWN = "skypilot.teardown"
SKYSERVE_STATUS = "skyserve.status"
SKYSERVE_PROBE = "skyserve.probe"
SKYSERVE_TEARDOWN = "skyserve.teardown"

# Closed set: an unrecognized authority is refused by name. Leaving it open would
# let a record invent an authority and so choose which check it may answer.
AUTHORITIES = (
    CONTROLLER_LAUNCH,
    CONTROLLER_ONBOARDING,
    CONTROLLER_CANCELLATION,
    CONTROLLER_RESTART,
    CONTROLLER_SPEND,
    SKYPILOT_STREAM,
    SKYPILOT_STATE,
    SKYPILOT_TEARDOWN,
    SKYSERVE_STATUS,
    SKYSERVE_PROBE,
    SKYSERVE_TEARDOWN,
)

_HANDLE_FIELDS = ("provider_resource_id", "kubernetes_node", "skypilot_cluster")
_BODY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")

# A submission is a pointer, so these are the only keys it may carry. Anything
# describing the observation itself now lives inside the attested body, and a
# submission still carrying one of the old fields is refused by name rather than
# ignored -- silently dropping it would leave a caller believing hand-written
# facts were honoured.
_SUBMISSION_KEYS = frozenset({"record_kind", "body", "body_sha256", "producer"})
_MOVED_KEYS = (
    "facts",
    "services",
    "check_id",
    "authority",
    "resource",
    "environment",
    "deployed_revision",
    "observed_at",
    "run_id",
    "attempt",
)

# Registered read-only producer clients able to authenticate an evidence body.
# Keyed by the lane name a submission points at. Empty by default for the same
# reason BASELINE_TARGETS is: shipping a client is engineering, whereas deciding
# which execution lane is authoritative for a baseline is authorization and stays
# the supervisor's. With none registered, every submission is unauthenticated
# diagnostics and no check can be satisfied from a record.
EVIDENCE_PRODUCERS: dict[str, object] = {}


def register_evidence_producer(lane: str, producer: object) -> None:
    """Record a reviewed read-only producer client for one execution lane."""
    __tracebackhide__ = True
    require(
        lane == lane.lower().strip() and bool(lane),
        "an evidence producer is registered under the lane's lowercase name",
    )
    EVIDENCE_PRODUCERS[lane] = producer


@dataclass(frozen=True)
class _Attestation:
    """An independent producer's confirmation of one operation's evidence set.

    ``authenticated`` false means no registered producer could vouch for the set,
    which is a supported outcome: the material is retained as diagnostics and
    every check it touches stays indeterminate. A producer that actively disagrees
    does not land here -- that refuses the capture.
    """

    authenticated: bool
    detail: str
    lane: str = ""


def _manifest_digest(bodies: list[tuple[str, str]]) -> str:
    """The digest an operation's whole retained evidence set hashes to.

    Authentication is of the **set**, not of each body in isolation, and that is
    load-bearing rather than an implementation convenience. A producer records one
    digest per run attempt, so per-body questions have no answer it could give; but
    more importantly, per-body authentication would leave the *composition* of the
    directory unattested. An attacker who could not alter any single body could
    still add a forged empty ``sky serve status`` listing beside the genuine ones,
    or withhold the record that would have refuted a check, and every remaining
    file would still authenticate perfectly.

    Hashing the sorted ``name:digest`` lines makes addition, removal and
    substitution all changes to the one value the producer vouches for. The same
    reasoning as ``_Reference.combined``: evidence assembled from several reads is
    pinned by a digest over all of them, and the order is fixed by sorting rather
    than by directory iteration order.

    ``bodies`` is keyed on the evidence **body** filenames, because this value has
    to be computable by the lane that publishes them. The producer's own copy of
    the set arrives as an artifact archive, and
    :func:`live_observer.manifest_of_archive` derives this same value from that
    archive's members using this same function -- so one authority vouches for the
    archive's bytes and the archive yields the value the retained set must hash to.
    Keying on the operator's submission filenames instead left the attested value
    uncomputable by the only party able to attest it.
    """
    __tracebackhide__ = True
    require(bool(bodies), "an evidence manifest needs at least one body")
    lines = "\n".join(f"{name}:{digest}" for name, digest in sorted(bodies))
    return hashlib.sha256(lines.encode()).hexdigest()


@dataclass(frozen=True)
class ReceiptEvidence:
    """One derived outcome, with the retained record it was derived from.

    The reference and digest name the copy this run retained, not the operator's
    original: a finding has to stay re-derivable from the run's own evidence
    directory after the operator's working files are gone.
    """

    check_id: str
    outcome: Outcome
    detail: str
    evidence_reference: str
    evidence_sha256: str
    observed_at: datetime
    handles: tuple[str, ...]
    # Whether an independent producer vouched for the body these observations were
    # parsed from. False means the outcome above is diagnostics only: the observer
    # downgrades it to indeterminate rather than letting unvouched material settle
    # a check.
    authenticated: bool = False
    # Set for the two cleanup checks whose satisfaction a record may claim but
    # only the provider can establish. The observer must pair these with a live
    # provider read before publishing anything but indeterminate.
    needs_provider_confirmation: bool = False
    # The provider instance the record says was released, so the observer knows
    # what to ask the provider about.
    instance: str = ""


@dataclass(frozen=True)
class ServiceInventory:
    """The authoritative service listing, as retained from an authorized client.

    Serving has no controller-side inventory at all -- U12's own existing-state
    inventory records that, which is why the previous implementation's scan of
    cluster names could never establish serving absence. ``sky serve status`` run
    by an authorized client is the authority, and this is its retained output.
    """

    services: tuple[str, ...]
    controllers: tuple[str, ...]
    endpoints: tuple[str, ...]
    observed_at: datetime
    evidence_reference: str
    evidence_sha256: str
    enumerated_via: str
    # An unauthenticated listing cannot establish that no service is running: a
    # forged empty listing is exactly how serving absence was reachable without
    # evidence. ``live_baseline`` requires this before the absence branch.
    authenticated: bool = False


# ---------------------------------------------------------------------------
# Outcome derivation. One rule per check, reading facts only.
#
# Each returns (Outcome, detail). Raising EvidenceError means the record is not
# evidence for this check at all -- a distinct answer from "the behavior was
# refuted", and it refuses the capture rather than recording a wrong finding.
# ---------------------------------------------------------------------------


def _number(facts: dict, key: str, label: str) -> float:
    __tracebackhide__ = True
    value = facts.get(key)
    require(
        isinstance(value, int | float) and not isinstance(value, bool),
        f"{label}: fact {key!r} must be a number",
    )
    assert isinstance(value, int | float)
    return float(value)


def _integer(facts: dict, key: str, label: str) -> int:
    __tracebackhide__ = True
    value = facts.get(key)
    require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{label}: fact {key!r} must be a whole number",
    )
    assert isinstance(value, int)
    return value


def _flag(facts: dict, key: str, label: str) -> bool:
    __tracebackhide__ = True
    value = facts.get(key)
    require(isinstance(value, bool), f"{label}: fact {key!r} must be true or false")
    assert isinstance(value, bool)
    return value


def _words(facts: dict, key: str, label: str) -> str:
    __tracebackhide__ = True
    value = facts.get(key)
    require(
        isinstance(value, str) and value.strip() != "",
        f"{label}: fact {key!r} must be a non-empty string",
    )
    assert isinstance(value, str)
    return _safe(value.strip(), f"{label}: {key}", MAX_IDENTIFIER)


def _entries(facts: dict, key: str, label: str, minimum: int) -> list[dict]:
    __tracebackhide__ = True
    value = facts.get(key)
    require(
        isinstance(value, list) and all(isinstance(item, dict) for item in value),
        f"{label}: fact {key!r} must be a list of objects",
    )
    assert isinstance(value, list)
    require(
        len(value) >= minimum,
        f"{label}: fact {key!r} needs at least {minimum} entries to evidence this "
        "check",
    )
    return value


def _names(facts: dict, key: str, label: str) -> list[str]:
    __tracebackhide__ = True
    value = facts.get(key)
    require(
        isinstance(value, list) and all(isinstance(item, str) for item in value),
        f"{label}: fact {key!r} must be a list of handles",
    )
    assert isinstance(value, list)
    return [_safe(item, f"{label}: {key} entry", MAX_IDENTIFIER) for item in value]


def _derive_ordering(facts: dict, label: str) -> tuple[Outcome, str]:
    """Cheapest-first ordering, from the options the launch was actually offered.

    Two options is the minimum that can evidence an *order*; a single-option
    record is refused rather than passed, which is the case the second review
    would otherwise have had accepted as satisfied.
    """
    __tracebackhide__ = True
    options = _entries(facts, "offered_options", label, minimum=2)
    prices = []
    for index, option in enumerate(options):
        prices.append(_number(option, "hourly_price", f"{label}: option {index}"))
        _words(option, "provider", f"{label}: option {index}")
    ordered = all(earlier <= later for earlier, later in itertools.pairwise(prices))
    rendered = ", ".join(f"{price:g}" for price in prices)
    return (
        Outcome.SATISFIED if ordered else Outcome.REFUTED,
        f"Launch was offered {len(prices)} options priced {rendered}; "
        + ("cheapest first" if ordered else "not in cheapest-first order"),
    )


def _derive_fallback(facts: dict, label: str) -> tuple[Outcome, str]:
    """Fallback, from the attempt sequence of a launch whose first option failed.

    A record whose first attempt succeeded describes a launch that never needed to
    fall back, so it cannot evidence this check either way and is refused.
    """
    __tracebackhide__ = True
    attempts = _entries(facts, "attempts", label, minimum=2)
    results = []
    for index, attempt in enumerate(attempts):
        _words(attempt, "provider", f"{label}: attempt {index}")
        result = _words(attempt, "result", f"{label}: attempt {index}")
        require(
            result in ("failed", "succeeded"),
            f"{label}: attempt {index} result must be 'failed' or 'succeeded'",
        )
        results.append(result)
    require(
        results[0] == "failed",
        f"{label}: this check needs a launch whose first provider option failed; "
        "the recorded launch succeeded on its first attempt and evidences nothing "
        "about fallback",
    )
    recovered = "succeeded" in results[1:]
    return (
        Outcome.SATISFIED if recovered else Outcome.REFUTED,
        f"After the first option failed, {len(results) - 1} further option(s) were "
        "attempted and one succeeded"
        if recovered
        else f"All {len(results)} options failed; the launch did not fall back "
        "to a working provider",
    )


def _derive_cni(facts: dict, label: str) -> tuple[Outcome, str]:
    """The CNI prerequisite, which this check asks to have been *recorded*."""
    __tracebackhide__ = True
    prerequisite = _words(facts, "cni_prerequisite", label)
    return (
        Outcome.SATISFIED,
        f"Onboarding recorded the CNI prerequisite: {prerequisite}",
    )


def _derive_progress(facts: dict, label: str) -> tuple[Outcome, str]:
    __tracebackhide__ = True
    count = _integer(facts, "progress_line_count", label)
    require(count >= 0, f"{label}: progress_line_count cannot be negative")
    return (
        Outcome.SATISFIED if count > 0 else Outcome.REFUTED,
        f"The launch stream carried {count} progress line(s)",
    )


def _derive_terminal(facts: dict, label: str) -> tuple[Outcome, str]:
    """The terminal stream event, against the maintained client's own constants.

    ``complete`` and ``error`` are the two terminal event types in
    ``src/superplane-controller/skypilot/types.go``; anything else is not a
    terminal event and the record is refused. What this check establishes is that
    a terminal event *ended* the stream, so a record showing events after it is a
    refutation rather than a pass.
    """
    __tracebackhide__ = True
    event = _words(facts, "terminal_event", label)
    require(
        event in ("complete", "error"),
        f"{label}: terminal_event must be one of the maintained client's terminal "
        "event types, 'complete' or 'error'",
    )
    trailing = _integer(facts, "events_after_terminal", label)
    require(trailing >= 0, f"{label}: events_after_terminal cannot be negative")
    return (
        Outcome.SATISFIED if trailing == 0 else Outcome.REFUTED,
        f"The stream ended on a {event} event"
        if trailing == 0
        else f"{trailing} event(s) followed the {event} event, so it did not end "
        "the stream",
    )


def _derive_cancel_stops(facts: dict, label: str) -> tuple[Outcome, str]:
    __tracebackhide__ = True
    _words(facts, "request_id", label)
    cancelled = _flag(facts, "cancelled", label)
    completed = _flag(facts, "launch_completed", label)
    satisfied = cancelled and not completed
    return (
        Outcome.SATISFIED if satisfied else Outcome.REFUTED,
        "The in-flight launch was cancelled and did not go on to complete"
        if satisfied
        else f"Cancellation recorded cancelled={cancelled}, "
        f"launch_completed={completed}",
    )


def _derive_cancel_deadline(facts: dict, label: str) -> tuple[Outcome, str]:
    __tracebackhide__ = True
    deadline = _number(facts, "deadline_seconds", label)
    elapsed = _number(facts, "elapsed_seconds", label)
    require(deadline > 0, f"{label}: deadline_seconds must be positive")
    require(elapsed >= 0, f"{label}: elapsed_seconds cannot be negative")
    satisfied = elapsed <= deadline
    return (
        Outcome.SATISFIED if satisfied else Outcome.REFUTED,
        f"Cancellation finished in {elapsed:g}s against a {deadline:g}s deadline"
        if satisfied
        else f"Cancellation took {elapsed:g}s, past its {deadline:g}s deadline",
    )


def _derive_restart(facts: dict, label: str) -> tuple[Outcome, str]:
    __tracebackhide__ = True
    _words(facts, "request_id", label)
    restarts = _integer(facts, "restarts", label)
    require(
        restarts >= 1,
        f"{label}: this check needs a record in which the controller actually "
        "restarted",
    )
    before = _names(facts, "clusters_before", label)
    after = _names(facts, "clusters_after", label)
    satisfied = sorted(before) == sorted(after)
    return (
        Outcome.SATISFIED if satisfied else Outcome.REFUTED,
        f"Across {restarts} restart(s) the same {len(before)} cluster(s) were "
        "tracked, so the operation resumed rather than duplicating"
        if satisfied
        else f"{len(before)} cluster(s) before the restart became {len(after)} "
        "after, so the restart did not resume the same operation",
    )


def _derive_state_survives(facts: dict, label: str) -> tuple[Outcome, str]:
    __tracebackhide__ = True
    require(
        _flag(facts, "redeployed", label),
        f"{label}: this check needs a record of an API server that was actually "
        "redeployed",
    )
    before = _names(facts, "clusters_before", label)
    after = _names(facts, "clusters_after", label)
    require(
        bool(before),
        f"{label}: a redeploy with no clusters beforehand cannot establish that "
        "handles survive it",
    )
    lost = sorted(set(before) - set(after))
    return (
        Outcome.SATISFIED if not lost else Outcome.REFUTED,
        f"All {len(before)} cluster handle(s) survived the API server redeploy"
        if not lost
        else f"{len(lost)} cluster handle(s) were lost across the redeploy, "
        "orphaning running capacity",
    )


def _derive_spend_continues(facts: dict, label: str) -> tuple[Outcome, str]:
    """That cost observation is *not* a spend control, as the baseline behaves.

    The check records a limitation, so the satisfying observation is spend
    continuing past the configured ceiling. A record showing spend stopping would
    mean the baseline does enforce a control, which contradicts the check and is
    reported as a refutation rather than quietly reinterpreted.
    """
    __tracebackhide__ = True
    ceiling = _number(facts, "configured_ceiling", label)
    observed = _number(facts, "observed_spend", label)
    require(ceiling > 0, f"{label}: configured_ceiling must be positive")
    continued = _flag(facts, "operation_continued", label)
    satisfied = observed > ceiling and continued
    return (
        Outcome.SATISFIED if satisfied else Outcome.REFUTED,
        f"Spend reached {observed:g} against a configured ceiling of {ceiling:g} "
        "and the operation continued: observation is not a control"
        if satisfied
        else f"Spend {observed:g} against ceiling {ceiling:g} with "
        f"operation_continued={continued} does not establish this limitation",
    )


def _derive_teardown_fallback(facts: dict, label: str) -> tuple[Outcome, str]:
    __tracebackhide__ = True
    calls = _entries(facts, "calls", label, minimum=1)
    sequence = []
    for index, call in enumerate(calls):
        name = _words(call, "call", f"{label}: call {index}")
        result = _words(call, "result", f"{label}: call {index}")
        require(
            name in ("down", "purge"),
            f"{label}: call {index} must be 'down' or 'purge'",
        )
        require(
            result in ("failed", "succeeded"),
            f"{label}: call {index} result must be 'failed' or 'succeeded'",
        )
        sequence.append((name, result))
    require(
        sequence[0][0] == "down",
        f"{label}: teardown is recorded as down first, then purge as the fallback",
    )
    satisfied = any(result == "succeeded" for _, result in sequence)
    rendered = ", ".join(f"{name}={result}" for name, result in sequence)
    return (
        Outcome.SATISFIED if satisfied else Outcome.REFUTED,
        f"Teardown sequence {rendered} reached a successful call"
        if satisfied
        else f"Teardown sequence {rendered} never succeeded",
    )


def _derive_teardown_instance(facts: dict, label: str) -> tuple[Outcome, str]:
    """Which instance a completed teardown claims to have released.

    The record names the instance and says the teardown succeeded. That is the
    claim; the registered provider reader supplies the verdict. Keeping the two
    apart is the point of this check: the previous version of the baseline could
    have recorded "SkyPilot down succeeded" as cleanup verified, which is exactly
    the case where a rented machine keeps billing.
    """
    __tracebackhide__ = True
    instance = _words(facts, "instance", label)
    require(
        _flag(facts, "teardown_succeeded", label),
        f"{label}: a teardown that did not succeed cannot begin to establish "
        "provider-side absence",
    )
    return (
        Outcome.SATISFIED,
        (
            f"Teardown reported releasing instance {instance}, pending independent "
            "provider confirmation"
        ),
    )


def _derive_endpoint_reachable(facts: dict, label: str) -> tuple[Outcome, str]:
    __tracebackhide__ = True
    require(
        _flag(facts, "authenticated", label),
        f"{label}: reachability is evidenced by an authenticated request; an "
        "unauthenticated one evidences the refusal check instead",
    )
    status = _integer(facts, "status_code", label)
    satisfied = 200 <= status < 300
    return (
        Outcome.SATISFIED if satisfied else Outcome.REFUTED,
        f"An authenticated request to the service returned {status}",
    )


def _derive_unauthenticated_refused(facts: dict, label: str) -> tuple[Outcome, str]:
    __tracebackhide__ = True
    require(
        not _flag(facts, "authenticated", label),
        f"{label}: this check needs a request made without credentials",
    )
    status = _integer(facts, "status_code", label)
    satisfied = status in (401, 403)
    return (
        Outcome.SATISFIED if satisfied else Outcome.REFUTED,
        f"An unauthenticated request returned {status}"
        + ("" if satisfied else ", which is not a refusal"),
    )


def _derive_owning_controller(facts: dict, label: str) -> tuple[Outcome, str]:
    __tracebackhide__ = True
    controller = _words(facts, "controller", label)
    return (
        Outcome.SATISFIED,
        f"The service listing names its owning controller: {controller}",
    )


def _derive_serving_teardown(facts: dict, label: str) -> tuple[Outcome, str]:
    __tracebackhide__ = True
    before = _integer(facts, "replicas_before", label)
    after = _integer(facts, "replicas_after", label)
    require(
        before >= 1,
        f"{label}: a teardown of a service with no replicas establishes nothing",
    )
    require(after >= 0, f"{label}: replicas_after cannot be negative")
    satisfied = after == 0
    return (
        Outcome.SATISFIED if satisfied else Outcome.REFUTED,
        f"Teardown took the service from {before} replica(s) to none"
        if satisfied
        else f"{after} of {before} replica(s) remained after teardown",
    )


def _derive_cancel_capacity(facts: dict, label: str) -> tuple[Outcome, str]:
    """That the cancelled launch's provider resource was released.

    Retained here only as the *claim* side: the record says which instance the
    cancelled launch held and that the controller released it. Whether the
    provider agrees is checked live against the registered provider reader, so a
    record alone cannot satisfy this. The observer refuses to publish satisfaction
    without that independent confirmation.
    """
    __tracebackhide__ = True
    instance = _words(facts, "instance", label)
    released = _flag(facts, "release_requested", label)
    require(
        released,
        f"{label}: this check needs a record in which the cancelled launch's "
        "capacity was actually released",
    )
    return (
        Outcome.SATISFIED,
        (
            f"The cancellation released instance {instance}, pending independent "
            "provider confirmation"
        ),
    )


@dataclass(frozen=True)
class _Rule:
    """Which authority may evidence a check, and how its facts become an outcome."""

    authorities: tuple[str, ...]
    derive: object
    # Satisfaction additionally needs the registered provider reader to confirm the
    # instance is gone. A retained record may claim a release; only the provider
    # can establish that capacity stopped being billed.
    needs_provider_confirmation: bool = False


# The complete set of checks a retained record may settle, and nothing else. A
# record naming a check outside this table is refused: the observable-now checks
# are established by reading the live system, and accepting a record for one of
# them would let a file override what the environment actually reported.
_DERIVATIONS: dict[str, _Rule] = {
    "provider.ordering-cheapest-first": _Rule((CONTROLLER_LAUNCH,), _derive_ordering),
    "provider.fallback-on-launch-failure": _Rule(
        (CONTROLLER_LAUNCH,), _derive_fallback
    ),
    "node.cni-prerequisite-recorded": _Rule((CONTROLLER_ONBOARDING,), _derive_cni),
    "status.progress-lines-streamed": _Rule((SKYPILOT_STREAM,), _derive_progress),
    "status.terminal-event-ends-stream": _Rule((SKYPILOT_STREAM,), _derive_terminal),
    "cancel.in-flight-launch-stops": _Rule(
        (CONTROLLER_CANCELLATION,), _derive_cancel_stops
    ),
    "cancel.timeout-bounded": _Rule(
        (CONTROLLER_CANCELLATION,), _derive_cancel_deadline
    ),
    "cancel.cancelled-launch-releases-capacity": _Rule(
        (CONTROLLER_CANCELLATION,),
        _derive_cancel_capacity,
        needs_provider_confirmation=True,
    ),
    "lifecycle.restart-resumes-not-duplicates": _Rule(
        (CONTROLLER_RESTART,), _derive_restart
    ),
    "lifecycle.api-state-store-survives-redeploy": _Rule(
        (SKYPILOT_STATE,), _derive_state_survives
    ),
    "cost.observation-not-a-spend-control": _Rule(
        (CONTROLLER_SPEND,), _derive_spend_continues
    ),
    "cleanup.down-then-purge-fallback": _Rule(
        (SKYPILOT_TEARDOWN,), _derive_teardown_fallback
    ),
    "cleanup.provider-side-absence-verified": _Rule(
        (SKYPILOT_TEARDOWN,),
        _derive_teardown_instance,
        needs_provider_confirmation=True,
    ),
    "serving.endpoint-reachable": _Rule((SKYSERVE_PROBE,), _derive_endpoint_reachable),
    "serving.unauthenticated-request-refused": _Rule(
        (SKYSERVE_PROBE,), _derive_unauthenticated_refused
    ),
    "serving.owning-controller-identified": _Rule(
        (SKYSERVE_STATUS,), _derive_owning_controller
    ),
    "serving.teardown-removes-replicas": _Rule(
        (SKYSERVE_TEARDOWN,), _derive_serving_teardown
    ),
}

SETTLEABLE_CHECKS = frozenset(_DERIVATIONS)

# What is missing when a check has no record, phrased as the input to supply.
# Named per check so a blocked run lists exactly what to retain rather than
# "evidence".
MISSING_RECORD: dict[str, str] = {
    "provider.ordering-cheapest-first": (
        "a retained controller.launch-decision record listing the provider options "
        "and prices the launch was offered"
    ),
    "provider.fallback-on-launch-failure": (
        "a retained controller.launch-decision record of a launch whose first "
        "provider option failed"
    ),
    "node.cni-prerequisite-recorded": (
        "a retained controller.onboarding record naming the CNI prerequisite"
    ),
    "status.progress-lines-streamed": (
        "a retained skypilot.api-stream record of the launch's progress stream"
    ),
    "status.terminal-event-ends-stream": (
        "a retained skypilot.api-stream record of the launch's terminal event"
    ),
    "cancel.in-flight-launch-stops": (
        "a retained controller.cancellation record for an in-flight launch"
    ),
    "cancel.timeout-bounded": (
        "a retained controller.cancellation record carrying that cancellation's "
        "deadline and elapsed time"
    ),
    "cancel.cancelled-launch-releases-capacity": (
        "a retained controller.cancellation record naming the instance the "
        "cancelled launch held, plus a registered provider reader to confirm it is "
        "gone"
    ),
    "lifecycle.restart-resumes-not-duplicates": (
        "a retained controller.restart record with the tracked clusters before and "
        "after the restart"
    ),
    "lifecycle.api-state-store-survives-redeploy": (
        "a retained skypilot.api-state record with the cluster list read before and "
        "after an API server redeploy"
    ),
    "cost.observation-not-a-spend-control": (
        "a retained controller.spend-observation record of spend against its "
        "configured ceiling"
    ),
    "cleanup.down-then-purge-fallback": (
        "a retained skypilot.teardown record of the down and purge calls and their "
        "outcomes"
    ),
    "cleanup.provider-side-absence-verified": (
        "a retained skypilot.teardown record naming the released instance, plus a "
        "registered read-only provider reader to independently confirm it is gone; "
        "a successful down or purge is not this"
    ),
    "serving.endpoint-reachable": (
        "a retained skyserve.probe record of an authenticated request to the "
        "service's port"
    ),
    "serving.unauthenticated-request-refused": (
        "a retained skyserve.probe record of an unauthenticated request to the "
        "same port"
    ),
    "serving.owning-controller-identified": (
        "a retained skyserve.status record naming the service's owning controller"
    ),
    "serving.teardown-removes-replicas": (
        "a retained skyserve.teardown record with the replica count before and after"
    ),
}


class OperationLedger:
    """Every accepted record from the operator's retained directory.

    Built once per capture. Construction reads and validates; nothing is read
    lazily, so a malformed directory blocks the run at a single point instead of
    surfacing halfway through a dimension.
    """

    def __init__(
        self,
        *,
        evidence: dict[str, list[ReceiptEvidence]],
        services: ServiceInventory | None,
    ) -> None:
        self._evidence = evidence
        self._services = services

    # -- queries used by the observer ---------------------------------------

    def evidence_for(
        self, check_id: str, handles: tuple[str, ...]
    ) -> tuple[ReceiptEvidence, ...]:
        """Accepted records for one check that concern one of these handles.

        Handle intersection rather than equality: a record may identify the
        machine by its SkyPilot cluster while the capture identifies it by its
        Kubernetes node, and both name the same machine.
        """
        wanted = frozenset(handles)
        return tuple(
            item
            for item in self._evidence.get(check_id, ())
            if wanted & frozenset(item.handles)
        )

    @property
    def service_inventory(self) -> ServiceInventory | None:
        return self._services


EMPTY_LEDGER = OperationLedger(evidence={}, services=None)


def _record_files(directory: Path) -> list[Path]:
    __tracebackhide__ = True
    try:
        entries = sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.name.endswith(".json")
        )
    except OSError:
        raise EvidenceError(
            "BLOCKED: the retained-record directory could not be listed"
        ) from None
    require(
        len(entries) <= MAX_RECORD_FILES,
        f"BLOCKED: the retained-record directory holds more than "
        f"{MAX_RECORD_FILES} records; narrow it to the authorized operation's own",
    )
    return entries


def _load_json(path: Path) -> dict:
    __tracebackhide__ = True
    try:
        raw = path.read_bytes()
    except OSError:
        raise EvidenceError(
            f"BLOCKED: retained record {path.name} could not be read"
        ) from None
    require(
        len(raw) <= MAX_RECORD_BYTES,
        f"retained record {path.name} exceeds the {MAX_RECORD_BYTES}-byte limit",
    )
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        # The body is withheld from the message: a malformed record may still
        # contain whatever the operation wrote into it.
        raise EvidenceError(
            f"retained record {path.name} is not readable JSON; body withheld"
        ) from None
    require(
        isinstance(payload, dict),
        f"retained record {path.name} must be a JSON object",
    )
    assert isinstance(payload, dict)
    return payload


def _verified_body(
    directory: Path, payload: dict, label: str, retain
) -> tuple[str, str, dict, str]:
    """Read the evidence body, retain a copy, and return its parsed content.

    The body is the evidence, so this returns the parsed object every observation
    is read from -- not merely a digest. The declared digest is recomputed from the
    bytes on disk, which catches a body edited after its submission was written;
    it deliberately establishes nothing about *who* produced either, which is
    :func:`_attest`'s job.

    Retaining the copy is what keeps a finding re-derivable once the operator's
    working directory is gone.
    """
    __tracebackhide__ = True
    name = payload.get("body")
    require(
        isinstance(name, str) and _BODY_NAME.fullmatch(name) is not None,
        f"{label}: 'body' must name a plain file beside the submission",
    )
    assert isinstance(name, str)
    declared = _safe_hash(payload.get("body_sha256"), f"{label}: body_sha256")
    path = directory / name
    require(
        path.is_file() and not path.is_symlink(),
        f"{label}: the named body file is not a regular file beside the submission",
    )
    try:
        size = path.stat().st_size
        require(
            size <= MAX_BODY_BYTES,
            f"{label}: the evidence body exceeds the {MAX_BODY_BYTES}-byte limit",
        )
        content = path.read_bytes()
    except OSError:
        raise EvidenceError(f"{label}: the evidence body could not be read") from None
    computed = hashlib.sha256(content).hexdigest()
    require(
        computed == declared,
        f"{label}: the evidence body does not match its declared sha256, so the "
        "submission no longer describes its own evidence",
    )
    try:
        body = json.loads(content)
    except (ValueError, UnicodeError, RecursionError):
        # Withheld: the body is the operation's own output and may carry anything.
        raise EvidenceError(
            f"{label}: the evidence body is not readable JSON; body withheld"
        ) from None
    require(
        isinstance(body, dict),
        f"{label}: the evidence body must be a JSON object",
    )
    assert isinstance(body, dict)
    reference, digest = retain(_retention_label(name), content)
    return reference, digest, body, computed


def _attest(
    lane: str, run: str, attempt: int, manifest: str, revision: str
) -> _Attestation:
    """Ask an independent producer to vouch for this operation's evidence set.

    The submissions name the lane that produced the evidence; the registered
    read-only client for that lane is asked what digest it recorded for the run and
    attempt the *bodies* identify. ``revision`` is the deployed revision under
    verification: the producer must confirm the attempt it reports actually executed
    that commit, because a re-run dispatched against another head is a different
    execution whose evidence is not evidence for this one. Three distinct answers:

    * **No registered producer for the lane.** Unauthenticated. The material is
      retained as diagnostics and every check it touches stays indeterminate.
      This is the default state, because :data:`EVIDENCE_PRODUCERS` ships empty.
    * **Producer agrees.** Authenticated, and only then may a check be satisfied.
    * **Producer disagrees.** That is a contradiction between two sources rather
      than a gap, so it refuses the capture instead of downgrading quietly.

    A read that fails is the producer client's own business to raise: treating an
    outage as "no record" would silently turn a reachability problem into a
    permanently unauthenticated capture.
    """
    __tracebackhide__ = True
    producer = EVIDENCE_PRODUCERS.get(lane)
    if producer is None:
        return _Attestation(
            authenticated=False,
            detail=(
                f"No reviewed read-only producer client is registered for lane "
                f"{lane}, so this evidence is unauthenticated diagnostics and "
                "cannot establish its check."
            ),
            lane=lane,
        )
    published = producer.published_digest(run=run, attempt=attempt, revision=revision)
    require(
        isinstance(published, str),
        f"a producer client answers with a digest string; lane {lane} did not",
    )
    assert isinstance(published, str)
    if published.strip() == "":
        # A gap, not a contradiction: the lane simply has no record of publishing
        # evidence for this attempt (or it has expired beyond retrieval).
        return _Attestation(
            authenticated=False,
            detail=(
                f"Lane {lane} has no record of publishing evidence for run {run} "
                f"attempt {attempt}, so this evidence is unauthenticated "
                "diagnostics; an absent producer record does not authenticate."
            ),
            lane=lane,
        )
    require(
        _safe_hash(published.strip(), f"lane {lane} producer digest") == manifest,
        f"the producer for lane {lane} recorded a different evidence digest for run "
        f"{run} attempt {attempt} than the retained evidence hashes to, so the two "
        "sources contradict each other; the retained set has been added to, removed "
        "from or edited since the lane published it",
    )
    return _Attestation(
        authenticated=True,
        detail=(
            f"Lane {lane} independently confirms this evidence set's digest for run "
            f"{run} attempt {attempt}."
        ),
        lane=lane,
    )


def _producer_lane(payload: dict, label: str) -> str:
    __tracebackhide__ = True
    lane = payload.get("producer")
    require(
        isinstance(lane, str) and lane.strip() != "",
        f"{label}: 'producer' must name the execution lane that produced this "
        "evidence, so an independent record of it can be asked for",
    )
    assert isinstance(lane, str)
    return _safe(lane.strip(), f"{label}: producer", MAX_IDENTIFIER)


def _retention_label(name: str) -> str:
    """A retention label from a body filename, in the store's own vocabulary."""
    lowered = re.sub(r"[^a-z0-9.-]+", "-", name.lower()).strip("-")
    return f"receipt-{lowered}"[:60] or "receipt-body"


def _instant(payload: dict, label: str) -> datetime:
    __tracebackhide__ = True
    value = payload.get("observed_at")
    require(isinstance(value, str), f"{label}: observed_at must be an ISO-8601 string")
    assert isinstance(value, str)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise EvidenceError(
            f"{label}: observed_at is not an ISO-8601 instant"
        ) from None
    require(
        moment.tzinfo is not None,
        f"{label}: observed_at needs an explicit UTC offset",
    )
    return moment


def _handles(payload: dict, label: str) -> tuple[str, ...]:
    __tracebackhide__ = True
    resource = payload.get("resource")
    require(isinstance(resource, dict), f"{label}: 'resource' must be an object")
    assert isinstance(resource, dict)
    unknown = sorted(set(resource) - set(_HANDLE_FIELDS))
    require(
        not unknown,
        f"{label}: resource may name only "
        + ", ".join(_HANDLE_FIELDS)
        + "; got "
        + ", ".join(unknown),
    )
    handles = tuple(
        _safe(resource[field], f"{label}: resource {field}", MAX_IDENTIFIER)
        for field in _HANDLE_FIELDS
        if resource.get(field)
    )
    require(
        bool(handles),
        f"{label}: a record must identify the resource it concerns by at least one "
        "handle",
    )
    return handles


def load_ledger(
    config: dict,
    *,
    window,
    retain,
    observed_handles: frozenset[str],
) -> OperationLedger:
    """Read and validate the operator's retained records, or return an empty ledger.

    ``observed_handles`` are the correlation handles of the machines this capture
    saw for itself. A record naming none of them is refused: it concerns a
    resource this capture has no independent knowledge of, which is precisely the
    foreign-evidence case the criteria model exists to exclude.

    Supplying no directory returns :data:`EMPTY_LEDGER`, which is a supported run:
    every affected check then stays unsatisfied and names its missing record.
    """
    __tracebackhide__ = True
    directory = str(config.get("receipts_dir", "")).strip()
    if not directory:
        return EMPTY_LEDGER
    root = Path(directory)
    require(
        root.is_dir() and not root.is_symlink(),
        "BLOCKED: the retained-record directory is not a readable directory",
    )
    # Three passes. Every body is read and screened first, because the set's
    # manifest digest -- the thing the producer actually vouches for -- is not known
    # until the last file has been read. Then the set is authenticated once. Only
    # then are outcomes derived, because which handles a serving record may name
    # depends on the service listing, and file order says nothing about which
    # record that is.
    screened: list[tuple] = []
    bodies: list[tuple[str, str]] = []
    lanes: set[str] = set()
    operations: set[tuple[str, int]] = set()
    for path in _record_files(root):
        label = f"retained record {path.name}"
        payload = _load_json(path)
        kind = payload.get("record_kind")
        require(
            kind in RECORD_KINDS,
            f"{label}: 'record_kind' must be one of " + ", ".join(RECORD_KINDS),
        )
        # A submission is a pointer. Any field describing the observation itself
        # now belongs inside the attested body, and carrying one here is refused
        # by name: that hand-written field is precisely what an offline
        # reproduction used to drive a verdict past a byte-identical digest.
        stale = sorted(set(payload) & set(_MOVED_KEYS))
        require(
            not stale,
            f"{label}: "
            + ", ".join(stale)
            + " must be inside the attested evidence body, not beside it; a field "
            "the digest does not cover cannot decide an outcome",
        )
        unknown = sorted(set(payload) - _SUBMISSION_KEYS)
        require(
            not unknown,
            f"{label}: a submission may carry only "
            + ", ".join(sorted(_SUBMISSION_KEYS))
            + "; got "
            + ", ".join(unknown),
        )
        # The body first, because everything below is read from it rather than
        # from the submission.
        reference, digest, body, computed = _verified_body(root, payload, label, retain)
        lanes.add(_producer_lane(payload, label))
        run, attempt = _require_selected_operation(body, config, window, label)
        operations.add((run, attempt))
        # Keyed on the BODY filename, not the submission's. The producer is asked
        # to vouch for this value, so it has to be derivable from what the lane
        # actually publishes -- the evidence bodies. A submission file is local
        # bookkeeping the lane never saw and could not reproduce, so keying on it
        # left the attested value uncomputable by the only party able to attest it.
        bodies.append((str(payload["body"]), computed))
        screened.append(
            (
                kind,
                body,
                label,
                _authority(body, label),
                _instant(body, label),
                reference,
                digest,
            )
        )
    require(
        len(lanes) <= 1,
        "the retained records name more than one producer lane ("
        + ", ".join(sorted(lanes))
        + "); one capture reads one authorized operation's evidence",
    )
    require(
        len(operations) <= 1,
        "the retained records name more than one run attempt ("
        + ", ".join(f"{run} attempt {attempt}" for run, attempt in sorted(operations))
        + "); evidence from two operations cannot be attested as one set",
    )
    if not screened:
        return EMPTY_LEDGER
    lane = next(iter(lanes))
    run, attempt = next(iter(operations))
    attestation = _attest(
        lane, run, attempt, _manifest_digest(bodies), config["revision"]
    )
    services: ServiceInventory | None = None
    pending: list[tuple] = []
    for kind, body, label, authority, observed_at, reference, digest in screened:
        if kind == CHECK_EVIDENCE:
            pending.append((body, label, authority, observed_at, reference, digest))
        else:
            require(
                services is None,
                f"{label}: the service inventory is enumerated once; two listings "
                "cannot both be the authoritative one",
            )
            services = _service_inventory(
                body, label, authority, observed_at, reference, digest, attestation
            )
    # A serving record may name a service from the authoritative listing as well as
    # a machine this capture saw. That is not a loosening for convenience: serving
    # has no controller-side inventory for the capture to observe independently --
    # which is exactly why scanning cluster names could never establish serving --
    # so the hash-, window- and target-validated listing is the only authority on
    # which services exist. Every other check still has to name a machine the
    # capture observed for itself.
    # Only an authenticated listing may lend its service handles: an unvouched
    # listing naming a service would otherwise let a forged serving submission
    # name it back and pass the resource screen.
    serving_handles = frozenset(
        services.services if services and services.authenticated else ()
    )
    evidence: dict[str, list[ReceiptEvidence]] = {}
    for body, label, authority, observed_at, reference, digest in pending:
        item = _check_evidence(
            body,
            label,
            authority,
            observed_at,
            reference,
            digest,
            observed_handles,
            serving_handles,
            attestation,
        )
        evidence.setdefault(item.check_id, []).append(item)
    return OperationLedger(evidence=evidence, services=services)


def _require_selected_operation(
    payload: dict, config: dict, window, label: str
) -> tuple[str, int]:
    """Target, revision, run identity and time, all read from the attested body.

    Returns the run and attempt the body names, because that is what the producer
    is asked about. Both are required whether or not a producer is registered:
    they are what an attestation is *about*, so evidence that does not say which
    run and attempt it came from is structurally unauthenticatable by anyone and
    is refused rather than accepted as permanently unvouched.
    """
    __tracebackhide__ = True
    require(
        payload.get("environment") == config["environment"],
        f"{label}: the evidence was produced against another environment",
    )
    require(
        payload.get("deployed_revision") == config["revision"],
        f"{label}: the evidence was produced against another deployed revision",
    )
    run = _words(payload, "run_id", label)
    attempt = _integer(payload, "attempt", label)
    require(attempt >= 1, f"{label}: 'attempt' must be a positive attempt number")
    require(
        window.contains(_instant(payload, label)),
        f"{label}: the evidence was observed outside the authorized execution "
        "window, so it may be replayed from an earlier session",
    )
    return run, attempt


def _authority(payload: dict, label: str) -> str:
    __tracebackhide__ = True
    authority = payload.get("authority")
    require(
        isinstance(authority, str) and authority.strip() != "",
        f"{label}: 'authority' must name the system that produced this record",
    )
    assert isinstance(authority, str)
    authority = _safe(authority.strip(), f"{label}: authority", MAX_IDENTIFIER)
    require(
        authority in AUTHORITIES,
        f"{label}: {authority!r} is not a recognized authority; recognized: "
        + ", ".join(sorted(AUTHORITIES)),
    )
    return authority


def _check_evidence(
    payload: dict,
    label: str,
    authority: str,
    observed_at: datetime,
    reference: str,
    digest: str,
    observed_handles: frozenset[str],
    serving_handles: frozenset[str],
    attestation: _Attestation,
) -> ReceiptEvidence:
    """One evidence body's derived outcome.

    ``payload`` here is the *body* -- the attested bytes -- so every value read
    below is covered by the digest the producer vouched for.
    """
    __tracebackhide__ = True
    check_id = payload.get("check_id")
    require(
        isinstance(check_id, str) and check_id in _DERIVATIONS,
        f"{label}: 'check_id' must be one of the checks a retained record may "
        "settle; the rest are established by reading the live system",
    )
    assert isinstance(check_id, str)
    rule = _DERIVATIONS[check_id]
    require(
        authority in rule.authorities,
        f"{label}: {check_id} may only be evidenced by "
        + ", ".join(rule.authorities)
        + f"; this record claims {authority}",
    )
    handles = _handles(payload, label)
    allowed = observed_handles | (
        serving_handles if check_id.startswith("serving.") else frozenset()
    )
    require(
        bool(frozenset(handles) & allowed),
        f"{label}: {check_id} names no resource this capture observed for itself, "
        "so the record concerns another operation",
    )
    facts = payload.get("observations")
    require(
        isinstance(facts, dict),
        f"{label}: the evidence body must carry an 'observations' object",
    )
    assert isinstance(facts, dict)
    # The derivation reads observations only. A verdict written into the body is
    # never consulted, and declaring one is refused so nobody believes it was
    # honoured.
    require(
        "outcome" not in payload and "passed" not in payload,
        f"{label}: an evidence body supplies observations, not verdicts; the "
        "outcome is derived here from those observations",
    )
    outcome, detail = rule.derive(facts, f"{label} ({check_id})")
    if not attestation.authenticated:
        # Retained as diagnostics, but it settles nothing. Keeping the derived
        # detail alongside the reason is deliberate: an operator debugging a
        # blocked run needs to see what the material said and why it did not
        # count.
        outcome, detail = (
            Outcome.INDETERMINATE,
            f"{detail} (unauthenticated: {attestation.detail})",
        )
    return ReceiptEvidence(
        check_id=check_id,
        outcome=outcome,
        detail=_safe(detail, f"{label}: derived detail"),
        evidence_reference=reference,
        evidence_sha256=digest,
        observed_at=observed_at,
        handles=handles,
        authenticated=attestation.authenticated,
        needs_provider_confirmation=rule.needs_provider_confirmation,
        instance=(
            _words(facts, "instance", f"{label} ({check_id})")
            if rule.needs_provider_confirmation
            else ""
        ),
    )


def _service_inventory(
    payload: dict,
    label: str,
    authority: str,
    observed_at: datetime,
    reference: str,
    digest: str,
    attestation: _Attestation,
) -> ServiceInventory:
    """The retained ``sky serve status`` listing, with its real service handles.

    ``payload`` is the attested body, so the listing itself is covered by the
    digest a producer vouched for. An unauthenticated listing is carried through
    with ``authenticated`` false, which ``live_baseline`` refuses for the absence
    branch -- a forged empty listing must not establish that nothing is serving.
    """
    __tracebackhide__ = True
    require(
        authority == SKYSERVE_STATUS,
        f"{label}: the service inventory is authoritative only from "
        f"{SKYSERVE_STATUS}; a cluster listing cannot establish serving presence",
    )
    entries = payload.get("services")
    require(
        isinstance(entries, list) and all(isinstance(item, dict) for item in entries),
        f"{label}: 'services' must be a list of service objects, empty if the "
        "listing found none",
    )
    assert isinstance(entries, list)
    names, controllers, endpoints = [], [], []
    for index, entry in enumerate(entries):
        where = f"{label}: service {index}"
        names.append(_words(entry, "name", where))
        controllers.append(_words(entry, "controller", where))
        endpoints.append(_words(entry, "endpoint", where))
    require(
        len(set(names)) == len(names),
        f"{label}: the service listing names the same service twice",
    )
    return ServiceInventory(
        services=tuple(names),
        controllers=tuple(controllers),
        endpoints=tuple(endpoints),
        observed_at=observed_at,
        evidence_reference=reference,
        evidence_sha256=digest,
        enumerated_via=(
            "sky serve status from an authorized client, retained and "
            + ("producer-authenticated" if attestation.authenticated else "hashed")
            + "; the authoritative service inventory, there being no "
            "controller-side one"
        ),
        authenticated=attestation.authenticated,
    )
