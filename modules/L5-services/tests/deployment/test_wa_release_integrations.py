"""Original integration criteria require observed, unskipped selected test cases."""

from pathlib import Path
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qualification_well_architected_integrations as integrations
import qualification_well_architected_acceptance as acceptance
import qualification_well_architected as driver


def xml_report():
    suite = ET.Element("testsuite")
    for selectors in integrations.SELECTORS.values():
        for selector in selectors:
            module, name = selector.split("::")
            ET.SubElement(suite, "testcase", classname="tests.well_architected." + module[:-3], name=name)
    return suite


@pytest.mark.parametrize("change", [None, "skipped", "missing", "failure", "other_module"])
def test_only_exact_successful_selectors_can_complete_an_original_integration_criterion(change):
    suite = xml_report()
    first = suite[0]
    if change in {"skipped", "failure"}:
        ET.SubElement(first, change)
    elif change == "missing":
        suite.remove(first)
    elif change == "other_module":
        first.set("classname", "tests.another_service.test_unrelated")
    report = integrations.evaluate_xml(ET.tostring(suite), "a" * 40)
    observed = {row["id"]: row for row in report["criteria"]}
    assert len(observed) == 9
    assert observed["WA01/AC-02"]["outcome"] == ("passed" if change is None else "incomplete")
    assert observed["WA05/AC-04"]["outcome"] == "passed"


def test_collector_pins_clean_source_and_passes_no_deployment_tokens_to_tests(tmp_path, monkeypatch):
    directory = tmp_path.resolve() / "private"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "PRIVATE-AWS")
    monkeypatch.setenv("ADP_TOKEN", "PRIVATE-TOKEN")
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", "PRIVATE-KEY")
    calls = []
    context = {"source_revision": "a" * 40, "run_id": 42}

    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(stdout=b"a" * 40 + b"\n", returncode=0)
        if command[:2] == ["git", "status"]:
            return SimpleNamespace(stdout=b"", returncode=0)
        assert command[1:3] == ["-m", "pytest"]
        assert all("PRIVATE-" not in str(value) for value in kwargs["env"].values())
        expected = ["tests/well_architected/" + selector for selectors in integrations.SELECTORS.values() for selector in selectors]
        assert command[-len(expected):] == expected
        (directory / "integration-results.xml").write_bytes(ET.tostring(xml_report()))
        return SimpleNamespace(returncode=0)

    report = integrations.collect(directory, context, driver, run=run)
    assert len(calls) == 3 and all(row["outcome"] == "passed" for row in report["criteria"])
    assert integrations.read(directory, context, driver) == report
    (directory / "integration-results.xml").write_bytes(ET.tostring(ET.Element("testsuite")))
    with pytest.raises(ValueError):
        integrations.read(directory, context, driver)


@pytest.mark.parametrize("dirty", [False, True])
def test_different_or_dirty_git_source_cannot_issue_test_acceptance(dirty):
    def run(command, **kwargs):
        return SimpleNamespace(stdout=(b"a" * 40 if dirty else b"b" * 40) if command[1] == "rev-parse" else b" M changed.py")

    with pytest.raises(ValueError, match="clean reviewed"):
        integrations.verify_source("a" * 40, run=run)


def test_executed_original_integration_rows_advance_without_claiming_installed_or_manual_cases():
    scope = {"tenant_id": "tenant", "product_id": "product", "project_id": "project"}
    cases = [{"run_id": "run-" + str(number), "result_revision": 1, "result": {"scope": scope}} for number in range(2)]
    observations = [{"run_id": case["run_id"], "result_revision": 1, "scope": scope} for case in cases]
    integration = integrations.evaluate_xml(ET.tostring(xml_report()), "a" * 40)
    report = acceptance.evaluate(cases, observations, {"source_revision": "a" * 40}, integration)
    passed = [row for row in report["story_criteria"] if row["outcome"] == "passed"]
    assert {row["id"] for row in passed} == set(integrations.SELECTORS)
    assert all(row["observation"] == "executed-integration" and row["verification_layer"] == "source-integration" for row in passed)
    assert report["outcome"] == "incomplete" and report["cleanup"]["outcome"] == "incomplete"
    assert next(row for row in report["story_criteria"] if row["id"] == "WA07/AC-05")["outcome"] == "incomplete"
    integration["source_revision"] = "b" * 40
    with pytest.raises(ValueError, match="original selected source"):
        acceptance.evaluate(cases, observations, {"source_revision": "a" * 40}, integration)


def test_receipt_progress_does_not_replace_unobserved_revocation_or_provider_evidence():
    scope = {"tenant_id": "tenant", "product_id": "product", "project_id": "project"}
    cases = [{"run_id": "run-" + str(number), "result_revision": 1, "result": {"scope": scope}} for number in range(2)]
    observations = [{"run_id": case["run_id"], "result_revision": 1, "scope": scope,
        "clients": {"publication": {"consumers": []}, "clients": {"api": "a" * 64},
            "usage": {"task": {"status": "completed"}, "retained_authorization": {"authorization": {}}, "provider_effect_count": None},
            "denial": {"status": 404, "source_revocation_observed": False}}} for case in cases]
    report = acceptance.evaluate(cases, observations, {"source_revision": "a" * 40})
    assert report["client_progress"][0]["canonical_completed_task_observed"] is True
    assert report["client_progress"][0]["retained_result_consent_observed"] is True
    assert report["client_progress"][0]["source_revocation_observed"] is False
    assert report["client_progress"][0]["provider_effect_count_observed"] is False
    assert next(row for row in report["live_checks"] if row["id"] == "context-reuse")["outcome"] == "incomplete"
    due = next(row for row in report["unresolved"] if row["code"] == "context-reuse:not-observed")
    assert "source revocation denial" in due["required_evidence"]
    assert "independent provider call count before/after reuse" in due["required_evidence"]
    assert report["outcome"] == "incomplete"
