"""Migration tests for durable PMM-03 cycle/slot admission."""

import importlib.util
from pathlib import Path

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

MIGRATIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _load(filename):
    spec = importlib.util.spec_from_file_location(filename, MIGRATIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVIDENCE = _load("057_model_invocability_evidence.py")
ADMISSION = _load("058_model_probe_admission.py")


def _run(sync_conn, operation):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with Operations.context(MigrationContext.configure(sync_conn)):
        operation()


async def _engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(_run, EVIDENCE.upgrade)
        await connection.run_sync(_run, ADMISSION.upgrade)
    return engine


async def test_upgrade_creates_cycle_and_slot_constraints():
    engine = await _engine()
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda conn: sa_inspect(conn).get_table_names())
        slot_uniques = await connection.run_sync(lambda conn: sa_inspect(conn).get_unique_constraints("model_probe_slots"))
        slot_columns = await connection.run_sync(lambda conn: sa_inspect(conn).get_columns("model_probe_slots"))
        cycle_checks = await connection.run_sync(lambda conn: sa_inspect(conn).get_check_constraints("model_probe_cycles"))
    await engine.dispose()
    assert {"model_invocability_evidence", "model_probe_cycles", "model_probe_slots"} <= set(tables)
    assert "uq_model_probe_slot_candidate" in {constraint["name"] for constraint in slot_uniques}
    assert "lease_token_sha256" in {column["name"] for column in slot_columns}
    assert "ck_model_probe_cycle_spend" in {constraint["name"] for constraint in cycle_checks}


async def test_upgrade_downgrade_upgrade_round_trip():
    engine = await _engine()
    async with engine.begin() as connection:
        await connection.run_sync(_run, ADMISSION.downgrade)
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda conn: sa_inspect(conn).get_table_names())
    assert "model_probe_cycles" not in tables
    assert "model_probe_slots" not in tables
    assert "model_invocability_evidence" in tables
    async with engine.begin() as connection:
        await connection.run_sync(_run, ADMISSION.upgrade)
    await engine.dispose()


def test_revision_extends_057_as_the_single_head():
    assert ADMISSION.revision == "058_model_probe_admission"
    assert ADMISSION.down_revision == "057_model_invocability_evidence"
    siblings = []
    for path in MIGRATIONS.glob("*.py"):
        if path.name != "058_model_probe_admission.py" and f'down_revision = "{ADMISSION.down_revision}"' in path.read_text():
            siblings.append(path.name)
    assert siblings == []
