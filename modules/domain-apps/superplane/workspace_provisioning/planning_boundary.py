"""Bounded retained preparation evidence; never an absence claim about AWS."""

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path


ARCHIVE_ONLY_LOCK_SHA256 = (
    "bed5e8ec7d123f506ef69516786ae87829b1442215e3a542aff470eabdc35506"
)
READONLY_INIT_RECIPE_SHA256 = (
    "df93a7e7a31729675adc4f430991637f75d531734ccb0db9f6135bde60365538"
)
AWS_PACKAGE = (
    "module/.terraform/providers/registry.terraform.io/hashicorp/aws/6.65.0/linux_amd64"
)
TLS_PACKAGE = (
    "module/.terraform/providers/registry.terraform.io/hashicorp/tls/4.4.1/linux_amd64"
)
LOCKED_PACKAGES = {
    "registry.terraform.io/hashicorp/aws": "6.65.0",
    "registry.terraform.io/hashicorp/tls": "4.4.1",
}


def file_sha256(path):
    # The AWS provider exceeds the inert probe's memory limit. Hash bounded
    # files incrementally, without executing or copying retained providers.
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def snapshot(directory):
    directory = Path(directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("retained preparation root ownership differs")
    entries = {}
    total = 0
    for path in sorted(directory.rglob("*")):
        info = path.lstat()
        name = str(path.relative_to(directory))
        if len(entries) >= 2048 or info.st_uid != os.getuid():
            raise ValueError("retained preparation ownership or size differs")
        if stat.S_ISDIR(info.st_mode):
            entries[name] = {"kind": "directory"}
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            if name == AWS_PACKAGE + "/terraform-provider-aws_v6.65.0_x5":
                if info.st_size > 1 << 30:
                    raise ValueError("retained AWS package exceeds inspection bound")
            else:
                total += info.st_size
            if total > 32 << 20:
                raise ValueError("retained preparation exceeds inspection bound")
            entries[name] = {
                "kind": "file",
                "sha256": file_sha256(path),
            }
        else:
            raise ValueError("retained preparation contains an unsafe entry")
    return entries


def provider_lock_refusal(directory):
    """Observe Terraform's mandatory package check, never execute a provider.

    This is restricted to the exact historical archive-only lock. Terraform
    1.9.8 cannot accept an unpacked cache against its zh-only identities, so
    validation must refuse both packages before loading any provider schema.
    A repaired/different lock is ineligible, including an otherwise failed plan.
    The caller's inert Job has no AWS, Kubernetes, database or worker authority.
    """
    directory = Path(directory)
    lock = directory / "module/.terraform.lock.hcl"
    if not lock.is_file() or file_sha256(lock) != ARCHIVE_ONLY_LOCK_SHA256:
        return None
    if not (directory / AWS_PACKAGE / "terraform-provider-aws_v6.65.0_x5").is_file():
        return None
    environment = {
        "PATH": "/usr/local/bin:/bin",
        "HOME": "/nonexistent",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
        "CHECKPOINT_DISABLE": "1",
        "TF_IN_AUTOMATION": "1",
    }
    binary = "/usr/local/bin/terraform"
    version = subprocess.run(
        [binary, "version", "-json"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    if json.loads(version.stdout).get("terraform_version") != "1.9.8":
        raise ValueError("provider lock refusal requires the qualified Terraform")
    result = subprocess.run(
        [binary, "validate", "-json"],
        cwd=directory / "module",
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 1 or result.stderr or len(result.stdout.encode()) > 16384:
        raise ValueError("provider lock refusal was not observed")
    value = json.loads(result.stdout)
    diagnostics = value.get("diagnostics", [])
    expected = {
        f"- {name}: the cached package for {name} {version} (in .terraform/providers) "
        "does not match any of the checksums recorded in the dependency lock file"
        for name, version in LOCKED_PACKAGES.items()
    }
    if (
        value.get("valid") is not False
        or value.get("error_count") != 1
        or value.get("warning_count") != 0
        or len(diagnostics) != 1
        or diagnostics[0].get("severity") != "error"
        or diagnostics[0].get("detail") != ""
    ):
        raise ValueError("unexpected retained provider validation result")
    lines = diagnostics[0].get("summary", "").splitlines()
    if (
        len(lines) != 3
        or lines[0] != "missing or corrupted provider plugins:"
        or {line.strip() for line in lines[1:]} != expected
    ):
        raise ValueError("retained providers did not reach the exact lock refusal")
    return {
        "version": 1,
        "terraform_version": "1.9.8",
        "terraform_sha256": file_sha256(binary),
        "lock_sha256": ARCHIVE_ONLY_LOCK_SHA256,
        "rejected_packages": dict(LOCKED_PACKAGES),
        "diagnostic_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
    }


def pre_init_diagnostics(entries):
    """Preserve one bounded checked-command failure without treating it as proof.

    The exact source, empty review and absent backend/plan still establish the
    preparation boundary. Diagnostics cannot substitute for any of those facts.
    """
    names = {
        name.split("/", 1)[0] for name in entries if name.startswith("command-failure-")
    }
    if len(names) > 1 or any(
        not re.fullmatch(r"command-failure-[a-z0-9_]{8}", name) for name in names
    ):
        raise ValueError("preparation diagnostics differ from checked-command layout")
    allowed = set()
    for name in names:
        expected = {name, name + "/stdout", name + "/stderr"}
        if {
            key for key in entries if key == name or key.startswith(name + "/")
        } != expected:
            raise ValueError("preparation diagnostics contain unexpected files")
        if entries[name] != {"kind": "directory"} or any(
            set(entries[key]) != {"kind", "sha256"}
            or entries[key]["kind"] != "file"
            or not re.fullmatch(r"[a-f0-9]{64}", entries[key]["sha256"])
            for key in expected - {name}
        ):
            raise ValueError("preparation diagnostics are not bounded file evidence")
        allowed.update(expected)
    return allowed


def verify_pre_init(entries, source):
    """Exact source, one variables file, empty review, no initialized backend/plan."""
    source = Path(source)
    expected = {}
    files = [
        p
        for p in source.iterdir()
        if p.is_file()
        and (p.suffix in {".tf", ".json"} or p.name == ".terraform.lock.hcl")
    ]
    files += [
        p
        for p in (source / "scripts").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ]
    for path in files:
        expected["module/" + str(path.relative_to(source))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    if (
        not expected
        or entries.get("review") != {"kind": "directory"}
        or entries.get("workspace.tfvars.json", {}).get("kind") != "file"
    ):
        raise ValueError("initial preparation boundary unavailable")
    diagnostics = pre_init_diagnostics(entries)
    for name, entry in entries.items():
        if name in diagnostics:
            continue
        if name in {".aws", ".aws/cli", ".aws/cli/cache", ".terraform.d"} and entry == {
            "kind": "directory"
        }:
            continue
        if (
            name
            in {
                ".aws/cli/cache/session.db",
                ".terraform.d/checkpoint_cache",
                ".terraform.d/checkpoint_signature",
            }
            and entry.get("kind") == "file"
        ):
            continue
        if name in {"module", "review", "workspace.tfvars.json"}:
            continue
        if name in expected:
            if entry != {"kind": "file", "sha256": expected[name]}:
                raise ValueError("retained module source differs")
        elif (
            name == "module/scripts" or name.startswith("module/scripts/")
        ) and entry == {"kind": "directory"}:
            continue
        elif (
            name.startswith("module/scripts/")
            and "/__pycache__/" in name
            and name.endswith(".pyc")
        ):
            continue
        else:
            raise ValueError("planning/backend output prevents pre-init recovery")
    if any(
        entries.get(name) != {"kind": "file", "sha256": value}
        for name, value in expected.items()
    ):
        raise ValueError("retained module source incomplete")
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def inspection_documents(directory):
    """Bounded non-secret documents needed to attest interrupted initialization.

    Return original text so the owner can join these documents to the retained
    file hashes. Refuse credentials/overrides before putting metadata in logs.
    """
    directory = Path(directory)
    metadata = directory / "module/.terraform/terraform.tfstate"
    if not metadata.exists():
        return None
    names = ("module/.terraform/terraform.tfstate", "workspace.tfvars.json")
    documents = {}
    for name in names:
        path = directory / name
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_size > 16384
        ):
            raise ValueError("initialized preparation document is unsafe")
        documents[name] = path.read_text()
    _backend_document(json.loads(documents[names[0]]))
    # Variables are never printed: their selected identity is enough for the
    # owner, and the full variables file remains hashed by snapshot().
    variables = json.loads(documents.pop("workspace.tfvars.json"))
    documents["target"] = {
        key: variables[key]
        for key in ("account_id", "aws_region", "environment", "org_id", "workspace_id")
    }
    return documents


def _backend_document(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "terraform_version", "backend"}
        or value.get("version") != 3
        or value.get("terraform_version") != "1.9.8"
        or not isinstance(value.get("backend"), dict)
        or set(value["backend"]) != {"type", "config", "hash"}
        or value["backend"]["type"] != "s3"
        or not isinstance(value["backend"]["config"], dict)
    ):
        raise ValueError("retained backend is not initialization-only metadata")
    config = value["backend"]["config"]
    allowed = {
        "bucket",
        "region",
        "key",
        "encrypt",
        "dynamodb_table",
        "allowed_account_ids",
        "workspace_key_prefix",
    }
    if any(key not in allowed and item is not None for key, item in config.items()):
        raise ValueError("retained backend contains credentials or overrides")
    return config


def verify_incomplete_init(entries, source, documents, *, target, backend):
    """Only the maintained two-provider recipe with its AWS package absent.

    The qualified recipe runs checked init before plan and never removes an
    installed provider. This is not recovery for a completed init, a failed
    plan, an apply, or arbitrary retained Terraform state.
    """
    source = Path(source)
    if (
        not isinstance(documents, dict)
        or set(documents) != {"module/.terraform/terraform.tfstate", "target"}
        or documents["target"] != target
    ):
        raise ValueError("initialized preparation target differs")
    name = "module/.terraform/terraform.tfstate"
    text = documents[name]
    if (
        not isinstance(text, str)
        or len(text.encode()) > 16384
        or entries.get(name)
        != {"kind": "file", "sha256": hashlib.sha256(text.encode()).hexdigest()}
    ):
        raise ValueError("retained backend bytes differ from inspection")
    config = _backend_document(json.loads(text))
    key = (
        f"{target['environment']}/modules/superplane-workspaces/v2/"
        f"{target['org_id']}/{target['workspace_id']}/terraform.tfstate"
    )
    expected = {
        "bucket": backend["bucket"],
        "region": backend["region"],
        "dynamodb_table": backend["lock_table"],
        "key": key,
        "encrypt": True,
    }
    if (
        any(config.get(k) != v for k, v in expected.items())
        or config.get("allowed_account_ids") not in (None, [target["account_id"]])
        or config.get("workspace_key_prefix")
        not in (None, key.rsplit("/", 1)[0] + "/workspaces")
    ):
        raise ValueError("retained backend differs from installation/workspace")
    # Intentionally require the reviewed recipe's exact provider identities.
    # Adding providers or platforms needs a separate boundary review.
    locks = re.findall(
        r'provider "([^"]+)" \{\s*version\s*=\s*"([0-9.]+)"',
        (source / ".terraform.lock.hcl").read_text(),
    )
    if (
        dict(locks)
        != {
            "registry.terraform.io/hashicorp/aws": "6.65.0",
            "registry.terraform.io/hashicorp/tls": "4.4.1",
        }
        or len(locks) != 2
    ):
        raise ValueError("incomplete initialization recipe is unsupported")
    prefix = "module/.terraform/providers/registry.terraform.io/hashicorp/tls/4.4.1/linux_amd64"
    files = {prefix + "/LICENSE.txt", prefix + "/terraform-provider-tls_v4.4.1_x5"}
    directories = {
        "module/.terraform",
        *(
            "/".join(prefix.split("/")[:i])
            for i in range(3, len(prefix.split("/")) + 1)
        ),
    }
    projected = {}
    for name, entry in entries.items():
        if name == "module/.terraform/terraform.tfstate":
            continue
        if name in directories and entry == {"kind": "directory"}:
            continue
        if name in files and entry.get("kind") == "file":
            continue
        projected[name] = entry
    # Exact source, variables and empty review remain required. In particular,
    # every AWS-provider path, plan, state and unknown output still refuses.
    verify_pre_init(projected, source)
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify_provider_lock_refusal(
    entries, source, documents, observation, *, target, backend
):
    """A complete cache blocked by the exact historical lock, not a failed plan."""
    if (
        not isinstance(observation, dict)
        or set(observation)
        != {
            "version",
            "terraform_version",
            "terraform_sha256",
            "lock_sha256",
            "rejected_packages",
            "diagnostic_sha256",
        }
        or type(observation["version"]) is not int
        or observation["version"] != 1
        or observation["terraform_version"] != "1.9.8"
        or observation["lock_sha256"] != ARCHIVE_ONLY_LOCK_SHA256
        or observation["rejected_packages"] != LOCKED_PACKAGES
        or any(
            not isinstance(observation[key], str)
            or not re.fullmatch(r"[a-f0-9]{64}", observation[key])
            for key in ("terraform_sha256", "diagnostic_sha256")
        )
        or file_sha256(Path(source) / ".terraform.lock.hcl") != ARCHIVE_ONLY_LOCK_SHA256
        or file_sha256(Path(source) / "scripts/prepare_workspace_plan.py")
        != READONLY_INIT_RECIPE_SHA256
    ):
        raise ValueError("historical provider lock refusal evidence differs")
    return verify_complete_cache(
        entries, source, documents, target=target, backend=backend
    )


def verify_complete_cache(entries, source, documents, *, target, backend):
    """Exact initialized two-provider cache, with no review/state outputs."""
    aws_files = {
        AWS_PACKAGE + "/LICENSE.txt",
        AWS_PACKAGE + "/terraform-provider-aws_v6.65.0_x5",
    }
    tls_files = {
        TLS_PACKAGE + "/LICENSE.txt",
        TLS_PACKAGE + "/terraform-provider-tls_v4.4.1_x5",
    }
    for name in aws_files | tls_files:
        entry = entries.get(name, {})
        if set(entry) != {"kind", "sha256"} or entry["kind"] != "file":
            raise ValueError("exact retained provider package is incomplete")
        if not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]):
            raise ValueError("retained provider package identity is invalid")
    aws_directories = {
        "/".join(AWS_PACKAGE.split("/")[:i])
        for i in range(6, len(AWS_PACKAGE.split("/")) + 1)
    }
    for name in aws_directories:
        if entries.get(name) != {"kind": "directory"}:
            raise ValueError("retained provider cache layout differs")
    projected = {
        name: entry
        for name, entry in entries.items()
        if name not in aws_files | aws_directories
    }
    # Reuse all exact module, empty-review, metadata, target and backend checks.
    # No arbitrary AWS path, alternate platform or provider output is removed.
    verify_incomplete_init(projected, source, documents, target=target, backend=backend)
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def archive_preparation(root, identity, receipt):
    """Preserve exact inspected bytes under the immutable recovery receipt's key."""
    proof = json.loads(receipt["proof"])
    boundary = proof.get("preparation_boundary")
    if boundary is None:
        return
    if boundary.get("kind") not in {
        "before-terraform-init-v1",
        "interrupted-read-only-plan-v1",
        "incomplete-provider-install-v1",
        "provider-lock-refusal-v1",
        "provider-socket-refusal-v1",
    }:
        raise ValueError("unsupported retained preparation boundary")
    name = hashlib.sha256(
        json.dumps(list(identity), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    private = Path(root) / ("superplane-worker-" + str(os.getuid()))
    if private.is_symlink() or private.stat().st_uid != os.getuid():
        raise ValueError("original private state ownership differs")
    source = private / name
    if (
        source.is_symlink()
        or not source.is_dir()
        or snapshot(source) != boundary["entries"]
    ):
        raise ValueError("retained preparation changed after owner inspection")
    suffix = hashlib.sha256(receipt["reauthorization_id"].encode()).hexdigest()
    destination = private / (name + ".recovered-" + suffix)
    if destination.exists() or destination.is_symlink():
        raise ValueError("retained preparation already archived")
    # Kubelet fsGroup staging may have added group access. Tighten only this
    # worker-owned private directory; never chown or relax the shared volume.
    private.chmod(0o700)
    source.chmod(0o700)
    source.rename(destination)
