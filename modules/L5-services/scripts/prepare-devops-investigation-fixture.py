"""Prepare a private synthetic investigation fixture; never contact AWS or invoke."""

import argparse
import base64
import json
import os
import re
import subprocess
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
from io import BytesIO

ROOT = Path(__file__).resolve().parents[3]
SOURCE = "modules/L5-services/fixtures/devops-investigation/index.mjs"
TERRAFORM = "modules/L5-services/infra/devops-investigation-fixture/"


def pinned(config, path):
    return subprocess.run(["git", "-C", str(config["repository"]), "show", config["commit"] + ":" + path],
                          check=True, capture_output=True).stdout


def write(path, value):
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def prepare(config, output):
    if set(config) != {"schema_version", "account_id", "region", "repository", "commit", "fixture_id", "native_space_id"} or config["schema_version"] != 1:
        raise ValueError("Use the exact version-1 investigation preparation input")
    for field, pattern in {"account_id": r"[0-9]{12}", "region": r"[a-z]{2}-[a-z]+-[1-9]", "commit": r"[a-f0-9]{40}",
                           "fixture_id": r"[a-f0-9]{16}", "native_space_id": r"[A-Za-z0-9-]{1,64}"}.items():
        if not isinstance(config[field], str) or not re.fullmatch(pattern, config[field]):
            raise ValueError("Select exact immutable source, fixture, account, region and actual native space identities")
    source = pinned(config, SOURCE)
    if not 1 <= len(source) <= 32768:
        raise ValueError("Fixture source is unavailable or outside bounds")
    archive = BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        info = ZipInfo("index.mjs", date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        info.external_attr = 0o100600 << 16
        bundle.writestr(info, source)
    artifact = archive.getvalue()
    digest = base64.b64encode(sha256(artifact).digest()).decode()
    terraform = {name: pinned(config, TERRAFORM + name) for name in ("main.tf", "variables.tf", "versions.tf")}
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, content in terraform.items():
        (output / name).write_bytes(content)
        (output / name).chmod(0o600)
    (output / "worker.zip").write_bytes(artifact)
    (output / "worker.zip").chmod(0o600)
    values = {"aws_account_id": config["account_id"], "aws_region": config["region"], "fixture_id": config["fixture_id"],
              "native_space_id": config["native_space_id"], "source_commit": config["commit"], "worker_zip": str(output / "worker.zip"),
              "worker_sha256_base64": digest}
    for phase in ("bootstrap", "sealed", "active"):
        write(output / (phase + ".tfvars.json"), {**values, "phase": phase})
    write(output / "backend.json", {"path": str(output / "terraform.tfstate")})
    for scenario in ("dependency_timeout", "recovered"):
        write(output / (scenario + ".json"), {"schema_version": 1, "scenario": scenario, "correlation_id": config["fixture_id"]})
    name = "l5-investigation-" + config["fixture_id"]
    manifest = {"schema_version": 1, "purpose": "synthetic-investigation", "synthetic": True,
                "account_id": config["account_id"], "region": config["region"], "native_space_id": config["native_space_id"],
                "fixture_id": config["fixture_id"], "source_commit": config["commit"], "source_sha256": sha256(source).hexdigest(),
                "code_sha256_base64": digest, "deployed": False, "aws_calls": 0, "native_submissions": 0,
                "function_arn": f"arn:aws:lambda:{config['region']}:{config['account_id']}:function:{name}",
                "log_group_name": "/aws/lambda/" + name,
                "telemetry_arn": f"arn:aws:logs:{config['region']}:{config['account_id']}:log-group:/aws/lambda/{name}:*",
                "resource_role_arn": f"arn:aws:iam::{config['account_id']}:role/{name}-reader",
                "execution_role_arn": f"arn:aws:iam::{config['account_id']}:role/{name}",
                "phases": ["bootstrap", "sealed", "active"],
                "files": {str(path.relative_to(output)): sha256(path.read_bytes()).hexdigest()
                          for path in sorted(output.iterdir()) if path.is_file()},
                "remaining": ["Review saved plans in the confirmed fixture account before applying each phase.",
                              "Read back the sealed role and isolated network before explicit activation.",
                              "Invoke only the published numeric version with the two prepared synthetic inputs and retain actual results.",
                              "Read actual correlated CloudWatch telemetry; preparation or empty logs are not evidence.",
                              "Review resource-verification owner consent before association; native investigation remains a separate bounded run."]}
    write(output / "preparation.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    manifest = prepare(json.loads(args.config.read_text()), args.output)
    print(json.dumps({"prepared": True, "deployed": False, "aws_calls": 0, "native_submissions": 0,
                      "code_sha256_base64": manifest["code_sha256_base64"]}))


if __name__ == "__main__":
    main()
