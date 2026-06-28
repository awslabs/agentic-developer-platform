"""Regression guard: knowledge_assets INSERTs must not use `:param::type` casts.

asyncpg (the gateway's async driver) raises PostgresSyntaxError on a named
bind parameter immediately followed by a `::` Postgres cast, e.g.
`:metadata::jsonb` — the `::` collides with asyncpg's parameter parsing and the
INSERT fails at runtime with "syntax error at or near \":\"". The portable form
is `CAST(:metadata AS jsonb)`.

The mocked FakeAsyncSession in the route tests never parses SQL, so this class
of bug ships green through the unit suite. This test inspects the raw SQL text
of the register/bulk INSERTs to block re-introduction. (#2213 follow-up.)
"""

import re
from pathlib import Path

# The colon-cast antipattern: a named bind (:word) immediately followed by ::type
BAD_PARAM_CAST = re.compile(r":[a-zA-Z_][a-zA-Z0-9_]*::[a-zA-Z]")

ROUTES = Path(__file__).resolve().parents[2] / "src" / "knowledge" / "routes.py"


def test_routes_has_no_param_colon_cast() -> None:
    """No `:param::type` cast in knowledge/routes.py — use CAST(:param AS type)."""
    src = ROUTES.read_text()
    offenders = BAD_PARAM_CAST.findall(src)
    assert not offenders, f"Found `:param::type` casts (break under asyncpg): {offenders}. Use CAST(:param AS type) instead."
