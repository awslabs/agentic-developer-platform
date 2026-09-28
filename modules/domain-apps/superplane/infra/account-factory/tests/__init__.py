"""Test package for the adopted Account Factory — Issue #5530 (w6-07).

This file does two things, both about how the module-wide lane collects tests.

## 1. It stops this directory's conftest from shadowing another suite's

Without this file, pytest's default (prepend) import mode names this directory's
`conftest.py` as the TOP-LEVEL module `conftest`, because the directory is not a package.
`modules/domain-apps/superplane/tests/conftest.py` would be named the same way, and whichever
loads first claims `sys.modules["conftest"]` for the whole run. This directory sorts earlier,
so the module-wide lane in superplane-domain-ci.yml resolved that module's
`from conftest import OBSERVED_AT, W1` to THIS package's conftest and failed collection for
six unrelated observation test files.

With `__init__.py` here, pytest walks up to the first non-package directory (`account-factory`,
which has none) and names the modules `tests.conftest`, `tests.test_modes`, and so on — so
nothing in this directory can shadow another suite's top-level module. The sibling
`infra/control-plane/tests/` has no conftest, which is why the collision appeared only when
this suite was added.

The hyphen in `account-factory` is not part of any module name: it is the base directory the
dotted name is computed FROM, not a component of it.

## 2. It puts the module directory on `sys.path` before `tests.conftest` is imported

The package under test is `account_factory`, inside the hyphenated `account-factory/`
directory — which is therefore not importable as a package itself, and ships no installed
distribution. Doing the `sys.path` insert HERE rather than in `conftest.py` is what lets
`conftest.py` keep every import at the top of the file: a package's `__init__.py` always
executes before any of its submodules, so `account_factory` is importable by the time
`tests.conftest` runs. Previously `conftest.py` imported after the insert and needed an
`E402` suppression, which the repo's pinned ruff (0.9.6) flagged and a newer ruff called an
unused directive — a contradiction that disappears once the ordering requirement is gone.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `tests/` -> `account-factory/`, the directory that CONTAINS the `account_factory` package.
MODULE_DIR = Path(__file__).resolve().parent.parent

if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))
