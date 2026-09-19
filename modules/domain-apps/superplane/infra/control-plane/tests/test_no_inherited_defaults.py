"""No upstream default may be inherited — Issue #5042 (U3), EPIC #4910.

## The property a plan cannot assert

`tests/no_inherited_defaults.tftest.hcl` proves that supplying an upstream account is
REJECTED, and that supplying an ADP-owned one is accepted. It cannot prove that
`account_id` has no default, because there is no way to assert an absence from inside a
plan: omitting the variable makes `terraform test` fail with "no value for required
variable", which is a framework error rather than an assertion, and it aborts the run
instead of reporting it.

That absence is the whole mechanism of acceptance criterion 3. A default is what makes a
deploy target an account nobody chose — so it is checked here, as text.

## The rule is provenance, not value class

An account id is not a secret (design §7: "Account IDs are non-secret metadata but still
access-controlled") and an account deliberately selected for a deployment is a legitimate
tfvars input. So this suite does NOT flag 12-digit numbers on sight. It checks three
specific things:

* `account_id` declares no `default` at all;
* the two upstream snapshot accounts appear nowhere as a value in this module's Terraform;
* ADP's own account is NOT in the deny list — blocking it would reject a legitimate account
  for resembling snapshot material, which is the value-class error the rule exists to
  avoid.

The third is the one that makes this a provenance suite rather than a blocklist, and it is
the one most likely to be "fixed" by someone hardening the list without reading why.

## Why comments are excluded

variables.tf documents the upstream accounts it blocks, and must: a deny list without the
provenance of each entry is a list nobody can safely modify later. Asserting over raw text
would flag that documentation as the very defect it explains.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CONTROL_PLANE = Path(__file__).resolve().parents[1]

# Upstream AISuperPlane snapshot accounts. Forbidden as VALUES anywhere in this module's
# Terraform: 605440105851 is upstream's ECR registry and state bucket, 938500344975 is its
# test cluster.
UPSTREAM_ACCOUNTS = ("605440105851", "938500344975")

# ADP's own account, live on main in .github/workflows/agent-*.yml as
# `${{ vars.BEADS_S3_BUCKET || 'adp-beads-state-193832579677' }}`. It appears in the
# upstream snapshot only because reference/tmp/agent-*.yml are copies of ADP's own
# workflows. It must never be added to the deny list.
ADP_OWN_ACCOUNT = "193832579677"

# Inputs that must be supplied deliberately rather than defaulted. Each is a value whose
# wrong-but-plausible default would deploy something somewhere nobody chose:
# `account_id` picks the account, `cors_allowed_origins` decides who may call the API with
# credentials, and the two secret names decide which credentials the pods resolve.
REQUIRED_WITHOUT_DEFAULT = (
    "account_id",
    "cors_allowed_origins",
    "database_secret_name",
    "jwt_secret_name",
)


def _terraform_files() -> list[Path]:
    return sorted(CONTROL_PLANE.glob("*.tf"))


def _strip_comments(text: str) -> str:
    """Remove comments AND heredoc bodies, leaving only executable configuration.

    Heredocs are stripped for the same reason comments are, and it is not an optimisation:
    the deny-list `error_message` names each blocked account and says what it is —

        account_id is an upstream AISuperPlane snapshot account (605440105851 = upstream
        ECR/state bucket, 938500344975 = upstream test cluster) ...

    — because an operator who hits that error needs to know why their account was refused.
    Without this, the tests below flagged that explanation as a hardcoded account, i.e. the
    guard treated the documentation of the rule as a violation of it.

    Heredocs in this module are only ever `description` and `error_message` prose. An
    account used as a real VALUE appears in a quoted string or an assignment, which this
    keeps.
    """
    out: list[str] = []
    heredoc_terminator: str | None = None

    for line in text.splitlines():
        if heredoc_terminator is not None:
            if line.strip() == heredoc_terminator:
                heredoc_terminator = None
            continue

        opening = re.search(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\s*$", line)
        if opening:
            heredoc_terminator = opening.group(1)
            continue

        stripped = line.strip()
        if stripped.startswith(("#", "//")):
            continue
        out.append(line.split("#", 1)[0])

    return "\n".join(out)


def _is_deny_list_line(line: str, account: str) -> bool:
    """Is this line a deny-list entry rather than a use of the account?

    The check is "the line contains nothing but quoted account ids and list punctuation",
    which is the shape of the `contains([...])` argument in variables.tf whether the entries
    sit on one line or several. An earlier version of this helper required exactly one
    quoted account per line; `terraform fmt` puts both on the same line, so that version
    reported the deny list itself as a violation — a guard whose own formatting assumption
    made a correct module fail.
    """
    if account not in line:
        return False
    # Remove every quoted 12-digit account, then require only list punctuation to remain.
    residue = re.sub(r'"[0-9]{12}"', "", line)
    return re.fullmatch(r"[\s\[\],()]*", residue) is not None


def _variable_blocks(text: str) -> dict[str, str]:
    """Map variable name -> block body, for `variable "x" { ... }` declarations."""
    blocks: dict[str, str] = {}
    for match in re.finditer(r'variable\s+"([^"]+)"\s*\{', text):
        name = match.group(1)
        depth = 0
        start = match.end() - 1
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    blocks[name] = text[start + 1 : index]
                    break
    return blocks


VARIABLES_TF = CONTROL_PLANE / "variables.tf"


def test_variables_file_parses_into_blocks() -> None:
    """A premise check: every later test reads the parsed blocks.

    If the brace-matching above returned nothing — a syntax change, a moved file — the
    parametrized tests would collect against an empty mapping and pass without checking
    anything.
    """
    assert VARIABLES_TF.is_file(), f"{VARIABLES_TF} must exist."
    blocks = _variable_blocks(VARIABLES_TF.read_text())
    assert len(blocks) >= 8, (
        f"parsed only {len(blocks)} variable blocks from variables.tf, which suggests the "
        "parser in this suite has stopped matching rather than that variables were removed."
    )


@pytest.mark.parametrize("name", REQUIRED_WITHOUT_DEFAULT)
def test_sensitive_inputs_declare_no_default(name: str) -> None:
    """These variables must be required, so a deploy names them explicitly.

    This is the assertion `terraform test` structurally cannot make — see the module
    docstring.
    """
    blocks = _variable_blocks(VARIABLES_TF.read_text())
    assert name in blocks, f"variables.tf must declare `{name}`."

    body = _strip_comments(blocks[name])
    assert not re.search(r"^\s*default\s*=", body, re.MULTILINE), (
        f"variable `{name}` declares a default. It must be required: a default is what "
        "makes a deploy use a value nobody chose, which is the inherited-default defect "
        "acceptance criterion 3 forbids. Supply it in "
        "environments/<env>/modules/superplane.tfvars instead."
    )


@pytest.mark.parametrize("account", UPSTREAM_ACCOUNTS)
def test_upstream_accounts_appear_only_as_denied_values(account: str) -> None:
    """An upstream account may appear only inside the deny list, never as a usable value.

    The distinction matters: `contains(["605440105851"], var.account_id)` is the rule that
    blocks it, while `account_id = "605440105851"` or a bucket name containing it would be
    the defect. Both are the same literal, so position is what separates them.
    """
    for path in _terraform_files():
        body = _strip_comments(path.read_text())
        if account not in body:
            continue

        for line in body.splitlines():
            if account not in line:
                continue
            # The only legitimate appearance: an entry in the deny list's `contains(...)`
            # argument, which is a list of quoted account ids.
            assert _is_deny_list_line(line, account), (
                f"{path.name} references upstream account {account} outside the deny "
                f"list:\n    {line.strip()}\nUpstream accounts may be named as forbidden "
                "values, never used as real ones."
            )


def test_adp_own_account_is_not_in_the_deny_list() -> None:
    """ADP's own account must not be blocked.

    This is the provenance rule's teeth. 193832579677 appears in the upstream snapshot, so
    it looks like a third upstream account and invites being added here for consistency. It
    is ADP's own, live on main today. Blocking it would reject a legitimate ADP account for
    resembling snapshot material — value-class reasoning, which is the error acceptance
    criterion 3 is written against.
    """
    for path in _terraform_files():
        body = _strip_comments(path.read_text())
        assert ADP_OWN_ACCOUNT not in body, (
            f"{path.name} references {ADP_OWN_ACCOUNT}, which is ADP's OWN account (live "
            "on main in .github/workflows/agent-developer.yml and siblings as "
            "`vars.BEADS_S3_BUCKET || 'adp-beads-state-193832579677'`). It must not be "
            "added to the upstream deny list: presence in the snapshot is not the test, "
            "ownership is. See the comment above `account_id` in variables.tf."
        )


def test_the_deny_list_still_blocks_both_upstream_accounts() -> None:
    """The complement of the test above: hardening must not become loosening.

    Read as text rather than trusted from the .tftest.hcl runs, because those runs assert
    behaviour for two specific values; this asserts the list itself has not been emptied in
    a refactor that left the validation block in place.
    """
    body = _strip_comments(VARIABLES_TF.read_text())
    for account in UPSTREAM_ACCOUNTS:
        assert account in body, (
            f"upstream account {account} is no longer named in variables.tf. Both upstream "
            "snapshot accounts must remain blocked — see "
            "tests/no_inherited_defaults.tftest.hcl for which is which."
        )


def test_no_backend_or_registry_hardcodes_an_account() -> None:
    """No account literal may be baked into a bucket, registry or ARN string.

    Upstream's defect was not a variable default — it was
    `bucket = "superplane-terraform-state-605440105851"`, a literal inside a string. So
    string interpolations must derive the account from the caller or the variable.
    """
    account_re = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")

    for path in _terraform_files():
        body = _strip_comments(path.read_text())
        for line in body.splitlines():
            for found in account_re.finditer(line):
                account = found.group(0)
                # Deny-list entries are checked by the tests above.
                if _is_deny_list_line(line, account):
                    continue
                raise AssertionError(
                    f"{path.name} embeds account literal {account} in a string:\n"
                    f"    {line.strip()}\nDerive it from var.account_id or "
                    "data.aws_caller_identity.current.account_id instead — a literal "
                    "silently targets one account regardless of who deploys."
                )
