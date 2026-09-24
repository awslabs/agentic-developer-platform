"""The platform AWS provider floor, checked as text — Issue #5831, EPIC #3959.

## Why this suite exists at all

`required_providers` is resolved by `terraform init`, before any test runs, and it is
not part of the plan graph — so no `terraform test` assertion can see it. Every
`.tftest.hcl` under `platform/infra` could pass while the root module silently pinned
an AWS provider that cannot read the live state. Reading the files as text is the only
mechanism that can check this. That is a weaker kind of test than a plan assertion and
it is used here deliberately, for a property that has no stronger form available.

It lives under `.github/scripts/tests/` rather than beside the Terraform because
`script-tests.yml` already runs on `platform/infra` path changes, on both `push` and
`pull_request`. A `.tftest.hcl` in the platform root would be merge-blocking only if a
new CI step were added to run it, and `platform/infra` root has no suite today.

## The defect being prevented

`~> 5.0` resolves to 5.100.0 — the final 5.x release, so the constraint is effectively
a pin. Provider 5.x publishes no *resource identity* schema for `aws_eks_addon`. Once
live state carries the identity fields a newer provider writes (`account_id`,
`addon_name`, `cluster_name`, `region`), Terraform can still plan but can no longer
serialise that state to JSON:

    Failed to marshal plan to json: error marshaling prior state:
    no resource identity schema found for aws_eks_addon.coredns

That breaks `terraform show -json <saved-plan>`, which is how a saved plan is inspected
before apply. The failure mode is quiet in the worst way: a plain `terraform plan` still
reports "no changes", so nothing looks wrong until the review step itself fails.

`MINIMUM_AWS_MAJOR_MINOR` is 6.42 because provider 6.42.0 added `aws_eks_addon`
`namespace_config`, a field already present in the dev state record. Verified with
Terraform 1.14.9 against a representative newer-provider state: 6.41.0 still fails with
the message above, 6.42.0 decodes and exports. Do not lower it.

## What is asserted

* every `required_providers` block under `platform/infra` that constrains `hashicorp/aws`
  admits nothing below 6.42.0 — in particular, no `~> 5.x` and no bare `>= 5.x`;
* the root and child constraints are byte-identical, so `terraform init` cannot fail on
  unsatisfiable constraints between them;
* the constraint stays below the next major, so a v7 release cannot arrive unreviewed;
* platform apply stays manual-only, so raising a provider floor cannot widen what applies
  automatically.

The suite fails if it finds no constrained files at all, rather than passing on an empty
glob — "nothing to check" and "everything checks out" must not share an exit code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PLATFORM_INFRA = REPO_ROOT / "platform" / "infra"
APPLY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "platform-infra-apply.yml"

# The release that added aws_eks_addon namespace_config. See the module docstring.
MINIMUM_AWS_MAJOR_MINOR = (6, 42)

# The single constraint both the root and the child module are expected to carry.
EXPECTED_CONSTRAINT = ">= 6.42.0, < 7.0.0"

AWS_SOURCE_RE = re.compile(r'source\s*=\s*"(?:registry\.terraform\.io/)?hashicorp/aws"')
VERSION_RE = re.compile(r'version\s*=\s*"([^"]+)"')
# Each comparator in a constraint string, e.g. ">= 6.42.0", "~> 5.0", "< 7.0.0".
COMPARATOR_RE = re.compile(r"(?P<op>~>|>=|<=|>|<|=)?\s*(?P<version>[0-9]+(?:\.[0-9]+)*)")


def _strip_comments(text: str) -> str:
    """Drop `#` and `//` comment lines.

    The provider blocks carry long comments that legitimately quote the defect being
    prevented — including the `~> 5.0` constraint and the 5.100.0 version. Asserting
    over raw text would flag the explanation of the rule as a violation of it.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "//")):
            continue
        out.append(line.split("#", 1)[0])
    return "\n".join(out)


def _aws_constraints(text: str) -> list[str]:
    """Return every version constraint attached to a `hashicorp/aws` source.

    `required_providers` entries put `source` and `version` in the same small braced
    block, so the version belonging to an aws source is the first `version = "..."`
    that follows it. Anchoring on the source line (rather than on the provider's local
    name) keeps this correct if a block is ever renamed or reordered.
    """
    constraints = []
    for source_match in AWS_SOURCE_RE.finditer(text):
        version_match = VERSION_RE.search(text, source_match.end())
        if version_match:
            constraints.append(version_match.group(1))
    return constraints


def _parsed(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _admits_below_floor(constraint: str, floor: tuple[int, int]) -> bool:
    """True if `constraint` permits any AWS provider release below `floor`.

    Only the lower bound can admit a too-old provider, so upper bounds (`<`, `<=`) are
    not what this inspects. A `~> X.Y` constraint pins the major and floats the last
    component, so its lowest admissible release is X.Y itself.
    """
    for match in COMPARATOR_RE.finditer(constraint):
        op = match.group("op") or "="
        if op in ("<", "<="):
            continue
        parts = _parsed(match.group("version"))
        lowest = (parts + (0, 0))[:2]
        if lowest < floor:
            return True
    return False


def _discover_constrained_files() -> list[Path]:
    if not PLATFORM_INFRA.is_dir():
        return []
    return sorted(
        path
        for path in PLATFORM_INFRA.rglob("*.tf")
        if AWS_SOURCE_RE.search(_strip_comments(path.read_text()))
    )


CONSTRAINED_FILES = _discover_constrained_files()


def test_at_least_one_constrained_file_exists() -> None:
    """Guard against this whole suite passing on an empty glob.

    Every parametrized test below is driven by the same discovery. If it silently
    returned nothing — a rename, a moved directory — those tests would collect zero
    cases and the suite would report green while checking nothing.
    """
    assert CONSTRAINED_FILES, (
        f"no .tf file under {PLATFORM_INFRA} constrains hashicorp/aws. Either the "
        "provider requirements moved (update this suite) or they were dropped, which "
        "would let Terraform resolve any AWS provider version at all."
    )


@pytest.mark.parametrize(
    "path", CONSTRAINED_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_aws_constraint_excludes_providers_that_cannot_decode_state(path: Path) -> None:
    """No platform AWS constraint may admit a provider below the state-decoding floor."""
    constraints = _aws_constraints(_strip_comments(path.read_text()))
    assert constraints, (
        f"{path.relative_to(REPO_ROOT)} names hashicorp/aws but declares no version "
        "constraint. An unconstrained provider can resolve to 5.x, which cannot "
        "serialise aws_eks_addon state to JSON."
    )

    floor = ".".join(str(part) for part in MINIMUM_AWS_MAJOR_MINOR)
    for constraint in constraints:
        assert not _admits_below_floor(constraint, MINIMUM_AWS_MAJOR_MINOR), (
            f"{path.relative_to(REPO_ROOT)} constrains hashicorp/aws to "
            f'"{constraint}", which admits a provider older than {floor}.0. Provider '
            "5.x publishes no resource identity schema for aws_eks_addon, so "
            "`terraform show -json <saved-plan>` fails with 'no resource identity "
            "schema found' and the saved plan cannot be inspected before apply. "
            f'Use "{EXPECTED_CONSTRAINT}" (see #5831).'
        )


@pytest.mark.parametrize(
    "path", CONSTRAINED_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_aws_constraint_stays_below_next_major(path: Path) -> None:
    """Every constraint must bound the major version.

    A provider major bump carries its own breaking changes and needs the same
    state-decoding review this issue performed. An open-ended `>= 6.42.0` would let
    a v7 release arrive on the next `init` without one.
    """
    for constraint in _aws_constraints(_strip_comments(path.read_text())):
        assert re.search(r"(<\s*7|~>\s*6)", constraint), (
            f"{path.relative_to(REPO_ROOT)} constrains hashicorp/aws to "
            f'"{constraint}", which does not bound the major version. AWS provider 7.x '
            "would be accepted on the next init without the state-decoding review "
            f'#5831 performed for 6.x. Use "{EXPECTED_CONSTRAINT}".'
        )


def test_root_and_child_constraints_are_identical() -> None:
    """Root and child modules must agree, or `terraform init` fails outright.

    Terraform intersects every constraint on a provider across the whole configuration.
    A child module left on `~> 5.0` while the root requires `>= 6.42.0` has an empty
    intersection, which fails init rather than degrading quietly.
    """
    found = {
        path: constraints
        for path in CONSTRAINED_FILES
        if (constraints := _aws_constraints(_strip_comments(path.read_text())))
    }
    distinct = {constraint for constraints in found.values() for constraint in constraints}

    assert distinct == {EXPECTED_CONSTRAINT}, (
        "every platform AWS constraint must be exactly "
        f'"{EXPECTED_CONSTRAINT}", but found {sorted(distinct)} across '
        f"{sorted(str(p.relative_to(REPO_ROOT)) for p in found)}. Divergent "
        "constraints either fail `terraform init` on an empty intersection or let a "
        "child module resolve a provider the root never reviewed."
    )


def test_platform_apply_remains_manual_only() -> None:
    """Raising a provider floor must not widen what applies automatically.

    #5831 requires that provider changes do not trigger broad automatic applies. The
    property that holds this is the apply workflow's trigger: `workflow_dispatch` with
    no `push`. Asserted here because it is the safety boundary a provider change is
    most likely to be blamed for crossing.
    """
    assert APPLY_WORKFLOW.is_file(), f"{APPLY_WORKFLOW} must exist."
    body = _strip_comments(APPLY_WORKFLOW.read_text())

    triggers = re.search(r"^on:\s*$(.*?)^\S", body + "\nX", re.MULTILINE | re.DOTALL)
    assert triggers, f"{APPLY_WORKFLOW.name} must declare an `on:` block."
    trigger_body = triggers.group(1)

    assert "workflow_dispatch" in trigger_body, (
        f"{APPLY_WORKFLOW.name} must keep its workflow_dispatch trigger; it is the "
        "only intended way to apply platform infrastructure."
    )
    for automatic in ("push:", "pull_request:", "schedule:"):
        assert not re.search(rf"^\s+{re.escape(automatic)}", trigger_body, re.MULTILINE), (
            f"{APPLY_WORKFLOW.name} gained an automatic `{automatic}` trigger. Platform "
            "apply is manual-only by design: a provider-constraint change must not be "
            "able to cause an unreviewed apply (#5831)."
        )
