"""The backend block and its tfvars, checked as text — Issue #5042 (U3), EPIC #4910.

## Why this suite exists at all

`tests/backend.tftest.hcl` covers acceptance criterion 1 as far as a plan can, and that is
not far enough. Terraform resolves the `backend` block during `init`, before any test runs,
and it is not part of the plan graph — so no `terraform test` assertion can see it. A
module could pass every run in that file while hardcoding upstream's bucket in
`versions.tf`, which is exactly the upstream defect:

    bucket         = "superplane-terraform-state-605440105851"
    key            = "control-plane/terraform.tfstate"

No environment segment in the key, so every environment writes the same state object; and
an account nobody deploying from ADP chose. The blast radius the issue records for getting
this wrong is "two environments share state; one apply destroys the other's resources".

Reading the file as text is the only mechanism that can check it. That is a weaker kind of
test than a plan assertion and it is used here deliberately, for the one property that has
no stronger form available.

## What is asserted

* the `backend "s3"` block declares no bucket, key, region or lock table inline;
* every `environments/*/modules/superplane-backend.tfvars` names a key whose leading
  segment is its own environment directory;
* the bucket is the `ACCOUNT_ID` placeholder, not a resolved account;
* no 12-digit literal appears anywhere in those files.

The suite fails if it finds no tfvars files at all, rather than passing on an empty glob —
"nothing to check" and "everything checks out" must not share an exit code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CONTROL_PLANE = Path(__file__).resolve().parents[1]
REPO_ROOT = CONTROL_PLANE.parents[4]
ENVIRONMENTS = REPO_ROOT / "environments"

BACKEND_TFVARS_GLOB = "*/modules/superplane-backend.tfvars"

# The key shape acceptance criterion 1 requires, and the one deploy-all.sh writes.
EXPECTED_KEY_TEMPLATE = "{environment}/modules/superplane/terraform.tfstate"

ACCOUNT_ID_RE = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")

# Settings that must never be inline in the backend block. Each would pin the state
# location into version control instead of leaving it to -backend-config.
FORBIDDEN_BACKEND_SETTINGS = ("bucket", "key", "region", "dynamodb_table", "profile")


def _strip_comments(text: str) -> str:
    """Drop `#` and `//` comment lines.

    Comments in these files legitimately quote the upstream defect — including its account
    id and its bad key — as documentation of what is being prevented. Asserting over raw
    text would therefore flag the explanation of the rule as a violation of it.
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
    assert match, 'versions.tf must declare a backend "s3" block.'

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


def _discover_backend_tfvars() -> list[Path]:
    if not ENVIRONMENTS.is_dir():
        return []
    return sorted(ENVIRONMENTS.glob(BACKEND_TFVARS_GLOB))


BACKEND_TFVARS = _discover_backend_tfvars()


def test_at_least_one_backend_tfvars_exists() -> None:
    """Guard against this whole suite passing on an empty glob.

    Every parametrized test below is driven by the same discovery. If it silently returned
    nothing — a rename, a moved directory — those tests would collect zero cases and the
    suite would report green while checking nothing.
    """
    assert BACKEND_TFVARS, (
        f"no {BACKEND_TFVARS_GLOB} found under {ENVIRONMENTS}. Either the backend tfvars "
        "were moved (update this suite) or they were deleted (deploy-all.sh and "
        "undeploy-phases.sh both require them)."
    )


def test_backend_block_declares_no_state_location() -> None:
    """The backend block must be empty, so the state location comes from -backend-config.

    This is the assertion `terraform test` cannot make, and the one that catches upstream's
    hardcoded bucket being copied in.
    """
    versions = CONTROL_PLANE / "versions.tf"
    assert versions.is_file(), f"{versions} must exist."

    body = _strip_comments(_backend_block(versions.read_text()))

    for setting in FORBIDDEN_BACKEND_SETTINGS:
        assert not re.search(rf"^\s*{setting}\s*=", body, re.MULTILINE), (
            f"versions.tf hardcodes `{setting}` in the backend block. The state location "
            "must be supplied by -backend-config from "
            "environments/<env>/modules/superplane-backend.tfvars, or the "
            "per-environment-state property depends on editing committed Terraform. This "
            "is the upstream defect (bucket = superplane-terraform-state-605440105851, "
            "key = control-plane/terraform.tfstate)."
        )

    assert not ACCOUNT_ID_RE.search(body), (
        "versions.tf embeds an account id in the backend block."
    )


@pytest.mark.parametrize("tfvars", BACKEND_TFVARS, ids=lambda p: p.parent.parent.name)
def test_state_key_is_scoped_to_its_own_environment(tfvars: Path) -> None:
    """The key's leading segment must be the environment directory that contains it.

    Checked against the directory name rather than against a hardcoded "dev" so that adding
    `environments/prod/` is covered the moment the file lands — a per-environment
    convention that only the first environment is tested against is one that the second
    environment breaks.
    """
    environment = tfvars.parent.parent.name
    body = _strip_comments(tfvars.read_text())

    match = re.search(r'^\s*key\s*=\s*"([^"]+)"', body, re.MULTILINE)
    assert match, f"{tfvars} must declare a state key."

    expected = EXPECTED_KEY_TEMPLATE.format(environment=environment)
    assert match.group(1) == expected, (
        f"{tfvars} declares key {match.group(1)!r} but lives in environments/"
        f"{environment}/, so its key must be {expected!r}. A key that does not track its "
        "environment is how two environments come to share one state object."
    )


@pytest.mark.parametrize("tfvars", BACKEND_TFVARS, ids=lambda p: p.parent.parent.name)
def test_bucket_uses_the_account_placeholder(tfvars: Path) -> None:
    """The bucket must carry the ACCOUNT_ID placeholder, never a resolved account.

    A committed account id is the "inherited default" defect in its most consequential
    form: it silently targets whichever account was current when the file was written, and
    it keeps working, so nothing surfaces the mistake.
    """
    body = _strip_comments(tfvars.read_text())

    match = re.search(r'^\s*bucket\s*=\s*"([^"]+)"', body, re.MULTILINE)
    assert match, f"{tfvars} must declare a state bucket."

    bucket = match.group(1)
    assert "ACCOUNT_ID" in bucket, (
        f"{tfvars} declares bucket {bucket!r}, which does not use the ACCOUNT_ID "
        "placeholder. bootstrap.sh and deploy-all.sh substitute the account the operator "
        "is actually authenticated to; a literal here overrides that silently."
    )

    assert not ACCOUNT_ID_RE.search(body), (
        f"{tfvars} contains a 12-digit account literal. Use the ACCOUNT_ID placeholder."
    )


@pytest.mark.parametrize("tfvars", BACKEND_TFVARS, ids=lambda p: p.parent.parent.name)
def test_state_is_locked_and_encrypted(tfvars: Path) -> None:
    """Locking and encryption are part of "two applies cannot collide", not extras.

    Without a lock table, two concurrent applies in the same environment corrupt one
    state object — the same outcome as a shared key, reached a different way.
    """
    body = _strip_comments(tfvars.read_text())

    assert re.search(r'^\s*dynamodb_table\s*=\s*"[^"]+"', body, re.MULTILINE), (
        f"{tfvars} must name a DynamoDB lock table; without one, concurrent applies "
        "corrupt state."
    )
    assert re.search(r"^\s*encrypt\s*=\s*true", body, re.MULTILINE), (
        f"{tfvars} must set encrypt = true."
    )
