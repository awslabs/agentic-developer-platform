"""Offline lifecycle tests; cloud transport is doubled and PostgreSQL is real.

## Why `harness_jobs` is put on the path HERE and not in the package under test

`workspace_provisioning` composes its retirement plan against a structural copy of the
shared descriptor contract (`execution_contract.py`), because
`src/superplane-api/tests/test_workspaces.py:908` asserts `harness_jobs` is not
importable from this module's interpreter and the domain CI lane installs no such
distribution. That keeps production honest but leaves the copy checked by nothing.

So the authoritative package is imported by the **tests**, from its source directory,
and the drift test compares the two definition by definition. This is the same split
`infra/account-provisioning/account_provisioning_tests/__init__.py` makes, for the same
stated reason: the class of defect worth catching lives exactly at this boundary — a
copy that has drifted composes plans the real admission layer refuses, and a suite
using only the copy stays green while the live path fails.

Doing it in this file rather than in `conftest.py` matters. A `sys.path` edit at
conftest time runs after pytest has already begun importing test modules in some
invocations, and the previous revision of this suite imported the authoritative module
directly with no path setup at all — it collected only because a sibling test package
earlier in the alphabet (`infra/account-provisioning/`) happened to perform this same
insertion first. That made collection order load-bearing: running this directory alone,
or before that sibling, failed with `ModuleNotFoundError`. A package `__init__` is
imported before any module inside it, so the root is present no matter which subset of
the suite is selected or in what order.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `tests/` -> `workspace_provisioning/` -> `superplane/` -> `domain-apps/` -> `modules/`,
# which contains `harness/jobs/` (the directory holding the `harness_jobs` package).
HARNESS_JOBS_DIR = Path(__file__).resolve().parents[4] / "harness" / "jobs"

if str(HARNESS_JOBS_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESS_JOBS_DIR))

# Preview deliberately uses the real mode validator. Keep collection independent
# of whether the Account Factory's own tests have already been imported.
ACCOUNT_FACTORY_DIR = Path(__file__).resolve().parents[2] / "infra" / "account-factory"
if str(ACCOUNT_FACTORY_DIR) not in sys.path:
    sys.path.insert(0, str(ACCOUNT_FACTORY_DIR))
