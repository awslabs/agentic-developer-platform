"""Build private, pinned deployment bundles. Never call AWS, apply, or submit work."""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "modules/gateway"))
from src.l5_devops_queue_observer import worker_zip  # noqa: E402


def source(repository, commit, name):
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Select an immutable full source commit")
    result = subprocess.run(
        ["git", "-C", str(repository), "show", commit + ":" + name],
        check=True, capture_output=True,
    ).stdout
    if not 1 <= len(result) <= 32768:
        raise ValueError("Fixture source exceeds its bounded single-file input")
    return result


def write(path, value):
    content = value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(0o600)


def prepare(config, output):
    if set(config) != {"schema_version", "account_id", "region", "queue", "archive"} or config["schema_version"] != 1:
        raise ValueError("Use the exact version-1 fixture preparation input")
    if not re.fullmatch(r"[0-9]{12}", config["account_id"]) or not re.fullmatch(r"[a-z]{2}-[a-z]+-[1-9]", config["region"]):
        raise ValueError("Select an exact fixture account and region")
    for kind in ("queue", "archive"):
        if set(config[kind]) != {"repository", "commit", "fixture_id"} or not re.fullmatch(r"[a-f0-9]{16}", config[kind]["fixture_id"]):
            raise ValueError("Each fixture requires its repository, full source commit and stable hexadecimal identity")
    if config["queue"]["fixture_id"] == config["archive"]["fixture_id"]:
        raise ValueError("The two fixtures need distinct identities")
    queue_source = source(config["queue"]["repository"], config["queue"]["commit"], "index.mjs")
    archive_source = source(config["archive"]["repository"], config["archive"]["commit"], "main.tf")
    # Source-only archive infrastructure is copied exactly for separate review.
    # No Terraform executable or provider is run by preparation.
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    queue = output / "queue"
    queue.mkdir(mode=0o700)
    template = ROOT / "modules/L5-services/infra/devops-queue-fixture"
    for path in template.glob("*.tf"):
        shutil.copyfile(path, queue / path.name)
        (queue / path.name).chmod(0o600)
    artifact = worker_zip(queue_source)
    (queue / "worker.zip").write_bytes(artifact)
    (queue / "worker.zip").chmod(0o600)
    digest = base64.b64encode(sha256(artifact).digest()).decode()
    values = {
        "aws_account_id": config["account_id"], "aws_region": config["region"],
        "fixture_id": config["queue"]["fixture_id"], "worker_zip": str(queue / "worker.zip"),
        "worker_sha256_base64": digest, "source_commit": config["queue"]["commit"],
    }
    for phase in ("bootstrap", "sealed", "active"):
        write(queue / (phase + ".tfvars.json"), {**values, "phase": phase})
    write(queue / "backend.json", {"path": str(queue / "terraform.tfstate")})
    archive = output / "archive"
    archive.mkdir(mode=0o700)
    (archive / "main.tf").write_bytes(archive_source)
    (archive / "main.tf").chmod(0o600)
    write(archive / "provider.tf", {
        "provider": {"aws": {"region": config["region"], "allowed_account_ids": [config["account_id"]]}},
        "terraform": {"backend": {"local": {"path": str(archive / "terraform.tfstate")}}},
    })
    # Terraform JSON has a .tf.json extension, not HCL's .tf extension.
    (archive / "provider.tf").rename(archive / "provider.tf.json")
    write(archive / "fixture.tfvars.json", {
        "bucket_name": "l5-fixture-" + config["account_id"] + "-archive-" + config["archive"]["fixture_id"],
    })
    manifest = {
        "schema_version": 1, "account_id": config["account_id"], "region": config["region"],
        "aws_calls": 0, "deployed": False, "native_submissions": 0,
        "queue": {"source_commit": config["queue"]["commit"], "source_sha256": sha256(queue_source).hexdigest(),
                  "code_sha256_base64": digest, "fixture_id": config["queue"]["fixture_id"],
                  "phases": ["bootstrap", "sealed", "active"]},
        "archive": {"source_commit": config["archive"]["commit"], "terraform_sha256": sha256(archive_source).hexdigest(),
                    "fixture_id": config["archive"]["fixture_id"], "executable_verification_target": False},
        "files": {str(path.relative_to(output)): sha256(path.read_bytes()).hexdigest()
                  for path in sorted(output.rglob("*")) if path.is_file()},
        "remaining_installation": [
            "Review source and saved Terraform plans in the confirmed fixture account before applying each phase.",
            "Select the two repositories in the Product GitHub installation; refresh source options and pin their returned commit references.",
            "Install exact owner configuration, binding and release-verification delegation through Product APIs.",
            "Select private versioned evidence storage and an asymmetric signing key; admit exact producer permissions through the owner connection.",
            "Bind producer sessions to that owner authority and request-specific evidence references; do not grant ambient Gateway fixture permissions.",
            "Qualify a real native task-to-asset association and scenario result schema before mounting release_verification.",
        ],
    }
    write(output / "preparation.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    manifest = prepare(json.loads(args.config.read_text()), args.output)
    print(json.dumps({"prepared": True, "deployed": False, "aws_calls": 0,
                      "native_submissions": 0, "queue_code_sha256_base64": manifest["queue"]["code_sha256_base64"]}))


if __name__ == "__main__":
    main()
