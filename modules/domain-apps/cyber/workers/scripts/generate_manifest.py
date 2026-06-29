#!/usr/bin/env python3
"""Generate worker tool manifest by introspecting the installed environment.

Runs at Docker build time. Outputs JSON to stdout describing all available
Python packages, system binaries, YARA rules, and runtime constraints.

IMPORTANT: This script MUST NOT hardcode any version strings. All versions
are introspected from the actual installed environment.
"""

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Python packages to introspect — these are the ones the agent may use in Mode B scripts.
# The list comes from requirements.txt; versions are always introspected, never hardcoded.
PYTHON_PACKAGES = [
    "lief",
    "pefile",
    "yara-python",
    "capstone",
    "oletools",
    "magika",
    "iocextract",
    "ppdeep",
    "pyelftools",
    "macholib",
    "signify",
    "boto3",
    "requests",
    "pydantic",
    "python-magic",
]

# System binaries the worker image installs via apt-get.
SYSTEM_BINARIES = [
    "strings",
    "yara",
    "binwalk",
    "file",
    "osslsigncode",
    "upx",
]

YARA_RULES_DIR = "/opt/yara-rules"


def get_python_packages() -> dict[str, str]:
    """Get installed versions of expected Python packages."""
    packages = {}
    for pkg in PYTHON_PACKAGES:
        try:
            version = importlib.metadata.version(pkg)
            packages[pkg] = version
        except importlib.metadata.PackageNotFoundError:
            # Package not installed — still include it so drift is visible.
            packages[pkg] = "__NOT_INSTALLED__"
    return packages


def get_binary_version(binary: str) -> str | None:
    """Attempt to get version string from a binary's --version output."""
    path = shutil.which(binary)
    if path is None:
        return None

    # Try common version flags
    for flag in ["--version", "-V", "-version"]:
        try:
            result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
                [path, flag],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = (result.stdout or result.stderr or "").strip()
            if output:
                # Return first non-empty line (usually contains version)
                return output.split("\n")[0]
        except (subprocess.TimeoutExpired, OSError):
            continue
    return "unknown"


def get_system_binaries() -> dict[str, dict[str, str]]:
    """Get paths and versions for expected system binaries."""
    binaries = {}
    for binary in SYSTEM_BINARIES:
        path = shutil.which(binary)
        if path is None:
            binaries[binary] = {
                "path": "__NOT_INSTALLED__",
                "version": "__NOT_INSTALLED__",
            }
            continue
        version = get_binary_version(binary) or "unknown"
        binaries[binary] = {"path": path, "version": version}
    return binaries


def get_yara_rule_count() -> int:
    """Count YARA rule files in the rules directory."""
    rules_dir = Path(YARA_RULES_DIR)
    if not rules_dir.exists():
        return 0
    count = 0
    for ext in ("*.yar", "*.yara"):
        count += len(list(rules_dir.rglob(ext)))
    return count


def generate_manifest() -> dict:
    """Generate the complete worker manifest."""
    image_tag = os.environ.get("IMAGE_TAG", "dev")

    manifest = {
        "image_tag": image_tag,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": {
            "version": platform.python_version(),
            "interpreter": sys.executable,
        },
        "python_packages": get_python_packages(),
        "system_binaries": get_system_binaries(),
        "resources": {
            "yara_rules_dir": YARA_RULES_DIR,
            "yara_rule_count": get_yara_rule_count(),
        },
        "runtime_constraints": {
            "timeout_seconds": 300,
            "network": "none — only AWS VPC endpoints reachable (SQS, DDB, S3, Secrets Manager)",
            "user": "uid=1001 gid=1001 non-root",
            "writable_dirs": ["/tmp"],
            "ephemeral_storage_mb": 1024,
            "cpu_limit": "2",
            "memory_limit_mb": 2048,
        },
        "script_contract": {
            "interpreter": "python3",
            "invocation": "python3 <script> <sample_path>",
            "sample_path_arg": "sys.argv[1]",
            "stdout_format": "single JSON line matching envelope 'findings' schema",
            "exit_code": "0 on success, nonzero on failure",
            "stderr_handling": "captured and surfaced in envelope 'notes' field on nonzero exit",
            "max_script_lines_guideline": 200,
        },
    }

    # Validate: fail if any expected package is missing
    missing_packages = [
        pkg
        for pkg, ver in manifest["python_packages"].items()
        if ver == "__NOT_INSTALLED__"
    ]
    missing_binaries = [
        b
        for b, info in manifest["system_binaries"].items()
        if info["path"] == "__NOT_INSTALLED__"
    ]

    if missing_packages or missing_binaries:
        errors = []
        if missing_packages:
            errors.append(f"Missing Python packages: {', '.join(missing_packages)}")
        if missing_binaries:
            errors.append(f"Missing system binaries: {', '.join(missing_binaries)}")
        print("\n".join(errors), file=sys.stderr)
        sys.exit(1)

    return manifest


if __name__ == "__main__":
    manifest = generate_manifest()
    print(json.dumps(manifest, indent=2))
