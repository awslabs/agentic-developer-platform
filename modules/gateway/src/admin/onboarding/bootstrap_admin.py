"""First-admin bootstrap — seed the initial platform admin's DB rows.

On a fresh deploy the `users` table is empty, so `get_access_status` returns
"new" for everyone (including the seeded Cognito admin) and the SPA shows
"request access" — with nobody able to approve, since approval requires a
registered admin. `create_test_users=true` only creates the Cognito user (+
"admins" group membership); it never writes the Postgres rows. This module
seeds the first admin's row set so the gate flips to "registered" and that admin
can approve real users via the UI.

It reuses `approve_request` (the atomic org/tenant/dept/team/user/identity
creator) and patches the two things that differ for an email/password admin:
the stray GitHub identity is removed, and the role/email are corrected.

Idempotent: safe to re-run. Requires USER_IDENTITY_INDEX_V2_WRITE=true (the
gateway pod env already sets it).

CLI:
    python -m src.admin.onboarding.bootstrap_admin \
        --cognito-sub <sub> --email <addr> [--org platform] \
        [--display-name <str>] [--role platform_admin] [--no-ddb]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.shared.database import get_session_factory
from src.shared.models.base import new_uuid
from src.shared.models.onboarding import TenantAccessRequest
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity

from .approval import approve_request
from .schemas import RESERVED_TENANT_IDS, TENANT_ID_PATTERN

logger = logging.getLogger(__name__)

# Fixed well-known org slug for the first admin. NOTE: bare "platform" is in
# RESERVED_TENANT_IDS (schemas.py), so we use "platform-admin" — a valid,
# unreserved slug that conveys the same intent. Overridable via --org.
DEFAULT_ADMIN_ORG = "platform-admin"


async def bootstrap_first_admin(
    db: AsyncSession,
    *,
    cognito_sub: str,
    org_id: str,
    display_name: str,
    email: str,
    role: str = "platform_admin",
    identity_writer: IdentityIndexWriter | None = None,
) -> dict:
    """Idempotently seed the first admin's org/tenant/dept/team/user/cognito-identity.

    Returns {"status": "bootstrapped"|"already-bootstrapped", "org_id", "user_id"}.

    Reuses approve_request for the atomic multi-table creation, then:
      - deletes the stray provider="github" UserIdentity it creates (this admin
        logs in via Cognito email/password, not GitHub),
      - sets users.role to `role` (default platform_admin) and users.email to the
        real address (approve_request writes a "<login>@github.onboard" placeholder).
    """
    # Validate the org slug the same way the real onboarding path does.
    if org_id in RESERVED_TENANT_IDS or not TENANT_ID_PATTERN.match(org_id):
        raise ValueError(f"Invalid org_id {org_id!r}: must match {TENANT_ID_PATTERN.pattern} and not be reserved ({sorted(RESERVED_TENANT_IDS)})")

    # Guard 1: already bootstrapped for this Cognito identity.
    existing = await db.scalar(select(User).where(User.cognito_sub == cognito_sub))
    if existing is not None:
        logger.info("First-admin already bootstrapped (cognito_sub=%s, user=%s)", cognito_sub, existing.id)
        return {"status": "already-bootstrapped", "org_id": existing.org_id, "user_id": existing.id}

    # Build a synthetic pending request and run it through the canonical approver.
    req = TenantAccessRequest(
        id=new_uuid(),
        cognito_sub=cognito_sub,
        provider="cognito",
        provider_user_id=cognito_sub,
        proposed_tenant_id=org_id,
        target_login=display_name,
        motivation="first-admin bootstrap",
        status="pending",
    )
    db.add(req)
    await db.flush()

    tenant_id = await approve_request(db=db, request=req, admin_sub=cognito_sub, identity_writer=identity_writer)

    # Resolve the freshly-created user.
    user = await db.scalar(select(User).where(User.cognito_sub == cognito_sub))
    if user is None:
        raise RuntimeError("bootstrap: approve_request did not create a User row")

    # Patch 1: drop the stray github identity approve_request always creates.
    # An email/password admin has no GitHub identity; leaving it would pollute the
    # GitHub-user resolution the agent webhook path reads.
    await db.execute(delete(UserIdentity).where(UserIdentity.user_id == user.id, UserIdentity.provider == "github"))

    # Patch 2: correct role + real email (approve_request writes org_admin +
    # "<login>@github.onboard").
    user.role = role
    user.email = email
    await db.commit()

    logger.info(
        "First-admin bootstrapped: user=%s org=%s role=%s email=%s",
        user.id,
        tenant_id,
        role,
        email,
    )
    return {"status": "bootstrapped", "org_id": tenant_id, "user_id": user.id}


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bootstrap_admin",
        description="Seed the first platform admin's DB rows (idempotent).",
    )
    p.add_argument("--cognito-sub", required=True, help="The admin's Cognito sub (subject) UUID")
    p.add_argument("--email", required=True, help="Admin login email → users.email")
    p.add_argument("--org", default=DEFAULT_ADMIN_ORG, help=f"Org/tenant slug (default: {DEFAULT_ADMIN_ORG})")
    p.add_argument("--display-name", default=None, help="Org display name (default: --org)")
    p.add_argument("--role", default="platform_admin", help="users.role (default: platform_admin)")
    p.add_argument("--no-ddb", action="store_true", help="Skip the DynamoDB identity-index write-through")
    return p


async def _amain(args: argparse.Namespace) -> int:
    writer = None if args.no_ddb else IdentityIndexWriter()
    factory = get_session_factory()
    async with factory() as db:
        result = await bootstrap_first_admin(
            db,
            cognito_sub=args.cognito_sub,
            org_id=args.org,
            display_name=args.display_name or args.org,
            email=args.email,
            role=args.role,
            identity_writer=writer,
        )
    print(json.dumps(result))  # machine-readable for the shell wrapper
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_amain(_build_argparser().parse_args())))


if __name__ == "__main__":
    main()
