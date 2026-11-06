#!/usr/bin/env python3
"""Read protected receiver configuration without replacing unreadable settings."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


def configuration(environment, region, account, *, run=subprocess.run):
    if (
        environment not in {"dev", "prod"}
        or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
        or not re.fullmatch(r"[0-9]{12}", account)
    ):
        raise ValueError("invalid selected receiver target")
    prefix = f"/adp/{environment}/insights/"
    names = [
        prefix + "l5-consumer-verification-keys",
        prefix + "l5-authority-gateway-url",
    ]
    values = []
    for name in names:
        completed = run(
            [
                "aws",
                "ssm",
                "get-parameter",
                "--name",
                name,
                "--region",
                region,
                "--output",
                "json",
                "--cli-connect-timeout",
                "3",
                "--cli-read-timeout",
                "10",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        if len(completed.stdout) > 65536:
            raise ValueError("receiver configuration exceeds its bound")
        response = json.loads(completed.stdout)
        item = response.get("Parameter", {}) if isinstance(response, dict) else {}
        if not isinstance(item, dict):
            raise ValueError("invalid receiver parameter response")
        if (
            item.get("Name") != name
            or item.get("ARN") != f"arn:aws:ssm:{region}:{account}:parameter{name}"
            or item.get("Type") != "String"
            or type(item.get("Version")) is not int
            or item["Version"] < 1
            or not isinstance(item.get("Value"), str)
        ):
            raise ValueError(
                "both exact public receiver parameters must be readable in the selected account"
            )
        values.append(item)
    selected = {item["Name"]: item["Value"] for item in values}
    keys = json.loads(selected[names[0]])
    if (
        not isinstance(keys, dict)
        or not 1 <= len(keys) <= 16
        or any(
            not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key)
            for key in keys
        )
    ):
        raise ValueError("receiver needs a nonempty bounded public key map")
    for pem in keys.values():
        if not isinstance(pem, str):
            raise ValueError(
                "receiver verification keys must be public Ed25519 SPKI PEM"
            )
        match = re.fullmatch(
            r"-----BEGIN PUBLIC KEY-----\s+([A-Za-z0-9+/=\r\n]+)\s+-----END PUBLIC KEY-----\s*",
            pem,
        )
        try:
            encoded = (
                base64.b64decode("".join(match.group(1).split()), validate=True)
                if match
                else b""
            )
        except ValueError:
            encoded = b""
        # RFC 8410 SubjectPublicKeyInfo: Ed25519 OID, absent parameters, 32-byte key.
        if len(encoded) != 44 or not encoded.startswith(
            bytes.fromhex("302a300506032b6570032100")
        ):
            raise ValueError(
                "receiver verification keys must be public Ed25519 SPKI PEM"
            )
    origin = selected[names[1]]
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in origin
    ):
        raise ValueError(
            "receiver authority cannot contain whitespace or control characters"
        )
    url = urlsplit(origin)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path
        or url.query
        or url.fragment
        or len(origin) > 512
        or not re.fullmatch(r"[a-z0-9.-]+", url.hostname)
        or url.port is not None
        and not 1 <= url.port <= 65535
        or url.scheme == "http"
        and origin
        not in {
            "http://bedrockgateway.adp-gateway.svc.cluster.local",
            "http://bedrockgateway.adp-gateway.svc.cluster.local:80",
        }
    ):
        raise ValueError(
            "receiver authority must be the private Gateway or a selected HTTPS origin"
        )
    return {"l5_consumer_verification_keys": keys, "l5_authority_gateway_url": origin}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("environment", "region", "account", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        value = configuration(args.environment, args.region, args.account)
        output = Path(args.output)
        if (
            not output.is_absolute()
            or output.exists()
            or output.is_symlink()
            or output.parent.is_symlink()
            or not output.parent.is_dir()
            or output.parent.stat().st_mode & 0o077
        ):
            raise ValueError("receiver variables require a new private output")
        descriptor = os.open(
            output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(
            "Receiver configuration unavailable: verify the two protected Insights SSM parameters and read authority. No defaults were applied.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
