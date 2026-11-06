"""Fail closed when protected shared-tools bindings do not match the selected state."""

import os
import re
import sys
from pathlib import Path


def values(path):
    entries = {}
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line in {"encrypt = true", "use_lockfile = true"}:
            field = line.split(" = ", 1)[0]
            if field in entries:
                raise ValueError("Private backend contains duplicate assignments")
            entries[field] = "true"
            continue
        match = re.fullmatch(r'(\w+)\s*=\s*"([^"\r\n]*)"', line)
        if match is None or match.group(1) in entries:
            raise ValueError("Private configuration contains duplicate or unsupported assignments")
        entries[match.group(1)] = match.group(2)
    return entries


def target_variables(path, *, environment=None):
    entries = {}
    names = ("aws_account_id", "aws_region")
    if environment is not None:
        if environment not in {"dev", "prod"}:
            raise ValueError("Select the protected tools environment")
        names += ("tools_reconciler_key_arn", "tools_reconciler_role_arn")
    for name in names:
        matches = re.findall(rf'(?m)^\s*{name}\s*=\s*"([^"\r\n]+)"\s*$', Path(path).read_text())
        if len(matches) != 1:
            raise ValueError("Protected variables require one confirmed account and region")
        entries[name] = matches[0]
    return entries


def validate(backend, tfvars, *, bucket, state_key, account, region, lock_table="", environment=None):
    backend_values = values(backend)
    variables = target_variables(tfvars, environment=environment)
    if not re.fullmatch(r"[a-z0-9.-]{3,63}", bucket) or not re.fullmatch(r"[a-zA-Z0-9_./-]+", state_key):
        raise ValueError("Coordinator must bind the approved existing state identity")
    if ".." in state_key or "/tools/agentcore/" not in f"/{state_key}" or not state_key.endswith(".tfstate"):
        raise ValueError("Coordinator must bind the approved existing state identity")
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region):
        raise ValueError("Coordinator must bind the confirmed target")
    if any(backend_values.get(key) != value for key, value in
           (("bucket", bucket), ("key", state_key), ("region", region))):
        raise ValueError("Protected backend does not match the approved existing state")
    if set(backend_values) - {"bucket", "key", "region", "dynamodb_table", "encrypt", "use_lockfile", "kms_key_id"}:
        raise ValueError("Protected backend contains unsupported configuration")
    use_lockfile = backend_values.get("use_lockfile") == "true"
    dynamodb_table = backend_values.get("dynamodb_table", "")
    if use_lockfile == bool(dynamodb_table):
        raise ValueError("Coordinator must select exactly one existing backend lock mechanism")
    if dynamodb_table and (not re.fullmatch(r"[A-Za-z0-9_.-]+", dynamodb_table) or
                           dynamodb_table != lock_table):
        raise ValueError("Protected DynamoDB lock table differs from approved binding")
    if variables.get("aws_account_id") != account or variables.get("aws_region") != region:
        raise ValueError("Protected Terraform variables do not match the confirmed target")
    if environment is not None:
        key = variables["tools_reconciler_key_arn"]
        role = variables["tools_reconciler_role_arn"]
        if (not re.fullmatch(rf"arn:aws:kms:{re.escape(region)}:{account}:key/[A-Za-z0-9-]+", key) or
                role != f"arn:aws:iam::{account}:role/adp-{environment}-role-tools-reconciler"):
            raise ValueError("Protected cleanup bindings do not match the confirmed environment")


if __name__ == "__main__":
    try:
        validate(sys.argv[1], sys.argv[2], bucket=os.environ["STATE_BUCKET"],
                 state_key=os.environ["STATE_KEY"], account=os.environ["EXPECTED_AWS_ACCOUNT_ID"],
                 region=os.environ["AWS_REGION"], lock_table=os.environ.get("STATE_LOCK_TABLE", ""),
                 environment=os.environ["TARGET_ENVIRONMENT"])
    except (IndexError, KeyError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
