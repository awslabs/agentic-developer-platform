"""Version-stage activation is a gateway grant for exactly the App key."""

import re
from pathlib import Path


def test_version_stage_permission_is_only_for_deployment_app_key():
    source = (Path(__file__).parents[1] / "infra/main.tf").read_text()
    block = re.search(r'Sid\s*=\s*"GitHubAppKeyActivation"(.*?)\n\s*},', source, re.S).group(1)
    assert re.search(r'Action\s*=\s*\["secretsmanager:UpdateSecretVersionStage"\]', block)
    assert 'Effect   = "Allow"' in block
    assert ':secret:adp/${var.environment}/github-app/adp-agent-platform-key-??????"' in block
    assert source.count('"secretsmanager:UpdateSecretVersionStage"') == 1
