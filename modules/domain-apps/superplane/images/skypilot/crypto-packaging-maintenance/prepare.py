"""Prepare authenticated maintained production source without running vendor suites."""

import hashlib
import json
import subprocess
import sys
import tarfile
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


recipe = Path(__file__).resolve().parent
lock = json.loads((recipe / "source-lock.json").read_text())
archive, root, out = map(Path, sys.argv[1:])
if digest(archive) != lock["source_sha256"]:
    raise ValueError("source archive hash mismatch")
root.mkdir(parents=True, exist_ok=False)
# The exact upstream archive was authenticated before extracting it.
with tarfile.open(archive) as source:
    prefix = "cryptography-46.0.7/"
    for member in source.getmembers():
        if not member.name.startswith(prefix):
            raise ValueError("unexpected source archive root")
        member.name = member.name[len(prefix) :]
        if not member.name:
            continue
        if member.issym() or member.islnk() or ".." in Path(member.name).parts:
            raise ValueError("source archive link or parent path")
        source.extract(member, root)
for name, expected in lock["patches"].items():
    path = recipe / name
    if digest(path) != expected:
        raise ValueError("patch hash mismatch")
    subprocess.run(
        ["patch", "--batch", "--fuzz=0", "-p1", "-i", str(path)],
        cwd=root,
        check=True,
    )
for name, expected in lock["production_source_sha256"].items():
    if digest(root / name) != expected:
        raise ValueError("production source hash mismatch")
for name, before, after in [
    ("pyproject.toml", 'version = "46.0.7"', 'version = "46.0.7+adp1"'),
    (
        "src/cryptography/__about__.py",
        '__version__ = "46.0.7"',
        '__version__ = "46.0.7+adp1"',
    ),
]:
    path = root / name
    data = path.read_text()
    if data.count(before) != 1:
        raise ValueError("version context mismatch")
    path.write_text(data.replace(before, after))
out.mkdir(parents=True, exist_ok=True)
manifest = {
    "source_lock_sha256": digest(recipe / "source-lock.json"),
    "source_archive_sha256": digest(archive),
    "patches": lock["patches"],
    "source_files": {
        str(p.relative_to(root)): digest(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    },
}
(out / "prepared-source.json").write_text(json.dumps(manifest, indent=2) + "\n")
