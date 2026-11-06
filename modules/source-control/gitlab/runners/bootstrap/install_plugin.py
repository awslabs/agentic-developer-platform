#!/usr/bin/env python3
"""Copy only the reviewed connector into the manager's read-only plugin mount."""

import hashlib
import os
import re
import shutil
import stat
import sys
from pathlib import Path


def verify(path, expected):
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("plugin_not_verified")
    if stat.S_IMODE(path.stat().st_mode) != 0o555:
        raise ValueError("plugin_not_verified")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError("plugin_not_verified")


def install(source, directory, expected):
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or directory.is_symlink() or not directory.is_dir():
        raise ValueError("plugin_not_verified")
    verify(source, expected)
    target = directory / "fleeting-plugin-aws"
    if target.exists() or target.is_symlink():
        verify(target, expected)
        return
    try:
        with source.open("rb") as original, target.open("xb") as destination:
            shutil.copyfileobj(original, destination)
        target.chmod(0o555)
        verify(target, expected)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def main():
    if len(sys.argv) != 4:
        raise SystemExit("plugin_not_verified")
    try:
        install(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
    except Exception:
        raise SystemExit("plugin_not_verified") from None


if __name__ == "__main__":
    main()
