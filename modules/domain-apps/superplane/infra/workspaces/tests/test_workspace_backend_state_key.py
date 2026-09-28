"""The backend block, checked as text — Issue #5532 (w6-09) design item 2, AC-01.

## Why a text-level test exists alongside backend.tftest.hcl

Terraform resolves the `backend` block during `init`, before any test runs, and it is not
part of the plan graph. **No `terraform test` assertion can see it.** `backend.tftest.hcl`
asserts `output.state_key_convention` has the right shape — which is the convention this
module DOCUMENTS — but it cannot verify the block that convention describes is actually
empty. A module could pass every run in that file while hardcoding a bucket and key in
`versions.tf`, and every workspace would then share one state object.

Reading the file as text is the only mechanism that can check it. That is a weaker kind of
test than a plan assertion, and it is used here for the one property with no stronger form
available. `../control-plane/tests/test_backend_state_key.py` records the same reasoning for
the control plane, where the upstream defect it caught was a real hardcoded bucket.

## Why this file was written, which is worth recording

`versions.tf` and `outputs.tf` cited "tests/test_backend_state_key.py" as the evidence for
this property. That file did not exist in this module. It exists in `../control-plane/` — which
is also why THIS file carries a `test_workspace_` prefix: two test files with the same basename
and no `__init__.py` cannot both be collected by pytest. And
`test_comment_references_resolve.py`'s first version accepted the citation for exactly that
reason — a file by that name resolved, in a sibling module that cannot assert anything about
this module's backend block. A mutation test exposed the acceptance as vacuous.

So the original comments were right that this test should exist, and the honest fix was to
write it rather than to repoint the comments at a weaker check.

## The stake here is higher than for the control plane

The control plane is instantiated once per environment. This module is instantiated once per
WORKSPACE, so the key's workspace segment is the only thing separating one tenant's record of
what exists from another's. Two workspaces initialized against the same key would each
compute that the other's cluster, VPC and node group are unconfigured and should be
destroyed — see the note in main.tf on what a diff against a shared record means.

## Scope

This asserts the state LOCATION is not pinned in version control, and that the documented
key convention carries both the environment and the workspace. It cannot assert what an
operator actually passes to `-backend-config` at init; that is live operational evidence.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKSPACES = Path(__file__).resolve().parents[1]
VERSIONS = WORKSPACES / "versions.tf"
OUTPUTS = WORKSPACES / "outputs.tf"

ACCOUNT_ID_RE = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")

# Settings that must never be inline in the backend block. Each would pin the state location
# into version control instead of leaving it to -backend-config, and for this module that
# means pinning it identically for every workspace.
FORBIDDEN_BACKEND_SETTINGS = ("bucket", "key", "region", "dynamodb_table", "profile")


def _strip_comments(text: str) -> str:
    """Drop `#` and `//` comment lines.

    The comments in `versions.tf` legitimately QUOTE the required key shape as documentation
    of the convention. Asserting over raw text would flag the explanation of the rule as a
    violation of it — the same reason the control plane's equivalent strips comments.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "//")):
            continue
        out.append(line.split("#", 1)[0])
    return "\n".join(out)


def _backend_block(text: str) -> str:
    """Return the body of the `backend "s3"` block from versions.tf."""
    match = re.search(r'backend\s+"s3"\s*\{', text)
    assert match, (
        'versions.tf must declare a backend "s3" block. Without one this module would keep '
        "state locally, and a workspace's record of what exists would live on whichever "
        "machine last ran it."
    )

    depth = 0
    start = match.end() - 1
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    raise AssertionError("unterminated backend block in versions.tf")


def test_versions_file_exists() -> None:
    """Premise check: the file every assertion below reads must be present."""
    assert VERSIONS.is_file(), (
        f"{VERSIONS} is missing. It declares the backend whose emptiness is the "
        f"per-workspace state isolation property; if it moved, update this suite's path "
        f"rather than deleting the check."
    )


def test_backend_block_declares_no_state_location() -> None:
    """The backend block must be empty, so the state location comes from -backend-config.

    This is the assertion `terraform test` cannot make, because the backend is resolved at
    init and never enters the plan graph.
    """
    body = _strip_comments(_backend_block(VERSIONS.read_text()))

    for setting in FORBIDDEN_BACKEND_SETTINGS:
        assert not re.search(rf"^\s*{setting}\s*=", body, re.MULTILINE), (
            f"versions.tf hardcodes `{setting}` in the backend block.\n\n"
            f"The state location must be supplied by -backend-config at init. This module "
            f"is instantiated ONCE PER WORKSPACE, so an inline `{setting}` is shared by "
            f"every workspace: two workspaces would then diff against one state object, and "
            f"each would compute that the other's cluster, VPC and node group should be "
            f"destroyed.\n\n"
            f"Required shape:\n"
            f'  key = "<environment>/modules/superplane-workspaces/<workspace>/terraform.tfstate"'
        )

    assert not ACCOUNT_ID_RE.search(body), (
        "versions.tf embeds a 12-digit account id in the backend block. The state bucket "
        "belongs to whichever account the operator is authenticated to; a committed literal "
        "points every deploy at whichever account was current when the line was written."
    )


def test_documented_key_convention_carries_environment_and_workspace() -> None:
    """The `state_key_convention` output must interpolate BOTH identities.

    `backend.tftest.hcl` asserts the rendered value for one set of inputs. This asserts the
    SOURCE interpolates both variables, which a single rendered example cannot distinguish
    from a literal that happens to match it.
    """
    assert OUTPUTS.is_file(), f"{OUTPUTS} is missing."

    match = re.search(
        r'output\s+"state_key_convention"\s*\{(.*?)^\}',
        OUTPUTS.read_text(),
        re.DOTALL | re.MULTILINE,
    )
    assert match, (
        'outputs.tf must declare `output "state_key_convention"`. The backend block is '
        "deliberately empty, so this output is where the required key shape is recorded for "
        "an operator and a CI lane to read."
    )
    value = match.group(1)

    for required in ("var.environment", "var.org_id", "var.workspace_id"):
        assert required in value, (
            f"`state_key_convention` does not interpolate `{required}`.\n\n"
            f"Both identities must appear in the key. Without the workspace segment every "
            f"workspace in an environment shares one state object; without the environment "
            f"segment dev and prod do. The workspace segment is the ONLY thing separating "
            f"one tenant's record of what exists from another's."
        )

    assert not ACCOUNT_ID_RE.search(value), (
        "`state_key_convention` embeds a 12-digit account id."
    )
