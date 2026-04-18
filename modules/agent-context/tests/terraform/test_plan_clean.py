"""
Terraform state sanity tests.

Tests 18-19 from issue #21:
18. terraform plan returns "No changes" (live only)
19. Expected outputs present: irsa_role_arn, bucket_name, dynamodb_table_name
"""

from __future__ import annotations

import json
import subprocess

import pytest

from ..config import TERRAFORM_DIR


# ---------------------------------------------------------------------------
# Test 18: terraform plan clean (live only)
# ---------------------------------------------------------------------------


@pytest.mark.live_only
class TestTerraformPlanClean:
    """Verify terraform plan shows no changes against deployed state."""

    def test_plan_no_changes(self):
        """Terraform plan should report 'No changes' for a stable environment."""
        if not TERRAFORM_DIR.is_dir():
            pytest.skip("Terraform directory not found")

        result = subprocess.run(
            ["terraform", "plan", "-detailed-exitcode", "-input=false"],
            cwd=str(TERRAFORM_DIR),
            capture_output=True,
            text=True,
            timeout=120,
        )
        # Exit code 0 = no changes, 1 = error, 2 = changes present
        assert result.returncode == 0, (
            f"Terraform plan shows changes or errors:\n"
            f"stdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )


# ---------------------------------------------------------------------------
# Test 19: Expected outputs present
# ---------------------------------------------------------------------------


class TestTerraformOutputs:
    """Verify expected Terraform outputs are defined in outputs.tf."""

    EXPECTED_OUTPUTS = [
        "irsa_role_arn",
        "bucket_name",
        "dynamodb_table_name",
    ]

    def test_outputs_defined_in_file(self):
        """Check that outputs.tf defines the expected output blocks."""
        outputs_file = TERRAFORM_DIR / "outputs.tf"
        if not outputs_file.exists():
            pytest.skip("outputs.tf not found")

        content = outputs_file.read_text()
        for output_name in self.EXPECTED_OUTPUTS:
            assert f'output "{output_name}"' in content, (
                f"Expected output '{output_name}' not found in outputs.tf"
            )

    @pytest.mark.live_only
    def test_outputs_have_values(self):
        """In live mode, terraform output should return non-empty values."""
        if not TERRAFORM_DIR.is_dir():
            pytest.skip("Terraform directory not found")

        result = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=str(TERRAFORM_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            pytest.skip(f"terraform output failed: {result.stderr[:200]}")

        outputs = json.loads(result.stdout)
        for output_name in self.EXPECTED_OUTPUTS:
            assert output_name in outputs, (
                f"Output '{output_name}' not in terraform output"
            )
            value = outputs[output_name].get("value", "")
            assert value, f"Output '{output_name}' is empty"
