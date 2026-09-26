"""Resource names read OUT OF THE TERRAFORM SOURCE — Issue #5042 (U3), EPIC #4910.

## Why this module exists

A checkpoint review of `84e3f7ee` found the ownership guard denying the two resources the
module actually creates (`adp-dev-superplane-control-plane`, `/adp/dev/superplane/namespace`)
while accepting a foreign name. The guard was wrong, but the reason the *test suite* did not
say so is the more important defect: every positive fixture used a hand-written name —
`adp-superplane-dev-api`, `/adp/superplane/dev/namespace` — that no Terraform in this module
produces. The suite proved the guard self-consistent with the test author's assumption, and
both were wrong in the same direction.

A hand-written positive fixture cannot catch a naming disagreement, because it *is* the
disagreement. So this module derives the names from `main.tf`, `config.tf`, `irsa.tf` and
`ecr.tf` and every positive fixture is built from them. If someone changes `name_prefix` in
`main.tf`, these names change with it; if they change it to something the guard rejects, the
tests fail. That is the property the previous fixtures lacked.

Deliberately NOT a Terraform invocation: parsing the two `local` definitions and the `name =`
expressions is enough to reproduce the names, and it keeps the suite offline and provider-free
(no account, no credentials, no `terraform init`).
"""

from __future__ import annotations

import re
from pathlib import Path

CONTROL_PLANE_DIR = Path(__file__).resolve().parents[1]


def _read(filename: str) -> str:
    return (CONTROL_PLANE_DIR / filename).read_text(encoding="utf-8")


def _local_value(filename: str, local_name: str) -> str:
    """Extract a simple string `local` definition, e.g. `name_prefix = "adp-${var.x}-y"`."""
    source = _read(filename)
    match = re.search(
        rf'^\s*{re.escape(local_name)}\s*=\s*"([^"]+)"\s*$', source, re.MULTILINE
    )
    if (
        match is None
    ):  # pragma: no cover - a rename should fail loudly, not silently pass
        raise AssertionError(
            f"could not find local '{local_name}' in {filename}. If it was renamed, update "
            f"this fixture module — the ownership guard's contract depends on it."
        )
    return match.group(1)


def _resolve(template: str, environment: str) -> str:
    """Substitute the only interpolation these prefixes use: `${var.environment}`."""
    return template.replace("${var.environment}", environment)


def name_prefix(environment: str) -> str:
    """`adp-<env>-superplane`, from main.tf. The FLAT family's prefix."""
    return _resolve(_local_value("main.tf", "name_prefix"), environment)


def parameter_prefix(environment: str) -> str:
    """`/adp/<env>/superplane`, from config.tf. The SSM family's prefix."""
    return _resolve(_local_value("config.tf", "parameter_prefix"), environment)


def iam_role_names(environment: str) -> list[str]:
    """Every `aws_iam_role` name this module declares, resolved for `environment`."""
    source = "\n".join(
        path.read_text() for path in sorted(CONTROL_PLANE_DIR.glob("*.tf"))
    )
    suffixes = re.findall(
        r'resource\s+"aws_iam_role"\s+"[^"]+"\s*\{[^}]*?name\s*=\s*'
        r'"\$\{local\.name_prefix\}([^"]*)"',
        source,
        re.DOTALL,
    )
    declared = re.findall(r'resource\s+"aws_iam_role"\s+"[^"]+"', source)
    if not suffixes or len(suffixes) != len(declared):  # pragma: no cover
        raise AssertionError("not every maintained aws_iam_role name could be derived")
    return [f"{name_prefix(environment)}{suffix}" for suffix in suffixes]


def ssm_parameter_names(environment: str) -> list[str]:
    """Every `aws_ssm_parameter` name this module declares, resolved for `environment`."""
    source = _read("config.tf")
    suffixes = re.findall(
        r'name\s*=\s*"\$\{local\.parameter_prefix\}([^"]*)"',
        source,
    )
    if not suffixes:  # pragma: no cover
        raise AssertionError("no aws_ssm_parameter names found in config.tf")
    return [f"{parameter_prefix(environment)}{suffix}" for suffix in suffixes]


def ecr_repository_names() -> list[str]:
    """The repository names from U2's lock — environment-INDEPENDENT by design.

    Read from the lock rather than the Terraform because that is where `ecr.tf` reads them
    from (`local.ecr_repository_candidates`), so this tracks the same source of truth.
    """
    lock = (
        CONTROL_PLANE_DIR.parents[1] / "releases" / "superplane.lock.yaml"
    ).read_text(encoding="utf-8")
    names = sorted(set(re.findall(r"ecr_repository:\s*(\S+)", lock)))
    if not names:  # pragma: no cover
        raise AssertionError("no ecr_repository entries found in superplane.lock.yaml")
    return names


def iam_role_arn(environment: str, account_id: str, role_name: str) -> str:
    return f"arn:aws:iam::{account_id}:role/{role_name}"


def ssm_parameter_arn(environment: str, account_id: str, region: str, name: str) -> str:
    # SSM parameter ARNs render the leading slash of the path as `parameter/adp/...`.
    return f"arn:aws:ssm:{region}:{account_id}:parameter{name}"


def ecr_repository_arn(account_id: str, region: str, repository: str) -> str:
    return f"arn:aws:ecr:{region}:{account_id}:repository/{repository}"
