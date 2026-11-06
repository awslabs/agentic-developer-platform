"""Original Transform patch attribution and independent Git tree comparison."""

from dataclasses import dataclass
import json
import os
import subprocess
import tempfile
from time import monotonic
import unicodedata
from hashlib import sha1, sha256


def _git_object(kind: bytes, contents: bytes) -> bytes:
    # Git object IDs use the repository's SHA-1 wire format; patches use SHA-256.
    return sha1(kind + b" " + str(len(contents)).encode() + b"\0" + contents, usedforsecurity=False).digest()


def _file_tree(files: dict[str, bytes], modes: dict[str, str] | None = None) -> str:
    if not isinstance(files, dict) or modes is not None and (
            not isinstance(modes, dict) or modes.keys() != files.keys()):
        raise ValueError("source snapshot file modes are incomplete")
    root = {}
    for path, contents in files.items():
        if (not isinstance(path, str) or not path or path.startswith("/") or "\\" in path
                or ":" in path.split("/", 1)[0] or unicodedata.normalize("NFC", path) != path
                or any(part in {"", ".", "..", ".git"} or "\0" in part
                       for part in path.split("/")) or not isinstance(contents, bytes)):
            raise ValueError("source snapshot has an invalid path or non-byte contents")
        mode = modes[path] if modes is not None else "100644"
        if not isinstance(mode, str) or mode not in {"100644", "100755"}:
            raise ValueError("source snapshot contains a link or unsupported file mode")
        cursor = root
        parts = path.split("/")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
            if not isinstance(cursor, dict):
                raise ValueError("source snapshot has conflicting paths")
        if parts[-1] in cursor:
            raise ValueError("source snapshot has conflicting paths")
        cursor[parts[-1]] = (contents, mode)

    def identity(directory):
        entries = []
        for name, value in sorted(directory.items(), key=lambda item: item[0].encode() +
                                  (b"/" if isinstance(item[1], dict) else b"")):
            if isinstance(value, dict):
                mode, digest = b"40000", identity(value)
            else:
                mode, digest = value[1].encode(), _git_object(b"blob", value[0])
            entries.append(mode + b" " + name.encode() + b"\0" + digest)
        return _git_object(b"tree", b"".join(entries))

    return identity(root).hex()


@dataclass(frozen=True)
class OriginalPatch:
    contents: bytes
    sha256: str
    bytes: int
    media_type: str
    native_job_ref: tuple[tuple[str, str], ...]
    provider_pins: tuple[tuple[str, str], ...]
    base_manifest: bytes


def capture_original_patch(contents: bytes, native_job_ref: dict, provider_pins: dict, base_manifest: dict):
    if not isinstance(contents, bytes):
        raise ValueError("provider patch requires original byte contents")
    if (not isinstance(native_job_ref, dict) or
            any(not isinstance(native_job_ref.get(field), str) or not native_job_ref[field]
                for field in ("tenant_id", "project_id", "native_id", "executor_uid",
                              "attempt_token", "input_hash")) or
            not isinstance(provider_pins, dict) or
            any(not isinstance(provider_pins.get(field), str) or not provider_pins[field]
                for field in ("interface", "cli_version", "binary_sha256", "image_digest",
                              "definition_revision")) or
            not isinstance(base_manifest, dict) or not base_manifest.get("source_snapshot_ids")):
        raise ValueError("provider patch requires pinned native, provider and source identities")
    identity = tuple(sorted((field, native_job_ref[field]) for field in (
        "tenant_id", "project_id", "native_id", "executor_uid", "attempt_token", "input_hash")))
    pins = tuple(sorted((field, provider_pins[field]) for field in (
        "interface", "cli_version", "binary_sha256", "image_digest", "definition_revision")))
    manifest = json.dumps(base_manifest, sort_keys=True, separators=(",", ":")).encode()
    return OriginalPatch(contents, sha256(contents).hexdigest(), len(contents),
                         "text/x-diff", identity, pins, manifest)


def compare_source_trees(base_files: dict[str, bytes], candidate_files: dict[str, bytes],
                         *, base_modes: dict[str, str] | None = None,
                         candidate_modes: dict[str, str] | None = None):
    base_tree = _file_tree(base_files, base_modes)
    candidate_tree = _file_tree(candidate_files, candidate_modes)
    paths = base_files.keys() | candidate_files.keys()
    if len({path.casefold() for path in paths}) != len(paths):
        raise ValueError("source snapshot has case-colliding paths")
    return {"base_tree": base_tree, "candidate_tree": candidate_tree,
            "changed_files": sorted(path for path in paths
                                    if base_files.get(path) != candidate_files.get(path)
                                    or (base_modes or {}).get(path, "100644") !=
                                    (candidate_modes or {}).get(path, "100644"))}


def verify_original_patch(base_files: dict[str, bytes], candidate_tree: str,
                          original_patch: OriginalPatch, base_modes: dict[str, str]):
    _file_tree(base_files, base_modes)
    if len(original_patch.contents) > 16 * 1024 * 1024:
        raise ValueError("original patch exceeds bounded verification size")
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_TERMINAL_PROMPT": "0"}
    deadline = monotonic() + 30
    with tempfile.TemporaryDirectory(prefix="transform-verify-") as directory:
        def git(*argv, contents=None):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ValueError("original patch verification exceeded its limit")
            try:
                completed = subprocess.run(["git", "-C", directory, *argv], input=contents,
                                           capture_output=True, env=environment, timeout=remaining,
                                           check=False)
            except subprocess.TimeoutExpired as error:
                raise ValueError("original patch verification exceeded its limit") from error
            if completed.returncode:
                raise ValueError("original patch cannot be independently applied")
            return completed.stdout

        git("init", "-q", "--object-format=sha1")
        for path, contents in base_files.items():
            digest = git("hash-object", "-w", "--no-filters", "--stdin", contents=contents).strip().decode()
            git("update-index", "--add", "--cacheinfo", f"{base_modes[path]},{digest},{path}")
        base_tree = git("write-tree").strip().decode()
        git("apply", "--cached", "--binary", "-", contents=original_patch.contents)
        if git("write-tree").strip().decode() != candidate_tree:
            raise ValueError("original patch does not produce the independently read candidate tree")
        changed = git("diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z",
                      base_tree, candidate_tree)
        return {"base_tree": base_tree, "candidate_tree": candidate_tree,
                "changed_files": sorted(path.decode() for path in changed.split(b"\0") if path)}
