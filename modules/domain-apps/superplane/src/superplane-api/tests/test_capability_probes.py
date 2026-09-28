"""A capability is established by exercising the adapter, not by its existence.

Issue #5524 (w6-01), EPIC #4910, Wave 6. Design requirement 3: "The real
capability check must exercise the configured production adapter, not merely test
that an object exists."

## The test that matters most

``test_a_placeholder_that_passes_is_not_none_is_refused``. Every other test here
supports it. The old check was four ``is not None`` tests, so an object with no
methods reported four green capabilities and the installer, the boot gate and the
post-rollout recheck all treated that as evidence the image was composed for
production. That object is built below and required to fail.

The rest of the file covers the ways a wrong adapter can be wrong — approves
everything, returns a value for a forged workspace, hangs, raises
``NotImplementedError`` — plus the four existing consumers' contracts, which must
keep their current meaning: the CLI's exit 2 and output shape, the boot gate's
refusal, the router's exactly-four-booleans, and the fail-closed direction
throughout.
"""

import asyncio
import json
from enum import Enum

import pytest
from app import capability_probes, installation
from app.services import (
    credential_evidence,
    provider_authority,
    provider_inventory,
    provisioning,
)
from app.services.provisioning import ProvisioningRefused, ProvisioningUnavailable
from superplane_contracts import (
    CONFORMANCE_LIMITATION,
    SMOKE_LIMITATION,
    ProbeKind,
    ProbeVerdict,
    declared_kinds,
)
from superplane_contracts.conformance import PROBE_OPERATION_ID, PROBE_WORKSPACE

# Each port's installer and the method a probe calls on it. Driving the tests from
# this table rather than writing four near-identical tests means a port added to
# `capability_probes` without a test here fails the coverage assertion at the end.
PORTS = {
    "credential_evidence": ("read", credential_evidence, "_reader"),
    "provider_authority": ("resolve", provider_authority, "_validator"),
    "allocation_inventory": ("read", provider_inventory, "_reader"),
    "operation_facade": ("report_progress", provisioning, "_facade"),
}


@pytest.fixture(autouse=True)
def _restore_adapters():
    """Install and remove adapters around each test.

    These ports are module globals, and `install_credential_evidence_reader`
    refuses to overwrite an installed reader — correctly, since a production
    composition must not be silently replaced. So the tests set the globals
    directly and restore them, rather than going through installers that are
    deliberately one-shot.
    """
    saved = {
        name: getattr(module, attribute)
        for name, (_, module, attribute) in PORTS.items()
    }
    yield
    for name, (_, module, attribute) in PORTS.items():
        setattr(module, attribute, saved[name])


def _install(adapter) -> None:
    """Install one adapter object behind all four ports."""
    for _, module, attribute in PORTS.values():
        setattr(module, attribute, adapter)


def _unconfigured(monkeypatch) -> None:
    """Make this process's settings configure no adapter at all.

    Needed because ``installation.capability_details`` composes before it probes —
    that IS the #5535 fix, and without it the packaged preflight probed an
    uncomposed process and reported every port absent regardless of configuration.
    The consequence here is that ``_install(None)`` alone no longer produces an
    uncomposed process: ``conftest`` sets ``DATABASE_URL``, so composition
    immediately installs the three real harness-backed adapters, and they correctly
    refuse the probe and report composed.

    So "nothing is installed" has to be expressed as what it actually is — a
    deployment that configured nothing — rather than as a global someone cleared.
    Clearing the globals and asserting False would have tested that composition is
    broken.

    ``monkeypatch.setattr`` on the settings object, and only for the duration of the
    test: ``compose()`` with no argument reads the process settings, which is the
    path the CLI and the boot gate take, and that path is the one under test.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "", raising=False)
    monkeypatch.setattr(settings, "adp_gateway_internal_url", "", raising=False)
    monkeypatch.setattr(settings, "adp_gateway_internal_api_key", "", raising=False)
    _install(None)


class _Placeholder:
    """An object that exists and implements nothing.

    The exact thing ``is not None`` admitted.
    """


class _ApprovesEverything:
    """Returns a usable result for any input, including unauthorized input."""

    async def read(self, **kwargs):
        return {"approved": True, "workspace": PROBE_WORKSPACE}

    async def resolve(self, *args, **kwargs):
        return {"approved": True}

    async def report_progress(self, *args, **kwargs):
        return {"state": "succeeded", "operation_id": PROBE_OPERATION_ID}


class _Stub:
    """Has the methods, but they are unimplemented.

    ``raise NotImplementedError`` is the placeholder's signature, and it must not
    be mistaken for a refusal.
    """

    async def read(self, **kwargs):
        raise NotImplementedError

    async def resolve(self, *args, **kwargs):
        raise NotImplementedError

    async def report_progress(self, *args, **kwargs):
        raise NotImplementedError


class _Correct:
    """Refuses unauthorized input the way each port's contract requires.

    ``None`` for the three ``NONE_MEANS_UNVERIFIED`` readers; raising for the
    facade, whose contract is ``RAISE_UNAVAILABLE``.
    """

    async def read(self, **kwargs):
        return None

    async def resolve(self, *args, **kwargs):
        return None

    async def report_progress(self, *args, **kwargs):
        raise ProvisioningRefused("no such operation")


class _Hangs:
    """Never returns. A boot gate must not wait on it forever."""

    async def read(self, **kwargs):
        await asyncio.sleep(3600)

    async def resolve(self, *args, **kwargs):
        await asyncio.sleep(3600)

    async def report_progress(self, *args, **kwargs):
        await asyncio.sleep(3600)


class TestTheDefectThisStoryFixes:
    """An adapter must answer correctly, not merely be present."""

    async def test_a_placeholder_that_passes_is_not_none_is_refused(self):
        """The whole point of the change.

        Asserted alongside the ``is not None`` test it replaces, so the two are
        visibly not the same question: the placeholder satisfies the old check and
        fails the new one.
        """
        placeholder = _Placeholder()
        _install(placeholder)

        # Exactly what the previous implementation asked, and it still says yes.
        assert credential_evidence.get_credential_evidence_reader() is not None
        assert provider_authority.get_provider_authority_validator() is not None
        assert provider_inventory.get_allocation_inventory_reader() is not None
        assert provisioning.get_operation_facade() is not None

        capabilities = await installation.capabilities_async()
        assert not any(capabilities.values()), (
            "an object that implements nothing reported as composed; this is the "
            "defect the story exists to fix"
        )

    async def test_the_placeholders_failure_is_reported_as_not_implemented(self):
        """Named precisely, so the remedy is obvious from the receipt.

        "Not implemented" sends the implementer to write the method. A generic
        "refused" would send them to debug their authorization logic.
        """
        _install(_Placeholder())
        details = await installation.capability_details()
        for name, report in details.items():
            assert report["composed"] is False, name
            assert ProbeVerdict.NOT_IMPLEMENTED.value in report["verdicts"], name

    async def test_an_adapter_that_approves_everything_is_refused(self):
        """A positive-only suite would pass this adapter, because everything passes."""
        _install(_ApprovesEverything())
        capabilities = await installation.capabilities_async()
        assert not any(capabilities.values())

    async def test_an_approve_all_adapters_failure_is_reported_as_admitted(self):
        _install(_ApprovesEverything())
        details = await installation.capability_details()
        for name, report in details.items():
            assert ProbeVerdict.ADMITTED.value in report["verdicts"], name

    async def test_a_stub_raising_notimplementederror_is_refused(self):
        """Having the method is not implementing it.

        This is the case a "does it have the attribute?" check would pass, and it
        is only one step less empty than the placeholder.
        """
        _install(_Stub())
        capabilities = await installation.capabilities_async()
        assert not any(capabilities.values())
        details = await installation.capability_details()
        for name, report in details.items():
            assert ProbeVerdict.NOT_IMPLEMENTED.value in report["verdicts"], name


class TestUncomposedStaysRefused:
    """The existing fail-closed behaviour is preserved, not relaxed.

    Since #5535 composed the remaining three ports, "uncomposed" means a deployment
    that configured nothing — see `_unconfigured`. The property is unchanged and is
    the one that matters for the installer: an image that was given no configuration
    reports no capability, and cannot be installed.
    """

    async def test_no_adapter_installed_reports_no_capability(self, monkeypatch):
        """An unconfigured deployment must keep refusing, on every port."""
        _unconfigured(monkeypatch)
        capabilities = await installation.capabilities_async()
        assert capabilities == dict.fromkeys(PORTS, False)

    async def test_the_absent_case_says_so_plainly(self, monkeypatch):
        _unconfigured(monkeypatch)
        details = await installation.capability_details()
        for name, report in details.items():
            assert report["detail"] == "no adapter installed", name

    async def test_the_readout_names_the_setting_that_is_missing(self, monkeypatch):
        """``detail`` says no adapter; ``composition`` says why there is none.

        The distinction is the operator's next step. "No adapter installed" alone
        leaves them reading source to find out which variable to set, and it reads
        identically for a misconfigured deployment and a broken image. Composition's
        own explanation names the setting, and it cannot inflate a capability because
        ``composed`` stays the probe's answer — asserted False here alongside it.
        """
        _unconfigured(monkeypatch)
        details = await installation.capability_details()
        for name, report in details.items():
            assert report["composed"] is False, name
            explanation = report["composition"]["detail"]
            assert "DATABASE_URL" in explanation or "ADP_GATEWAY_INTERNAL_URL" in (
                explanation
            ), (name, explanation)

    async def test_an_adapter_missing_the_probed_method_is_refused(self):
        """Partial implementations fail on the part they are missing."""

        class OnlyOneMethod:
            async def read(self, **kwargs):
                return None

        _install(OnlyOneMethod())
        capabilities = await installation.capabilities_async()
        # The two `read` ports are satisfied; `resolve` and `report_progress` are not.
        assert capabilities["credential_evidence"] is True
        assert capabilities["allocation_inventory"] is True
        assert capabilities["provider_authority"] is False
        assert capabilities["operation_facade"] is False


class TestCorrectAdaptersAreAccepted:
    """The gate must not fail composed images, or it will be removed."""

    async def test_an_adapter_that_refuses_correctly_reports_composed(self):
        _install(_Correct())
        capabilities = await installation.capabilities_async()
        assert all(capabilities.values()), capabilities

    async def test_a_correct_adapter_is_composed_but_never_reported_conformant(self):
        """F2, as an assertion. This test previously required the false claim.

        It asserted ``conformant is True`` for every port on a correct adapter, and
        the production entry point duly reported it — which is exactly the finding:
        one composite call presented as passing every distinct conformance probe. The
        boot gate has no valid control request to vary against (offline, no
        provisioned tenant, deliberately no credential), so it cannot isolate any
        individual authorization rule and must not claim to.

        ``composed`` stays True, because that claim is honest and the installer
        depends on it: a real implementation is installed and it refused a
        wholly-unauthorized request in its declared shape. ``conformant`` is the
        seeded offline suite's claim to make, not this gate's.
        """
        _install(_Correct())
        details = await installation.capability_details()
        for name, report in details.items():
            assert report["composed"] is True, (name, report["failed"])
            assert report["conformant"] is False, (
                f"{name}: the boot gate reported verified conformance; it ran no "
                "valid control, so no authorization rule was isolated"
            )
            assert report["isolated"] is False, name

    async def test_the_boot_gate_names_every_dimension_it_did_not_verify(self):
        """The honest readout: one probe run, everything else explicitly unproven.

        Silence here would read as coverage. An operator quoting a green boot gate
        must be able to see, from the report itself, that the workspace, operation,
        authority and permission rules were never individually exercised.
        """
        _install(_Correct())
        details = await installation.capability_details()
        for name, report in details.items():
            assert len(report["exercised"]) == 1, name
            assert report["exercised"] == [ProbeKind.UNKNOWN_MUST_NOT_SUCCEED.value], (
                name
            )
            # Derived from the registry rather than hand-listed: each port declares a
            # different obligation set (credential_evidence binds no operation
            # identity, for instance), and the invariant is that *every* declared kind
            # except the single one actually run is named as unexercised. Nothing may
            # be silently dropped, which is what would let a dimension read as covered.
            declared = {item.value for item in declared_kinds(name)}
            assert set(report["not_exercised"]) == declared - set(
                report["exercised"]
            ), name
            assert ProbeKind.VALID_CONTROL.value in set(report["not_exercised"]), name

    async def test_the_boot_gate_calls_each_adapter_exactly_once(self):
        """One probe per port, and the request is sent as itself — never varied.

        Varying a field of a request already unauthorized in every other field was
        the mechanism of F2. The smoke tier does not attempt it, and this asserts the
        adapter is not called repeatedly with pseudo-variations that would invite the
        report to attribute refusals per dimension again.
        """
        calls: list[str] = []

        class Recording(_Correct):
            async def read(self, **kwargs):
                calls.append("read")
                return await super().read(**kwargs)

            async def resolve(self, *args, **kwargs):
                calls.append("resolve")
                return await super().resolve(*args, **kwargs)

            async def report_progress(self, *args, **kwargs):
                calls.append("report_progress")
                return await super().report_progress(*args, **kwargs)

        _install(Recording())
        await installation.capability_details()
        # One object is installed behind all four ports, so this counts calls across
        # them: two readers plus one validator plus one facade, one probe each.
        assert len(calls) == len(PORTS), calls
        assert sorted(calls) == ["read", "read", "report_progress", "resolve"], calls

    async def test_an_adapter_that_crashes_offline_is_not_composed(self):
        """A connection error is not a refusal, and must not pass the gate.

        An earlier revision of this module tolerated it, reasoning that the
        ``--network=none`` preflight would otherwise reject genuinely composed
        images. That reasoning was wrong, and this test is the record of why: every
        port's contract already declares what to answer when it cannot establish the
        truth — return ``None``, return an unresolved value, or raise its declared
        unavailable error. An adapter that lets ``OSError`` escape instead has not
        answered at all, so nothing was established about what it refuses.

        Tolerating it meant an adapter that crashed on *every* call reported all four
        capabilities as composed, which is the manufactured confidence this whole
        module exists to remove.
        """

        class CrashesOffline:
            async def read(self, **kwargs):
                raise OSError("Name or service not known")

            async def resolve(self, *args, **kwargs):
                raise OSError("Name or service not known")

            async def report_progress(self, *args, **kwargs):
                raise OSError("Name or service not known")

        _install(CrashesOffline())
        capabilities = await installation.capabilities_async()
        assert not any(capabilities.values()), (
            "an adapter that crashes on every call reported as composed; this was "
            "the defect the offline leniency created"
        )
        details = await installation.capability_details()
        # The three readers owed `None` and raised: wrong shape. The facade's
        # contract *is* to raise, but `OSError` is a fault rather than its declared
        # refusal, so it is FAILED rather than accepted.
        assert (
            ProbeVerdict.WRONG_REFUSAL_SHAPE.value
            in (details["credential_evidence"]["verdicts"])
        )
        assert ProbeVerdict.FAILED.value in details["operation_facade"]["verdicts"]
        for name, report in details.items():
            assert report["composed"] is False, name
            assert report["conformant"] is False, name

    async def test_an_offline_adapter_answering_its_contract_is_composed(self):
        """The offline case the leniency was meant to protect, done correctly.

        This is the other half of the test above: an adapter that cannot reach its
        vault and *says so the way its contract requires* still passes with no
        network. So dropping the leniency does not make the preflight reject correct
        images — it only rejects adapters that fail to answer.
        """

        class OfflineButAnswers:
            async def read(self, **kwargs):
                # Contract: None means unverified. Unreachable vault, said properly.
                return None

            async def resolve(self, *args, **kwargs):
                return None

            async def report_progress(self, *args, **kwargs):
                # Contract: RAISE_UNAVAILABLE. A declared refusal, not a fault.
                raise ProvisioningUnavailable("facade unreachable")

        _install(OfflineButAnswers())
        capabilities = await installation.capabilities_async()
        assert all(capabilities.values()), capabilities

    async def test_notimplementederror_is_not_given_that_leniency(self):
        """The one exception, and the reason the leniency is safe.

        A stub raises too, and if raising were uniformly tolerated the placeholder
        would slip through the same door opened for the offline case.
        """
        _install(_Stub())
        assert not any((await installation.capabilities_async()).values())


class TestProbesAreSafeToRunOnEveryBoot:
    """No mutation, no spend, no unbounded wait."""

    async def test_a_hanging_adapter_does_not_hold_the_gate_open(self, monkeypatch):
        """Bounded, because this runs in a boot gate and an installer preflight."""
        monkeypatch.setattr(capability_probes, "PROBE_TIMEOUT_SECONDS", 0.05)
        _install(_Hangs())
        capabilities = await asyncio.wait_for(
            installation.capabilities_async(), timeout=10
        )
        assert set(capabilities) == set(PORTS)

    async def test_a_hanging_adapter_is_not_composed(self, monkeypatch):
        """A hang produced no answer, so it establishes nothing.

        Separate from the bounded-wait test above because they are different claims,
        and only one of them was made before: the earlier revision asserted the gate
        returned promptly but deliberately did not assert the capability was false —
        and it was true, because a timeout was classified as a merely wrong-shaped
        refusal and wrong-shaped refusals passed. An adapter that never returns was
        therefore reported as composed for production.
        """
        monkeypatch.setattr(capability_probes, "PROBE_TIMEOUT_SECONDS", 0.05)
        _install(_Hangs())
        capabilities = await installation.capabilities_async()
        assert not any(capabilities.values()), (
            "a hanging adapter reported as composed; a call that never answered is "
            "not evidence of a refusal"
        )
        details = await installation.capability_details()
        for name, report in details.items():
            assert ProbeVerdict.TIMED_OUT.value in report["verdicts"], name
            assert report["composed"] is False, name

    async def test_the_facade_is_never_asked_to_open_an_operation(self):
        """Probing with ``open_operation`` would provision on every boot.

        Asserted by installing a facade that records its calls, because this is the
        one probe that could plausibly be written as a mutation — "open an operation
        and see if it refuses" is a tempting and very expensive check.
        """
        calls: list[str] = []

        class Recording:
            async def open_operation(self, **kwargs):
                calls.append("open_operation")
                raise AssertionError("the probe must never open an operation")

            async def report_progress(self, *args, **kwargs):
                calls.append("report_progress")
                raise ProvisioningRefused("no such operation")

        provisioning._facade = Recording()
        report = await capability_probes.probe_port("operation_facade")
        # One call per probe, so the count follows the exercised probe set rather
        # than being 1 — what matters is that every call was `report_progress` and
        # none was `open_operation`.
        assert calls, "the probe called nothing"
        assert set(calls) == {"report_progress"}
        assert len(calls) == report["probes"]

    async def test_probes_present_only_sentinel_identifiers(self):
        """So a correct adapter has nothing real to act on.

        Every value the probe passes is recorded and checked to be a sentinel. If a
        probe ever passed a real workspace or allocation id, a correct adapter might
        legitimately act on it — which is how a safety check becomes an outage.
        """
        seen: list[object] = []

        class Recording:
            # `return None` is this port's declared refusal, not a redundant
            # statement: under NONE_MEANS_UNVERIFIED it is what a correct adapter
            # answers. Spelled out so the fixture reads as a conforming adapter.
            async def read(self, **kwargs):
                seen.extend(kwargs.values())
                return None  # noqa: RET501, PLR1711

            async def resolve(self, authority, **kwargs):
                seen.append(authority)
                seen.extend(kwargs.values())
                return None  # noqa: RET501, PLR1711

            async def report_progress(self, operation_id, *args, **kwargs):
                seen.append(operation_id)
                raise ProvisioningRefused("no such operation")

        _install(Recording())
        await installation.capabilities_async()
        assert seen, "the probes called nothing"
        free_text = [text for value in seen for text in _strings_in(value)]
        assert free_text, "no string arguments were captured"
        for text in free_text:
            assert "conformance_probe" in text, (
                f"a probe passed {text!r}, which is not a sentinel; a correct "
                "adapter could legitimately act on a real identifier"
            )

    async def test_the_probes_action_kind_is_a_closed_vocabulary_value(self):
        """The one non-sentinel argument, and why it is not a hazard.

        ``ProviderHandle`` requires an ``OperationKind``, and there is no sentinel
        member — the vocabulary is exactly ``provision``/``submit``/``release``.
        Excluded from the sentinel sweep above rather than silently passing it,
        because "the check has an exception" is worth stating: the kind names *what*
        an operation would do, while every identifier saying *which* workspace,
        allocation and operation it would touch is a sentinel. A correct adapter
        therefore still has no real subject to act on.
        """
        from superplane_contracts import OperationKind

        handle = capability_probes._handle("__conformance_probe_x__", PROBE_WORKSPACE)
        assert isinstance(handle.operation, OperationKind)
        for identifier in (
            handle.workspace,
            handle.allocation_id,
            handle.idempotency_key,
            handle.resource_name,
        ):
            assert "conformance_probe" in identifier

    async def test_an_adapter_exception_never_escapes_the_probe(self):
        """An escaping exception inside a boot gate looks like a failed start.

        Ordinary exceptions, however exotic, are reported as results and fail the
        capability. Process-control exceptions are handled by the test below — they
        are deliberately *not* in this set.
        """

        class Hostile:
            async def read(self, **kwargs):
                raise RuntimeError("hostile")

            async def resolve(self, *args, **kwargs):
                raise ValueError("hostile")

            async def report_progress(self, *args, **kwargs):
                raise ArithmeticError("hostile")

        _install(Hostile())
        capabilities = await installation.capabilities_async()
        assert set(capabilities) == set(PORTS)
        assert not any(capabilities.values()), (
            "an adapter that raises an unsanctioned error reported as composed"
        )

    @pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
    async def test_process_control_exceptions_are_not_swallowed(self, control):
        """Cancellation and shutdown must travel, not be recorded as an answer.

        The earlier revision caught ``BaseException``, which meant a ``SystemExit``
        raised while the gate was probing became an adapter "result" and the boot
        continued. Worse, it suppressed ``asyncio.CancelledError``, so a cancelled
        probe could not be cancelled. These are the caller being torn down, not the
        adapter answering, and conflating the two makes shutdown unreliable.
        """

        class Interrupting:
            async def read(self, **kwargs):
                raise control()

            async def resolve(self, *args, **kwargs):
                raise control()

            async def report_progress(self, *args, **kwargs):
                raise control()

        _install(Interrupting())
        with pytest.raises(control):
            await capability_probes.probe_port("credential_evidence")

    async def test_cancellation_is_not_swallowed(self):
        """``CancelledError`` must propagate, or the probe cannot be cancelled."""

        class Cancelling:
            async def read(self, **kwargs):
                raise asyncio.CancelledError

            async def resolve(self, *args, **kwargs):
                raise asyncio.CancelledError

            async def report_progress(self, *args, **kwargs):
                raise asyncio.CancelledError

        _install(Cancelling())
        with pytest.raises(asyncio.CancelledError):
            await capability_probes.probe_port("credential_evidence")


class TestExistingConsumerContractsAreUnchanged:
    """Four consumers read this. All keep their current meaning."""

    def test_the_cli_still_exits_2_and_prints_the_capabilities_key(
        self, capsys, monkeypatch
    ):
        """``installation/runner.py:288`` parses exactly this shape."""
        _unconfigured(monkeypatch)
        assert installation.main(["capabilities"]) == 2
        payload = json.loads(capsys.readouterr().out)
        assert set(payload["capabilities"]) == set(PORTS)
        assert not any(payload["capabilities"].values())

    def test_the_cli_exits_0_when_every_adapter_refuses_correctly(self, capsys):
        _install(_Correct())
        assert installation.main(["capabilities"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert all(payload["capabilities"].values())

    def test_the_cli_reports_which_probe_failed(self, capsys):
        """The old output gave an operator four booleans and no next step."""
        _install(_Placeholder())
        installation.main(["capabilities"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["probes"]["operation_facade"]["composed"] is False
        assert payload["probes"]["operation_facade"]["failed"]

    def test_every_cli_report_carries_the_smoke_limitation(self, capsys):
        """A green capabilities run is exactly what gets quoted as live evidence.

        It must carry the *smoke* boundary, not the conformance one. This is the
        wording an operator or a reviewer reads off a passing preflight, so it is the
        last place the two tiers may be conflated: the stronger text would claim
        per-rule isolation that no boot-time run performed.
        """
        _install(_Correct())
        installation.main(["capabilities"])
        payload = json.loads(capsys.readouterr().out)
        for name, report in payload["probes"].items():
            assert report["limitation"] == SMOKE_LIMITATION, name
            assert report["limitation"] != CONFORMANCE_LIMITATION, name
            assert "does NOT isolate" in report["limitation"], name
            assert "not conformance evidence" in report["limitation"], name
            assert report["provenance"] == (
                "offline conformance probes; no provider contacted"
            ), name

    def test_config_flags_cannot_turn_an_absent_adapter_into_a_capability(
        self, monkeypatch, capsys
    ):
        """The property the pre-existing suite asserts, still true.

        Now for a stronger reason: the answer comes from calling the adapter, so
        there is no flag in the path that could be flipped.
        """
        _unconfigured(monkeypatch)
        monkeypatch.setenv("CREDENTIAL_EVIDENCE_AVAILABLE", "true")
        monkeypatch.setenv("B_OPERATION_AUTHORITY_AVAILABLE", "true")
        assert installation.main(["capabilities"]) == 2
        assert not any(json.loads(capsys.readouterr().out)["capabilities"].values())

    def test_the_sync_entry_point_refuses_to_run_inside_a_loop(self):
        """Rather than raising a confusing error from deep inside ``asyncio.run``.

        The two async consumers must call ``capabilities_async``; this makes getting
        that wrong say so.
        """

        async def attempt():
            with pytest.raises(RuntimeError, match="capabilities_async"):
                installation.capabilities()

        asyncio.run(attempt())

    async def test_the_boolean_fold_treats_a_missing_verdict_as_false(self):
        """Fail-closed on a probe-layer bug.

        Defaulting a missing key to True is how a gate silently stops gating.
        """
        assert installation.capabilities_from({"x": {}}) == {"x": False}
        assert installation.capabilities_from({"x": {"composed": "yes"}}) == {
            "x": False
        }
        assert installation.capabilities_from({"x": {"composed": True}}) == {"x": True}


class TestTheReadoutMatchesTheRegistry:
    """The registry and the probe layer must name the same ports."""

    async def test_the_readout_keys_are_exactly_the_registry_capability_ports(self):
        from superplane_contracts import API_CAPABILITY_PORTS

        _install(None)
        capabilities = await installation.capabilities_async()
        assert set(capabilities) == set(API_CAPABILITY_PORTS)

    async def test_the_router_reports_exactly_four_booleans(self):
        """``installation/runner.py:1188`` asserts ``len(capabilities) == 4``.

        And booleans specifically: the probe detail must not leak into a
        tenant-facing response, where an adapter's refusal message could carry a
        provider error or another tenant's identifier.
        """
        _install(None)
        capabilities = await installation.capabilities_async()
        assert len(capabilities) == 4
        assert all(isinstance(value, bool) for value in capabilities.values())

    def test_every_probed_port_is_covered_by_this_file(self):
        """So a port added to the probe layer cannot arrive untested."""
        assert set(capability_probes._PROBES) == set(PORTS)


def _strings_in(value: object) -> list[str]:
    """Every free-text string reachable from a probe argument.

    Enum members are skipped: they are closed vocabularies, not identifiers, and
    there is no sentinel ``OperationKind``. Note that ``str``-valued enums *are*
    ``str`` instances, so the enum check has to come first or every enum member
    would be swept up as free text.
    """
    if isinstance(value, Enum):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (frozenset, set, tuple, list)):
        return [text for item in value for text in _strings_in(item)]
    found: list[str] = []
    for attribute in vars(value).values() if hasattr(value, "__dict__") else ():
        found.extend(_strings_in(attribute))
    return found
