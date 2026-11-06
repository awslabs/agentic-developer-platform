"""Offline V11 selection checks; collecting browser cases is not live acceptance."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e.product_onboarding.product_v11_selection import main, require_v11_inventory, selectors

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/e2e-new-ui-playwright.yml"
REVIEWED = (
    "tests/e2e/new_ui/test_product_profile.py::test_product_full_journey[idea]",
    "tests/e2e/new_ui/test_product_profile.py::test_product_full_journey[existing_assets]",
    "tests/e2e/new_ui/test_product_profile.py::test_product_full_journey[running_product]",
    "tests/e2e/new_ui/test_product_resume.py::test_v11_cross_client_unavailable",
    "tests/e2e/new_ui/test_product_profile.py::test_v11_release_regressions",
)


def test_v11_workflow_selects_exact_five_cases_without_b02_publisher(tmp_path):
    workflow = WORKFLOW.read_text()
    assert selectors() == REVIEWED
    assert "PRODUCT_V11_MODE: ${{ inputs.product_v11 && 'true' || 'false' }}" in workflow
    assert 'mapfile -t selected_cases < <(python -m tests.e2e.product_onboarding.product_v11_selection --selectors)' in workflow
    assert 'python -m pytest "${selected_cases[@]}" -p regression_pytest' in workflow
    assert 'selected_cases=(tests/e2e/new_ui/)' in workflow
    assert '[[ ! "$PRODUCT_EXPECTED_REVISION" =~ ^[a-f0-9]{40}$ ]]' in workflow
    assert '"$E2E_NEW_UI_EXPECTED_FLAG" != \'on\'' in workflow
    assert 'if: always() && inputs.product_v11' in workflow
    assert 'if: always() && inputs.product_source_revision != \'\' && !inputs.product_v11' in workflow
    assert workflow.index("Verify complete scenario evidence") < workflow.index("Verify exact V11 browser case inventory")
    assert workflow.index("Verify exact V11 browser case inventory") < workflow.index("Upload test results")

    inventory = tmp_path / "inventory.json"
    environment = {**os.environ, "E2E_NEW_UI_ENABLED": "0", "REGRESSION_INVENTORY": str(inventory),
                   "PYTHONPATH": f"{ROOT / '.github/scripts'}:{ROOT}"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *selectors(), "-p", "regression_pytest",
         "--collect-only", "-q", "--disable-warnings"],
        cwd=ROOT, env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert require_v11_inventory(json.loads(inventory.read_text())) == REVIEWED
    assert main(["--inventory", str(inventory)]) == 0
    assert "5 tests collected" in result.stdout


@pytest.mark.parametrize("inventory", [
    [],
    list(REVIEWED[:-1]),
    list(REVIEWED) + ["tests/e2e/new_ui/test_product_profile.py::test_product_create"],
    list(REVIEWED[:-1]) + [REVIEWED[0]],
    list(reversed(REVIEWED)),
    "all new UI tests",
    None,
])
def test_v11_selection_rejects_missing_duplicate_unrelated_or_reordered_cases(inventory):
    with pytest.raises(ValueError, match="V11 browser inventory"):
        require_v11_inventory(inventory)


def test_v11_selector_command_and_missing_inventory_fail_closed(tmp_path, capsys):
    assert main(["--selectors"]) == 0
    assert capsys.readouterr().out.splitlines() == list(REVIEWED)
    with pytest.raises(FileNotFoundError):
        main(["--inventory", str(tmp_path / "missing.json")])
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(list(REVIEWED[:-1])))
    with pytest.raises(ValueError, match="V11 browser inventory"):
        main(["--inventory", str(inventory)])
