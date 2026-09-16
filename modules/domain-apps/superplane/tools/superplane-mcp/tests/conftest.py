"""Test imports for the superplane-mcp tool surface.

The directory `superplane-mcp/` is fixed by the design note's layout (§3 line 155)
and by U1's README, which other units reference — so the path keeps its hyphen.
A hyphen is not a legal Python identifier, so the importable package lives one
level inside it as `superplane_mcp/`, and this file puts that on `sys.path`.

Two things this arrangement buys, both learned the hard way:

* `import superplane_mcp` works normally, so no test or future consumer needs
  import gymnastics.
* pytest does not try to collect the package's own `__init__.py` as a test
  module. When `__init__.py` sat directly in the hyphenated directory, pytest
  treated that directory as the rootdir package and attempted to import
  `__init__` as a top-level module, which fails on its relative imports with
  "attempted relative import with no known parent package" — 48 collection
  errors that had nothing to do with the tests themselves.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The directory holding the importable `superplane_mcp` package.
PACKAGE_PARENT = Path(__file__).resolve().parent.parent

if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))
