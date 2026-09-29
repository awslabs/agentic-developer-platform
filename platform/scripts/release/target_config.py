#!/usr/bin/env python3
"""Load private, account-specific tfvars for a release upgrade."""

import argparse
import hashlib
import json
from pathlib import Path
import re

from common import ACCOUNTS, REGION, identity
import storage


MODULES = {"gateway": "ADP_GATEWAY_UPDATE_TFVARS", "webhook-ingress": "ADP_WEBHOOK_UPDATE_TFVARS"}


def download(s3, environment, directory):
    account = ACCOUNTS[environment]
    bucket = f"adp-terraform-state-{account}"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    paths = {}
    for module, variable in MODULES.items():
        key = f"adp-release-config/{environment}/{module}.tfvars.json"
        response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
        body = response["Body"].read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError(f"Release target config is too large: {module}")
        data = json.loads(body)
        foreign = set(re.findall(rb"(?<![0-9])[0-9]{12}(?![0-9])", body)) - {account.encode()}
        if (not isinstance(data, dict) or not data or foreign
                or data.get("environment", "dev") != "dev"
                or data.get("aws_region", REGION) != REGION):
            raise ValueError(f"Release target config is invalid for {environment}: {module}")
        path = directory / f"{module}.tfvars.json"
        path.write_bytes(body)
        path.chmod(0o600)
        paths[variable] = path
        print(f"Verified {module} target config SHA256: {hashlib.sha256(body).hexdigest()}")
    return paths


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=ACCOUNTS)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args()
    identity(ACCOUNTS[args.environment])
    values = download(storage.client(), args.environment, args.directory.resolve())
    if args.github_env:
        with args.github_env.open("a") as env:
            for name, path in values.items():
                env.write(f"{name}={path}\n")
    else:
        for name, path in values.items():
            print(f"{name}={path}")
