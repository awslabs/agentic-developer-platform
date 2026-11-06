#!/usr/bin/env python3
"""Install ADP-provided skills without replacing user-owned directories."""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path


SOURCE = "adp-cli"


# Byproducts a client creates by merely running the skill (Python bytecode caches
# from scripts/plan_writer.py and scripts/publish.py). They are not user edits, so
# they must neither change the fingerprint nor be copied into a client directory;
# otherwise one run of the skill would block every later update as "modified".
BYPRODUCT_DIRS = ("__pycache__",)
BYPRODUCT_SUFFIXES = (".pyc", ".pyo")
IGNORE_BYPRODUCTS = shutil.ignore_patterns(*BYPRODUCT_DIRS, *("*" + suffix for suffix in BYPRODUCT_SUFFIXES))


def byproduct(path, root):
    relative = path.relative_to(root)
    return any(part in BYPRODUCT_DIRS for part in relative.parts) or path.suffix in BYPRODUCT_SUFFIXES


def fingerprint(directory):
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != ".adp-provenance.json" and not byproduct(path, directory):
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def unchanged(directory, sha256):
    if directory.is_symlink() or any(path.is_symlink() for path in directory.rglob("*")):
        return False
    return sha256 == fingerprint(directory)


def sync(client, source, home):
    manifest = json.loads((source / "MANIFEST.json").read_text())
    if not isinstance(manifest.get("version"), str) or not manifest["version"]:
        raise ValueError("skills manifest is missing a version")
    if not isinstance(manifest.get("skills"), list):
        raise ValueError("skills manifest is missing its skills list")
    destination = home / (".claude" if client == "claude-code" else ".codex") / "skills"
    installed = 0
    for item in manifest["skills"]:
        name = item.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name):
            raise ValueError("invalid skills manifest name")
        if client not in item.get("clients", []):
            continue
        skill = source / name
        if not (skill / "SKILL.md").is_file():
            raise ValueError(f"missing skill: {name}")
        target = destination / name
        if target.is_symlink() or target.exists():
            marker = target / ".adp-provenance.json" if target.is_dir() else None
            try:
                provenance = json.loads(marker.read_text()) if marker else {}
            except (OSError, ValueError):
                provenance = {}
            if provenance.get("source") != SOURCE or not unchanged(target, provenance.get("sha256")):
                print(f"[WARN] Refusing unmarked or modified skill directory: {target}", file=sys.stderr)
                continue
            if provenance.get("version") == manifest["version"] and fingerprint(skill) == provenance["sha256"]:
                continue
        destination.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".adp-{name}-", dir=destination))
        backup = None
        try:
            shutil.copytree(skill, staging, dirs_exist_ok=True, ignore=IGNORE_BYPRODUCTS)
            (staging / ".adp-provenance.json").write_text(json.dumps({
                "source": SOURCE, "version": manifest["version"], "sha256": fingerprint(staging)
            }) + "\n")
            if target.exists():
                backup = Path(tempfile.mkdtemp(prefix=f".adp-{name}-old-", dir=destination))
                backup.rmdir()
                target.rename(backup)
            staging.rename(target)
            installed += 1
        finally:
            if backup and backup.exists():
                if not target.exists():
                    backup.rename(target)
                else:
                    shutil.rmtree(backup)
            if staging.exists():
                shutil.rmtree(staging)
    print(f"[OK] Installed {installed} ADP skills for {client}.", file=sys.stderr)


def remove(home):
    removed = 0
    for client in (".claude", ".codex"):
        directory = home / client / "skills"
        if not directory.is_dir():
            continue
        for target in directory.iterdir():
            if target.is_symlink() or not target.is_dir():
                continue
            marker = target / ".adp-provenance.json"
            if not marker.is_file():
                continue
            try:
                provenance = json.loads(marker.read_text())
            except (OSError, ValueError):
                continue
            if provenance.get("source") != SOURCE:
                continue
            if not unchanged(target, provenance.get("sha256")):
                print(f"[WARN] Preserving modified ADP skill directory: {target}", file=sys.stderr)
                continue
            shutil.rmtree(target)
            removed += 1
    print(f"[OK] Removed {removed} ADP skills.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=("claude-code", "codex"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--home", type=Path, required=True)
    arguments = parser.parse_args()
    if not arguments.remove and (arguments.client is None or arguments.source is None):
        parser.error("sync requires --client and --source")
    try:
        if arguments.remove:
            remove(arguments.home)
        else:
            sync(arguments.client, arguments.source, arguments.home)
    except (ValueError, OSError) as error:
        parser.exit(1, f"ADP skills: {error}\n")


if __name__ == "__main__":
    main()
