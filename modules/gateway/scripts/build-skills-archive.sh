#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ "${1:-}" == "--stage" ]]; then
  SOURCE="${GATEWAY_DIR}/../agent-factory/skills"
  TARGET="${GATEWAY_DIR}/skills-source"
  python3 - "$SOURCE" "$TARGET" <<'PY'
import json
import pathlib
import re
import shutil
import sys

source, target = map(pathlib.Path, sys.argv[1:])
manifest = json.loads((source / "MANIFEST.json").read_text())
if manifest.get("schema_version") != 1 or not isinstance(manifest.get("skills"), list):
    raise SystemExit("invalid skills manifest")
shutil.rmtree(target, ignore_errors=True)
target.mkdir()
shutil.copyfile(source / "MANIFEST.json", target / "MANIFEST.json")
for skill in manifest["skills"]:
    name = skill.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name):
        raise SystemExit("invalid skill name")
    destination = target / name
    destination.mkdir()
    if not (source / name / "SKILL.md").is_file():
        raise SystemExit(f"missing skill: {name}")
    for path in sorted((source / name).rglob("*")):
        relative = path.relative_to(source / name)
        if "tests" in relative.parts or "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise SystemExit("symlinks cannot be packaged as skills")
        if path.is_file():
            target_file = destination / relative
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target_file)
shared = source / "_shared"
if shared.is_dir():
    # Same exclusions as the skills: a test run that imported a _shared script leaves
    # __pycache__ behind, and that must never ship (CI shard order made it visible).
    shutil.copytree(shared, target / "_shared", symlinks=True, ignore=shutil.ignore_patterns("tests", "__pycache__"))
# The skills tell the agent to read the canonical issue-authoring guide and
# templates from _shared/authoring/; pack them from the rules tree beside skills.
AUTHORING = (
    "agents/issue-authoring.md",
    "templates/epic-issue.md",
    "templates/developer-issue.md",
    "templates/delivery-loop/evaluation-template.md",
    "templates/delivery-loop/defect-template.md",
)
rules = source.parent / "rules"
if rules.is_dir():
    for relative in AUTHORING:
        path = rules / relative
        if not path.is_file():
            raise SystemExit(f"missing authoring resource: {relative}")
        target_file = target / "_shared" / "authoring" / pathlib.Path(relative).name
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target_file)
PY
  exit 0
fi

if [[ "${1:-}" != "--build" || "$#" != 4 ]]; then
  echo "Usage: build-skills-archive.sh --stage | --build SOURCE OUTPUT ADP_SCRIPT" >&2
  exit 1
fi
python3 - "$2" "$3" "$4" <<'PY'
import gzip
import hashlib
import io
import json
import pathlib
import re
import sys
import tarfile

source, output, adp = map(pathlib.Path, sys.argv[1:])
manifest = json.loads((source / "MANIFEST.json").read_text())
if manifest.get("schema_version") != 1 or not isinstance(manifest.get("skills"), list):
    raise SystemExit("invalid skills manifest")
version = re.search(r'^readonly ADP_VERSION="([^"]+)"', adp.read_text(), re.MULTILINE)
if not version:
    raise SystemExit("CLI version missing")
manifest["version"] = version.group(1)
files = [("MANIFEST.json", (json.dumps(manifest, sort_keys=True) + "\n").encode())]
names = set()
for skill in manifest["skills"]:
    if not isinstance(skill, dict):
        raise SystemExit("invalid skills manifest entry")
    name, clients, invocation = (skill.get(key) for key in ("name", "clients", "invocation"))
    if (not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name)
            or name in names or not isinstance(clients, list) or not clients
            or any(client not in ("codex", "claude-code") for client in clients)
            or not isinstance(invocation, str) or not invocation.strip()):
        raise SystemExit("invalid skills manifest entry")
    names.add(name)
    if not (source / name / "SKILL.md").is_file():
        raise SystemExit(f"missing skill: {name}")
    for path in sorted((source / name).rglob("*")):
        relative = path.relative_to(source / name)
        if "tests" in relative.parts or "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise SystemExit("symlinks cannot be packaged as skills")
        if path.is_file():
            files.append(("skills/" + path.relative_to(source).as_posix(), path.read_bytes()))
shared = source / "_shared"
if shared.is_dir():
    for path in sorted(shared.rglob("*")):
        if "tests" in path.relative_to(shared).parts or "__pycache__" in path.relative_to(shared).parts:
            continue
        if path.is_symlink():
            raise SystemExit("symlinks cannot be packaged as skills")
        if path.is_file():
            files.append((path.relative_to(source).as_posix(), path.read_bytes()))
# Canonical issue-authoring guide and templates, read by the skills from
# _shared/authoring/ (see SKILL.md "Backlog draft"). Packed from the rules tree
# beside the skills so the installed bundle is complete without a checkout.
AUTHORING = (
    "agents/issue-authoring.md",
    "templates/epic-issue.md",
    "templates/developer-issue.md",
    "templates/delivery-loop/evaluation-template.md",
    "templates/delivery-loop/defect-template.md",
)
rules = source.parent / "rules"
if rules.is_dir():
    for relative in AUTHORING:
        path = rules / relative
        if not path.is_file():
            raise SystemExit(f"missing authoring resource: {relative}")
        files.append(("_shared/authoring/" + pathlib.Path(relative).name, path.read_bytes()))
if sum(len(body) for _, body in files) > 4 * 1024 * 1024:
    raise SystemExit("skills archive exceeds unpacked limit")
output.mkdir(parents=True, exist_ok=True)
archive_path = output / "adp-skills.tar.gz"
with archive_path.open("wb") as output_file:
    with gzip.GzipFile(fileobj=output_file, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|") as archive:
            for name, body in files:
                entry = tarfile.TarInfo(name)
                entry.size = len(body)
                entry.mode = 0o644
                archive.addfile(entry, io.BytesIO(body))
if archive_path.stat().st_size > 1024 * 1024:
    archive_path.unlink()
    raise SystemExit("skills archive exceeds 1 MiB")
digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
(output / "adp-skills.tar.gz.sha256").write_text(f"{digest}  adp-skills.tar.gz\n")
PY
