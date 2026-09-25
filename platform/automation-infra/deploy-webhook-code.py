#!/usr/bin/env python3
"""Single-attempt code deployment from a committed manifest and clean build."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
import zipfile


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class Receipt:
    """Remote conditional journal. An uncertain write poisons this process."""

    def __init__(self, s3, bucket, key, local):
        self.s3, self.bucket, self.key = s3, bucket, key
        self.local = Path(local)
        self.etag = None
        self.poisoned = False
        self.data = {}

    def save(self):
        if not (not self.poisoned):
            raise AssertionError("Uncertain journal write; reconcile without replay")
        body = encode(self.data)
        self.local.parent.mkdir(parents=True, exist_ok=True)
        with self.local.open("wb") as stream:
            stream.write(encode({"receipt": self.data, "commit_acknowledged": False}))
            stream.flush()
            os.fsync(stream.fileno())
        self.poisoned = True
        result = self.s3.put_object(
            Bucket=self.bucket,
            Key=self.key,
            Body=body,
            ContentType="application/json",
            **({"IfMatch": self.etag} if self.etag else {"IfNoneMatch": "*"}),
        )
        if not (result.get("VersionId") not in (None, "", "null")):
            raise AssertionError("Versioned journal required")
        if not (result.get("ETag")):
            raise AssertionError("Journal commit acknowledgement required")
        self.etag = result["ETag"]
        self.poisoned = False
        with self.local.open("wb") as stream:
            stream.write(
                encode(
                    {
                        "receipt": self.data,
                        "commit_acknowledged": True,
                        "etag": self.etag,
                        "version": result["VersionId"],
                    }
                )
            )
            stream.flush()
            os.fsync(stream.fileno())


def validate_manifest(manifest):
    account = manifest["account_id"]
    if not (re.fullmatch(r"[0-9]{12}", account)):
        raise AssertionError()
    if not (re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", manifest["region"])):
        raise AssertionError()
    prefix = manifest["archive_prefix"]
    if not (re.fullmatch(r"lambda-artifacts/[a-z0-9/_-]+", prefix)):
        raise AssertionError()
    targets = manifest["targets"]
    if not (0 < len(targets) <= 20):
        raise AssertionError("Unbound profile or excessive targets")
    if not (len({t["function_arn"] for t in targets}) == len(targets)):
        raise AssertionError()
    if not (len({t["artifact"] for t in targets}) == len(targets)):
        raise AssertionError()
    for target in targets:
        if not (re.fullmatch(r"[a-z0-9_-]+\.zip", target["artifact"])):
            raise AssertionError()
        if not (
            re.fullmatch(
                rf"arn:aws:lambda:{re.escape(manifest['region'])}:{account}:function:[A-Za-z0-9_-]+",
                target["function_arn"],
            )
        ):
            raise AssertionError()
        if not (
            re.fullmatch(
                rf"arn:aws:iam::{account}:role/(adp-|bedrockgw-)[A-Za-z0-9_-]+",
                target["execution_role"],
            )
        ):
            raise AssertionError()
        if not (not re.search(r"trusted-|operator", target["execution_role"])):
            raise AssertionError()


def archive(path):
    if not (not path.is_symlink() and path.is_file()):
        raise AssertionError()
    if not (0 < path.stat().st_size <= 250 * 1024 * 1024):
        raise AssertionError()
    body = path.read_bytes()
    with zipfile.ZipFile(path) as package:
        for entry in package.infolist():
            parts = Path(entry.filename).parts
            if not (not entry.filename.startswith("/") and ".." not in parts):
                raise AssertionError()
            if not ((entry.external_attr >> 16) & 0o170000 != 0o120000):
                raise AssertionError("Archive symlink refused")
    return body, base64.b64encode(hashlib.sha256(body).digest()).decode()


def deploy(
    manifest, artifacts, source_sha, run_id, s3, lambdas, receipt_path, sleep=time.sleep
):
    validate_manifest(manifest)
    if not (re.fullmatch(r"[a-f0-9]{40}", source_sha)):
        raise AssertionError()
    if not (re.fullmatch(r"[0-9]+", run_id)):
        raise AssertionError()
    bucket = "adp-terraform-state-" + manifest["account_id"]
    prefix = manifest["archive_prefix"]
    # A source commit has one deployment journal; a new workflow run cannot replay it.
    journal = Receipt(s3, bucket, f"{prefix}/receipts/{source_sha}.json", receipt_path)
    prepared = []
    for target in manifest["targets"]:
        body, digest = archive(Path(artifacts) / target["artifact"])
        prepared.append((target, body, digest))
    journal.data = {
        "source_sha": source_sha,
        "run_id": run_id,
        "manifest_sha256": hashlib.sha256(encode(manifest)).hexdigest(),
        "complete": False,
        "operations": [],
    }
    journal.save()
    for target, body, digest in prepared:
        operation = {"target": target, "code_sha256": digest, "upload_intent": True}
        journal.data["operations"].append(operation)
        journal.save()
        key = f"{prefix}/sources/{source_sha}/{hashlib.sha256(body).hexdigest()}-{target['artifact']}"
        upload = s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            IfNoneMatch="*",
            Metadata={"commit": source_sha},
        )
        version = upload.get("VersionId")
        if not (version not in (None, "", "null")):
            raise AssertionError("Versioned code archive required")
        operation["source"] = {"bucket": bucket, "key": key, "version": version}
        journal.save()
        current = lambdas.get_function_configuration(
            FunctionName=target["function_arn"]
        )
        if not (current["Role"] == target["execution_role"]):
            raise AssertionError("Execution role drift before update")
        if not (
            current.get("RevisionId")
            and current.get("LastUpdateStatus") == "Successful"
        ):
            raise AssertionError()
        operation["before"] = {
            k: current[k] for k in ("Role", "RevisionId", "CodeSha256")
        }
        operation["update_intent"] = True
        journal.save()
        # No Publish, alias change, role/configuration update, retry or fallback.
        updated = lambdas.update_function_code(
            FunctionName=target["function_arn"],
            S3Bucket=bucket,
            S3Key=key,
            S3ObjectVersion=version,
            RevisionId=current["RevisionId"],
            Publish=False,
        )
        if not (
            updated["Role"] == target["execution_role"]
            and updated["CodeSha256"] == digest
        ):
            raise AssertionError()
        if not (updated.get("RevisionId")):
            raise AssertionError("Missing update revision")
        operation["update_response"] = {
            k: updated[k] for k in ("Role", "RevisionId", "CodeSha256")
        }
        journal.save()
        for attempt in range(60):
            final = lambdas.get_function_configuration(
                FunctionName=target["function_arn"]
            )
            if not (
                final["Role"] == target["execution_role"]
                and final["CodeSha256"] == digest
            ):
                raise AssertionError("Code or role drift")
            if not (final["RevisionId"] == updated["RevisionId"]):
                raise AssertionError("Concurrent revision change")
            if not (final["LastUpdateStatus"] in ("InProgress", "Successful")):
                raise AssertionError("Lambda update failed")
            if final["LastUpdateStatus"] == "Successful":
                break
            sleep(5)
        else:
            raise AssertionError(
                "Update timeout; reconcile saved intent without replay"
            )
        operation["verified"] = True
        journal.save()
    journal.data["complete"] = True
    journal.save()
    return journal.data


def main():
    import boto3
    from botocore.config import Config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args()
    if not (
        os.environ["GITHUB_REF"] == "refs/heads/main"
        and os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
    ):
        raise AssertionError()
    if not (os.environ["GITHUB_RUN_ATTEMPT"] == "1"):
        raise AssertionError("No workflow replay")
    manifest = json.loads(Path(args.manifest).read_text())
    validate_manifest(manifest)
    session = boto3.Session(region_name=manifest["region"])
    config = Config(
        connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1}
    )
    identity = session.client("sts", config=config).get_caller_identity()
    if not (identity["Account"] == manifest["account_id"]):
        raise AssertionError()
    if not (
        re.fullmatch(
            rf"arn:aws:sts::{manifest['account_id']}:assumed-role/adp-[a-z0-9-]+-trusted-webhook-code/webhook-code-[0-9]+-1",
            identity["Arn"],
        )
    ):
        raise AssertionError()
    deploy(
        manifest,
        args.artifacts,
        os.environ["GITHUB_SHA"],
        os.environ["GITHUB_RUN_ID"],
        session.client("s3", config=config),
        session.client("lambda", config=config),
        args.receipt,
    )


if __name__ == "__main__":
    main()
