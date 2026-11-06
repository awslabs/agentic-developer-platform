#!/usr/bin/env python3
"""Load public native-credential configuration without hiding read failures."""

import argparse
import json
import re
import subprocess


def render(value, account, region):
    if not isinstance(value, dict) or set(value) != {"enabled", "issuer", "approval_origin", "key_id", "key_arn"}:
        raise ValueError("Native credential configuration has an unsupported shape")
    if type(value["enabled"]) is not bool:
        raise ValueError("Native credential enablement must be a boolean")
    if not value["enabled"]:
        return "false disabled disabled disabled disabled"
    for key in ("issuer", "approval_origin"):
        if not isinstance(value[key], str) or not re.fullmatch(r"https://[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?", value[key]):
            raise ValueError("Native credential origins must be exact HTTPS origins")
    if not isinstance(value["key_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value["key_id"]):
        raise ValueError("Native credential key identifier is invalid")
    if not isinstance(value["key_arn"], str) or not re.fullmatch(rf"arn:aws:kms:{re.escape(region)}:{account}:key/[a-f0-9-]{{36}}", value["key_arn"]):
        raise ValueError("Native credential signer must be an immutable key in the selected account and region")
    return " ".join(["true", value["issuer"], value["approval_origin"], value["key_id"], value["key_arn"]])


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Native credential configuration contains duplicate keys")
        result[key] = value
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("environment")
    parser.add_argument("region")
    parser.add_argument("account")
    args = parser.parse_args()
    if not (
        re.fullmatch(r"[a-z][a-z0-9-]{0,31}", args.environment)
        and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", args.region)
        and re.fullmatch(r"[0-9]{12}", args.account)
    ):
        raise SystemExit("Invalid native credential deployment target")
    result = subprocess.run(
        [
            "aws",
            "ssm",
            "get-parameter",
            "--name",
            f"/adp/{args.environment}/gateway/native-credentials-config",
            "--region",
            args.region,
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        if "(ParameterNotFound)" in result.stderr:
            print("false disabled disabled disabled disabled")
            return
        raise SystemExit("Cannot read native credential configuration; deployment refused")
    try:
        response = json.loads(result.stdout)
        raw = response["Parameter"]["Value"]
        if not isinstance(raw, str) or len(raw) > 4096:
            raise ValueError("Native credential configuration is oversized")
        print(render(json.loads(raw, object_pairs_hook=unique), args.account, args.region))
    except (ValueError, TypeError, KeyError):
        raise SystemExit("Invalid native credential configuration; deployment refused") from None


if __name__ == "__main__":
    main()
