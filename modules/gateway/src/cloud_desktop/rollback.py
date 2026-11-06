"""Operator rollback of Dev Box admissions without touching owner inventory."""

from __future__ import annotations

import argparse
import asyncio
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database import get_session_factory
from src.shared.models.dev_box import DevBoxPolicy


async def suspend_admissions(db: AsyncSession, *, org_id: str, deployment_id: str, policy_id: str) -> bool:
    policy = await db.scalar(
        select(DevBoxPolicy)
        .where(DevBoxPolicy.id == policy_id, DevBoxPolicy.org_id == org_id, DevBoxPolicy.deployment_id == deployment_id)
        .with_for_update()
    )
    if policy is None:
        raise ValueError("Dev Box rollback policy identity mismatch")
    was_enabled = policy.enabled
    policy.enabled = False
    await db.flush()
    return was_enabled


async def run(org_id: str, deployment_id: str, policy_id: str) -> bool:
    if not deployment_id or os.getenv("BG_DEV_BOX_DEPLOYMENT_ID") != deployment_id:
        raise ValueError("Dev Box rollback deployment is not configured for this gateway")
    async with get_session_factory().begin() as db:
        return await suspend_admissions(db, org_id=org_id, deployment_id=deployment_id, policy_id=policy_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pause Dev Box admission without deleting owned resources")
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--policy-id", required=True)
    arguments = parser.parse_args()
    try:
        was_enabled = asyncio.run(run(arguments.org_id, arguments.deployment_id, arguments.policy_id))
    except ValueError as error:
        parser.error(str(error))
    print("Dev Box admissions disabled" if was_enabled else "Dev Box admissions already disabled")


if __name__ == "__main__":
    main()
