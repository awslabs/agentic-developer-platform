"""Compose the API trust ports and owned transports without import-time effects.

Offline capability checks exercise refusal contracts without connecting. Lifespan
opens the shared operation store and starts the producer only after installation
checks. Unconfigured adapters report unavailable. Provider authority requires the
actual Gateway protected run mapping; a lease fence never substitutes for run ID.
Shutdown unregisters only adapters installed by this composition and closes its
owned transports once.
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
    uninstall_credential_evidence_reader,
)
from app.services.provider_authority import (
    get_provider_authority_validator,
    set_provider_authority_validator,
    uninstall_provider_authority_validator,
)
from app.services.provider_inventory import (
    get_allocation_inventory_reader,
    set_allocation_inventory_reader,
    uninstall_allocation_inventory_reader,
)
from app.services.provisioning import (
    get_operation_facade,
    set_operation_facade,
    uninstall_operation_facade,
)

logger = logging.getLogger(__name__)

PORT_CREDENTIAL_EVIDENCE = "credential_evidence"
PORT_OPERATION_FACADE = "operation_facade"
PORT_PROVIDER_AUTHORITY = "provider_authority"
PORT_ALLOCATION_INVENTORY = "allocation_inventory"

# The three ports backed by `harness_jobs`, all of which need the one operation
# store. Named as a group because they are composed or not composed together: they
# are three views of the same durable state, and a deployment with the facade but
# no inventory reader could admit operations it could never reconcile.
HARNESS_PORTS: tuple[str, ...] = (
    PORT_OPERATION_FACADE,
    PORT_PROVIDER_AUTHORITY,
    PORT_ALLOCATION_INVENTORY,
)

# Surfaced verbatim in the capability readout when no operation store is
# configured, so a failing preflight names the setting to change rather than only
# reporting that something is missing.
_NO_OPERATION_STORE = (
    "no PostgreSQL operation store is configured: set DATABASE_URL. The harness "
    "operation store backs this port"
)


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
    """The outcome of one composition pass, plus the transports it owns.

    ``_installed`` records, per port, the exact object this pass put behind it.
    That is what makes shutdown able to release only its own registrations — see
    ``aclose``. A pass that found an adapter already installed records nothing,
    because it owns nothing.
    """

    ports: dict[str, PortComposition] = field(default_factory=dict)
    _closeables: list[Any] = field(default_factory=list)
    _installed: dict[str, Any] = field(default_factory=dict)
    _connections: Any = None
    ledger: Any = None
    dispatcher: Any = None
    dispatch_enabled: bool = True

    @property
    def operation_connect(self) -> Any:
        if self._connections is None:
            raise RuntimeError("operation authority is not configured")
        return self._connections.connect

    @property
    def installed(self) -> frozenset[str]:
        """Ports with an adapter behind them, however it got there."""
        return frozenset(name for name, entry in self.ports.items() if entry.installed)

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

    async def aopen(self) -> None:
        """Connect the transports this composition built but did not open.

        Separate from `compose()` because composition is synchronous and runs where
        there is no event loop — see the module docstring. Called by the lifespan;
        deliberately *not* called by the packaged capability commands, whose whole
        point is to establish what the adapters refuse with no network.

        A store that cannot be reached is logged and left unopened rather than
        raised: the adapters are installed and answer their ports' unavailable
        outcome, which is a 503 an operator can diagnose. Failing the boot instead
        would take down the endpoints that do not need the operation store at all,
        including the readiness surface that would report the fault.
        """
        connections = self._connections
        if connections is None or connections.opened:
            return
        try:
            await connections.open()
            await connections.ensure_ready()
        except Exception:
            # No message and no repr: a DSN carries a password, and a schema
            # mismatch names a deployment version. `harness_connection` has
            # already logged the actionable form of both.
            logger.error(
                "the harness operation store is not ready; the operation facade, "
                "provider authority and allocation inventory ports will answer "
                "unavailable",
                exc_info=False,
            )
            await connections.aclose()

    async def aclose(self) -> None:
        """Release the adapters and transports **this** composition installed.

        ## The defect this repairs

        This method used to close its owned transports and deliberately leave every
        adapter installed, on the reasoning that a second startup "must not find a
        half-composed port". The result was the opposite of the intent: shutdown
        closed the vault client's connection pool and left the closed client behind
        the port, so a second lifespan in the same process found a *fully* composed
        port whose transport was dead. Every subsequent credential read failed, and
        the capability readout still reported the port composed, because
        installation is what it observes.

        Worse, it could not be recovered from. `install_credential_evidence_reader`
        refuses a second install, so the second lifespan's `compose()` saw a
        pre-existing reader, correctly declined to displace it, and had no way to
        replace the broken one.

        ## Why uninstall is identity-scoped

        Each `uninstall_*` takes the object to remove and is a no-op unless it is
        the installed one. So a composition releases what it installed and cannot
        remove an adapter a test, an embedding host, or a concurrently-live
        composition put there — which is the same rule `compose()` follows on the
        way in, applied on the way out.

        Each step is independent and nothing propagates: a shutdown path that
        raised would abandon the rest of its cleanup.
        """
        for port, adapter in list(self._installed.items()):
            uninstall = _UNINSTALL.get(port)
            if uninstall is None:
                continue
            try:
                if not uninstall(adapter):
                    # Another object holds the port now. Left exactly as it is:
                    # whoever installed it owns it.
                    logger.warning(
                        "the %s port no longer holds this composition's adapter; "
                        "leaving the installed one in place",
                        port,
                    )
            except Exception:
                logger.warning(
                    "the %s port could not be released during shutdown",
                    port,
                    exc_info=False,
                )
        self._installed.clear()

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
        self._connections = None
        self.ledger = None
        self.dispatcher = None

    def start_dispatcher(self) -> None:
        from app.operation_activation import dispatch_enabled

        if (
            self.dispatch_enabled
            and dispatch_enabled()
            and self.dispatcher is not None
            and self._connections is not None
            and self._connections.opened
        ):
            self.dispatcher.start()


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
    result._installed[PORT_CREDENTIAL_EVIDENCE] = client
    if hasattr(client, "aclose"):
        result._closeables.append(client)
    logger.info("Installed the ADP vault credential-evidence reader")
    result.ports[PORT_CREDENTIAL_EVIDENCE] = PortComposition(
        port=PORT_CREDENTIAL_EVIDENCE,
        installed=True,
        detail="composed the ADP vault credential-evidence reader",
    )


# Port -> its getter, used to detect an adapter a host or test already installed.
_GETTERS: dict[str, Any] = {
    PORT_CREDENTIAL_EVIDENCE: get_credential_evidence_reader,
    PORT_OPERATION_FACADE: get_operation_facade,
    PORT_PROVIDER_AUTHORITY: get_provider_authority_validator,
    PORT_ALLOCATION_INVENTORY: get_allocation_inventory_reader,
}

# Port -> the identity-scoped release used at shutdown. See `Composition.aclose`.
_UNINSTALL: dict[str, Any] = {
    PORT_CREDENTIAL_EVIDENCE: uninstall_credential_evidence_reader,
    PORT_OPERATION_FACADE: uninstall_operation_facade,
    PORT_PROVIDER_AUTHORITY: uninstall_provider_authority_validator,
    PORT_ALLOCATION_INVENTORY: uninstall_allocation_inventory_reader,
}


def _preexisting(port: str, result: Composition) -> bool:
    """Record a host-installed adapter and report whether there was one.

    A pre-installed adapter is never displaced — see the module docstring's second
    rule — and is never recorded in `_installed`, because this pass does not own it
    and must not release it at shutdown.
    """
    if _GETTERS[port]() is None:
        return False
    result.ports[port] = PortComposition(
        port=port,
        installed=True,
        detail="an adapter was already installed by the host; left in place",
        preexisting=True,
    )
    return True


def _compose_harness_ports(settings: Any, result: Composition) -> None:
    """Compose the three `harness_jobs`-backed ports over one operation store.

    All three or none: they are three views of the same durable state, and a
    deployment holding the facade without the inventory reader could admit
    operations it has no authorized way to reconcile.

    The domain supplies what the harness refuses to — the approval source and the
    budget ledger, which `OperationFacadeService.__post_init__` requires and has no
    safe default for, and the `authenticate` callable `InventoryAuthority` requires
    for the same stated reason: "a package that could mint the credential it checks
    is a package whose authority check is decorative."
    """
    from app.adapters.harness_connection import build_harness_connections

    outstanding = [port for port in HARNESS_PORTS if not _preexisting(port, result)]
    if not outstanding:
        return

    connections = build_harness_connections(settings)
    if connections is None:
        for port in outstanding:
            result.ports[port] = PortComposition(
                port=port, installed=False, detail=_NO_OPERATION_STORE
            )
        return

    # Imported here rather than at the top of this function, and deliberately
    # *after* the unconfigured check: `app.database` raises `DatabaseURLMissing` at
    # import when no DSN is set, and the adapter modules reach it through the
    # models. Importing eagerly would turn "no operation store configured" — a
    # supported state with an accurate readout — into a composition failure whose
    # detail told an operator to read the logs instead of naming the setting.
    from harness_jobs.facade import OperationFacadeService
    from harness_jobs.inventory import InventoryAuthority
    from harness_jobs.store import OperationStore

    from app.adapters.harness_allocation_inventory import HarnessAllocationInventory
    from app.adapters.harness_execution_authority import HarnessExecutionAuthority
    from app.adapters.harness_operation_facade import HarnessOperationFacade
    from app.adapters.harness_provider_authority import HarnessProviderAuthority
    from app.adapters.operation_authority_source import GrantBackedAuthority
    from app.adapters.operation_budget_ledger import OperationBudgetLedger
    from app.database import async_session_factory

    connect = connections.connect
    store = OperationStore()
    authority_source = GrantBackedAuthority(async_session_factory)
    ledger = OperationBudgetLedger(
        connect, limits_for=authority_source.budget_limits_for
    )
    execution = HarnessExecutionAuthority(connect, store=store)

    # The pool is owned by the composition, not by any one adapter: three adapters
    # share it, so the *last* of them closing it would close it under the other
    # two. `aclose` releases it once, after every port is released.
    result._connections = connections
    result._closeables.append(connections)
    result.ledger = ledger
    result.dispatch_enabled = (
        getattr(settings, "superplane_operation_dispatch_enabled", True) is True
    )
    endpoint = getattr(settings, "superplane_operation_gateway_url", "")
    if endpoint:
        from app.adapters.operation_dispatch import (
            OperationDispatcher,
            ProducerTransport,
        )

        result.dispatcher = OperationDispatcher(
            connect,
            transport=ProducerTransport(
                endpoint, getattr(settings, "superplane_operation_gateway_region", "")
            ),
            enabled=result.dispatch_enabled,
        )
        result._closeables.append(result.dispatcher)
        execution._verify_run = result.dispatcher.verify_run

    async def activation_ready():
        from app.installation import prepared_lifecycle_binding

        return await prepared_lifecycle_binding(result)

    adapters: dict[str, Any] = {}
    if PORT_OPERATION_FACADE in outstanding:
        adapters[PORT_OPERATION_FACADE] = HarnessOperationFacade(
            enabled=result.dispatch_enabled,
            activation_verify=activation_ready,
            lifecycle_verify=result.dispatcher.binding_ready
            if result.dispatcher
            else None,
            service=OperationFacadeService(
                connect=connect,
                resolver=authority_source,
                approvals=authority_source,
                ledger=ledger,
                store=store,
            ),
        )
    if PORT_PROVIDER_AUTHORITY in outstanding:
        adapters[PORT_PROVIDER_AUTHORITY] = HarnessProviderAuthority(execution)
    if PORT_ALLOCATION_INVENTORY in outstanding:
        adapters[PORT_ALLOCATION_INVENTORY] = HarnessAllocationInventory(
            InventoryAuthority(
                connect=connect,
                authenticate=execution.authenticate,
                store=store,
            )
        )

    installers: dict[str, Any] = {
        PORT_OPERATION_FACADE: set_operation_facade,
        PORT_PROVIDER_AUTHORITY: set_provider_authority_validator,
        PORT_ALLOCATION_INVENTORY: set_allocation_inventory_reader,
    }
    for port, adapter in adapters.items():
        installers[port](adapter)
        result._installed[port] = adapter
        result.ports[port] = PortComposition(
            port=port,
            installed=True,
            detail="composed over the harness_jobs operation store",
        )
    logger.info(
        "Installed the harness_jobs trust adapters: %s", ", ".join(sorted(adapters))
    )


def _failure_detail(failure: BaseException) -> str:
    """The operator-facing reason a composition pass failed.

    Two configuration faults carry messages that name the setting to change and are
    written to be credential-free by the modules that raise them —
    `DatabaseTransportUnverifiable` ("SUPERPLANE_DATABASE_CA is not set...") and
    `DatabaseURLMissing`. Those are passed through, because a readout that said
    "see the logs" for a missing environment variable would send an operator
    hunting for a stack trace to learn a variable name.

    Everything else is summarized by type only. An arbitrary exception's message is
    not known to be credential-free — an asyncpg connection error quotes the DSN,
    and a DSN carries a password — and this string is returned by the capability
    endpoint.
    """
    from app.config import DatabaseURLMissing
    from app.schema_boundary import DatabaseTransportUnverifiable

    if isinstance(failure, DatabaseTransportUnverifiable | DatabaseURLMissing):
        return str(failure)
    return (
        f"the harness_jobs adapters could not be composed "
        f"({type(failure).__name__}); see the API logs for the failure"
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

    try:
        _compose_harness_ports(settings, result)
    except Exception as failure:
        # Reported, never raised. An import failure or a malformed database URL
        # must not take down the whole process: the endpoints that need no
        # operation store keep working, the three ports answer unavailable, and
        # the readiness surface that reports the fault stays up to report it.
        #
        # Any port left unaccounted for by the partial pass is filled in below, so
        # a failure midway through cannot produce a readout missing a port.
        logger.exception("the harness_jobs trust adapters could not be composed")
        detail = _failure_detail(failure)
        for port in HARNESS_PORTS:
            result.ports.setdefault(
                port,
                PortComposition(port=port, installed=False, detail=detail),
            )

    absent = sorted(result.unconfigured)
    if absent:
        logger.info(
            "Trust adapters not composed in this process: %s", ", ".join(absent)
        )
    return result


if set(_GETTERS) != set(API_CAPABILITY_PORTS) or set(_UNINSTALL) != set(
    API_CAPABILITY_PORTS
):  # pragma: no cover - import-time guard
    # The registry and this module must name the same ports, and every port must
    # have both a getter and a release. Checked at import so a port added to one
    # table and not the others fails immediately, rather than being composed by
    # nothing — or, worse, installed and never released — while the readout still
    # accounts for four ports.
    raise RuntimeError("composition does not account for every API_CAPABILITY_PORTS")
