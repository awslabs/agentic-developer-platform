"""Retain only bounded native receipts with the existing runner S3 publisher."""

import hashlib
import importlib.util
import os
from pathlib import Path

from .native_smoke_inputs import aws, private_write, runner_environment
from .native_test_session import AdmissionUnavailable, canonical, private_file
from .operational_release import qualification


def main():
    mode = qualification(os.environ)
    smoke_name = "operational-smoke.json" if mode == "operational" else "native-smoke.json"
    evidence_name = "operational-tools-smoke" if mode == "operational" else "native-tools-smoke"
    digest_name = "operational_smoke_sha256" if mode == "operational" else "native_smoke_sha256"
    env, account, role_name = runner_environment(os.environ)
    directory = Path(os.environ["ADP_TOOLS_SMOKE_EVIDENCE_DIR"])
    paths = []
    for name in (smoke_name, "input-delivery.json"):
        path = directory / name
        if path.exists():
            private_file(path, 16384)
            paths.append(str(path))
    if not paths:
        return
    publisher = (
        Path(__file__).resolve().parents[4] / ".github/scripts/upload_ci_evidence_s3.py"
    )
    spec = importlib.util.spec_from_file_location("ci_evidence_publisher", publisher)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.MAX_BYTES = 32768
    original_archive = module.archive
    original_upload = module.upload

    def archive(files, destination):
        if len(files) > 2:
            raise AdmissionUnavailable("Native receipt file bound exceeded")
        records = original_archive(files, destination)
        if destination.stat().st_size > 65536:
            raise AdmissionUnavailable("Native receipt archive bound exceeded")
        return records

    def upload(bucket, key, path):
        maximum = 8192 if path.suffix == ".json" else 65536
        if path.stat().st_size > maximum:
            raise AdmissionUnavailable("Native receipt upload size bound exceeded")
        return original_upload(bucket, key, path)

    calls = 0

    def bounded_aws(*arguments):
        nonlocal calls
        calls += 1
        if calls > 5:
            raise AdmissionUnavailable("Native evidence request allowance exhausted")
        args = list(arguments)
        if args[0] == "s3api":
            args += ["--expected-bucket-owner", account]
        result = aws(args, env, timeout=10)
        if args[0] == "sts" and (
            result.get("Account") != account
            or not result.get("Arn", "").startswith(
                f"arn:aws:sts::{account}:assumed-role/{role_name}/"
            )
        ):
            raise AdmissionUnavailable("Native evidence runner identity does not match")
        return result

    module.archive = archive
    module.upload = upload
    module.aws = bounded_aws
    # Only these two public-safe private receipts enter the archive; never the
    # bundle, fixture query, login, capability or ambient credential files.
    os.environ.update(
        CI_EVIDENCE_PATH="\n".join(paths),
        CI_EVIDENCE_NAME=evidence_name,
        CI_EVIDENCE_RETENTION_DAYS="14",
        CI_EVIDENCE_ENVIRONMENT="dev",
        CI_EVIDENCE_IF_NO_FILES_FOUND="error",
    )
    manifest = module.main()
    smoke = directory / smoke_name
    if smoke.exists():
        record = canonical(
            {
                "version": 1,
                digest_name: hashlib.sha256(
                    private_file(smoke, 16384)
                ).hexdigest(),
                "manifest": manifest,
            }
        )
        if len(record) > 8192:
            raise AdmissionUnavailable("Native retention receipt size bound exceeded")
        private_write(directory / "retention.json", record)


if __name__ == "__main__":
    try:
        main()
    except (AdmissionUnavailable, ValueError, TypeError, KeyError, OSError):
        raise SystemExit(
            "Native receipt retention failed; release publication refused"
        ) from None
