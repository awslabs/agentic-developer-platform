"""Make the domain module's own packages importable — Issue #5041 (U2), EPIC #4910.

`modules/domain-apps/superplane/` is not itself a Python package (no `__init__.py`), so
relative imports out of this directory have no parent package to climb into. The sibling
suite under `tests/acceptance/` sidesteps this by reading files as text; these suites need
to import `releases.resolve_lock`, because the point is to exercise the same code path the
build lanes call rather than a re-implementation of it.

Putting the module root on `sys.path` here keeps that one concern in one place, and matches
how `spike/tests/` ends up importing `spike.*` (pytest inserts the module root as the
basedir there because `spike/` carries `__init__.py`).
"""

from __future__ import annotations

import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]

if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))
