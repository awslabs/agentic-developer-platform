"""Product-scoped repository set for a run's GitHub installation token (#9550).

A hosted run's installation token is minted for the one repository the run was
dispatched to. That is the right default, but a Product routinely spans
repositories (a planning repository plus one or more code repositories, all
bound as sources of the same Product), and the hosted AIDLC emit step must read
and file stories in the Product's other repositories. Before this module every
such call was refused with 404/403 because the token could not see them.

The widening is derived only from server-side evidence:

* the run's bound repository (``InstallationBinding.repo`` and, when the trusted
  execution row carries it, its GitHub id), never from the request body;
* the owner-configured ``ProductSourceBinding`` rows of the **one** Product that
  binds that repository as a source. A repository bound to no Product, or to
  more than one, keeps the single-repository scope (nothing is guessed);
* only bindings whose GitHub App installation is the run's own installation
  (one installation is one GitHub account, which is also what the token is
  minted for), with every name read back from GitHub by immutable id.

Any failure to prove the set (GitHub unavailable, a name that does not match its
id, a binding whose connection is disputed) degrades to the single repository and
is logged; it never widens and never blocks the mint. The token's permissions are
unchanged; the audit row records the repositories actually minted.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.products.models import Product, ProductProjectBinding, ProductSourceBinding
from src.shared.models.vault import ChannelTenantMap

logger = logging.getLogger(__name__)

_FULL_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
#: Whole-resolution budget. The mint is on the worker's bootstrap path; a slow
#: GitHub read must degrade to the single repository, not stall the run.
RESOLUTION_TIMEOUT_SECONDS = 8.0
#: Bound on how many bindings are read back. A Product with more repositories
#: than this is not widened: the token stays single-repository and the log says so.
MAX_BOUND_REPOSITORIES = 25


@dataclass(frozen=True)
class ProductRepositoryScope:
    """The repositories one Product binds in the run's installation."""

    product_id: str
    repositories: tuple[str, ...]
    repository_ids: tuple[int, ...]

    @property
    def repository_names(self) -> tuple[str, ...]:
        """The ``repositories`` values GitHub's token endpoint takes (name only)."""
        return tuple(full.split("/", 1)[1] for full in self.repositories)


class _DegradeError(Exception):
    """Internal: stop widening and fall back to the single repository."""


def _active_source_bindings():
    return select(ProductSourceBinding).where(
        ProductSourceBinding.kind == "repository",
        ProductSourceBinding.provider == "github",
        ProductSourceBinding.unlinked_at.is_(None),
        ProductSourceBinding.status != "revoked",
    )


async def _bound_repository_id(github, installation_id: int, repo_full_name: str) -> int:
    document = await github.installation_request(installation_id, "GET", f"/repos/{repo_full_name}")
    identity = document.get("id")
    if (
        not isinstance(identity, int)
        or isinstance(identity, bool)
        or identity <= 0
        or str(document.get("full_name", "")).casefold() != repo_full_name.casefold()
    ):
        raise _DegradeError("bound repository identity could not be verified")
    return identity


async def _single_product(db: AsyncSession, *, tenant_id: str, repository_id: int) -> Product | None:
    rows = (
        (
            await db.execute(
                select(ProductProjectBinding.product_id)
                .join(
                    ProductSourceBinding,
                    (ProductSourceBinding.tenant_id == ProductProjectBinding.tenant_id)
                    & (ProductSourceBinding.project_id == ProductProjectBinding.project_id),
                )
                .where(
                    ProductProjectBinding.tenant_id == tenant_id,
                    ProductProjectBinding.valid_to.is_(None),
                    ProductSourceBinding.kind == "repository",
                    ProductSourceBinding.provider == "github",
                    ProductSourceBinding.unlinked_at.is_(None),
                    ProductSourceBinding.status != "revoked",
                    ProductSourceBinding.native_id == str(repository_id),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != 1:
        if len(rows) > 1:
            logger.info("token_scope: repository %s is bound to %d Products; keeping single-repository scope", repository_id, len(rows))
        return None
    product = await db.get(Product, (tenant_id, rows[0]))
    if product is None or product.archived_at is not None:
        return None
    return product


async def _resolve(
    db: AsyncSession,
    *,
    tenant_id: str,
    installation_id: int,
    repo_full_name: str,
    repository_id: int | None,
    github,
) -> ProductRepositoryScope | None:
    from src.products.routes import product_github_bindings

    owner = repo_full_name.split("/", 1)[0]
    bound_id = repository_id if repository_id is not None else await _bound_repository_id(github, installation_id, repo_full_name)
    product = await _single_product(db, tenant_id=tenant_id, repository_id=bound_id)
    if product is None:
        return None

    bindings = await product_github_bindings(db, product)
    connection_ids = {binding.installation_id for binding in bindings if binding.installation_id}
    connections = {}
    if connection_ids:
        for connection in (
            await db.scalars(
                select(ChannelTenantMap).where(
                    ChannelTenantMap.id.in_(connection_ids),
                    ChannelTenantMap.org_id == tenant_id,
                    ChannelTenantMap.provider == "github",
                )
            )
        ).all():
            connections[connection.id] = connection

    same_installation = []
    for binding in bindings:
        connection = connections.get(binding.installation_id or "")
        if connection is None or connection.ownership_disputed:
            continue
        if str(connection.installation_id or "") != str(installation_id):
            continue
        if not binding.native_id.isascii() or not binding.native_id.isdecimal():
            raise _DegradeError("a bound repository has no numeric identity")
        same_installation.append(int(binding.native_id))
    # The run's own repository first (the response contract), the rest in id order.
    repository_ids = [bound_id, *sorted(set(same_installation) - {bound_id})]
    if len(repository_ids) > MAX_BOUND_REPOSITORIES:
        raise _DegradeError(f"Product binds {len(repository_ids)} repositories; above the widening bound")
    if repository_ids == [bound_id]:
        return ProductRepositoryScope(product.id, (repo_full_name,), (bound_id,))

    names = {bound_id: repo_full_name}
    for identity in repository_ids:
        if identity == bound_id:
            continue
        document = await github.get_installation_repository(installation_id, identity)
        name = document.get("full_name")
        repo_owner = (document.get("owner") or {}).get("login", "")
        if (
            document.get("id") != identity
            or not isinstance(name, str)
            or not _FULL_NAME.fullmatch(name)
            or name.casefold() != f"{repo_owner}/{document.get('name')}".casefold()
            or repo_owner.casefold() != owner.casefold()
        ):
            raise _DegradeError(f"repository {identity} did not read back as a repository of {owner}")
        names[identity] = name
    return ProductRepositoryScope(
        product.id,
        tuple(names[identity] for identity in repository_ids),
        tuple(repository_ids),
    )


async def product_repository_scope(
    db: AsyncSession,
    *,
    tenant_id: str,
    installation_id: int,
    repo_full_name: str,
    repository_id: int | None,
    github,
    timeout_seconds: float = RESOLUTION_TIMEOUT_SECONDS,
) -> ProductRepositoryScope | None:
    """The Product-bound repositories a run's token may list, or ``None``.

    ``None`` means "single repository": the bound repository is not a source of
    exactly one Product, or the set could not be proven within the budget.
    """
    if not _FULL_NAME.fullmatch(repo_full_name):
        return None
    try:
        async with asyncio.timeout(timeout_seconds):
            return await _resolve(
                db,
                tenant_id=tenant_id,
                installation_id=installation_id,
                repo_full_name=repo_full_name,
                repository_id=repository_id,
                github=github,
            )
    except _DegradeError as exc:
        logger.warning("token_scope: keeping single-repository scope for %s: %s", repo_full_name, exc)
    except (TimeoutError, Exception) as exc:  # noqa: BLE001 - degrade, never block the mint
        logger.warning("token_scope: keeping single-repository scope for %s: %s", repo_full_name, type(exc).__name__)
    return None
