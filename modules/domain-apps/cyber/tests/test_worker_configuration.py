"""Render the production worker environment fragment, including Terraform whitespace."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
TERRAFORM = shutil.which("terraform")


@pytest.mark.skipif(
    TERRAFORM is None, reason="Terraform is required to render its template"
)
@pytest.mark.parametrize(
    "validation_environment",
    [
        {},
        {
            "ADP_CODEX_VALIDATION_BACKEND": "service",
            "ADP_CODEX_VALIDATION_SERVICE_ENDPOINT": "https://fixture.example/tools/validation",
        },
    ],
)
@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"URL_ANALYSIS_EVIDENCE_BUCKET": "evidence"},
        {"A": "line one\nline two", "B": "true"},
    ],
)
def test_worker_environment_renders_as_yaml(tmp_path, environment, validation_environment):
    source = (
        ROOT / "modules/agent-factory/webhook-ingress/infra/scaledjob.tf"
    ).read_text()
    directive = re.search(
        r"%\{\s*for name, value in [^\n]*local.domain_worker_environment", source
    )
    assert directive is not None
    start = directive.start()
    end = source.index("                  # Issue #4184:", start)
    fragment = source[start:end].replace(
        "local.domain_worker_environment", "environment"
    ).replace(
        "local.codex_validation_service_environment", "validation_environment"
    )
    fragment = fragment.replace("${var.environment}", "dev").replace(
        "${local.account_id}", "123456789012"
    )
    (tmp_path / "env.tftpl").write_text(
        "env:\n                  - name: BEFORE\n                    value: before\n"
        + fragment
    )
    expression = (
        'jsonencode(yamldecode(templatefile("env.tftpl", {environment='
        + json.dumps(environment)
        + ",validation_environment="
        + json.dumps(validation_environment)
        + "})))\n"
    )
    result = subprocess.run(
        [TERRAFORM, "console"],
        cwd=tmp_path,
        input=expression,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    rendered = json.loads(json.loads(result.stdout))["env"]
    assert rendered[0] == {"name": "BEFORE", "value": "before"}
    assert rendered[-1] == {
        "name": "AGENT_RUN_LOGS_BUCKET",
        "value": "adp-dev-agent-run-logs-123456789012",
    }
    assert {row["name"]: row["value"] for row in rendered[1:-1]} == {
        **environment, **validation_environment
    }
