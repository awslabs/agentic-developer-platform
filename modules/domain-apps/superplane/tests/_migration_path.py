"""Makes `migration.*` importable, contracts and all. Import for side effect.

Issue #5061 (U19), EPIC #4910.

`migration/` imports two sibling trees: `superplane_contracts` (U11's frozen
contracts, which live one level inside `contracts/`) and `spike` (U12's recorded
baseline). Those resolve from two different directories:

* the module root, which `conftest.py` already puts on `sys.path` — that is how
  `spike.baseline_inventory` and `migration` itself resolve;
* `contracts/`, which holds the importable `superplane_contracts` package —
  `tests/_contracts_path.py` explains why the package sits one level inside rather
  than at the fixed-name directory level.

Both are needed *before* `from migration...`, because `migration/handover.py` imports
`superplane_contracts` at its own import time. Rather than have every migration test
import two path helpers in the right order, this module reuses `_contracts_path` and
adds the module root, so a test file has one import to make and no ordering to get
right.

Kept as a separate module for the reason `_contracts_path` gives: path setup must run
before the imports it enables, and statements before an import make that import an
import-not-at-top. Expressing the ordering as import order beats a `noqa` plus a
comment asking the reader not to reorder the lines.
"""

from __future__ import annotations

import sys
from pathlib import Path

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)

MODULE_ROOT = Path(__file__).resolve().parents[1]

if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))
