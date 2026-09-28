"""The Terraform suites in this module actually run in CI — Issue #5532 (w6-09), AC-03.

## The failure this exists to prevent

I wrote four `.tftest.hcl` files for this module and they passed locally. They ran in no CI
lane at all. `superplane-infra-plan.yml` hardcoded its working directory to the control-plane
module, so `terraform test` never entered `infra/workspaces/` — and nothing failed, because a
suite that is never invoked reports nothing. A green PR would have shown "Terraform tests
(no AWS): success" while the tests this story was judged on sat unexecuted.

That is the same hazard the plan lane's own header calls out by name — "0 tests ran" and "all
tests passed" share exit code 0 — one level up. The lane guards against its OWN test count
falling to zero; nothing guarded against a whole root module having no leg in the lane.

AC-03 requires the affected checks to run on the final PR head. A test file that CI does not
execute does not satisfy that, so the wiring is part of the deliverable and is asserted here.

## Why the assertion is derived, not a list

The test below discovers every directory in `infra/` containing `.tftest.hcl` files and
requires each to appear as a matrix leg. A hand-written list of "the modules I expect" would
pass the moment someone adds a third root module and forgets the lane — which is exactly the
mistake being guarded, reintroduced one level up.

## Scope

This asserts the lane REFERENCES each root module, which is a static property of the workflow
file and checkable offline. It cannot prove GitHub Actions scheduled the job — only a run on
the PR head shows that, and that evidence belongs with AC-03 on the final head.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[6]
SUPERPLANE_INFRA = Path(__file__).resolve().parents[2]
PLAN_LANE = REPO_ROOT / ".github" / "workflows" / "superplane-infra-plan.yml"


def _root_modules_with_terraform_tests() -> list[Path]:
    """Directories under infra/ that own a Terraform test suite.

    Derived from the filesystem so a new root module is picked up without editing this file.
    """
    found = {
        path.parent.parent for path in SUPERPLANE_INFRA.glob("*/tests/*.tftest.hcl")
    }
    return sorted(found)


ROOT_MODULES = _root_modules_with_terraform_tests()


def test_the_discovery_found_root_modules() -> None:
    """Premise check: an empty discovery would make the parametrized test below vacuous.

    Without this, a glob that stopped matching collects zero cases and reports green — the
    precise failure mode this whole file exists to catch.
    """
    assert len(ROOT_MODULES) >= 2, (
        f"expected at least the control-plane and workspaces root modules to own "
        f"Terraform test suites, found {[p.name for p in ROOT_MODULES]}. If a module was "
        f"renamed, fix this file's glob rather than deleting the check."
    )
    names = {p.name for p in ROOT_MODULES}
    assert "workspaces" in names, (
        "this module's own Terraform suite was not discovered, so the check below would not "
        "cover the module it was written for."
    )


def test_plan_lane_exists() -> None:
    assert PLAN_LANE.exists(), (
        f"{PLAN_LANE} is missing. That lane is what executes every root module's "
        f"`.tftest.hcl` files; without it this module's Terraform tests run nowhere, which "
        f"AC-03 does not permit. If the lane was renamed, update this path."
    )


def _matrix_dirs() -> frozenset[str]:
    """The `dir:` values of the plan lane's test matrix.

    Parsed specifically rather than by substring-searching the whole file. A plain
    `path in lane_text` check is what I wrote first, and a mutation test caught it being
    vacuous: every root module's path ALSO appears in the lane's `on.pull_request.paths`
    list, so deleting a matrix leg left the substring present and the assertion green —
    the check passed while the suite it was protecting had stopped running.

    Regex rather than a YAML parse because PyYAML is not a declared dependency of this
    module, and a test that asserts CI coverage should not be the thing that adds one.
    """
    lane = PLAN_LANE.read_text()
    include = re.search(
        r"^\s*matrix:\s*\n\s*include:\s*\n(.*?)(?=^\s{0,4}\S)",
        lane,
        re.MULTILINE | re.DOTALL,
    )
    assert include, (
        f"could not locate the `matrix: include:` block in {PLAN_LANE.name}. The lane's "
        f"shape changed; fix this parser rather than dropping the check, since its whole "
        f"purpose is noticing when a root module's tests stop running."
    )
    return frozenset(
        re.findall(r"^\s*dir:\s*(\S+)\s*$", include.group(1), re.MULTILINE)
    )


MATRIX_DIRS = _matrix_dirs()


def test_the_matrix_parser_found_legs() -> None:
    """Premise check: an empty parse would make the coverage assertion vacuous."""
    assert len(MATRIX_DIRS) >= 2, (
        f"parsed {sorted(MATRIX_DIRS)} from the plan lane's matrix, expected at least two "
        f"root modules. An empty or partial parse makes the coverage test below pass for "
        f"the wrong reason."
    )


@pytest.mark.parametrize(
    "root_module", ROOT_MODULES, ids=[p.name for p in ROOT_MODULES]
)
def test_every_root_module_runs_in_ci(root_module: Path) -> None:
    """Each root module owning Terraform tests must have a matrix leg in the plan lane."""
    relative = root_module.relative_to(REPO_ROOT).as_posix()
    suite_count = len(list(root_module.glob("tests/*.tftest.hcl")))

    assert relative in MATRIX_DIRS, (
        f"`{relative}` owns {suite_count} .tftest.hcl file(s) that NO CI lane executes.\n\n"
        f"{PLAN_LANE.name} runs `terraform test` once per matrix leg, so a root module "
        f"absent from the matrix has its entire suite silently skipped — the lane still "
        f"reports success, because a suite that is never invoked reports nothing.\n\n"
        f"Legs currently present: {sorted(MATRIX_DIRS)}\n\n"
        f"Add a leg to the `tests` job:\n"
        f"    - name: {root_module.name}\n"
        f"      dir: {relative}\n\n"
        f"and add `{relative}/**` to the lane's `on.pull_request.paths` so a change to the "
        f"module can trigger the lane that checks it."
    )


@pytest.mark.parametrize(
    "root_module", ROOT_MODULES, ids=[p.name for p in ROOT_MODULES]
)
def test_every_root_module_can_trigger_the_lane(root_module: Path) -> None:
    """A matrix leg is useless if edits to the module never start the workflow.

    Separate from the test above because the two fail independently: a leg with no path
    trigger runs only when something ELSE in the lane's trigger set changes, which looks
    like working CI until the day a workspaces-only PR arrives and no lane runs at all.
    """
    lane = PLAN_LANE.read_text()
    relative = root_module.relative_to(REPO_ROOT).as_posix()

    # The paths list is quoted globs, e.g. 'modules/.../workspaces/**'. Match the glob
    # rather than the bare path so a `dir:` reference alone cannot satisfy this.
    assert re.search(rf"['\"]?{re.escape(relative)}/\*\*", lane), (
        f"`{relative}/**` is not in {PLAN_LANE.name}'s `on.pull_request.paths`.\n\n"
        f"A pull request touching only this module would then start no run of the lane that "
        f"executes its tests. The matrix leg would exist and never be scheduled, which "
        f"presents as passing CI rather than as missing CI."
    )


# ---------------------------------------------------------------------------
# The suite must be LOADABLE by the Terraform version CI pins
#
# A file that fails to parse is not reported as a failing test. `terraform init` rejects it and
# every run in it is reported as NOT EXECUTED — the same "reports nothing" shape as a missing
# matrix leg, which is why this guard lives in this file.
#
# It is not hypothetical. `kms_key_policy.tftest.hcl` used `override_resource { override_during
# = plan }`, which does not exist in Terraform 1.9.8 — the version the plan lane pins — so run
# 35508993241 failed during init with `Unsupported argument` and the whole file's five runs never
# ran. It passed locally, where a newer Terraform is installed. The lane's own test-count guard
# caught that something was wrong; nothing explained why, and "a newer version accepts it" is not
# a property the gate has.
#
# Scope, stated honestly: this is a targeted check for the constructs that have actually bitten
# this module, not a Terraform parser. It cannot prove 1.9.8 accepts an arbitrary new file — only
# running `terraform test` under the pinned version does that, and that is what the lane does.
# Its value is naming the version cause at edit time instead of leaving a future author to
# rediscover it from a CI init error.
# ---------------------------------------------------------------------------
PINNED_TERRAFORM_INCOMPATIBILITIES: tuple[tuple[str, str], ...] = (
    (
        r"override_during\s*=",
        "`override_during` was added after Terraform 1.9.8, which the plan lane pins. On 1.9.8 "
        "this is an `Unsupported argument` error during `terraform init`, so the ENTIRE FILE "
        "fails to load and all of its runs are reported as not executed rather than as "
        "failures.\n\n"
        "To make an AWS-assigned identifier known to an assertion, declare it as a "
        "`mock_resource` default on the mocked provider and give that one run `command = apply`. "
        "Verified on both 1.9.8 and 1.15.3: `mock_resource` defaults do not resolve under "
        "`command = plan` on either version, and do resolve under `command = apply`, which for a "
        "fully mocked provider makes no API call and needs no credential. See the note above "
        "`variables` in kms_key_policy.tftest.hcl.",
    ),
)


@pytest.mark.parametrize(
    "pattern,explanation",
    PINNED_TERRAFORM_INCOMPATIBILITIES,
    ids=[p for p, _ in PINNED_TERRAFORM_INCOMPATIBILITIES],
)
def test_no_suite_uses_syntax_the_pinned_terraform_cannot_load(
    pattern: str, explanation: str
) -> None:
    """No `.tftest.hcl` in this module may use a construct 1.9.8 rejects at init."""
    offenders: list[str] = []
    for root_module in ROOT_MODULES:
        for suite in sorted(root_module.glob("tests/*.tftest.hcl")):
            for number, line in enumerate(
                suite.read_text(encoding="utf-8").splitlines(), start=1
            ):
                # Skip comment lines: the explanation of why a construct is banned necessarily
                # names it, and that must not trip the check that bans it.
                if line.lstrip().startswith("#"):
                    continue
                if re.search(pattern, line):
                    offenders.append(
                        f"{suite.relative_to(REPO_ROOT).as_posix()}:{number}: {line.strip()}"
                    )

    assert not offenders, (
        "Terraform test file(s) use syntax the pinned Terraform cannot load:\n\n"
        + "".join(f"  {entry}\n" for entry in offenders)
        + f"\n{explanation}"
    )


def test_the_pinned_terraform_version_is_still_what_this_guard_assumes() -> None:
    """The guard above is version-specific, so the version it assumes must be checked.

    Without this, bumping the lane to a Terraform that supports `override_during` would leave a
    ban with no remaining reason, and a future author would have no way to tell the ban was
    obsolete rather than load-bearing.
    """
    lane = PLAN_LANE.read_text()
    versions = set(re.findall(r"terraform_version:\s*'([^']+)'", lane))

    assert versions == {"1.9.8"}, (
        f"{PLAN_LANE.name} now pins Terraform {sorted(versions)}, not 1.9.8.\n\n"
        f"`PINNED_TERRAFORM_INCOMPATIBILITIES` in this file lists constructs banned BECAUSE "
        f"1.9.8 rejects them at init. If the pin moved, re-check each entry against the new "
        f"version and delete the ones it supports — keeping a ban whose reason has expired is "
        f"how a test suite accumulates rules nobody can justify. If the pin moved to a version "
        f"that still lacks `override_during`, just update this expected value."
    )


def test_no_test_file_basename_collides_across_the_module() -> None:
    """Two test files with the same basename cannot both be collected.

    None of these test directories has an `__init__.py`, so pytest imports each file as a
    top-level module named after its basename. Two files sharing one basename produce
    "import file mismatch" and the SECOND is never collected — it reports nothing, which is
    indistinguishable from passing.

    This is the same hazard as a missing matrix leg, arriving by a different route, and it
    is not hypothetical: this module's backend test was first written as
    `test_backend_state_key.py`, colliding with `../control-plane/tests/`'s file of that
    name. The whole-module pytest run surfaced it as a collection ERROR, and the two
    directories' suites had not previously been run together in one invocation — so the
    collision was invisible in the per-directory runs used during development.

    Renaming to `test_workspace_backend_state_key.py` fixed it. This keeps the next one from
    being found by accident.
    """
    by_basename: dict[str, list[Path]] = {}
    for path in SUPERPLANE_INFRA.rglob("test_*.py"):
        if "__pycache__" in path.parts or "src" in path.parts:
            continue
        by_basename.setdefault(path.name, []).append(path)

    collisions = {name: sorted(p) for name, p in by_basename.items() if len(p) > 1}

    assert not collisions, (
        "test files share a basename, so pytest can collect only one of each pair:\n\n"
        + "".join(
            f"  {name}:\n" + "".join(f"    - {p}\n" for p in paths)
            for name, paths in sorted(collisions.items())
        )
        + "\nThese directories have no `__init__.py`, so each file is imported as a "
        "top-level module named after its basename. The second file to be collected raises "
        "'import file mismatch' and its tests never run — silently, since a suite that is "
        "not collected reports nothing.\n\n"
        "Give one of them a module-specific prefix (as "
        "test_workspace_backend_state_key.py does) rather than adding __init__.py, which "
        "would change how the existing lanes collect these directories."
    )
