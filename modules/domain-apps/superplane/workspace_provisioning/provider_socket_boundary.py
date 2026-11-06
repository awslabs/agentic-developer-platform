"""The historical long-TMPDIR failure before either provider can serve a schema.

This module is also embedded by the owner into a credential-free read-only probe.
It does not execute a Terraform plan or read AWS state.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


SOCKET_RUNTIME_HASHES = {
    "process.py": "e62f479c4a2a44dfc57e5fdc5b584658024b767a026a6f3e1a2c64fa2b8f95a7",
    "terraform.py": "6653304db73ff705aa4e67e6d1d274ed6b65c397bd2ea11db9663fff3467e9b0",
}
SOCKET_LOCK_SHA256 = "1da168e54694ac10fa3734ad12916a84d311a6579fb7cf2700ef489f942801d6"
SOCKET_RECIPE_SHA256 = (
    "df93a7e7a31729675adc4f430991637f75d531734ccb0db9f6135bde60365538"
)


def socket_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def socket_runtime(root):
    return {name: socket_hash(root / name) for name in SOCKET_RUNTIME_HASHES}


def provider_socket_refusal(directory):
    directory = Path(directory)
    package = Path(importlib.util.find_spec("workspace_provisioning").origin).parent
    lock = directory / "module/.terraform.lock.hcl"
    if (
        not lock.is_file()
        or socket_hash(lock) != SOCKET_LOCK_SHA256
        or socket_runtime(package) != SOCKET_RUNTIME_HASHES
    ):
        return None
    if (
        os.getuid() != 65531
        or directory.parent != Path("/run/state/superplane-worker-65531")
        or not re.fullmatch(r"[a-f0-9]{64}", directory.name)
        or socket_hash(directory / "module/scripts/prepare_workspace_plan.py")
        != SOCKET_RECIPE_SHA256
    ):
        raise ValueError("historical provider socket source/layout differs")
    environment = {
        "PATH": "/usr/local/bin:/bin",
        "HOME": "/nonexistent",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
        "CHECKPOINT_DISABLE": "1",
        "TF_IN_AUTOMATION": "1",
        "TF_LOG": "ERROR",
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
        raise ValueError("provider socket refusal requires qualified Terraform")
    # The original directory stays read-only. Reproduce only its socket address
    # length in fresh private tmpfs; no provider receives any cloud credentials.
    length = len(str(directory / ".provider-tmp-12345678"))
    with tempfile.TemporaryDirectory(prefix="sp-probe-", dir="/tmp") as scratch:
        temporary = Path(scratch) / ("p" * (length - len(scratch) - 1))
        temporary.mkdir(mode=0o700)
        environment["TMPDIR"] = str(temporary)
        result = subprocess.run(
            [binary, "validate", "-json"],
            cwd=directory / "module",
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        validate_socket_diagnostic(result, temporary)
    with Path(binary).open("rb") as stream:
        binary_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "version": 1,
        "terraform_version": "1.9.8",
        "terraform_sha256": binary_sha,
        "lock_sha256": SOCKET_LOCK_SHA256,
        "runtime_sources": dict(SOCKET_RUNTIME_HASHES),
        "operation_directory_name": directory.name,
        "temporary_path_length": length,
        "diagnostic_sha256": hashlib.sha256(
            (result.stdout + result.stderr).encode()
        ).hexdigest(),
    }


def validate_socket_diagnostic(result, temporary):
    if max(len(result.stdout.encode()), len(result.stderr.encode())) > 16384:
        raise ValueError("provider socket diagnostic exceeded its bound")
    value = json.loads(result.stdout)
    diagnostics = value.get("diagnostics", [])
    errors = re.findall(
        r"provider\.terraform-provider-(aws_v6\.65\.0_x5|tls_v4\.4\.1_x5): "
        r'plugin init error: error="listen unix '
        + re.escape(str(temporary))
        + r'/plugin[0-9]+: bind: invalid argument"',
        result.stderr,
    )
    if (
        result.returncode != 1
        or value.get("valid") is not False
        or value.get("error_count") != 1
        or value.get("warning_count") != 0
        or len(diagnostics) != 1
        or diagnostics[0].get("severity") != "error"
        or diagnostics[0].get("summary") != "Failed to load plugin schemas"
        or len(result.stderr.splitlines()) != 2
        or sorted(errors) != ["aws_v6.65.0_x5", "tls_v4.4.1_x5"]
    ):
        raise ValueError("exact two-provider socket refusal was not observed")


def verify_provider_socket_refusal(
    entries,
    source,
    documents,
    observation,
    *,
    target,
    backend,
    runtime_source,
    operation_directory_name,
):
    from .planning_boundary import verify_complete_cache

    expected_length = len(
        "/run/state/superplane-worker-65531/" + "a" * 64 + "/.provider-tmp-12345678"
    )
    if (
        not isinstance(observation, dict)
        or set(observation)
        != {
            "version",
            "terraform_version",
            "terraform_sha256",
            "lock_sha256",
            "runtime_sources",
            "operation_directory_name",
            "temporary_path_length",
            "diagnostic_sha256",
        }
        or type(observation["version"]) is not int
        or observation["version"] != 1
        or observation["terraform_version"] != "1.9.8"
        or observation["lock_sha256"] != SOCKET_LOCK_SHA256
        or observation["runtime_sources"] != SOCKET_RUNTIME_HASHES
        or socket_runtime(Path(runtime_source)) != SOCKET_RUNTIME_HASHES
        or observation["operation_directory_name"] != operation_directory_name
        or not re.fullmatch(r"[a-f0-9]{64}", operation_directory_name)
        or type(observation["temporary_path_length"]) is not int
        or observation["temporary_path_length"] != expected_length
        or socket_hash(Path(source) / ".terraform.lock.hcl") != SOCKET_LOCK_SHA256
        or socket_hash(Path(source) / "scripts/prepare_workspace_plan.py")
        != SOCKET_RECIPE_SHA256
        or any(
            not isinstance(observation[key], str)
            or not re.fullmatch(r"[a-f0-9]{64}", observation[key])
            for key in ("terraform_sha256", "diagnostic_sha256")
        )
    ):
        raise ValueError("historical provider socket refusal evidence differs")
    return verify_complete_cache(
        entries, source, documents, target=target, backend=backend
    )
