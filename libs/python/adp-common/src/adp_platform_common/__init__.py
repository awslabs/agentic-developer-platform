"""ADP Common - Shared utilities for ADP modules.

Imported as ``adp_platform_common``, not ``adp_common``: the shorter name is a
flat CLI helper module (``modules/gateway/cli/adp_common.py``) that would shadow
this package wherever both are on ``sys.path``. See the note in
``pyproject.toml`` and the guard in
``modules/gateway/tests/auth/test_adp_common_packaging.py``.
"""

__version__ = "0.1.0"
