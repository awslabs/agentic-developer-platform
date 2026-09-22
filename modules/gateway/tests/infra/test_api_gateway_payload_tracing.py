"""Guards the API Gateway logging posture introduced by issue #5672.

Background. `aws_api_gateway_method_settings.settings.data_trace_enabled` writes the
FULL request and response of every call — headers included — into the stage's
CloudWatch log group. Headers are where callers present their bearer tokens and the
internal-plane shared secret; bodies are the prompts and completions. Anything
written there is readable by a much wider population than the service itself: CI
roles, build roles with broad administrative rights, any operator with log read
access. Credentials in logs are directly replayable, and personal data written in
the clear cannot be un-written.

The setting used to be `var.environment != "prod"`. That is the shape of the bug
worth guarding: it was not a single wrong value but a rule that exposed every
environment whose name was not literally "prod", including every future one nobody
thought to add. It is now an explicit `var.enable_payload_tracing`, default false,
set true in no shipped environment.

These tests parse the Terraform source rather than running a plan, for the same
reasons as test_cloudfront_spa_fallback.py: the regression they guard is textual —
someone re-deriving the setting from an environment name, or flipping the default —
and `terraform plan` needs AWS credentials and live data sources.
"""

import re
from pathlib import Path

_MODULE_DIR = Path(__file__).resolve().parents[2] / "infra" / "modules" / "api-gateway"
_MAIN_TF = _MODULE_DIR / "main.tf"
_VARIABLES_TF = _MODULE_DIR / "variables.tf"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ENVIRONMENTS_DIR = _REPO_ROOT / "environments"


def _strip_comments(hcl: str) -> str:
    """Drop `#` and `//` line comments so prose about a setting never matches as code."""
    out = []
    for line in hcl.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def _extract_block(hcl: str, header: str) -> str:
    """Return the text of the brace-balanced block whose opening line contains `header`."""
    start = hcl.index(header)
    depth = 0
    for i in range(start, len(hcl)):
        if hcl[i] == "{":
            depth += 1
        elif hcl[i] == "}":
            depth -= 1
            if depth == 0:
                return hcl[start : i + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _main_tf_code() -> str:
    return _strip_comments(_MAIN_TF.read_text())


def _variables_tf_code() -> str:
    return _strip_comments(_VARIABLES_TF.read_text())


class TestPayloadTracingIsNotDerivedFromEnvironmentName:
    """The original defect: exposure as a side effect of how an environment is named."""

    def test_data_trace_reads_the_explicit_variable(self):
        code = _main_tf_code()
        settings = _extract_block(code, 'resource "aws_api_gateway_method_settings"')

        match = re.search(r"data_trace_enabled\s*=\s*(.+)", settings)
        assert match, "no data_trace_enabled assignment found in aws_api_gateway_method_settings"

        value = match.group(1).strip()
        assert value == "var.enable_payload_tracing", (
            f"data_trace_enabled is {value!r}, expected 'var.enable_payload_tracing'. "
            "Payload tracing must come from one explicit, default-off input so that "
            "enabling it shows up as a reviewable plan diff."
        )

    def test_data_trace_is_never_derived_from_the_environment(self):
        """`var.environment != "prod"` exposed every environment not named exactly "prod"."""
        code = _main_tf_code()
        settings = _extract_block(code, 'resource "aws_api_gateway_method_settings"')

        match = re.search(r"data_trace_enabled\s*=\s*(.+)", settings)
        value = match.group(1)
        assert "var.environment" not in value, (
            f"data_trace_enabled is derived from the environment name ({value.strip()!r}). "
            "That was issue #5672: any environment whose name was not literally 'prod' — "
            "including every future environment — wrote caller credentials and private "
            "prompt/completion content to CloudWatch by default."
        )

    def test_no_method_settings_block_enables_tracing_unconditionally(self):
        """Covers a second method_settings resource being added for a narrower path."""
        code = _main_tf_code()
        assert not re.search(r"data_trace_enabled\s*=\s*true", code), (
            "A method_settings block hard-codes data_trace_enabled = true. Every stage and method path must take it from var.enable_payload_tracing."
        )


class TestPayloadTracingDefaultsOff:
    def test_variable_exists_and_is_boolean(self):
        block = _extract_block(_variables_tf_code(), 'variable "enable_payload_tracing"')
        assert re.search(r"type\s*=\s*bool", block), "enable_payload_tracing must be a bool, not a string"

    def test_variable_defaults_to_false(self):
        block = _extract_block(_variables_tf_code(), 'variable "enable_payload_tracing"')
        match = re.search(r"default\s*=\s*(\S+)", block)
        assert match, "enable_payload_tracing must declare an explicit default"
        assert match.group(1) == "false", (
            f"enable_payload_tracing defaults to {match.group(1)!r}. It must default to false: "
            "an environment that says nothing about payload tracing must not get it."
        )

    def test_no_environment_opts_into_payload_tracing(self):
        """Including any future environment: the assertion is over every tfvars file."""
        offenders = []
        for tfvars in sorted(_ENVIRONMENTS_DIR.rglob("*.tfvars")):
            code = _strip_comments(tfvars.read_text())
            if re.search(r"enable_payload_tracing\s*=\s*true", code):
                offenders.append(str(tfvars.relative_to(_REPO_ROOT)))

        assert not offenders, (
            f"These environments enable full payload tracing: {offenders}. "
            "Payload traces write caller credentials and private conversation content "
            "to CloudWatch; no shipped environment may opt in."
        )


class TestAccessLogRemainsMetadataOnly:
    """The sanctioned gateway log source must stay free of credentials and content."""

    # $context.identity.sourceIp is the caller's address, which the access log has
    # always recorded and which operators need to correlate a request. It is not a
    # credential and not message content. Anything matching these fragments IS.
    _FORBIDDEN_FRAGMENTS = [
        "$input.body",
        "$context.requestOverride",
        "$input.params",
        "authorization",
        "x-api-key",
        "cookie",
        "$context.authorizer.claims",
        "$context.identity.apiKey",
    ]

    def _access_log_format(self) -> str:
        stage = _extract_block(_main_tf_code(), 'resource "aws_api_gateway_stage"')
        return _extract_block(stage, "access_log_settings")

    def test_access_log_carries_no_headers_body_or_credentials(self):
        fmt = self._access_log_format().lower()
        for fragment in self._FORBIDDEN_FRAGMENTS:
            assert fragment.lower() not in fmt, (
                f"The stage access log format references {fragment!r}. The access log is the "
                "one sanctioned gateway log source precisely because it is metadata-only; "
                "adding headers, bodies or credentials to it reintroduces issue #5672 "
                "through the log that replaced payload tracing."
            )

    def test_access_log_still_supports_troubleshooting(self):
        """Turning payload tracing off is only acceptable while these fields remain."""
        fmt = self._access_log_format()
        for field in [
            "$context.requestId",
            "$context.status",
            "$context.httpMethod",
            "$context.resourcePath",
            "$context.integrationErrorMessage",
            "$context.integrationLatency",
        ]:
            assert field in fmt, (
                f"The access log no longer records {field}. Operators lost the payload-trace "
                "view in #5672 on the basis that metadata-level troubleshooting stays intact."
            )
