#!/usr/bin/python3 -I
"""Dev Box transport for adp-cred and the AWS SDK credential_process protocol.

Installed as host/adp-cred in the Crew bundle. Only configuration is written to
disk; STS credentials pass from the authenticated loopback adapter to the SDK.
The EKS adp-cred transport and explicit assume command are unchanged.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONFIG = "/run/adp-crew-aws/config"
PROGRAM = "/opt/adp/crew/current/host/adp-cred"
SHELL = "/run/adp-dev-box-shell"


class Unavailable(Exception):
    pass


def adapter(environment, directory=SHELL):
    sources = [environment]
    try:
        folder, path = Path(directory), Path(directory) / "model.env"
        info, file_info = folder.lstat(), path.lstat()
        if (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
                and stat.S_ISREG(file_info.st_mode) and file_info.st_uid == os.getuid() and stat.S_IMODE(file_info.st_mode) == 0o600):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as stream:
                sources.append(dict(line.rstrip("\n").split("=", 1) for line in stream if "=" in line))
    except (OSError, ValueError):
        pass
    for source in sources:
        base = source.get("ADP_CREW_TOOLS_URL") or source.get("ANTHROPIC_BASE_URL", "")
        key = source.get("ADP_CREW_TOOLS_TOKEN") or source.get("ANTHROPIC_API_KEY", "")
        if re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", base) and re.fullmatch(r"[0-9a-f]{64}", key):
            return base, key
    raise Unavailable("The Dev Box adapter is not available for this user")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(body, environment, opener=None):
    base, key = adapter(environment)
    call = urllib.request.Request(base + "/aws/credentials", data=json.dumps(body).encode(), method="POST",
                                  headers={"x-api-key": key, "content-type": "application/json"})
    try:
        with (opener or urllib.request.build_opener(NoRedirect()).open)(call, timeout=40) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError()
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        # Error bodies can contain provider values. Do not read or print them.
        message = {403: "AWS connection access is disabled or the owner is no longer authorized",
                   404: "Connect an AWS account in ADP Settings",
                   409: "Select an AWS connection with its profile or adp-cred --label; verify the connection if needed"}
        raise Unavailable(message.get(error.code, "ADP could not issue AWS credentials")) from None
    except (OSError, ValueError, urllib.error.URLError):
        raise Unavailable("The ADP AWS credential provider is unavailable") from None


def connections(environment, opener=None):
    payload = request({"operation": "list"}, environment, opener)
    rows = payload.get("connections") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 1000:
        raise Unavailable("Invalid AWS connection list")
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row["id"])
                or not isinstance(row.get("label"), str) or len(row["label"]) > 255
                or not re.fullmatch(r"[0-9]{12}", row.get("account_id", ""))):
            raise Unavailable("Invalid AWS connection list")
    return rows


def profile(row):
    label = re.sub(r"[^a-zA-Z0-9_-]", "-", row["label"])[:64] or "account"
    return "adp-aws-" + label + "-" + hashlib.sha256(row["id"].encode()).hexdigest()[:8]


def sync(environment, config=CONFIG, opener=None):
    parser = configparser.RawConfigParser()
    parser["default"] = {"credential_process": PROGRAM + " credential-process", "region": environment.get("AWS_REGION", "us-east-1")}
    # Always leave a refusing default provider even before connection discovery succeeds.
    failure = None
    try:
        rows = connections(environment, opener)
    except Unavailable as error:
        rows, failure = [], error
    for row in rows:
        parser["profile " + profile(row)] = {
            "credential_process": PROGRAM + " credential-process --connection-id " + row["id"],
            "region": environment.get("AWS_REGION", "us-east-1"),
        }
    directory = Path(config).parent
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise Unavailable("Untrusted AWS configuration directory")
    fd, path = tempfile.mkstemp(prefix=".config-", dir=directory)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o644)  # Nonsecret profile metadata, shared with the owner shell.
            parser.write(output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(path, config)
    finally:
        if os.path.exists(path):
            os.unlink(path)
    if failure:
        raise failure


def credentials(environment, connection_id=None, label=None, opener=None):
    body = {"operation": "assume"}
    if connection_id:
        body["connection_id"] = connection_id
    elif label or environment.get("ADP_AWS_LABEL"):
        body["label"] = label or environment["ADP_AWS_LABEL"]
    result = request(body, environment, opener)
    fields = {"Version", "AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration"}
    try:
        if not isinstance(result, dict) or set(result) != fields or result["Version"] != 1:
            raise ValueError()
        if any(not isinstance(result[k], str) or not 0 < len(result[k]) <= 32768 for k in fields - {"Version"}):
            raise ValueError()
        expiry = datetime.fromisoformat(result["Expiration"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise Unavailable("ADP returned an invalid or expired AWS session") from None
    return result


def main(argv=None, environment=None):
    environment = dict(os.environ if environment is None else environment)
    parser = argparse.ArgumentParser(prog="adp-cred")
    parser.add_argument("operation", choices=("credential-process", "list", "assume", "sync"))
    parser.add_argument("--service", choices=("aws",), default="aws")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--label")
    selection.add_argument("--connection-id")
    parser.add_argument("--purpose")
    parser.add_argument("--exec", dest="command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.operation == "sync":
            sync(environment)
        elif args.operation == "credential-process":
            print(json.dumps(credentials(environment, args.connection_id, args.label)))
        elif args.operation == "list":
            print(json.dumps([{**row, "profile": profile(row)} for row in connections(environment)]))
        elif args.command is not None:
            if not args.command:
                raise Unavailable("--exec requires a command")
            rows = [row for row in connections(environment) if (args.label is None or row["label"] == args.label)
                    and (args.connection_id is None or row["id"] == args.connection_id)]
            if len(rows) != 1:
                raise Unavailable("Select one connected AWS account with --label")
            # The subprocess gets a refreshable, ID-bound profile, not expiring environment keys.
            # Use a private temporary config so a newly connected account works before the next sync.
            with tempfile.TemporaryDirectory(prefix="adp-aws-") as directory:
                config = str(Path(directory) / "config")
                sync(environment, config)
                for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
                            "AWS_DEFAULT_PROFILE", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "ADP_AWS_LABEL"):
                    environment.pop(key, None)
                environment.update(AWS_CONFIG_FILE=config, AWS_PROFILE=profile(rows[0]), AWS_EC2_METADATA_DISABLED="true",
                                   AWS_SHARED_CREDENTIALS_FILE="/dev/null")
                return subprocess.call(args.command, env=environment)
        else:
            rows = [row for row in connections(environment) if (args.label is None or row["label"] == args.label)
                    and (args.connection_id is None or row["id"] == args.connection_id)]
            if len(rows) != 1:
                raise Unavailable("Select one connected AWS account with --label")
            print(profile(rows[0]))
        return 0
    except (Unavailable, OSError) as error:
        print(str(error) if isinstance(error, Unavailable) else "AWS credential command failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
