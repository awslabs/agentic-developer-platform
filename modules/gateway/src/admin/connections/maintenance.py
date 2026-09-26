"""Revision-bound maintenance of the existing deployment App secret.

The operator generates keys on GitHub. ADP verifies a supplied key before moving
AWSCURRENT, retaining AWSPREVIOUS and never deleting the old key on GitHub.
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID

import boto3
import httpx
from botocore.exceptions import ClientError
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .github_client import _mint_app_jwt
from .service import _get_environment, _invalidate_verification_cache, invalidate_app_credentials_cache


class AppMaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_app_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    expected_key_version: str = Field(min_length=32, max_length=64)


class AppKeyRequest(AppMaintenanceRequest):
    operation_id: UUID
    private_key: SecretStr = Field(min_length=64, max_length=32768)


def _store():
    return boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _paths():
    prefix = f"adp/{_get_environment()}/github-app/adp-agent-platform"
    return prefix + "-id", prefix + "-key"


def _current(sm):
    id_path, key_path = _paths()
    try:
        app = sm.get_secret_value(SecretId=id_path)
        key = sm.get_secret_value(SecretId=key_path)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"ResourceNotFoundException", "InvalidRequestException"}:
            raise HTTPException(404, "GitHub App credentials are unavailable") from None
        raise HTTPException(503, "GitHub App credential store is unavailable") from None
    app_id = app.get("SecretString", "")
    if not app_id.isascii() or not app_id.isdigit() or not key.get("VersionId"):
        raise HTTPException(409, "GitHub App configuration is incomplete")
    return app_id, key["VersionId"]


async def maintenance_status():
    app_id, version = await asyncio.to_thread(_current, _store())
    return {"app_id": app_id, "key_version": version, "contract": "app-maintenance-v1"}


async def require_revision(request: AppMaintenanceRequest):
    current = await maintenance_status()
    if current["app_id"] != request.expected_app_id or current["key_version"] != request.expected_key_version:
        raise HTTPException(409, "App configuration changed; review current App and key revision")
    return current


async def rotate_supplied_key(request: AppKeyRequest):
    sm = _store()
    app_id, version = await asyncio.to_thread(_current, sm)
    operation = str(request.operation_id)
    if app_id != request.expected_app_id:
        raise HTTPException(409, "App identity changed; nothing rotated")
    _, key_path = _paths()
    private_key = request.private_key.get_secret_value()
    # An exact replay is read-only. A changed payload with the same ID is refused.
    if version == operation:
        record = await asyncio.to_thread(sm.get_secret_value, SecretId=key_path, VersionId=operation)
        if record.get("SecretString") != private_key:
            raise HTTPException(409, "Operation ID is already bound to another key")
        invalidate_app_credentials_cache()
        _invalidate_verification_cache()
        return {"rotated": True, "app_id": app_id, "key_version": operation, "operation_id": operation, "replayed": True}
    if version != request.expected_key_version:
        raise HTTPException(409, "Key revision changed; review current App before rotation")
    # A staged same-ID request can finish after a lost activation response.
    # Older completed operations must never reactivate their previous key.
    try:
        previous = await asyncio.to_thread(sm.get_secret_value, SecretId=key_path, VersionId=operation)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise HTTPException(503, "Unable to reconcile key operation") from None
        previous = None
    if previous is not None:
        if previous.get("SecretString") != private_key or "AWSPENDING" not in previous.get("VersionStages", []):
            raise HTTPException(409, "Operation exists with different input or is no longer pending; inspect App status")
    try:
        jwt = _mint_app_jwt(app_id, private_key)
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                "https://api.github.com/app", headers={"Authorization": "Bearer " + jwt, "Accept": "application/vnd.github+json"}
            )
            response.raise_for_status()
            verified_id = response.json().get("id")
        if str(verified_id) != app_id:
            raise ValueError("identity mismatch")
    except Exception:
        raise HTTPException(422, "Supplied key could not authenticate the expected GitHub App; active key unchanged") from None
    try:
        # Explicit AWSPENDING prevents put_secret_value from moving AWSCURRENT.
        await asyncio.to_thread(
            sm.put_secret_value, SecretId=key_path, ClientRequestToken=operation, SecretString=private_key, VersionStages=["AWSPENDING"]
        )
        await asyncio.to_thread(
            sm.update_secret_version_stage, SecretId=key_path, VersionStage="AWSCURRENT", MoveToVersionId=operation, RemoveFromVersionId=version
        )
    except ClientError:
        raise HTTPException(
            409, "Key activation was not confirmed. Read maintenance status and reconcile this operation; old versions are retained"
        ) from None
    invalidate_app_credentials_cache()
    _invalidate_verification_cache()
    return {
        "rotated": True,
        "app_id": app_id,
        "key_version": operation,
        "operation_id": operation,
        "replayed": False,
        "previous_key_retained": True,
        "message": "Verified supplied key activated. Previous secret version and GitHub key retained; "
        "OAuth and webhook settings unchanged. Cached consumers converge on their normal credential refresh.",
    }
