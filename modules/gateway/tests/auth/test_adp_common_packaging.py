"""`libs/python/adp-common` packaging and its name collision (issue #5044).

U9 is this library's first consumer, so making it packageable is part of the
story. The non-obvious part is the *name*.

``adp_common`` was already taken by ``modules/gateway/cli/adp_common.py`` — a
flat single-module CLI helper that eight files import as ``import adp_common``
after prepending ``cli/`` to ``sys.path``. A flat module and a package cannot
share a top-level name: whichever ``sys.path`` entry comes first wins, and when
the flat module wins, every ``adp_common.<submodule>`` import raises
``ModuleNotFoundError: ... 'adp_common' is not a package``.

So publishing the library as ``adp_common`` would have broken ``adp login`` in
any environment holding both. The import package is ``adp_platform_common``; the
distribution name stays ``adp-common``.

This file is the drift guard. Without it the next person to touch the library
renames the package back to the obvious name, CI passes (the CLI tests and the
library tests never share a process on the same path ordering by accident), and
the breakage surfaces in a built image.
"""

from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIB_ROOT = _REPO_ROOT / "libs" / "python" / "adp-common"
_LIB_SRC = _LIB_ROOT / "src"
_CLI_HELPER = _REPO_ROOT / "modules" / "gateway" / "cli" / "adp_common.py"


def _pyproject() -> dict:
    return tomllib.loads((_LIB_ROOT / "pyproject.toml").read_text())


# --- the collision is real, not hypothetical --------------------------------


def test_the_cli_helper_occupies_the_flat_adp_common_name():
    """The precondition for everything below. A file, not a package."""
    assert _CLI_HELPER.is_file()
    assert not (_CLI_HELPER.parent / "adp_common" / "__init__.py").exists()


def test_the_cli_helper_is_imported_as_a_top_level_module_by_real_callers():
    """Several shipped files depend on the flat name resolving to that module."""
    callers = [
        _REPO_ROOT / "modules" / "gateway" / "cli" / "adp-admin.py",
        _REPO_ROOT / "modules" / "gateway" / "cli" / "adp-aws.py",
        _REPO_ROOT / "modules" / "gateway" / "cli" / "adp-superplane.py",
    ]

    for caller in callers:
        assert "import adp_common" in caller.read_text(), caller


def test_a_flat_module_shadows_a_package_of_the_same_name(tmp_path, monkeypatch):
    """Demonstrates the failure that dictated the rename.

    Built from scratch in a temp directory rather than against the real trees, so
    it stays a statement about Python's import system — it cannot start passing
    because someone reordered the repository's own ``sys.path``.
    """
    flat_dir = tmp_path / "flat"
    flat_dir.mkdir()
    (flat_dir / "collide_demo.py").write_text("MARKER = 'flat-module'\n")

    pkg_dir = tmp_path / "pkg" / "collide_demo"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("MARKER = 'package'\n")
    (pkg_dir / "logging.py").write_text("VALUE = 1\n")

    # Flat module first, exactly as tests/cli/ arranges it with `cli/`.
    monkeypatch.syspath_prepend(str(tmp_path / "pkg"))
    monkeypatch.syspath_prepend(str(flat_dir))
    for name in [n for n in sys.modules if n.startswith("collide_demo")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    module = importlib.import_module("collide_demo")
    assert module.MARKER == "flat-module"

    with pytest.raises(ModuleNotFoundError) as failure:
        importlib.import_module("collide_demo.logging")

    assert "is not a package" in str(failure.value)


# --- the library avoids the collision ---------------------------------------


def test_the_library_does_not_use_the_contested_name():
    assert (_LIB_SRC / "adp_platform_common" / "__init__.py").is_file()
    assert not (_LIB_SRC / "adp_common").exists()


def test_the_distribution_name_is_unchanged():
    """Only the import name moved. `adp-common` is referenced by CI paths."""
    assert _pyproject()["project"]["name"] == "adp-common"


def test_the_wheel_ships_the_renamed_package():
    """Packaging is explicit: hatchling would otherwise guess from the dist name.

    This is the actual "make it packageable" assertion. Without the explicit
    ``packages`` entry, a build of a distribution named ``adp-common`` looks for
    ``src/adp_common`` and produces an empty or failed wheel.
    """
    packages = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]

    assert packages == ["src/adp_platform_common"]


def test_the_import_package_and_its_modules_are_importable():
    """The property the collision denied: submodules resolve."""
    package = importlib.import_module("adp_platform_common")
    domain_auth = importlib.import_module("superplane_auth.policy")

    assert Path(package.__file__).parent.name == "adp_platform_common"
    assert domain_auth.TRUSTED_VALIDATION_PATH == "gateway.cognito_jwt"


def test_the_pre_existing_logging_module_moved_with_the_package():
    """The library's one prior module must survive the rename.

    It had zero consumers, which is exactly why nothing else would have caught a
    move that broke it. Asserted on the file and its contents rather than by
    importing it, because it needs ``structlog`` — see the next test.
    """
    moved = _LIB_SRC / "adp_platform_common" / "logging.py"

    assert moved.is_file()
    assert "def configure_logging(" in moved.read_text()


def test_the_logging_module_needs_a_dependency_the_gateway_does_not_install():
    """Records why the test above does not simply import it.

    ``logging.py`` imports ``structlog``, which is declared by this library but is
    NOT in the gateway's dependencies, so importing it here raises
    ``ModuleNotFoundError``. That is a property of the library's own dependency
    set, not of the rename.

    It also explains a constraint on the policy module: since the library is used
    by consumers with different dependency sets, ``domain_auth`` is deliberately
    standard-library-only (asserted below), so consuming the policy never drags in
    structlog or boto3.
    """
    declared = _pyproject()["project"]["dependencies"]
    assert any(dep.startswith("structlog") for dep in declared)

    with pytest.raises(ModuleNotFoundError) as failure:
        importlib.import_module("adp_platform_common.logging")

    assert "structlog" in str(failure.value)


def test_the_policy_module_imports_only_the_standard_library():
    """A consumer gets the policy without the library's heavier dependencies.

    If ``domain_auth`` grew a ``pydantic``/``structlog``/``boto3`` import, every
    consumer of the authorization decision would inherit it — including ones that
    only need to make a decision, not log or call AWS. Kept as a source assertion
    because the gateway environment happens to have pydantic and boto3 installed,
    so an import-based check here would pass while breaking a leaner consumer.
    """
    source = (_REPO_ROOT / "modules/domain-apps/superplane/auth/superplane_auth/policy.py").read_text()
    import_lines = [line for line in source.splitlines() if line.startswith(("import ", "from ")) and "import" in line]

    third_party = [line for line in import_lines if any(pkg in line for pkg in ("pydantic", "structlog", "boto3", "fastapi", "jwt"))]

    assert third_party == []


def test_the_rename_is_explained_where_someone_would_undo_it():
    """A comment at the point of temptation, not only in a commit message."""
    text = (_LIB_ROOT / "pyproject.toml").read_text()

    assert "adp_common.py" in text
    assert "is not a package" in text


def test_the_policy_module_is_importable_without_gateway_internals():
    """Consumers of this policy do not run inside the gateway process.

    U7, U10, U16a and U17a consume this model from outside ``modules/gateway``, so
    an accidental ``from src.…`` import here would make the library unusable for
    every one of them while still passing this suite (which does have ``src`` on
    the path). Asserted against the source text for that reason.
    """
    source = (_REPO_ROOT / "modules/domain-apps/superplane/auth/superplane_auth/policy.py").read_text()

    assert "from src." not in source
    assert "import src" not in source
