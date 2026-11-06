#!/usr/bin/env python3
"""Inventory stored legacy bindings; output is private and never grants apply authority.

DATABASE_URL must address the selected gateway PostgreSQL instance. Supply an
explicit tenant and a private output path. This dry run never changes the DB.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.orchestration.legacy_binding_migration import inventory_legacy_bindings


def rollback_diagnostics(inventory: list[dict], candidate_reader: str) -> dict:
    if candidate_reader not in {"compatible", "below-floor", "unknown"}:
        raise ValueError("candidate reader must be compatible, below-floor or unknown")
    pending = sorted({action_id for flow in inventory for node in flow["nodes"] for action_id in node["pending_action_ids"]})
    publications = sorted({publication_id for flow in inventory for publication_id in flow.get("pending_publication_ids", [])})
    unresolved = sorted({node["node_id"] for flow in inventory for node in flow["nodes"] if node["classification"] == "legacy_binding_unresolved"})
    return {
        "reader_floor": "bound-repository-v1",
        "candidate_reader": candidate_reader,
        "writer_refusal": candidate_reader != "compatible",
        "pending_action_ids": pending,
        "pending_publication_ids": publications,
        "unresolved_node_ids": unresolved,
        "required_action": "verify_image_and_reconcile_pending_effects"
        if candidate_reader == "compatible"
        else "quiesce_gateway_and_tick_writers_keep_stopped",
    }


async def run(org_id: str, flow_id: str | None, output: Path, candidate_reader: str | None = None) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url or not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("DATABASE_URL must be a PostgreSQL asyncpg connection")
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            inventory = await inventory_legacy_bindings(session, org_id=org_id, flow_id=flow_id)
            await session.rollback()
    finally:
        await engine.dispose()
    diagnostics = rollback_diagnostics(inventory, candidate_reader) if candidate_reader else None
    write_private_inventory(output, inventory, diagnostics)
    if diagnostics and diagnostics["writer_refusal"]:
        raise ValueError("reader below or unverified against bound-repository-v1; keep gateway/tick writers quiesced")


def write_private_inventory(output: Path, inventory: list[dict], diagnostics: dict | None = None) -> None:
    data = json.dumps({"schema_version": 1, "inventory": inventory, **({"rollback": diagnostics} if diagnostics else {})}, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", dir=output.parent, prefix=".legacy-inventory-", delete=False) as destination:
        temporary_path = Path(destination.name)
        try:
            os.chmod(destination.fileno(), 0o600)
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
            os.link(temporary_path, output)
            directory = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--flow-id")
    parser.add_argument("--output", type=Path, required=True, help="new private file; existing files are not overwritten")
    parser.add_argument(
        "--candidate-reader",
        choices=("compatible", "below-floor", "unknown"),
        help="assessed image compatibility; unknown and below-floor refuse writer resumption",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args.org_id, args.flow_id, args.output, args.candidate_reader))
    except ValueError as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
