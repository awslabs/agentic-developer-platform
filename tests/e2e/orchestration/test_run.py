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
from tests.e2e.orchestration.fixtures import FixtureRequest, provision
from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.run import (
    EXIT_CONFIG_INVALID,
    EXIT_FAILED,
    EXIT_INCOMPLETE,
    EXIT_NO_SCENARIOS,
    EXIT_OK,
    EXIT_USAGE,
    STATUS_FAILED,
    STATUS_INCOMPLETE,
    STATUS_PASS,
    STATUS_READY,
    build_parser,
    cleanup_qualification,
    main,
    preflight,
    resume_qualification,
)
from tests.e2e.orchestration.test_fixtures import FakeProvider

QUAL_ID = "q-0123456789abcdef"
ORG = FixtureRequest("org", "organization", "qual-org")


@pytest.fixture
def stub_identity(monkeypatch):
    """Stub sts:GetCallerIdentity so preflight needs no credentials."""
    monkeypatch.setattr(
        runner,
        "_caller_identity",
        lambda: ({"account": "111122223333", "arn": "arn:aws:sts::111122223333:assumed-role/qual"}, None),
    )


@pytest.fixture
def register_scenarios(monkeypatch):
    """Install a fake `tests.e2e.orchestration.scenarios` module (#5157's slot)."""

    def _register(registry: dict):
        module = types.ModuleType("tests.e2e.orchestration.scenarios")
        module.REGISTRY = registry  # type: ignore[attr-defined]
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
        assert outcome.exit_code == EXIT_FAILED
        assert "could not be verified" in outcome.report["detail"]

    def test_preflight_never_exposes_a_secret_value(self, valid_config, stub_identity, register_scenarios):
        """Only reference NAMES are reported, and nothing is resolved."""
        register_scenarios({"bounded": StubAdapter()})
        report = preflight(valid_config).report
        assert report["secret_refs"] == ["github_app_key"]

    def test_preflight_with_no_adapters_is_not_a_pass(self, valid_config, stub_identity):
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
    def test_successful_run_passes_and_records_the_inventory(self, valid_config, register_scenarios):
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_PASS
        assert outcome.exit_code == EXIT_OK
        assert outcome.report["scenarios_executed"] == 1
        assert outcome.report["live_resources"] == 1
        assert adapter.executions == 1

    def test_failing_scenario_reports_failed_and_retains_fixtures(self, valid_config, register_scenarios):
        """Fixtures survive a failure so they can be resumed or cleaned up."""
        register_scenarios({"bounded": StubAdapter(fail=True)})

        outcome = runner.run(valid_config)

        assert outcome.status == STATUS_FAILED
        assert outcome.exit_code == EXIT_FAILED
        assert outcome.report["failures"][0]["scenario"] == "bounded"
        assert "retained for --resume or --cleanup" in outcome.report["detail"]

    def test_max_runs_bound_stops_further_scenarios(self, write_config, register_scenarios):
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


class TestResumeMode:
    def test_resume_reconciles_and_reports_ready(self, valid_config, register_scenarios):
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})
        inventory = Inventory.create(valid_config.artifact_directory, QUAL_ID, "dev")
        adapter.provider.fail_create_after_resource_exists = True
        with pytest.raises(Exception):
            provision(inventory, valid_config, adapter.provider, ORG)
        adapter.provider.fail_create_after_resource_exists = False

        outcome = resume_qualification(valid_config, QUAL_ID)

        assert outcome.status == STATUS_READY
        assert outcome.report["reconciled"] == ["org"]
        assert len(adapter.provider.resources) == 1

    def test_resume_reports_failure_when_a_fixture_cannot_be_reconciled(
        self, valid_config, register_scenarios
    ):
        """A possible leak surfaces as a failure needing a human."""
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})
        inventory = Inventory.create(valid_config.artifact_directory, QUAL_ID, "dev")
        adapter.provider.fail_create = True
        with pytest.raises(Exception):
            provision(inventory, valid_config, adapter.provider, ORG)
        adapter.provider.fail_find = True

        outcome = resume_qualification(valid_config, QUAL_ID)

        assert outcome.status == STATUS_FAILED
        assert outcome.report["reconcile_failed"][0]["fixture_id"] == "org"
        assert "may be leaked" in outcome.report["detail"]

    def test_resume_of_an_unknown_qualification_fails_cleanly(self, write_config):
        code = main(["--config", str(write_config()), "--resume", QUAL_ID])
        assert code == EXIT_FAILED

    def test_resume_refuses_another_environments_inventory(self, write_config, valid_config):
        """Its resource ids belong to another account."""
        from tests.e2e.orchestration.config import load_config

        Inventory.create(valid_config.artifact_directory, QUAL_ID, "staging")
        config = load_config(write_config())  # environment: dev
        with pytest.raises(Exception, match="another environment"):
            resume_qualification(config, QUAL_ID)


class TestCleanupMode:
    def test_cleanup_deletes_verified_fixtures_and_reports_pass(self, valid_config, register_scenarios):
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})
        inventory = Inventory.create(valid_config.artifact_directory, QUAL_ID, "dev")
        provision(inventory, valid_config, adapter.provider, ORG)

        outcome = cleanup_qualification(valid_config, QUAL_ID)

        assert outcome.status == STATUS_PASS
        assert outcome.report["deleted"] == ["org"]
        assert adapter.provider.resources == {}

    def test_cleanup_refuses_foreign_fixtures_and_reports_incomplete(
        self, valid_config, register_scenarios
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

    def test_cleanup_retains_sanitized_evidence(self, valid_config, register_scenarios):
        """Evidence survives cleanup, minus the provider dedupe key."""
        adapter = StubAdapter()
        register_scenarios({"bounded": adapter})
        inventory = Inventory.create(valid_config.artifact_directory, QUAL_ID, "dev")
        provision(inventory, valid_config, adapter.provider, ORG)

        evidence = cleanup_qualification(valid_config, QUAL_ID).report["evidence"]

        assert evidence[0]["observed_resource_id"] == "organization-1"
        assert "idempotency_token" not in evidence[0]
