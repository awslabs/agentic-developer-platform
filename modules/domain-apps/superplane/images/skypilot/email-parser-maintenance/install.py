"""Apply the exact CPython parser repair without changing other runtime files."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    root = Path(__file__).resolve().parent
    lock = json.loads((root / "source-lock.json").read_text())
    if ".".join(map(str, sys.version_info[:3])) != lock["python_version"]:
        raise SystemExit("Unexpected Python runtime")
    target = Path(sysconfig.get_path("stdlib")) / lock["relative_path"]
    if target.is_symlink() or not target.is_file():
        raise SystemExit("Expected a regular installed parser source file")
    before = target.read_bytes()
    if digest(before) != lock["before_sha256"]:
        raise SystemExit("Installed parser does not match the reviewed source")
    patch = (root / lock["patch"]).read_bytes()
    if digest(patch) != lock["patch_sha256"]:
        raise SystemExit("Parser patch checksum mismatch")
    if digest((root / "PSF-LICENSE").read_bytes()) != lock["license_sha256"]:
        raise SystemExit("Source license checksum mismatch")

    # Apply only independently hash-checked bytes to a temporary file. Both
    # exact input/output hashes and zero fuzz are required before installation.
    with tempfile.TemporaryDirectory(prefix="adp-email-parser-") as directory:
        scratch = Path(directory)
        source = scratch / "_parseaddr.py"
        patch_path = scratch / "source.patch"
        source.write_bytes(before)
        patch_path.write_bytes(patch)
        subprocess.run(
            [
                "patch",
                "--batch",
                "--fuzz=0",
                "--no-backup-if-mismatch",
                "--reject-file=-",
                str(source),
                str(patch_path),
            ],
            check=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        after = source.read_bytes()
    if digest(after) != lock["after_sha256"]:
        raise SystemExit("Patched parser does not match the reviewed result")
    compile(after, str(target), "exec", dont_inherit=True)

    # Bytecode for this module must not retain the previous implementation.
    # Other modules and other interpreter cache tags remain unchanged.
    cache_tag = sys.implementation.cache_tag
    caches = [target.with_suffix(".pyc")]
    caches.extend(
        target.parent / "__pycache__" / (target.stem + "." + cache_tag + suffix)
        for suffix in (".pyc", ".opt-1.pyc", ".opt-2.pyc")
    )
    removed = {}
    for cache in caches:
        if cache.is_symlink():
            raise SystemExit("Unexpected parser bytecode symlink")
        if cache.exists():
            removed[str(cache)] = digest(cache.read_bytes())
    # Check all paths before mutation; write_bytes preserves existing file mode.
    target.write_bytes(after)
    for path in removed:
        Path(path).unlink()
    print(
        json.dumps(
            {
                "path": str(target),
                "before_sha256": digest(before),
                "after_sha256": digest(after),
                "removed_bytecode": removed,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
