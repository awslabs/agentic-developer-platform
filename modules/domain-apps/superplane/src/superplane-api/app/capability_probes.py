"""Exercise each configured trust adapter instead of testing that it exists.

Issue #5524 (w6-01), EPIC #4910, Wave 6.

## The check this replaces

``installation.capabilities()`` used to answer four ``is not None`` tests. That
asks whether a name is bound, not whether anything is behind it. An object that
exists, implements none of its port's calls, or approves everything it is asked
passed that check — and the installer, the boot gate and the post-rollout recheck
all treat passing it as evidence the image is composed for production. A check
that cannot distinguish a real adapter from a placeholder is worse than no check,
because it manufactures confidence.

So each capability here is established by *calling* the configured adapter and
requiring it to refuse, in the shape its port's contract declares.

## This is a read-only smoke check, and it says so

**What it establishes:** a real implementation is installed behind each of the four
names, it implements the call, and it refuses a wholly-unauthorized request in the
shape its contract declares.

**What it does not establish:** which rule did the refusing. It cannot isolate the
workspace, operation-identity, authority or permission rules individually, and it
reports every one of them in ``not_exercised`` rather than as a verified refusal.
Per-rule evidence needs a seeded valid control request the adapter accepts, which
does not exist here — see below. That evidence is the seeded offline contract
suite's job, and live evidence is the Wave 6 operations evaluator's.

Every report carries ``SMOKE_LIMITATION`` saying exactly this, so a green boot gate
cannot be quoted as conformance. ``conformant`` is reported as ``False`` on every
smoke report, with ``isolated`` ``False`` beside it, because no control ran.

## Why isolation is impossible here, and why faking it was the defect

Isolating a rule requires a request the adapter *accepts*, so that varying one
field and getting a refusal attributes the refusal to that field. This check has no
such request: it runs on every boot, inside the image, with ``--network=none``, and
must not provision, spend or deliver anything. There is no provisioned tenant to
name and deliberately no credential to present, so every request it can construct
is unauthorized in every field at once.

Two earlier revisions claimed isolation anyway:

1. The first called each adapter **once** with every field forged simultaneously
   and applied that single outcome to every probe descriptor.
2. The second issued one call per probe — but varied one field of a baseline that
   was already unauthorized in every *other* field.

Both reported the same adapter as fully conformant: one that checks the workspace
and ignores the operation identity, the authority and the permission entirely.
Under (2) it refuses all four calls, because the baseline workspace is one it was
never granted, and the report credited it with refusing an unminted authority and a
forged operation identity. The refusals were real; the attribution was invented.

So this module now runs exactly one probe per port —
``ProbeKind.UNKNOWN_MUST_NOT_SUCCEED``, the unauthorized request presented as
itself — via ``smoke_probes_for``. It no longer builds varied probes, and
``smoke_not_exercised_for`` names everything it therefore leaves unverified.

## Why an unauthorized read is the safe question to ask at startup

Every probe presents sentinel values from ``superplane_contracts.conformance`` — a
workspace no grant covers, an operation nobody issued, an authority never minted —
so a correct adapter has nothing to act on: the input is unauthorized by
construction rather than by the adapter's good behaviour. All four probes are
reads. In particular the facade is probed with ``report_progress`` and never
``open_operation``, because opening an operation on every boot is exactly the
mutation this check must not perform.

A placeholder cannot fake a refusal for the right reason: it has no method, or it
raises ``NotImplementedError``, or it returns something. Each is detected, and that
— not per-rule authorization evidence — is what this gate is for.

## What makes a capability false

The probe not producing a correctly-shaped refusal. Admitted, not implemented,
timed out, failed with a fault, or refused in the wrong shape — all false.

An earlier revision made this gate *lenient*: only ``ADMITTED`` and
``NOT_IMPLEMENTED`` failed it, so a timeout, an ``OSError`` or any other unexpected
exception reported the port as composed. The justification was the offline
preflight — a correct adapter whose vault is unreachable cannot establish
ownership. But every port's contract already says what to answer in that case: its
declared unknown outcome. An adapter that lets the connection error escape instead
has not answered, and a call that produced no contract-valid answer is not evidence
that the adapter refuses anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from superplane_contracts import (
    API_CAPABILITY_PORTS,
    CredentialReference,
    OperationKind,
    ProbeTimeout,
    ProbeVerdict,
    ProviderHandle,
    Submitter,
    classify_response,
    report_for,
    run_probes,
    smoke_not_exercised_for,
    smoke_probes_for,
)
from superplane_contracts.conformance import (
    PROBE_ALLOCATION_ID,
    PROBE_DIGEST,
    PROBE_OPERATION_ID,
    PROBE_ORG_ID,
    PROBE_WORKSPACE,
)

from app.services.credential_evidence import get_credential_evidence_reader
from app.services.provider_authority import get_provider_authority_validator
from app.services.provider_inventory import get_allocation_inventory_reader
from app.services.provisioning import get_operation_facade

# A hung adapter must not hold the boot gate open forever. Generous enough that a
# slow-but-correct adapter is not failed by it, short enough that a handful of them
# cannot stall a rollout: this runs on every boot and in the installer's preflight.
#
# Applied per probe, and a timeout now *fails* the capability rather than being
# tolerated — a hang establishes nothing about what the adapter refuses.
PROBE_TIMEOUT_SECONDS = 5.0

# The only verdict that leaves a capability true. Every other verdict — admitted,
# not implemented, timed out, failed, wrong refusal shape — means this call produced
# no contract-valid refusal, so it is not evidence the port is composed.
#
# Named as a single value rather than a denylist of failures so that a verdict added
# to `ProbeVerdict` later fails closed instead of silently joining the passing set.
# That direction matters: the previous revision listed the *disqualifying* verdicts,
# and every verdict it did not think to list passed the gate.
PASSING = ProbeVerdict.REFUSED

# The single unauthorized request presented per port, in canonical field names.
#
# Every value is a sentinel carrying `conformance_probe`: offline there is no real
# tenant to name, and naming a *plausible* one would be the hazard the sentinels exist
# to remove — a correct adapter must have nothing real to act on.
# `test_probes_present_only_sentinel_identifiers` asserts this over every argument
# actually passed.
#
# This request is presented **as itself**, never with one field varied. Varying a field
# of a request that is already unauthorized in every other field cannot isolate
# anything: every call has a legitimate reason to be refused regardless of the varied
# field, so a refusal attributes to nothing. That was the second of the two defects
# this module's docstring records. Per-rule evidence requires a valid control request
# the adapter accepts, which does not exist at boot.
_PROBE_WORKSPACE_VALUE = "__conformance_probe_workspace__"
_PROBE_AUTHORITY_VALUE = "__conformance_probe_forged_authority__"

_REQUEST: dict[str, dict[str, object]] = {
    "credential_evidence": {
        "workspace": _PROBE_WORKSPACE_VALUE,
    },
    "provider_authority": {
        "operation_id": "__conformance_probe_submitter__",
        "operation_authority": _PROBE_AUTHORITY_VALUE,
    },
    "allocation_inventory": {
        "workspace": _PROBE_WORKSPACE_VALUE,
        "operation_id": "__conformance_probe_allocation__",
        "operation_authority": _PROBE_AUTHORITY_VALUE,
    },
    "operation_facade": {
        "operation_id": "__conformance_probe_operation__",
    },
}


def _submitter(submitter_id: object, workspace: object) -> Submitter:
    """A submitter carrying no lease scope at all.

    Empty ``lease_scopes`` is the point: an adapter that consults a lease finds
    none, and one that does not consult a lease is the adapter this gate exists to
    catch.
    """
    return Submitter(
        submitter_id=str(submitter_id),
        workspaces=frozenset({str(workspace)}),
        lease_scopes=frozenset(),
    )


def _handle(allocation_id: object, workspace: object) -> ProviderHandle:
    return ProviderHandle(
        operation=OperationKind.PROVISION,
        provider="__conformance_probe__",
        resource_name=str(allocation_id),
        idempotency_key=str(allocation_id),
        allocation_id=str(allocation_id),
        workspace=str(workspace),
    )


async def _call_credential_evidence(adapter: Any, request: dict[str, object]) -> object:
    return await adapter.read(
        org_id=PROBE_ORG_ID,
        workspace_id=request["workspace"],
        reference=CredentialReference(
            credential_id=PROBE_OPERATION_ID,
            service="__conformance_probe__",
            label="__conformance_probe__",
        ),
        principal=PROBE_OPERATION_ID,
        report_digest=PROBE_DIGEST,
    )


async def _call_provider_authority(adapter: Any, request: dict[str, object]) -> object:
    # `operation_id` maps to the submitter identity here: this port binds
    # `submitter_id`, and a submitter nobody issued is what "forged operation
    # identity" means for a validator that resolves B's operation record.
    return await adapter.resolve(
        request["operation_authority"],
        submitter=_submitter(request["operation_id"], PROBE_WORKSPACE),
        handle=_handle(PROBE_ALLOCATION_ID, PROBE_WORKSPACE),
    )


async def _call_allocation_inventory(
    adapter: Any, request: dict[str, object]
) -> object:
    return await adapter.read(
        submitter=_submitter(PROBE_OPERATION_ID, request["workspace"]),
        workspace=request["workspace"],
        allocation_id=request["operation_id"],
        operation_authority=request["operation_authority"],
        report_digest=PROBE_DIGEST,
    )


async def _call_operation_facade(adapter: Any, request: dict[str, object]) -> object:
    # `report_progress`, never `open_operation`: this runs on every boot, and opening
    # an operation would be the mutation this check must not perform. The call carries
    # an operation id and nothing else, which is a second reason no rule can be
    # isolated through it.
    return await adapter.report_progress(request["operation_id"])


# Port name -> (fetch the installed adapter, invoke it with one probe request, the
# attribute that call needs). The attribute is listed explicitly so an absent method
# is detected as absent, rather than by catching the `AttributeError` its call would
# raise — an `AttributeError` from *inside* a real implementation is a bug in that
# implementation, and reporting it as "not implemented" would send the implementer
# looking for a method that is right there.
_PROBES: dict[
    str,
    tuple[
        Callable[[], Any], Callable[[Any, dict[str, object]], Awaitable[object]], str
    ],
] = {
    "credential_evidence": (
        get_credential_evidence_reader,
        _call_credential_evidence,
        "read",
    ),
    "provider_authority": (
        get_provider_authority_validator,
        _call_provider_authority,
        "resolve",
    ),
    "allocation_inventory": (
        get_allocation_inventory_reader,
        _call_allocation_inventory,
        "read",
    ),
    "operation_facade": (
        get_operation_facade,
        _call_operation_facade,
        "report_progress",
    ),
}

if set(_PROBES) != set(API_CAPABILITY_PORTS):  # pragma: no cover - import-time guard
    # The registry and this module must name the same ports. Checked at import so a
    # port added to one and not the other fails immediately, rather than silently
    # going unprobed while the readout still reports four green capabilities.
    raise RuntimeError("capability probes do not match API_CAPABILITY_PORTS")

if set(_REQUEST) != set(_PROBES):  # pragma: no cover - import-time guard
    raise RuntimeError("every probed port needs a probe request")


async def probe_port(port_name: str) -> dict[str, object]:
    """Smoke-check one configured adapter and report what that establishes.

    Returns the port's report summary plus ``composed``: the gate's answer for this
    port. ``conformant`` and ``isolated`` are both ``False`` on every report here,
    because no valid control request is available at boot and therefore no individual
    authorization rule was isolated — the dimensions are listed in ``not_exercised``.

    Never raises for an adapter's behaviour — an adapter that throws is a result to
    report, not an error to propagate, because this runs inside a boot gate and an
    escaping exception there would be indistinguishable from the image failing to
    start. Cancellation and process-control exceptions are **not** caught: they mean
    this task is being torn down, not that an adapter answered.
    """
    fetch, call, required_attribute = _PROBES[port_name]
    adapter = fetch()

    probes = smoke_probes_for(port_name)
    unexercised = smoke_not_exercised_for(port_name)

    if adapter is None:
        # Not composed at all: the honest, expected state in this repository today.
        # Reported as not-implemented rather than as a separate "absent" verdict, so
        # an uncomposed image and a placeholder image are treated identically —
        # neither may be installed.
        results = [classify_response(item, call_missing=True) for item in probes]
        return _summarize(
            port_name, results, unexercised, detail="no adapter installed"
        )

    if not callable(getattr(adapter, required_attribute, None)):
        results = [classify_response(item, call_missing=True) for item in probes]
        return _summarize(
            port_name,
            results,
            unexercised,
            detail=f"installed adapter has no callable {required_attribute!r}",
        )

    async def invoke(request: dict[str, object]) -> object:
        # The timeout is per probe, so one hanging case cannot consume the budget of
        # the others and have them reported as timeouts too.
        #
        # Re-raised as the contract's own `ProbeTimeout` rather than passed through as
        # `TimeoutError`, because builtin `TimeoutError` is an `OSError` subclass: a
        # socket timing out *inside* the adapter would otherwise be classified as the
        # gate abandoning the call. Both fail the capability, but they are different
        # defects and the report must not conflate them.
        try:
            return await asyncio.wait_for(
                call(adapter, request), timeout=PROBE_TIMEOUT_SECONDS
            )
        except TimeoutError as expired:
            raise ProbeTimeout(f"probe exceeded {PROBE_TIMEOUT_SECONDS}s") from expired

    results = await run_probes(port_name, invoke, _REQUEST[port_name], probes=probes)
    return _summarize(port_name, list(results), unexercised)


def _summarize(
    port_name: str,
    results: list,
    not_exercised: tuple[str, ...] = (),
    *,
    detail: str = "",
) -> dict[str, object]:
    """Fold probe results into the readout, adding the gate's own verdict.

    ``composed`` requires every probe run here to have produced a correctly-shaped
    refusal. It is deliberately **not** the same claim as ``conformant``, which stays
    ``False`` on every report this module produces: ``conformant`` additionally
    requires an admitted valid control, and there is none at boot. Reporting both, plus
    ``isolated``, is what keeps the distinction visible — a reader must be able to tell
    "a real adapter refused an unauthorized request" from "each authorization rule was
    verified", and an earlier revision made them identical by fabricating the second.
    """
    report = report_for(port_name, results, not_exercised=not_exercised)
    summary = report.summary()
    verdicts = {item.verdict for item in results}
    summary["composed"] = bool(results) and verdicts == {PASSING}
    summary["verdicts"] = sorted(item.value for item in verdicts)
    if detail:
        summary["detail"] = detail
    return summary


async def probe_all() -> dict[str, dict[str, object]]:
    """Exercise every API-side trust adapter, concurrently.

    Concurrent because sequential timeouts would multiply the delay on a boot gate.
    ``return_exceptions`` is not needed: ``probe_port`` reports adapter failures
    rather than raising them.
    """
    names = sorted(_PROBES)
    reports = await asyncio.gather(*(probe_port(name) for name in names))
    return dict(zip(names, reports, strict=True))
