"""Capability-mediated ingestion: workers never hold shared cloud/database keys."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os

import httpx
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.knowledge.github_app_service import _get_ops_app_credentials, mint_installation_token, mint_installation_token_with_expiry
from src.knowledge.ingestion_authority import authorize_ingestion, deny
from src.knowledge.ingestion_backends import graph_command, state_command
from src.knowledge.ingestion_catalog import catalog_command, repository
from src.knowledge.ingestion_storage import client, object_command, vector_command
from src.knowledge.source_admission import admit_source
from src.knowledge.source_policy.repo_acl import derive_acl_from_github
from src.shared.database import get_db
from src.shared.database_agent_context import get_agent_context_db

router = APIRouter(prefix="/internal/v1/knowledge-assets", tags=["ingestion-control"])


class IngestionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    callback_grant: str = Field(min_length=1, max_length=4096)
    operation: str = Field(min_length=1, max_length=64)
    arguments: dict = Field(default_factory=dict)


def receipt_ticket(authority, receipt: str) -> str:
    if not isinstance(receipt, str) or not 1 <= len(receipt) <= 4096:
        raise deny()
    body = base64.urlsafe_b64encode(json.dumps([authority.asset_id, authority.attempt_id, receipt], separators=(",", ":")).encode()).decode()
    key = os.environ.get("AGENT_RUN_CREDENTIAL_KEY", "").encode()
    if not key:
        raise HTTPException(503, "Ingestion authority is not configured")
    mac = hmac.new(key, b"ingestion-receipt-v1." + body.encode(), hashlib.sha256).hexdigest()
    return body + "." + mac


def unseal_receipt(authority, ticket):
    try:
        body, mac = ticket.split(".")
        asset, attempt, receipt = json.loads(base64.urlsafe_b64decode(body))
        if asset != authority.asset_id or attempt != authority.attempt_id or not hmac.compare_digest(receipt_ticket(authority, receipt), ticket):
            raise ValueError()
        return receipt
    except (ValueError, TypeError, AttributeError):
        raise deny() from None


async def prepare_source(db, authority, gateway_db=None):
    await admit_source(authority.asset_type, authority.source_ref, authority.tenant_id, authority.owner_sub, product_id=authority.product_id)
    result = {"github_token": None, "allowed_principals": []}
    if authority.asset_type != "repo":
        return result
    if authority.product_source:
        from src.knowledge.product_sources import source_token

        result["github_token"] = await source_token(gateway_db, authority)
        return result
    acl_token = ""
    if authority.installation_id:
        app_id, key = await _get_ops_app_credentials()
        # The broad token is used only by the trusted ACL resolver; it is never
        # returned to the worker. The clone token is read-only and one-repo.
        acl_token = await mint_installation_token(app_id, key, authority.installation_id)
        # Resolve the immutable repository ID, then let GitHub enforce that it
        # belongs to this installation. A name alone can select another owner's
        # repository with the same basename.
        async with httpx.AsyncClient(follow_redirects=False, timeout=15) as http:
            response = await http.get(f"https://api.github.com/repos/{authority.source}", headers={"Authorization": f"Bearer {acl_token}"})
            response.raise_for_status()
            repo = response.json()
            if repo.get("full_name", "").casefold() != authority.source.casefold() or type(repo.get("id")) is not int:
                raise deny()
        token, _ = await mint_installation_token_with_expiry(
            app_id, key, authority.installation_id, repository_ids=[repo["id"]], permissions={"contents": "read", "metadata": "read"}
        )
        result["github_token"] = token
    if authority.registered_only:
        return result
    if acl_token:
        principals = await asyncio.to_thread(derive_acl_from_github, authority.source, acl_token)
    else:
        async with httpx.AsyncClient(follow_redirects=False, timeout=15) as http:
            response = await http.get(f"https://api.github.com/repos/{authority.source}")
            response.raise_for_status()
            if response.json().get("private") is not False or response.json().get("visibility") != "public":
                raise deny()
            principals = ["*"]
    await db.execute(
        text("""INSERT INTO repositories
        (repo_name,git_url,owner,allowed_principals,tenant_id,owner_sub,acl_public_verified)
        VALUES(:repo,:url,:owner,CAST(:acl AS jsonb),:tenant,:sub,:public)
        ON CONFLICT(repo_name) DO NOTHING"""),
        {
            "repo": authority.source,
            "url": f"https://github.com/{authority.source}",
            "owner": authority.source.split("/")[0],
            "acl": json.dumps(principals),
            "tenant": authority.tenant_id,
            "sub": authority.owner_sub,
            "public": principals == ["*"],
        },
    )
    row = await repository(db, authority)  # Reject conflicting immutable owner.
    await db.execute(
        text("""UPDATE repositories SET allowed_principals=CAST(:acl AS jsonb),
        acl_public_verified=:public,updated_at=NOW() WHERE id=:id"""),
        {"acl": json.dumps(principals), "public": principals == ["*"], "id": row["id"]},
    )
    result["allowed_principals"] = principals
    return result


@router.post("/ingestion-control")
async def ingestion_control(body: IngestionCommand, db: AsyncSession = Depends(get_agent_context_db), gateway_db: AsyncSession = Depends(get_db)):
    authority = await authorize_ingestion(body.callback_grant, db, terminal=body.operation == "ack")
    if authority.product_source and body.operation != "ack":
        from src.knowledge.product_sources import current_grant

        await current_grant(gateway_db, authority.tenant_id, authority.product_id, authority.product_source)
    from src.knowledge import product_storage

    if authority.product_id and product_storage.configured() and body.operation != "ack":
        await product_storage.require_service_product(gateway_db, authority.tenant_id, authority.product_id)
    if len(json.dumps(body.arguments)) > 8 * 1024 * 1024:
        raise HTTPException(413, "Ingestion command is too large")
    op, args = body.operation, body.arguments
    try:
        if op != "bootstrap":
            await repository(db, authority)
        if op == "bootstrap":
            if set(args) != {"receipt_handle"}:
                raise deny()
            source = await prepare_source(db, authority, gateway_db) if authority.product_source else await prepare_source(db, authority)
            result = {
                **authority.envelope(),
                **source,
                "callback_grant": body.callback_grant,
                "receipt_ticket": receipt_ticket(authority, args["receipt_handle"]),
            }
        elif op == "ack":
            receipt = unseal_receipt(authority, args.get("receipt_ticket"))
            queue = os.environ.get("INGESTION_QUEUE_URL", "")
            if not queue:
                raise HTTPException(503, "Ingestion queue is not configured")
            await asyncio.to_thread(client("sqs").delete_message, QueueUrl=queue, ReceiptHandle=receipt)
            result = None
        elif op.startswith("catalog."):
            result = await catalog_command(db, authority, op.removeprefix("catalog."), args)
            if authority.product_source and op == "catalog.complete_index_run":
                await db.execute(
                    text("""UPDATE knowledge_assets SET last_refreshed_at=NOW(),refresh_error=NULL
                    WHERE id=CAST(:asset AS uuid) AND ingestion_attempt_id=CAST(:attempt AS uuid)
                    AND EXISTS (SELECT 1 FROM index_runs WHERE id=CAST(:run AS uuid) AND status='complete')"""),
                    {"asset": authority.asset_id, "attempt": authority.attempt_id, "run": authority.run_id},
                )
        elif op.startswith("object."):
            if authority.product_id and product_storage.configured():
                storage = await product_storage.load_storage(db, authority.tenant_id, authority.product_id)
                result = await asyncio.to_thread(object_command, authority, op.removeprefix("object."), args, storage=storage)
            else:
                result = await asyncio.to_thread(object_command, authority, op.removeprefix("object."), args)
        elif op.startswith("vectors."):
            result = await asyncio.to_thread(vector_command, authority, op.removeprefix("vectors."), args)
        elif op in {"state", "history"}:
            result = await asyncio.to_thread(state_command, authority, op, args)
        elif op.startswith("graph."):
            await repository(db, authority)
            result = await asyncio.to_thread(graph_command, authority, op.removeprefix("graph."), args)
        elif op == "repo_acl":
            row = await repository(db, authority)
            result = list(row["allowed_principals"]) if row else []
        else:
            raise deny()
        await db.commit()
        return {"result": result}
    except HTTPException:
        await db.rollback()
        raise
    except ClientError as exc:
        await db.rollback()
        if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            raise HTTPException(404, "Ingestion object not found") from None
        raise HTTPException(503, "Ingestion storage unavailable") from None
    except Exception:
        await db.rollback()
        # SDK exceptions can contain presigned URLs, GitHub responses or other
        # credentials. Neither responses nor logs may reproduce those values.
        raise HTTPException(503, "Ingestion operation unavailable") from None
