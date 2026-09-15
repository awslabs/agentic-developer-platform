#!/usr/bin/env python3
"""Read-only pre-upgrade audit for #4924; never repairs placements.

Uses the gateway's existing BG_DATABASE_URL / BG_RDS_* configuration and IAM/TLS
connection support. Run against the intended PostgreSQL database before deploying
an image containing revision 048. Default output contains counts only; --include-ids
adds bounded internal identifiers, never emails or credentials. Exit 0: full
audit clean; 1: inconsistent; 2: query/configuration/connection error; 3: explicit
--source-pointers-only diagnostic passed but full audit is incomplete.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATEWAY_ROOT))
_SPEC = importlib.util.spec_from_file_location("team_integrity_048", GATEWAY_ROOT / "alembic/versions/048_team_integrity_gate.py")
assert _SPEC is not None and _SPEC.loader is not None
CHECKPOINT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(CHECKPOINT)


async def collect(engine, *, include_ids: bool = False, detail_limit: int = 100, source_pointers_only: bool = False) -> dict:
    if engine.dialect.name != "postgresql":
        raise ValueError("operator audit requires PostgreSQL for a verified read-only snapshot")
    async with engine.connect() as connection, connection.begin():
        # Must precede all source reads (including schema inspection). Repeated
        # counts and optional details therefore describe the same snapshot.
        await connection.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        if (await connection.execute(sa.text("SHOW transaction_read_only"))).scalar_one() != "on":
            raise RuntimeError("database did not enter a read-only transaction")
        report = await connection.run_sync(
            lambda sync: CHECKPOINT.audit(sync, include_ids=include_ids, detail_limit=detail_limit, source_pointers_only=source_pointers_only)
        )
        report["transaction_read_only"] = True
        report["isolation"] = (await connection.execute(sa.text("SHOW transaction_isolation"))).scalar_one()
        report["observed_at"] = datetime.now(UTC).isoformat()
        report["alembic_revisions"] = await connection.run_sync(
            lambda sync: list(sync.execute(sa.text("SELECT version_num FROM alembic_version ORDER BY version_num")).scalars())
            if sa.inspect(sync).has_table("alembic_version")
            else []
        )
        return report


async def run(*, include_ids: bool = False, detail_limit: int = 100, source_pointers_only: bool = False) -> dict:
    from src.shared.database import get_engine

    engine = get_engine()
    try:
        return await collect(engine, include_ids=include_ids, detail_limit=detail_limit, source_pointers_only=source_pointers_only)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-ids", action="store_true", help="include internal IDs for operator placement review")
    parser.add_argument("--detail-limit", type=int, default=100, help="maximum rows per finding class (1-10000)")
    parser.add_argument("--source-pointers-only", action="store_true", help="limited pre-040 diagnostic; never returns full-audit success")
    args = parser.parse_args(argv)
    if not 1 <= args.detail_limit <= 10000:
        parser.error("--detail-limit must be between 1 and 10000")
    try:
        report = asyncio.run(run(include_ids=args.include_ids, detail_limit=args.detail_limit, source_pointers_only=args.source_pointers_only))
    except Exception as exc:
        # Database errors can embed URLs, parameters, and credentials. Report the
        # error class only, and fail closed without manufacturing zero counts.
        print(json.dumps({"status": "ERROR", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return {"CLEAN": 0, "INCONSISTENT": 1, "SOURCE_POINTERS_CLEAN": 3}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
