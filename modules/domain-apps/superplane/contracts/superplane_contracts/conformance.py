"""Shared conformance probes: what a candidate adapter must refuse.

Issue #5524 (w6-01), EPIC #4910, Wave 6.

## Why this ships as a reusable module rather than as tests in each story

Sixteen Wave 6 stories each supply an adapter behind a port in
``integration.py``. If each story writes its own tests, each tests its adapter
against its own reading of the contract — and the mock it wrote to make those
tests pass is shaped like its own code, so the tests pass by construction and
establish nothing about interoperability. That is the failure mode the story
names: "no criterion ends at a mocked adapter".

So the checks live here, once, beside the contract they enforce, and every
implementing story runs the same ones. A story cannot weaken a check without
editing this module, which is reviewed by the contract owner.

## These are negative probes, and that is the whole design

A suite that only feeds an adapter well-formed authorized input proves very
little: an adapter that returns success unconditionally passes every such test.
What distinguishes a real implementation from a placeholder is what it **refuses**
— a workspace it was never granted, an operation identity nobody issued, an
authority value never minted, a request carrying no permission. Each probe below
presents exactly one of those and requires refusal.

The probes carry no valid credentials and name no real resources, so running them
is safe against a production adapter: there is no code path on which a correct
implementation acts on them. The readiness check in ``app/capability_probes.py``
relies on that — it probes with these and requires refusal, so it exercises the
configured adapter without mutating anything.

## Two tiers, because they can establish different things

This module serves two callers with genuinely different evidence available to
them, and conflating them is what produced the defect described below.

**The seeded offline conformance suite** (:func:`isolation_probes_for` driven
through :func:`run_probes`) is where per-rule isolation happens. It runs from a
**valid, authority-owned fixture request** that the candidate adapter is seeded to
accept, and it begins by presenting that request unchanged as
:attr:`ProbeKind.VALID_CONTROL`. Only then does it vary one field at a time.

**The startup smoke check** (the API server's boot gate and the ``--network=none``
image preflight) has no valid fixture available: offline, inside an image, with no
provisioned tenant and deliberately no real credential, there is no
authority-owned identity to present. So it presents a wholly-unauthorized sentinel
request — :attr:`ProbeKind.UNKNOWN_MUST_NOT_SUCCEED` — and reports **only** that.
Every other dimension is reported as not-exercised. That is the honest readout for
what a boot probe can see, and :func:`smoke_probes_for` is the only supported way
to build it.

## Why a valid control is what makes a refusal mean anything

A refusal is only evidence about the dimension a probe varied if the *rest* of the
request would have been accepted. Two revisions of this module got this wrong in
turn:

1. The first invoked the adapter **once** with every field replaced by a sentinel
   simultaneously and applied that single outcome to every probe descriptor.
2. The second issued one call per probe — but varied one field of a baseline that
   was *already* unauthorized in every other field. Every call therefore had a
   legitimate reason to be refused for the baseline alone.

Both admitted the same adapter: one that checks the workspace and ignores the
operation identity, the authority and the permission entirely. Under (2) it
refuses all four calls (the baseline workspace is one it was never granted) and is
reported as correctly refusing an unminted authority and a forged operation
identity. That is precisely the authorization bypass this suite exists to catch,
so it passed the suite it was supposed to fail.

The control removes that. With an otherwise-valid request the adapter demonstrably
accepts, a subsequent refusal is attributable to the one field that changed — and
an adapter that ignores that field accepts the probe and is recorded as accepting
it. If the control itself is refused, no negative result from that run is
attributable at all, and the report says so rather than counting refusals: an
adapter that refuses *everything*, including valid work, would otherwise pass a
purely negative suite while being completely broken.

:func:`build_request` guarantees the one-field rule rather than leaving it to each
caller: it refuses a probe whose varied field is absent from the baseline and
refuses a dimension with no override value, so a port that forgets one fails
loudly instead of silently re-sending the baseline.

## A dimension that cannot be exercised is not reported as passing

A port's contract can require a refusal the readiness call has no way to present —
``operation_facade`` is probed through ``report_progress``, which carries an
operation id and nothing else, so no permission or authority value can be varied
through it. Those dimensions are listed in
:attr:`ConformanceReport.not_exercised` and named in the summary, never counted as
refusals. Silently dropping them is how the gap in the earlier revision survived:
the stale-version probe was reported as verified against adapters that are never
passed a version at all.

## What a passing result does and does not establish

A passing **isolation** set establishes that the adapter accepts a seeded valid
request and refuses each unauthorized variation of it, one dimension at a time. A
passing **smoke** set establishes only that a real implementation is installed and
refuses a wholly-unauthorized request — it says nothing about which rule did the
refusing, and it reports every other dimension as not-exercised.

Neither establishes that the adapter authorizes *real* work, reaches its provider,
or works live: the fixture identities are seeded, not provisioned, and no
credential is presented. Those need the live gate, and no offline check can stand
in for them. Every report this module produces carries the matching qualification
in :attr:`ConformanceReport.limitation`, so neither result can be quoted as live
acceptance and a smoke result cannot be quoted as conformance.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum

from .health import ContractViolation
from .integration import PortContract, UnknownOutcome, port


class ProbeTimeout(Exception):
    """Raised by a caller's ``call`` to report that it abandoned a probe on time.

    The bound belongs to the caller — it is the side that knows whether it is a boot
    gate with seconds to spare or a contract suite that can wait — so the caller
    applies it and raises this to say so.

    Defined here, and deliberately **not** ``TimeoutError``, because builtin
    ``TimeoutError`` is a subclass of ``OSError``: a socket timing out *inside* an
    adapter would otherwise be indistinguishable from the runner abandoning the call,
    and those mean different things. The first is an adapter that failed to answer in
    the way its contract requires; the second is no answer at all. Both fail, but a
    report that confuses them sends the implementer to the wrong place.

    Its existence also keeps this module free of ``asyncio``: the contracts package
    owns no scheduler, timer or queue, which
    ``test_the_package_owns_no_lifecycle_machinery`` enforces structurally.
    """


# Sentinel values the probes present. Deliberately recognizable and deliberately
# never valid: a correct adapter has no grant covering this workspace, no record of
# this operation and no lease matching this authority, so every probe below is
# unauthorized by construction rather than by the adapter's good behaviour.
#
# The `__` affixes keep them from colliding with a real identifier and make them
# obvious in any log line a refusal produces.
PROBE_WORKSPACE = "__conformance_probe_workspace__"
PROBE_OPERATION_ID = "__conformance_probe_operation__"
PROBE_AUTHORITY = "__conformance_probe_forged_authority__"
PROBE_ORG_ID = "__conformance_probe_org__"
PROBE_ALLOCATION_ID = "__conformance_probe_allocation__"
PROBE_DIGEST = "__conformance_probe_digest__"

# The permission the MISSING_PERMISSION probe presents: syntactically a permission,
# granted to nobody. A blank string would be the weaker probe — an adapter comparing
# against a falsy value can reject it without consulting any grant, which would pass
# the probe while establishing nothing about whether permissions are checked.
PROBE_UNGRANTED_PERMISSION = "__conformance_probe_ungranted_permission__"

# A version no registry entry declares, used by the stale-version probe. Far enough
# from `v1` that it cannot be confused for a real successor a future registry adds.
STALE_VERSION = "v0"
UNSERVED_FUTURE_VERSION = "v999"

# Every report carries one of these. A green offline result is exactly the artifact
# someone will later quote as evidence the integration works, so the qualification
# travels with the result rather than living only in this docstring.
#
# Two strings, not one, because the two tiers establish different things and a single
# shared sentence would have to be vague enough to cover both — which is how a boot
# probe's result comes to be read as conformance.
CONFORMANCE_LIMITATION = (
    "Offline conformance only: establishes that the adapter accepts a seeded valid "
    "control request and refuses each unauthorized variation of it in isolation. The "
    "fixture identities are seeded, not provisioned, and no credential is presented, "
    "so this does not establish that the adapter authorizes real work, reaches its "
    "provider, or works live. Live acceptance remains with the Wave 6 operations "
    "evaluator."
)

SMOKE_LIMITATION = (
    "Startup smoke check only: establishes that an implementation is installed and "
    "refuses a wholly-unauthorized sentinel request in its declared shape. It does "
    "NOT isolate any individual authorization rule — the workspace, operation "
    "identity, authority and permission dimensions are reported as not-exercised, "
    "because no valid control request exists offline to vary against. It is not "
    "conformance evidence; the seeded offline suite and the Wave 6 operations "
    "evaluator own that."
)


class ProbeKind(str, Enum):
    """The class of unauthorized input a probe presents.

    ``str``-valued for the same reason the rest of the package's enums are: a
    report serializes to stable strings a receiver in another language compares
    against.
    """

    FORGED_WORKSPACE = "forged_workspace"
    """A workspace the adapter was never granted. Must refuse, not scope-correct."""

    FORGED_OPERATION_IDENTITY = "forged_operation_identity"
    """An operation identity nobody issued. Must refuse, not create a binding."""

    UNKNOWN_AUTHORITY = "unknown_authority"
    """An authority value never minted. Must refuse, not accept on shape alone."""

    STALE_CONTRACT_VERSION = "stale_contract_version"
    """A version the adapter does not serve. Must refuse, not best-effort interpret."""

    MISSING_PERMISSION = "missing_permission"
    """A request carrying no permission, or the wrong one. Must refuse."""

    UNKNOWN_MUST_NOT_SUCCEED = "unknown_must_not_succeed"
    """An unestablished outcome. Must surface as unknown, never as success.

    Varies nothing: it presents the wholly-unauthorized sentinel request, which names
    no workspace, operation or authority that exists. This is the *only* dimension the
    startup smoke check can present, because it has no valid fixture to vary against.
    """

    VALID_CONTROL = "valid_control"
    """A request the adapter is seeded to accept. Must be admitted, not refused.

    The one probe whose passing verdict is an *admission*, and the reason every other
    probe in an isolation run means anything: a refusal is attributable to the field a
    probe varied only if the rest of the request would have been accepted. Without it,
    an adapter that refuses everything — including legitimate work — passes a purely
    negative suite while being entirely broken, and an adapter that enforces one rule
    is credited for the rules it ignores, because every call was already unauthorized
    for another reason.

    Available only where a caller can supply a seeded valid fixture. The startup smoke
    check cannot, which is why it reports every varied dimension as not-exercised
    instead of pretending to isolate them.
    """


class ProbeVerdict(str, Enum):
    """What an adapter did when presented with a probe."""

    REFUSED = "refused"
    """Correct: the adapter declined, by the shape its port declares."""

    ADMITTED = "admitted"
    """Incorrect: the adapter returned a usable result for unauthorized input."""

    WRONG_REFUSAL_SHAPE = "wrong_refusal_shape"
    """Refused, but not in the way its port's ``unknown_outcome`` requires.

    Kept distinct from ``ADMITTED`` because the remedy differs: an adapter that
    raises where its contract says return ``None`` is a real defect (a caller
    catching nothing crashes instead of refusing) but it is not an authorization
    bypass, and collapsing the two would hide which of the sixteen adapters is
    actually dangerous.
    """

    NOT_IMPLEMENTED = "not_implemented"
    """The adapter has no such call at all — a placeholder, not an implementation.

    This is the verdict that catches what the previous readiness check could not:
    an object that exists, satisfies an ``is not None`` test, and implements
    nothing.
    """

    TIMED_OUT = "timed_out"
    """The adapter did not answer within the probe's bound.

    Distinct from ``WRONG_REFUSAL_SHAPE`` because a hang is not a refusal at all: it
    produced no answer, so nothing was established about what it would refuse. An
    earlier revision folded this into the wrong-shape verdict and then let
    wrong-shape pass the gate, which meant an adapter that never returned was
    reported as composed.
    """

    FAILED = "failed"
    """The adapter raised something its contract does not sanction as a refusal.

    An adapter whose vault is unreachable is required to answer with its port's
    declared unknown outcome. Letting a connection error escape instead is a defect,
    and it means the call produced no contract-valid answer — so it cannot be
    evidence that the adapter refuses anything.
    """

    ADMITTED_CONTROL = "admitted_control"
    """Correct, and only for ``VALID_CONTROL``: the seeded valid request was admitted.

    Deliberately a distinct value from ``REFUSED`` rather than reusing it for "the
    control passed". Reusing it would make a summary listing verdicts unreadable — an
    ``ADMITTED`` that passes next to an ``ADMITTED`` that fails — and it would let a
    gate that checks ``verdicts == {REFUSED}`` silently accept a run with no control.
    """

    REFUSED_VALID_CONTROL = "refused_valid_control"
    """Incorrect, and only for ``VALID_CONTROL``: legitimate seeded work was refused.

    The failure that invalidates the rest of the run. An adapter refusing everything
    passes every negative probe, so without this verdict "refuses the forged workspace"
    and "refuses all work including valid work" are the same observation.
    """


# Which request field each probe kind varies, and what it varies it to. Kept as one
# table rather than inline at each probe so the isolation rule has a single
# definition: a probe varies `field` to `value` and changes nothing else.
#
# `MISSING_PERMISSION` maps to a sentinel rather than to `None` because a port whose
# baseline omits the field entirely would otherwise be indistinguishable from one
# varying it — `build_request` requires the field to be present in the baseline, and
# a permission nobody granted is the honest way to present "carrying the wrong one".
_VARIED_FIELD: dict[ProbeKind, tuple[str, object]] = {
    ProbeKind.FORGED_WORKSPACE: ("workspace", PROBE_WORKSPACE),
    ProbeKind.FORGED_OPERATION_IDENTITY: ("operation_id", PROBE_OPERATION_ID),
    ProbeKind.UNKNOWN_AUTHORITY: ("operation_authority", PROBE_AUTHORITY),
    ProbeKind.MISSING_PERMISSION: ("permission", PROBE_UNGRANTED_PERMISSION),
    ProbeKind.STALE_CONTRACT_VERSION: ("contract_version", STALE_VERSION),
}

# The kinds that present their caller's baseline unchanged rather than varying a field.
# Kept as a set so `varied_field`, `Probe.__post_init__` and `build_request` share one
# definition: a kind added to one and not the others is the kind of divergence that
# let a never-presented probe be reported as a verified refusal.
_VARIES_NOTHING = frozenset(
    {ProbeKind.UNKNOWN_MUST_NOT_SUCCEED, ProbeKind.VALID_CONTROL}
)

# The one kind whose correct answer is an admission rather than a refusal. Named here,
# beside the polarity it inverts, so `classify_response` has a single place to consult
# instead of each caller re-deriving it.
_EXPECTS_ADMISSION = frozenset({ProbeKind.VALID_CONTROL})


def varied_field(kind: ProbeKind) -> tuple[str, object]:
    """The request field ``kind`` varies, and the value it varies it to.

    Published because a story cannot use :func:`probes_for` without it. That
    function takes ``exercisable`` as a set of request *field names*, and a caller
    with no way to ask which name a kind varies would have to guess — and a guess
    that misses (``"workspace_id"`` for ``"workspace"``) silently drops the probe
    instead of failing, which is the same class of quiet omission this revision is
    fixing.

    ``UNKNOWN_MUST_NOT_SUCCEED`` and ``VALID_CONTROL`` vary nothing and yield
    ``("", None)``: each presents its caller's baseline unchanged, so there is no
    dimension to name. They differ in what the baseline *is* — an unauthorized
    sentinel request for the first, a seeded valid one for the second — and therefore
    in which answer is correct.
    """
    if not isinstance(kind, ProbeKind):
        raise ContractViolation("varied_field() takes a ProbeKind")
    if kind in _VARIES_NOTHING:
        return ("", None)
    declared = _VARIED_FIELD.get(kind)
    if declared is None:  # pragma: no cover - guards a future ProbeKind
        raise ContractViolation(
            f"probe kind {kind.value!r} declares no varied field; add it to "
            "_VARIED_FIELD or it cannot be executed in isolation"
        )
    return declared


@dataclass(frozen=True)
class Probe:
    """One unauthorized input and the refusal it requires.

    Frozen so a caller cannot retune a probe into a weaker one between building the
    set and running it.

    ``varies``/``value`` are what make a probe *executable*: the runner builds the
    port's baseline request, replaces this one field, and issues a call for this
    probe alone. Without them a probe is only a label, and one adapter response
    ends up standing in as evidence for every probe in the set.
    """

    kind: ProbeKind
    port_name: str
    description: str
    required_outcome: UnknownOutcome
    varies: str = ""
    """The single request field this probe replaces. Empty only for ``UNKNOWN_MUST_NOT_SUCCEED``."""

    value: object = None
    """What ``varies`` is replaced with."""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ProbeKind):
            raise ContractViolation("Probe.kind must be a ProbeKind")
        if not isinstance(self.required_outcome, UnknownOutcome):
            raise ContractViolation("Probe.required_outcome must be an UnknownOutcome")
        if not self.description.strip():
            raise ContractViolation("Probe.description is required")
        # Raises if the port is unknown, so a probe cannot be written against a
        # port this registry does not declare.
        port(self.port_name)
        if self.kind in _VARIES_NOTHING:
            # These kinds vary nothing: each asks what the adapter does with its
            # caller's baseline unchanged. A field here would make one a second copy
            # of whichever probe it duplicated.
            if self.varies:
                raise ContractViolation(
                    f"{self.kind.value!r} varies no field; it presents the baseline "
                    "unchanged"
                )
            return
        expected = _VARIED_FIELD.get(self.kind)
        if expected is None:  # pragma: no cover - guards a future ProbeKind
            raise ContractViolation(
                f"probe kind {self.kind.value!r} has no varied field declared; add "
                "it to _VARIED_FIELD or the probe cannot be executed in isolation"
            )
        if not self.varies:
            raise ContractViolation(
                f"probe {self.kind.value!r} on port {self.port_name!r} varies no "
                "field; a probe with nothing varied re-sends the baseline and its "
                "result is evidence for no particular case"
            )
        if (self.varies, self.value) != expected:
            # The kind and the varied field must agree, or a report naming
            # "unknown_authority" could have been produced by forging a workspace.
            raise ContractViolation(
                f"probe {self.kind.value!r} must vary {expected[0]!r}; the kind a "
                "report names has to be the dimension actually varied"
            )


@dataclass(frozen=True)
class ProbeResult:
    """What one probe established about one adapter."""

    probe: Probe
    verdict: ProbeVerdict
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, ProbeVerdict):
            raise ContractViolation("ProbeResult.verdict must be a ProbeVerdict")

    @property
    def passed(self) -> bool:
        """True only for the one correct answer this probe's kind requires.

        A refusal for a negative probe; an admission for ``VALID_CONTROL``, whose
        polarity is inverted. There is deliberately no "warning" tier: a probe that
        admitted unauthorized input, a probe the adapter could not answer, and a
        control the adapter refused are all reasons an image must not be treated as
        composed.

        Expressed as a per-kind allowlist of one verdict rather than a denylist of
        failures, so a verdict added to ``ProbeVerdict`` later fails closed instead of
        silently joining the passing set — the direction an earlier revision got
        backwards, where every verdict nobody thought to list passed the gate.
        """
        if self.probe.kind in _EXPECTS_ADMISSION:
            return self.verdict is ProbeVerdict.ADMITTED_CONTROL
        return self.verdict is ProbeVerdict.REFUSED


@dataclass(frozen=True)
class ConformanceReport:
    """The outcome of running a probe set against one adapter.

    Carries ``limitation`` on every instance, including the passing ones. An
    operator reading a green report is exactly the operator about to describe the
    integration as working.
    """

    port_name: str
    results: tuple[ProbeResult, ...]
    limitation: str = CONFORMANCE_LIMITATION
    provenance: str = "offline conformance probes; no provider contacted"
    contract_version: str = field(default="")
    not_exercised: tuple[str, ...] = ()
    """Probe kinds this port declares that the caller's call could not present.

    Carried on the report so an unexercised dimension is visible rather than absent.
    A reader comparing two reports must be able to tell "the adapter refused this"
    from "nothing asked" — conflating them is how a never-presented stale-version
    probe came to be reported as a verified refusal.
    """

    def __post_init__(self) -> None:
        contract = port(self.port_name)
        if not self.contract_version:
            object.__setattr__(self, "contract_version", contract.contract_version)
        if not isinstance(self.results, tuple) or not self.results:
            # An empty result set is the dangerous case: `all(())` is True, so an
            # adapter nobody probed would report as conformant. Refused at
            # construction so that outcome is unreachable rather than merely
            # unlikely.
            raise ContractViolation(
                f"conformance report for {self.port_name!r} has no probe results; "
                "an unprobed adapter must not report as conformant"
            )
        if any(item.probe.port_name != self.port_name for item in self.results):
            raise ContractViolation(
                "conformance report mixes results from different ports"
            )
        if not self.limitation.strip():
            raise ContractViolation(
                "a conformance report must carry its limitation — a green offline "
                "result is the artifact most likely to be quoted as live evidence"
            )

    @property
    def conformant(self) -> bool:
        """True only when every probe answered correctly **and** isolation held.

        Requires :attr:`isolated` as well as every probe passing. A run whose valid
        control was refused has passing negative probes that establish nothing — the
        adapter might refuse every request it ever receives — so reporting it as
        conformant would be the false positive this property exists to prevent.
        """
        return self.isolated and all(item.passed for item in self.results)

    @property
    def isolated(self) -> bool:
        """Whether this run's negative results are attributable to one dimension each.

        True only when a :attr:`ProbeKind.VALID_CONTROL` probe was run and admitted.
        A smoke run has no control, so this is False and :attr:`conformant` is False
        with it — correctly: a smoke run is not conformance evidence.
        """
        control = [
            item for item in self.results if item.probe.kind is ProbeKind.VALID_CONTROL
        ]
        return bool(control) and all(item.passed for item in control)

    @property
    def failures(self) -> tuple[ProbeResult, ...]:
        """The probes this adapter did not answer correctly."""
        return tuple(item for item in self.results if not item.passed)

    def summary(self) -> dict[str, object]:
        """A caller-safe dict for receipts and CLI output.

        Names the port, the counts, the failing probe kinds and the limitation. It
        does **not** include probe details verbatim, because an adapter's refusal
        message is the one place a tenant identifier or provider error could have
        been interpolated, and a receipt is a low-control destination.
        """
        return {
            "port": self.port_name,
            "contract_version": self.contract_version,
            "conformant": self.conformant,
            # Reported beside `conformant` because they fail for different reasons and
            # the remedy differs: not-isolated means this run cannot attribute its
            # refusals (no admitted control), while a failure means a specific rule is
            # missing. An operator reading only `conformant: false` cannot tell which.
            "isolated": self.isolated,
            "probes": len(self.results),
            "failed": [item.probe.kind.value for item in self.failures],
            "exercised": sorted({item.probe.kind.value for item in self.results}),
            "not_exercised": list(self.not_exercised),
            "provenance": self.provenance,
            "limitation": self.limitation,
        }


def declared_kinds(port_name: str) -> tuple[ProbeKind, ...]:
    """Every probe kind this port's contract obliges its implementation to refuse.

    Derived from the registry entry rather than hand-listed per port, so a port
    whose obligations change gets the matching probes automatically instead of
    keeping an out-of-date hand-written set that still passes.

    This is the *obligation* set, deliberately independent of whether any particular
    call can present each case. Keeping the two separate is what lets
    :func:`not_exercised_for` report a dimension as unexercised instead of having it
    disappear: an obligation nothing exercised and an obligation the adapter refused
    must not read the same way.
    """
    contract = port(port_name)
    kinds: list[ProbeKind] = [
        # The control. An obligation on every port: an implementation that refuses
        # legitimate work is as broken as one that admits illegitimate work, and
        # without this the negative probes below are unattributable.
        ProbeKind.VALID_CONTROL,
        # Varies nothing: presents a request naming nothing that exists, so an adapter
        # reporting a resolved outcome for it has resolved something it cannot have
        # established. Every port can be called, so this is always an obligation.
        ProbeKind.UNKNOWN_MUST_NOT_SUCCEED,
        # Every port's contract is versioned, so refusing an unserved version is an
        # obligation for all of them. Whether a given *call* can present one is a
        # separate question, answered by `carries_contract_version`.
        ProbeKind.STALE_CONTRACT_VERSION,
    ]
    # Workspace/tenant probes only where the port actually binds a tenant scope.
    # A port with no workspace in its bound identifiers has nothing to forge, and
    # a probe asserting otherwise would be testing a rule the contract never made.
    if _binds_any(contract, ("workspace", "workspace_id")):
        kinds.append(ProbeKind.FORGED_WORKSPACE)
    if _binds_any(contract, ("operation_id", "allocation_id", "lease_id")):
        kinds.append(ProbeKind.FORGED_OPERATION_IDENTITY)
    if _binds_any(contract, ("operation_authority",)) or contract.owner.value in (
        "harness_jobs",
        "gateway_vault",
    ):
        kinds.append(ProbeKind.UNKNOWN_AUTHORITY)
    if contract.required_permission is not None:
        kinds.append(ProbeKind.MISSING_PERMISSION)
    return tuple(kinds)


_DESCRIPTIONS: dict[ProbeKind, str] = {
    ProbeKind.UNKNOWN_MUST_NOT_SUCCEED: "an outcome the adapter could not establish",
    ProbeKind.VALID_CONTROL: (
        "the seeded valid fixture request, unchanged; must be admitted, or no "
        "refusal in this run is attributable to the dimension a probe varied"
    ),
    ProbeKind.STALE_CONTRACT_VERSION: (
        f"a request written against {STALE_VERSION!r}, which this contract does not serve"
    ),
    ProbeKind.FORGED_WORKSPACE: (
        f"workspace {PROBE_WORKSPACE!r}, which no grant covers; must be refused "
        "rather than corrected to the granted workspace"
    ),
    ProbeKind.FORGED_OPERATION_IDENTITY: (
        "an operation identity that was never issued; must be refused rather than "
        "used to create a binding"
    ),
    ProbeKind.UNKNOWN_AUTHORITY: (
        f"authority {PROBE_AUTHORITY!r}, never minted by B; must be refused rather "
        "than accepted on its shape"
    ),
    ProbeKind.MISSING_PERMISSION: (
        f"a request carrying {PROBE_UNGRANTED_PERMISSION!r} where the port's "
        "required permission is demanded"
    ),
}


def smoke_probes_for(port_name: str) -> tuple[Probe, ...]:
    """The only probe a startup gate can honestly run: the unauthorized baseline.

    A boot gate and a ``--network=none`` image preflight have no seeded valid fixture
    — offline, in an image, with no provisioned tenant and deliberately no credential,
    there is no authority-owned identity to present. So they cannot isolate any
    individual rule, and this returns exactly one probe:
    :attr:`ProbeKind.UNKNOWN_MUST_NOT_SUCCEED`, presenting a request that names
    nothing that exists.

    Everything else is reported by :func:`smoke_not_exercised_for`. That is the whole
    point of a separate constructor: an earlier revision let the startup gate build a
    *varied* probe set against an already-unauthorized baseline, so an adapter that
    checked only the workspace refused every call and was reported as having verified
    the operation identity, the authority and the permission. Those refusals were real
    and the attribution was fabricated.

    A caller wanting per-rule evidence uses :func:`isolation_probes_for` with a seeded
    fixture, which is the contract suite's job and not a boot gate's.
    """
    contract = port(port_name)
    return (
        Probe(
            kind=ProbeKind.UNKNOWN_MUST_NOT_SUCCEED,
            port_name=port_name,
            description=_DESCRIPTIONS[ProbeKind.UNKNOWN_MUST_NOT_SUCCEED],
            required_outcome=contract.unknown_outcome,
        ),
    )


def smoke_not_exercised_for(port_name: str) -> tuple[str, ...]:
    """Every dimension a startup smoke check leaves unverified.

    Which is every obligation except the unauthorized-baseline probe itself. Reported
    so a green boot gate cannot be read as "each rule in the contract was checked
    here" — it was not, and saying so is the difference between a smoke check and
    manufactured conformance.
    """
    exercised = {item.kind for item in smoke_probes_for(port_name)}
    return tuple(
        sorted(item.value for item in set(declared_kinds(port_name)) - exercised)
    )


def isolation_probes_for(
    port_name: str, *, exercisable: Iterable[str] | None = None
) -> tuple[Probe, ...]:
    """The per-rule probe set, for a caller with a seeded valid fixture request.

    Leads with :attr:`ProbeKind.VALID_CONTROL` — the fixture unchanged, which the
    adapter must admit — and then one probe per dimension, each varying a single field
    of that same valid request. The control is what makes the rest attributable: a
    refusal means the adapter enforced *that* field, because the request was otherwise
    acceptable to it.

    ``exercisable`` names the request fields the caller can vary through the call it
    will make. An obligation whose field is not among them is **omitted** and
    reported by :func:`not_exercised_for` rather than emitted and passed: the call
    could not present that case, so a refusal would not be evidence about it. That
    is the defect an earlier revision had — every port emitted a stale-version
    probe, including every one whose call carries no version at all, and each was
    reported as a verified refusal.

    ``None`` means "every dimension this port's call can carry", which for the
    stale-version case still respects ``carries_contract_version``: a call that
    exchanges no version cannot present a stale one, and pretending otherwise is
    what produced the false pass. A caller driving a fuller request builder passes
    the fields it actually has.
    """
    contract = port(port_name)
    available = None if exercisable is None else set(exercisable)
    if (
        available is not None
        and "contract_version" in available
        and not contract.carries_contract_version
    ):
        # The caller claims its call can vary a version this port does not exchange.
        # Refused rather than honoured: honouring it would emit a stale-version probe
        # whose "refusal" is the adapter ignoring an argument it never received, which
        # is the false pass this revision removes — and leaving it silent would let any
        # story restore that pass by adding one string.
        raise ContractViolation(
            f"port {port_name!r} declares carries_contract_version=False, so "
            "'contract_version' cannot be exercised through it; a probe varying a "
            "field the call does not carry establishes nothing"
        )
    probes: list[Probe] = []
    for kind in declared_kinds(port_name):
        field_name, value = varied_field(kind)
        if kind is ProbeKind.UNKNOWN_MUST_NOT_SUCCEED:
            # Belongs to the smoke tier, not here, and emitting it here made this tier
            # unpassable by a *correct* adapter. Both varies-nothing kinds send the
            # baseline unchanged, so in this tier — where the baseline is the seeded
            # **valid** fixture — this kind would demand a refusal of the exact request
            # `VALID_CONTROL` demands be admitted. No implementation can satisfy both,
            # and the usual response to an unpassable check is to weaken it.
            #
            # The two tiers therefore divide the varies-nothing cases by which baseline
            # each has: the smoke tier's request is unauthorized in every field, so it
            # owns "an unauthorized request must not succeed"; this tier's is valid, so
            # it owns the control and the per-field variations. `not_exercised_for`
            # names this kind, so the obligation is visibly delegated rather than
            # dropped.
            continue
        if kind in _VARIES_NOTHING:
            probes.append(
                Probe(
                    kind=kind,
                    port_name=port_name,
                    description=_DESCRIPTIONS[kind],
                    required_outcome=contract.unknown_outcome,
                )
            )
            continue
        if kind is ProbeKind.STALE_CONTRACT_VERSION and not (
            contract.carries_contract_version
        ):
            # No call through this port exchanges a version, so there is no stale one
            # to send. Omitted here and surfaced by `not_exercised_for`.
            continue
        if available is not None and field_name not in available:
            continue
        probes.append(
            Probe(
                kind=kind,
                port_name=port_name,
                description=_DESCRIPTIONS[kind],
                required_outcome=contract.unknown_outcome,
                varies=field_name,
                value=value,
            )
        )
    return tuple(probes)


def not_exercised_for(
    port_name: str, *, exercisable: Iterable[str] | None = None
) -> tuple[str, ...]:
    """The probe kinds this port declares but the caller's isolation call cannot present.

    Reported so an unexercisable dimension is visible as unexercised instead of
    being dropped — a dimension nobody probed and a dimension the adapter refused
    must not read the same way in a report.
    """
    emitted = {
        item.kind for item in isolation_probes_for(port_name, exercisable=exercisable)
    }
    return tuple(
        sorted(item.value for item in set(declared_kinds(port_name)) - emitted)
    )


def build_request(probe: Probe, baseline: dict[str, object]) -> dict[str, object]:
    """The request for one probe: ``baseline`` with this probe's one field replaced.

    The isolation guarantee lives here rather than in each caller, because a caller
    that built these by hand could quietly vary two fields at once — and a probe
    that varies the workspace *and* the authority tells you nothing about which of
    the two the adapter checked.

    Refuses a probe whose field is absent from the baseline instead of inserting it.
    An inserted field either is not a parameter of the call (a ``TypeError`` at the
    adapter) or is one the baseline forgot, and in the second case every other probe
    for this port has been running without it.
    """
    if not isinstance(baseline, dict):
        raise ContractViolation("baseline request must be a dict")
    if probe.kind in _VARIES_NOTHING:
        # Both kinds present the baseline exactly as given, and differ only in which
        # answer is correct: `UNKNOWN_MUST_NOT_SUCCEED` sends an unauthorized request
        # and requires a refusal, `VALID_CONTROL` sends the seeded valid fixture and
        # requires an admission. Tested against `_VARIES_NOTHING` rather than naming
        # one kind, because an earlier revision named only the first and the control
        # then raised here — making the probe that establishes isolation the one probe
        # that could not run.
        return dict(baseline)
    if probe.varies not in baseline:
        raise ContractViolation(
            f"probe {probe.kind.value!r} varies {probe.varies!r}, which the baseline "
            f"request for port {probe.port_name!r} does not contain; a probe cannot "
            "isolate a field the call does not carry"
        )
    request = dict(baseline)
    request[probe.varies] = probe.value
    return request


async def run_probes(
    port_name: str,
    call: Callable[[dict[str, object]], Awaitable[object]],
    baseline: dict[str, object],
    *,
    probes: Iterable[Probe] | None = None,
) -> tuple[ProbeResult, ...]:
    """Execute each probe for ``port_name`` as its own call and classify each result.

    This is the shared runner every Wave 6 story uses, so "was each case actually
    isolated?" is answered once here instead of sixteen times in sixteen ways.
    ``call`` receives one request dict per probe and invokes the candidate adapter
    with it; it is the only part a story supplies.

    An ``Exception`` from ``call`` is classified as that probe's result rather than
    propagated, because an adapter that raises is a result to report — and one probe's
    crash must not abandon the probes after it, or a report would silently cover only
    the cases up to the first failure.

    Only ``Exception`` is caught, never ``BaseException``. ``CancelledError``,
    ``KeyboardInterrupt`` and ``SystemExit`` derive from ``BaseException`` and so
    propagate untouched: they mean this runner's own caller is being torn down, not
    that an adapter answered, and an earlier revision caught ``BaseException`` here —
    which turned a Ctrl-C into a probe verdict and let a cancelled suite report
    results.

    The valid control is executed **first** when one is present, so a run that the
    adapter refuses wholesale is visible at the top of the report rather than inferred
    from it. Ordering is enforced here rather than trusted to each caller.
    """
    selected = tuple(probes) if probes is not None else isolation_probes_for(port_name)
    selected = tuple(
        sorted(selected, key=lambda item: item.kind is not ProbeKind.VALID_CONTROL)
    )
    results: list[ProbeResult] = []
    for probe in selected:
        request = build_request(probe, baseline)
        try:
            returned = await call(request)
        except NotImplementedError as error:
            # A placeholder, not a refusal: the call does not exist.
            results.append(classify_response(probe, call_missing=True, raised=error))
        except ProbeTimeout as error:
            results.append(classify_response(probe, raised=error, timed_out=True))
        except Exception as error:  # noqa: BLE001 - an adapter failure is a result
            results.append(classify_response(probe, raised=error))
        else:
            results.append(classify_response(probe, returned=returned))
    return tuple(results)


def _binds_any(contract: PortContract, names: Iterable[str]) -> bool:
    """Whether the port binds any of ``names``."""
    wanted = set(names)
    return any(item in wanted for item in contract.bound_identifiers)


def classify_response(
    probe: Probe,
    *,
    returned: object = None,
    raised: BaseException | None = None,
    call_missing: bool = False,
    timed_out: bool = False,
) -> ProbeResult:
    """Turn what an adapter did into a verdict.

    Kept as one function rather than duplicated at each probe site so the mapping
    from "what happened" to "pass or fail" has a single definition. The important
    property is that **the default is failure**: every branch that cannot
    positively establish a correctly-shaped refusal returns a failing verdict, so
    an adapter behaviour nobody anticipated does not fall through to a pass.

    ``call_missing`` is the placeholder case — the adapter has no such method.
    Passed explicitly rather than inferred from ``AttributeError``, because an
    ``AttributeError`` raised *inside* a real implementation is a bug in that
    implementation, not evidence the method is absent, and conflating the two would
    report a broken adapter as an unimplemented one.

    :attr:`ProbeKind.VALID_CONTROL` inverts the polarity: for it alone, a usable
    result is the pass and a refusal is the failure. Handled here rather than by a
    separate function so there is one mapping from "what happened" to "pass or fail",
    and a caller cannot accidentally classify a control with the negative rule.
    """
    if call_missing:
        return ProbeResult(
            probe=probe,
            verdict=ProbeVerdict.NOT_IMPLEMENTED,
            detail="adapter does not implement this call",
        )

    if timed_out:
        # No answer at all, so nothing was established. Kept ahead of the `raised`
        # branch because the timeout arrives *as* an exception and would otherwise be
        # classified by whatever shape the port's contract happens to require —
        # which, for a port whose unknown outcome is RAISE_UNAVAILABLE, would read a
        # hang as a correct refusal.
        return ProbeResult(
            probe=probe,
            verdict=ProbeVerdict.TIMED_OUT,
            detail="adapter did not answer within the probe timeout",
        )

    if probe.kind in _EXPECTS_ADMISSION:
        # The control, whose correct answer is an admission. Kept above every branch
        # below because those all read a refusal as success, and applying them here
        # would score an adapter that refuses legitimate work as passing its control —
        # which would restore exactly the unattributability the control removes.
        if raised is not None:
            return ProbeResult(
                probe=probe,
                verdict=ProbeVerdict.FAILED,
                detail=(
                    f"raised {type(raised).__name__} for the valid control request; "
                    "no negative result in this run is attributable"
                ),
            )
        if returned is None or _is_unresolved(returned):
            return ProbeResult(
                probe=probe,
                verdict=ProbeVerdict.REFUSED_VALID_CONTROL,
                detail=(
                    "refused the valid control request; an adapter that refuses "
                    "legitimate work passes every negative probe for the wrong reason"
                ),
            )
        return ProbeResult(probe=probe, verdict=ProbeVerdict.ADMITTED_CONTROL)

    required = probe.required_outcome

    if raised is not None:
        if required is UnknownOutcome.RAISE_UNAVAILABLE:
            # The port's refusal *is* an exception, but not every exception is that
            # refusal. Matched against the types the registry declares, as an
            # allowlist: an `AttributeError` or an `ArithmeticError` from inside the
            # adapter is a crash that happens to be raisable, and accepting any
            # exception would make these ports unfailable — every bug would read as
            # correct behaviour.
            declared = set(port(probe.port_name).refusal_exceptions)
            names = {cls.__name__ for cls in type(raised).__mro__}
            if declared & names:
                return ProbeResult(probe=probe, verdict=ProbeVerdict.REFUSED)
            return ProbeResult(
                probe=probe,
                verdict=ProbeVerdict.FAILED,
                detail=(
                    f"raised {type(raised).__name__}, which is not among this port's "
                    f"declared refusals ({', '.join(sorted(declared))})"
                ),
            )
        # Raising where the contract says "return None" or "return an unresolved
        # value" means the call produced no contract-valid answer: a caller written
        # against this port catches nothing and dies instead of refusing. It is not
        # an authorization bypass, so it stays distinct from ADMITTED — but it is
        # not evidence of a refusal either, and it fails.
        return ProbeResult(
            probe=probe,
            verdict=ProbeVerdict.WRONG_REFUSAL_SHAPE,
            detail=f"raised where {required.value} is required",
        )

    if required is UnknownOutcome.NONE_MEANS_UNVERIFIED:
        if returned is None:
            return ProbeResult(probe=probe, verdict=ProbeVerdict.REFUSED)
        return ProbeResult(
            probe=probe,
            verdict=ProbeVerdict.ADMITTED,
            detail="returned a value for unauthorized input; None is the refusal",
        )

    if required is UnknownOutcome.UNRESOLVED_VALUE:
        if _is_unresolved(returned):
            return ProbeResult(probe=probe, verdict=ProbeVerdict.REFUSED)
        return ProbeResult(
            probe=probe,
            verdict=ProbeVerdict.ADMITTED,
            detail="returned a resolved outcome for input it could not establish",
        )

    # required is RAISE_UNAVAILABLE and nothing was raised.
    return ProbeResult(
        probe=probe,
        verdict=ProbeVerdict.ADMITTED,
        detail="returned instead of refusing; unauthorized input must not yield a value",
    )


# The states that count as "not established". Compared against the string form so
# this works for the enums in this package (`ReconcileResult.UNRESOLVED`,
# `CostExposure.UNRESOLVED`, `CheckStatus.NOT_CHECKED`, all `str`-valued) and for
# the API server's plain-string `OperationProgress.state == "unknown"`, without
# importing either side's types. Importing them would make this module depend on
# `app.*`, which is exactly the dependency the registry refuses to take.
_UNRESOLVED_TOKENS = frozenset(
    {"unresolved", "unknown", "not_checked", "absent_unknown", "indeterminate"}
)


def _is_unresolved(value: object) -> bool:
    """Whether a returned value explicitly reports an unestablished outcome.

    Fail-closed: anything this cannot positively read as unresolved is treated as
    resolved, and therefore as an admission. An adapter returning some novel shape
    that happens to mean "unknown" fails the probe, which is the safe direction —
    the remedy is for the adapter to report unresolved in one of the contract's
    declared ways, not for this check to start guessing.
    """
    if value is None:
        # `None` is a refusal under NONE_MEANS_UNVERIFIED, but under
        # UNRESOLVED_VALUE the contract asked for a *typed* unresolved value, and
        # `None` is not one. Reported as not-unresolved so the wrong shape is
        # visible rather than quietly accepted.
        return False
    if isinstance(value, str):
        return value.strip().lower() in _UNRESOLVED_TOKENS
    for attribute in ("state", "result", "status", "exposure", "presence"):
        inner = getattr(value, attribute, None)
        if inner is None:
            continue
        candidate = getattr(inner, "value", inner)
        if (
            isinstance(candidate, str)
            and candidate.strip().lower() in _UNRESOLVED_TOKENS
        ):
            return True
    return False


def report_for(
    port_name: str,
    results: Iterable[ProbeResult],
    *,
    not_exercised: Iterable[str] = (),
    limitation: str = "",
) -> ConformanceReport:
    """Assemble a report, refusing an empty or mixed-port result set.

    A thin wrapper over the constructor, provided so callers do not each build the
    tuple and risk passing a generator — which would be consumed by the first
    ``all()`` and leave the report empty.

    The limitation is chosen from the results rather than taken on trust when the
    caller does not name one: a result set with no valid control cannot have isolated
    anything, so it gets :data:`SMOKE_LIMITATION`. Derived rather than defaulted
    because the wrong qualification on a green report is precisely how a boot-gate
    result gets quoted as conformance evidence — and a caller that had to remember to
    pass it would sometimes forget.
    """
    collected = tuple(results)
    if not limitation:
        has_control = any(
            item.probe.kind is ProbeKind.VALID_CONTROL for item in collected
        )
        limitation = CONFORMANCE_LIMITATION if has_control else SMOKE_LIMITATION
    return ConformanceReport(
        port_name=port_name,
        results=collected,
        limitation=limitation,
        not_exercised=tuple(not_exercised),
    )
