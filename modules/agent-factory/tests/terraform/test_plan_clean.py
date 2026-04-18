"""
Terraform state sanity tests.

Tests 26-27 from the issue:
 26. terraform plan returns "No changes." (live-only)
 27. Expected outputs present in terraform output.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.config import EXPECTED_TF_OUTPUTS, INFRA_DIR


class TestTerraformPlanClean:
    """Test 26: terraform plan against deployed state shows no changes."""

    @pytest.mark.live_only
    def test_plan_no_changes(self):
        if not INFRA_DIR.exists():
            pytest.skip(f"Infra directory not found: {INFRA_DIR}")

        result = subprocess.run(
            ["terraform", "plan", "-detailed-exitcode", "-input=false"],
            cwd=str(INFRA_DIR),
            capture_output=True,
            text=True,
            timeout=120,
        )
        # Exit code 0 = no changes, 1 = error, 2 = changes pending
        assert result.returncode == 0, (
            f"Terraform plan shows pending changes (exit code {result.returncode}).\n"
            f"stdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )


class TestTerraformOutputs:
    """Test 27: Expected terraform outputs are present."""

    @pytest.mark.live_only
    def test_expected_outputs_present(self):
        if not INFRA_DIR.exists():
            pytest.skip(f"Infra directory not found: {INFRA_DIR}")

        result = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=str(INFRA_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            pytest.skip(f"terraform output failed: {result.stderr[:200]}")

        outputs = json.loads(result.stdout)
        output_keys = set(outputs.keys())

        missing = []
        for expected_key in EXPECTED_TF_OUTPUTS:
            if expected_key not in output_keys:
                missing.append(expected_key)

        assert not missing, (
            f"Missing Terraform outputs: {missing}\n"
            f"Available outputs: {sorted(output_keys)}"
        )

    @pytest.mark.live_only
    @pytest.mark.parametrize("output_key", EXPECTED_TF_OUTPUTS)
    def test_output_value_non_empty(self, output_key):
        """Each expected output should have a non-empty value."""
        if not INFRA_DIR.exists():
            pytest.skip(f"Infra directory not found: {INFRA_DIR}")

        result = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=str(INFRA_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            pytest.skip(f"terraform output failed: {result.stderr[:200]}")

        outputs = json.loads(result.stdout)
        if output_key not in outputs:
            pytest.skip(f"Output '{output_key}' not present (will be caught by test_expected_outputs_present)")

        value = outputs[output_key].get("value", "")
        assert value, f"Output '{output_key}' is empty"
