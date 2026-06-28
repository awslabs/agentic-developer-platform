"""Regression guard: SQL must not use `:param::type` casts (asyncpg-incompatible).

asyncpg (the gateway's async driver) raises PostgresSyntaxError on a named
bind parameter immediately followed by a `::` Postgres cast, e.g.
`:metadata::jsonb` — the `::` collides with asyncpg's parameter parsing and the
statement fails at runtime with "syntax error at or near \":\"". The portable
form is `CAST(:metadata AS jsonb)`.

The mocked FakeAsyncSession in the route tests never parses SQL, so this class
of bug ships green through the unit suite (it bit register #2272 and the
status-callback endpoint #2213). This test scans the gateway SQL surface to
block re-introduction anywhere, not just one file.
"""

import re
from pathlib import Path

# The colon-cast antipattern: a named bind (:word) immediately followed by ::type.
# Anchored on a leading boundary that excludes ARN-style "arn:aws:iam::..." (a
# digit/colon precedes the ::), matching only SQLAlchemy :named binds.
BAD_PARAM_CAST = re.compile(r"(?<![\w:]):[a-zA-Z_][a-zA-Z0-9_]*::[a-zA-Z]")

SRC = Path(__file__).resolve().parents[2] / "src"

# Files that issue raw SQL via SQLAlchemy text() with named binds.
SQL_FILES = [
    SRC / "knowledge" / "routes.py",
    SRC / "internal" / "status_callback_routes.py",
]


def test_no_param_colon_cast_in_sql_files() -> None:
    """No `:param::type` cast in any gateway SQL file — use CAST(:param AS type)."""
    offenders: dict[str, list[str]] = {}
    for f in SQL_FILES:
        if not f.exists():
            continue
        hits = BAD_PARAM_CAST.findall(f.read_text())
        if hits:
            offenders[str(f.relative_to(SRC))] = hits
    assert not offenders, f"Found `:param::type` casts (break under asyncpg): {offenders}. Use CAST(:param AS type) instead."
