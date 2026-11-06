"""Dedicated reconciler: python -m src.gitlab.home_worker [--once]."""

import argparse
import asyncio
import hashlib
import logging
import os

import boto3
from sqlalchemy import select, text

from src.gitlab.admin_client import GitlabAdmin, ProvisioningError
from src.gitlab.config import discover_url, environment, region
from src.gitlab.home_models import ProductGitlabHome
from src.gitlab.homes import desired_members, enabled, membership_digest
from src.products.models import Product
from src.shared.database import get_session_factory
from src.shared.models.base import utcnow

logger = logging.getLogger(__name__)


async def reconcile_product(db, admin, product, url):
    home = await db.get(ProductGitlabHome, (product.tenant_id, product.id))
    if home is None:
        home = ProductGitlabHome(tenant_id=product.tenant_id, product_id=product.id, instance_url=url)
        db.add(home)
    if home.instance_url != url:
        raise ProvisioningError("instance_migration_required")
    if home.group_id is not None:
        existing = await admin.request("GET", f"groups/{home.group_id}", missing=True)
        if not existing or existing.get("full_path") != home.full_path or existing.get("description") != f"ADP product {product.id}":
            raise ProvisioningError("namespace_ownership_conflict")
    desired = await desired_members(db, product)
    tenant_key = hashlib.sha256(product.tenant_id.encode()).hexdigest()[:24]
    parent = await admin.group(f"tenant-{tenant_key}", "ADP workspace", f"ADP tenant {tenant_key}")
    # Parent memberships inherit into every Product; only the service owns it.
    await admin.memberships("groups", parent["id"], {})
    await admin.effective_memberships("groups", parent["id"], {})
    # GitLab requires sibling display names to be unique; ADP Product names are
    # not unique. Keep the immutable identifier in both name and path.
    group = await admin.group(
        f"product-{product.id}", f"{product.name} ({product.id})", f"ADP product {product.id}", parent_id=(parent["id"], parent["full_path"])
    )
    if home.group_id is not None and home.group_id != group["id"]:
        raise ProvisioningError("group_identity_changed")
    home.group_id, home.full_path = group["id"], group["full_path"]
    # Revoke departed users before any provisioning that might fail on a new user.
    retained = {native_id: desired[uid]["access_level"] for uid, native_id in (home.members or {}).items() if uid in desired}
    await admin.memberships("groups", group["id"], retained)
    await admin.descendants(group["id"])
    await admin.project_boundaries(group["id"])
    native = {}
    for uid, identity in desired.items():
        native[uid] = await admin.user(identity)
    await admin.memberships("groups", group["id"], {native[uid]: row["access_level"] for uid, row in desired.items()})
    await admin.effective_memberships("groups", group["id"], {native[uid]: row["access_level"] for uid, row in desired.items()})
    # A team edit racing this transaction must not be labelled ready.
    await db.refresh(product)
    current = await desired_members(db, product)
    if membership_digest(current) != membership_digest(desired):
        await admin.memberships("groups", group["id"], {})
        raise ProvisioningError("membership_changed")
    home.members = native
    home.membership_digest = membership_digest(desired)
    home.status = "archived" if product.archived_at or product.lifecycle_status == "retired" else "ready"
    home.error_code = None
    home.checked_at = utcnow()
    await db.flush()
    return home


async def reconcile_once(factory=None, admin=None, url=None):
    if not enabled():
        return
    url = url or await asyncio.to_thread(discover_url)
    if not url:
        raise ProvisioningError("instance_not_configured")
    factory = factory or get_session_factory()
    if admin is None:
        secret_id = os.getenv("ADP_GITLAB_ADMIN_SECRET", f"adp/{environment()}/gitlab-admin-token")
        response = await asyncio.to_thread(boto3.client("secretsmanager", region_name=region()).get_secret_value, SecretId=secret_id)
        async with GitlabAdmin(url, response["SecretString"]) as client:
            return await reconcile_once(factory, client, url)
    async with factory() as db:
        product_ids = (
            await db.execute(
                select(Product.tenant_id, Product.id)
                .outerjoin(ProductGitlabHome, (ProductGitlabHome.tenant_id == Product.tenant_id) & (ProductGitlabHome.product_id == Product.id))
                .order_by(ProductGitlabHome.checked_at.asc().nullsfirst(), Product.tenant_id, Product.id)
                .limit(100)
            )
        ).all()
    for product_key in product_ids:
        async with factory() as db:
            # Serialize provider mutations, but commit each Product independently:
            # a slow/failing later Product must not hide already-completed syncs.
            if db.bind.dialect.name == "postgresql" and not await db.scalar(text("SELECT pg_try_advisory_xact_lock(37173777)")):
                return
            product = await db.get(Product, tuple(product_key))
            if product is None:
                continue
            try:
                async with asyncio.timeout(60):
                    await reconcile_product(db, admin, product, url)
            except Exception as error:
                # Provider mutations may already have succeeded. Keep IDs and retry
                # by immutable namespace/identity rather than losing the binding.
                home = await db.get(ProductGitlabHome, (product.tenant_id, product.id))
                if home:
                    home.status = "error"
                    home.error_code = str(error) if isinstance(error, ProvisioningError) else "provisioning_unavailable"
                    home.checked_at = utcnow()
                logger.warning(
                    "GitLab home reconciliation incomplete",
                    extra={"product_id": product.id, "error_code": home.error_code if home else "provisioning_unavailable"},
                )
            await db.commit()


async def main(once=False):
    while True:
        await reconcile_once()
        if once:
            return
        await asyncio.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    asyncio.run(main(parser.parse_args().once))
