"""Protect executable references and distinguish reports from producers."""

import ast
from pathlib import Path

from tests.e2e.product_onboarding.product_completion_inventory import CASES

ROOT = Path(__file__).resolve().parents[3]


def test_original_gateway_browser_cases_have_owners_and_evidence():
    assert set(CASES) == {8250, 8252, 8256, 8258, 8261, 8264, 8267, 8268, 8273, 8276}
    for issue, criteria in CASES.items():
        assert set(criteria) == {f"AC-{index:02}" for index in range(1, 5 if issue == 8276 else 4)}, issue
        for criterion, spec in criteria.items():
            assert spec.fixture_owner and (ROOT / spec.evidence_builder).is_file(), (issue, criterion)
            assert spec.producers or spec.validator or spec.offline_checks, (issue, criterion)
            for selector in (*spec.producers, *((spec.validator,) if spec.validator else ()), *spec.offline_checks):
                assert "collect_" not in selector, (issue, criterion)
                if selector.startswith("case:"):
                    assert selector in {"case:PR04-C01", "case:PR04-C02", "case:PR04-C03"}
                    continue
                path, *names = selector.split("::")
                tree = ast.parse((ROOT / path).read_text())
                functions = tree.body if len(names) == 1 else next(
                    item.body for item in tree.body if isinstance(item, ast.ClassDef) and item.name == names[0])
                assert names[-1].split("[")[0] in {item.name for item in functions
                                              if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}, selector


def test_collect_only_wrappers_are_not_executable_producers():
    for issue in (8250, 8252):
        for spec in CASES[issue].values():
            assert spec.producers and not spec.validator
            assert all("test_v04.py::live_" not in item and "test_v05.py::live_" not in item
                       for item in spec.producers)
    for issue in (8256,):
        assert all(not spec.producers and spec.validator for spec in CASES[issue].values())


def test_catalog_live_surfaces_and_insights_offline_cli_api_are_distinct():
    membership = CASES[8273]["AC-01"]
    assert membership.fixture_owner == "catalog_live_provider"
    assert any("test_v12.py::live_catalog_membership" in item for item in membership.producers)
    assert any("test_product_catalog.py::test_catalog_membership" in item for item in membership.producers)
    assert all(spec.producers for spec in CASES[8273].values())
    insights = CASES[8276]
    assert all(not spec.producers and spec.offline_checks for spec in list(insights.values())[:3])
    assert insights["AC-04"].producers == (
        "tests/e2e/product_onboarding/test_v13_live.py::live_insights_cli_checkpoint",)
    assert {item.rsplit("::", 1)[-1] for item in insights["AC-04"].offline_checks} == {
        "test_insights_cli_checkpoint", "test_insights_cli_revocation"}


def test_deployed_handoff_financial_and_cli_cases_execute_owned_run_callbacks():
    for issue, version in ((8261, "08"), (8264, "09"), (8267, "10")):
        for criterion, spec in CASES[issue].items():
            assert spec.producers and not spec.validator, (issue, criterion)
            assert all(selector.startswith(f"tests/e2e/product_onboarding/test_v{version}.py::live_")
                       for selector in spec.producers)
            for selector in spec.producers:
                path, function = selector.split("::")
                source = ast.parse((ROOT / path).read_text())
                node = next(item for item in source.body if isinstance(item, ast.FunctionDef)
                            and item.name == function)
                body = ast.get_source_segment((ROOT / path).read_text(), node)
                assert 'getfixturevalue("product_v' in body
                assert 'pytest.skip(' in body
