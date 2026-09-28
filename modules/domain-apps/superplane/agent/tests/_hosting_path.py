"""Puts the `superplane_hosting` package on `sys.path`. Import for side effect.

Issue #5050 (U5), EPIC #4910.

This follows the arrangement already proven in this module by
`tests/_contracts_path.py` and `tools/superplane-mcp/tests/conftest.py`. The domain module
has no packaging of its own — `superplane-domain-ci.yml` installs the *gateway's*
dependency set and then runs pytest over this source tree — so the importable package sits
one level inside its fixed-name directory as `hosting/superplane_hosting/`, and importing
this module puts that parent on the path.

Why one level inside: with `__init__.py` directly in `hosting/`, pytest treats that
directory as the rootdir package and tries to import `__init__` as a top-level module,
which fails on its relative imports. See `_contracts_path.py` for the same note.

Why a separate module rather than three lines in conftest: the path setup has to run
*before* `from superplane_hosting import ...`, and statements before an import make that
import an import-not-at-top. Making the setup an import expresses the ordering requirement
through import order rather than through a comment asking the reader not to move a line.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The directory holding the importable `superplane_hosting` package.
PACKAGE_PARENT = Path(__file__).resolve().parent.parent / "hosting"

if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))
