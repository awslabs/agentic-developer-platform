"""The single runtime composition, and the defect it removes. Issue #5535.

The load-bearing test here is
``test_the_packaged_capability_command_reflects_configuration``. Before #5535 the
packaged preflight and the API lifespan answered the same question differently,
because composition lived in ``app/main.py`` and the packaged entry point does not
import it. That test fails against the old arrangement and is the reason this file
exists; the rest pin the properties that keep the fix safe.
"""

from __future__ import annotations

import logging

import pytest
from app.composition import (
    HARNESS_PORTS,
    PORT_ALLOCATION_INVENTORY,
    PORT_CREDENTIAL_EVIDENCE,
    PORT_OPERATION_FACADE,
    PORT_PROVIDER_AUTHORITY,
    compose,
)


class _Configured:
    adp_gateway_internal_url = "https://gateway.internal"
    adp_gateway_internal_api_key = "internal-key"
    # No `database_url`, so the three harness-backed ports report "no operation
    # store configured" rather than composing. That is the point for most of this
    # file: it isolates the vault port. `TestTheHarnessPortsAreComposed` supplies
    # one explicitly.
    database_url = ""
    superplane_db_schema = ""


class _Unconfigured:
    adp_gateway_internal_url = ""
    adp_gateway_internal_api_key = ""
    database_url = ""
    superplane_db_schema = ""


class _WithOperationStore(_Configured):
    """Configured for the harness ports as well as the vault one.

    A real DSN shape, and deliberately one nothing listens on: composition must not
    connect (`test_the_pool_is_not_connected_by_composing`), so a reachable database
    is not needed to assert that the ports compose. Tests that need live SQL are in
    the harness adapter suites and use the real PostgreSQL fixture.

    The host is loopback and the port is explicit, and both matter. `app.schema_boundary`
    fails closed on transport: with no CA configured, the only thing that may connect
    unverified is a host it can establish is local, so an invented hostname like
    ``composition-test`` raises `DatabaseTransportUnverifiable` *during* composition.
    An earlier revision of this fixture used one, and every harness-port assertion
    below failed on the transport guard rather than on the behaviour under test — the
    port readout was even honest about it, which is how it was found. Port 1 is
    privileged and unused, so a connection attempt would be refused immediately
    rather than hanging, making a regression in "does not connect" fail fast.
    """

    database_url = "postgresql+asyncpg://composition-test@127.0.0.1:1/superplane"


@pytest.fixture(autouse=True)
def _no_preinstalled_adapters(monkeypatch):
    """Compose into an empty process, as a fresh container would.

    ``conftest`` imports ``app.main``, which must not install anything at import —
    but a previously-run test in the same session may have. Each test here starts
    from the uncomposed state so "left a pre-existing adapter alone" is a property
    this file can assert rather than inherit.

    All four ports, not just the vault one: since #5535 the other three are really
    composed, so a test that installed one would otherwise leak it into the next.
    """
    import app.services.credential_evidence as evidence
    import app.services.provider_authority as authority
    import app.services.provider_inventory as inventory
    import app.services.provisioning as provisioning

    monkeypatch.setattr(evidence, "_reader", None)
    monkeypatch.setattr(authority, "_validator", None)
    monkeypatch.setattr(inventory, "_reader", None)
    monkeypatch.setattr(provisioning, "_facade", None)


class TestTheDefect:
    """Configuration must reach the packaged command, not only the web app."""

    def test_the_packaged_capability_command_reflects_configuration(self, monkeypatch):
        """`app.installation` must compose, without importing `app.main`.

        This is the #5535 defect in one assertion. The installer runs
        ``python -m app.installation capabilities`` (``installation/runner.py:385``).
        That module does not import ``app.main``, where composition used to live, so
        the preflight reported every port uncomposed *regardless of configuration* —
        and since the installer requires all four ports true, no deployment could
        satisfy it. Here the two configurations must produce different answers.
        """
        import app.services.credential_evidence as evidence
        from app import installation

        monkeypatch.setattr(installation, "compose", lambda: compose(_Unconfigured()))
        unconfigured = compose(_Unconfigured())
        assert evidence.get_credential_evidence_reader() is None

        monkeypatch.setattr(evidence, "_reader", None)
        configured = compose(_Configured())
        assert evidence.get_credential_evidence_reader() is not None

        assert PORT_CREDENTIAL_EVIDENCE not in unconfigured.installed
        assert PORT_CREDENTIAL_EVIDENCE in configured.installed

    def test_the_packaged_command_does_not_import_the_web_application(self):
        """The reason the defect existed, pinned so it cannot silently return.

        If ``app.installation`` ever reaches composition *via* ``app.main``, the
        packaged command acquires a dependency on a web framework and on
        ``app.main``'s import-time work (it builds the domain policy at module
        scope). Either would make the preflight a different process than the one it
        is meant to certify.
        """
        import inspect

        from app import installation

        source = inspect.getsource(installation)
        assert "from app.composition import compose" in source
        assert "from app.main" not in source.split("management-capabilities")[0]

    def test_both_entry_points_call_the_same_factory(self):
        """One composition root, asserted by identity rather than by resemblance."""
        from app import composition, installation, main

        assert installation.compose is composition.compose
        assert main.compose is composition.compose


class TestCredentialEvidence:
    """The one port this image can compose."""

    def test_a_configured_deployment_composes_the_vault_reader(self):
        from app.adapters.adp_vault_client import AdpVaultClient
        from app.services.credential_evidence import get_credential_evidence_reader

        result = compose(_Configured())

        assert isinstance(get_credential_evidence_reader(), AdpVaultClient)
        assert result.ports[PORT_CREDENTIAL_EVIDENCE].installed is True
        assert result.ports[PORT_CREDENTIAL_EVIDENCE].preexisting is False

    def test_an_unconfigured_deployment_composes_nothing_and_says_why(self, caplog):
        """No stub, and a detail naming the missing settings.

        A permissive stub would be a bypass; one that refused every read would
        report a configuration gap as a per-credential denial. `None` makes the
        provider-connection routes answer 503 "unavailable", which is the truthful
        answer. The detail is what turns that 503 into an actionable next step.
        """
        from app.services.credential_evidence import get_credential_evidence_reader

        with caplog.at_level(logging.INFO):
            result = compose(_Unconfigured())

        assert get_credential_evidence_reader() is None
        assert result.ports[PORT_CREDENTIAL_EVIDENCE].installed is False
        assert (
            "ADP_GATEWAY_INTERNAL_URL" in result.ports[PORT_CREDENTIAL_EVIDENCE].detail
        )
        assert "Installed the ADP vault" not in caplog.text

    def test_it_does_not_displace_an_adapter_someone_else_installed(self, monkeypatch):
        """A substituted reader wins, and composition must not raise replacing it.

        ``install_credential_evidence_reader`` refuses a second install, so a
        composition that installed unconditionally would crash the lifespan of any
        host that had already composed one — and a version that forced the install
        would silently displace a deliberately substituted trust source.
        """
        import app.services.credential_evidence as evidence

        sentinel = object()
        monkeypatch.setattr(evidence, "_reader", sentinel)

        result = compose(_Configured())

        assert evidence.get_credential_evidence_reader() is sentinel
        assert result.ports[PORT_CREDENTIAL_EVIDENCE].preexisting is True

    def test_composition_is_idempotent(self):
        """Two startups in one process (autoreload, embedded host, CLI then server)."""
        from app.services.credential_evidence import get_credential_evidence_reader

        compose(_Configured())
        first = get_credential_evidence_reader()
        compose(_Configured())

        assert get_credential_evidence_reader() is first


class TestTheHarnessPortsAreComposed:
    """The three `harness_jobs`-backed ports, which used to be hardcoded blocked.

    A previous revision listed them in a `BLOCKED_PORTS` table and reported them
    absent *regardless of configuration*. Since the installer requires all four
    capabilities true, that made the gate unsatisfiable by any deployment — so the
    load-bearing assertion here is simply that a configured deployment composes
    them, which is what the old arrangement could not do.
    """

    @pytest.mark.parametrize("port", list(HARNESS_PORTS))
    def test_a_configured_operation_store_composes_the_port(self, port, monkeypatch):
        """Configured means composed. The old table made this unreachable."""
        result = compose(_WithOperationStore())

        assert result.ports[port].installed is True, result.ports[port].detail
        assert result.ports[port].preexisting is False
        assert result.installed >= {port}

    @pytest.mark.parametrize("port", list(HARNESS_PORTS))
    def test_without_an_operation_store_it_names_the_setting(self, port):
        """No stub either way, and a detail an operator can act on.

        The requirement the old table failed is not "report something" — it did
        that — but that the reason be *configuration* the operator controls rather
        than a property of the image they cannot change.
        """
        result = compose(_Configured())

        assert result.ports[port].installed is False
        assert "DATABASE_URL" in result.ports[port].detail

    def test_the_adapters_are_the_production_ones(self, monkeypatch):
        """Composed means the real adapter, not a placeholder that refuses.

        A stub refusing every call would pass the capability probe — the probe's
        pass condition *is* a refusal — so "the port is composed" has to be checked
        against the type as well. This is the assertion that would have caught the
        class of fix that satisfies the gate without integrating anything.
        """
        from app.adapters.harness_allocation_inventory import HarnessAllocationInventory
        from app.adapters.harness_operation_facade import HarnessOperationFacade
        from app.adapters.harness_provider_authority import HarnessProviderAuthority
        from app.services.provider_authority import get_provider_authority_validator
        from app.services.provider_inventory import get_allocation_inventory_reader
        from app.services.provisioning import get_operation_facade

        compose(_WithOperationStore())

        assert isinstance(get_operation_facade(), HarnessOperationFacade)
        assert isinstance(get_provider_authority_validator(), HarnessProviderAuthority)
        assert isinstance(get_allocation_inventory_reader(), HarnessAllocationInventory)

    def test_the_pool_is_not_connected_by_composing(self):
        """`compose()` must stay usable with no event loop and no network.

        The packaged preflight runs `python -m app.installation capabilities` under
        `--network=none`. A composition that connected would fail there instead of
        reporting a capability — and the capability it must report is established by
        the adapters refusing an unauthorized probe, which they do without a
        database.
        """
        result = compose(_WithOperationStore())

        assert result._connections is not None
        assert result._connections.opened is False

    def test_a_host_supplied_adapter_is_reported_as_composed(self, monkeypatch):
        """The blocker is that *this image* cannot build one, not that none may exist.

        The offline contract suites inject their own adapters. Reporting those as
        blocked would make the readout lie about the running process.
        """
        from app.services import provisioning

        monkeypatch.setattr(provisioning, "_facade", object())

        result = compose(_Configured())

        assert result.ports[PORT_OPERATION_FACADE].installed is True
        assert result.ports[PORT_OPERATION_FACADE].preexisting is True

    def test_nothing_is_composed_merely_by_importing(self):
        """The import-time rule, observed rather than simulated.

        A module-level install would give every test process and every unrelated CLI
        action a network dependency it never configured, and would make the ports'
        single-install guards unusable for substitution.
        """
        import sys

        import app.composition  # noqa: F401
        from app.services.credential_evidence import get_credential_evidence_reader

        assert "app.composition" in sys.modules
        assert get_credential_evidence_reader() is None


class TestShutdown:
    """Transports close, and the adapters this pass installed are released.

    This class used to assert the opposite — ``test_closing_does_not_uninstall_the
    _adapter`` — on the reasoning that a second startup "must not find a
    half-composed port". The reasoning inverted in practice: shutdown closed the
    vault client's connection pool and left the *closed* client installed, so the
    second lifespan found a fully composed port over a dead transport, every
    credential read failed, and the capability readout still said composed because
    installation is what it observes. It was also unrecoverable —
    ``install_credential_evidence_reader`` refuses a second install, so the second
    lifespan could not replace the broken reader it correctly declined to displace.
    """

    @pytest.mark.asyncio
    async def test_it_closes_transports_it_opened(self):
        closed: list[str] = []

        class _Closeable:
            async def aclose(self) -> None:
                closed.append("closed")

        result = compose(_Unconfigured())
        result._closeables.append(_Closeable())
        await result.aclose()

        assert closed == ["closed"]

    @pytest.mark.asyncio
    async def test_one_failing_close_does_not_abandon_the_others(self):
        """Shutdown is not a place to propagate: a leak is worse than a logged warning."""
        closed: list[str] = []

        class _Broken:
            async def aclose(self) -> None:
                raise RuntimeError("transport refused to close")

        class _Fine:
            async def aclose(self) -> None:
                closed.append("fine")

        result = compose(_Unconfigured())
        result._closeables.extend([_Fine(), _Broken()])
        await result.aclose()

        assert closed == ["fine"]

    @pytest.mark.asyncio
    async def test_closing_releases_the_adapters_it_installed(self):
        """The port is empty afterwards, so the next startup can compose into it."""
        from app.services.credential_evidence import get_credential_evidence_reader

        result = compose(_Configured())
        assert get_credential_evidence_reader() is not None

        await result.aclose()

        assert get_credential_evidence_reader() is None

    @pytest.mark.asyncio
    async def test_a_second_lifespan_gets_a_live_transport(self):
        """The defect itself, as an assertion: no reused closed client.

        Fails against the previous arrangement, where the second composition found
        the first one's closed client still installed and reported the port composed
        over a transport that could not answer.
        """
        from app.services.credential_evidence import get_credential_evidence_reader

        first = compose(_Configured())
        first_reader = get_credential_evidence_reader()
        await first.aclose()

        second = compose(_Configured())
        second_reader = get_credential_evidence_reader()

        assert second_reader is not None
        assert second_reader is not first_reader
        assert second.ports[PORT_CREDENTIAL_EVIDENCE].preexisting is False
        await second.aclose()

    @pytest.mark.asyncio
    async def test_repeated_startup_and_shutdown_stays_clean(self):
        """Three cycles, as an autoreload or an embedding host would drive it."""
        from app.services.credential_evidence import get_credential_evidence_reader

        seen = []
        for _ in range(3):
            result = compose(_WithOperationStore())
            assert len(result.installed) == 4, result.unconfigured
            seen.append(get_credential_evidence_reader())
            await result.aclose()
            assert get_credential_evidence_reader() is None

        # Every cycle built its own, so none of them is a survivor of an earlier
        # cycle's shutdown.
        assert len(set(map(id, seen))) == 3

    @pytest.mark.asyncio
    async def test_it_does_not_release_an_adapter_it_did_not_install(self, monkeypatch):
        """Shutdown releases only its own registrations.

        A composition that cleared the ports unconditionally would uninstall a
        test's substituted adapter, or a concurrently-live composition's — and
        because `compose()` correctly declines to displace a pre-existing adapter,
        the port would then be empty with nobody able to refill it.
        """
        import app.services.provisioning as provisioning

        sentinel = object()
        monkeypatch.setattr(provisioning, "_facade", sentinel)

        result = compose(_WithOperationStore())
        assert result.ports[PORT_OPERATION_FACADE].preexisting is True

        await result.aclose()

        assert provisioning.get_operation_facade() is sentinel

    @pytest.mark.asyncio
    async def test_it_leaves_a_port_another_object_has_taken_over(self, monkeypatch):
        """Someone replaced the adapter after composition. Not ours to remove.

        The uninstall helpers are identity-scoped, so this is a no-op rather than a
        clear. Asserted because the unconditional version of this code would delete
        a live adapter and log nothing about whose it was.
        """
        import app.services.provider_inventory as inventory

        result = compose(_WithOperationStore())
        assert result.ports[PORT_ALLOCATION_INVENTORY].installed is True

        replacement = object()
        inventory.set_allocation_inventory_reader(replacement)
        await result.aclose()

        assert inventory.get_allocation_inventory_reader() is replacement

    @pytest.mark.asyncio
    async def test_closing_twice_is_harmless(self):
        """Shutdown paths run twice under some failure orderings."""
        result = compose(_Configured())
        await result.aclose()
        await result.aclose()


class TestReadout:
    """The summary an operator reads."""

    def test_it_accounts_for_every_api_capability_port(self):
        """A port in the registry but not the readout would go silently unexplained."""
        from superplane_contracts import API_CAPABILITY_PORTS

        assert set(compose(_Configured()).summary()) == set(API_CAPABILITY_PORTS)

    def test_the_summary_is_json_safe(self):
        """The packaged command prints it; a non-serialisable value would crash it."""
        import json

        json.dumps(compose(_Configured()).summary())

    def test_unconfigured_maps_every_absent_port_to_a_reason(self):
        result = compose(_Unconfigured())

        assert set(result.unconfigured) == {
            PORT_CREDENTIAL_EVIDENCE,
            PORT_OPERATION_FACADE,
            PORT_PROVIDER_AUTHORITY,
            PORT_ALLOCATION_INVENTORY,
        }
        assert all(reason.strip() for reason in result.unconfigured.values())


class TestTheVaultTimeoutIsConfigurable:
    """The read bound is a deployment decision, not an image constant. Issue #5535.

    Before this, `AdpVaultClient._DEFAULT_TIMEOUT` was a module constant, so the only
    way to change the bound was to ship a new image. The bound belongs to the
    deployment's network: the value that is generous in one cluster is an outage in
    another.
    """

    def test_a_configured_timeout_reaches_the_client(self):
        class _WithTimeout(_Configured):
            adp_vault_timeout_seconds = 2.5

        compose(_WithTimeout())

        from app.services.credential_evidence import get_credential_evidence_reader

        assert get_credential_evidence_reader()._timeout == 2.5

    def test_settings_without_the_attribute_fall_back_to_the_safe_bound(self):
        """`_Configured` has no timeout attribute — an unbounded read is not the default.

        A read with no bound holds a request worker for as long as the vault stays
        silent, so a slow vault becomes an exhausted pool and an API-wide outage:
        much larger than the one unavailable credential the caller asked about.
        """
        from app.adapters.adp_vault_client import _DEFAULT_TIMEOUT
        from app.services.credential_evidence import get_credential_evidence_reader

        compose(_Configured())

        assert get_credential_evidence_reader()._timeout == _DEFAULT_TIMEOUT
        assert 0 < _DEFAULT_TIMEOUT <= 120

    @pytest.mark.parametrize("value", [0, -1, 0.0, 121, 10_000])
    def test_an_unusable_timeout_is_refused_at_startup(self, value):
        """Refused when settings are built, not on the first credential read.

        Zero or negative would time out immediately, turning every read into a
        spurious "vault unavailable" and reporting a healthy vault as broken. Failing
        lazily instead would surface as intermittent 503s under load, which read as a
        vault fault rather than as the setting that is actually wrong.
        """
        import pydantic
        from app.config import Settings

        with pytest.raises(pydantic.ValidationError):
            Settings(adp_vault_timeout_seconds=value)

    def test_a_usable_timeout_is_accepted(self):
        from app.config import Settings

        assert Settings(adp_vault_timeout_seconds=30).adp_vault_timeout_seconds == 30

    def test_the_two_defaults_agree(self):
        """The settings default and the adapter fallback must be the same number.

        They are declared in different modules, and nothing else would notice them
        drifting apart. If they did, the timeout a deployment actually got would
        depend on whether its settings object carried the attribute — so the same
        configuration would behave differently in the app and in the packaged
        command, which is the class of split-brain defect #5535 exists to remove.
        """
        import os

        from app.adapters.adp_vault_client import _DEFAULT_TIMEOUT

        os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/unused")
        from app.config import Settings

        assert Settings().adp_vault_timeout_seconds == _DEFAULT_TIMEOUT


async def test_current_identity_composition_shares_registered_producer_transport():
    from app.current_identity import MappedProducerIdentityReader

    class Configured(_WithOperationStore):
        superplane_operation_gateway_url = "https://gateway.example"
        superplane_operation_gateway_region = "us-east-1"

    result = compose(Configured())
    try:
        assert result.dispatcher is not None
        assert isinstance(result.identity_reader, MappedProducerIdentityReader)
        assert result.identity_reader.transport is result.dispatcher.transport
    finally:
        await result.aclose()
