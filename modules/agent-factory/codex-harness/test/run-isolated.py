#!/usr/bin/env python3
"""Run local/CI tests without inheriting ADP login, config or credential stores.

Usage: python3 test/run-isolated.py -- <executable> <args...>
This isolates configuration, not arbitrary filesystem/network access. Tests must
still use fixtures for external operations. No parent settings are read or edited.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def isolated_environment(root: Path) -> dict[str, str]:
    # An allowlist avoids inheriting newly introduced credential/deployment flags.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
    # setup-python distributions need their matching libpython. Dropping its
    # loader path can silently load Ubuntu's older ABI-compatible SONAME and
    # crash in asyncio. Derive this from the running interpreter, never inherit
    # arbitrary LD_LIBRARY_PATH/LD_PRELOAD entries from the caller.
    runtime_lib = Path(sys.base_prefix) / "lib"
    if (runtime_lib / f"libpython{sys.version_info.major}.{sys.version_info.minor}.so.1.0").is_file():
        env["LD_LIBRARY_PATH"] = str(runtime_lib)
    directories = (
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "BG_CONFIG_DIR",
        "ADP_HOME",
        "ADP_LEGACY_CONFIG_DIR",
        "ADP_STATE_DIR",
        "ADP_RUNTIME_DIR",
        "ADP_LOG_DIR",
        "GH_CONFIG_DIR",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "AZURE_CONFIG_DIR",
        "CLOUDSDK_CONFIG",
        "DOCKER_CONFIG",
        "NPM_CONFIG_CACHE",
    )
    for key in directories:
        directory = root / key.lower()
        directory.mkdir(mode=0o700)
        env[key] = str(directory)
    for key in (
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "ADP_TOKEN_FILE",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "KUBECONFIG",
        "GIT_CONFIG_GLOBAL",
        "NPM_CONFIG_USERCONFIG",
        "NPM_CONFIG_GLOBALCONFIG",
    ):
        file = root / key.lower()
        file.touch(mode=0o600)
        env[key] = str(file)
    env.update(
        LANG="C.UTF-8",
        LC_ALL="C.UTF-8",
        TZ="UTC",
        NO_COLOR="1",
        AWS_EC2_METADATA_DISABLED="true",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
    )
    return env


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--":
        print(__doc__, file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="adp-isolated-tests-") as temporary:
        return subprocess.run(
            sys.argv[2:], env=isolated_environment(Path(temporary)), check=False
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
