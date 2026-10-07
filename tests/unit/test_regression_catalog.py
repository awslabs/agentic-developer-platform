"""Stable coverage identities, focused selection and report integration (offline)."""

import ast
import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
import yaml

from tests.e2e.cli_uplift import cases, report
from tests.regression import catalog

ROOT = catalog.ROOT


def test_catalog_references_and_source_definitions_are_valid():
    data = catalog.load()
    assert len(data["modules"]) >= 48
    assert len(data["features"]) >= 133
    assert len(data["scenarios"]) >= 629
    assert len(data["tests"]) >= 99
    definitions = set()
    for fid, row in data["features"].items():
        assert row["module_id"] == fid[:7]
        assert row["module_id"] in data["modules"]
    for sid, row in data["scenarios"].items():
        assert row["feature_id"] == sid[:12]
        assert row["feature_id"] in data["features"]
        assert row["action"] and row["expected"]
        assert len(row["test_ids"]) == len(set(row["test_ids"]))
        for tid in row["test_ids"]:
            assert sid in data["tests"][tid]["scenario_ids"]
    for tid, row in data["tests"].items():
        path = ROOT / row["path"]
        assert path.is_file(), tid
        assert path.resolve().is_relative_to(ROOT)
        key = row["path"], row["selector"]
        assert key not in definitions
        definitions.add(key)
        assert row["scenario_ids"]
        assert row["module_ids"] == sorted({sid[:7] for sid in row["scenario_ids"]})
        assert row["feature_ids"] == sorted({sid[:12] for sid in row["scenario_ids"]})
        for sid in row["scenario_ids"]:
            assert tid in data["scenarios"][sid]["test_ids"]
        if row["selector"].startswith("CLI "):
            assert row["selector"][4:] in cases.BY_ID
        elif path.suffix == ".py":
            tree = ast.parse(path.read_text())
            for name in row["selector"].split("::"):
                tree = next(
                    node
                    for node in tree.body
                    if isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    )
                    and node.name == name
                )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("MOD-002,mod-001,MOD-002", ["MOD-001", "MOD-002"]),
        ("Agent Models; Organizations", ["MOD-001", "MOD-002"]),
        ("Usage, costs and pricing", ["MOD-030"]),
        ("Usage, costs and pricing;Chat", ["MOD-016", "MOD-030"]),
    ],
)
def test_module_input_names_ids_and_deduplication(value, expected):
    assert catalog.modules(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "MOD-999", "MOD-001,", "all,MOD-001", "MOD-001;echo injected", "MOD-001-F001"],
)
def test_unknown_empty_and_injected_inputs_are_rejected(value):
    with pytest.raises(ValueError):
        catalog.modules(value)


def test_plan_keeps_local_blocked_placeholder_and_unmapped_honest():
    selection = catalog.plan("MOD-001")
    assert selection["lanes"] == ["module-cli"]
    assert selection["cli_scope"] == "case:E38"
    assert selection["unmapped_scenarios"]
    assert "TEST-034" in selection["outside_coordinator"]
    assert catalog.plan("MOD-034")["lanes"] == []
    assert catalog.plan("MOD-029")["lanes"] == []
    assert len(catalog.modules("all")) == len(catalog.load()["modules"])


def test_case_selection_runs_exact_journeys_plus_install_login():
    result = cases.resolve_suites(["case:E38", "case:E38", "case:D03"])
    assert {case.id for case in result} == {"E01", "C01", "E38", "D03"}
    assert not cases.is_full(["case:E38"])
    with pytest.raises(ValueError):
        cases.resolve_suites(["case:E99"])


def test_no_runner_exits_incomplete_and_still_stores_gap_plan(tmp_path):
    target = tmp_path / "plan.json"
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / ".github/scripts/regression_modules.py"),
            "--modules",
            "MOD-034",
            "--output",
            str(target),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 2
    result = json.loads(target.read_text())
    assert result["lanes"] == []
    assert result["without_automated_e2e"]
    assert result["scenarios"]


def test_cli_junit_tags_and_focused_scopes_match_schema():
    matrix = cases.new_matrix(["case:E38"])
    root = ET.fromstring(report.junit(matrix, "synthetic-evaluation"))
    properties = root.findall(".//property")
    assert any(
        p.attrib == {"name": "adp_module_id", "value": "MOD-001"} for p in properties
    )
    schema = report.load_schema()
    assert set("case:" + key for key in cases.BY_ID) <= set(
        schema["properties"]["suites"]["items"]["enum"]
    )
    errors = []
    report._check(
        catalog.cli_tags("E38"),
        schema["properties"]["cases"]["items"]["properties"]["tags"],
        "$",
        errors,
    )
    assert errors == []
    invalid = catalog.cli_tags("E38") | {"secret": "should not be accepted"}
    report._check(
        invalid,
        schema["properties"]["cases"]["items"]["properties"]["tags"],
        "$",
        errors,
    )
    assert errors


def test_real_pytest_selection_tags_parameter_variants_and_excludes_untagged(tmp_path):
    source = tmp_path / "test_sample.py"
    source.write_text(
        'import pytest\n@pytest.mark.parametrize("value", [1,2])\ndef test_selected(value): pass\ndef test_unmapped(): raise AssertionError("must not run")\n'
    )
    data = {
        "modules": {"MOD-001": {"name": "Agent Models"}},
        "tests": {
            "TEST-001": {
                "path": "test_sample.py",
                "selector": "test_selected",
                "kind": "E2E",
                "module_ids": ["MOD-001"],
                "feature_ids": ["MOD-001-F001"],
                "scenario_ids": ["MOD-001-F001-S001"],
            }
        },
    }
    (tmp_path / "conftest.py").write_text(
        "from pathlib import Path\nfrom tests.regression import catalog\n"
        "catalog.ROOT = Path(__file__).parent\n"
        f"catalog.load = lambda: {data!r}\n"
    )
    junit = tmp_path / "result.xml"
    inventory = tmp_path / "inventory.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "regression_pytest",
            "--adp-modules",
            "MOD-001",
            "--junitxml",
            str(junit),
            "-q",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": str(ROOT / ".github/scripts"),
            "REGRESSION_INVENTORY": str(inventory),
        },
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert len(json.loads(inventory.read_text())) == 2
    metadata = json.loads(inventory.with_suffix(".tags.json").read_text())
    assert all(row["test_ids"] == ["TEST-001"] for row in metadata.values())
    root = ET.parse(junit).getroot()
    assert len(root.findall(".//testcase")) == 2
    assert len(root.findall('.//property[@name="adp_scenario_id"]')) == 2


def test_selected_skipped_lane_fails_while_unselected_lanes_are_not_required(
    monkeypatch,
):
    spec = importlib.util.spec_from_file_location(
        "regression_report", ROOT / ".github/scripts/regression_report.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("REGRESSION_MODULES", "MOD-001")
    jobs = {
        name: {"result": "success"}
        for name in ["profile", "before", "after", "smoke", "module-cli"]
    }
    jobs["gateway"] = {"result": "skipped"}
    monkeypatch.setenv("REGRESSION_JOBS", json.dumps(jobs))
    assert module.main(["aggregate", "--required", "gateway"]) == 0
    jobs["module-cli"]["result"] = "skipped"
    monkeypatch.setenv("REGRESSION_JOBS", json.dumps(jobs))
    assert module.main(["aggregate", "--required", "gateway"]) == 1


def test_coordinator_selection_and_cleanup_contracts():
    flow = yaml.safe_load((ROOT / ".github/workflows/adp-regression.yml").read_text())
    jobs = flow["jobs"]
    for row in catalog.load()["tests"].values():
        lane = catalog.lane(row)
        if lane:
            assert lane in jobs
            assert lane in jobs["summary"]["needs"]
            assert lane in jobs["after"]["needs"]
    assert "needs.module-onboarding.outputs.cleanup_ok" in jobs["module-cli"]["if"]
    assert "needs.module-budgets.outputs.cleanup_ok" in jobs["module-cli"]["if"]
    assert "needs.before.result == 'success'" in jobs["module-cli"]["if"]
    for name in ("offline", "cli", "gateway", "credentials", "integrations"):
        assert "inputs.modules == ''" in jobs[name]["if"]
