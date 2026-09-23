"""Apply the ingestion source policy before registration and queue dispatch."""

from __future__ import annotations

import asyncio
import logging
import os

from src.knowledge.source_policy.scope import IngestionScope
from src.knowledge.source_policy.source_admission import SourceAdmissionError, validate_source
from src.knowledge.type_registry import validate_source_ref

log = logging.getLogger(__name__)


async def admit_source(asset_type: str, source: str, tenant_id: str | None, owner_sub: str | None) -> None:
    if not validate_source_ref(asset_type, source):
        raise SourceAdmissionError("source does not match a registered validator")
    if not tenant_id and not owner_sub and asset_type != "repo":
        raise SourceAdmissionError("private source ownership is required")
    scope = IngestionScope(
        tenant_id=tenant_id,
        owner_sub=owner_sub,
        visibility="personal" if owner_sub else "tenant" if tenant_id else "shared",
    )
    try:
        await asyncio.to_thread(
            validate_source,
            asset_type,
            source,
            scope,
            default_bucket=os.environ.get("AGENT_CONTEXT_S3_BUCKET", ""),
            allowlist=os.environ.get("S3_SOURCE_ALLOWLIST", ""),
        )
    except ValueError as exc:
        log.warning("knowledge_source_denied tenant=%s owner=%s type=%s reason=%s", tenant_id, owner_sub, asset_type, exc)
        raise SourceAdmissionError(str(exc)) from exc
