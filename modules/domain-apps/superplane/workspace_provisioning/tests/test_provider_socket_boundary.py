"""Native refusal must identify both socket binds, never an arbitrary plan error."""

import json
from types import SimpleNamespace

import pytest

from workspace_provisioning.provider_socket_boundary import validate_socket_diagnostic


@pytest.mark.parametrize(
    "change",
    [
        None,
        "success",
        "checksum-error",
        "one-provider",
        "duplicate-provider",
        "different-path",
        "permission-error",
        "extra-error",
        "oversized",
    ],
)
def test_only_the_two_exact_socket_failures_are_evidence(change):
    temporary = "/tmp/private-probe/" + "x" * 100
    value = {
        "valid": False,
        "error_count": 1,
        "warning_count": 0,
        "diagnostics": [
            {"severity": "error", "summary": "Failed to load plugin schemas"}
        ],
    }
    stderr = (
        "\n".join(
            f'[ERROR] provider.terraform-provider-{name}: plugin init error: error="listen unix {temporary}/plugin1234: bind: invalid argument"'
            for name in ("aws_v6.65.0_x5", "tls_v4.4.1_x5")
        )
        + "\n"
    )
    result = SimpleNamespace(returncode=1, stdout="", stderr=stderr)
    if change == "success":
        result.returncode = 0
        value["valid"] = True
    elif change == "checksum-error":
        value["diagnostics"][0]["summary"] = "missing or corrupted provider plugins"
    elif change == "one-provider":
        result.stderr = stderr.splitlines()[0] + "\n"
    elif change == "duplicate-provider":
        result.stderr = stderr.replace("tls_v4.4.1_x5", "aws_v6.65.0_x5")
    elif change == "different-path":
        result.stderr = stderr.replace(temporary, "/tmp/other")
    elif change == "permission-error":
        result.stderr = stderr.replace("invalid argument", "permission denied")
    elif change == "extra-error":
        result.stderr += "[ERROR] other failure\n"
    elif change == "oversized":
        result.stderr += "x" * 16384
    result.stdout = json.dumps(value)
    if change is None:
        validate_socket_diagnostic(result, temporary)
    else:
        with pytest.raises(ValueError):
            validate_socket_diagnostic(result, temporary)
