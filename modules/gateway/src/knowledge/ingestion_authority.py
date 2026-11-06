"""Server-owned authority for one registered ingestion attempt (#4720).

Queue fields and worker requests never select an owner, repository, bucket or
privileged identity. Authority is reconstructed from the current database row.
Workers receive object-specific capabilities, not the controller's credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.knowledge.dispatch import build_scope_envelope, extract_source_identifier
from src.knowledge.ingestion_callback_grant import GrantError, verify_ingestion_grant
from src.knowledge.source_policy.scope import compute_s3_prefix, parse_scope


def deny() -> HTTPException:
    return HTTPException(403, "Ingestion authority is invalid or no longer active")


@dataclass(frozen=True)
class IngestionAuthority:
    asset_id: str
    attempt_id: str
    asset_type: str
    source_ref: str
    tenant_id: str | None
    owner_sub: str | None
    project_id: str | None
    product_id: str | None
    installation_id: int | None
    product_source: dict | None = None
    source_object_version: str | None = None

    @property
    def source(self) -> str:
        return extract_source_identifier(self.source_ref, self.asset_type)

    @property
    def scope(self):
        return parse_scope(build_scope_envelope(self.tenant_id, self.owner_sub, self.project_id, self.product_id))

    @property
    def run_id(self) -> str:
        return str(uuid.uuid5(uuid.UUID(self.attempt_id), self.asset_id))

    @property
    def registered_only(self) -> bool:
        return bool(self.product_id or self.owner_sub or self.asset_type != "repo")

    @property
    def tracking_source(self) -> str:
        return self.asset_id if self.registered_only else self.source

    def envelope(self) -> dict[str, Any]:
        return {
            "registry_asset_id": self.asset_id,
            "ingestion_attempt_id": self.attempt_id,
            "source": self.source,
            "content_type": self.asset_type,
            "scope": self.scope.to_dict(),
            "installation_id": self.installation_id,
            "triggered_by": "registered_ingestion",
        }

    def permits_object(self, key: str) -> bool:
        """Explicit object families; a common tenant prefix is never authority."""
        if (
            not isinstance(key, str)
            or len(key) > 1024
            or "\\" in key
            or any(p in {"", ".", ".."} for p in key.split("/"))
            or any(ord(c) < 32 for c in key)
        ):
            return False
        scope = self.scope
        if self.registered_only:
            prefix = f"{compute_s3_prefix(scope, 'content')}/assets/{self.asset_id}/attempts/{self.attempt_id}/"
            return key.startswith(prefix)
        repo = self.source
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            return False
        safe_name = repo.replace("/", "-")
        objects = {
            f"{compute_s3_prefix(scope, 'content/wikis')}/{safe_name}-wiki.md",
            f"{compute_s3_prefix(scope, 'content/code-indexes')}/{safe_name}-code-index.md",
            f"{compute_s3_prefix(scope, 'sbom')}/repos/{repo}/source.cdx.json",
            f"{compute_s3_prefix(scope, 'code-indexes')}/{safe_name}.json",
        }
        # Raw code-index data and lexical shards are confined to this repository.
        prefixes = (
            f"{compute_s3_prefix(scope, 'content')}/repos/{repo}/",
            f"zoekt-shards/{repo}/",
        )
        return key in objects or any(key.startswith(p) for p in prefixes)

    def vector_metadata(self) -> dict[str, str]:
        return {
            "asset_id": self.asset_id,
            "ingestion_attempt_id": self.attempt_id,
            "visibility": self.scope.visibility,
            **({"repo": self.source, "org_id": self.source.split("/")[0]} if self.asset_type == "repo" else {}),
            **({"tenant_id": self.tenant_id} if self.tenant_id else {}),
            **({"owner_sub": self.owner_sub} if self.owner_sub else {}),
            **({"product_id": self.product_id} if self.product_id else {}),
        }


async def authorize_ingestion(token: str, db: AsyncSession, *, terminal: bool = False) -> IngestionAuthority:
    try:
        grant = verify_ingestion_grant(token)
        uuid.UUID(grant.asset_id)
    except (GrantError, ValueError, TypeError):
        raise deny() from None
    row = (
        (
            await db.execute(
                text("""
        SELECT id, asset_type, source_ref, tenant_id, owner_sub, project_id,
               product_id, installation_id, ingestion_attempt_id,
               callback_grant_sha256, status, to_jsonb(knowledge_assets)->'product_source' AS product_source,
               to_jsonb(knowledge_assets)->>'source_object_version' AS source_object_version
        FROM knowledge_assets WHERE id = CAST(:id AS uuid) FOR UPDATE
    """),
                {"id": grant.asset_id},
            )
        )
        .mappings()
        .first()
    )
    allowed = {"registered", "queued", "indexing"}
    if terminal:
        allowed |= {"complete", "partial", "failed"}
    if (
        row is None
        or row["status"] not in allowed
        or row["tenant_id"] != grant.tenant_id
        or str(row["ingestion_attempt_id"]) != grant.attempt_id
        or not hmac.compare_digest(str(row["callback_grant_sha256"] or ""), hashlib.sha256(token.encode()).hexdigest())
    ):
        raise deny()
    if row["asset_type"] not in {"repo", "url", "doc"}:
        raise deny()  # Infrastructure discovery is a separately privileged workload.
    try:
        authority = IngestionAuthority(
            asset_id=str(row["id"]),
            attempt_id=grant.attempt_id,
            project_id=str(row["project_id"]) if row["project_id"] else None,
            **{k: row[k] for k in ("asset_type", "source_ref", "tenant_id", "owner_sub", "product_id", "installation_id")},
            product_source=row.get("product_source"),
            source_object_version=row.get("source_object_version"),
        )
        authority.scope
    except (ValueError, TypeError):
        raise deny() from None
    return authority
