"""Deliver owner-created native inputs through the existing private CI bucket.

S3 carries no bearer credential and creates no authority. The gateway consumes
the original owner approval; the job's dedicated access JWT stays a secret.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from .native_smoke import SearchFixture
from .native_test_session import (
    AdmissionUnavailable,
    ApprovedReference,
    JWT,
    canonical,
    private_file,
    unique_object,
)

MAX_BUNDLE_BYTES = 32768
MAX_HEAD_ATTEMPTS = 12
DELIVERY_SECONDS = 120
REPOSITORY = "aws-e/adp"
WORKFLOW = "Deploy tools platform"


def refuse(message="Native input delivery is unavailable; inspect the original job"):
    raise AdmissionUnavailable(message)


def binding_environment(reference):
    """Syntax-check owner inputs; this synthetic map does not issue approval."""
    return {
        "ADP_SOURCE_REVISION": reference.get("source_revision"),
        "ADP_DEFINITION_REVISION": reference.get("definition_revision"),
        "ADP_TOOLS_SMOKE_GATEWAY_URL": reference.get("gateway_origin"),
        "GITHUB_RUN_ID": reference.get("github_run_id"),
        "GITHUB_RUN_ATTEMPT": reference.get("github_run_attempt"),
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
    }


def job(environ):
    expected = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_WORKFLOW": WORKFLOW,
        "GITHUB_JOB": "release",
    }
    if any(environ.get(key) != value for key, value in expected.items()):
        refuse("Native inputs require the original direct protected release job")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", environ.get("GITHUB_RUN_ID", "")):
        refuse()
    for name in ("ADP_SOURCE_REVISION", "ADP_DEFINITION_REVISION"):
        if not re.fullmatch(r"[0-9a-f]{40}", environ.get(name, "")):
            refuse()


def key(environ):
    job(environ)
    return (
        f"artifacts/14/{REPOSITORY}/{environ['GITHUB_RUN_ID']}/1/release/native-tools-inputs/"
        f"{environ['ADP_SOURCE_REVISION']}/{environ['ADP_DEFINITION_REVISION']}.json"
    )


def runner_environment(environ):
    result = dict(environ)
    role = result.get("CI_EVIDENCE_RUNNER_ROLE_ARN") or result.get("AWS_ROLE_ARN", "")
    token = result.get("CI_EVIDENCE_RUNNER_TOKEN_FILE") or result.get(
        "AWS_WEB_IDENTITY_TOKEN_FILE", ""
    )
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9/+=,.@_-]+)", role)
    if not match or not token:
        refuse("Native input delivery requires the existing runner IRSA identity")
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN",
        "ADP_TOOLS_SMOKE_OPERATIONAL_ACCESS_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    ):
        result.pop(name, None)
    result.update(
        AWS_ROLE_ARN=role,
        AWS_WEB_IDENTITY_TOKEN_FILE=token,
        AWS_EC2_METADATA_DISABLED="true",
        AWS_CONFIG_FILE="/dev/null",
        AWS_SHARED_CREDENTIALS_FILE="/dev/null",
        BOTO_CONFIG="/dev/null",
        AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="true",
    )
    return result, match.group(1), match.group(2).rsplit("/", 1)[-1]


def aws(arguments, environ, *, timeout=10, missing=False):
    child = {
        **environ,
        "AWS_MAX_ATTEMPTS": "1",
        "AWS_RETRY_MODE": "standard",
        "AWS_PAGER": "",
        "AWS_CLI_AUTO_PROMPT": "off",
    }
    try:
        result = subprocess.run(
            [
                "aws",
                *arguments,
                "--output",
                "json",
                "--no-cli-pager",
                "--cli-connect-timeout",
                "5",
                "--cli-read-timeout",
                "5",
            ],
            env=child,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        refuse()
    if result.returncode:
        # GetObject without ListBucket may report403 for a key not created yet.
        # Only bounded HEAD availability probes may wait on these responses.
        if missing and any(
            code in result.stderr
            for code in (b"(404)", b"(403)", b"(NoSuchKey)", b"(AccessDenied)")
        ):
            return None
        refuse()
    try:
        if len(result.stdout) > 65536:
            refuse()
        parsed = json.loads(result.stdout, object_pairs_hook=unique_object)
        if not isinstance(parsed, dict):
            refuse()
        return parsed
    except (ValueError, TypeError):
        refuse()


def private_write(path, data):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def validate_bundle(bundle, environ, directory, *, now):
    if (
        not isinstance(bundle, dict)
        or set(bundle) != {"version", "approval", "fixture", "approval_receipt"}
        or type(bundle["version"]) is not int
        or bundle["version"] != 1
    ):
        refuse(
            "Native input object must contain only the original approval and explicit fixture"
        )
    approval, fixture = directory / "approval.json", directory / "fixture.json"
    private_write(approval, canonical(bundle["approval"]))
    private_write(fixture, canonical(bundle["fixture"]))
    reference = ApprovedReference.read(approval, environ)
    SearchFixture.read(fixture, reference)
    receipt = bundle["approval_receipt"]
    if (
        not isinstance(receipt, dict)
        or set(receipt)
        != {
            "run_id",
            "approval_revision",
            "scope",
            "cap_usd",
            "expires_at",
            "run_started_at",
        }
        or receipt["run_id"] != reference.run_id
        or receipt["approval_revision"] != reference.approval_revision
        or receipt["scope"] != "all_in"
        or receipt["cap_usd"] != "20"
        or type(receipt["run_started_at"]) is not int
        or receipt["run_started_at"] <= 0
        or type(receipt["expires_at"]) is not int
        or not receipt["run_started_at"]
        <= now
        < min(receipt["expires_at"], receipt["run_started_at"] + 300)
    ):
        refuse("Original owner approval is missing, expired or does not bind this job")
    return reference


def download(
    environ=None,
    *,
    call=aws,
    monotonic=time.monotonic,
    wall_clock=time.time,
    sleep=time.sleep,
):
    environ = dict(os.environ if environ is None else environ)
    object_key = key(environ)
    token = environ.get("ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN", "")
    if not isinstance(token, str) or not JWT.fullmatch(token) or len(token) > 16384:
        refuse(
            "Missing dedicated native test ACCESS JWT secret; refresh tokens are unsupported"
        )
    region = environ.get("ADP_TOOLS_AWS_REGION", "")
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region):
        refuse()
    runner, account, role_name = runner_environment(environ)
    bucket = f"adp-dev-ci-evidence-{account}"
    deadline = monotonic() + DELIVERY_SECONDS
    identity = call(
        ["sts", "get-caller-identity", "--region", region], runner, timeout=10
    )
    if identity.get("Account") != account or not identity.get("Arn", "").startswith(
        f"arn:aws:sts::{account}:assumed-role/{role_name}/"
    ):
        refuse("Native input delivery did not authenticate as the original runner")
    directory = Path(environ["RUNNER_TEMP"]) / "adp-native-tools-inputs"
    directory.mkdir(mode=0o700)
    head = None
    head_attempts = 0
    for _ in range(MAX_HEAD_ATTEMPTS):
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        head_attempts += 1
        head = call(
            [
                "s3api",
                "head-object",
                "--bucket",
                bucket,
                "--key",
                object_key,
                "--expected-bucket-owner",
                account,
                "--checksum-mode",
                "ENABLED",
                "--region",
                region,
            ],
            runner,
            timeout=min(10, remaining),
            missing=True,
        )
        if head is not None:
            break
        remaining = deadline - monotonic()
        if remaining > 0 and head_attempts < MAX_HEAD_ATTEMPTS:
            sleep(min(10, remaining))
    if (
        head is None
        or type(head.get("ContentLength")) is not int
        or not 0 < head["ContentLength"] <= MAX_BUNDLE_BYTES
        or head.get("ServerSideEncryption") != "AES256"
        or not isinstance(head.get("VersionId"), str)
        or head["VersionId"] in {"", "null"}
        or not isinstance(head.get("ETag"), str)
        or not isinstance(head.get("ChecksumSHA256"), str)
        or deadline - monotonic() <= 0
    ):
        refuse(
            "Original job approval/fixture object was not available within the bounded delivery window"
        )
    object_file = directory / "bundle.json"
    private_write(object_file, b"")
    response = call(
        [
            "s3api",
            "get-object",
            "--bucket",
            bucket,
            "--key",
            object_key,
            "--expected-bucket-owner",
            account,
            "--if-match",
            head["ETag"],
            "--checksum-mode",
            "ENABLED",
            "--region",
            region,
            str(object_file),
        ],
        runner,
        timeout=min(10, deadline - monotonic()),
    )
    data = private_file(object_file, MAX_BUNDLE_BYTES)
    encoded_digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
    if (
        len(data) != head["ContentLength"]
        or response.get("VersionId") != head["VersionId"]
        or response.get("ServerSideEncryption") != "AES256"
        or response.get("ChecksumSHA256") != encoded_digest
        or head["ChecksumSHA256"] != encoded_digest
        or monotonic() > deadline
    ):
        refuse("Native inputs differ from the original checksummed object version")
    bundle = json.loads(data, object_pairs_hook=unique_object)
    reference = validate_bundle(bundle, environ, directory, now=wall_clock())
    private_write(directory / "login.jwt", token.encode())
    delivery = {
        "version": 1,
        "run_id": reference.run_id,
        "github_run_id": reference.github_run_id,
        "github_run_attempt": "1",
        "source_revision": reference.source_revision,
        "definition_revision": reference.definition_revision,
        "bucket": bucket,
        "key": object_key,
        "version_id": head["VersionId"],
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "head_attempts": head_attempts,
        "get_attempts": 1,
        "run_started_at": bundle["approval_receipt"]["run_started_at"],
        "expires_at": bundle["approval_receipt"]["expires_at"],
    }
    private_write(directory / "delivery.json", canonical(delivery))
    values = {
        "ADP_TOOLS_SMOKE_APPROVAL_FILE": str(directory / "approval.json"),
        "ADP_TOOLS_SMOKE_FIXTURE_FILE": str(directory / "fixture.json"),
        "ADP_TOOLS_SMOKE_NATIVE_LOGIN_FILE": str(directory / "login.jwt"),
        "ADP_TOOLS_SMOKE_DELIVERY_FILE": str(directory / "delivery.json"),
        "CI_EVIDENCE_RUNNER_ROLE_ARN": runner["AWS_ROLE_ARN"],
        "CI_EVIDENCE_RUNNER_TOKEN_FILE": runner["AWS_WEB_IDENTITY_TOKEN_FILE"],
    }
    env_path = environ.get("GITHUB_ENV")
    if not env_path or any("\n" in value or "\r" in value for value in values.values()):
        refuse()
    with open(env_path, "a") as stream:
        for name, value in values.items():
            stream.write(f"{name}={value}\n")
    return values


def publish(
    reference_file,
    fixture_file,
    receipt_file,
    *,
    account,
    region,
    environ=None,
    call=aws,
    now=None,
):
    """Owner-side once-write delivery after the supported approvals API succeeds.

    This helper does not read an owner login, call approval APIs, designate a
    fixture or install a policy. Its three inputs contain no bearer credential.
    """
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(
        r"[a-z]{2}-[a-z]+-[0-9]+", region
    ):
        refuse()
    environ = dict(os.environ if environ is None else environ)
    reference = json.loads(
        private_file(reference_file, 16384), object_pairs_hook=unique_object
    )
    fixture = json.loads(
        private_file(fixture_file, 16384), object_pairs_hook=unique_object
    )
    receipt = json.loads(
        private_file(receipt_file, 16384), object_pairs_hook=unique_object
    )
    env = binding_environment(reference)
    env.update(
        GITHUB_REPOSITORY=REPOSITORY, GITHUB_WORKFLOW=WORKFLOW, GITHUB_JOB="release"
    )
    bundle = {
        "version": 1,
        "approval": reference,
        "fixture": fixture,
        "approval_receipt": receipt,
    }
    data = canonical(bundle)
    if len(data) > MAX_BUNDLE_BYTES:
        refuse()
    with tempfile.TemporaryDirectory(prefix="native-owner-inputs-") as temporary:
        directory = Path(temporary)
        validate_bundle(bundle, env, directory, now=time.time() if now is None else now)
        identity = call(
            ["sts", "get-caller-identity", "--region", region], environ, timeout=10
        )
        if identity.get("Account") != account:
            refuse("Native input publishing account does not match the explicit target")
        path = directory / "bundle.json"
        private_write(path, data)
        checksum = base64.b64encode(hashlib.sha256(data).digest()).decode()
        bucket, object_key = f"adp-dev-ci-evidence-{account}", key(env)
        response = call(
            [
                "s3api",
                "put-object",
                "--bucket",
                bucket,
                "--key",
                object_key,
                "--body",
                str(path),
                "--expected-bucket-owner",
                account,
                "--if-none-match",
                "*",
                "--server-side-encryption",
                "AES256",
                "--checksum-algorithm",
                "SHA256",
                "--checksum-sha256",
                checksum,
                "--region",
                region,
            ],
            environ,
            timeout=10,
        )
        if (
            not response.get("VersionId")
            or response["VersionId"] == "null"
            or response.get("ChecksumSHA256") != checksum
            or response.get("ServerSideEncryption") != "AES256"
        ):
            refuse(
                "Native input publication was not confirmed; do not overwrite or retry"
            )
    return {
        "bucket": bucket,
        "key": object_key,
        "version_id": response["VersionId"],
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("download")
    owner = commands.add_parser("publish")
    owner.add_argument("--approval-reference", required=True, type=Path)
    owner.add_argument("--fixture", required=True, type=Path)
    owner.add_argument("--approval-receipt", required=True, type=Path)
    owner.add_argument("--account-id", required=True)
    owner.add_argument("--region", required=True)
    arguments = parser.parse_args()
    if arguments.mode == "download":
        report = {"version": 1, "delivery_status": "unconfirmed"}
        try:
            values = download()
            report = json.loads(
                private_file(values["ADP_TOOLS_SMOKE_DELIVERY_FILE"], 16384)
            )
            report["delivery_status"] = "confirmed"
            print("Original native approval and explicit fixture delivered")
        finally:
            evidence = Path(os.environ["ADP_TOOLS_SMOKE_EVIDENCE_DIR"])
            evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
            private_write(evidence / "input-delivery.json", canonical(report))
    else:
        print(
            json.dumps(
                publish(
                    arguments.approval_reference,
                    arguments.fixture,
                    arguments.approval_receipt,
                    account=arguments.account_id,
                    region=arguments.region,
                )
            )
        )


if __name__ == "__main__":
    try:
        main()
    except AdmissionUnavailable as error:
        raise SystemExit(str(error)) from None
    except (ValueError, TypeError, KeyError, OSError):
        raise SystemExit(
            "Native smoke input delivery failed; original approval, fixture and dedicated access login required"
        ) from None
