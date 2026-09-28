"""The shared conformance probes reject adapters that should be rejected.

Issue #5524 (w6-01), EPIC #4910, Wave 6. AC-01.

## Why the tests here are mostly about bad adapters

A conformance suite is a claim about what it would catch, and that claim is only
as good as the counterexamples it has actually been run against. A suite tested
only on a correct adapter demonstrates that it does not produce false alarms,
which is the cheap half; the half that matters is whether it produces an alarm
when one is warranted.

So the bulk of this file builds adapters that are wrong in one specific way each
— one that approves a workspace it was never granted, one that accepts an
authority string nobody minted, one that returns a confident answer for an
outcome it could not establish, one that raises where its contract says return
``None``, and one that exists but implements nothing — and asserts that each is
reported as failing, for the right reason. The final class covers the correct
adapter, which must pass.

The "exists but implements nothing" case is the one this story exists for: it is
precisely what the old ``is not None`` readiness check admitted.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from superplane_contracts import (
    CONFORMANCE_LIMITATION,
    PROBE_AUTHORITY,
    PROBE_WORKSPACE,
    PRODUCTION_PORTS,
    SMOKE_LIMITATION,
    STALE_VERSION,
    UNSERVED_FUTURE_VERSION,
    ConformanceReport,
    ContractViolation,
    Probe,
    ProbeKind,
    ProbeResult,
    ProbeVerdict,
    ProvisioningRefused,
    UnknownOutcome,
    build_request,
    classify_response,
    declared_kinds,
    isolation_probes_for,
    not_exercised_for,
    port,
    report_for,
    run_probes,
    varied_field,
)

# A port for each refusal shape, so the shape-specific assertions below name a real
# registry entry instead of inventing one.
NONE_PORT = "credential_evidence"  # NONE_MEANS_UNVERIFIED
RAISE_PORT = "operation_facade"  # RAISE_UNAVAILABLE
UNRESOLVED_PORT = "provider_client"  # UNRESOLVED_VALUE


def _correct_results(port_name: str, probes=None) -> list[ProbeResult]:
    """What a fully correct adapter's results look like, for a ``NONE_MEANS_UNVERIFIED`` port.

    Answers each probe by its polarity: ``None`` refuses every negative probe, and the
    valid control is admitted with a value. Written as a helper because getting the
    polarity wrong is precisely the mistake this story is about — a list comprehension
    passing ``returned=None`` to every probe silently refuses the control too, which is
    an adapter that rejects all legitimate work.
    """
    selected = isolation_probes_for(port_name) if probes is None else probes
    return [
        classify_response(
            item,
            returned=(
                {"admitted": "seeded control"}
                if item.kind is ProbeKind.VALID_CONTROL
                else None
            ),
        )
        for item in selected
    ]


def _probe(port_name: str, kind: ProbeKind = ProbeKind.FORGED_WORKSPACE) -> Probe:
    """One probe for a port, with the refusal shape that port's contract requires.

    Takes the varied field from ``varied_field`` rather than hardcoding it, so these
    tests cannot construct a probe whose declared kind disagrees with the dimension
    it actually varies — the disagreement ``Probe.__post_init__`` refuses.
    """
    varies, value = varied_field(kind)
    return Probe(
        kind=kind,
        port_name=port_name,
        description="a probe built by the conformance tests",
        required_outcome=port(port_name).unknown_outcome,
        varies=varies,
        value=value,
    )


class TestProbeSetsCoverWhatEachPortBinds:
    """``isolation_probes_for`` derives obligations from the registry, not from a hand-list."""

    @pytest.mark.parametrize("contract", PRODUCTION_PORTS, ids=lambda c: c.name)
    def test_every_port_is_obliged_to_refuse_the_universal_cases(
        self, contract
    ) -> None:
        """The obligation set, which is what the contract states.

        Asserted against ``declared_kinds`` rather than ``isolation_probes_for``: every port's
        contract is versioned, so refusing an unserved version is an obligation for
        all of them, but whether a given *call* can present one is a separate
        question. An earlier revision asked this of ``isolation_probes_for`` and, to keep it
        true, emitted a stale-version probe for every port — including the ones whose
        calls carry no version — then reported each as a verified refusal.
        """
        kinds = set(declared_kinds(contract.name))
        assert ProbeKind.STALE_CONTRACT_VERSION in kinds
        assert ProbeKind.UNKNOWN_MUST_NOT_SUCCEED in kinds

    @pytest.mark.parametrize("contract", PRODUCTION_PORTS, ids=lambda c: c.name)
    def test_a_version_no_call_carries_is_reported_unexercised_not_refused(
        self, contract
    ) -> None:
        """The F2 sub-finding, asserted per port.

        No port's call currently exchanges a contract version — ``grep -n
        contract_version`` across the adapter, delivery, provisioning and auth
        modules finds nothing — so every port must report the dimension as
        unexercised. The failure this forbids is the quiet one: a probe emitted,
        "refused" because the adapter ignored an argument it never received, and
        counted as evidence the version rule is enforced.
        """
        emitted = {item.kind for item in isolation_probes_for(contract.name)}
        unexercised = not_exercised_for(contract.name)
        if contract.carries_contract_version:
            assert ProbeKind.STALE_CONTRACT_VERSION in emitted
            assert ProbeKind.STALE_CONTRACT_VERSION.value not in unexercised
        else:
            assert ProbeKind.STALE_CONTRACT_VERSION not in emitted
            assert ProbeKind.STALE_CONTRACT_VERSION.value in unexercised

    def test_no_obligation_is_silently_dropped(self) -> None:
        """Every declared kind is either exercised or named as unexercised.

        The property that makes the report honest. Without it, narrowing a call is
        indistinguishable from deleting an obligation.
        """
        for contract in PRODUCTION_PORTS:
            exercised = {
                item.kind.value for item in isolation_probes_for(contract.name)
            }
            accounted = exercised | set(not_exercised_for(contract.name))
            assert accounted == {item.value for item in declared_kinds(contract.name)}

    def test_narrowing_the_call_moves_obligations_to_unexercised(self) -> None:
        """A caller whose call carries less does not thereby have less to prove.

        This is the readiness gate's situation: ``report_progress`` carries only an
        operation id. What it cannot present must show up as unexercised rather than
        vanishing from the accounting.
        """
        full = {
            item.kind.value for item in isolation_probes_for("allocation_inventory")
        }
        narrowed = isolation_probes_for(
            "allocation_inventory", exercisable={"workspace"}
        )
        narrow_kinds = {item.kind.value for item in narrowed}
        assert narrow_kinds < full
        moved = full - narrow_kinds
        assert moved <= set(
            not_exercised_for("allocation_inventory", exercisable={"workspace"})
        )

    def test_claiming_to_exercise_a_version_a_port_does_not_carry_is_refused(
        self,
    ) -> None:
        """A caller cannot opt back into the false pass.

        If ``exercisable`` silently honoured ``contract_version`` on a port whose
        call has no version parameter, any story could restore the defect by passing
        one extra string.
        """
        assert not port("credential_evidence").carries_contract_version
        with pytest.raises(ContractViolation, match="carries_contract_version=False"):
            isolation_probes_for(
                "credential_evidence", exercisable={"workspace", "contract_version"}
            )

    @pytest.mark.parametrize("contract", PRODUCTION_PORTS, ids=lambda c: c.name)
    def test_probe_refusal_shape_matches_the_ports_contract(self, contract) -> None:
        """A probe cannot demand a refusal shape the port never promised.

        If it could, sixteen implementers would be failing a check for not doing
        something their contract never asked of them, and the usual response to that
        is to weaken the check.
        """
        for item in isolation_probes_for(contract.name):
            assert item.required_outcome is contract.unknown_outcome

    def test_permission_bearing_ports_are_probed_for_missing_permission(self) -> None:
        for contract in PRODUCTION_PORTS:
            kinds = {item.kind for item in isolation_probes_for(contract.name)}
            assert (ProbeKind.MISSING_PERMISSION in kinds) is (
                contract.required_permission is not None
            ), (
                f"port {contract.name!r} demands "
                f"{contract.required_permission!r} but its probe set "
                f"{'omits' if contract.required_permission else 'includes'} the "
                "missing-permission probe"
            )

    def test_workspace_bound_ports_are_probed_with_a_forged_workspace(self) -> None:
        """Only the ports that actually bind a tenant scope.

        A probe asserting a tenant rule against a port with no tenant in its bound
        identifiers would be testing a rule the contract never made — which fails
        honest implementations and teaches implementers to distrust the suite.
        """
        for contract in PRODUCTION_PORTS:
            binds_workspace = any(
                item in ("workspace", "workspace_id")
                for item in contract.bound_identifiers
            )
            kinds = {item.kind for item in isolation_probes_for(contract.name)}
            assert (ProbeKind.FORGED_WORKSPACE in kinds) is binds_workspace

    def test_a_probe_cannot_be_written_against_an_unknown_port(self) -> None:
        with pytest.raises(ContractViolation, match="unknown production port"):
            Probe(
                kind=ProbeKind.FORGED_WORKSPACE,
                port_name="no_such_port",
                description="x",
                required_outcome=UnknownOutcome.NONE_MEANS_UNVERIFIED,
            )

    def test_probes_are_immutable(self) -> None:
        """So a caller cannot retune a probe into a weaker one before running it."""
        item = _probe(NONE_PORT)
        with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
            item.required_outcome = UnknownOutcome.RAISE_UNAVAILABLE  # type: ignore[misc]


class TestAdaptersThatMustFail:
    """The counterexamples. Each is wrong in exactly one way."""

    def test_an_adapter_that_merely_exists_is_not_implemented(self) -> None:
        """The defect this whole story is about.

        An object that is not ``None``, and therefore passed the previous readiness
        check, while implementing none of the calls its port declares.
        """
        result = classify_response(_probe(NONE_PORT), call_missing=True)
        assert not result.passed
        assert result.verdict is ProbeVerdict.NOT_IMPLEMENTED

    def test_an_adapter_that_returns_a_value_for_a_forged_workspace_is_admitted(
        self,
    ) -> None:
        """Returning *anything* for unauthorized input is the authorization bypass.

        Under ``NONE_MEANS_UNVERIFIED`` the refusal *is* ``None``, so a truthy
        return — even an empty-looking record — is an admission.
        """
        result = classify_response(
            _probe(NONE_PORT), returned={"workspace": PROBE_WORKSPACE, "ok": True}
        )
        assert result.verdict is ProbeVerdict.ADMITTED
        assert not result.passed

    @pytest.mark.parametrize(
        "sneaky",
        [
            0,
            False,
            "",
            [],
            {},
            (),
        ],
    )
    def test_falsey_but_present_returns_are_still_admissions(
        self, sneaky: object
    ) -> None:
        """``None`` is the refusal, not "anything falsey".

        This is the case a truthiness check would get wrong. An adapter returning
        ``0`` or ``""`` for unauthorized input has still answered the question, and
        a caller writing ``if result:`` would refuse while a caller writing
        ``if result is not None:`` would proceed. The contract says ``None``, so the
        probe requires exactly ``None``.
        """
        result = classify_response(_probe(NONE_PORT), returned=sneaky)
        assert result.verdict is ProbeVerdict.ADMITTED

    def test_an_adapter_accepting_an_unminted_authority_is_admitted(self) -> None:
        """Accepting on shape alone.

        ``PROBE_AUTHORITY`` is a well-formed string and nothing more. An adapter
        that validates the shape of an authority rather than checking it against B's
        live lease and fence will accept it, which is the forged-authority bypass
        AC-01 names.
        """
        result = classify_response(
            _probe("provider_authority", ProbeKind.UNKNOWN_AUTHORITY),
            returned={"authority": PROBE_AUTHORITY, "valid": True},
        )
        assert result.verdict is ProbeVerdict.ADMITTED

    def test_an_adapter_that_resolves_an_unestablished_outcome_is_admitted(
        self,
    ) -> None:
        """Confidence without evidence.

        Under ``UNRESOLVED_VALUE`` the adapter had a way to say "I could not
        establish this" and returned a definite answer instead. That is the
        confident-wrong-answer failure, and it is worse than a crash because the
        caller acts on it.
        """
        result = classify_response(
            _probe(UNRESOLVED_PORT, ProbeKind.UNKNOWN_MUST_NOT_SUCCEED),
            returned=_Resolved(state="succeeded"),
        )
        assert result.verdict is ProbeVerdict.ADMITTED

    def test_an_adapter_returning_none_where_a_typed_unresolved_is_required_fails(
        self,
    ) -> None:
        """``None`` is not a typed unresolved value.

        Under ``UNRESOLVED_VALUE`` the caller must keep the operation open, and it
        needs a value carrying that state to do so. A bare ``None`` gives it nothing
        to carry, so this is reported rather than quietly accepted as "close enough".
        """
        result = classify_response(
            _probe(UNRESOLVED_PORT, ProbeKind.UNKNOWN_MUST_NOT_SUCCEED), returned=None
        )
        assert not result.passed

    def test_an_adapter_raising_where_none_is_required_is_the_wrong_shape(self) -> None:
        """A real defect, reported distinctly from a bypass.

        The caller written against this port catches nothing and dies instead of
        refusing. Kept separate from ``ADMITTED`` because collapsing the two would
        hide which of the sixteen adapters is actually dangerous.
        """
        result = classify_response(_probe(NONE_PORT), raised=RuntimeError("boom"))
        assert result.verdict is ProbeVerdict.WRONG_REFUSAL_SHAPE
        assert not result.passed

    def test_an_adapter_returning_instead_of_raising_is_admitted(self) -> None:
        """Under ``RAISE_UNAVAILABLE`` any return value is an admission.

        Including ``None``: this port's callers proceed on absence of an exception,
        so a silent ``None`` is how provisioning would continue with no facade.
        """
        for returned in (None, {"operation_id": "x"}, "ok"):
            result = classify_response(_probe(RAISE_PORT), returned=returned)
            assert result.verdict is ProbeVerdict.ADMITTED, returned

    def test_an_adapter_that_approves_everything_fails_every_probe(self) -> None:
        """The unconditional-success placeholder, run against a whole probe set.

        This is what a suite of positive-only tests would pass: every well-formed
        authorized request succeeds, because every request succeeds.
        """
        probes = isolation_probes_for(NONE_PORT)
        report = report_for(
            NONE_PORT,
            [classify_response(item, returned={"approved": True}) for item in probes],
        )
        assert not report.conformant
        # Every *negative* probe is a bypass. The valid control is not: admitting the
        # seeded valid request is the one correct thing this adapter does, and scoring
        # it as a failure would make the control impossible to distinguish from the
        # bypasses it exists to make attributable.
        negatives = [
            item for item in probes if item.kind is not ProbeKind.VALID_CONTROL
        ]
        assert len(report.failures) == len(negatives)
        assert {item.probe.kind for item in report.failures} == {
            item.kind for item in negatives
        }
        assert report.isolated, (
            "the control was admitted, so the variations are attributable — which is "
            "what makes every one of them a reportable bypass rather than noise"
        )

    def test_a_placeholder_fails_every_probe_and_says_why(self) -> None:
        report = report_for(
            NONE_PORT,
            [
                classify_response(item, call_missing=True)
                for item in isolation_probes_for(NONE_PORT)
            ],
        )
        assert not report.conformant
        assert {item.verdict for item in report.failures} == {
            ProbeVerdict.NOT_IMPLEMENTED
        }

    def test_one_bad_probe_among_good_ones_fails_the_report(self) -> None:
        """Conformance is not a majority vote.

        An adapter that refuses four unauthorized cases and admits the fifth is an
        adapter with an authorization bypass.
        """
        probes = isolation_probes_for(NONE_PORT)
        assert len(probes) > 1
        assert probes[0].kind is ProbeKind.VALID_CONTROL
        # The control is admitted (correctly), the negative probes but the last are
        # refused (correctly), and the last is admitted (the bypass).
        results = [classify_response(probes[0], returned={"allocation": "x"})]
        results += [classify_response(item, returned=None) for item in probes[1:-1]]
        results.append(classify_response(probes[-1], returned={"approved": True}))
        report = report_for(NONE_PORT, results)
        assert report.isolated, (
            "precondition: the control passed, so failures attribute"
        )
        assert not report.conformant
        assert len(report.failures) == 1
        assert report.failures[0].probe.kind is probes[-1].kind


#: A port that binds a tenant, an operation identity and an authority, and demands a
#: permission — so a single adapter can be wrong about exactly one of the four while
#: looking correct on the others. `NONE_MEANS_UNVERIFIED`, so a refusal is `None` and
#: a bypass is any returned value, which keeps the flawed adapters below readable.
ISOLATION_PORT = "allocation_inventory"

#: The **seeded valid fixture**: an authority-owned request the adapters below are
#: built to accept. Offline and entirely synthetic — no real tenant, no real
#: credential, no live authority — but internally consistent, which is the property
#: that matters: a correct adapter has an affirmative reason to admit it.
#:
#: This is what the two earlier revisions lacked, and why both reported a
#: workspace-only reader as fully conformant. Their baseline was unauthorized in
#: *every* field, so varying one field left the other three still disqualifying, and
#: every call had a legitimate reason to be refused regardless of the varied field.
#: A refusal attributes to the varied dimension only when the request would otherwise
#: have been accepted.
ISOLATION_FIXTURE: dict[str, object] = {
    "workspace": "fixture-owned-workspace",
    "operation_id": "fixture-allocation-0001",
    "operation_authority": "fixture-minted-authority",
    "permission": port(ISOLATION_PORT).required_permission,
}

#: The dimensions a reader can get wrong independently of the others, which is what
#: makes "regress readers that omit each check separately" expressible as a parametrize.
ISOLATION_DIMENSIONS = (
    "workspace",
    "operation_id",
    "operation_authority",
    "permission",
)


class _ChecksOnly:
    """A reader that validates exactly the fields it was told to, and nothing else.

    The single most important fixture in this file, and the direct analogue of the
    offline reader in the supervisor's reproduction: one whose author checked the
    tenant scope and assumed the operation identity, the authority and the permission
    were someone else's problem.

    Each instance admits the seeded valid fixture — which is what makes it a
    *plausible* implementation rather than one that refuses everything — and then
    refuses only variations in the fields it actually checks. Against both earlier
    revisions every one of these was reported fully conformant.
    """

    def __init__(self, *fields: str) -> None:
        self.enforces = frozenset(fields)
        self.seen: list[dict[str, object]] = []

    async def read(self, request: dict[str, object]) -> object:
        self.seen.append(dict(request))
        # Only the fields this reader checks are consulted. A field it does not check
        # is not looked at, so a forged value there sails straight through — exactly
        # how a real partial implementation fails.
        for field in self.enforces:
            if request[field] != ISOLATION_FIXTURE[field]:
                return None  # refuses: a field it checks does not match
        # Everything it checks matches, so it answers — correctly for the valid
        # control, and as a bypass for any dimension it neglected to check.
        return {"allocation": request["operation_id"], "authorized": True}


def _run(adapter: object, *, probes=None) -> tuple:
    """Drive the real shared runner against ``adapter``.

    Deliberately the production ``run_probes`` rather than a test double: the finding
    was in the runner's per-probe isolation, so a test that reimplemented the loop
    would prove nothing about the code the sixteen stories will actually call.
    """
    return asyncio.run(
        run_probes(
            ISOLATION_PORT,
            adapter.read,
            ISOLATION_FIXTURE,
            probes=probes,
        )
    )


def _isolation_probes():
    """The probe set for the seeded fixture's fields, used by nearly every test below."""
    return isolation_probes_for(ISOLATION_PORT, exercisable=set(ISOLATION_FIXTURE))


class TestTheValidControlIsWhatMakesARefusalMeanSomething:
    """F2's actual repair: isolation requires a request the adapter accepts.

    Both earlier revisions varied one field of a baseline that was already
    unauthorized in every other field. A refusal of such a request is genuine but
    attributes to nothing — the workspace alone was reason enough. The supervisor's
    reproduction made that concrete: a reader checking only the workspace reported
    ``conformant=true`` with four refused probes, while a direct call with the accepted
    workspace and a forged authority was admitted.

    The control closes it. The fixture is admitted first, establishing that the
    adapter *would* have accepted the request; only then does a refusal of a
    one-field variation mean that field was checked.
    """

    def test_the_control_runs_first_so_later_refusals_are_attributable(self) -> None:
        """Ordering is load-bearing, not cosmetic.

        If a variation ran before the control, its refusal would be recorded before
        anything established the request was otherwise acceptable — the exact
        unattributable state this repair removes.
        """
        probes = _isolation_probes()
        assert probes[0].kind is ProbeKind.VALID_CONTROL

        adapter = _ChecksOnly(*ISOLATION_DIMENSIONS)
        _run(adapter, probes=probes)
        assert adapter.seen[0] == ISOLATION_FIXTURE, (
            "the first call must be the unmodified fixture; a suite that varies a "
            "field before proving the baseline is accepted cannot attribute refusals"
        )

    def test_a_reader_that_refuses_everything_is_not_conformant(self) -> None:
        """The hole a purely negative suite cannot see.

        An adapter refusing all legitimate work passes every negative probe
        trivially. Before the control existed such an adapter was indistinguishable
        from a correct one — and it is broken in a way that takes production down.
        """

        class RefusesEverything:
            async def read(self, request: dict[str, object]) -> object:
                return None

        report = report_for(
            ISOLATION_PORT, _run(RefusesEverything(), probes=_isolation_probes())
        )
        assert not report.isolated, (
            "an adapter that refused the valid control cannot be reported as having "
            "isolated anything; its refusals establish nothing"
        )
        assert not report.conformant
        refused_control = [
            item
            for item in report.failures
            if item.verdict is ProbeVerdict.REFUSED_VALID_CONTROL
        ]
        assert len(refused_control) == 1

    def test_refusing_the_control_disqualifies_the_whole_report(self) -> None:
        """Because ``conformant`` requires ``isolated``, not merely no failures.

        Without that coupling, a run whose control failed but whose negative probes
        all "passed" would report four verified dimensions on the strength of
        refusals that attribute to nothing.
        """

        class RefusesEverything:
            async def read(self, request: dict[str, object]) -> object:
                return None

        results = _run(RefusesEverything(), probes=_isolation_probes())
        negatives = [item for item in results if item.probe.varies]
        assert all(item.passed for item in negatives), (
            "precondition: every negative probe is individually 'passing' here"
        )
        report = report_for(ISOLATION_PORT, results)
        assert not report.conformant, (
            "all-negatives-pass must not be enough; the control failed, so no "
            "dimension was isolated"
        )

    def test_a_run_with_no_control_at_all_cannot_report_conformant(self) -> None:
        """The case ``conformant``'s dependence on ``isolated`` exists for.

        Here every probe present passes, so an ``all(passed)`` definition of
        conformance returns True — while nothing established the request would
        otherwise have been accepted. This is the F2 shape in its purest form: real
        refusals, invented attribution. Asserted with the control simply omitted,
        because a deleted probe is the easiest way to reintroduce the defect.
        """
        negatives = [
            item
            for item in _isolation_probes()
            if item.kind is not ProbeKind.VALID_CONTROL
        ]
        results = [classify_response(item, returned=None) for item in negatives]
        assert all(item.passed for item in results), "precondition: all probes pass"

        report = report_for(ISOLATION_PORT, results)
        assert not report.isolated
        assert not report.conformant, (
            "a run with no valid control must not be conformant however many "
            "negative probes it refused; conformance requires isolation"
        )
        assert report.summary()["conformant"] is False
        assert report.summary()["isolated"] is False

    def test_the_summary_publishes_isolation_beside_conformance(self) -> None:
        """So a consumer cannot read one as the other."""
        summary = report_for(
            ISOLATION_PORT,
            _run(_ChecksOnly(*ISOLATION_DIMENSIONS), probes=_isolation_probes()),
        ).summary()
        assert summary["isolated"] is True
        assert summary["conformant"] is True


class TestEachDimensionIsProbedInIsolation:
    """One call per probe against a valid control, so a partial reader is caught.

    Each test builds a reader flawed in exactly one way — omitting the operation,
    authority, workspace or permission check *separately*, per the required repair —
    drives the real runner, and requires the report to name each bypass individually.
    A test asserting only "the correct adapter passes" would have passed on both
    broken revisions.
    """

    @pytest.mark.parametrize("omitted", ISOLATION_DIMENSIONS)
    def test_a_reader_omitting_one_check_is_caught_on_exactly_that_dimension(
        self, omitted: str
    ) -> None:
        """The regression the supervisor asked for, per dimension.

        A reader that enforces everything *except* one rule. The bypass must be
        reported against the dimension actually missing — not smeared across all of
        them, and not hidden by a blanket refusal.
        """
        enforced = tuple(d for d in ISOLATION_DIMENSIONS if d != omitted)
        adapter = _ChecksOnly(*enforced)
        probes = _isolation_probes()
        report = report_for(ISOLATION_PORT, _run(adapter, probes=probes))

        assert report.isolated, (
            "this reader admits the valid fixture, so the control must pass and the "
            "variations must be attributable"
        )
        assert not report.conformant, (
            f"a reader that never checks {omitted!r} was reported conformant"
        )
        bypassed = {
            item.probe.varies
            for item in report.failures
            if item.verdict is ProbeVerdict.ADMITTED
        }
        assert bypassed == {omitted}, (
            f"expected the bypass to be attributed to {omitted!r} alone, got "
            f"{sorted(bypassed)}"
        )

    @pytest.mark.parametrize("enforced", ISOLATION_DIMENSIONS)
    def test_a_reader_checking_only_one_dimension_is_caught_on_all_the_others(
        self, enforced: str
    ) -> None:
        """The supervisor's reproduction adapter, generalized to each dimension.

        Their offline reader admitted unless the workspace matched, and checked
        neither allocation nor authority. Under the previous baseline every probe was
        refused for the workspace alone and the report credited all four dimensions.
        """
        adapter = _ChecksOnly(enforced)
        probes = _isolation_probes()
        report = report_for(ISOLATION_PORT, _run(adapter, probes=probes))

        assert report.isolated
        assert not report.conformant
        bypassed = {
            item.probe.varies
            for item in report.failures
            if item.verdict is ProbeVerdict.ADMITTED
        }
        assert bypassed == {d for d in ISOLATION_DIMENSIONS if d != enforced}

    @pytest.mark.parametrize("enforced", ISOLATION_DIMENSIONS)
    def test_the_one_dimension_it_does_enforce_is_credited(self, enforced: str) -> None:
        """The other half: the suite must not simply fail everything.

        A check that failed a partial adapter on every probe would be useless for
        diagnosis — the implementer needs to know which rule is missing — and it would
        also pass this file's adversarial tests while establishing nothing.
        """
        adapter = _ChecksOnly(enforced)
        results = _run(adapter, probes=_isolation_probes())
        credited = {
            item.probe.varies for item in results if item.passed and item.probe.varies
        }
        assert credited == {enforced}

    def test_each_probe_is_its_own_call_varying_exactly_one_field(self) -> None:
        """The mechanism, asserted directly against what the adapter received.

        Without this, the suite could regress to one call and still satisfy the
        verdict assertions above by coincidence.
        """
        adapter = _ChecksOnly()  # enforces nothing; records every request
        probes = _isolation_probes()
        _run(adapter, probes=probes)

        assert len(adapter.seen) == len(probes), (
            "the runner must issue one call per probe; a single call cannot be "
            "evidence about more than one dimension"
        )
        for probe, request in zip(probes, adapter.seen, strict=True):
            differs = {
                name
                for name, value in request.items()
                if value != ISOLATION_FIXTURE[name]
            }
            assert differs == ({probe.varies} if probe.varies else set()), (
                f"probe {probe.kind.value!r} varied {sorted(differs)}; a probe that "
                "changes two fields at once cannot attribute a refusal to either"
            )

    def test_every_dimension_the_fixture_carries_is_actually_varied(self) -> None:
        """No dimension may be quietly absent from the probe set.

        The suite's value is the set of rules it isolates; a missing probe is a rule
        nobody checks, and it would read as success.
        """
        results = _run(_ChecksOnly(), probes=_isolation_probes())
        varied = {item.probe.varies for item in results if item.probe.varies}
        assert varied == set(ISOLATION_DIMENSIONS)

    def test_an_adapter_that_enforces_everything_is_conformant(self) -> None:
        """The correct adapter still passes, so the gate is not merely unpassable.

        This is the test that caught a defect in the repair itself: an intermediate
        revision emitted ``UNKNOWN_MUST_NOT_SUCCEED`` into this tier, where it
        demanded a refusal of the very fixture ``VALID_CONTROL`` requires be
        admitted. No implementation could satisfy both, and an unpassable check gets
        weakened rather than obeyed.
        """
        adapter = _ChecksOnly(*ISOLATION_DIMENSIONS)
        report = report_for(ISOLATION_PORT, _run(adapter, probes=_isolation_probes()))
        assert report.conformant
        assert report.isolated
        assert report.failures == ()

    def test_an_adapter_that_refuses_only_the_forged_workspace_is_the_named_case(
        self,
    ) -> None:
        """The reviewer's example, spelled out as its own assertion.

        "An adapter rejecting only the forged workspace was reported conformant for
        unminted authority, forged operation and missing permission." Each of those
        three is now individually named.
        """
        report = report_for(
            ISOLATION_PORT,
            _run(_ChecksOnly("workspace"), probes=_isolation_probes()),
        )
        failed = {item.probe.kind.value for item in report.failures}
        assert ProbeKind.UNKNOWN_AUTHORITY.value in failed
        assert ProbeKind.FORGED_OPERATION_IDENTITY.value in failed
        assert ProbeKind.MISSING_PERMISSION.value in failed
        assert ProbeKind.FORGED_WORKSPACE.value not in failed


class TestTheFixtureItselfIsAValidControlAndNotSentinelGarbage:
    """Guards the one assumption every isolation test silently rests on.

    ``_ChecksOnly`` compares each field against ``ISOLATION_FIXTURE``, so whatever
    that dict contains is "valid" *by construction*. That makes the fixture a
    tautology: replacing its values with the old all-unauthorized sentinels leaves
    every other test in this file green while destroying the property they depend on.

    That is not hypothetical — it was verified by editing the fixture back to the
    pre-repair sentinel baseline and observing the whole suite still pass. Without
    this class, F2 could be reintroduced through the test fixture alone. So these
    tests constrain the fixture against the probe machinery's *own* sentinel values,
    which is the one independent reference point available offline.
    """

    def test_no_fixture_field_holds_a_probe_sentinel(self) -> None:
        """The fixture must not collide with any value a probe forges.

        If a field held the same sentinel the probe for that field substitutes, the
        "variation" would not vary anything: the request would be unchanged, a
        correct adapter would admit it, and the probe would be scored a bypass. And
        if the fixture were built *entirely* of sentinels — the pre-repair baseline —
        it would be unauthorized in every field, which is F2 exactly.
        """
        forged = {
            varied_field(kind)[1]
            for kind in declared_kinds(ISOLATION_PORT)
            if kind not in (ProbeKind.VALID_CONTROL, ProbeKind.UNKNOWN_MUST_NOT_SUCCEED)
        }
        for name, value in ISOLATION_FIXTURE.items():
            assert value not in forged, (
                f"fixture field {name!r} holds {value!r}, which is a value a probe "
                "forges; the variation for that field would be a no-op"
            )

    def test_no_fixture_identity_is_marked_as_a_conformance_probe_sentinel(
        self,
    ) -> None:
        """The sentinels are self-labelling, and the fixture must not wear that label.

        Every probe sentinel carries ``conformance_probe``. A fixture identity that
        did too would be, by the probe machinery's own convention, a value declared
        unauthorized — so admitting it would not be evidence of anything.
        """
        for name, value in ISOLATION_FIXTURE.items():
            if name == "permission":
                continue  # the port's real required permission, not an identity
            assert "conformance_probe" not in str(value), (
                f"fixture field {name!r} is labelled a probe sentinel; a control "
                "request must be one the adapter has an affirmative reason to admit"
            )

    def test_the_fixture_carries_the_ports_genuinely_required_permission(self) -> None:
        """Not an arbitrary string, or the missing-permission probe proves nothing."""
        assert (
            ISOLATION_FIXTURE["permission"] == port(ISOLATION_PORT).required_permission
        )
        assert ISOLATION_FIXTURE["permission"] is not None

    def test_every_probed_dimension_is_present_in_the_fixture(self) -> None:
        """A dimension absent from the fixture cannot be varied, so it goes unchecked."""
        assert set(ISOLATION_DIMENSIONS) <= set(ISOLATION_FIXTURE)

    def test_the_fixture_names_nothing_that_could_be_a_real_tenant(self) -> None:
        """The safety half: seeded, not provisioned.

        The fixture must be valid *to the fixture adapter* without naming anything a
        live system would honour — no real account, no credential, no live authority.
        """
        blob = " ".join(str(value) for value in ISOLATION_FIXTURE.values()).lower()
        for marker in ("arn:", "aws_", "secret", "token", "password", "acct-"):
            assert marker not in blob, (
                f"fixture contains {marker!r}; offline fixtures must not resemble "
                "real credentials or accounts"
            )
        assert str(ISOLATION_FIXTURE["workspace"]).startswith("fixture-")


class TestRequestBuildingEnforcesTheIsolation:
    """``build_request`` is where a caller could quietly reintroduce the defect."""

    def test_a_request_differs_from_the_baseline_in_exactly_one_field(self) -> None:
        for probe in _isolation_probes():
            request = build_request(probe, ISOLATION_FIXTURE)
            differs = {
                name
                for name, value in request.items()
                if value != ISOLATION_FIXTURE[name]
            }
            assert differs == ({probe.varies} if probe.varies else set())

    def test_the_varied_field_carries_the_kinds_own_sentinel(self) -> None:
        """So a report naming a kind describes the input that was actually sent."""
        for probe in _isolation_probes():
            if not probe.varies:
                continue
            expected_field, expected_value = varied_field(probe.kind)
            request = build_request(probe, ISOLATION_FIXTURE)
            assert request[expected_field] == expected_value

    def test_the_baseline_is_not_mutated_between_probes(self) -> None:
        """Otherwise probe N+1 inherits probe N's sentinel and varies two fields."""
        before = dict(ISOLATION_FIXTURE)
        _run(
            _ChecksOnly(),
            probes=_isolation_probes(),
        )
        assert ISOLATION_FIXTURE == before

    def test_varying_a_field_the_baseline_lacks_is_refused(self) -> None:
        """Rather than inserted.

        An inserted field is either not a parameter of the call, or one the baseline
        forgot — and in the second case every other probe for this port has been
        running without it.
        """
        probe = _probe(ISOLATION_PORT, ProbeKind.FORGED_WORKSPACE)
        with pytest.raises(ContractViolation, match="does not contain"):
            build_request(probe, {"operation_id": "x"})

    def test_the_unvaried_probe_sends_the_baseline_unchanged(self) -> None:
        probe = _probe(ISOLATION_PORT, ProbeKind.UNKNOWN_MUST_NOT_SUCCEED)
        assert build_request(probe, ISOLATION_FIXTURE) == ISOLATION_FIXTURE


class TestTheRunnerReportsFailuresRatherThanPropagating:
    """A crash is a result; a teardown is not."""

    def test_a_crashing_adapter_fails_every_probe_and_raises_nothing(self) -> None:
        class Crashes:
            async def read(self, request: dict[str, object]) -> object:
                raise OSError("vault unreachable")

        report = report_for(ISOLATION_PORT, _run(Crashes()))
        assert not report.conformant
        assert not report.isolated, (
            "an adapter that crashed on the control established nothing it could be "
            "credited for"
        )
        # Two verdicts, because the crash means something different per polarity.
        # NONE_MEANS_UNVERIFIED: for a negative probe, raising at all is the wrong
        # refusal shape. For the control, a crash is not a refusal of legitimate work
        # either — it is a plain failure, and conflating the two would report a broken
        # adapter as merely over-strict.
        assert {item.verdict for item in report.failures} == {
            ProbeVerdict.WRONG_REFUSAL_SHAPE,
            ProbeVerdict.FAILED,
        }
        control = [
            item
            for item in report.results
            if item.probe.kind is ProbeKind.VALID_CONTROL
        ]
        assert [item.verdict for item in control] == [ProbeVerdict.FAILED]

    def test_an_adapter_with_no_such_call_is_not_implemented(self) -> None:
        class Placeholder:
            async def read(self, request: dict[str, object]) -> object:
                raise NotImplementedError

        report = report_for(ISOLATION_PORT, _run(Placeholder()))
        assert {item.verdict for item in report.results} == {
            ProbeVerdict.NOT_IMPLEMENTED
        }

    @pytest.mark.parametrize("escape", [KeyboardInterrupt, SystemExit])
    def test_process_control_exceptions_are_not_swallowed(self, escape) -> None:
        """These mean the process is going down, not that an adapter answered.

        Catching ``BaseException`` — which the earlier revision did — turned a Ctrl-C
        into a probe verdict, so an interrupted run could report results.
        """

        class Interrupted:
            async def read(self, request: dict[str, object]) -> object:
                raise escape()

        with pytest.raises(escape):
            _run(Interrupted())

    def test_cancellation_is_not_swallowed(self) -> None:
        """A cancelled suite must not be mistaken for a completed one."""

        class Cancelled:
            async def read(self, request: dict[str, object]) -> object:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            _run(Cancelled())

    def test_one_probes_failure_does_not_stop_the_others(self) -> None:
        """A report covering only the probes before the first crash would overstate.

        It would also be reported as conformant if the crash happened to come last and
        the earlier ones passed.
        """
        calls: list[str] = []

        class CrashesOnce:
            async def read(self, request: dict[str, object]) -> object:
                calls.append("call")
                if len(calls) == 1:
                    raise OSError("transient")
                return None

        probes = _isolation_probes()
        results = _run(CrashesOnce(), probes=probes)
        assert len(results) == len(probes)
        assert len(calls) == len(probes)


class TestVersionRefusal:
    """AC-01: a request written against an unserved version must be refused."""

    @pytest.mark.parametrize("version", [STALE_VERSION, UNSERVED_FUTURE_VERSION])
    def test_stale_and_future_versions_are_both_probed_as_refusals(
        self, version: str
    ) -> None:
        """Neither direction is best-effort interpreted.

        A future version is the more tempting one to accept — it looks like a newer
        peer — and accepting it means interpreting fields whose meaning this end
        never agreed to.
        """
        probe = _probe(NONE_PORT, ProbeKind.STALE_CONTRACT_VERSION)
        admitted = classify_response(probe, returned={"contract_version": version})
        assert admitted.verdict is ProbeVerdict.ADMITTED
        refused = classify_response(probe, returned=None)
        assert refused.passed

    def test_the_stale_version_is_not_one_this_package_serves(self) -> None:
        """Otherwise the probe would be presenting a valid request.

        Asserted rather than assumed: if ``SUPPORTED_VERSIONS`` ever grew to include
        ``v0``, this probe would silently start testing nothing.
        """
        from superplane_contracts import SUPPORTED_VERSIONS

        assert STALE_VERSION not in SUPPORTED_VERSIONS
        assert UNSERVED_FUTURE_VERSION not in SUPPORTED_VERSIONS


class TestCorrectAdaptersPass:
    """The other half: a correct refusal is recognized, in each shape."""

    def test_returning_none_passes_under_none_means_unverified(self) -> None:
        assert classify_response(_probe(NONE_PORT), returned=None).passed

    def test_raising_a_declared_refusal_passes_under_raise_unavailable(self) -> None:
        """The refusal must be one the port's contract names, not any exception.

        An earlier revision passed on *any* exception here, which made these ports
        unfailable: an ``OSError`` from a broken client, an ``ArithmeticError`` from a
        bug, a timeout — every one of them read as a correct refusal.
        """
        assert port(RAISE_PORT).refusal_exceptions
        result = classify_response(
            _probe(RAISE_PORT), raised=ProvisioningRefused("not composed")
        )
        assert result.passed

    @pytest.mark.parametrize(
        "crash",
        [
            RuntimeError("boom"),
            OSError("connection refused"),
            ValueError("bad value"),
            ArithmeticError("divide by zero"),
            AttributeError("'NoneType' has no attribute 'x'"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_an_undeclared_exception_is_a_failure_not_a_refusal(
        self, crash: BaseException
    ) -> None:
        """A crash is not evidence of anything the adapter refuses.

        These are exactly the exceptions the earlier revision accepted as refusals on
        a ``RAISE_UNAVAILABLE`` port. An adapter whose vault is unreachable is
        required to answer with its declared refusal; letting the transport error
        escape instead means the call produced no contract-valid answer.
        """
        result = classify_response(_probe(RAISE_PORT), raised=crash)
        assert result.verdict is ProbeVerdict.FAILED
        assert not result.passed
        assert type(crash).__name__ in result.detail

    def test_a_subclass_of_a_declared_refusal_still_passes(self) -> None:
        """Matched through the MRO, so a story may raise a more specific refusal.

        Requiring the exact type would push implementers toward raising the base class
        and losing the detail, or toward widening the allowlist until it means
        nothing.
        """

        class MoreSpecific(ProvisioningRefused):
            pass

        assert classify_response(
            _probe(RAISE_PORT), raised=MoreSpecific("no facade")
        ).passed

    @pytest.mark.parametrize(
        "unresolved",
        [
            "unresolved",
            "UNRESOLVED",
            " unknown ",
            "not_checked",
        ],
    )
    def test_unresolved_string_forms_pass(self, unresolved: str) -> None:
        result = classify_response(
            _probe(UNRESOLVED_PORT, ProbeKind.UNKNOWN_MUST_NOT_SUCCEED),
            returned=unresolved,
        )
        assert result.passed

    @pytest.mark.parametrize(
        "attribute", ["state", "result", "status", "exposure", "presence"]
    )
    def test_unresolved_is_read_from_any_contract_state_attribute(
        self, attribute: str
    ) -> None:
        """Covers the several ways this package's types report an unknown outcome.

        ``ReconcileResult.UNRESOLVED``, ``CostExposure.UNRESOLVED``,
        ``CheckStatus.NOT_CHECKED`` and ``OperationProgress.state == "unknown"`` all
        mean the same thing on different attributes, and the probe reads all of
        them rather than importing either side's enums.
        """
        carrier = type("Carrier", (), {attribute: "unresolved"})()
        result = classify_response(
            _probe(UNRESOLVED_PORT, ProbeKind.UNKNOWN_MUST_NOT_SUCCEED),
            returned=carrier,
        )
        assert result.passed

    def test_an_enum_valued_unresolved_state_passes(self) -> None:
        """The real shape: a ``str``-valued enum member, not a bare string."""
        from superplane_contracts import ReconcileResult

        result = classify_response(
            _probe(UNRESOLVED_PORT, ProbeKind.UNKNOWN_MUST_NOT_SUCCEED),
            returned=_Resolved(state=ReconcileResult.UNRESOLVED),
        )
        assert result.passed

    def test_a_correct_adapter_is_conformant_across_its_whole_probe_set(self) -> None:
        report = report_for(NONE_PORT, _correct_results(NONE_PORT))
        assert report.conformant
        assert report.isolated
        assert report.failures == ()


class TestUnrecognizedShapesFailClosed:
    """An outcome nobody anticipated must not fall through to a pass."""

    def test_a_novel_unknown_shape_fails_rather_than_being_guessed_at(self) -> None:
        """Fail-closed, and deliberately so.

        An adapter returning some new shape that happens to mean "unknown" fails.
        The remedy is for the adapter to report unresolved one of the contract's
        declared ways — not for this check to start inferring intent, because an
        inference broad enough to catch novel shapes is broad enough to read a
        success as an unknown.
        """
        result = classify_response(
            _probe(UNRESOLVED_PORT, ProbeKind.UNKNOWN_MUST_NOT_SUCCEED),
            returned=type("Novel", (), {"outcome": "indeterminate"})(),
        )
        assert not result.passed

    def test_an_unrelated_state_value_is_not_read_as_unresolved(self) -> None:
        result = classify_response(
            _probe(UNRESOLVED_PORT, ProbeKind.UNKNOWN_MUST_NOT_SUCCEED),
            returned=_Resolved(state="running"),
        )
        assert result.verdict is ProbeVerdict.ADMITTED

    def test_call_missing_wins_over_a_returned_value(self) -> None:
        """An absent call cannot be excused by whatever the caller passed alongside."""
        result = classify_response(_probe(NONE_PORT), returned=None, call_missing=True)
        assert result.verdict is ProbeVerdict.NOT_IMPLEMENTED

    def test_an_attribute_error_from_inside_an_adapter_is_not_not_implemented(
        self,
    ) -> None:
        """A bug in a real implementation is not evidence the method is absent.

        Conflating the two would report a broken adapter as an unimplemented one,
        sending the implementer to look for a missing method that is right there.
        """
        result = classify_response(
            _probe(NONE_PORT), raised=AttributeError("'NoneType' has no attribute 'x'")
        )
        assert result.verdict is ProbeVerdict.WRONG_REFUSAL_SHAPE


class TestReportsCannotOverstateWhatTheyEstablish:
    """The report is the artifact someone will quote as evidence."""

    def test_an_unprobed_adapter_cannot_report_as_conformant(self) -> None:
        """``all(())`` is True, which is how a green report for nothing happens.

        Refused at construction so that outcome is unreachable rather than merely
        unlikely — the empty case arises naturally from a probe loop that silently
        collected nothing.
        """
        with pytest.raises(ContractViolation, match="no probe results"):
            ConformanceReport(port_name=NONE_PORT, results=())

    def test_a_generator_of_results_does_not_produce_an_empty_report(self) -> None:
        """``report_for`` materializes, so the first ``all()`` cannot drain the results."""
        results = (item for item in _correct_results(NONE_PORT))
        report = report_for(NONE_PORT, results)
        assert report.results
        assert report.conformant
        assert report.conformant  # still true on a second read

    def test_a_report_cannot_mix_ports(self) -> None:
        """Otherwise one port's refusals would vouch for another's admissions."""
        with pytest.raises(ContractViolation, match="different ports"):
            ConformanceReport(
                port_name=NONE_PORT,
                results=(
                    classify_response(_probe(NONE_PORT), returned=None),
                    classify_response(_probe(RAISE_PORT), raised=RuntimeError()),
                ),
            )

    def test_a_report_with_a_control_carries_the_conformance_limitation(self) -> None:
        report = report_for(NONE_PORT, _correct_results(NONE_PORT))
        assert report.limitation == CONFORMANCE_LIMITATION
        assert "does not establish" in report.limitation
        assert "live" in report.limitation.lower()

    def test_a_report_without_a_control_carries_the_weaker_smoke_limitation(
        self,
    ) -> None:
        """The two tiers must not be able to quote each other's evidence boundary.

        A run with no valid control isolated nothing, so describing it with the
        conformance wording would claim per-rule verification it does not have. This
        is F2 restated at the level of the prose an operator actually reads.
        """
        report = report_for(
            NONE_PORT, [classify_response(_probe(NONE_PORT), returned=None)]
        )
        assert report.limitation == SMOKE_LIMITATION
        assert report.limitation != CONFORMANCE_LIMITATION
        assert "does NOT isolate" in report.limitation
        assert not report.isolated
        assert not report.conformant

    def test_a_report_without_a_limitation_is_refused(self) -> None:
        """Including on the passing path, which is the one that gets quoted."""
        with pytest.raises(ContractViolation, match="limitation"):
            ConformanceReport(
                port_name=NONE_PORT,
                results=(classify_response(_probe(NONE_PORT), returned=None),),
                limitation="   ",
            )

    def test_the_summary_names_failures_without_echoing_adapter_detail(self) -> None:
        """A receipt is a low-control destination.

        An adapter's refusal message is the one place a tenant identifier or a
        provider error string could have been interpolated, so the summary reports
        the failing probe *kinds* and not the details.
        """
        leaky = "tenant acct-1234 denied for org-secret"
        report = report_for(
            NONE_PORT,
            [
                ProbeResult(
                    probe=_probe(NONE_PORT),
                    verdict=ProbeVerdict.ADMITTED,
                    detail=leaky,
                )
            ],
        )
        summary = report.summary()
        assert summary["conformant"] is False
        assert summary["failed"] == [ProbeKind.FORGED_WORKSPACE.value]
        assert leaky not in repr(summary)
        # A limitation is always present; which one depends on whether a control ran,
        # and this hand-built single-probe report has none. The two preceding tests own
        # that distinction — here the point is only that the redaction holds.
        assert summary["limitation"] == SMOKE_LIMITATION

    def test_the_summary_records_the_contract_version(self) -> None:
        """So a report cannot be replayed as evidence for a later contract."""
        report = report_for(
            NONE_PORT, [classify_response(_probe(NONE_PORT), returned=None)]
        )
        assert report.summary()["contract_version"] == port(NONE_PORT).contract_version

    def test_a_verdict_must_be_the_enum(self) -> None:
        with pytest.raises(ContractViolation, match="must be a ProbeVerdict"):
            ProbeResult(probe=_probe(NONE_PORT), verdict="refused")  # type: ignore[arg-type]

    def test_only_refused_counts_as_passed(self) -> None:
        """There is no warning tier, deliberately.

        An adapter that admitted unauthorized input and one that could not answer
        are both reasons an image must not be treated as composed.
        """
        for verdict in ProbeVerdict:
            result = ProbeResult(probe=_probe(NONE_PORT), verdict=verdict)
            assert result.passed is (verdict is ProbeVerdict.REFUSED)


class TestProbesAreSafeToRunAgainstProduction:
    """The readiness check runs these on every boot, so they must mutate nothing."""

    def test_probe_sentinels_are_recognizable_and_cannot_be_real_identifiers(
        self,
    ) -> None:
        """Unauthorized by construction, not by the adapter's good behaviour.

        This is what makes the startup check safe: there is no code path on which a
        correct implementation acts on these, so probing a live adapter cannot
        provision, spend or deliver anything.
        """
        from superplane_contracts import (
            PROBE_ALLOCATION_ID,
            PROBE_DIGEST,
            PROBE_OPERATION_ID,
            PROBE_ORG_ID,
        )

        sentinels = (
            PROBE_WORKSPACE,
            PROBE_OPERATION_ID,
            PROBE_AUTHORITY,
            PROBE_ORG_ID,
            PROBE_ALLOCATION_ID,
            PROBE_DIGEST,
        )
        for sentinel in sentinels:
            assert sentinel.startswith("__") and sentinel.endswith("__"), sentinel
            assert "conformance_probe" in sentinel, sentinel
        assert len(set(sentinels)) == len(sentinels), (
            "sentinels must be distinguishable"
        )

    def test_no_sentinel_looks_like_a_plausible_real_value(self) -> None:
        """A sentinel resembling a real id could match a real record by accident."""
        from superplane_contracts import PROBE_ALLOCATION_ID, PROBE_OPERATION_ID

        for sentinel in (PROBE_WORKSPACE, PROBE_OPERATION_ID, PROBE_ALLOCATION_ID):
            assert not sentinel.startswith(("ws-", "op-", "alloc-", "arn:"))

    def test_the_probe_module_reaches_no_provider(self) -> None:
        """Asserted as an absence, because absences decay.

        Nothing structurally prevents a future edit from importing an HTTP client
        "just to check the endpoint responds", and that import is what would turn a
        safe offline probe into a live call on every boot.

        Parsed rather than grepped. A substring scan for ``"requests"`` matches the
        word in a docstring — these modules discuss what an adapter does with
        requests at length — and a test that fails on its own prose gets deleted
        rather than fixed. The import graph is what actually determines reach.
        """
        source = (
            Path(__file__).resolve().parent.parent
            / "contracts"
            / "superplane_contracts"
            / "conformance.py"
        )
        imported = _imported_roots(source)
        forbidden = imported & {"app", "httpx", "boto3", "requests", "urllib", "socket"}
        assert not forbidden, (
            f"conformance.py imports {sorted(forbidden)}; probes must not be able to "
            "reach an implementation or a provider"
        )

    def test_the_probe_module_imports_only_from_its_own_package(self) -> None:
        """Stronger than an explicit denylist, and does not need maintaining.

        A denylist only catches the clients someone thought to forbid. This asserts
        the whole import surface instead: the standard library plus this package's
        own siblings. Any new third-party or application dependency fails, including
        one nobody predicted.
        """
        allowed = {
            "__future__",
            "asyncio",
            "collections",
            "dataclasses",
            "enum",
            "typing",
        }
        # `asyncio` is here because `run_probes` awaits the caller's own call and must
        # distinguish a timeout and a cancellation from an adapter's answer. It is
        # standard library and carries no transport: it cannot reach an
        # implementation or a provider, which is what this test exists to prevent.
        # The client denylist in the test above is what guards that, and it still
        # holds.
        for name in ("conformance.py", "integration.py"):
            source = (
                Path(__file__).resolve().parent.parent
                / "contracts"
                / "superplane_contracts"
                / name
            )
            unexpected = _imported_roots(source) - allowed - {""}
            assert not unexpected, (
                f"{name} imports {sorted(unexpected)}, which is neither the standard "
                "library subset these modules use nor a sibling in this package"
            )


class _Resolved:
    """A returned value carrying a ``state``, as the real progress types do."""

    def __init__(self, state: object) -> None:
        self.state = state


def _imported_roots(source: Path) -> set[str]:
    """The top-level module names ``source`` imports.

    Uses ``ast`` rather than a line scan so a multi-line parenthesized import or a
    conditional one inside a function is still seen. A relative import
    (``from .health import ...``) yields ``""``, which callers treat as "a sibling in
    this package" — the one dependency these modules are allowed.
    """
    tree = ast.parse(source.read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add("")  # relative: a sibling in this package
            elif node.module:
                roots.add(node.module.split(".")[0])
    return roots
