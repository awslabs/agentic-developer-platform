"""
Issue #2949: Verify gateway wiring in webhook-ingress Terraform config.

Validates that the webhook Lambda's environment variables (GATEWAY_API_URL,
INTERNAL_API_KEY_ARN) are resolved from data sources rather than defaulting
to empty strings, and that the IAM policy unconditionally grants
secretsmanager:GetSecretValue on the internal-api-key secret.

These are static config checks (parse .tf files) — no live AWS needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INFRA_DIR = Path(__file__).resolve().parents[2] / "infra"


class TestGatewayApiUrlWiring:
    """GATEWAY_API_URL must resolve from SSM data source, not empty var."""

    def test_data_source_exists(self):
        """main.tf declares data.aws_ssm_parameter.gateway_internal_alb_dns."""
        main_tf = (INFRA_DIR / "main.tf").read_text()
        assert 'data "aws_ssm_parameter" "gateway_internal_alb_dns"' in main_tf, (
            "Missing data source: aws_ssm_parameter.gateway_internal_alb_dns in main.tf"
        )

    def test_ssm_parameter_path(self):
        """SSM parameter path follows /adp/<env>/gateway/internal-alb-dns convention."""
        main_tf = (INFRA_DIR / "main.tf").read_text()
        assert "/gateway/internal-alb-dns" in main_tf, (
            "SSM parameter path must contain /gateway/internal-alb-dns"
        )

    def test_local_gateway_api_url_defined(self):
        """locals block defines gateway_api_url with http:// prefix."""
        main_tf = (INFRA_DIR / "main.tf").read_text()
        # Should reference the data source value with http:// prefix
        assert "gateway_api_url" in main_tf
        assert "data.aws_ssm_parameter.gateway_internal_alb_dns.value" in main_tf

    def test_lambda_env_uses_local(self):
        """Lambda env GATEWAY_API_URL references local, not var."""
        lambdas_tf = (INFRA_DIR / "lambdas.tf").read_text()
        # Should use local.gateway_api_url, NOT var.gateway_api_url
        assert "local.gateway_api_url" in lambdas_tf, (
            "GATEWAY_API_URL must reference local.gateway_api_url (resolved from SSM)"
        )
        # Ensure var.gateway_api_url is NOT directly used in the env block
        env_block = _extract_env_block(lambdas_tf)
        assert "var.gateway_api_url" not in env_block, (
            "Lambda env must not reference var.gateway_api_url directly — "
            "use local.gateway_api_url which resolves from SSM with var override"
        )


class TestInternalApiKeyArnWiring:
    """INTERNAL_API_KEY_ARN must resolve from Secrets Manager data source."""

    def test_data_source_exists(self):
        """main.tf declares data.aws_secretsmanager_secret.gateway_internal_api_key."""
        main_tf = (INFRA_DIR / "main.tf").read_text()
        assert 'data "aws_secretsmanager_secret" "gateway_internal_api_key"' in main_tf, (
            "Missing data source: aws_secretsmanager_secret.gateway_internal_api_key in main.tf"
        )

    def test_secret_name_convention(self):
        """Secret name follows adp/<env>/gateway/internal-api-key convention."""
        main_tf = (INFRA_DIR / "main.tf").read_text()
        assert "/gateway/internal-api-key" in main_tf, (
            "Secret name must contain /gateway/internal-api-key"
        )

    def test_local_internal_api_key_arn_defined(self):
        """locals block defines internal_api_key_arn."""
        main_tf = (INFRA_DIR / "main.tf").read_text()
        assert "internal_api_key_arn" in main_tf
        assert "data.aws_secretsmanager_secret.gateway_internal_api_key.arn" in main_tf

    def test_lambda_env_uses_local(self):
        """Lambda env INTERNAL_API_KEY_ARN references local, not var."""
        lambdas_tf = (INFRA_DIR / "lambdas.tf").read_text()
        assert "local.internal_api_key_arn" in lambdas_tf, (
            "INTERNAL_API_KEY_ARN must reference local.internal_api_key_arn"
        )
        env_block = _extract_env_block(lambdas_tf)
        assert "var.internal_api_key_arn" not in env_block, (
            "Lambda env must not reference var.internal_api_key_arn directly"
        )


class TestIamReadInternalApiKey:
    """IAM policy must unconditionally grant GetSecretValue on the internal-api-key."""

    def test_read_internal_api_key_statement_present(self):
        """iam.tf contains ReadInternalApiKey statement."""
        iam_tf = (INFRA_DIR / "iam.tf").read_text()
        assert "ReadInternalApiKey" in iam_tf, (
            "iam.tf must contain a ReadInternalApiKey IAM statement"
        )

    def test_statement_is_unconditional(self):
        """ReadInternalApiKey is NOT wrapped in a conditional (no ternary on var)."""
        iam_tf = (INFRA_DIR / "iam.tf").read_text()
        # The old pattern was: var.internal_api_key_arn != "" ? [...] : []
        # Ensure that pattern is gone
        assert 'var.internal_api_key_arn != ""' not in iam_tf, (
            "ReadInternalApiKey must be unconditional — remove the ternary guard. "
            "The ARN is always resolved from data source now."
        )

    def test_statement_uses_local_arn(self):
        """ReadInternalApiKey references local.internal_api_key_arn, not var."""
        iam_tf = (INFRA_DIR / "iam.tf").read_text()
        # Find the ReadInternalApiKey block and verify it uses local
        idx = iam_tf.find("ReadInternalApiKey")
        assert idx != -1
        # Look at the surrounding ~200 chars after the Sid
        block = iam_tf[idx : idx + 300]
        assert "local.internal_api_key_arn" in block, (
            "ReadInternalApiKey Resource must reference local.internal_api_key_arn"
        )
        assert "var.internal_api_key_arn" not in block, (
            "ReadInternalApiKey Resource must NOT reference var.internal_api_key_arn"
        )

    def test_get_secret_value_action(self):
        """ReadInternalApiKey grants secretsmanager:GetSecretValue."""
        iam_tf = (INFRA_DIR / "iam.tf").read_text()
        idx = iam_tf.find("ReadInternalApiKey")
        block = iam_tf[idx : idx + 300]
        assert "secretsmanager:GetSecretValue" in block


class TestVarOverridePreserved:
    """var.gateway_api_url and var.internal_api_key_arn still exist for pipeline override."""

    def test_gateway_api_url_variable_exists(self):
        """variables.tf still declares gateway_api_url for pipeline threading."""
        variables_tf = (INFRA_DIR / "variables.tf").read_text()
        assert 'variable "gateway_api_url"' in variables_tf

    def test_internal_api_key_arn_variable_exists(self):
        """variables.tf still declares internal_api_key_arn for pipeline threading."""
        variables_tf = (INFRA_DIR / "variables.tf").read_text()
        assert 'variable "internal_api_key_arn"' in variables_tf

    def test_local_prefers_var_when_set(self):
        """locals use var override when non-empty (pipeline-threading path)."""
        main_tf = (INFRA_DIR / "main.tf").read_text()
        # The pattern should be: var.X != "" ? var.X : data_source
        assert 'var.gateway_api_url != ""' in main_tf, (
            "gateway_api_url local must prefer var override when set"
        )
        assert 'var.internal_api_key_arn != ""' in main_tf, (
            "internal_api_key_arn local must prefer var override when set"
        )


# =============================================================================
# Helpers
# =============================================================================


def _extract_env_block(lambdas_tf: str) -> str:
    """Extract the environment { variables { ... } } block from lambdas.tf."""
    # Find the environment block in the Lambda resource
    match = re.search(
        r"environment\s*\{[^}]*variables\s*=\s*\{([^}]+)\}",
        lambdas_tf,
        re.DOTALL,
    )
    return match.group(1) if match else ""
