"""Validate an interrupted initial proposal without planning or cloud mutation.

The installation owner may embed this module in an inert read-only probe. These
facts do not authorize execution, publish a lifecycle artifact, or prove AWS
resource absence. Completion needs a separate current grant and final preflights.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


SAVED_PLAN_FILES = frozenset(
    {
        "workspace.tfplan",
        "workspace-plan.json",
        "workspace-inventory.json",
        "workspace-estimate.json",
        "workspace-authorization.proposed.json",
    }
)
SAVED_PLAN_RUNTIME = {
    "process.py": "b16587aef5a5eae589af5fc71e5f44a6ab3f14875f4e295a1bba8a951163de4f",
    "terraform.py": "6653304db73ff705aa4e67e6d1d274ed6b65c397bd2ea11db9663fff3467e9b0",
    "runtime.py": "ea5dc191bb963a62f52d097c2cc7a9165a47334233f5fd1b797907b3af6b16a1",
}
# Reviewed completion runtime with bounded read-only authority retries.
COMPLETION_RETRY_RUNTIME = {
    **SAVED_PLAN_RUNTIME,
    "runtime.py": "41ee8f9ca6d7f1222300c0a1535d44683a81f918285f02f9a1247e33026598ce",
}
SAVED_PLAN_RECIPE = "df93a7e7a31729675adc4f430991637f75d531734ccb0db9f6135bde60365538"
OPAQUE_CREDENTIAL_FILES = frozenset(
    ".aws/cli/cache/" + name
    for name in ("session.db", "session.db-shm", "session.db-wal")
)


def saved_plan_snapshot(directory):
    """Retain credential-cache metadata without opening or copying credentials."""
    directory = Path(directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("saved preparation ownership differs")
    entries, total = {}, 0
    for path in sorted(directory.rglob("*")):
        info = path.lstat()
        name = str(path.relative_to(directory))
        if len(entries) >= 2048 or info.st_uid != os.getuid():
            raise ValueError("saved preparation size or ownership differs")
        if stat.S_ISDIR(info.st_mode):
            entries[name] = {"kind": "directory"}
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            if name in OPAQUE_CREDENTIAL_FILES:
                entries[name] = {
                    "kind": "opaque-credential-cache",
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                    "inode": info.st_ino,
                }
                continue
            if name.endswith("/terraform-provider-aws_v6.65.0_x5"):
                if info.st_size > 1 << 30:
                    raise ValueError("saved provider exceeds inspection bound")
            else:
                total += info.st_size
            if total > 32 << 20:
                raise ValueError("saved preparation exceeds inspection bound")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            entries[name] = {"kind": "file", "sha256": digest}
        else:
            raise ValueError("saved preparation contains an unsafe entry")
    return entries


def initial_plan_only(plan):
    """A complete create-only initial plan, including an empty managed prior state."""
    if (
        plan.get("terraform_version") != "1.9.8"
        or plan.get("applyable") is not True
        or plan.get("complete") is not True
        or plan.get("errored") is not False
        or not plan.get("resource_changes")
    ):
        raise ValueError("saved plan is not a complete initial proposal")
    for change in plan["resource_changes"]:
        expected = ["create"] if change.get("mode") == "managed" else ["read"]
        if (
            change.get("mode") not in {"managed", "data"}
            or change["change"]["actions"] != expected
            or change["change"].get("before") is not None
            or change.get("previous_address")
            or change["change"].get("importing")
        ):
            raise ValueError("saved proposal includes a non-initial change")
    pending = [plan.get("prior_state", {}).get("values", {}).get("root_module", {})]
    count = 0
    while pending:
        module = pending.pop()
        count += 1
        if count > 2048 or any(
            resource.get("mode") != "data" for resource in module.get("resources", [])
        ):
            raise ValueError("saved proposal contains prior managed state")
        pending.extend(module.get("child_modules", []))


def verify_saved_entries(entries, source, documents, *, target, backend):
    from workspace_provisioning.planning_boundary import verify_complete_cache

    files = {"review/" + name for name in SAVED_PLAN_FILES}
    if any(
        set(entries.get(name, {})) != {"kind", "sha256"}
        or entries[name]["kind"] != "file"
        or not re.fullmatch(r"[a-f0-9]{64}", entries[name]["sha256"])
        for name in files
    ):
        raise ValueError("saved proposal files are incomplete")
    projected = {}
    for name, value in entries.items():
        if name in files:
            continue
        if name in OPAQUE_CREDENTIAL_FILES:
            if (
                set(value) != {"kind", "size", "mtime_ns", "inode"}
                or value["kind"] != "opaque-credential-cache"
                or any(
                    type(value[k]) is not int or value[k] < 0
                    for k in ("size", "mtime_ns", "inode")
                )
            ):
                raise ValueError("credential cache metadata differs")
            continue
        projected[name] = value
    # This rejects a completed backend receipt, apply outputs, extra review files,
    # changed source, alternate providers, local state and unknown paths.
    verify_complete_cache(projected, source, documents, target=target, backend=backend)
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def inspect_saved_plan(directory, *, terraform_binary="/usr/local/bin/terraform"):
    """Render the original artifact and independently reproduce its three reports.

    This runs only the retained, owner-verified guard and Terraform show. The
    caller must compare the module against qualified source before invoking it.
    AWS credentials and backend access are unavailable in the owner's probe.
    """
    directory = Path(directory)
    module, review = directory / "module", directory / "review"
    variables = json.loads((directory / "workspace.tfvars.json").read_text())
    target = {
        key: variables[key]
        for key in (
            "account_id",
            "aws_region",
            "environment",
            "workspace_name",
            "org_id",
            "workspace_id",
        )
    }
    plan = json.loads((review / "workspace-plan.json").read_text())
    initial_plan_only(plan)
    if any(
        plan.get("variables", {}).get(key, {}).get("value") != value
        for key, value in variables.items()
    ):
        raise ValueError("saved plan variables differ from retained input")
    environment = {
        "PATH": "/usr/local/bin:/bin",
        "HOME": "/nonexistent",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
        "CHECKPOINT_DISABLE": "1",
        "TF_IN_AUTOMATION": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    import sys

    with tempfile.TemporaryDirectory(prefix="sp-plan-", dir="/tmp") as temporary:
        output = Path(temporary)
        environment["TMPDIR"] = temporary
        flags = [
            sys.executable,
            "-B",
            str(module / "scripts/check_workspace_plan.py"),
            "--module-dir",
            str(module),
            "--terraform-binary",
            terraform_binary,
            "--plan-file",
            str(review / "workspace.tfplan"),
            "--plan-json",
            str(review / "workspace-plan.json"),
        ]
        for flag, name in (
            ("inventory", "workspace-inventory.json"),
            ("estimate", "workspace-estimate.json"),
            ("emit-authorization", "workspace-authorization.proposed.json"),
        ):
            flags.extend(["--" + flag, str(output / name)])
        for key, value in target.items():
            flags.extend(["--" + key.replace("_", "-"), value])
        result = subprocess.run(
            flags, env=environment, capture_output=True, timeout=120
        )
        if result.returncode:
            raise ValueError("retained saved plan failed the maintained offline guard")
        for name in SAVED_PLAN_FILES - {"workspace.tfplan", "workspace-plan.json"}:
            if (output / name).read_bytes() != (review / name).read_bytes():
                raise ValueError("retained proposal reports differ from saved artifact")
    return {
        "version": 1,
        "terraform_version": "1.9.8",
        "target": target,
        "files": {
            name: hashlib.sha256((review / name).read_bytes()).hexdigest()
            for name in sorted(SAVED_PLAN_FILES)
        },
        "initial_create_only": True,
        "offline_guard_passed": True,
    }
