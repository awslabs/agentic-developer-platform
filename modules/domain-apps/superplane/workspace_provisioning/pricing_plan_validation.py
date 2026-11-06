"""Revalidate a preserved initial plan after the exact Oregon pricing refusal.

This module performs offline checks only. It never initializes, plans, applies,
or writes the original directory. Owner provenance, expiry, backend absence and
fresh recovery approval remain separate requirements.
"""

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from workspace_provisioning.saved_plan_validation import (
    SAVED_PLAN_FILES,
    initial_plan_only,
)
from workspace_provisioning.terraform import source_digest, workspace_source

ORIGINAL_FILES = frozenset(
    {"workspace.tfplan", "workspace-plan.json", "workspace-inventory.json"}
)
DERIVED_FILES = SAVED_PLAN_FILES - ORIGINAL_FILES
OLD_GUARD_SHA256 = "146faecae44c0ec761603ec9d2a95bd7d4e603ac88799e1ea3a541b0a3a35e31"
PRICING_RUNTIME = {
    "process.py": "b16587aef5a5eae589af5fc71e5f44a6ab3f14875f4e295a1bba8a951163de4f",
    "terraform.py": "6653304db73ff705aa4e67e6d1d274ed6b65c397bd2ea11db9663fff3467e9b0",
    "runtime.py": "108bb731b088a160b352b10943252c8e0b804093affc93ae9b4a5ab44cdc1120",
}
REPLACEMENTS = frozenset({"scripts/check_workspace_plan.py", "regional-pricing.json"})
REFUSAL = (
    "The plan's change inventory or bounded estimate could not be produced: "
    "the bounded estimate cannot be produced: bounded pricing is supported only "
    "for us-east-1; us-west-2 needs verified per-service regional rates. "
    "An EC2 multiplier does not bound EKS, NAT, KMS and gp3 prices.. "
    "Design item 4 requires both as evidence, so a plan whose cost cannot be "
    "bounded is not applied."
)


def require(value, message="saved pricing proposal refused"):
    if not value:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def module_files(module):
    module = Path(module)
    paths = [
        p
        for p in module.iterdir()
        if p.is_file()
        and (p.suffix in {".tf", ".json"} or p.name == ".terraform.lock.hcl")
    ]
    paths += [
        p
        for p in (module / "scripts").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ]
    require(all(not p.is_symlink() for p in paths))
    return {str(p.relative_to(module)): sha(p) for p in paths}


def replacement_bytes(source=None):
    source = Path(source) if source is not None else workspace_source()
    return {name: (source / name).read_bytes() for name in REPLACEMENTS}


def verify_transition(original, corrected):
    """Only the reviewed pricing guard and catalog may differ; Terraform cannot."""
    before, after = module_files(original), module_files(corrected)
    require(before.get("scripts/check_workspace_plan.py") == OLD_GUARD_SHA256)
    require("regional-pricing.json" not in before)
    require(REPLACEMENTS <= set(after))
    require(
        {k: v for k, v in before.items() if k not in REPLACEMENTS}
        == {k: v for k, v in after.items() if k not in REPLACEMENTS},
        "pricing completion cannot change Terraform or another checker",
    )
    return {name: after[name] for name in REPLACEMENTS}


def diagnostic_paths(entries):
    directories = [
        name
        for name, entry in entries.items()
        if re.fullmatch(r"command-failure-[a-z0-9_]{8}", name)
        and entry == {"kind": "directory"}
    ]
    require(len(directories) == 1)
    prefix = directories[0]
    names = {prefix, prefix + "/stdout", prefix + "/stderr"}
    require({n for n in entries if n.startswith("command-failure-")} == names)
    require(
        all(
            entries.get(prefix + "/" + n, {}).get("kind") == "file"
            for n in ["stdout", "stderr"]
        )
    )
    return names


def verify_pricing_entries(entries, source, documents, *, target, backend):
    from workspace_provisioning.planning_boundary import verify_complete_cache
    from workspace_provisioning.saved_plan_validation import OPAQUE_CREDENTIAL_FILES

    require(target["aws_region"] == "us-west-2")
    require(sha(Path(source) / "scripts/check_workspace_plan.py") == OLD_GUARD_SHA256)
    files = {"review/" + name for name in ORIGINAL_FILES}
    require({name for name in entries if name.startswith("review/")} == files)
    for name in files:
        require(
            set(entries[name]) == {"kind", "sha256"} and entries[name]["kind"] == "file"
        )
        require(re.fullmatch(r"[a-f0-9]{64}", entries[name]["sha256"]))
    ignored = files | diagnostic_paths(entries)
    projected = {}
    for name, entry in entries.items():
        if name in ignored:
            continue
        if name in OPAQUE_CREDENTIAL_FILES:
            require(set(entry) == {"kind", "size", "mtime_ns", "inode"})
            require(entry["kind"] == "opaque-credential-cache")
            require(
                all(
                    type(entry[k]) is int and entry[k] >= 0
                    for k in ["size", "mtime_ns", "inode"]
                )
            )
            continue
        projected[name] = entry
    verify_complete_cache(projected, source, documents, target=target, backend=backend)
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def reproduce_pricing(
    directory, *, replacements=None, terraform_binary="/usr/local/bin/terraform"
):
    """Render the original binary plan, reproduce its refusal, then price it offline."""
    directory = Path(directory)
    module, review = directory / "module", directory / "review"
    require({p.name for p in review.iterdir()} == ORIGINAL_FILES)
    require(sha(module / "scripts/check_workspace_plan.py") == OLD_GUARD_SHA256)
    original = {name: sha(review / name) for name in ORIGINAL_FILES}
    variables = json.loads((directory / "workspace.tfvars.json").read_text())
    target = {
        key: variables[key]
        for key in [
            "account_id",
            "aws_region",
            "environment",
            "workspace_name",
            "org_id",
            "workspace_id",
        ]
    }
    require(target["aws_region"] == "us-west-2")
    plan = json.loads((review / "workspace-plan.json").read_text())
    initial_plan_only(plan)
    require(
        all(
            plan.get("variables", {}).get(k, {}).get("value") == v
            for k, v in variables.items()
        )
    )
    diagnostics = list(directory.glob("command-failure-*/stderr"))
    require(
        len(diagnostics) == 1
        and diagnostics[0].read_text() == "::error::" + REFUSAL + "\n"
    )
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
    replacements = replacement_bytes() if replacements is None else replacements
    require(set(replacements) == REPLACEMENTS)
    with tempfile.TemporaryDirectory(prefix="sp-price-", dir="/tmp") as temporary:
        scratch = Path(temporary)
        environment["TMPDIR"] = temporary
        corrected = scratch / "module"
        corrected.mkdir()
        for name in module_files(module):
            destination = corrected / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(module / name, destination)
        for name, content in replacements.items():
            (corrected / name).write_bytes(content)
        change = verify_transition(module, corrected)
        # Only the already verified read-only provider cache is shared. No init,
        # provider operation or backend API is invoked by Terraform show.
        (corrected / ".terraform").symlink_to(
            module / ".terraform", target_is_directory=True
        )

        def check(source, output):
            output.mkdir()
            flags = [
                sys.executable,
                "-B",
                str(source / "scripts/check_workspace_plan.py"),
                "--module-dir",
                str(source),
                "--terraform-binary",
                terraform_binary,
                "--plan-file",
                str(review / "workspace.tfplan"),
                "--plan-json",
                str(review / "workspace-plan.json"),
            ]
            for flag, name in [
                ("inventory", "workspace-inventory.json"),
                ("estimate", "workspace-estimate.json"),
                ("emit-authorization", "workspace-authorization.proposed.json"),
            ]:
                flags += ["--" + flag, str(output / name)]
            for key, value in target.items():
                flags += ["--" + key.replace("_", "-"), value]
            return subprocess.run(
                flags, env=environment, capture_output=True, timeout=120
            )

        denied = check(module, scratch / "old")
        require(
            denied.returncode != 0
            and denied.stderr.decode() == "::error::" + REFUSAL + "\n"
        )
        checked = check(corrected, scratch / "new")
        require(
            checked.returncode == 0,
            "corrected offline pricing guard refused the saved plan",
        )
        require(
            sha(scratch / "new/workspace-inventory.json")
            == original["workspace-inventory.json"]
        )
        derived = {
            name: (scratch / "new" / name).read_bytes() for name in DERIVED_FILES
        }
        observation = {
            "version": 1,
            "terraform_version": "1.9.8",
            "target": target,
            "files": {
                **original,
                **{
                    name: hashlib.sha256(raw).hexdigest()
                    for name, raw in derived.items()
                },
            },
            "initial_create_only": True,
            "offline_guard_passed": True,
            "pricing_revalidation": {
                "version": 1,
                "original_files": original,
                "replacements": change,
                "original_module_sha256": source_digest(module),
                "corrected_module_sha256": source_digest(corrected),
                "refusal_sha256": sha(diagnostics[0]),
            },
        }
    require(original == {name: sha(review / name) for name in ORIGINAL_FILES})
    return observation, derived
