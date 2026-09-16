"""The expected CLI release, derived from the revision — not from the download.

R10: preflight used to hash three of the nine served helpers and then hand those
same observed values to the instance as `expected_hashes`. That compares a
download against itself. It proves the bytes did not change between two reads and
nothing else, so a stale `adp-aws.py` — the file E04 and E05 depend on — was
structurally invisible.

The fix needs a source of truth the gateway cannot influence. That source is the
git object store at `expected_revision`: `git cat-file blob <rev>:<path>` returns
the exact bytes committed at that revision, and a commit SHA cannot be edited
without becoming a different SHA. So the expected hashes are a function of the
revision under test, and the served bytes are the thing being judged.

The helper list is read the same way — parsed out of `install.sh`'s own
`CLI_FILES` line at that revision — rather than duplicated here. A tenth helper
added to the installer therefore becomes a hash this evaluation checks, without
anyone remembering to update a list in the test harness. That is what closes the
"three of nine" gap permanently rather than for today's nine.
"""

from __future__ import annotations

import hashlib
import re
import subprocess

CLI_DIR = "modules/gateway/cli"

# `CLI_FILES="a b c"` in install.sh. Anchored so a similarly-named variable
# elsewhere in the script cannot match.
CLI_FILES_LINE = re.compile(r'^CLI_FILES="([^"]+)"', re.M)

# The installer itself is not in CLI_FILES (it does not install itself) but it is
# served, it is what `curl | sh` executes, and `adp update` re-pulls it. A stale
# install.sh is exactly the mixed-artifact case R10 is about.
ALWAYS = ("install.sh",)


class ReleaseError(RuntimeError):
    """The expected release could not be derived. Nothing was evaluated."""


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def git_blob(revision, path, *, repo_root=None):
    """The committed bytes of one file at one revision.

    Reads the object store directly rather than the working tree, so a dirty
    checkout, a rebase or a stray local edit cannot change what this evaluation
    considers to be the release.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "cat-file", "blob", f"{revision}:{path}"],
            cwd=repo_root,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError(f"git could not be run: {type(exc).__name__}") from None
    if result.returncode:
        raise ReleaseError(
            f"{path} does not exist at revision {revision[:12]}; "
            "the expected release cannot be derived"
        )
    return result.stdout


def helper_names(revision, *, repo_root=None, read=git_blob):
    """Every file the release installs, from the installer's own list.

    Deliberately parsed rather than hardcoded: a hardcoded list is how three of
    nine came to be checked. If the installer's list cannot be found, this raises
    instead of falling back to a guess — silently checking a subset is the bug.
    """
    text = read(revision, f"{CLI_DIR}/install.sh", repo_root=repo_root).decode(
        "utf-8", "replace"
    )
    found = CLI_FILES_LINE.search(text)
    require(
        found,
        "install.sh at the revision under test has no CLI_FILES list; "
        "the set of installed helpers cannot be determined",
    )
    names = tuple(found.group(1).split())
    require(names, "install.sh declares an empty CLI_FILES list")
    ordered = list(names)
    for name in ALWAYS:
        if name not in ordered:
            ordered.append(name)
    return tuple(sorted(ordered))


def manifest(revision, *, repo_root=None, read=git_blob):
    """name -> sha256 for the whole release, at the revision under test.

    This is what the instance is given as `expected_hashes`, and what the served
    bytes are compared against. Both consumers get the same immutable values, so
    "the gateway serves the release" and "the instance installed the release" are
    the same assertion rather than two independent guesses.
    """
    require(
        re.fullmatch(r"[0-9a-f]{40}", str(revision or "")),
        "A release manifest must be derived from a full 40-character commit SHA",
    )
    hashes = {}
    for name in helper_names(revision, repo_root=repo_root, read=read):
        payload = read(revision, f"{CLI_DIR}/{name}", repo_root=repo_root)
        require(
            payload.startswith(b"#!"),
            f"{name} at the revision under test is not a script; the release is malformed",
        )
        hashes[name] = hashlib.sha256(payload).hexdigest()
    return hashes


def compare(served, expected):
    """Classify served bytes against the expected release.

    Returns (ok, report). Three distinct outcomes, because they mean different
    things to an operator: `missing` is a route or an allowlist problem,
    `mismatched` is a stale or mixed deployment, and `unexpected` is a file being
    served that the release does not contain.
    """
    missing = sorted(set(expected) - set(served))
    unexpected = sorted(set(served) - set(expected))
    mismatched = sorted(
        name
        for name, digest in served.items()
        if name in expected and digest != expected[name]
    )
    report = {
        "checked": sorted(expected),
        "missing": missing,
        "mismatched": mismatched,
        "unexpected": unexpected,
        "expected_count": len(expected),
        "served_count": len(served),
    }
    return (not missing and not mismatched), report


__all__ = [
    "CLI_DIR",
    "ReleaseError",
    "compare",
    "git_blob",
    "helper_names",
    "manifest",
]
