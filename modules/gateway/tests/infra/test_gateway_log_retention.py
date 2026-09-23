"""CloudWatch retention policy guards for issue #5672."""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CHECKOV_CONFIG = _REPO_ROOT / ".github" / "security" / "checkov.yml"
_GATEWAY_ROOT_VARIABLES = _REPO_ROOT / "modules" / "gateway" / "infra" / "variables.tf"
_GATEWAY_MODULE_VARIABLES = _REPO_ROOT / "modules" / "gateway" / "infra" / "modules" / "api-gateway" / "variables.tf"
_GATEWAY_ENVIRONMENT_DIRS = [
    _REPO_ROOT / "environments",
    _REPO_ROOT / "modules" / "gateway" / "infra" / "environments",
]

_GATEWAY_API_LOG_GROUP = _REPO_ROOT / "modules" / "gateway" / "infra" / "modules" / "api-gateway" / "main.tf"


def _block_body(tf_text: str, declaration: str) -> str:
    match = re.search(declaration, tf_text)
    assert match, f"Terraform block not found: {declaration}"
    start = tf_text.index("{", match.start())
    depth = 0
    for index in range(start, len(tf_text)):
        if tf_text[index] == "{":
            depth += 1
        elif tf_text[index] == "}":
            depth -= 1
            if depth == 0:
                return tf_text[start : index + 1]
    raise AssertionError(f"Terraform block is unterminated: {declaration}")


def _log_group_bodies(tf_text: str):
    """Yield (resource_name, body) for each aws_cloudwatch_log_group in the file."""
    for match in re.finditer(r'resource\s+"aws_cloudwatch_log_group"\s+"([^"]+)"\s*\{', tf_text):
        start = match.end() - 1
        depth = 0
        for i in range(start, len(tf_text)):
            if tf_text[i] == "{":
                depth += 1
            elif tf_text[i] == "}":
                depth -= 1
                if depth == 0:
                    yield match.group(1), tf_text[start : i + 1]
                    break


def _all_scanned_log_groups():
    found = []
    for tf_file in sorted(_REPO_ROOT.rglob("*.tf")):
        for name, body in _log_group_bodies(tf_file.read_text()):
            found.append((str(tf_file.relative_to(_REPO_ROOT)), name, body))
    return found


class TestEveryScannedLogGroupDeclaresRetention:
    def test_at_least_one_log_group_is_discovered(self):
        """Guards the discovery itself: a silently-empty sweep would pass vacuously."""
        assert _all_scanned_log_groups(), "found no aws_cloudwatch_log_group resources — has the layout moved?"

    def test_no_log_group_relies_on_the_never_expire_default(self):
        offenders = [f"{path}:{name}" for path, name, body in _all_scanned_log_groups() if "retention_in_days" not in body]

        assert not offenders, (
            f"These log groups do not set retention_in_days, so they default to never expiring: {offenders}. "
            "A log group with unbounded retention makes a retention or deletion commitment impossible to "
            "honour retroactively."
        )


class TestGatewayApiLogGroupRetention:
    """The specific group #5672 is about."""

    def test_gateway_api_log_group_sets_retention(self):
        groups = dict((name, body) for name, body in _log_group_bodies(_GATEWAY_API_LOG_GROUP.read_text()))
        assert "api_gateway" in groups, "the api_gateway log group resource is missing"

        body = groups["api_gateway"]
        match = re.search(r"retention_in_days\s*=\s*(.+)", body)
        assert match, (
            "The gateway API log group does not set retention_in_days. It is the sanctioned gateway "
            "access-log destination and must have a bounded, explicit lifetime."
        )
        assert match.group(1).strip() == "var.log_retention_days"

    @pytest.mark.parametrize(
        ("variables_file", "variable_name"),
        [
            (_GATEWAY_ROOT_VARIABLES, "api_gateway_log_retention_days"),
            (_GATEWAY_MODULE_VARIABLES, "log_retention_days"),
        ],
    )
    def test_gateway_retention_input_defaults_to_at_least_one_year_and_rejects_zero(self, variables_file, variable_name):
        body = _block_body(variables_file.read_text(), rf'variable\s+"{variable_name}"\s*\{{')
        default_match = re.search(r"default\s*=\s*(\d+)", body)
        assert default_match and int(default_match.group(1)) >= 365

        allowed_match = re.search(r"contains\(\[([^]]+)]", body)
        assert allowed_match, f"{variable_name} must validate the supported CloudWatch retention values"
        allowed_values = [int(value) for value in re.findall(r"\d+", allowed_match.group(1))]
        assert allowed_values and min(allowed_values) >= 365

    def test_every_shipped_environment_has_bounded_gateway_retention(self):
        root_body = _block_body(_GATEWAY_ROOT_VARIABLES.read_text(), r'variable\s+"api_gateway_log_retention_days"\s*\{')
        root_default = int(re.search(r"default\s*=\s*(\d+)", root_body).group(1))
        environment_files = sorted(path for directory in _GATEWAY_ENVIRONMENT_DIRS for path in directory.rglob("*.tfvars"))
        assert environment_files, "no shipped Terraform environments were found"

        for environment_file in environment_files:
            match = re.search(r"^\s*api_gateway_log_retention_days\s*=\s*(\d+)\s*$", environment_file.read_text(), re.MULTILINE)
            effective_retention = int(match.group(1)) if match else root_default
            assert effective_retention >= 365, f"{environment_file} sets gateway retention below the scanner-enforced minimum"

    def test_gateway_retention_rule_has_no_inline_exception(self):
        groups = dict((name, body) for name, body in _log_group_bodies(_GATEWAY_API_LOG_GROUP.read_text()))
        assert "CKV_AWS_338" not in groups["api_gateway"]


class TestScannerRetentionPolicy:
    def test_retention_rule_is_not_suppressed_globally(self):
        config = _CHECKOV_CONFIG.read_text()
        assert not re.search(r"^\s*-\s*CKV_AWS_338\s*$", config, re.MULTILINE)

    def test_retention_skip_no_longer_calls_these_logs_disposable(self):
        config = _CHECKOV_CONFIG.read_text()
        assert "ephemeral debugging data" not in config, (
            "The CKV_AWS_338 skip once justified itself with 'logs are ephemeral debugging data, not "
            "audit records'. That reasoning is what allowed a log group carrying caller credentials and "
            "private prompt/completion content to be treated as throwaway (#5672). It is withdrawn; do "
            "not restore it."
        )

    @pytest.mark.parametrize("check_id", ["CKV_AWS_158", "CKV_AWS_18"])
    def test_log_encryption_and_access_logging_remain_unskipped(self, check_id):
        """Both were previously fixed in Terraform rather than skipped — keep it that way."""
        config = _CHECKOV_CONFIG.read_text()
        assert not re.search(rf"^\s*-\s*{check_id}\s*$", config, re.MULTILINE), (
            f"{check_id} has been added back to the checkov skip list. It was fixed in Terraform "
            "(Band B #2380), not suppressed. Suppressing it would remove a control on the same log "
            "groups #5672 is about."
        )
