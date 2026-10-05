"""Trusted domain database/queue bindings; request bodies never select credentials.

The paid operation store belongs to the domain. Its UUID tenant is distinct from
ADP's opaque run tenant and both are pinned by the deployment binding below.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import ssl
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import boto3
from botocore.config import Config
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.concurrency import run_in_threadpool


@dataclass(frozen=True)
class DomainBinding:
    domain: str
    org_id: str
    adp_org_id: str
    producer_registry_id: str
    worker_registry_id: str
    database_secret_id: str
    database_schema: str
    queue_url: str
    worker_namespace: str
    worker_service_account: str
    worker_container: str
    worker_image_digests: tuple[str, ...]
    repo: str
    observation_url: str
    observation_credential_secret_id: str
    worker_scaled_job: str = "superplane-paid-worker"

    @property
    def domain_org_id(self):
        return self.org_id


def bindings() -> tuple[DomainBinding, ...]:
    try:
        data = json.loads(os.environ.get("ADP_DOMAIN_OPERATION_BINDINGS", "[]"))
        if not isinstance(data, list) or len(data) > 64:
            raise ValueError("invalid configured bindings")
        result = []
        for item in data:
            value = DomainBinding(**item)
            if (
                any(not isinstance(v, str) or not v for k, v in item.items() if k != "worker_image_digests")
                or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value.database_schema)
                or value.database_schema == "public"
                or not value.queue_url.startswith("https://sqs.")
                or not value.observation_url.startswith("https://")
                or not value.worker_image_digests
                or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) for digest in value.worker_image_digests)
            ):
                raise ValueError("unsafe configured binding")
            if any((b.domain, b.org_id) == (value.domain, value.org_id) for b in result):
                raise ValueError("ambiguous configured binding")
            result.append(value)
        return tuple(result)
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, "domain operation binding unavailable") from None


def binding_for(domain: str, org_id: str) -> DomainBinding:
    matches = [binding for binding in bindings() if (binding.domain, binding.org_id) == (domain, org_id)]
    if len(matches) != 1:
        raise HTTPException(503, "domain operation binding unavailable")
    return matches[0]


def aws_client(service):
    return boto3.client(
        service,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(connect_timeout=3, read_timeout=15, retries={"total_max_attempts": 2}),
    )


async def secret(secret_id):
    try:
        response = await run_in_threadpool(aws_client("secretsmanager").get_secret_value, SecretId=secret_id)
        value = response["SecretString"]
        if not isinstance(value, str) or not 1 <= len(value) <= 65536:
            raise ValueError("invalid configured secret")
        return value
    except Exception:
        raise HTTPException(503, "domain operation credentials unavailable") from None


async def database_dsn(binding):
    try:
        value = json.loads(await secret(binding.database_secret_id))
        dsn = value["dsn"]
        if set(value) != {"dsn"} or not isinstance(dsn, str) or not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("invalid dedicated domain DSN")
        return dsn
    except (ValueError, KeyError, TypeError):
        raise HTTPException(503, "domain operation database unavailable") from None


def database_ssl():
    return ssl.create_default_context(cafile=os.environ.get("RDS_CA_BUNDLE", "/etc/ssl/certs/rds-global-bundle.pem"))


@asynccontextmanager
async def operation_connect(binding):
    connection = None
    try:
        connection = await asyncpg.connect(
            await database_dsn(binding),
            ssl=database_ssl(),
            timeout=10,
            command_timeout=20,
            server_settings={"search_path": binding.database_schema + ",public"},
        )
        await harness("schema").check_schema_version(connection)
        yield connection
    finally:
        if connection is not None:
            await connection.close()


@asynccontextmanager
async def operation_session(binding):
    dsn = (await database_dsn(binding)).replace("postgres://", "postgresql://", 1).replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(
        dsn,
        connect_args={"ssl": database_ssl(), "server_settings": {"search_path": binding.database_schema + ",public"}, "command_timeout": 20},
        pool_size=1,
        max_overflow=0,
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


def harness(module):
    """Import canonical package source staged by the existing image build step."""
    here = Path(__file__).resolve()
    candidates = [here.parents[2] / "contracts/harness-jobs"]
    if len(here.parents) > 4:
        candidates.insert(0, here.parents[4] / "modules/harness/jobs")
    source = next((path for path in candidates if (path / "harness_jobs/__init__.py").is_file()), None)
    if source is None:
        raise HTTPException(503, "domain operation contract unavailable")
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    return importlib.import_module("harness_jobs." + module)
