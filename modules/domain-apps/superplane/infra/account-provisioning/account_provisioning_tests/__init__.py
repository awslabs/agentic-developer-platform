"""Test package for the governed account-creation and bootstrap runners — #5531 (w6-08).

The unique package name prevents collisions with Account Factory's `tests.conftest`
during the domain-wide CI run. Naming both packages `tests` made their isolated runs
pass while combined collection failed. This initializer also establishes the import
roots before this package's conftest imports the implementations under test.

## The three roots, and why each is here

* `account-provisioning/` — the package under test. The directory is hyphenated, so it is
  not importable as a package itself and ships no installed distribution.
* `account-factory/` — the sibling decision layer this package consumes (`assess_attempt`,
  `bootstrap_plan`, `recovery_report`). A sibling directory rather than a dependency, for
  the reason `pyproject.toml` gives.
* `modules/harness/jobs/` — the authoritative durable-execution contract, **on the path for
  the tests only**.

That third one is the point of this file, so it is worth being explicit about what it does
and does not mean. The production package deliberately does NOT import `harness_jobs`: two
tests assert it is not importable from the API app and the provisioning adapter, because
composing the real facade is #5535's (w6-12) decision. Those tests check importability from
*those* apps' interpreters, and nothing here changes that.

But the whole class of defect this suite exists to catch lives exactly at that boundary. The
package carries a *copy* of `CallOutcome`, and the previous revision compared the copy
against the real thing with `is` — which never matches, so a successfully created account
read back as `UNKNOWN` with its id discarded. A test suite using only the local copy passes
while the live path loses accounts. So the real class is imported HERE, in the tests, and
driven through the real entry points. A drift test compares the two definitions member by
member.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `account_provisioning_tests/` -> `account-provisioning/`, which contains the `account_provisioning` package.
MODULE_DIR = Path(__file__).resolve().parent.parent
# `account-provisioning/` -> `infra/`, which contains the sibling `account-factory/`.
INFRA_DIR = MODULE_DIR.parent
ACCOUNT_FACTORY_DIR = INFRA_DIR / "account-factory"
# `infra/` -> `superplane/` -> `domain-apps/` -> `modules/`, which contains `harness/jobs/`.
MODULES_DIR = INFRA_DIR.parent.parent.parent
HARNESS_JOBS_DIR = MODULES_DIR / "harness" / "jobs"

for root in (MODULE_DIR, ACCOUNT_FACTORY_DIR, HARNESS_JOBS_DIR):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
