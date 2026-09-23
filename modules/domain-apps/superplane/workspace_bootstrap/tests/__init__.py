"""Test package for workspace bootstrap — Issue #5533 (w6-10), EPIC #4910.

This file exists for the two reasons the sibling
`../../infra/account-factory/tests/__init__.py` records, and they both still apply
here:

## 1. It stops this directory's conftest from shadowing another suite's

Without an `__init__.py`, pytest's default prepend import mode names this
directory's `conftest.py` as the TOP-LEVEL module `conftest`. So does
`../../tests/conftest.py` and `../../infra/account-factory/tests/conftest.py`.
Whichever loads first claims `sys.modules["conftest"]` for the whole run, and the
module-wide lane in `superplane-domain-ci.yml` then resolves another suite's
`from conftest import ...` against the wrong file and fails collection for
unrelated tests. With this file present, pytest walks up to the first
non-package directory and names the modules `tests.conftest`,
`tests.test_target`, and so on.

### Why the parent directory is a package, and named with an underscore

An `__init__.py` HERE is necessary but not sufficient, and the sibling
`__init__.py` files overstate what it achieves. They describe `tests.conftest` as
if it were unique. It is not — it is unique only per parent directory, and
pytest stops its walk at the first directory without an `__init__.py`
regardless of whether that directory's name is a valid identifier. So
`infra/account-factory/tests/`, `src/superplane-api/tests/` and this suite all
resolve to `tests.conftest` on their own. Two of those colliding aborts
collection for the WHOLE lane with `ImportPathMismatchError` — a harder failure
than the shadowing the `__init__.py` was added to prevent, because it takes
every unrelated suite down with it.

The existing pair escapes only because `../conftest.py` sets
`collect_ignore = ["src"]`, so the `superplane-api` suite is never collected by
this lane. That is a collection boundary added for an unrelated reason
(transferred dependencies), not a defence against this. Hyphenating a directory
does NOT help: it was tried here first and changed nothing, because the walk
had already stopped.

The fix that works is `../spike/__init__.py`'s: make the PARENT a package, so
the walk continues past it and the name becomes
`workspace_bootstrap.tests.conftest`. That requires the parent to be a valid
identifier, which is why this directory is `workspace_bootstrap/` and not
`workspace-bootstrap/` or `bootstrap/`.

The underscore name also has to be one that is free repo-wide, because making
the directory a package publishes it as a top-level importable name.
`bootstrap` was not free: `platform/scripts/bootstrap.py` is already imported
under exactly that name by `platform/scripts/tests/test_release.py`, so
`bootstrap/__init__.py` would have created a second, order-dependent collision
across lanes. `workspace_bootstrap` is unused anywhere in the repo.

## 2. It puts the module directory on `sys.path` before the conftest runs

The package under test is `superplane_bootstrap`, inside `workspace_bootstrap/`,
and ships no installed distribution. A package's `__init__.py` always executes
before any of its submodules, so doing the insert here rather than in
`conftest.py` lets `conftest.py` keep every import at the top of the file — no
import-not-at-top and no `E402` suppression.

`contracts/` is added too: the bootstrap package's own modules import nothing from
it (they duck-type the binding, see `superplane_bootstrap/target.py`), but the
tests construct real `OperationBinding` and `CredentialReference` objects. Using
the genuine contract types is the point — a test that faked the binding could not
show that this package reads the same fields the contract actually publishes.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `tests/` -> `workspace_bootstrap/`, the dir that CONTAINS `superplane_bootstrap`.
MODULE_DIR = Path(__file__).resolve().parent.parent

# `workspace_bootstrap/` -> `superplane/` -> `contracts/`, which contains the importable
# `superplane_contracts` package (see ../../tests/_contracts_path.py).
CONTRACTS_DIR = MODULE_DIR.parent / "contracts"

for path in (MODULE_DIR, CONTRACTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
