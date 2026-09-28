"""Bind an extracted Git archive to a dispatcher-approved revision and tree."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

import native_image as image
import build

MAX_ATTESTATION = 64 * 1024 * 1024


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def measure(root, name):
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or str(path) != name
        or any(part in {".", "..", ".git"} for part in path.parts)
    ):
        raise image.ImageRefused("noncanonical source path")
    target = root
    for part in path.parts[:-1]:
        target = target / part
        if target.is_symlink() or not target.is_dir():
            raise image.ImageRefused("source parent is not a real directory")
    target = root / name
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode):
        payload = os.fsencode(os.readlink(target))
        size, mode = len(payload), "120000"
    elif stat.S_ISREG(info.st_mode):
        payload = None
        size = info.st_size
        mode = "100755" if info.st_mode & 0o111 else "100644"
    else:
        raise image.ImageRefused("special source file")
    sha = hashlib.sha256()
    blob = hashlib.sha1(b"blob " + str(size).encode() + b"\0", usedforsecurity=False)
    if payload is not None:
        sha.update(payload)
        blob.update(payload)
    else:
        with target.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                sha.update(chunk)
                blob.update(chunk)
    return {"mode": mode, "sha256": sha.hexdigest(), "blob": blob.hexdigest()}


def tree_id(files):
    tree = {}
    for name, value in files.items():
        cursor = tree
        parts = name.split("/")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = (value["mode"], value["blob"])

    def encode(entries):
        payload = bytearray()
        for name, value in sorted(
            entries.items(),
            key=lambda item: os.fsencode(
                item[0] + ("/" if isinstance(item[1], dict) else "")
            ),
        ):
            mode, oid = ("40000", encode(value)) if isinstance(value, dict) else value
            payload.extend(
                mode.encode() + b" " + os.fsencode(name) + b"\0" + bytes.fromhex(oid)
            )
        return hashlib.sha1(
            b"tree " + str(len(payload)).encode() + b"\0" + payload,
            usedforsecurity=False,
        ).hexdigest()

    return encode(tree)


def create(root, revision):
    root = Path(root).resolve()
    image.pattern(revision, r"[a-f0-9]{40}")
    if git(root, "rev-parse", "HEAD").decode().strip() != revision or git(
        root, "status", "--porcelain", "--untracked-files=all"
    ):
        raise image.ImageRefused("dispatcher requires clean exact checkout")
    files = {}
    for entry in git(root, "ls-tree", "-rz", revision).split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        if kind != "blob":
            raise image.ImageRefused("submodules are not supported")
        name = os.fsdecode(name)
        actual = measure(root, name)
        if (actual["mode"], actual["blob"]) != (mode, oid):
            raise image.ImageRefused("dispatcher checkout differs from Git objects")
        files[name] = actual
    tree = git(root, "rev-parse", revision + "^{tree}").decode().strip()
    if tree_id(files) != tree:
        raise image.ImageRefused("dispatcher tree differs")
    return {"version": 1, "revision": revision, "tree": tree, "files": files}


def verify(root, attestation, digest, revision):
    root = Path(root).resolve()
    image.digest(digest)
    with Path(attestation).open("rb") as source:
        raw = source.read(MAX_ATTESTATION + 1)
    if len(raw) > MAX_ATTESTATION or hashlib.sha256(raw).hexdigest() != digest:
        raise image.ImageRefused("source attestation digest differs")
    value = json.loads(raw)
    image.exact(value, {"version", "revision", "tree", "files"})
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["revision"] != revision
    ):
        raise image.ImageRefused("source attestation revision differs")
    image.pattern(value["tree"], r"[a-f0-9]{40}")
    if not isinstance(value["files"], dict) or not value["files"]:
        raise image.ImageRefused("empty source attestation")
    for name, expected in value["files"].items():
        image.exact(expected, {"mode", "sha256", "blob"})
        if measure(root, name) != expected:
            raise image.ImageRefused("extracted source differs: " + name)
    observed = set()
    for directory, directories, names in os.walk(root, followlinks=False):
        for name in list(directories):
            if (Path(directory) / name).is_symlink():
                directories.remove(name)
                names.append(name)
        for name in names:
            observed.add((Path(directory) / name).relative_to(root).as_posix())
    if observed != set(value["files"]) or tree_id(value["files"]) != value["tree"]:
        raise image.ImageRefused("extracted source inventory/tree differs")
    return {
        "revision": revision,
        "tree": value["tree"],
        "attestation_sha256": digest,
        "files": len(observed),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if Path(args.output).resolve().is_relative_to(Path(args.checkout).resolve()):
        raise image.ImageRefused("attestation must be outside checkout")
    build.write(Path(args.output), create(args.checkout, args.revision))


if __name__ == "__main__":
    main()
