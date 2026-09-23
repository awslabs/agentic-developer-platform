"""`python -m superplane_bootstrap` — see `cli.py` for the subcommands and exit codes.

Issue #5533 (w6-10), EPIC #4910. Separate from `cli.py` for the same reason
`installation/` keeps them separate: the CLI is importable and testable as a function
(`cli.main(argv)` returns an exit code), and this file is the only place that turns one
into a process exit. A test can therefore exercise every subcommand without a subprocess
and without `SystemExit`.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
