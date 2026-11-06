"""A selected test is not enough if it only collects or validates a report."""

from dataclasses import replace
from pathlib import Path

import pytest

from tests.e2e.product_onboarding.product_completion_inventory import CASES
from tests.e2e.product_onboarding.product_completion_runner import required_cases


def junit_classname(selector):
    path = Path(selector.split("::", 1)[0])
    if path.is_absolute():
        path = path.relative_to(Path(__file__).resolve().parents[3])
    return ".".join(path.with_suffix("").parts)


def complete_inventory():
    inventory = {issue: dict(criteria) for issue, criteria in CASES.items()}
    for issue, criteria in inventory.items():
        for criterion, spec in criteria.items():
            if not spec.producers:
                inventory[issue][criterion] = replace(
                    spec,
                    producers=(
                        "tests/e2e/new_ui/test_product_profile.py::test_v11_release_regressions",
                    ),
                )
    return inventory


def test_all_original_cases_have_unique_check_names_and_an_execution_slot():
    cases = required_cases(complete_inventory())
    assert len(cases) == 27
    assert "issue-8276-ac-04" not in cases
    assert cases["issue-8250-ac-01"].fixture_owner == "product_scope_fixture"


def test_current_inventory_refuses_missing_deployed_guidance_producer():
    """8256 now selects its deployed producers; 8258 guidance is still unwired."""
    with pytest.raises(
        ValueError, match="issue-8258-ac-02 has no deployed test producer"
    ):
        required_cases()


@pytest.mark.parametrize(
    "selector",
    [
        "tests/e2e/product_onboarding/test_v04.py::live_scope_isolation",
        "tests/e2e/product_onboarding/test_v05.py::live_bidirectional_resume",
        "tests/e2e/product_onboarding/test_v05.py::collect_pinned_resume_case",
        "tests/e2e/product_onboarding/test_v05.py::not_run",
        "pytest --collect-only tests/e2e/new_ui/test_product_profile.py::test_v11_release_regressions",
        "modules/insights/tests/test_v13_registry.py::V13OfflineScenarios::test_registry_lineage",
        "modules/gateway/tests/products/test_i13.py::running_product_adoption",
        "tests/unit/test_product_browser_provider.py::test_suite_namespaces_preserve_attempt_identity_and_serialize_shared_membership",
        "tests/e2e/product_onboarding/test_product_completion_inventory.py::test_original_gateway_browser_cases_have_owners_and_evidence",
    ],
)
def test_collection_and_not_run_cannot_serve_as_producers(selector):
    inventory = complete_inventory()
    inventory[8250]["AC-01"] = replace(inventory[8250]["AC-01"], producers=(selector,))
    with pytest.raises(
        ValueError, match="issue-8250-ac-01 has a nonexecuting producer"
    ):
        required_cases(inventory)


@pytest.mark.parametrize("issue, criterion, report_check", [
    (8256, "AC-01", "tests/e2e/product_onboarding/test_v06.py::live_sources_reuse"),
    (8256, "AC-02", "tests/e2e/product_onboarding/test_v06.py::live_source_status"),
    (8256, "AC-03", "tests/e2e/product_onboarding/test_v06.py::live_source_recovery"),
    (8258, "AC-02", None), (8258, "AC-03", None),
])
def test_live_report_validator_cannot_substitute_for_deployed_producer(issue, criterion, report_check):
    inventory = complete_inventory()
    spec = inventory[issue][criterion]
    inventory[issue][criterion] = replace(spec, producers=(report_check or spec.validator,))
    with pytest.raises(ValueError, match="nonexecuting producer"):
        required_cases(inventory)


def test_deployed_sources_producer_is_accepted_once_the_inventory_can_select_it():
    """The new 8256/AC-01 producer executes a run; the reviewed report check does not."""
    inventory = complete_inventory()
    producer = "tests/e2e/product_onboarding/test_product_v06_live.py::live_sources_reuse"
    inventory[8256]["AC-01"] = replace(inventory[8256]["AC-01"], producers=(producer,))
    assert required_cases(inventory)["issue-8256-ac-01"].producers == (producer,)
    inventory[8256]["AC-01"] = replace(inventory[8256]["AC-01"], producers=())
    with pytest.raises(ValueError, match="issue-8256-ac-01 has no deployed test producer"):
        required_cases(inventory)


def test_missing_original_case_rejected():
    inventory = complete_inventory()
    del inventory[8276]["AC-04"]
    with pytest.raises(ValueError, match="missing or unexpected functional cases"):
        required_cases(inventory)


def test_pytest_runner_uses_real_command_and_requires_executed_case():
    from subprocess import CompletedProcess

    from tests.e2e.product_onboarding.product_completion_runner import run_case

    commands = []

    def execute(command, **kwargs):
        from pathlib import Path
        from xml.etree.ElementTree import Element, ElementTree, SubElement

        commands.append((command, kwargs))
        report = Path(command[-2].split("=", 1)[1])
        suite = Element("testsuite", tests="1", failures="0", errors="0", skipped="0")
        SubElement(suite, "testcase", name=command[-1].rsplit("::", 1)[-1],
                   classname=junit_classname(command[-1]))
        ElementTree(suite).write(report)
        return CompletedProcess(command, 0, "", "")

    spec = complete_inventory()[8268]["AC-02"]
    assert run_case(spec, execute=execute) == [
        {"selector": spec.producers[0], "status": "passed", "tests": 1}
    ]
    assert commands[0][0][-1] == spec.producers[0]
    assert commands[0][0][1:4] == ["-m", "pytest", "-q"]
    assert commands[0][0][4] == f"--rootdir={Path(__file__).resolve().parents[3]}"
    assert commands[0][1]["capture_output"] is True


@pytest.mark.parametrize(
    "tests,failures,errors,skipped,exit_code",
    [
        (0, 0, 0, 0, 0),
        (1, 1, 0, 0, 0),
        (1, 0, 1, 0, 0),
        (1, 0, 0, 1, 0),
        (1, 0, 0, 0, 1),
    ],
)
def test_pytest_runner_rejects_missing_failed_or_skipped_outcomes(
    tests, failures, errors, skipped, exit_code
):
    from subprocess import CompletedProcess

    from tests.e2e.product_onboarding.product_completion_runner import run_case

    def execute(command, **kwargs):
        from pathlib import Path
        from xml.etree.ElementTree import Element, ElementTree, SubElement

        report = Path(command[-2].split("=", 1)[1])
        suite = Element(
            "testsuite",
            tests=str(tests),
            failures=str(failures),
            errors=str(errors),
            skipped=str(skipped),
        )
        for _ in range(tests):
            SubElement(suite, "testcase", name=command[-1].rsplit("::", 1)[-1],
                   classname=junit_classname(command[-1]))
        ElementTree(suite).write(report)
        return CompletedProcess(command, exit_code, "", "")

    with pytest.raises(ValueError, match="failed|skipped|not run"):
        run_case(complete_inventory()[8268]["AC-02"], execute=execute)


def test_cli_case_requires_owned_evaluator_and_evidence():
    from tests.e2e.product_onboarding.product_completion_runner import run_case

    spec = CASES[8250]["AC-03"]
    with pytest.raises(ValueError, match="owned evaluator"):
        run_case(spec)
    with pytest.raises(ValueError, match="verified run evidence"):
        run_case(spec, cli_case=lambda _: {"status": "not_run"})
    assert (
        run_case(
            spec,
            cli_case=lambda _: {
                "status": "passed",
                "selector": "case:PR04-C03",
                "evidence_ref": "evidence/example-cli",
                "run_id": "product-12-1",
            },
        )[0]["status"]
        == "passed"
    )


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_live_command_retains_private_diagnostics_and_partial_report(tmp_path, timeout):
    import json
    from subprocess import CompletedProcess, TimeoutExpired
    from tests.e2e.product_onboarding.product_completion_runner import run_case

    tmp_path.chmod(0o700)
    spec = complete_inventory()[8268]["AC-02"]

    def execute(command, **kwargs):
        report = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        report.write_text("partial failed report")
        report.chmod(0o644)
        if timeout:
            raise TimeoutExpired(command, 3600, output=b"fixture setup reached", stderr=b"provider timed out")
        return CompletedProcess(command, 1, "fixture setup reached", "provider refused access")

    with pytest.raises(ValueError, match="private execution diagnostics"):
        run_case(spec, execute=execute, evidence_dir=tmp_path)
    metadata = json.loads(next(tmp_path.glob("*.execution.json")).read_text())
    assert metadata["timed_out"] is timeout
    assert metadata["returncode"] == (None if timeout else 1)
    assert spec.producers[0] in metadata["command"]
    assert (tmp_path / metadata["outputs"]["stdout"]["path"]).read_text() == "fixture setup reached"
    assert (tmp_path / metadata["outputs"]["stderr"]["path"]).read_text().startswith("provider ")
    assert next(tmp_path.glob("*.xml")).read_text() == "partial failed report"
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.iterdir())


def test_failed_command_output_is_bounded_and_existing_diagnostics_are_preserved(tmp_path):
    import json
    from subprocess import CompletedProcess
    from tests.e2e.product_onboarding.product_completion_runner import run_case

    tmp_path.chmod(0o700)
    spec = complete_inventory()[8268]["AC-02"]
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        return CompletedProcess(command, 1, "x" * (1024 * 1024 + 10), "failure")

    with pytest.raises(ValueError, match="private execution diagnostics"):
        run_case(spec, execute=execute, evidence_dir=tmp_path)
    metadata = json.loads(next(tmp_path.glob("*.execution.json")).read_text())
    assert metadata["outputs"]["stdout"]["truncated"] is True
    assert next(tmp_path.glob("*.stdout.txt")).stat().st_size == 1024 * 1024
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(FileExistsError):
        run_case(spec, execute=execute, evidence_dir=tmp_path)
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before
    assert len(calls) == 1


def test_cli_receipt_for_another_case_cannot_pass_the_requested_selector():
    from tests.e2e.product_onboarding.product_completion_runner import run_case

    spec = CASES[8250]["AC-03"]
    for reported_selector in (None, "case:PR04-C02"):
        with pytest.raises(ValueError, match="verified run evidence"):
            run_case(spec, cli_case=lambda _: {
                "selector": reported_selector,
                "status": "passed",
                "evidence_ref": "evidence/example-cli",
                "run_id": "product-12-1",
            })


def test_runner_executes_a_real_selected_pytest_case():
    from tests.e2e.product_onboarding.product_completion_inventory import Case
    from tests.e2e.product_onboarding.product_completion_runner import run_case

    selector = (
        "tests/e2e/product_onboarding/test_product_completion_inventory.py::"
        "test_collect_only_wrappers_are_not_executable_producers"
    )
    spec = Case(
        "offline_smoke",
        "tests/e2e/product_onboarding/product_completion_inventory.py",
        (selector,),
    )
    assert run_case(spec) == [{"selector": selector, "status": "passed", "tests": 1}]


def test_report_validator_runs_only_after_producer_and_cannot_substitute_for_it():
    from subprocess import CompletedProcess
    from xml.etree.ElementTree import Element, ElementTree, SubElement

    from tests.e2e.product_onboarding.product_completion_inventory import Case
    from tests.e2e.product_onboarding.product_completion_runner import run_case

    producer = "tests/e2e/product_onboarding/test_product_completion_inventory.py::test_collect_only_wrappers_are_not_executable_producers"
    validator = "tests/e2e/product_onboarding/test_product_completion_inventory.py::test_catalog_live_surfaces_and_insights_offline_cli_api_are_distinct"
    spec = Case("owned_report", "evidence/example", (producer,), validator)
    invoked = []

    def execute(command, **kwargs):
        from pathlib import Path

        invoked.append(command[-1])
        report = Path(command[-2].split("=", 1)[1])
        suite = Element("testsuite", tests="1", failures="0", errors="0", skipped="0")
        SubElement(suite, "testcase", name=command[-1].rsplit("::", 1)[-1],
                   classname=junit_classname(command[-1]))
        ElementTree(suite).write(report)
        return CompletedProcess(command, 0, "", "")

    assert [item["selector"] for item in run_case(spec, execute=execute)] == [
        producer,
        validator,
    ]
    assert invoked == [producer, validator]
    with pytest.raises(ValueError, match="not a deployed test producer"):
        run_case(replace(spec, producers=()), execute=execute)


def test_attempt_owned_junit_is_durable_and_never_overwritten(tmp_path):
    from hashlib import sha256
    from pathlib import Path

    from tests.e2e.product_onboarding.product_completion_inventory import Case
    from tests.e2e.product_onboarding.product_completion_runner import run_case

    selector = (
        "tests/e2e/product_onboarding/test_product_completion_inventory.py::"
        "test_collect_only_wrappers_are_not_executable_producers"
    )
    spec = Case(
        "offline_smoke",
        "tests/e2e/product_onboarding/product_completion_inventory.py",
        (selector,),
    )
    result = run_case(spec, evidence_dir=tmp_path)[0]
    report = Path(result["report_path"])
    assert report.parent == tmp_path and report.is_file()
    assert report.stat().st_mode & 0o777 == 0o600
    assert result["report_sha256"] == sha256(report.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="already has"):
        run_case(spec, evidence_dir=tmp_path)
    assert result["report_sha256"] == sha256(report.read_bytes()).hexdigest()
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="private and owned"):
        run_case(spec, evidence_dir=tmp_path)
    tmp_path.chmod(0o700)


def test_attempt_report_refuses_symlink_without_touching_its_target(tmp_path):
    from subprocess import CompletedProcess

    from tests.e2e.product_onboarding.product_completion_inventory import Case
    from tests.e2e.product_onboarding.product_completion_runner import run_case

    target = tmp_path / "unrelated.json"
    target.write_text("private receipt")
    target.chmod(0o600)
    spec = Case("offline_smoke", "tests/e2e/product_onboarding/product_completion_inventory.py",
                ("tests/e2e/product_onboarding/test_product_completion_inventory.py::"
                 "test_original_gateway_browser_cases_have_owners_and_evidence",))

    def execute(command, **kwargs):
        Path(command[5].split("=", 1)[1]).symlink_to(target)
        return CompletedProcess(command, 0, "", "")

    with pytest.raises(ValueError, match="bounded test report"):
        run_case(spec, execute=execute, evidence_dir=tmp_path)
    assert target.read_text() == "private receipt"
    assert target.stat().st_mode & 0o777 == 0o600


def test_complete_inventory_preflights_missing_producer_before_claiming_fixtures(
    tmp_path,
):
    from tests.e2e.product_onboarding.product_completion_runner import run_inventory

    with pytest.raises(ValueError, match="no deployed test producer"):
        run_inventory(tmp_path, cli_case=lambda _: pytest.fail("CLI ran"))
    assert list(tmp_path.iterdir()) == []


def test_suite_dispatches_original_issues_in_order_to_owned_directories(
    tmp_path, monkeypatch
):
    from tests.e2e.product_onboarding import product_completion_runner as runner

    recorded = []

    def fake_scope(cases, **kwargs):
        group = [
            (cases[check_id], kwargs["evidence_root"] / check_id)
            for check_id in ("issue-8250-ac-01", "issue-8250-ac-02", "issue-8250-ac-03")
        ]
        recorded.extend(group)
        for _, path in group:
            path.mkdir(mode=0o700)
        return {path.name: [{"status": "passed"}] for _, path in group}

    def fake_run(spec, **kwargs):
        recorded.append((spec, kwargs["evidence_dir"]))
        return [{"status": "passed"}]

    def fake_shared(cases, issue, **kwargs):
        group = [
            (cases[check_id], kwargs["evidence_root"] / check_id)
            for check_id in (
                (f"issue-{issue}-ac-01",)
                if issue == 8258
                else (
                    f"issue-{issue}-ac-01",
                    f"issue-{issue}-ac-02",
                    f"issue-{issue}-ac-03",
                )
            )
        ]
        recorded.extend(group)
        for _, path in group:
            path.mkdir(mode=0o700)
        return {path.name: [{"status": "passed"}] for _, path in group}

    monkeypatch.setattr(runner, "run_scope_suite", fake_scope)
    monkeypatch.setattr(runner, "run_shared_browser_suite", fake_shared)
    monkeypatch.setattr(runner, "run_catalog_suite", lambda cases, **kwargs: fake_shared(cases, 8273, **kwargs))
    monkeypatch.setattr(runner, "run_case", fake_run)
    monkeypatch.setattr(runner, "verify_fixture_providers", lambda cases: None)
    completed = runner.run_inventory(
        tmp_path, inventory=complete_inventory(), cli_case=lambda _: None
    )
    assert len(completed) == len(recorded) == 27
    assert list(completed)[:3] == [
        "issue-8250-ac-01",
        "issue-8250-ac-02",
        "issue-8250-ac-03",
    ]
    assert {path.name for spec, path in recorded} == set(completed)
    assert all(path.stat().st_mode & 0o777 == 0o700 for spec, path in recorded)
    assert all(path.parent == tmp_path for spec, path in recorded)


def test_scope_suite_runs_browser_once_before_cli_and_retains_shared_junit(tmp_path):
    from subprocess import CompletedProcess
    from xml.etree.ElementTree import Element, ElementTree, SubElement

    from tests.e2e.product_onboarding.product_completion_runner import run_scope_suite

    cases = {
        f"issue-8250-{criterion.lower()}": spec
        for criterion, spec in CASES[8250].items()
    }
    observed = []

    def execute(command, **kwargs):
        from pathlib import Path

        observed.append("browser")
        assert len(command[6:]) == 4
        assert kwargs["cwd"] == tmp_path
        assert kwargs["env"]["PRODUCT_SUITE_ID"] == "issue-8250"
        assert kwargs["env"]["PYTHONPATH"].split(":")[0] == str(
            Path(__file__).resolve().parents[3]
        )
        assert all(
            Path(selector.split("::", 1)[0]).is_absolute() for selector in command[6:]
        )
        assert (tmp_path / "test-results").stat().st_mode & 0o777 == 0o700
        report = Path(command[5].split("=", 1)[1])
        suite = Element("testsuite", tests="4", failures="0", errors="0", skipped="0")
        for selector in command[6:]:
            SubElement(suite, "testcase", name=selector.rsplit("::", 1)[-1],
                       classname=junit_classname(selector))
        ElementTree(suite).write(report)
        return CompletedProcess(command, 0, "", "")

    def cli_case(selector):
        observed.append(selector)
        assert (tmp_path / "issue-8250-ac-01").is_dir()
        return {
            "status": "passed",
            "selector": selector,
            "run_id": "product-12-1",
            "evidence_ref": "evidence/cli",
        }

    results = run_scope_suite(
        cases, evidence_root=tmp_path, cli_case=cli_case, execute=execute
    )
    assert observed == ["browser", "case:PR04-C01", "case:PR04-C02", "case:PR04-C03"]
    assert [len(results[check_id]) for check_id in results] == [3, 3, 1]
    assert (
        results["issue-8250-ac-01"][0]["report_path"]
        == results["issue-8250-ac-02"][0]["report_path"]
    )


@pytest.mark.parametrize("invalid", ["skipped", "missing", "extra"])
def test_scope_suite_refuses_incomplete_browser_before_cli(tmp_path, invalid):
    from subprocess import CompletedProcess
    from xml.etree.ElementTree import Element, ElementTree, SubElement

    from tests.e2e.product_onboarding.product_completion_runner import run_scope_suite

    cases = {
        f"issue-8250-{criterion.lower()}": spec
        for criterion, spec in CASES[8250].items()
    }

    def execute(command, **kwargs):
        from pathlib import Path

        report = Path(command[5].split("=", 1)[1])
        names = [item.rsplit("::", 1)[-1] for item in command[6:]]
        if invalid == "missing":
            names.pop()
        elif invalid == "extra":
            names.append("unexpected")
        suite = Element(
            "testsuite",
            tests=str(len(names)),
            failures="0",
            errors="0",
            skipped=str(int(invalid == "skipped")),
        )
        for index, name in enumerate(names):
            selector = command[6:][min(index, len(command) - 7)]
            SubElement(suite, "testcase", name=name,
                       classname=junit_classname(selector))
        ElementTree(suite).write(report)
        return CompletedProcess(command, 0, "", "")

    with pytest.raises(ValueError, match="skipped, failed or not run"):
        run_scope_suite(
            cases,
            evidence_root=tmp_path,
            cli_case=lambda _: pytest.fail(
                "CLI cannot run without a complete browser suite"
            ),
            execute=execute,
        )


def test_scope_suite_preflights_reviewed_cases_before_claiming_fixtures(tmp_path):
    from tests.e2e.product_onboarding.product_completion_runner import run_scope_suite

    cases = {
        f"issue-8250-{criterion.lower()}": spec
        for criterion, spec in CASES[8250].items()
    }
    cases["issue-8250-ac-02"] = replace(
        cases["issue-8250-ac-02"], producers=("case:PR04-C02",)
    )
    with pytest.raises(ValueError, match="reviewed owned selectors"):
        run_scope_suite(
            cases,
            evidence_root=tmp_path,
            cli_case=lambda _: pytest.fail("CLI ran"),
            execute=lambda *args, **kwargs: pytest.fail("Browser ran"),
        )
    assert list(tmp_path.iterdir()) == []


def test_full_inventory_preflights_authenticated_cli_evaluator(tmp_path, monkeypatch):
    from tests.e2e.product_onboarding.product_completion_runner import run_inventory

    monkeypatch.delenv("PRODUCT_B03_MODE", raising=False)
    with pytest.raises(ValueError, match="pinned, authenticated browser attempt"):
        run_inventory(tmp_path, inventory=complete_inventory())
    assert list(tmp_path.iterdir()) == []


def test_isolated_selected_pytest_executes_from_private_attempt(tmp_path):
    from tests.e2e.product_onboarding.product_completion_runner import _pytest_results

    selector = (
        "tests/e2e/product_onboarding/test_product_completion_inventory.py::"
        "test_collect_only_wrappers_are_not_executable_producers"
    )
    result = _pytest_results(
        (selector,), execute=None, evidence_dir=tmp_path, working_directory=tmp_path
    )
    assert result[0]["status"] == "passed"
    assert result[0]["tests"] == 1
    assert result[0]["command"][-1].endswith("::test_collect_only_wrappers_are_not_executable_producers")
    assert result[0]["report_sha256"]


@pytest.mark.parametrize("issue,expected", [(8252, 4), (8258, 3), (8268, 5)])
def test_shared_browser_fixture_runs_once_per_issue_with_private_results(
    tmp_path, issue, expected
):
    from subprocess import CompletedProcess
    from xml.etree.ElementTree import Element, ElementTree, SubElement

    from tests.e2e.product_onboarding.product_completion_runner import (
        run_shared_browser_suite,
    )

    cases = {
        f"issue-{issue}-{criterion.lower()}": spec
        for criterion, spec in CASES[issue].items()
    }
    calls = []

    def execute(command, **kwargs):
        from pathlib import Path

        calls.append(command)
        assert len(command[6:]) == expected
        assert kwargs["cwd"] == tmp_path / f"issue-{issue}-browser"
        assert kwargs["env"]["PRODUCT_B03_MODE"] == "false"
        assert kwargs["env"]["PRODUCT_SUITE_ID"] == f"issue-{issue}"
        assert kwargs["env"]["E2E_SCREENSHOT_DIR"] == str(
            kwargs["cwd"] / "test-results" / "screenshots"
        )
        assert (kwargs["cwd"] / "test-results").stat().st_mode & 0o777 == 0o700
        report = Path(command[5].split("=", 1)[1])
        suite = Element(
            "testsuite", tests=str(expected), failures="0", errors="0", skipped="0"
        )
        for selector in command[6:]:
            SubElement(suite, "testcase", name=selector.rsplit("::", 1)[-1],
                       classname=junit_classname(selector))
        ElementTree(suite).write(report)
        return CompletedProcess(command, 0, "", "")

    result = run_shared_browser_suite(
        cases, issue, evidence_root=tmp_path, execute=execute
    )
    assert len(calls) == 1
    assert sum(len(items) for items in result.values()) == expected
    assert (
        len({item["report_path"] for items in result.values() for item in items}) == 1
    )


def test_shared_browser_suite_refuses_missing_case_before_claiming_fixture(tmp_path):
    from tests.e2e.product_onboarding.product_completion_runner import (
        shared_browser_selection,
    )

    cases = {
        f"issue-8252-{criterion.lower()}": spec
        for criterion, spec in CASES[8252].items()
    }
    cases["issue-8252-ac-02"] = replace(cases["issue-8252-ac-02"], producers=())
    with pytest.raises(ValueError, match="reviewed owned browser suite"):
        shared_browser_selection(cases, 8252)
    assert list(tmp_path.iterdir()) == []


def test_incomplete_owned_provider_inventory_refuses_before_claiming_any_fixtures(
    tmp_path,
):
    from tests.e2e.product_onboarding.product_completion_runner import run_inventory

    with pytest.raises(
        ValueError, match="issue-8261-ac-01 is missing owned fixture providers"
    ):
        run_inventory(
            tmp_path,
            inventory=complete_inventory(),
            cli_case=lambda _: pytest.fail("CLI ran"),
        )
    assert list(tmp_path.iterdir()) == []


def test_preflight_resolves_all_declared_live_fixture_providers():
    from tests.e2e.product_onboarding.product_completion_runner import (
        required_cases,
        verify_fixture_providers,
    )

    providers = {
        "product_v08_handoff_fixture",
        "product_v08_repository_fixture",
        "product_v08_provider_cleanup_fixture",
        "product_v09_selective_fixture",
        "product_v09_denial_fixture",
        "product_v09_resume_fixture",
        "product_v10_journeys_fixture",
        "product_v10_peer_fixture",
        "product_v10_release_fixture",
    }
    assert (
        verify_fixture_providers(
            required_cases(complete_inventory()), available=providers
        )
        is None
    )
    with pytest.raises(
        ValueError, match="issue-8267-ac-01 is missing owned fixture providers"
    ):
        verify_fixture_providers(
            required_cases(complete_inventory()),
            available=providers - {"product_v10_journeys_fixture"},
        )


def test_preflight_requires_an_actual_pytest_fixture_name(tmp_path, monkeypatch):
    from tests.e2e.product_onboarding import product_completion_runner as runner

    package = tmp_path / "tests/e2e/product_onboarding"
    package.mkdir(parents=True)
    monkeypatch.setattr(runner, "__file__", str(package / "product_completion_runner.py"))
    (package / "test_v08.py").write_text(
        'def live_admin_handoff(request):\n'
        '    request.getfixturevalue("product_v08_handoff_fixture")\n'
    )
    cases = {
        f"issue-{issue}-ac-{number:02}": replace(
            CASES[issue][f"AC-{number:02}"],
            producers=(
                "tests/e2e/product_onboarding/test_v08.py::live_admin_handoff",
            ),
        )
        for issue in (8261, 8264, 8267)
        for number in (1, 2, 3)
    }
    fixture_file = package / "conftest.py"
    for decorator in (
        "@other.fixture",
        '@pytest.fixture(name="another_fixture")',
        '@pytest.fixture(name=unknown_name)',
    ):
        fixture_file.write_text(
            f"{decorator}\ndef product_v08_handoff_fixture():\n    pass\n"
        )
        with pytest.raises(ValueError, match="missing owned fixture providers"):
            runner.verify_fixture_providers(cases)

    for decorator in (
        "@pytest.fixture",
        '@pytest.fixture(name="product_v08_handoff_fixture")',
    ):
        fixture_file.write_text(
            f"{decorator}\ndef product_v08_handoff_fixture():\n    pass\n"
        )
        assert runner.verify_fixture_providers(cases) is None


def test_catalog_suite_orders_owned_api_browser_history_and_cleanup(tmp_path):
    from subprocess import CompletedProcess
    from xml.etree.ElementTree import Element, ElementTree, SubElement

    from tests.e2e.product_onboarding.product_completion_runner import run_catalog_suite

    cases = {f"issue-8273-{criterion.lower()}": spec
             for criterion, spec in CASES[8273].items()}
    selectors = tuple(selector for spec in CASES[8273].values() for selector in spec.producers)
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        assert kwargs["cwd"] == tmp_path / "issue-8273-catalog"
        assert kwargs["env"]["PRODUCT_B03_MODE"] == "false"
        assert tuple(path.rsplit("::", 1)[-1] for path in command[6:]) == tuple(
            selector.rsplit("::", 1)[-1] for selector in selectors
        )
        suite = Element("testsuite", tests="4", failures="0", errors="0", skipped="0")
        for selector in selectors:
            SubElement(suite, "testcase", name=selector.rsplit("::", 1)[-1],
                       classname=junit_classname(selector))
        ElementTree(suite).write(command[5].split("=", 1)[1])
        return CompletedProcess(command, 0, "", "")

    results = run_catalog_suite(cases, evidence_root=tmp_path, execute=execute)
    assert len(commands) == 1
    assert [len(results[check_id]) for check_id in cases] == [2, 1, 1]
    assert [item["selector"] for items in results.values() for item in items] == list(selectors)
    assert all(item["report_sha256"] for items in results.values() for item in items)
    assert (tmp_path / "issue-8273-ac-01").stat().st_mode & 0o777 == 0o700


def test_catalog_suite_refuses_modified_selectors_before_fixture_claim(tmp_path):
    from tests.e2e.product_onboarding.product_completion_runner import run_catalog_suite

    cases = {f"issue-8273-{criterion.lower()}": spec
             for criterion, spec in CASES[8273].items()}
    cases["issue-8273-ac-03"] = replace(
        cases["issue-8273-ac-03"], producers=cases["issue-8273-ac-02"].producers
    )
    with pytest.raises(ValueError, match="owned Catalog selectors"):
        run_catalog_suite(
            cases, evidence_root=tmp_path,
            execute=lambda *args, **kwargs: pytest.fail("Catalog fixture was claimed"),
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("invalid", ["missing", "skipped", "extra"])
def test_catalog_suite_refuses_incomplete_results(tmp_path, invalid):
    from subprocess import CompletedProcess
    from xml.etree.ElementTree import Element, ElementTree, SubElement

    from tests.e2e.product_onboarding.product_completion_runner import run_catalog_suite

    cases = {f"issue-8273-{criterion.lower()}": spec
             for criterion, spec in CASES[8273].items()}

    def execute(command, **kwargs):
        names = [selector.rsplit("::", 1)[-1] for selector in command[6:]]
        if invalid == "missing":
            names.pop()
        elif invalid == "extra":
            names.append("not_selected")
        suite = Element("testsuite", tests=str(len(names)), failures="0", errors="0",
                        skipped=str(int(invalid == "skipped")))
        for index, name in enumerate(names):
            selector = command[6:][min(index, len(command) - 7)]
            SubElement(suite, "testcase", name=name,
                       classname=junit_classname(selector))
        ElementTree(suite).write(command[5].split("=", 1)[1])
        return CompletedProcess(command, 0, "", "")

    with pytest.raises(ValueError, match="skipped, failed or not run"):
        run_catalog_suite(cases, evidence_root=tmp_path, execute=execute)
    assert not (tmp_path / "issue-8273-ac-03").exists()


@pytest.mark.parametrize("classname", ["", "tests.e2e.product_onboarding.unrelated"])
def test_selected_case_requires_matching_junit_module(classname):
    from subprocess import CompletedProcess
    from xml.etree.ElementTree import Element, ElementTree, SubElement

    from tests.e2e.product_onboarding.product_completion_runner import run_case

    def execute(command, **kwargs):
        report = Path(command[-2].split("=", 1)[1])
        suite = Element("testsuite", tests="1", failures="0", errors="0", skipped="0")
        SubElement(suite, "testcase", name=command[-1].rsplit("::", 1)[-1],
                   classname=classname)
        ElementTree(suite).write(report)
        return CompletedProcess(command, 0, "", "")

    with pytest.raises(ValueError, match="skipped, failed or not run"):
        run_case(complete_inventory()[8268]["AC-02"], execute=execute)
