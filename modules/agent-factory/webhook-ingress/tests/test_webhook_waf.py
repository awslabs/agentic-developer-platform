"""Plan the production WAF resources with an AWS mock, without a backend or AWS calls.

Copy the two complete production files; only the neighbouring stage/KMS resources
are fixtures. The Terraform assertions cover rule ordering, invalid inputs and
logging privacy. They do not simulate AWS's live rate counters or IP-set contents.
"""

import os
import shutil
import subprocess
from pathlib import Path


def test_webhook_waf_plans(tmp_path):
    source = Path(__file__).resolve().parents[1]
    terraform = shutil.which("terraform")
    assert terraform, "Terraform >= 1.14 is required for the mocked WAF tests"
    for name in ("waf.tf", "waf-variables.tf"):
        shutil.copyfile(source / "infra" / name, tmp_path / name)
    fixture = Path(__file__).with_name("fixtures") / "waf"
    shutil.copyfile(fixture / "dependencies.tf", tmp_path / "dependencies.tf")
    (tmp_path / "tests").mkdir()
    shutil.copyfile(fixture / "waf.tftest.hcl", tmp_path / "tests" / "waf.tftest.hcl")
    # A caller's TF_CLI_ARGS or TF_DATA_DIR must not connect the test to a real
    # backend/workspace. The AWS provider is mocked in the test file.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TF_CLI_ARGS", "TF_VAR_", "AWS_"))
        and key not in ("TF_DATA_DIR", "TF_WORKSPACE")
    }
    env.update({"TF_IN_AUTOMATION": "1", "AWS_EC2_METADATA_DISABLED": "true"})
    for args in (
        ["init", "-backend=false", "-input=false", "-no-color"],
        ["validate", "-no-color"],
        ["test", "-no-color"],
    ):
        result = subprocess.run(
            [terraform, *args],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=240,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if args[0] == "test":
            assert "0 failed" in result.stdout
            print(result.stdout)
