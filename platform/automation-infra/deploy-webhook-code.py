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
        assert not self.poisoned, "Uncertain journal write; reconcile without replay"
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
        assert result.get("VersionId") not in (None, "", "null"), (
            "Versioned journal required"
        )
        assert result.get("ETag"), "Journal commit acknowledgement required"
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
    assert re.fullmatch(r"[0-9]{12}", account)
    assert re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", manifest["region"])
    prefix = manifest["archive_prefix"]
    assert re.fullmatch(r"lambda-artifacts/[a-z0-9/_-]+", prefix)
    targets = manifest["targets"]
    assert 0 < len(targets) <= 20, "Unbound profile or excessive targets"
    assert len({t["function_arn"] for t in targets}) == len(targets)
    assert len({t["artifact"] for t in targets}) == len(targets)
    for target in targets:
        assert re.fullmatch(r"[a-z0-9_-]+\.zip", target["artifact"])
        assert re.fullmatch(
            rf"arn:aws:lambda:{re.escape(manifest['region'])}:{account}:function:[A-Za-z0-9_-]+",
            target["function_arn"],
        )
        assert re.fullmatch(
            rf"arn:aws:iam::{account}:role/(adp-|bedrockgw-)[A-Za-z0-9_-]+",
            target["execution_role"],
        )
        assert not re.search(r"trusted-|operator", target["execution_role"])


def archive(path):
    assert not path.is_symlink() and path.is_file()
    assert 0 < path.stat().st_size <= 250 * 1024 * 1024
    body = path.read_bytes()
    with zipfile.ZipFile(path) as package:
        for entry in package.infolist():
            parts = Path(entry.filename).parts
            assert not entry.filename.startswith("/") and ".." not in parts
            assert (entry.external_attr >> 16) & 0o170000 != 0o120000, (
                "Archive symlink refused"
            )
    return body, base64.b64encode(hashlib.sha256(body).digest()).decode()


def deploy(
    manifest, artifacts, source_sha, run_id, s3, lambdas, receipt_path, sleep=time.sleep
):
    validate_manifest(manifest)
    assert re.fullmatch(r"[a-f0-9]{40}", source_sha)
    assert re.fullmatch(r"[0-9]+", run_id)
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
        assert version not in (None, "", "null"), "Versioned code archive required"
        operation["source"] = {"bucket": bucket, "key": key, "version": version}
        journal.save()
        current = lambdas.get_function_configuration(
            FunctionName=target["function_arn"]
        )
        assert current["Role"] == target["execution_role"], (
            "Execution role drift before update"
        )
        assert (
            current.get("RevisionId")
            and current.get("LastUpdateStatus") == "Successful"
        )
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
        assert (
            updated["Role"] == target["execution_role"]
            and updated["CodeSha256"] == digest
        )
        assert updated.get("RevisionId"), "Missing update revision"
        operation["update_response"] = {
            k: updated[k] for k in ("Role", "RevisionId", "CodeSha256")
        }
        journal.save()
        for attempt in range(60):
            final = lambdas.get_function_configuration(
                FunctionName=target["function_arn"]
            )
            assert (
                final["Role"] == target["execution_role"]
                and final["CodeSha256"] == digest
            ), "Code or role drift"
            assert final["RevisionId"] == updated["RevisionId"], (
                "Concurrent revision change"
            )
            assert final["LastUpdateStatus"] in ("InProgress", "Successful"), (
                "Lambda update failed"
            )
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
    assert (
        os.environ["GITHUB_REF"] == "refs/heads/main"
        and os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
    )
    assert os.environ["GITHUB_RUN_ATTEMPT"] == "1", "No workflow replay"
    manifest = json.loads(Path(args.manifest).read_text())
    validate_manifest(manifest)
    session = boto3.Session(region_name=manifest["region"])
    config = Config(
        connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1}
    )
    identity = session.client("sts", config=config).get_caller_identity()
    assert identity["Account"] == manifest["account_id"]
    assert re.fullmatch(
        rf"arn:aws:sts::{manifest['account_id']}:assumed-role/adp-[a-z0-9-]+-trusted-webhook-code/webhook-code-[0-9]+-1",
        identity["Arn"],
    )
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
