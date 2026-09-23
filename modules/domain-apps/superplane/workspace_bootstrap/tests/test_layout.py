"""The module-wide lane can still collect every suite — Issue #5533 (w6-10), AC-03.

AC-03 requires the affected domain checks to run on the final PR head. This file
guards the precondition for that: the module-wide step in
`.github/workflows/superplane-domain-ci.yml` runs

    python3 -m pytest modules/domain-apps/superplane/ -m "not superplane_live"

as ONE pytest invocation from the repository root. A test-package layout mistake
there is not a local failure — pytest aborts during COLLECTION and reports zero
results for every suite in the module, so "the checks ran and passed" and "the checks
never ran" become indistinguishable from the lane's exit code alone.

This suite caused exactly that, and the first fix for it was wrong, so the rule is
worth stating precisely. pytest's prepend import mode names a file by walking UP from
it while each directory contains an `__init__.py`, then treating the first directory
without one as the root. Two consequences the sibling `tests/__init__.py` files get
wrong:

- An `__init__.py` in `tests/` alone does not produce a unique name. It produces
  `tests.conftest`, which is unique only among suites that do not share it — and
  `infra/account-factory/tests/`, `src/superplane-api/tests/` and this suite all
  resolve to `tests.conftest`. Any two of them collected together abort the lane.
- Whether a directory's name is a valid Python identifier is irrelevant to where the
  walk stops. Renaming this directory to `workspace-bootstrap/` was tried first and
  changed nothing, because the walk had already stopped at it.

The existing pair escapes only because `../conftest.py` sets
`collect_ignore = ["src"]` for an unrelated reason (the transferred components carry
their own dependency set), so `superplane-api`'s suite is never collected here. That
is luck, not a defence, which is why this is a test and not a comment: the next story
to add a test package gets a named failure explaining the rule, rather than a
collection abort surfacing in somebody else's suite.

Deliberately checks the WHOLE module, not just this directory. The defect is a
collision — a property of a pair of suites — so a test scoped to its own suite could
not have caught it. `workspace_bootstrap/tests/` was correct in isolation both before
and after the bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# pytest's own resolver, not a local reimplementation of it. Asserting against a
# hand-rolled model of the naming rule is what hid this defect the first time: the
# model said hyphenating the directory would change the name, and it did not.
_pathlib = pytest.importorskip("_pytest.pathlib")
resolve_pkg_root_and_module_name = _pathlib.resolve_pkg_root_and_module_name

MODULE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = MODULE_ROOT.parents[2]

# Collected by their own lanes with their own toolchains, so the module-wide
# invocation never reaches them — `../conftest.py` sets `collect_ignore = ["src"]`.
# Listed here so this test reflects what the lane actually collects; if that exclusion
# is ever lifted, the pre-existing `tests.conftest` collision between `account-factory`
# and `superplane-api` becomes real and this test reports it.
EXCLUDED_TREES = ("src",)


def _collected_conftests() -> list[Path]:
    return sorted(
        conftest
        for conftest in MODULE_ROOT.rglob("tests/conftest.py")
        if conftest.relative_to(MODULE_ROOT).parts[0] not in EXCLUDED_TREES
    )


def _package_module_name(path: Path) -> str | None:
    """The dotted module name pytest gives this file, or None if it gets a bare one.

    `resolve_pkg_root_and_module_name` raises for a file whose directory has no
    `__init__.py`. Those files are imported under their bare stem as TOP-LEVEL
    modules, which is a genuine hazard but a DIFFERENT one — see
    `test_the_known_bare_conftest_shadowing_is_unchanged` below. Only packaged suites
    can produce the `ImportPathMismatchError` that aborts collection outright, so they
    are what this module asserts on.
    """
    try:
        return resolve_pkg_root_and_module_name(
            path, consider_namespace_packages=False
        )[1]
    except _pathlib.CouldNotResolvePathError:
        return None


def test_no_two_packaged_test_suites_resolve_to_the_same_conftest_module():
    """The regression. A duplicate dotted name aborts collection module-wide.

    This is the failure this story introduced and then fixed: two suites that are both
    packages and both resolve to `tests.conftest` make pytest raise
    `ImportPathMismatchError` during collection, which reports zero results for every
    suite in the module rather than failing just the guilty pair.

    To fix a collision, give the suite a unique dotted name by making its PARENT
    directory a package — `../spike/__init__.py` is the precedent. The parent's name
    must therefore be a valid identifier, and must also be free repo-wide, since
    making it a package publishes it as a top-level importable name. Renaming the
    directory alone does not work; that was tried first here.
    """
    packaged: dict[str, list[str]] = {}
    for conftest in _collected_conftests():
        name = _package_module_name(conftest)
        if name is None:
            continue
        packaged.setdefault(name, []).append(str(conftest.relative_to(MODULE_ROOT)))

    assert packaged, (
        "expected at least one packaged test suite; if this became empty, this test "
        "is no longer checking anything"
    )

    collisions = {name: paths for name, paths in packaged.items() if len(paths) > 1}
    assert collisions == {}, (
        "two or more packaged test suites resolve to the same pytest module name, so "
        "the module-wide lane will abort with ImportPathMismatchError and report zero "
        f"results for EVERY suite in the module: {collisions}"
    )


def test_the_known_bare_conftest_shadowing_is_unchanged():
    """A pre-existing hazard, pinned rather than fixed — it is not this story's to fix.

    `tests/` and `tools/superplane-mcp/tests/` are both non-packages, so both of their
    `conftest.py` files are imported as the top-level module `conftest`, and whichever
    loads first claims `sys.modules["conftest"]` for the run. This does NOT abort the
    lane today only because `tests/` sorts first and is therefore the one that wins,
    and it is the one whose tests do `from conftest import ...`. Reverse the order —
    by running the MCP suite first, as
    `python3 -m pytest tools/... tests/` does — and six observation test files fail to
    import.

    That is latent and order-dependent, and fixing it means editing suites this story
    does not own, so it is recorded here instead: the assertion pins the known pair so
    that a THIRD bare conftest, or a change to this one, is a visible, explained
    failure rather than a mysterious import error in somebody else's suite.

    `superplane-mcp` has its own CI step that runs it from its own directory, where no
    other conftest is in scope, so the dedicated lane is unaffected either way.
    """
    bare = sorted(
        str(conftest.relative_to(MODULE_ROOT))
        for conftest in _collected_conftests()
        if _package_module_name(conftest) is None
    )

    assert bare == ["tests/conftest.py", "tools/superplane-mcp/tests/conftest.py"], (
        "the set of non-packaged conftest files in this module changed. Each one is "
        "imported as the top-level module `conftest`, so they shadow each other in an "
        "order-dependent way. A new suite should carry a `tests/__init__.py` AND a "
        "package parent (see this suite), not join this set. Found: " + repr(bare)
    )


def test_this_suites_conftest_is_namespaced_under_its_own_package():
    """The specific property that keeps this suite from colliding.

    Asserted by name as well as by uniqueness, because the test above would also pass
    if this suite were simply deleted — and a prefixed name is the thing a future edit
    could remove by dropping `workspace_bootstrap/__init__.py` without touching any
    test file.
    """
    name = _package_module_name(Path(__file__).resolve().parent / "conftest.py")

    assert name == "workspace_bootstrap.tests.conftest", (
        f"this suite's conftest resolves to {name!r}; it must be namespaced under "
        "workspace_bootstrap, which requires workspace_bootstrap/__init__.py to exist"
    )


def test_the_package_name_this_suite_publishes_is_not_used_elsewhere():
    """Making the parent a package publishes a top-level importable name.

    `bootstrap` was the obvious name for this directory and is NOT free:
    `platform/scripts/bootstrap.py` is imported under exactly that name by
    `platform/scripts/tests/test_release.py`, so `bootstrap/__init__.py` would have
    traded this collision for an order-dependent one across lanes. This checks the
    name actually chosen is still unique, since such a conflict would otherwise
    surface in an unrelated lane rather than here.
    """
    package_dir = Path(__file__).resolve().parents[1]
    name = package_dir.name
    assert (package_dir / "__init__.py").is_file(), (
        f"{name}/__init__.py is what namespaces this suite; without it the conftest "
        "collides with the sibling suites'"
    )

    clashes = [
        str(candidate.relative_to(REPO_ROOT))
        for candidate in REPO_ROOT.rglob(f"{name}.py")
        if ".git" not in candidate.parts
    ]
    clashes += [
        str(candidate.relative_to(REPO_ROOT))
        for candidate in REPO_ROOT.rglob(f"{name}/__init__.py")
        if ".git" not in candidate.parts and candidate.parent != package_dir
    ]

    assert clashes == [], (
        f"{name!r} is published as a top-level importable package by this directory, "
        f"but the same name is also importable from: {clashes}"
    )
