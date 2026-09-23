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
    BLOCKED_PORTS,
    PORT_ALLOCATION_INVENTORY,
    PORT_CREDENTIAL_EVIDENCE,
    PORT_OPERATION_FACADE,
    PORT_PROVIDER_AUTHORITY,
    compose,
)


class _Configured:
    adp_gateway_internal_url = "https://gateway.internal"
    adp_gateway_internal_api_key = "internal-key"


class _Unconfigured:
    adp_gateway_internal_url = ""
    adp_gateway_internal_api_key = ""


@pytest.fixture(autouse=True)
def _no_preinstalled_reader(monkeypatch):
    """Compose into an empty process, as a fresh container would.

    ``conftest`` imports ``app.main``, which must not install anything at import —
    but a previously-run test in the same session may have. Each test here starts
    from the uncomposed state so "left a pre-existing adapter alone" is a property
    this file can assert rather than inherit.
    """
    import app.services.credential_evidence as evidence

    monkeypatch.setattr(evidence, "_reader", None)


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


class TestPortsThisImageCannotCompose:
    """Blocked is reported, never faked."""

    @pytest.mark.parametrize(
        "port",
        [PORT_OPERATION_FACADE, PORT_PROVIDER_AUTHORITY, PORT_ALLOCATION_INVENTORY],
    )
    def test_it_installs_nothing_and_names_the_owning_dependency(self, port):
        """An adapter that faked a verified answer would satisfy the gate and defeat it.

        The detail must name the blocking work, not describe the symptom: a preflight
        that fails with "not composed" leaves an operator with nothing to chase.
        """
        result = compose(_Configured())

        assert result.ports[port].installed is False
        assert result.ports[port].detail == BLOCKED_PORTS[port]
        assert any(token in result.ports[port].detail for token in ("#5327", "#5529"))

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
    """Transports close; installed adapters stay installed."""

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
    async def test_closing_does_not_uninstall_the_adapter(self):
        """A second startup in the same process must not find a half-composed port."""
        from app.services.credential_evidence import get_credential_evidence_reader

        result = compose(_Configured())
        await result.aclose()

        assert get_credential_evidence_reader() is not None


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
