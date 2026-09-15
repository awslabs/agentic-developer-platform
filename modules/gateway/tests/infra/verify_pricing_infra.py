"""Exercise the real budget-lambda configuration with Terraform's mock AWS provider.

No AWS API calls or credentials are used. The real archive provider still builds
both Lambda ZIPs, so source omission and Terraform expression errors are checked.
Run from any directory: python tests/infra/verify_pricing_infra.py [--plugin-dir PATH].
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", type=Path, help="Optional local Terraform provider mirror")
    args = parser.parse_args()
    gateway = Path(__file__).resolve().parents[2]
    stage = Path(tempfile.mkdtemp(prefix="adp-pricing-infra-"))
    infra = stage / "infra"
    infra.mkdir()
    for source in (gateway / "infra/modules/budget-lambda").glob("*.tf"):
        shutil.copy2(source, infra / source.name)
    for directory in ("pricing_policy", "lambda"):
        shutil.copytree(gateway / directory, stage / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(Path(__file__).with_name("pricing"), infra / "tests")
    (infra / "versions.tf").write_text(
        'terraform {\n required_version = ">= 1.14.0"\n required_providers {\n'
        ' aws = { source = "hashicorp/aws", version = "~> 6.0" }\n'
        ' archive = { source = "hashicorp/archive", version = "~> 2.0" }\n }\n}\n'
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AWS_", "TF_"))}
    init = ["terraform", "init", "-backend=false", "-input=false", "-no-color"]
    if args.plugin_dir:
        init.append(f"-plugin-dir={args.plugin_dir.resolve()}")
    print(f"Isolated mock-provider test directory: {stage}", flush=True)
    for command in (init, ["terraform", "validate", "-no-color"], ["terraform", "test", "-no-color"]):
        subprocess.run(command, cwd=infra, env=env, check=True)

    # Verify the actual Terraform-generated archives preserve all package bytes.
    expected_policy = sorted((gateway / "pricing_policy").rglob("*.py")) + sorted((gateway / "pricing_policy/snapshots").glob("*.json"))
    for filename, function in (("usage_tracker.zip", "budget-usage-tracker"), ("pricing_refresh.zip", "pricing-refresh")):
        expected = {str(path.relative_to(gateway)): path.read_bytes() for path in expected_policy}
        for directory in (gateway / "lambda/shared", gateway / "lambda" / function):
            for path in directory.glob("*.py"):
                if path.name in expected:
                    raise AssertionError(f"Duplicate flattened archive member: {path.name}")
                expected[path.name] = path.read_bytes()
        with zipfile.ZipFile(infra / filename) as archive:
            assert set(archive.namelist()) == set(expected), filename
            assert all(archive.read(name) == content for name, content in expected.items()), filename
            unpacked = stage / function
            archive.extractall(unpacked)
        subprocess.run(
            [sys.executable, "-m", "pricing_policy.selfcheck"],
            cwd=unpacked,
            env={"PATH": env.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": ""},
            check=True,
        )
        print(f"Verified actual Terraform archive: {filename} ({len(expected)} files)")
    print("Pricing infrastructure mock tests and real archive checks passed.")


if __name__ == "__main__":
    main()
