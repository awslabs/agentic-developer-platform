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

Reading the list from the installer means this module must also accept every
KIND of artifact the installer installs. #8242: the release gained
`native-host-requirements.txt`, the hash-pinned pip input the native credential
host builds its runtime from. It is neither a script nor a JSON document, so the
blanket "must start with #!" rule rejected it and the whole manifest raised
`ReleaseError` before preflight could check anything — a derivation failure, not
a product defect. Each artifact is therefore judged by its own contract, which is
the same split `install.sh::validate_staged` already makes on the download side.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess

CLI_DIR = "modules/gateway/cli"

# `CLI_FILES="a b c"` in install.sh. Anchored so a similarly-named variable
# elsewhere in the script cannot match.
CLI_FILES_LINE = re.compile(r'^CLI_FILES="([^"]+)"', re.MULTILINE)
CLI_VERSION_LINE = re.compile(
    rb'^readonly ADP_VERSION="([0-9]+\.[0-9]+\.[0-9]+)"', re.MULTILINE
)

# The installer itself is not in CLI_FILES (it does not install itself) but it is
# served, it is what `curl | sh` executes, and `adp update` re-pulls it. A stale
# install.sh is exactly the mixed-artifact case R10 is about.
ALWAYS = ("install.sh",)

# The pip input for the native credential host's runtime. Plain text with no
# shebang and no JSON shape; what makes it a release artifact rather than an
# arbitrary file is that every requirement is version- and hash-pinned.
NATIVE_REQUIREMENTS = "native-host-requirements.txt"
REQUIREMENTS_SUFFIX = "requirements.txt"

# The three kinds of artifact the installer installs.
JSON_KIND = "json"
REQUIREMENTS_KIND = "requirements"
SCRIPT_KIND = "script"

# `name==version`, then one or more `--hash=sha256:<64 hex>`. Nothing else is
# accepted: a bare `cryptography` or an unhashed pin would let pip resolve
# whatever the index serves at install time, which is the supply-chain hole the
# pinned file exists to close. Matched against logical lines, after pip's own
# backslash continuations are joined.
REQUIREMENT_PIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.+!-]*")
REQUIREMENT_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")

# pip's own strictest marker: with it, pip refuses to install anything unhashed.
# A file that declares it is MORE pinned than one that does not, so refusing the
# release because that line is not a requirement would abort a correct — indeed
# a safer — runtime. An allowlist of exactly one: every other bare option line
# (an index URL, a `-r` include, `--no-binary`) still makes the file something
# this harness will not vouch for.
GLOBAL_OPTIONS = ("--require-hashes",)

# Shared tail of both callers' messages, so the explanation of WHY an unpinned
# pip input is refused lives in one place even though the two sentences differ.
REQUIREMENTS_PROBLEM = (
    "a version- and hash-pinned pip requirements file; the native credential "
    "host would build an unverified runtime"
)

MANIFEST_PROBLEM = {
    JSON_KIND: "is not a JSON document",
    REQUIREMENTS_KIND: f"is not {REQUIREMENTS_PROBLEM}",
    SCRIPT_KIND: "is not a script",
}

# install.sh validates the downloaded requirements file against a digest written
# into the script itself (`validate_staged`), because a pip input cannot be
# recognised by shape the way a shebang or a JSON manifest can. Anchored on that
# comparison so the pin is read from the check that actually enforces it, not
# from the first 64 hex characters that happen to appear in the script.
INSTALLER_NATIVE_PIN = re.compile(
    r'\[\s*"\$\{name\}"\s*=\s*"native-host-requirements\.txt"\s*\]'
    r'.*?"([0-9a-f]{64})"',
    re.DOTALL,
)


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
        result = subprocess.run(
            ["git", "cat-file", "blob", f"{revision}:{path}"],
            cwd=repo_root,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError(f"git could not be run: {type(exc).__name__}") from None
    if result.returncode:
        raise ReleaseError(
            f"{path} does not exist at revision {revision[:12]}; "
            "the expected release cannot be derived"
        )
    return result.stdout


def requirement_pins(payload):
    """`{"name==version": [hashes]}` for a pinned pip file, or None if it is not one.

    None rather than an exception because both callers want to name where the
    payload came from (a revision, or a URL) in their own sentence, and because
    the served-bytes caller uses the same answer to reject the SPA fallback: an
    HTML body simply has no pinned requirement in it.

    An empty file parses cleanly and pins nothing, so it is refused too — the
    native host would build no runtime at all and fail later, somewhere less
    obvious.
    """
    try:
        text = payload.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return None
    # pip joins backslash continuations before parsing a requirement and its
    # hashes; so does this, or every hash line would look like a bare option.
    pins = {}
    for raw in text.replace("\\\n", " ").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line in GLOBAL_OPTIONS:
            continue
        requirement, *hashes = line.split()
        if not REQUIREMENT_PIN.fullmatch(requirement):
            return None
        if not hashes or not all(
            REQUIREMENT_HASH.fullmatch(option) for option in hashes
        ):
            return None
        pins[requirement] = sorted(hashes)
    return pins or None


def artifact_kind(name):
    """Which contract a released file is held to, from its name alone."""
    if name.endswith(".json"):
        return JSON_KIND
    if name.endswith(REQUIREMENTS_SUFFIX):
        return REQUIREMENTS_KIND
    return SCRIPT_KIND


def artifact_mismatch(name, payload):
    """The kind `name` must be when `payload` is not it; None when it matches.

    One rule for both sides of the comparison: `manifest` judges the bytes
    committed at the revision, `preflight.check_served_cli_hashes` judges the
    bytes the gateway serves, and a rule that differed between them would let a
    mixed release through on one side or abort a correct one on the other.

    It returns the KIND rather than a sentence because the two callers report
    the same judgement from opposite directions — "the revision ships" versus
    "the gateway served" — and their existing wording is what operators and the
    offline suite read. Share the rule; keep the sentences.

    Every branch has to reject a 200 whose body is the SPA's index.html, which
    is what a misrouted `/cli/<name>` returns.
    """
    kind = artifact_kind(name)
    if kind == JSON_KIND:
        try:
            document = json.loads(payload)
        except (ValueError, TypeError):
            document = None
        matches = isinstance(document, dict)
    elif kind == REQUIREMENTS_KIND:
        matches = requirement_pins(payload) is not None
    else:
        matches = isinstance(payload, bytes) and payload.startswith(b"#!")
    return None if matches else kind


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


def installer_native_pin(revision, *, repo_root=None, read=git_blob):
    """The native-requirements digest install.sh will accept at this revision."""
    text = read(revision, f"{CLI_DIR}/install.sh", repo_root=repo_root).decode(
        "utf-8", "replace"
    )
    found = INSTALLER_NATIVE_PIN.search(text)
    require(
        found,
        f"install.sh at the revision under test ships {NATIVE_REQUIREMENTS} but "
        "no digest to validate it against; the download could not be checked",
    )
    return found.group(1)


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
        mismatch = artifact_mismatch(name, payload)
        if mismatch:
            # Raised rather than require()d: the message is only well-defined
            # for a mismatch, and require() would build it for every artifact —
            # looking up the None that means "this one is fine".
            raise ReleaseError(
                f"{name} at the revision under test {MANIFEST_PROBLEM[mismatch]}; "
                "the release is malformed"
            )
        hashes[name] = hashlib.sha256(payload).hexdigest()
    if NATIVE_REQUIREMENTS in hashes:
        # The installer refuses any other bytes, so a pin that does not match the
        # shipped file is a release that cannot be installed at all. Catch that
        # here, from the revision, rather than as an opaque install failure on
        # the instance.
        pinned = installer_native_pin(revision, repo_root=repo_root, read=read)
        require(
            pinned == hashes[NATIVE_REQUIREMENTS],
            f"install.sh pins {NATIVE_REQUIREMENTS} digest {pinned[:12]} but the "
            f"revision ships {hashes[NATIVE_REQUIREMENTS][:12]}; the installer "
            "would refuse the released runtime",
        )
    return hashes


def cli_version(revision, *, repo_root=None, read=git_blob):
    """Return the CLI version declared by the immutable `adp` blob."""
    payload = read(revision, f"{CLI_DIR}/adp", repo_root=repo_root)
    found = CLI_VERSION_LINE.search(payload)
    require(found, "adp at the revision under test has no exact semantic ADP_VERSION")
    return found.group(1).decode()


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
    "NATIVE_REQUIREMENTS",
    "REQUIREMENTS_PROBLEM",
    "ReleaseError",
    "artifact_kind",
    "artifact_mismatch",
    "cli_version",
    "compare",
    "git_blob",
    "helper_names",
    "installer_native_pin",
    "manifest",
    "requirement_pins",
]
