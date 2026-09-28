"""No legacy target or secret literal survives anywhere in this module — Issue #5530.

`dependencies.lock.yaml` asserts that this file exists and does this job:

    `tests/test_no_legacy_targets.py` asserts these exact values do not reappear anywhere in
    this directory.

This is AC-01's directory-wide half. `test_render.py` establishes that no legacy value reaches
RENDERED OUTPUT; this file establishes the stronger property that the values are not present
as usable configuration anywhere in the adopted module — because adoption copied files from a
tree whose `config.env` shipped all four as working defaults.

## The distinction this file has to make

Every legacy value necessarily APPEARS in this module: `modes.py` has to name them to refuse
them, and the lock and docs have to explain what was removed. A naive `grep` would either fail
on the denylist or pass by having no teeth.

So the rule enforced is: a legacy value may appear only as **something being refused or
described**, never as a value something would use. Concretely — allowed inside the
`LEGACY_FORBIDDEN_VALUES` denylist, in comments and docstrings, and in test fixtures that
assert refusal; refused in YAML values, shell assignments, or as a Python string used as data.

## What this file deliberately does NOT establish

That no real account id exists anywhere in ADP, or that the eventual live target is correct.
The live target is unresolved by design (`target: status: unresolved` in the lock) and remains
the EPIC A supervisor's to settle. This checks only that the LEGACY targets were not carried
across in the adoption.
"""

from __future__ import annotations

import re

import pytest
from account_factory.modes import LEGACY_FORBIDDEN_VALUES

# The legacy `config.env` values, restated here as literals rather than imported, so this
# test still fails if someone empties `LEGACY_FORBIDDEN_VALUES`. Importing them alone would
# make the check vacuous exactly when the denylist regresses.
LEGACY_VALUES = (
    "605440105851",  # the upstream management account — not ADP's
    "github-arc-runner-eks",  # core ADP's ARC runner cluster
    "prsaws+aisuperplane@amazon.com",  # a named individual's address
    "superplane-test",  # a fixed account name that collides across requests
)

# Files that may discuss the legacy values in prose. Everything else must not contain them
# at all. `modes.py` additionally holds the denylist, handled below.
PROSE_SUFFIXES = (".md",)

SKIP_DIRECTORIES = {"__pycache__", ".pytest_cache", "vendor"}

# A line that only NAMES a legacy value in explanation is fine; a line that ASSIGNS it is
# not. These are the assignment shapes that would make a value operative.
ASSIGNMENT_PATTERNS = (
    re.compile(
        r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*['\"]?(?P<value>[^'\"#\s]+)"
    ),  # sh/py
    re.compile(r"^\s*(?:-\s*)?[A-Za-z_.-]+\s*:\s*['\"]?(?P<value>[^'\"#\s]+)"),  # yaml
)


def module_files(module_dir):
    """Every text file in the adopted module, excluding vendored upstream copies.

    `vendor/` is excluded because those files are third-party, byte-for-byte at a pinned
    commit, and their integrity is enforced by checksum in `test_dependencies.py`. Editing
    them to satisfy a grep here would break that checksum — they must not be modified.
    """
    for path in sorted(module_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRECTORIES for part in path.relative_to(module_dir).parts):
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def test_the_module_has_files_to_scan(module_dir):
    """Guards against the scan passing because it found nothing."""
    scanned = list(module_files(module_dir))
    assert len(scanned) >= 8, f"only {len(scanned)} files scanned"
    names = {path.name for path, _ in scanned}
    assert {"modes.py", "render.py", "cleanup.py", "dependencies.lock.yaml"} <= names


def test_the_denylist_still_contains_every_legacy_value():
    """If this fails, the refusal in `modes.py` has been weakened."""
    assert set(LEGACY_VALUES) == set(LEGACY_FORBIDDEN_VALUES)


@pytest.mark.parametrize("value", LEGACY_VALUES)
def test_no_legacy_value_is_ever_assigned_as_a_usable_value(value, module_dir):
    """The core assertion: present as explanation, never as configuration."""
    offences = []
    for path, text in module_files(module_dir):
        for number, line in enumerate(text.splitlines(), start=1):
            if value not in line:
                continue
            stripped = line.lstrip()
            # A comment or docstring line describing the legacy value is allowed.
            if stripped.startswith(("#", "*", ">", '"""', "'''")):
                continue
            for pattern in ASSIGNMENT_PATTERNS:
                match = pattern.match(line)
                if match and value in match.group("value"):
                    offences.append(
                        f"{path.relative_to(module_dir)}:{number}: {stripped}"
                    )
    assert not offences, "legacy value used as configuration:\n" + "\n".join(offences)


@pytest.mark.parametrize("value", LEGACY_VALUES)
def test_each_legacy_value_appears_only_in_files_permitted_to_name_it(
    value, module_dir
):
    """Bounds WHERE the values may appear at all, so they cannot spread through the module.

    Permitted: the denylist in `modes.py`, the lock's explanation of what was removed, the
    tests that assert refusal, and Markdown documentation.
    """
    permitted = {
        "account_factory/modes.py",
        "dependencies.lock.yaml",
        "tests/test_no_legacy_targets.py",
        "tests/test_modes.py",
        # Names the legacy cluster in order to assert that rendering refuses it.
        "tests/test_render.py",
    }
    found = {
        str(path.relative_to(module_dir))
        for path, text in module_files(module_dir)
        if value in text
    }
    unexpected = {
        path for path in found - permitted if not path.endswith(PROSE_SUFFIXES)
    }
    assert not unexpected, f"{value!r} appears in {sorted(unexpected)}"


def test_no_default_target_is_configured_anywhere(module_dir):
    """The legacy defect was not the values themselves but that they were DEFAULTS.

    A run that supplied nothing still acted on a specific real target. So no file may define
    a default for any of the identities that select a target.
    """
    target_keys = (
        "AWS_ACCOUNT_ID",
        "ACCOUNT_ID",
        "CLUSTER_NAME",
        "ACCOUNT_EMAIL",
        "ACCOUNT_NAME",
        "ORGANIZATION_ID",
    )
    offences = []
    for path, text in module_files(module_dir):
        if path.suffix == ".md" or path.name.startswith("test_"):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith(("#", "*", ">", '"""', "'''")):
                continue
            for key in target_keys:
                # A shell-style default assignment with a non-empty value.
                if re.match(rf"^\s*(?:export\s+)?{key}\s*=\s*['\"]?[^'\"\s#]+", line):
                    offences.append(
                        f"{path.relative_to(module_dir)}:{number}: {stripped}"
                    )
    assert not offences, "a target identity is defaulted:\n" + "\n".join(offences)


@pytest.mark.parametrize(
    "description,pattern",
    [
        ("an AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
        ("a PEM private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
        (
            "a populated secret assignment",
            re.compile(
                r"(?i)^\s*(?:export\s+)?(?:aws_secret_access_key|aws_session_token)"
                r"\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{16,}"
            ),
        ),
    ],
)
def test_no_secret_literal_is_committed_in_this_module(
    description, pattern, module_dir
):
    """AC-01's "no secret literals" half, over the module as committed.

    Detector regexes and documentation examples are excluded by file: `render.py` defines the
    patterns it searches FOR, and the render tests feed AWS's own published example values
    through them. Those are the code that prevents disclosure, not a disclosure.
    """
    allowed = {"account_factory/render.py", "tests/test_render.py"}
    offences = []
    for path, text in module_files(module_dir):
        relative = str(path.relative_to(module_dir))
        if relative in allowed or relative == "tests/test_no_legacy_targets.py":
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offences.append(f"{relative}:{number}")
    assert not offences, f"{description} found at: {offences}"


def test_no_legacy_script_was_copied_into_the_module(module_dir):
    """Adoption was selective: the legacy shell scripts were NOT carried across.

    The issue's instruction was to read the reference as evidence and copy only selected
    reviewed implementation. These filenames are the legacy flow's; their presence would mean
    the whole tree had been adopted wholesale, including the fail-open and unpinned paths.
    """
    legacy_names = {
        "config.env",
        "00-validate-prerequisites.sh",
        "01-setup-iam-roles.sh",
        "02-enable-eks-capabilities.sh",
        "03-deploy-rgds.sh",
        "04-provision-account.sh",
        "04-provision-account.yaml",
        "05-validate.sh",
        "06-teardown-account.sh",
        "07-teardown-capabilities.sh",
        "deploy.sh",
    }
    present = {path.name for path, _ in module_files(module_dir)} & legacy_names
    assert not present, f"legacy artefacts were copied in: {sorted(present)}"


def test_no_fail_open_error_suppression_in_any_adopted_shell_code(module_dir):
    """`2>/dev/null || true` on a destructive step reports a failed delete as completion.

    Nearly every step of `07-teardown-capabilities.sh` ended that way. If shell code is ever
    added to this module, it must not reintroduce the pattern.
    """
    offences = []
    for path, text in module_files(module_dir):
        if path.suffix != ".sh":
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if "|| true" in line or "2>/dev/null" in line:
                offences.append(f"{path.relative_to(module_dir)}:{number}")
    assert not offences, f"fail-open error suppression at: {offences}"


# Capabilities that would let this module fetch, execute or mutate anything. Checked as
# IDENTIFIERS in executable code rather than as text, which is what makes the check both
# precise and immune to the module's own prose: `dependencies.py` quotes
# `git clone --depth 1` in an error message and `/tmp/kro` in a docstring on purpose, and a
# text search cannot tell that from an invocation. An identifier search can.
FORBIDDEN_CAPABILITIES = {
    "subprocess": "runs an external process",
    "popen": "runs an external process",
    "system": "runs an external process via the shell",
    "urlopen": "opens a network connection",
    "urlretrieve": "downloads over the network",
    "requests": "makes an HTTP request",
    "httpx": "makes an HTTP request",
    "socket": "opens a network connection",
    "boto3": "calls AWS",
    "kubernetes": "calls the Kubernetes API",
    "eval": "executes arbitrary code",
    "exec": "executes arbitrary code",
}


def _executable_identifiers(text: str) -> set[str]:
    """Identifiers in executable Python code, excluding comments and string literals."""
    import io
    import tokenize

    names = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.NAME:
                names.add(token.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return {"<unparsable>"}
    return names


def test_the_module_cannot_fetch_execute_or_mutate_anything(module_dir):
    """Offline by construction, not merely by intent — the strongest form of Design item 1.

    The legacy flow fetched its graphs with `git clone` at deploy time and resolved chart
    versions over HTTP, then applied with `kubectl`. This module cannot do any of that:
    there is no code path to a subprocess, a socket, an AWS client or a Kubernetes client.
    That is why rendering can be trusted to be offline regardless of what it is asked to
    render, and why quoting the legacy commands in docstrings is safe.
    """
    offences = []
    for path, text in module_files(module_dir):
        if path.suffix != ".py":
            continue
        # The test that verifies this check must be allowed to name the capabilities.
        if path.name == "test_no_legacy_targets.py":
            continue
        identifiers = _executable_identifiers(text)
        for capability, why in sorted(FORBIDDEN_CAPABILITIES.items()):
            if capability in identifiers:
                offences.append(
                    f"{path.relative_to(module_dir)}: uses {capability!r}, which {why}"
                )
    assert not offences, "the module gained a fetch/execute capability:\n" + "\n".join(
        offences
    )


def test_the_capability_check_detects_a_real_reintroduction():
    """Proves the check above has teeth.

    An earlier version of this check searched the source TEXT with string literals stripped,
    which made `subprocess.run(["git", "clone", url])` invisible — the forbidden words lived
    entirely inside the stripped strings. This asserts the identifier form catches it, and
    that prose describing the defect still does not trip it.
    """
    offending = 'import subprocess\nsubprocess.run(["git", "clone", "https://x/y"])\n'
    assert "subprocess" in _executable_identifiers(offending)

    prose_only = (
        '"""The legacy 03-deploy-rgds.sh ran git clone into /tmp/kro at deploy time."""\n'
        'MESSAGE = "refusing: git clone --depth 1 of a branch is not a pin"\n'
    )
    identifiers = _executable_identifiers(prose_only)
    assert not FORBIDDEN_CAPABILITIES.keys() & identifiers


def test_no_shell_script_in_the_module_fetches_at_run_time(module_dir):
    """The same property for shell code, where a quoted string IS the command."""
    forbidden = (
        ("a deploy-time git clone", re.compile(r"git\s+clone")),
        ("a /tmp working copy of upstream", re.compile(r"/tmp/kro")),
        ("a releases/latest lookup", re.compile(r"releases/latest")),
        ("an unpinned run-time fetch", re.compile(r"\bcurl\b|\bwget\b")),
    )
    offences = []
    for path, text in module_files(module_dir):
        if path.suffix != ".sh":
            continue
        body = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        for description, pattern in forbidden:
            if pattern.search(body):
                offences.append(f"{path.relative_to(module_dir)}: {description}")
    assert not offences, "run-time fetching in shell code:\n" + "\n".join(offences)
