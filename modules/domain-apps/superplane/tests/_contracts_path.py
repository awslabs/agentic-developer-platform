"""Puts the `superplane_contracts` package on `sys.path`. Import for side effect.

Issue #5043 (U8), EPIC #4910.

`contracts/` is a path fixed by the design note's layout (§3 line 158) and by U1's
README, and the module has no packaging of its own — `superplane-domain-ci.yml`
installs the *gateway's* dependency set and then runs pytest over this module's
source tree. So the importable package lives one level inside as
`superplane_contracts/`, and importing this module puts its parent on the path.

This follows the arrangement proven in `tools/superplane-mcp/tests/conftest.py`,
whose docstring records why the package sits one level inside rather than at the
fixed-name directory level: with `__init__.py` directly in the outer directory,
pytest treats that directory as the rootdir package and tries to import
`__init__` as a top-level module, which fails on its relative imports.

## Why this is a separate module rather than three lines in conftest

Because path setup has to run *before* `from superplane_contracts import ...`, and
statements before an import make that import a module-level import-not-at-top.
Suppressing that with a `noqa` would be suppressing a real smell; making the setup
an import instead means conftest is two ordinary imports in a row, and the
ordering requirement is expressed by import order rather than by a comment asking
the reader not to move a line.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The directory holding the importable `superplane_contracts` package.
PACKAGE_PARENT = Path(__file__).resolve().parent.parent / "contracts"

if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))
