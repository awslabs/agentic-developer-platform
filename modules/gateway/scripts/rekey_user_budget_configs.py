#!/usr/bin/env python3
"""Rekey mis-keyed `user` budget_configs rows onto the Cognito sub.

Issue #4511: Budget Management wrote `user`-scoped budgets keyed on the Cognito
*Username* (`GitHub_<github_id>` for GitHub-onboarded users) while enforcement and
the /api/me/budget read path key `user` entities by the Cognito *sub*. Rows keyed
any other way are inert — they render in Budget Management with a friendly
display name but never enforce and never accumulate spend.

This script repairs those rows. It is deliberately conservative:

  * **Dry-run by default.** Pass `--apply` to write.
  * **Never deletes anything.** Rows it cannot fix are reported and left alone.
    Unresolvable rows are still live UI state (`get_budgets_list` reads and
    display-names them), so deleting them out from under an operator who can see
    them is a worse surprise than an inert row — and it is unrecoverable.
  * **Never overwrites a live cap.** If the resolved sub already has a row for
    the same (org, entity_type, period), the existing sub-keyed row is the one
    actually enforcing. Both rows are reported for human reconciliation and
    nothing is changed. Silently raising or lowering a live spend cap as a
    side-effect of a cleanup script is not acceptable.
  * **Idempotent.** A second run reports zero changes.

**Ordering matters: deploy the code fix FIRST, then run this.** Reversed, the
still-broken UI writes fresh mis-keyed rows behind the repair.

Usage:
    # Dry-run (default) — prints per-row classification and counts:
    DATABASE_URL=postgresql+asyncpg://... python rekey_user_budget_configs.py

    # Apply the rekeys that are unambiguous:
    DATABASE_URL=postgresql+asyncpg://... python rekey_user_budget_configs.py --apply

    # Limit to one tenant:
    ... python rekey_user_budget_configs.py --org-id org-001

Environment variables:
    DATABASE_URL: Postgres connection string (required)
"""

import argparse
import asyncio
import logging
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# The script lives in modules/gateway/scripts/; the package root is its parent.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.shared.identity import UnresolvableUserEntityError, resolve_user_entity_id  # noqa: E402
from src.shared.models.budget import BudgetConfig  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rekey-user-budget-configs")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Rekey mis-keyed user budget_configs rows onto the Cognito sub (#4511).")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the rekeys. Without this flag the script only reports.",
    )
    parser.add_argument("--org-id", default=None, help="Restrict to a single org_id.")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        logger.error("DATABASE_URL is required")
        return 2

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("Mode: %s", mode)

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    already_keyed = 0
    rekeyed: list[str] = []
    collisions: list[str] = []
    unresolvable: list[str] = []

    try:
        async with factory() as db:
            query = select(BudgetConfig).where(BudgetConfig.entity_type == "user")
            if args.org_id:
                query = query.where(BudgetConfig.org_id == args.org_id)
            rows = (await db.execute(query.order_by(BudgetConfig.org_id, BudgetConfig.entity_id))).scalars().all()

            logger.info("Examining %d user-scoped budget_configs rows", len(rows))

            for row in rows:
                label = f"org={row.org_id} entity_id={row.entity_id} period={row.period_type}"

                try:
                    resolved = await resolve_user_entity_id(db, row.org_id, row.entity_id)
                except UnresolvableUserEntityError as exc:
                    unresolvable.append(f"{label} — {exc.message}")
                    logger.warning("UNRESOLVABLE %s", label)
                    continue

                if resolved == row.entity_id:
                    # Already correctly sub-keyed. Nothing to do — this is what
                    # makes the script idempotent.
                    already_keyed += 1
                    continue

                # Would the rekey collide with a row that is already enforcing?
                existing = await db.scalar(
                    select(BudgetConfig).where(
                        BudgetConfig.org_id == row.org_id,
                        BudgetConfig.entity_type == "user",
                        BudgetConfig.entity_id == resolved,
                        BudgetConfig.period_type == row.period_type,
                    )
                )
                if existing is not None:
                    collisions.append(
                        f"{label} — resolves to {resolved}, which already has a row "
                        f"(amount={existing.budget_amount_usd}, mode={existing.enforcement_mode}); "
                        f"mis-keyed row amount={row.budget_amount_usd}. Left unchanged — reconcile by hand."
                    )
                    logger.warning("COLLISION %s → %s", label, resolved)
                    continue

                rekeyed.append(f"{label} → {resolved} (amount={row.budget_amount_usd})")
                if args.apply:
                    row.entity_id = resolved
                    logger.info("REKEYED %s → %s", label, resolved)
                else:
                    logger.info("[DRY-RUN] Would rekey %s → %s", label, resolved)

            if args.apply and rekeyed:
                await db.commit()
                logger.info("Committed %d rekeys", len(rekeyed))
    finally:
        await engine.dispose()

    print()
    print("=" * 72)
    print(f"#4511 user budget_configs repair — {mode}")
    print("=" * 72)
    print(f"  rekeyed:            {len(rekeyed)}")
    print(f"  collision:          {len(collisions)}")
    print(f"  unresolvable:       {len(unresolvable)}")
    print(f"  already sub-keyed:  {already_keyed}")
    print()

    for title, entries in (("REKEYED", rekeyed), ("COLLISIONS (need human reconciliation)", collisions), ("UNRESOLVABLE", unresolvable)):
        if entries:
            print(f"{title}:")
            for entry in entries:
                print(f"  - {entry}")
            print()

    if not args.apply and rekeyed:
        print("Re-run with --apply to write these rekeys.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
