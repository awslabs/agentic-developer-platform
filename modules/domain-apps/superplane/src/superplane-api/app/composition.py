"""One runtime composition of this API's trust adapters, for every entry point.

Issue #5535 (Superplane W6), EPIC #4910.

## The defect this module removes

Composition used to be `app/main.py:compose_vault_client()` — a function inside
the FastAPI application module. The packaged image's capability preflight runs

    docker run --rm --network=none --entrypoint python \
        <api-image> -m app.installation capabilities

and `app/installation.py` does **not** import `app.main`, so that function never
ran in the preflight. The preflight therefore reported all four trust ports
uncomposed *no matter how the deployment was configured* — including
`credential_evidence`, the one port that does have a production adapter. The
installer requires all four true (`installation/runner.py:410-419`), so the gate
could not be satisfied by any configuration. It was not a gate; it was a wall.

Two code paths answered the same question differently. This module is the single
answer both of them now ask. The lifespan calls `compose()`; so does every
packaged capability and readiness command, before it probes.

## Why a returned record rather than a bare install

`compose()` reports, per port, whether an adapter was installed and *why not*
when one was not. The boolean the capability gate publishes is deliberately
**not** derived from this record: a port is only reported composed after
`app/capability_probes.py` has called the adapter and had it refuse an
unauthorized request. This record explains a False; it can never manufacture a
True. An earlier generation of this check answered `is not None`, and the whole
point of #5524 was that "a name is bound" is not evidence.

The distinction matters for the operator: "no vault URL configured" and "a vault
URL is configured but the vault refused the probe" are different faults with
different fixes, and four booleans cannot tell them apart.

## Three rules this module keeps deliberately

* **Nothing at import time.** A module-level install would give every test
  process and every unrelated CLI action a network dependency it never
  configured, and would make the ports' single-install guards unusable for
  substitution.
* **A pre-installed adapter is never displaced.** A test or an embedding host
  that installed its own reader is the authority. Overwriting it would let
  production composition silently replace a deliberately substituted trust
  source — which is exactly why `install_credential_evidence_reader` refuses a
  second install in the first place.
* **Unconfigured composes nothing, rather than composing a stub.** A permissive
  stub is a bypass; a stub that refuses everything reports a configuration gap as
  a per-request denial. `None` makes the consumer answer 503 "unavailable", which
  is the truthful answer and the one an operator can act on.

## What this module does not compose, and why that is reported rather than faked

Three of the four ports have no production adapter that can be built here today,
and each blocker is external to this component. `unconfigured` names the
dependency rather than installing something that would pass a probe:

* `operation_facade` and `allocation_inventory` are owned by `harness_jobs`
  (`modules/harness/jobs/`), which is **not in this image**.
  `scripts/stage-domain-auth.sh` stages `superplane_auth` and
  `superplane_contracts` only, and `releases/build-image.sh` pins the Docker
  build context to this component directory, so no `COPY` can reach the harness
  package. Widening that context is release-owned (#5327). `harness_jobs`
  additionally has no production `ApprovalSource` or `BudgetLedger` — and
  `tests/test_admission_bypass.py:221` asserts it must never ship one, because
  budget authority is domain-owned — so `OperationFacadeService` cannot be
  constructed even once the package is reachable.
* `provider_authority` cannot be satisfied from `harness_jobs` at all. The port
  requires `run_id`, `submitter_id` and `handle.resource_name`, and the harness
  package has no source for any of them (nor for the provision/submit/release
  `OperationKind` taxonomy). `_verify_authority` requires the resolved binding to
  equal the request handle field for field, so an adapter cannot synthesize the
  missing values by echoing the request — that is precisely the "create a binding
  from the supplied handle alone" the port forbids. Owed by #5529.

Reporting these is the honest outcome. Installing an adapter that returned a
verified binding it did not verify would satisfy the gate and defeat it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from superplane_contracts import API_CAPABILITY_PORTS

from app.adapters.adp_vault_client import build_vault_client
from app.services.credential_evidence import (
    get_credential_evidence_reader,
    install_credential_evidence_reader,
)
from app.services.provider_authority import get_provider_authority_validator
from app.services.provider_inventory import get_allocation_inventory_reader
from app.services.provisioning import get_operation_facade

logger = logging.getLogger(__name__)

PORT_CREDENTIAL_EVIDENCE = "credential_evidence"
PORT_OPERATION_FACADE = "operation_facade"
PORT_PROVIDER_AUTHORITY = "provider_authority"
PORT_ALLOCATION_INVENTORY = "allocation_inventory"

# Why each port that cannot be composed in this image is absent, naming the owning
# work rather than describing the symptom. Surfaced verbatim in the capability
# readout so a failing preflight tells an operator which dependency to chase
# instead of only that something is missing.
#
# Keyed by port name and asserted against `API_CAPABILITY_PORTS` at import, so a
# port added to the registry without a reason here fails immediately rather than
# being reported as composed-and-unexplained.
BLOCKED_PORTS: dict[str, str] = {
    PORT_OPERATION_FACADE: (
        "harness_jobs is not present in this image (build context is release-owned, "
        "#5327) and has no production ApprovalSource or BudgetLedger to construct "
        "OperationFacadeService with; budget authority is domain-owned"
    ),
    PORT_PROVIDER_AUTHORITY: (
        "no shared implementation exists: harness_jobs has no source for run_id, "
        "submitter_id or handle.resource_name, which this port binds; owed by #5529"
    ),
    PORT_ALLOCATION_INVENTORY: (
        "harness_jobs.InventoryAuthority is field-compatible but the package is not "
        "present in this image (build context is release-owned, #5327)"
    ),
}


@dataclass(frozen=True)
class PortComposition:
    """What composition did about one port, and why.

    ``installed`` says an adapter is now behind the port's getter. It does **not**
    say the adapter works — that is the probe's answer, and conflating the two is
    the `is not None` check #5524 replaced.
    """

    port: str
    installed: bool
    detail: str
    preexisting: bool = False


@dataclass
class Composition:
    """The outcome of one composition pass, plus the transports it owns."""

    ports: dict[str, PortComposition] = field(default_factory=dict)
    _closeables: list[Any] = field(default_factory=list)

    @property
    def installed(self) -> frozenset[str]:
        """Ports with an adapter behind them, however it got there."""
        return frozenset(
            name for name, entry in self.ports.items() if entry.installed
        )

    @property
    def unconfigured(self) -> dict[str, str]:
        """Ports with no adapter, mapped to why — for the operator's next step."""
        return {
            name: entry.detail
            for name, entry in self.ports.items()
            if not entry.installed
        }

    def summary(self) -> dict[str, dict[str, object]]:
        """A JSON-safe readout, for the packaged commands' output."""
        return {
            name: {
                "installed": entry.installed,
                "detail": entry.detail,
                "preexisting": entry.preexisting,
            }
            for name, entry in sorted(self.ports.items())
        }

    async def aclose(self) -> None:
        """Release transports this composition opened.

        Deliberately does **not** uninstall the adapters. The ports' getters are
        process-global and a second startup in the same process (an autoreload, an
        embedding host, a test) must not find a half-composed port; leaving the
        installed object in place keeps `compose()` idempotent, which is the
        behaviour `test_composition_is_idempotent` pins.

        Each close is independent: one adapter failing to shut down must not leave
        the rest of them open, so a failure is logged and the loop continues rather
        than propagating out of a shutdown path.
        """
        while self._closeables:
            closeable = self._closeables.pop()
            try:
                await closeable.aclose()
            except Exception:
                # Never fatal, and never with the object's repr: an adapter's repr
                # can carry a base URL or a key.
                logger.warning(
                    "A composed adapter failed to close cleanly during shutdown",
                    exc_info=False,
                )


def _compose_credential_evidence(settings: Any, result: Composition) -> None:
    """Install the ADP vault client as the credential-evidence reader (#5528).

    Silent when nothing is configured: `build_vault_client` returns None for an
    unconfigured deployment and logs that itself, no reader is installed, and the
    provider-connection routes answer 503 "ADP vault evidence is unavailable" —
    the honest answer, as against a 403 that would blame the caller's permissions
    for a missing setting.
    """
    existing = get_credential_evidence_reader()
    if existing is not None:
        result.ports[PORT_CREDENTIAL_EVIDENCE] = PortComposition(
            port=PORT_CREDENTIAL_EVIDENCE,
            installed=True,
            detail="a credential-evidence reader was already installed; left in place",
            preexisting=True,
        )
        return

    client = build_vault_client(settings)
    if client is None:
        result.ports[PORT_CREDENTIAL_EVIDENCE] = PortComposition(
            port=PORT_CREDENTIAL_EVIDENCE,
            installed=False,
            detail=(
                "ADP vault is not configured: set ADP_GATEWAY_INTERNAL_URL and "
                "ADP_GATEWAY_INTERNAL_API_KEY"
            ),
        )
        return

    install_credential_evidence_reader(client)
    if hasattr(client, "aclose"):
        result._closeables.append(client)
    logger.info("Installed the ADP vault credential-evidence reader")
    result.ports[PORT_CREDENTIAL_EVIDENCE] = PortComposition(
        port=PORT_CREDENTIAL_EVIDENCE,
        installed=True,
        detail="composed the ADP vault credential-evidence reader",
    )


# Port -> (getter, the reason it cannot be composed here). Ports whose adapter is
# owned outside this image are recorded from their getter, so a host that injects
# one is reported as composed rather than as blocked.
_EXTERNAL_PORTS = (
    (PORT_OPERATION_FACADE, get_operation_facade),
    (PORT_PROVIDER_AUTHORITY, get_provider_authority_validator),
    (PORT_ALLOCATION_INVENTORY, get_allocation_inventory_reader),
)


def compose(settings: Any | None = None) -> Composition:
    """Build every adapter this deployment has configured, once.

    Reads configuration a single time, through the ``settings`` handed in (the
    process settings by default, so a caller can compose against an explicit
    configuration without mutating the global one).

    Returns the record rather than a boolean. Whether a port is *usable* is the
    probe's question, not this function's — see the module docstring.
    """
    if settings is None:
        from app.config import settings as process_settings

        settings = process_settings

    result = Composition()
    _compose_credential_evidence(settings, result)

    for port, getter in _EXTERNAL_PORTS:
        adapter = getter()
        if adapter is not None:
            # An embedding host or a contract test injected one. Report it rather
            # than claiming the port is blocked: the blocker is that *this image*
            # cannot build one, not that nothing may occupy the port.
            result.ports[port] = PortComposition(
                port=port,
                installed=True,
                detail="an adapter was already installed by the host; left in place",
                preexisting=True,
            )
            continue
        result.ports[port] = PortComposition(
            port=port, installed=False, detail=BLOCKED_PORTS[port]
        )

    absent = sorted(result.unconfigured)
    if absent:
        logger.info(
            "Trust adapters not composed in this process: %s", ", ".join(absent)
        )
    return result


if set(BLOCKED_PORTS) | {PORT_CREDENTIAL_EVIDENCE} != set(
    API_CAPABILITY_PORTS
):  # pragma: no cover - import-time guard
    # The registry and this module must name the same ports. Checked at import so a
    # port added to one and not the other fails immediately, rather than being
    # composed by nothing while the readout still accounts for four ports.
    raise RuntimeError("composition does not account for every API_CAPABILITY_PORTS")
