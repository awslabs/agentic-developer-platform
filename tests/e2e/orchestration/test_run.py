"""Runner CLI tests (#5156).

Proves the CLI contract: the four modes are explicit and mutually exclusive,
``--preflight`` mutates nothing, and an empty or unexecuted scenario registry
can never report PASS. Network-free — the only AWS call the runner would make
(``sts:GetCallerIdentity``) is stubbed.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from tests.e2e.orchestration import run as runner
from tests.e2e.orchestration.config import ConnectionResolutionError, ResolvedConnection
from tests.e2e.orchestration.fixtures import FixtureError, FixtureRequest, provision
from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.run import (
    EXIT_CONFIG_INVALID,
    EXIT_FAILED,
    EXIT_INCOMPLETE,
    EXIT_NO_SCENARIOS,
    EXIT_OK,
    EXIT_TARGET_UNVERIFIED,
    EXIT_USAGE,
    STATUS_FAILED,
    STATUS_INCOMPLETE,
    STATUS_PASS,
    STATUS_READY,
    STATUS_REFUSED,
    build_parser,
    cleanup_qualification,
    main,
    preflight,
    resume_qualification,
)
from tests.e2e.orchestration.test_fixtures import FakeProvider

QUAL_ID = "q-0123456789abcdef"
ORG = FixtureRequest("org", "organization", "qual-org")

# Sentinel so `resolver=None` ("no registry at all") is distinguishable from
# "argument not passed" (install the default recognising resolver).
_DEFAULT_RESOLVER = object()


@pytest.fixture
def stub_identity(monkeypatch):
    """Stub sts:GetCallerIdentity so no test needs credentials.

    The account matches ``connection.expected_account_id`` in the shared valid
    config, so the target verifies and the mode under test is reached. Tests that
    want a refusal stub a *different* account explicitly.
    """
    monkeypatch.setattr(
        runner,
        "_caller_identity",
        lambda: ({"account": "111122223333", "arn": "arn:aws:sts::111122223333:assumed-role/qual"}, None),
    )


@pytest.fixture
def stub_wrong_account(monkeypatch):
    """Credentials that resolve to an account the config does not authorize."""
    monkeypatch.setattr(
        runner,
        "_caller_identity",
        lambda: ({"account": "999988887777", "arn": "arn:aws:sts::999988887777:assumed-role/other"}, None),
    )


@pytest.fixture
def stub_no_identity(monkeypatch):
    """No readable caller identity at all."""
    monkeypatch.setattr(runner, "_caller_identity", lambda: (None, "no credentials"))


class StubConnectionResolver:
    """A stand-in connection registry: the protocol fixture for this slice.

    The real registry is the platform's and is supplied by #5157; this harness
    only defines the contract. Defaults to "registered and active for the account
    the shared valid config declares" so the ordinary path verifies; tests that
    want a refusal construct one that answers differently or raises.
    """

    def __init__(
        self,
        *,
        account_id: str = "111122223333",
        org: str = "aws-e",
        active: bool = True,
        known: bool = True,
        raises: Exception | None = None,
        answer_ref: str | None = None,
        returns: object = None,
    ):
        self.account_id = account_id
        self.org = org
        self.active = active
        self.known = known
        self.raises = raises
        self.answer_ref = answer_ref
        self.returns = returns
        self.calls: list[str] = []

    def resolve_connection(self, connection_ref: str):
        self.calls.append(connection_ref)
        if self.raises is not None:
            raise self.raises
        if self.returns is not None:
            return self.returns
        if not self.known:
            return None  # positively not registered
        return ResolvedConnection(
            connection_ref=self.answer_ref or connection_ref,
            account_id=self.account_id,
            org=self.org,
            active=self.active,
            detail=None if self.active else "revoked by an administrator",
        )


@pytest.fixture
def stub_resolver(monkeypatch):
    """Install only a recognising connection resolver, with no adapters.

    For tests whose subject is a mode's behaviour *after* the target gate (an
    empty registry, a missing inventory) and which therefore still need the gate
    to pass without registering a scenario.
    """
    module = types.ModuleType("tests.e2e.orchestration.scenarios")
    module.REGISTRY = {}  # type: ignore[attr-defined]
    resolver = StubConnectionResolver()
    module.CONNECTION_RESOLVER = resolver  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tests.e2e.orchestration.scenarios", module)
    return resolver


@pytest.fixture
def register_scenarios(monkeypatch):
    """Install a fake `tests.e2e.orchestration.scenarios` module (#5157's slot).

    Also installs a default resolver that recognises the shared valid config's
    connection, because target verification is now fail-closed: without one every
    mutating mode refuses. Pass ``resolver=`` to override, including ``None`` to
    exercise "no registry available".
    """

    def _register(registry: dict, *, resolver: object = _DEFAULT_RESOLVER):
        module = types.ModuleType("tests.e2e.orchestration.scenarios")
        module.REGISTRY = registry  # type: ignore[attr-defined]
        if resolver is _DEFAULT_RESOLVER:
            resolver = StubConnectionResolver()
        if resolver is not None:
            module.CONNECTION_RESOLVER = resolver  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "tests.e2e.orchestration.scenarios", module)
        return module

    return _register


class StubAdapter:
    """A scenario adapter shaped like the ones #5157 will supply."""

    def __init__(self, provider: FakeProvider | None = None, *, fail: bool = False):
        self.provider = provider or FakeProvider()
        self.providers = (self.provider,)
        self.fail = fail
        self.executions = 0

    def planned_fixtures(self, config):
        return [ORG]

    def execute(self, *, config, inventory, providers):
        self.executions += 1
        if self.fail:
            raise RuntimeError("scenario failed")
        provision(inventory, config, providers["organization"], ORG)


class TestCliModes:
    def test_config_is_required(self):
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["--preflight"])
        assert excinfo.value.code == EXIT_USAGE

    def test_a_mode_is_required(self):
        """Running with no mode must not default to a mutating one."""
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["--config", "c.json"])
        assert excinfo.value.code == EXIT_USAGE

    @pytest.mark.parametrize(
        "conflicting",
        [
            ["--preflight", "--run"],
            ["--run", "--cleanup", QUAL_ID],
            ["--resume", QUAL_ID, "--cleanup", QUAL_ID],
            ["--preflight", "--resume", QUAL_ID],
        ],
    )
    def test_conflicting_modes_are_a_usage_error(self, conflicting):
        """Combining modes is refused rather than resolved by precedence."""
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["--config", "c.json", *conflicting])
        assert excinfo.value.code == EXIT_USAGE

    def test_each_mode_parses_alone(self):
        parser = build_parser()
        assert parser.parse_args(["--config", "c.json", "--preflight"]).preflight is True
        assert parser.parse_args(["--config", "c.json", "--run"]).run is True
        assert parser.parse_args(["--config", "c.json", "--resume", QUAL_ID]).resume == QUAL_ID
        assert parser.parse_args(["--config", "c.json", "--cleanup", QUAL_ID]).cleanup == QUAL_ID

    def test_invalid_config_exits_with_the_config_code(self, write_config, capsys):
        code = main(["--config", str(write_config({"environment": "prod"})), "--preflight"])
        assert code == EXIT_CONFIG_INVALID
        assert "not authorized against production" in capsys.readouterr().err

    def test_missing_config_file_exits_with_the_config_code(self, tmp_path):
        assert main(["--config", str(tmp_path / "absent.json"), "--preflight"]) == EXIT_CONFIG_INVALID


class TestPreflightIsReadOnly:
    def test_preflight_makes_no_mutation(self, valid_config, stub_identity, register_scenarios, artifact_dir):
        """The dry-run must create no inventory and no resource."""
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})

        outcome = preflight(valid_config)

        assert outcome.status == STATUS_READY
        assert outcome.exit_code == EXIT_OK
        assert outcome.report["mutations"] == []
        assert adapter.provider.create_calls == []
        assert adapter.executions == 0
        assert list(artifact_dir.iterdir()) == [], "preflight must not write an inventory"

    def test_preflight_reports_the_resources_a_run_would_create(
        self, valid_config, stub_identity, register_scenarios
    ):
        register_scenarios({"bounded": StubAdapter()})
        report = preflight(valid_config).report
        assert report["planned_resources"] == [
            {"scenario": "bounded", "fixture_id": "org", "kind": "organization"}
        ]

    def test_preflight_reports_the_verified_account(self, valid_config, stub_identity, register_scenarios):
        """It checks the ACTUAL target, not what the config claims."""
        register_scenarios({"bounded": StubAdapter()})
        assert preflight(valid_config).report["caller_identity"]["account"] == "111122223333"

    def test_preflight_fails_when_authorization_cannot_be_verified(
        self, valid_config, register_scenarios, monkeypatch
    ):
        """An unverifiable target blocks the effect; it is never assumed."""
        monkeypatch.setattr(runner, "_caller_identity", lambda: (None, "no credentials"))
        register_scenarios({"bounded": StubAdapter()})

        outcome = preflight(valid_config)

        assert outcome.status == STATUS_FAILED
        assert outcome.exit_code == EXIT_TARGET_UNVERIFIED
        assert "could not be verified" in outcome.report["detail"]

    def test_preflight_never_exposes_a_secret_value(self, valid_config, stub_identity, register_scenarios):
        """Only reference NAMES are reported, and nothing is resolved."""
        register_scenarios({"bounded": StubAdapter()})
        report = preflight(valid_config).report
        assert report["secret_refs"] == ["github_app_key"]

    def test_preflight_with_no_adapters_is_not_a_pass(self, valid_config, stub_identity, stub_resolver):
        outcome = preflight(valid_config)
        assert outcome.status == STATUS_INCOMPLETE
        assert outcome.exit_code == EXIT_NO_SCENARIOS


class TestEmptyRegistryCannotPass:
    def test_run_with_no_adapters_reports_incomplete(self, valid_config):
        """The central guarantee: nothing executed is never a PASS."""
        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_INCOMPLETE
        assert outcome.status != STATUS_PASS
        assert outcome.exit_code == EXIT_NO_SCENARIOS
        assert outcome.report["scenarios_executed"] == 0
        assert "NOT a pass" in outcome.report["detail"]

    def test_run_with_no_adapters_creates_no_inventory(self, valid_config, artifact_dir):
        runner.run(valid_config)
        assert list(artifact_dir.iterdir()) == []

    def test_cli_exit_code_distinguishes_nothing_ran_from_success(self, write_config):
        """CI can tell a vacuous run from a real pass by the exit code alone."""
        code = main(["--config", str(write_config()), "--run"])
        assert code == EXIT_NO_SCENARIOS
        assert code != EXIT_OK

    def test_cli_prints_the_incomplete_status(self, write_config, capsys):
        main(["--config", str(write_config()), "--run"])
        captured = capsys.readouterr()
        assert json.loads(captured.out)["status"] == STATUS_INCOMPLETE
        assert "status=incomplete" in captured.err

    def test_registry_that_is_not_a_dict_is_treated_as_empty(self, valid_config, register_scenarios):
        register_scenarios(["not", "a", "dict"])  # type: ignore[arg-type]
        assert runner.run(valid_config).status == STATUS_INCOMPLETE

    def test_config_naming_an_unregistered_scenario_is_refused(
        self, write_config, register_scenarios, capsys
    ):
        """A missing adapter must not silently shrink the run."""
        register_scenarios({"bounded": StubAdapter()})
        code = main(["--config", str(write_config({"scenarios": ["does-not-exist"]})), "--run"])
        assert code == EXIT_CONFIG_INVALID
        assert "no registered adapter" in capsys.readouterr().err


class TestRun:
    def test_successful_run_passes_and_records_the_inventory(
        self, valid_config, register_scenarios, stub_identity
    ):
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_PASS
        assert outcome.exit_code == EXIT_OK
        assert outcome.report["scenarios_executed"] == 1
        assert outcome.report["live_resources"] == 1
        assert adapter.executions == 1

    def test_failing_scenario_reports_failed_and_retains_fixtures(
        self, valid_config, register_scenarios, stub_identity
    ):
        """Fixtures survive a failure so they can be resumed or cleaned up."""
        register_scenarios({"bounded": StubAdapter(fail=True)})

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_FAILED
        assert outcome.exit_code == EXIT_FAILED
        assert outcome.report["failures"][0]["scenario"] == "bounded"
        assert "retained for --resume or --cleanup" in outcome.report["detail"]

    def test_max_runs_bound_stops_further_scenarios(self, write_config, register_scenarios, stub_identity):
        """The run cap is enforced across scenarios, not just within one."""
        from tests.e2e.orchestration.config import load_config

        config = load_config(write_config({"bounds": {"max_runs": 1}}))
        adapters = {
            "a_first": StubAdapter(),
            "b_second": StubAdapter(),
        }
        register_scenarios(adapters)

        outcome = runner.run(config)

        assert adapters["a_first"].executions == 1
        assert adapters["b_second"].executions == 0
        assert outcome.status == STATUS_FAILED
        assert "max_runs" in outcome.report["failures"][0]["error"]


class TestAttemptsCountAgainstMaxRuns:
    """`max_runs` bounds ATTEMPTS, not successes.

    Counting only successes would make the cap unenforceable in exactly the case
    it matters: an adapter that fails every time would be invoked once per
    registered scenario while the counter stayed at zero.
    """

    def _config(self, write_config, max_runs: int):
        from tests.e2e.orchestration.config import load_config

        return load_config(write_config({"bounds": {"max_runs": max_runs}}))

    def test_max_runs_one_stops_after_a_single_failing_attempt(
        self, write_config, register_scenarios, stub_identity
    ):
        """The checklist case: max_runs=1 with multiple FAILING adapters.

        The first attempt fails and still consumes the single budgeted run, so
        the second adapter is never invoked.
        """
        adapters = {
            "a_first": StubAdapter(fail=True),
            "b_second": StubAdapter(fail=True),
            "c_third": StubAdapter(fail=True),
        }
        register_scenarios(adapters)

        outcome = runner.run(self._config(write_config, 1))

        assert adapters["a_first"].executions == 1
        assert adapters["b_second"].executions == 0, "a failed attempt must still consume the run budget"
        assert adapters["c_third"].executions == 0
        assert outcome.report["attempts"] == 1
        assert outcome.report["scenarios_executed"] == 0
        assert outcome.status == STATUS_FAILED

    def test_a_failed_attempt_is_counted_before_the_adapter_is_invoked(
        self, write_config, register_scenarios, stub_identity
    ):
        """Two failures against max_runs=2 exhaust the budget for a third."""
        adapters = {
            "a_first": StubAdapter(fail=True),
            "b_second": StubAdapter(fail=True),
            "c_third": StubAdapter(),
        }
        register_scenarios(adapters)

        outcome = runner.run(self._config(write_config, 2))

        assert adapters["a_first"].executions == 1
        assert adapters["b_second"].executions == 1
        assert adapters["c_third"].executions == 0
        assert outcome.report["attempts"] == 2
        assert any("max_runs" in f["error"] for f in outcome.report["failures"])

    def test_mixed_success_and_failure_both_consume_the_budget(
        self, write_config, register_scenarios, stub_identity
    ):
        adapters = {
            "a_first": StubAdapter(fail=True),
            "b_second": StubAdapter(),
            "c_third": StubAdapter(),
        }
        register_scenarios(adapters)

        outcome = runner.run(self._config(write_config, 2))

        assert adapters["c_third"].executions == 0
        assert outcome.report["attempts"] == 2
        assert outcome.report["scenarios_executed"] == 1

    def test_the_reported_attempt_count_never_exceeds_the_bound(
        self, write_config, register_scenarios, stub_identity
    ):
        register_scenarios({f"s{i}": StubAdapter(fail=True) for i in range(6)})
        outcome = runner.run(self._config(write_config, 3))
        assert outcome.report["attempts"] == 3


class TestTargetVerificationGatesMutations:
    """Every mutating mode verifies the real target before acting.

    The selected connection is resolved through the registry, and the account it
    is authorized for is compared against ``sts:GetCallerIdentity``. Reporting
    whichever account the credentials happen to reach is not verification, so a
    mismatch or an unreadable identity refuses the mutation.
    """

    def test_run_refuses_a_mismatched_account(
        self, valid_config, register_scenarios, stub_wrong_account, artifact_dir
    ):
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_REFUSED
        assert outcome.exit_code == EXIT_TARGET_UNVERIFIED
        assert adapter.executions == 0
        assert adapter.provider.create_calls == []
        assert "target mismatch" in outcome.report["detail"]
        assert list(artifact_dir.iterdir()) == [], "a refused run must not write an inventory"

    def test_run_refuses_when_the_identity_cannot_be_read(
        self, valid_config, register_scenarios, stub_no_identity, artifact_dir
    ):
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_REFUSED
        assert adapter.executions == 0
        assert list(artifact_dir.iterdir()) == []

    def test_the_refusal_reports_both_the_expected_and_the_observed_account(
        self, valid_config, register_scenarios, stub_wrong_account
    ):
        """An operator needs to see which account was reached, not just 'denied'."""
        register_scenarios({"bounded": StubAdapter()})
        target = runner.run(valid_config).report["target"]
        assert target["expected_account_id"] == "111122223333"
        assert target["observed_account_id"] == "999988887777"
        assert target["verified"] is False

    def test_cleanup_refuses_a_mismatched_account_and_deletes_nothing(
        self, valid_config, register_scenarios, stub_identity, monkeypatch
    ):
        """The most dangerous mode: a wrong-account cleanup must delete nothing."""
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})
        inventory = Inventory.create(valid_config.artifact_directory, QUAL_ID, "dev")
        provision(inventory, valid_config, adapter.provider, ORG)

        # The credentials change between the run and the cleanup.
        monkeypatch.setattr(
            runner,
            "_caller_identity",
            lambda: ({"account": "999988887777", "arn": "arn:aws:sts::999988887777:role/other"}, None),
        )
        outcome = cleanup_qualification(valid_config, QUAL_ID)

        assert outcome.status == STATUS_REFUSED
        assert outcome.exit_code == EXIT_TARGET_UNVERIFIED
        assert adapter.provider.delete_calls == []
        assert adapter.provider.resources != {}, "the fixture must survive a refused cleanup"

    def test_resume_refuses_a_mismatched_account(self, valid_config, register_scenarios, stub_wrong_account):
        register_scenarios({"bounded": StubAdapter()})
        outcome = resume_qualification(valid_config, QUAL_ID)
        assert outcome.status == STATUS_REFUSED
        assert outcome.exit_code == EXIT_TARGET_UNVERIFIED

    def test_resume_refuses_before_it_reads_the_inventory(
        self, valid_config, register_scenarios, stub_wrong_account
    ):
        """The gate precedes the load: an absent inventory is not the error reported."""
        register_scenarios({"bounded": StubAdapter()})
        outcome = resume_qualification(valid_config, "q-neverexisted01")
        assert outcome.status == STATUS_REFUSED
        assert "target mismatch" in outcome.report["detail"]

    def test_a_repository_outside_the_registered_org_is_refused(
        self, write_config, register_scenarios, stub_identity
    ):
        """The registered connection's org must constrain the repository.

        Otherwise a config could name an account it is authorized for while
        acting on a repository belonging to somebody else.
        """
        from tests.e2e.orchestration.config import load_config

        config = load_config(write_config({"connection": {"repository": "someone-else/adp"}}))
        register_scenarios({"bounded": StubAdapter()})

        outcome = runner.run(config)

        assert outcome.status == STATUS_REFUSED
        assert "someone-else" in outcome.report["detail"]
        assert "belongs to 'aws-e'" in outcome.report["detail"]

    def test_a_verified_target_allows_the_run(self, valid_config, register_scenarios, stub_identity):
        """The gate must not be so strict that a correct config cannot run."""
        register_scenarios({"bounded": StubAdapter()})
        outcome = runner.run(valid_config)
        assert outcome.status == STATUS_PASS
        assert outcome.report["target"]["verified"] is True

    def test_cli_exit_code_distinguishes_a_refusal_from_a_failure(
        self, write_config, register_scenarios, stub_wrong_account
    ):
        """"We refused to touch this account" is a different action from "it failed"."""
        register_scenarios({"bounded": StubAdapter()})
        code = main(["--config", str(write_config()), "--run"])
        assert code == EXIT_TARGET_UNVERIFIED
        assert code != EXIT_FAILED
        assert code != EXIT_OK


class TestConnectionMustBeRegistered:
    """The connection registry, not the config, is the authority on the target.

    Comparing the live identity against ``expected_account_id`` alone is
    self-referential: both values come from the same file, so ``connection_ref``
    would be decorative and an unregistered ref could still provision fixtures.
    Every "not registered and active" branch refuses before any adapter runs.
    """

    def _config(self, write_config, ref: str):
        from tests.e2e.orchestration.config import load_config

        return load_config(write_config({"connection": {"connection_ref": ref}}))

    def test_an_unregistered_connection_ref_is_refused(
        self, write_config, register_scenarios, stub_identity, artifact_dir
    ):
        """The reviewer's repro at a1c812bf, inverted.

        Changing ONLY connection_ref to an unregistered value previously still
        returned status=pass, exit 0, and invoked provider.create for qual-org.
        """
        config = self._config(write_config, "unregistered-review-probe")
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter}, resolver=StubConnectionResolver(known=False))

        outcome = runner.run(config)

        assert outcome.status == STATUS_REFUSED
        assert outcome.status != STATUS_PASS
        assert outcome.exit_code == EXIT_TARGET_UNVERIFIED
        assert outcome.exit_code != EXIT_OK
        assert "not a registered connection" in outcome.report["detail"]
        # The point of the fix: no fixture was created and no inventory written.
        assert adapter.executions == 0
        assert adapter.provider.create_calls == []
        assert list(artifact_dir.iterdir()) == []

    def test_the_resolver_is_asked_about_the_configured_ref(
        self, write_config, register_scenarios, stub_identity
    ):
        """connection_ref must actually reach the registry, not be ignored."""
        config = self._config(write_config, "adp-dev-embark1")
        resolver = StubConnectionResolver()
        register_scenarios({"bounded": StubAdapter()}, resolver=resolver)

        runner.run(config)

        assert resolver.calls == ["adp-dev-embark1"]

    def test_a_revoked_connection_is_refused(self, valid_config, register_scenarios, stub_identity):
        """Registered once is not authorized now."""
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter}, resolver=StubConnectionResolver(active=False))

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_REFUSED
        assert "not active" in outcome.report["detail"]
        assert adapter.provider.create_calls == []

    def test_an_unreadable_registry_refuses_rather_than_trusting_the_config(
        self, valid_config, register_scenarios, stub_identity
    ):
        """"Could not determine" must not fall back to the config's own claim."""
        adapter = StubAdapter()
        register_scenarios(
            {"bounded": adapter},
            resolver=StubConnectionResolver(raises=ConnectionResolutionError("registry unreachable")),
        )

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_REFUSED
        assert "registry unreachable" in outcome.report["detail"]
        assert "no mutation is allowed" in outcome.report["detail"]
        assert adapter.provider.create_calls == []

    def test_an_arbitrary_resolver_exception_is_also_a_refusal(
        self, valid_config, register_scenarios, stub_identity
    ):
        """A registry is third-party code; any failure leaves the target unknown."""
        adapter = StubAdapter()
        register_scenarios(
            {"bounded": adapter}, resolver=StubConnectionResolver(raises=TimeoutError("timed out"))
        )

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_REFUSED
        assert "TimeoutError" in outcome.report["detail"]
        assert adapter.provider.create_calls == []

    def test_no_resolver_at_all_is_a_refusal(self, valid_config, register_scenarios, stub_identity):
        """Fail-closed: absent authority is not implicit permission."""
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter}, resolver=None)

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_REFUSED
        assert "no connection resolver is available" in outcome.report["detail"]
        assert adapter.provider.create_calls == []

    def test_a_registry_account_disagreeing_with_the_config_is_refused(
        self, valid_config, register_scenarios, stub_identity
    ):
        """A stale or edited config must not win over the registry.

        The credentials here match the CONFIG's declared account, so the old
        self-referential check would have passed this.
        """
        adapter = StubAdapter()
        register_scenarios(
            {"bounded": adapter}, resolver=StubConnectionResolver(account_id="555566667777")
        )

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_REFUSED
        assert "disagrees with the connection registry" in outcome.report["detail"]
        assert "555566667777" in outcome.report["detail"]
        assert adapter.provider.create_calls == []

    def test_a_registry_org_disagreeing_with_the_config_is_refused(
        self, valid_config, register_scenarios, stub_identity
    ):
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter}, resolver=StubConnectionResolver(org="other-org"))

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_REFUSED
        assert "other-org" in outcome.report["detail"]
        assert adapter.provider.create_calls == []

    def test_credentials_outside_the_registered_account_are_refused(
        self, valid_config, register_scenarios, stub_wrong_account
    ):
        """The registry and the config agree; the credentials are somewhere else.

        The refusal must cite the REGISTERED account as the authority, so the
        message tells an operator what actually authorized the target.
        """
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter}, resolver=StubConnectionResolver())

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_REFUSED
        assert "registered connection" in outcome.report["detail"]
        assert "999988887777" in outcome.report["detail"], "must report the observed account"
        assert adapter.provider.create_calls == []

    def test_a_resolver_answering_about_another_connection_is_refused(
        self, valid_config, register_scenarios, stub_identity
    ):
        """A wiring bug that would otherwise verify the wrong target entirely."""
        adapter = StubAdapter()
        register_scenarios(
            {"bounded": adapter}, resolver=StubConnectionResolver(answer_ref="some-other-connection")
        )

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_REFUSED
        assert "answered for" in outcome.report["detail"]
        assert adapter.provider.create_calls == []

    def test_a_malformed_resolution_is_refused(self, valid_config, register_scenarios, stub_identity):
        """A resolver returning the wrong type must not be trusted."""
        adapter = StubAdapter()
        register_scenarios(
            {"bounded": adapter},
            resolver=StubConnectionResolver(returns={"account_id": "111122223333"}),
        )

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_REFUSED
        assert "not a ResolvedConnection" in outcome.report["detail"]
        assert adapter.provider.create_calls == []

    def test_a_non_account_id_resolution_is_refused(
        self, valid_config, register_scenarios, stub_identity
    ):
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter}, resolver=StubConnectionResolver(account_id="nope"))

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_REFUSED
        assert "12-digit AWS account id" in outcome.report["detail"]
        assert adapter.provider.create_calls == []

    def test_resume_refuses_an_unregistered_connection_before_reading_the_inventory(
        self, valid_config, register_scenarios, stub_identity
    ):
        """The gate precedes the load, so the reported cause is the real one."""
        register_scenarios({"bounded": StubAdapter()}, resolver=StubConnectionResolver(known=False))

        outcome = resume_qualification(valid_config, "q-neverexisted01")

        assert outcome.status == STATUS_REFUSED
        assert "not a registered connection" in outcome.report["detail"]

    def test_cleanup_refuses_an_unregistered_connection_and_deletes_nothing(
        self, valid_config, register_scenarios, stub_identity
    ):
        """The worst outcome available here is deleting a stranger's resource."""
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})
        inventory = Inventory.create(valid_config.artifact_directory, QUAL_ID, "dev")
        provision(inventory, valid_config, adapter.provider, ORG)
        assert adapter.provider.resources, "the fixture must exist before cleanup is attempted"

        # Same config and same inventory; only the registry's answer changes.
        register_scenarios({"bounded": adapter}, resolver=StubConnectionResolver(known=False))
        outcome = cleanup_qualification(valid_config, QUAL_ID)

        assert outcome.status == STATUS_REFUSED
        assert outcome.report["deleted"] == []
        assert adapter.provider.delete_calls == []
        assert adapter.provider.resources, "the fixture must survive a refusal"

    def test_the_verified_report_names_the_registered_connection(
        self, valid_config, register_scenarios, stub_identity
    ):
        """Evidence must record what authorized the run, not just that it ran."""
        register_scenarios({"bounded": StubAdapter()})

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_PASS
        resolved = outcome.report["target"]["resolved_connection"]
        assert resolved == {
            "connection_ref": "adp-dev-embark1",
            "account_id": "111122223333",
            "org": "aws-e",
            "active": True,
        }


class TestResumeMode:
    def test_resume_reconciles_and_reports_ready(self, valid_config, register_scenarios, stub_identity):
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})
        inventory = Inventory.create(valid_config.artifact_directory, QUAL_ID, "dev")
        adapter.provider.fail_create_after_resource_exists = True
        with pytest.raises(FixtureError, match="retained for resume"):
            provision(inventory, valid_config, adapter.provider, ORG)
        adapter.provider.fail_create_after_resource_exists = False

        outcome = resume_qualification(valid_config, QUAL_ID)

        assert outcome.status == STATUS_READY
        assert outcome.report["reconciled"] == ["org"]
        assert len(adapter.provider.resources) == 1

    def test_resume_reports_failure_when_a_fixture_cannot_be_reconciled(
        self, valid_config, register_scenarios, stub_identity
    ):
        """A possible leak surfaces as a failure needing a human."""
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})
        inventory = Inventory.create(valid_config.artifact_directory, QUAL_ID, "dev")
        adapter.provider.fail_create = True
        with pytest.raises(FixtureError, match="retained for resume"):
            provision(inventory, valid_config, adapter.provider, ORG)
        adapter.provider.fail_find = True

        outcome = resume_qualification(valid_config, QUAL_ID)

        assert outcome.status == STATUS_FAILED
        assert outcome.report["reconcile_failed"][0]["fixture_id"] == "org"
        assert "may be leaked" in outcome.report["detail"]

    def test_resume_of_an_unknown_qualification_fails_cleanly(
        self, write_config, stub_identity, stub_resolver
    ):
        code = main(["--config", str(write_config()), "--resume", QUAL_ID])
        assert code == EXIT_FAILED

    def test_resume_refuses_another_environments_inventory(
        self, write_config, valid_config, stub_identity, stub_resolver
    ):
        """Its resource ids belong to another account."""
        from tests.e2e.orchestration.config import load_config

        Inventory.create(valid_config.artifact_directory, QUAL_ID, "staging")
        config = load_config(write_config())  # environment: dev
        with pytest.raises(Exception, match="another environment"):
            resume_qualification(config, QUAL_ID)


class TestCleanupMode:
    def test_cleanup_deletes_verified_fixtures_and_reports_pass(
        self, valid_config, register_scenarios, stub_identity
    ):
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})
        inventory = Inventory.create(valid_config.artifact_directory, QUAL_ID, "dev")
        provision(inventory, valid_config, adapter.provider, ORG)

        outcome = cleanup_qualification(valid_config, QUAL_ID)

        assert outcome.status == STATUS_PASS
        assert outcome.report["deleted"] == ["org"]
        assert adapter.provider.resources == {}

    def test_cleanup_refuses_foreign_fixtures_and_reports_incomplete(
        self, valid_config, register_scenarios, stub_identity
    ):
        """Refusing to delete is a distinct outcome from succeeding."""
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})
        inventory = Inventory.create(valid_config.artifact_directory, QUAL_ID, "dev")
        provision(inventory, valid_config, adapter.provider, ORG)
        adapter.provider.resources["organization-1"]["adp:qualification-id"] = "q-somebodyelse00"

        outcome = cleanup_qualification(valid_config, QUAL_ID)

        assert outcome.status == STATUS_INCOMPLETE
        assert outcome.exit_code == EXIT_INCOMPLETE
        assert outcome.report["deleted"] == []
        assert adapter.provider.delete_calls == []

    def test_cleanup_retains_sanitized_evidence(self, valid_config, register_scenarios, stub_identity):
        """Evidence survives cleanup, minus the provider dedupe key."""
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})
        inventory = Inventory.create(valid_config.artifact_directory, QUAL_ID, "dev")
        provision(inventory, valid_config, adapter.provider, ORG)

        evidence = cleanup_qualification(valid_config, QUAL_ID).report["evidence"]

        assert evidence[0]["observed_resource_id"] == "organization-1"
        assert "idempotency_token" not in evidence[0]
