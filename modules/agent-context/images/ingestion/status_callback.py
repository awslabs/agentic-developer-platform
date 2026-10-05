"""Status callback emitter: worker → gateway knowledge_assets row.

Issue #2049: Minimal status-callback bridge (C1/Decision 5).

Emits status updates to the gateway's /internal/v1/knowledge-assets/status-callback
endpoint so the knowledge_assets row reflects live ingestion progress.

Fail-open discipline: callback errors are logged but NEVER raised. Ingestion must
never be blocked by a callback failure (same principle as telemetry.safe_emit).

Usage:
    from status_callback import emit_status_callback

    emit_status_callback(asset_id, "indexing")
    emit_status_callback(asset_id, "complete", status_detail={...})
    emit_status_callback(asset_id, "failed", error="timeout after 900s")
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger("sqs-worker.status_callback")

# Configuration from environment (injected via K8s ConfigMap)
_GATEWAY_CALLBACK_URL = os.environ.get("GATEWAY_CALLBACK_URL", "")
_GATEWAY_INTERNAL_API_KEY = os.environ.get("GATEWAY_INTERNAL_API_KEY", "")

# Timeout for the HTTP POST (seconds) — short to avoid blocking ingestion
_CALLBACK_TIMEOUT = 10


def emit_status_callback(
    asset_id: str | None,
    status: str,
    status_detail: dict[str, Any] | None = None,
    error: str | None = None,
    tenant_id: str | None = None,
    callback_grant: str | None = None,
) -> None:
    """Post a status update to the gateway callback endpoint.

    Fail-open: any exception is caught and logged. Ingestion is never blocked.

    Args:
        asset_id: The knowledge_assets row UUID (from registry_asset_id in SQS message).
                  If None or empty, the callback is skipped silently.
        status: One of "indexing", "complete", "failed".
        status_detail: Optional compact projection of run-state for status_detail JSONB.
        error: Optional error message (used when status is "failed").
        tenant_id: Owning tenant of the asset (scope.tenant_id). Issue #5663 (A09):
                   this is now only a CHECKED ASSERTION at the gateway — it no
                   longer selects which row is written, and if it contradicts the
                   grant the callback is refused. Still sent because a disagreement
                   between the routing scope this pod ingested under and the tenant
                   the gateway recorded is worth failing on rather than ignoring.
        callback_grant: Opaque server-minted authority for this asset, taken
                   verbatim from the SQS envelope's "callback_grant" field
                   (issue #5663, A09). The worker cannot produce, inspect
                   usefully, or retarget it; forwarding it unchanged is the whole
                   contract. Without it the gateway has no server-owned fact to
                   check the body against, and once the gateway enforces, such a
                   callback is refused — so this must never be reconstructed or
                   defaulted, only passed through.
    """
    # Skip if no asset_id (legacy messages without registry_asset_id)
    if not asset_id:
        return

    # Skip if callback URL is not configured (local dev, or pre-bridge deployments)
    if not _GATEWAY_CALLBACK_URL:
        logger.debug("GATEWAY_CALLBACK_URL not set — skipping status callback for %s", asset_id)
        return

    try:
        url = f"{_GATEWAY_CALLBACK_URL.rstrip('/')}/internal/v1/knowledge-assets/status-callback"

        payload: dict[str, Any] = {
            "asset_id": asset_id,
            "status": status,
        }
        if status_detail is not None:
            payload["status_detail"] = status_detail
        if error is not None:
            payload["error"] = error[:1000]  # Truncate to match gateway limit
        if tenant_id:
            payload["tenant_id"] = tenant_id
        if callback_grant:
            payload["callback_grant"] = callback_grant

        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }

        resp = requests.post(
            url,
            data=json.dumps(payload),
            headers=headers,
            timeout=_CALLBACK_TIMEOUT,
        )

        if resp.status_code == 200:
            logger.info("Status callback sent: asset=%s status=%s", asset_id, status)
        elif resp.status_code == 404:
            # Asset row not found (possible race if row was removed) — not fatal
            logger.warning("Status callback 404: asset=%s not found in gateway", asset_id)
        else:
            logger.warning(
                "Status callback failed: asset=%s status=%d body=%s",
                asset_id,
                resp.status_code,
                resp.text[:200],
            )

    except requests.Timeout:
        logger.warning("Status callback timeout: asset=%s (%.0fs)", asset_id, _CALLBACK_TIMEOUT)
    except Exception as e:
        # Fail-open: never let a callback failure crash the worker
        logger.warning("Status callback error: asset=%s error=%s", asset_id, str(e)[:200])
