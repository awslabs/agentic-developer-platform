"""Reconstruct a validation tree from gateway source and a bounded host manifest.

No source URLs, commands or check definitions come from the manifest. A service
first provisions its own workspace through the existing Task source authority.
Git's object/index machinery applies changes without executing repository code.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile

import rfc8785

from lib.codex_workspace import WorkspaceError


def apply_validation_manifest(workspace, manifest):
    fields = {"schema_version", "provider", "repository_id", "repository", "source_revision",
              "local_head", "base_tree", "tree", "changes"}
    state = workspace.state()
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or manifest["schema_version"] != "1.0" or len(rfc8785.dumps(manifest)) > 262144
            or not state["clean"]
            or any(manifest[key] != value for key, value in {
                "provider": workspace.provider, "repository": workspace.repository,
                "repository_id": workspace.repository_id, "source_revision": workspace.source_revision,
                "base_tree": state["tree"],
            }.items())
            or any(not isinstance(manifest[key], str) or not re.fullmatch(r"[a-f0-9]{40}", manifest[key])
                   for key in ["local_head", "base_tree", "tree"])
            or not isinstance(manifest["changes"], list) or len(manifest["changes"]) > 100):
        raise WorkspaceError("Validation manifest differs from authorized source")
    changes, seen, total = [], set(), 0
    for entry in manifest["changes"]:
        if (not isinstance(entry, dict) or set(entry) != {"path", "mode", "deleted", "content_base64"}
                or entry["mode"] not in {"100644", "100755"} or type(entry["deleted"]) is not bool):
            raise WorkspaceError("Invalid validation change")
        path = entry["path"]
        workspace._parts(path)
        if path in seen or re.search(r"[\x00-\x1f\x7f]", path):
            raise WorkspaceError("Duplicate or invalid validation path")
        seen.add(path)
        if entry["deleted"]:
            if entry["content_base64"] is not None:
                raise WorkspaceError("Deleted validation file contains data")
            content = None
        else:
            try:
                content = base64.b64decode(entry["content_base64"], validate=True)
            except (ValueError, TypeError):
                raise WorkspaceError("Invalid validation file encoding") from None
        total += len(content) if content is not None else 0
        if total > 192 * 1024:
            raise WorkspaceError("Validation changes exceed bound")
        changes.append((path, entry["mode"], content))

    with tempfile.TemporaryDirectory(prefix="adp-validation-index-") as directory:
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": directory,
            "BG_CONFIG_DIR": directory, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_INDEX_FILE": os.path.join(directory, "index"),
            "GIT_AUTHOR_NAME": "ADP", "GIT_AUTHOR_EMAIL": "adp@localhost",
            "GIT_COMMITTER_NAME": "ADP", "GIT_COMMITTER_EMAIL": "adp@localhost",
        }

        def git(*args, content=None):
            result = subprocess.run(
                ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                 "-c", "commit.gpgsign=false", *args], cwd=workspace.root, env=env,
                input=content, capture_output=True, timeout=30, check=False,
            )
            if result.returncode:
                raise WorkspaceError("Validation tree reconstruction failed")
            return result.stdout.decode().strip()

        git("read-tree", manifest["base_tree"])
        records = bytearray()
        for path, mode, content in changes:
            blob = "0" * 40 if content is None else git("hash-object", "-w", "--stdin", content=content)
            records.extend(f"{'0' if content is None else mode} {blob}\t{path}\0".encode())
        if records:
            git("update-index", "-z", "--index-info", content=bytes(records))
        tree = git("write-tree")
        if tree != manifest["tree"]:
            raise WorkspaceError("Reconstructed validation tree differs from host manifest")
        commit = git("commit-tree", tree, "-p", state["localHead"], content=b"Reconstructed validation source\n")
    # Only a tree whose identity matched may become the executable source.
    workspace._git("reset", "--hard", commit)
    result = workspace.state()
    if not result["clean"] or result["tree"] != manifest["tree"]:
        raise WorkspaceError("Validation checkout differs from reconstructed tree")
    return result
