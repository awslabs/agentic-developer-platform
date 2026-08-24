"""Guard: no Alembic revision id may exceed the ``alembic_version`` column width.

Issue #4123 (follow-up to #4070 ·A0).

Alembic records the applied revision in ``alembic_version.version_num``, which
is created as ``VARCHAR(32)`` by default. A revision id longer than 32 chars
runs ``upgrade()`` to completion and then overflows on the version write; under
transactional DDL the whole migration rolls back, leaving the schema one
revision behind live code. This exact failure shipped in 026/027 and was invisible
in CI because SQLite — the test database — does not enforce VARCHAR length, while
Postgres does.

This test parses every version file statically (no import side effects, no app
deps) and fails if any ``revision`` or ``down_revision`` string exceeds 32 chars.
It would have caught the pre-fix 026/027 ids (38 and 34 chars).
"""

import ast
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

# Alembic's default alembic_version.version_num width. Every prior migration id
# in this repo fits; keep it that way rather than widening a bookkeeping column.
MAX_REVISION_ID_LEN = 32


def _revision_ids(path: Path) -> dict[str, str | None]:
    """Return the module-level ``revision`` / ``down_revision`` string values."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: dict[str, str | None] = {}
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if not names & {"revision", "down_revision"}:
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for name in names & {"revision", "down_revision"}:
                found[name] = value.value
        elif isinstance(value, ast.Constant) and value.value is None:
            for name in names & {"revision", "down_revision"}:
                found[name] = None
    return found


_VERSION_FILES = sorted(p for p in MIGRATIONS_DIR.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("path", _VERSION_FILES, ids=lambda p: p.name)
def test_revision_ids_fit_alembic_version_column(path: Path) -> None:
    ids = _revision_ids(path)
    assert "revision" in ids, f"{path.name}: no module-level 'revision' found"
    for kind, value in ids.items():
        if value is None:
            continue
        assert len(value) <= MAX_REVISION_ID_LEN, (
            f"{path.name}: {kind} {value!r} is {len(value)} chars, exceeds "
            f"alembic_version.version_num VARCHAR({MAX_REVISION_ID_LEN}); "
            f"shorten it or the migration will roll back on Postgres"
        )
