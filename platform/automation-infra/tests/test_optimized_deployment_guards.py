"""Deployment boundaries must remain enforced under Python optimization."""

from pathlib import Path
import subprocess
import sys

import pytest


DRIVER = Path(__file__).resolve().parents[1] / "deploy-webhook-code.py"


@pytest.mark.parametrize("optimization", ["-O", "-OO"])
@pytest.mark.parametrize(
    "case", ["target_account", "archive_traversal", "poisoned_receipt"]
)
def test_optimized_interpreter_refuses_before_side_effects(
    optimization, case, tmp_path
):
    code = r"""
import pathlib, runpy, sys, zipfile
driver = runpy.run_path(sys.argv[1])
case, root = sys.argv[2], pathlib.Path(sys.argv[3])
if case == "target_account":
    def attempt():
        driver["validate_manifest"]({
            "account_id": "invalid-account", "region": "us-east-1",
            "archive_prefix": "lambda-artifacts/test", "targets": []})
elif case == "archive_traversal":
    path = root / "bad.zip"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("../outside.py", "pass")
    def attempt():
        driver["archive"](path)
else:
    class ForbiddenS3:
        def put_object(self, **kwargs):
            raise RuntimeError("unexpected AWS write")
    receipt = driver["Receipt"](ForbiddenS3(), "fixture", "fixture", root / "receipt.json")
    receipt.poisoned = True
    def attempt():
        receipt.save()
try:
    attempt()
except AssertionError:
    if case == "poisoned_receipt" and (root / "receipt.json").exists():
        raise RuntimeError("refusal happened after a local write")
    print("refused-before-side-effects")
else:
    raise RuntimeError("security validation was optimized away")
"""
    result = subprocess.run(
        [sys.executable, optimization, "-c", code, str(DRIVER), case, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "refused-before-side-effects"
