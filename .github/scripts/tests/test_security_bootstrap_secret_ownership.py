"""Bootstrap must not install public credentials or delete rotated versions."""

from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "path,names",
    [
        (
            "modules/agent-factory/webhook-ingress/infra/secrets.tf",
            ["webhook_secret", "github_app_id", "github_app_key", "marker_signing_key"],
        ),
        ("modules/source-control/gitlab/infra/secrets.tf", ["gitlab_root_password"]),
        (
            "modules/source-control/gitlab/infra/ssm.tf",
            ["gitlab_api_token_placeholder"],
        ),
    ],
)
def test_versions_are_relinquished_without_destroying_credentials(path, names):
    text = (ROOT / path).read_text()
    assert not re.search(r'resource\s+"aws_secretsmanager_secret_version"', text)
    assert not re.search(r"recovery_window_in_days\s*=\s*0\b", text)
    for name in names:
        migration = re.search(
            r"removed\s*\{\s*from\s*=\s*aws_secretsmanager_secret_version\."
            + name
            + r"\s+.*?\n\}",
            text,
            re.S,
        )
        assert migration, f"Missing migration for {name}"
        assert re.search(r"destroy\s*=\s*false", migration.group())
