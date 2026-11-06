#!/usr/bin/env python3
"""Connect an owner's Dev Box using short-lived Session Manager credentials."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import adp_common as common

BOX_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
INSTANCE_ID = re.compile(r"i-[0-9a-f]{8,17}\Z")
REGION = re.compile(r"[a-z]{2}(?:-gov)?-[a-z]+-\d\Z")


def key_path(box_id):
    if not BOX_ID.fullmatch(box_id):
        raise common.CliError("Specify an owned Dev Box ID.", "invalid_argument", 2)
    return Path.home() / ".ssh" / f"adp-devbox-{box_id}"


def ensure_key(box_id):
    path = key_path(box_id)
    common.private_directory(path.parent)
    if not path.exists() and not path.is_symlink() and not Path(str(path) + ".pub").exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True, capture_output=True)
    public = Path(str(path) + ".pub")
    for candidate, limit in ((path, 0o077), (public, 0o022)):
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & limit:
            raise common.CliError("Dev Box SSH key permissions are unsafe.", "unsafe_file", 2)
    key = public.read_text().strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?", key):
        raise common.CliError("Dev Box public key is invalid.", "invalid_argument", 2)
    return path, key


def credentials_environment(grant):
    credentials = grant.get("credentials")
    if (
        not isinstance(credentials, dict)
        or any(not isinstance(credentials.get(key), str) or not credentials[key] for key in ("access_key_id", "secret_access_key", "session_token"))
        or not INSTANCE_ID.fullmatch(grant.get("instance_id", ""))
        or not REGION.fullmatch(grant.get("region", ""))
        or grant.get("os_user") != "ec2-user"
    ):
        raise common.CliError("Dev Box SSH grant is invalid.", "invalid_response", 5)
    try:
        expires = datetime.fromisoformat(grant["expires_at"])
    except (ValueError, TypeError, KeyError) as error:
        raise common.CliError("Dev Box SSH grant expiry is invalid.", "invalid_response", 5) from error
    if expires.tzinfo is None or not datetime.now(UTC) < expires.astimezone(UTC) <= datetime.now(UTC) + timedelta(minutes=15):
        raise common.CliError("Dev Box SSH grant has expired or exceeds fifteen minutes.", "invalid_response", 5)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
    environment.update(
        {
            "AWS_ACCESS_KEY_ID": credentials["access_key_id"],
            "AWS_SECRET_ACCESS_KEY": credentials["secret_access_key"],
            "AWS_SESSION_TOKEN": credentials["session_token"],
            "AWS_DEFAULT_REGION": grant["region"],
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )
    return environment


def push_key(grant, public_key, environment):
    result = subprocess.run(
        [
            "aws",
            "ec2-instance-connect",
            "send-ssh-public-key",
            "--instance-id",
            grant["instance_id"],
            "--instance-os-user",
            grant["os_user"],
            "--ssh-public-key",
            public_key,
            "--region",
            grant["region"],
            "--output",
            "json",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise common.CliError("Dev Box could not accept the short-lived SSH key.", "key_delivery_failed", 5)
    try:
        accepted = json.loads(result.stdout)
    except ValueError as error:
        raise common.CliError("Dev Box key delivery was not confirmed.", "invalid_response", 5) from error
    if accepted.get("Success") is not True:
        raise common.CliError("Dev Box key delivery was refused.", "key_delivery_failed", 5)


def prepare(box_id):
    key_path(box_id)
    expected_gateway = os.environ.get("ADP_DEVBOX_GATEWAY_URL")
    if expected_gateway and common.gateway_url() != expected_gateway:
        raise common.CliError("Dev Box alias targets a different gateway; rerun ssh-config.", "deployment_mismatch", 4)
    common.ensure_can_mutate("devbox.ssh.access")
    grant = common.api("POST", f"/cloud-desktops/{box_id}/ssh-access")
    if grant.get("desktop_id") != box_id:
        raise common.CliError("Dev Box grant names a different box.", "invalid_response", 5)
    environment = credentials_environment(grant)
    key, public_key = ensure_key(box_id)
    push_key(grant, public_key, environment)
    return key, grant, environment


def proxy(box_id):
    _, grant, environment = prepare(box_id)
    os.execvpe(
        "aws",
        [
            "aws",
            "ssm",
            "start-session",
            "--target",
            grant["instance_id"],
            "--document-name",
            "AWS-StartSSHSession",
            "--parameters",
            "portNumber=22",
            "--region",
            grant["region"],
        ],
        environment,
    )


def resolve_box(selector):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", selector):
        raise common.CliError("Specify a Dev Box name or ID without shell punctuation.", "invalid_argument", 2)
    response = common.api("GET", "/cloud-desktops")
    if not isinstance(response, dict) or not isinstance(response.get("items"), list):
        raise common.CliError("Dev Box list is invalid.", "invalid_response", 5)
    matches = [
        item
        for item in response["items"]
        if isinstance(item, dict) and item.get("status") != "released" and (item.get("desktop_id") == selector or item.get("name") == selector)
    ]
    if (
        len(matches) != 1
        or not isinstance(matches[0].get("name"), str)
        or not isinstance(matches[0].get("desktop_id"), str)
        or not BOX_ID.fullmatch(matches[0]["desktop_id"])
    ):
        raise common.CliError("Dev Box not found or name is ambiguous for this owner.", "desktop_not_found", 4)
    return matches[0]


def host_alias(box):
    name = re.sub(r"[^a-z0-9-]", "-", box["name"].lower()).strip("-") or box["desktop_id"]
    resolved = common.deployment()
    if resolved and not resolved.legacy:
        prefix = re.sub(r"[^a-z0-9-]", "-", resolved.name.lower()).strip("-")
        name = f"{prefix}-{name}"
    return "adp-devbox-" + name[:100]


def proxy_command(box_id):
    executable = Path(__file__).resolve().with_name("adp")
    if not executable.is_file():
        raise common.CliError("ADP CLI is missing; reinstall before writing SSH config.", "cli_missing", 2)
    resolved = common.deployment()
    values = {"ADP_DEVBOX_GATEWAY_URL": common.gateway_url()}
    if resolved:
        values.update(ADP_DEPLOYMENT_ID=resolved.id, ADP_DEPLOYMENT_URL=resolved.gateway_url)
    for key in ("ADP_TENANT_ID", "ADP_TENANT_SUB", "ADP_TENANT_SOURCE", "ADP_TENANT_MODE", "ADP_TENANT_MEMBERSHIP"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    if any(any(character in str(value) for character in "\r\n\0%") for value in (*values.values(), str(executable))):
        raise common.CliError("Dev Box SSH configuration contains an unsafe path or selection.", "unsafe_file", 2)
    command = ["env"]
    for key in (
        "ADP_DEPLOYMENT",
        "ADP_DEPLOYMENT_ID",
        "ADP_DEPLOYMENT_NAME",
        "ADP_DEPLOYMENT_SOURCE",
        "ADP_DEPLOYMENT_URL",
        "ADP_TENANT",
        "ADP_TENANT_ID",
        "ADP_TENANT_SUB",
        "ADP_TENANT_SOURCE",
        "ADP_TENANT_MODE",
        "ADP_TENANT_MEMBERSHIP",
        "ADP_LEGACY_CONFIG_DIR",
        "BG_CONFIG_DIR",
        "BG_AWS_PROFILE",
        "BG_AWS_RETIRED_PROFILES",
        "ADP_STATE_DIR",
        "ADP_RUNTIME_DIR",
        "ADP_LOG_DIR",
    ):
        command.extend(("-u", key))
    command.extend(f"{key}={value}" for key, value in values.items())
    command.extend((str(executable), "devbox", "proxy", box_id))
    return shlex.join(command)


def host_entry(box):
    alias = host_alias(box)
    box_id = box["desktop_id"]
    identity = str(key_path(box_id))
    if any(character in identity for character in "\r\n\0%"):
        raise common.CliError("Dev Box SSH key path is unsafe for an SSH configuration.", "unsafe_file", 2)
    digest = hashlib.sha256(f"{box_id}:{common.gateway_url()}:{os.environ.get('ADP_TENANT_ID', '')}".encode()).hexdigest()[:20]
    marker = f"# ADP Dev Box {digest}"
    entry = "\n".join(
        (
            f"{marker} begin",
            f"Host {alias}",
            f"    HostName {box_id}",
            "    User ec2-user",
            f"    IdentityFile {json.dumps(identity)}",
            "    IdentitiesOnly yes",
            f"    HostKeyAlias {alias}",
            f"    ProxyCommand {proxy_command(box_id)}",
            f"{marker} end",
            "",
        )
    )
    return alias, marker, entry


def write_host(box):
    alias, marker, entry = host_entry(box)
    directory = common.private_directory(Path.home() / ".ssh")
    config = directory / "config"
    lock_file = directory / ".adp-devbox-config.lock"
    descriptor = os.open(lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise common.CliError("Dev Box SSH config lock is unsafe.", "unsafe_file", 2)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if config.is_symlink():
            raise common.CliError("SSH config must not be a symlink.", "unsafe_file", 2)
        if config.exists():
            info = config.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise common.CliError("SSH config ownership or permissions are unsafe.", "unsafe_file", 2)
            previous = config.read_text()
        else:
            previous = ""
        begin, end = marker + " begin", marker + " end"
        # A box released and provisioned again under the same name leaves this CLI's own entry for the old
        # box behind (dev 2026-10-26: `ssh-config` then refused the alias as "already exists"). Entries this
        # CLI wrote are recognised by their markers and replaced; any other entry with the alias is the
        # user's own and is never touched.
        base = previous
        for stale in list(re.finditer(r"# ADP Dev Box [0-9a-f]{20} begin\n.*?# ADP Dev Box [0-9a-f]{20} end\n?", previous, flags=re.S)):
            block = stale.group(0)
            if not block.startswith(begin) and re.search(rf"(?im)^\s*Host\s+{re.escape(alias)}(?:\s|$)", block):
                base = base.replace(block, "", 1)
        remaining = base
        if begin in base:
            if base.count(begin) != 1 or base.count(end) != 1:
                raise common.CliError("Dev Box SSH entry is malformed; inspect config.", "unsafe_file", 2)
            start = base.index(begin)
            stop = base.index(end, start) + len(end)
            remaining = base[:start] + base[stop:]
        if re.search(rf"(?im)^\s*Host\s+{re.escape(alias)}(?:\s|$)", remaining):
            raise common.CliError("SSH host alias already exists; inspect config.", "unsafe_file", 2)
        if begin in base:
            updated = base[:start] + entry.rstrip("\n") + base[stop:]
        else:
            first_host = re.search(r"(?im)^(?:Host|Match)\s", base)
            boundary = first_host.start() if first_host else len(base)
            updated = base[:boundary].rstrip("\n") + ("\n\n" if boundary else "") + entry + base[boundary:]
        if updated != previous:
            descriptor, temporary = tempfile.mkstemp(prefix=".adp-devbox-", dir=directory)
            try:
                with os.fdopen(descriptor, "w") as output:
                    output.write(updated)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, config)
            finally:
                Path(temporary).unlink(missing_ok=True)
    return alias


def main(argv=None):
    parser = argparse.ArgumentParser(prog="adp devbox")
    parser.add_argument("action", choices=("proxy", "ssh", "ssh-config"))
    parser.add_argument("box")
    args = parser.parse_args(argv)
    try:
        if args.action == "proxy":
            proxy(args.box)
        else:
            box = resolve_box(args.box)
            ensure_key(box["desktop_id"])
            alias = write_host(box)
            if args.action == "ssh":
                os.execvp("ssh", ["ssh", alias])
            print(f"SSH host configured: {alias}")
    except (common.CliError, OSError, subprocess.SubprocessError) as error:
        print(f"Dev Box SSH: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
