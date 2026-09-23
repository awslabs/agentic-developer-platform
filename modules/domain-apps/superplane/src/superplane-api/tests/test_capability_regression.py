"""A capability must go false again when its integration breaks. Issue #5535.

AC-02 has two directions, and the second is the one a boot-time-only check cannot
satisfy: *"capability results must pass only with real configured integrations and
regress to failure when an integration becomes unavailable (even after startup)."*

A gate that runs once at boot manufactures confidence for the rest of the pod's
life. These tests establish that both directions hold, and that nothing caches the
answer between calls.

Also here: liveness / control-plane readiness / workspace readiness are three
separate answers. The load-bearing case is
``test_control_plane_readiness_does_not_require_any_workspace_credential`` — a
control plane with zero workspaces must come up, so workspace credentials cannot
be a startup prerequisite.
"""

from __future__ import annotations

import asyncio

import pytest
from app.capability_probes import probe_port
from app.installation import capabilities_from


class _RefusingReader:
    """A correct adapter: refuses unauthorized input by returning None.

    ``NONE_MEANS_UNVERIFIED`` is what the registry declares for this port, so
    ``None`` is the refusal and returning it is what passing looks like.
    """

    async def read(self, **_kwargs):
        return None


class _UnreachableReader:
    """A configured adapter whose service has gone away after startup."""

    def __init__(self) -> None:
        self.calls = 0

    async def read(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("vault is unreachable")


class _AdmittingReader:
    """The dangerous adapter: returns something for wholly-unauthorized input."""

    async def read(self, **_kwargs):
        return object()


class _Placeholder:
    """An object that exists and implements nothing — what `is not None` accepted."""


@pytest.fixture
def reader(monkeypatch):
    """Install a reader for the duration of one test, then restore."""
    import app.services.credential_evidence as evidence

    def install(adapter):
        monkeypatch.setattr(evidence, "_reader", adapter)
        return adapter

    return install


class TestBothDirectionsOfAC02:
    """Pass only when really configured; regress when the integration breaks."""

    @pytest.mark.asyncio
    async def test_a_reachable_correct_adapter_passes(self, reader):
        reader(_RefusingReader())

        report = await probe_port("credential_evidence")

        assert report["composed"] is True
        assert report["verdicts"] == ["refused"]

    @pytest.mark.asyncio
    async def test_an_adapter_whose_service_died_fails(self, reader):
        """The regression direction, and the reason a boot-only gate is insufficient.

        The adapter is still installed and still configured — only its service is
        gone. The capability must not stay green on the strength of having passed
        once at startup.
        """
        reader(_UnreachableReader())

        report = await probe_port("credential_evidence")

        assert report["composed"] is False

    @pytest.mark.asyncio
    async def test_the_capability_transitions_within_one_process(self, reader):
        """Reachable, then unreachable, with no restart in between.

        A single process observing both answers is what proves nothing is cached at
        module or process scope. Asserting each state in its own test would pass even
        against an implementation that memoised the first verdict forever.
        """
        reader(_RefusingReader())
        healthy = await probe_port("credential_evidence")

        broken_adapter = reader(_UnreachableReader())
        broken = await probe_port("credential_evidence")

        assert healthy["composed"] is True
        assert broken["composed"] is False
        assert broken_adapter.calls > 0, "the probe did not re-call the adapter"

    @pytest.mark.asyncio
    async def test_it_recovers_when_the_service_returns(self, reader):
        """Failure must not latch either: a fixed vault must clear the capability.

        A latched failure is as wrong as a latched success — it would keep a
        recovered deployment permanently unready and send an operator looking for a
        fault that is already fixed.
        """
        reader(_UnreachableReader())
        assert (await probe_port("credential_evidence"))["composed"] is False

        reader(_RefusingReader())
        assert (await probe_port("credential_evidence"))["composed"] is True


class TestWhatMustNeverPass:
    """The fail-closed direction, per adapter defect."""

    @pytest.mark.asyncio
    async def test_an_adapter_that_admits_unauthorized_input_fails(self, reader):
        """The authorization bypass. Every probe value is a sentinel, so a correct
        adapter has nothing real to act on and returning a value cannot be right."""
        reader(_AdmittingReader())

        assert (await probe_port("credential_evidence"))["composed"] is False

    @pytest.mark.asyncio
    async def test_a_placeholder_that_passes_is_not_none_fails(self, reader):
        """The defect #5524 removed: an object that exists and implements nothing."""
        reader(_Placeholder())

        report = await probe_port("credential_evidence")

        assert report["composed"] is False
        assert report["verdicts"] == ["not_implemented"]

    @pytest.mark.asyncio
    async def test_an_uncomposed_port_fails(self, reader):
        reader(None)

        report = await probe_port("credential_evidence")

        assert report["composed"] is False
        assert report["detail"] == "no adapter installed"

    @pytest.mark.asyncio
    async def test_a_hanging_adapter_fails_rather_than_holding_the_gate(
        self, reader, monkeypatch
    ):
        """A hang establishes nothing, and must not stall a rollout indefinitely.

        The adapter sleeps far past any plausible timeout. The timeout itself is
        shortened via ``monkeypatch`` so this test is fast — via monkeypatch rather
        than a bare assignment with a ``finally``, because a shortened module-level
        timeout that escaped this test would make every later probe in the session
        time out spuriously. The production value is asserted separately below, so
        shortening it here cannot hide a bad default.
        """
        import app.capability_probes as probes

        class _Hanging:
            async def read(self, **_kwargs):
                await asyncio.sleep(60)

        reader(_Hanging())
        monkeypatch.setattr(probes, "PROBE_TIMEOUT_SECONDS", 0.05)

        report = await probe_port("credential_evidence")

        assert report["composed"] is False
        assert report["verdicts"] == ["timed_out"]

    def test_the_production_timeout_is_bounded(self):
        """Generous enough not to fail a slow adapter, short enough not to stall."""
        from app.capability_probes import PROBE_TIMEOUT_SECONDS

        assert 0 < PROBE_TIMEOUT_SECONDS <= 30

    def test_a_report_missing_its_verdict_is_read_as_false(self):
        """ "I could not tell" must fold to False; defaulting to True stops gating."""
        assert capabilities_from({"credential_evidence": {}}) == {
            "credential_evidence": False
        }


class TestThreeSeparateReadinessAnswers:
    """Liveness, control-plane readiness and workspace readiness are distinct."""

    def test_liveness_is_process_health_and_probes_no_integration(self):
        """`/health` must not consult the vault, the facade or the database.

        A liveness probe that fails when a dependency is down gets the pod killed
        and restarted for a fault a restart cannot fix — turning a degraded
        integration into a crash loop. `/health` reports posture it already holds.
        """
        import inspect

        from app.routers import health

        source = inspect.getsource(health.health_check)
        for forbidden in ("capabilities", "SELECT 1", "get_credential_evidence_reader"):
            assert forbidden not in source

    def test_liveness_and_control_plane_readiness_are_different_endpoints(self):
        """Collapsing them is what makes a dependency outage a restart loop."""
        from app.main import app

        paths = {route.path for route in app.routes}
        assert {"/health", "/readyz"} <= paths

    def test_readiness_reports_which_mode_it_answered_for(self):
        """A control plane and a full install are both "ready", for different reasons.

        An operator debugging a management-only installation needs to know the 200
        came from the management contract rather than from the full surface, because
        the two require different things to be true.
        """
        import inspect

        from app.routers import health

        returned = inspect.getsource(health.readiness).split("return")[-1]
        assert "mode" in returned and "management" in returned

    def test_control_plane_readiness_does_not_require_any_workspace_credential(self):
        """The control-plane-first requirement, as a property of `/readyz`.

        A control plane must start and become ready with **zero workspaces
        registered**. So readiness may require the management database and the
        org/auth contract, and must NOT require workspace credentials, a composed
        vault, or any capability probe — none of which a fresh installation has.
        """
        import inspect

        from app.routers import health

        source = inspect.getsource(health.readiness)
        for forbidden in (
            "capabilities",
            "get_credential_evidence_reader",
            "get_operation_facade",
            "provider_connection",
            "workspace",
        ):
            assert forbidden not in source, (
                f"/readyz consults {forbidden!r}; a control plane with zero "
                "workspaces would then never become ready"
            )

    def test_control_plane_readiness_requires_the_management_database(self):
        """It is not a no-op either: an unreachable database is not ready."""
        import inspect

        from app.routers import health

        assert "SELECT 1" in inspect.getsource(health.readiness)

    def test_workspace_readiness_is_never_inferred_from_control_plane_health(self):
        """`/readyz` must not report per-workspace state.

        A ready control plane says nothing about whether a particular workspace's
        provider is working. A client reading workspace health off `/readyz` would
        see every workspace as healthy the moment the process came up.
        """
        import inspect

        from app.routers import health

        returned = inspect.getsource(health.readiness).split("return")[-1]
        assert "workspace" not in returned


class TestEveryRuntimeCallerReachesTheComposedAdapter:
    """The wiring audit, as assertions rather than as a paragraph. Issue #5535.

    The design asks for an audit of every runtime caller and public endpoint. A
    written audit is true on the day it is written; these fail when it stops being
    true.
    """

    # (owning module, the module-global that holds the installed adapter).
    # A tuple, not a dict: nothing mutates it, and a mutable class attribute shared
    # across tests is a contamination source waiting for someone to append to it.
    PORT_MODULES = (
        ("app.services.credential_evidence", "_reader"),
        ("app.services.provisioning", "_facade"),
        ("app.services.provider_authority", "_validator"),
        ("app.services.provider_inventory", "_reader"),
    )

    def test_no_module_outside_a_ports_own_service_touches_its_global(self):
        """Every caller must go through the getter, so substitution is total.

        A module reading the global directly would bypass whatever the getter
        enforces, and would not see an adapter installed after its own import. The
        composition root is exempt: installing *is* its job.
        """
        import re
        from pathlib import Path

        app_root = Path(__file__).resolve().parents[1] / "app"

        # Two ports both name their global `_reader` (credential_evidence and
        # allocation_inventory), so a name alone cannot say which port a module is
        # touching. Every owning module is therefore exempt for that name — the
        # question here is whether a NON-owner reads a port global, not which port.
        owners = {
            app_root / Path(*module.split(".")[1:]).with_suffix(".py")
            for module, _attribute in self.PORT_MODULES
        }
        attributes = {attribute for _module, attribute in self.PORT_MODULES}

        offenders = []
        for source_file in sorted(app_root.rglob("*.py")):
            if source_file in owners or source_file == app_root / "composition.py":
                continue
            text = source_file.read_text()
            for attribute in sorted(attributes):
                # Matches both bypass shapes:
                #   * a bare `_reader` (the module's own global), and
                #   * a qualified `module._reader` (the shape a caller elsewhere
                #     would have to write, and the one an earlier version of this
                #     test missed entirely because its lookbehind excluded `.`).
                # `\w` before the name is still excluded, so this does not match
                # `get_credential_evidence_reader`.
                if re.search(rf"(?<!\w){re.escape(attribute)}\b", text):
                    offenders.append(f"{source_file.name} touches {attribute}")
        assert offenders == []

    def test_every_mounted_endpoint_has_a_recorded_authorization_decision(self):
        """A route absent from the inventory is a hole, not a default-deny.

        `app/domain_guard.py` refuses an uninventoried route, so an endpoint added
        without an entry would 500 rather than authorize — and until someone calls
        it, nothing says so.
        """
        from app.endpoint_inventory import RouteNotInventoried, classify
        from app.main import app

        # Catches only `RouteNotInventoried`, the refusal this test is about. A blanket
        # `except Exception` would silently absorb a TypeError or an import failure in
        # `classify` itself and report it as a missing inventory entry, sending the
        # reader to the inventory table when the bug is in the classifier.
        uninventoried = []
        for route in app.routes:
            for method in sorted(getattr(route, "methods", set()) or set()):
                if method in ("HEAD", "OPTIONS"):
                    continue
                try:
                    classify(method, route.path)
                except RouteNotInventoried:
                    uninventoried.append(f"{method} {route.path}")
        assert uninventoried == []

    def test_provisioning_has_no_fallback_when_the_facade_is_absent(self):
        """ "Facade unavailable, so provision directly" is the forbidden substitution.

        It is a *broader* authority being available exactly where the correct,
        narrower one could not be built — so the unconfigured case would end up more
        privileged than the configured one.
        """
        from app.services.provisioning import ProvisioningUnavailable, _require_facade

        with pytest.raises(ProvisioningUnavailable):
            _require_facade()

    def test_an_absent_evidence_reader_is_unavailable_and_not_a_denial(self):
        """503, never 403: a missing setting is not the caller's permissions.

        Reporting it as 403 sends an operator to check grants that are fine, while
        the actual fault — an unconfigured vault — goes unexamined.
        """
        import inspect

        from app.routers import provider_connections

        source = inspect.getsource(provider_connections._vault_evidence)
        assert "503" in source
