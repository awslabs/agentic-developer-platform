"""Version-stage activation is a gateway grant for exactly the App key."""

import re
from pathlib import Path


def test_version_stage_permission_is_only_for_deployment_app_key():
    # The grant moved from an inline policy in main.tf into the consolidated data
    # policy (#7944); the statement itself is unchanged.
    source = (Path(__file__).parents[1] / "infra/gateway-role-policies.tf").read_text()
    block = re.search(r'Sid\s*=\s*"GitHubAppKeyActivation"(.*?)\n\s*},', source, re.S).group(1)
    assert re.search(r'Action\s*=\s*\["secretsmanager:UpdateSecretVersionStage"\]', block)
    assert 'Effect   = "Allow"' in block
    assert '"${local.gateway_secret_arn_prefix}:adp/${var.environment}/github-app/adp-agent-platform-key-??????"' in block
    assert re.search(r'gateway_secret_arn_prefix\s*=\s*"arn:aws:secretsmanager:\$\{[^}]+\}:\$\{[^}]+\}:secret"', source)
    assert source.count('"secretsmanager:UpdateSecretVersionStage"') == 1
    assert '"secretsmanager:UpdateSecretVersionStage"' not in (Path(__file__).parents[1] / "infra/main.tf").read_text()
