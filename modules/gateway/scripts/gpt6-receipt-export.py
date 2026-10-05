#!/usr/bin/env python3
"""Export pricing-only S3 evidence with operator IAM; run the existing repair.

The gateway deliberately cannot read conversation objects. The operator reads
only the failed dry-run request IDs, strips conversation contents, and supplies
a hash-pinned export through a private maintenance Job volume. No credentials or
presigned URLs are delegated. SQL decision and allocation verification remain in
the deployed pricing_correction module.
"""

import argparse
import asyncio
import concurrent.futures
import gzip
import hashlib
import io
import json
import subprocess
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import boto3
from botocore.exceptions import ClientError

FIELDS = ("request_id", "org_id", "user_id", "account_type", "root_human_id", "team_id", "department_id", "timestamp", "pricing_decision")


def export_receipts(preview_file, arguments_file, destination):
    preview = json.loads(Path(preview_file).read_text())
    args = json.loads(Path(arguments_file).read_text())
    account, org, bucket = (args[args.index(key) + 1] for key in ("--account-id", "--org-id", "--bucket"))
    identity = boto3.client("sts").get_caller_identity()
    if identity["Account"] != account or preview["account_id"] != account or preview["org_id"] != org or preview["applied"]:
        raise ValueError("operator/export scope mismatch")
    wanted = {row["request_id"] for row in preview["skipped"] if row["reason"] == "ClientError"}
    if not wanted:
        raise ValueError("no inaccessible receipts in preview")
    status = json.loads(subprocess.check_output(["adp", "status", "--json"], text=True))
    base = status["detail"]["gateway_url"].rstrip("/")
    token = subprocess.check_output(["adp", "token"], text=True).strip()
    rows = {}
    for family in ("sol", "luna"):
        offset = 0
        while True:
            query = urllib.parse.urlencode({"org_id": org, "model": "openai.gpt-6-" + family, "limit": 100, "offset": offset})
            request = urllib.request.Request(base + "/usage/logs?" + query, headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(request, timeout=45) as response:
                page = json.load(response)
            rows.update({row["request_id"]: row for row in page["items"] if row["request_id"] in wanted})
            if not page["has_more"]:
                break
            offset += 100
    if wanted - rows.keys():
        raise ValueError(f"{len(wanted - rows.keys())} preview requests missing from usage API")
    s3 = boto3.client("s3")

    def read(row):
        key = f"{org}/{row['user_id']}/{row['timestamp'][:10].replace('-', '/')}/{row['request_id']}.json"
        try:
            response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
        except ClientError as exc:
            return key, {"error": exc.response["Error"]["Code"]}
        body = json.loads(response["Body"].read())
        log = {key: body.get(key) for key in FIELDS}
        if log["org_id"] != org or log["request_id"] != row["request_id"] or log["user_id"] != row["user_id"]:
            raise ValueError("S3 receipt identity mismatch")
        return key, {"log": log, "etag": response["ETag"], "version_id": response.get("VersionId")}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        receipts = dict(pool.map(read, rows.values()))
    failures = {key: value["error"] for key, value in receipts.items() if "error" in value}
    receipts = {key: value for key, value in receipts.items() if "log" in value}
    manifest = {
        "account_id": account,
        "org_id": org,
        "bucket": bucket,
        "reader_arn": identity["Arn"],
        "exported_at": datetime.now(UTC).isoformat(),
        "receipts": receipts,
        "failures": failures,
    }
    content = gzip.compress(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(), mtime=0)
    Path(destination).write_bytes(content)
    print(json.dumps({"receipts": len(receipts), "failures": len(failures), "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}))


class ExportReader:
    def __init__(self, content, expected_sha256, *, account_id, org_id, bucket):
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ValueError("receipt export hash mismatch")
        self.manifest = json.loads(gzip.decompress(content))
        if any(self.manifest[key] != value for key, value in (("account_id", account_id), ("org_id", org_id), ("bucket", bucket))):
            raise ValueError("receipt export scope mismatch")
        self.account_id, self.bucket = account_id, bucket

    def get_object(self, *, Bucket, Key, ExpectedBucketOwner):  # noqa: N803
        if Bucket != self.bucket or ExpectedBucketOwner != self.account_id:
            raise ValueError("receipt read scope mismatch")
        if Key not in self.manifest["receipts"]:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "Receipt not in reviewed export"}}, "GetObject")
        log = self.manifest["receipts"][Key]["log"]
        if set(log) != set(FIELDS):
            raise ValueError("export contains unexpected receipt fields")
        return {"Body": io.BytesIO(json.dumps(log).encode())}


def run_repair(export_file, expected_sha256, arguments_file):
    from src.budget import reconcile_gpt6 as repair

    parser = argparse.ArgumentParser()
    for key in ("account-id", "org-id", "bucket", "actor"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--start", type=repair.instant, required=True)
    parser.add_argument("--end", type=repair.instant, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(json.loads(Path(arguments_file).read_text()))
    reader = ExportReader(Path(export_file).read_bytes(), expected_sha256, account_id=args.account_id, org_id=args.org_id, bucket=args.bucket)
    # Replace only the repair collector's data source. Database/STS clients keep
    # the Job's existing identity; finance logic and plan checks are unchanged.
    repair.boto3 = SimpleNamespace(client=lambda service: reader if service == "s3" else boto3.client(service))
    args.actor += "; receipt export " + expected_sha256
    asyncio.run(repair.reconcile(args))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("preview")
    export.add_argument("arguments")
    export.add_argument("destination")
    run = commands.add_parser("run")
    run.add_argument("export")
    run.add_argument("sha256")
    run.add_argument("arguments")
    args = parser.parse_args()
    if args.command == "export":
        export_receipts(args.preview, args.arguments, args.destination)
    else:
        run_repair(args.export, args.sha256, args.arguments)
