"""Installed L5 worker resources; envelopes and local configuration are not authority."""

import asyncio
import base64
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
import json
import os
import re
import ssl
from datetime import UTC, datetime
from hashlib import sha256
from importlib.resources import files
from urllib.parse import urlsplit

import asyncpg
import boto3
import httpx
from botocore.exceptions import ClientError
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from l5_services.product_client import ProductClient
from l5_services.models.configuration import ConfigurationVersion
from l5_services.workers.execution import AuthorityRevoked


SERVICE_IDS = frozenset({"security", "devops", "finops", "transform", "well_architected"})


def _service_id(service_id):
    if service_id not in SERVICE_IDS:
        raise ValueError("unknown installed L5 service")
    if os.environ.get("L5_SERVICE_ID") != service_id:
        raise ValueError("installed L5 service identity does not match")
    return service_id


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required for installed L5 workers")
    return value


def gateway_url():
    url = required("L5_GATEWAY_URL").rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not re.fullmatch(
            r"[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com", parsed.hostname or "")
            or not re.fullmatch(r"/[a-zA-Z0-9_-]+", parsed.path)
            or parsed.netloc != parsed.hostname or parsed.query or parsed.fragment):
        raise ValueError("L5_GATEWAY_URL must be an API Gateway stage URL")
    return url


class SignedGateway(httpx.Auth):
    def __init__(self, region):
        self.region = region

    def auth_flow(self, request):
        credentials = boto3.Session().get_credentials()
        if credentials is None:
            raise RuntimeError("Lambda IAM transport credentials unavailable")
        unsigned = AWSRequest(method=request.method, url=str(request.url), data=request.content,
                              headers={key: value for key, value in request.headers.items()
                                       if key.lower() not in {"authorization", "x-amz-date", "x-amz-security-token"}})
        SigV4Auth(credentials.get_frozen_credentials(), "execute-api", self.region).add_auth(unsigned)
        for key, value in unsigned.headers.items():
            request.headers[key] = value
        yield request


def gateway_transport():
    if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        raise RuntimeError("Gateway transport requires an installed Lambda workload")
    return httpx.AsyncClient(base_url=gateway_url(), auth=SignedGateway(required("AWS_REGION")), timeout=15)


def sessions():
    host = required("L5_RDS_HOST")
    database = required("L5_RDS_DATABASE")
    username = required("L5_RDS_USER")
    region = required("AWS_REGION")
    port = int(required("L5_RDS_PORT"))
    if not 1 <= port <= 65535:
        raise ValueError("invalid RDS port")
    certificate = ssl.create_default_context(cadata=files("l5_services").joinpath("certs/rds-global-bundle.pem").read_text())

    async def connect():
        token = boto3.client("rds", region_name=region).generate_db_auth_token(
            DBHostname=host, Port=port, DBUsername=username, Region=region)
        return await asyncpg.connect(host=host, port=port, user=username, password=token,
                                     database=database, ssl=certificate)

    engine = create_async_engine("postgresql+asyncpg://", async_creator=connect, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


class ScopedSources:
    def __init__(self, transport):
        self.transport = transport

    def _client(self, request):
        return ProductClient(self.transport, request["delegation_id"])

    async def read_source(self, product_id, project_id, snapshot_id, request):
        return await self._client(request).read_source(product_id, project_id, snapshot_id, request)

    async def read_source_manifest(self, product_id, project_id, snapshot_id, request):
        return await self._client(request).read_source_manifest(product_id, project_id, snapshot_id, request)

    async def stream_source(self, product_id, project_id, snapshot_id, request):
        async for chunk in self._client(request).stream_source(product_id, project_id, snapshot_id, request):
            yield chunk


class ArchiveStore:
    def __init__(self, bucket, client, service_id="security"):
        self.bucket = bucket
        self.client = client
        self.service_id = service_id

    async def put_archive(self, run_id, content):
        if not re.fullmatch(r"[0-9a-f-]{36}", run_id):
            raise ValueError("invalid run identity")
        digest = sha256(content).hexdigest()
        key = f"l5/{self.service_id}/work/{run_id}/{digest}.zip"
        response = await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=key,
                                           Body=content, Metadata={"sha256": digest})
        version = response.get("VersionId")
        if not version:
            raise ValueError("worker archive requires versioned storage")
        return {"artifact_id": key, "version": version, "sha256": digest,
                "media_type": "application/zip", "bytes": len(content)}

    async def put_archive_file(self, run_id, path):
        if not re.fullmatch(r"[0-9a-f-]{36}", run_id):
            raise ValueError("invalid run identity")
        def upload():
            digest, size = sha256(), 0
            with open(path, "rb") as content:
                while chunk := content.read(64 * 1024):
                    size += len(chunk)
                    if size > 1088 * 1024 * 1024:
                        raise ValueError("worker archive exceeds size limit")
                    digest.update(chunk)
                key = f"l5/{self.service_id}/work/{run_id}/{digest.hexdigest()}.zip"
                content.seek(0)
                try:
                    response = self.client.put_object(
                        Bucket=self.bucket, Key=key, Body=content, ContentLength=size,
                        Metadata={"sha256": digest.hexdigest()}, IfNoneMatch="*",
                        ChecksumSHA256=base64.b64encode(digest.digest()).decode())
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
                        raise
                    response = self.client.head_object(Bucket=self.bucket, Key=key)
                version = response.get("VersionId")
                if not version or version == "null":
                    raise ValueError("worker archive requires versioned storage")
                return {"artifact_id": key, "version": version, "sha256": digest.hexdigest(),
                        "media_type": "application/zip", "bytes": size}
        reference = await asyncio.to_thread(upload)
        # The stored object, including a pre-existing conditional-write collision,
        # must independently reproduce the sealed artifact before use.
        async with self.get_archive_file(run_id, reference):
            pass
        return reference

    @asynccontextmanager
    async def get_archive_file(self, run_id, reference):
        key = reference["artifact_id"]
        if (not re.fullmatch(r"[0-9a-f-]{36}", run_id)
                or not key.startswith(f"l5/{self.service_id}/work/{run_id}/")
                or not key.endswith(f"/{reference['sha256']}.zip")
                or type(reference["bytes"]) is not int or not 0 <= reference["bytes"] <= 1088 * 1024 * 1024
                or not reference.get("version") or reference["version"] == "null"):
            raise ValueError("archive belongs to another run or exceeds bounds")
        with tempfile.TemporaryDirectory(prefix="l5-archive-") as directory:
            path = Path(directory) / "archive.zip"
            def download():
                result = self.client.get_object(Bucket=self.bucket, Key=key, VersionId=reference["version"])
                body = result["Body"]
                try:
                    if (result.get("VersionId") != reference["version"]
                            or result.get("ContentLength") != reference["bytes"]):
                        raise ValueError("worker archive version or size changed")
                    digest, size = sha256(), 0
                    with path.open("wb") as target:
                        while chunk := body.read(64 * 1024):
                            size += len(chunk)
                            if size > reference["bytes"]:
                                raise ValueError("worker archive content exceeds receipt")
                            digest.update(chunk)
                            target.write(chunk)
                    if size != reference["bytes"] or digest.hexdigest() != reference["sha256"]:
                        raise ValueError("worker archive content changed")
                finally:
                    body.close()
            await asyncio.to_thread(download)
            yield path

    async def get_archive(self, run_id, reference):
        key = reference["artifact_id"]
        if not key.startswith(f"l5/{self.service_id}/work/{run_id}/") or not key.endswith(f"/{reference['sha256']}.zip"):
            raise ValueError("archive belongs to another run")
        result = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key,
                                         VersionId=reference["version"])
        if result.get("VersionId") != reference["version"] or result.get("ContentLength", 1 << 30) > 128 * 1024 * 1024:
            raise ValueError("worker archive version or size changed")
        content = await asyncio.to_thread(result["Body"].read)
        if len(content) != reference["bytes"] or sha256(content).hexdigest() != reference["sha256"]:
            raise ValueError("worker archive content changed")
        return content


def archive_store(service_id="security"):
    _service_id(service_id)
    return ArchiveStore(required("L5_ARCHIVE_BUCKET"), boto3.client("s3", region_name=required("AWS_REGION")), service_id)


def queue_publisher(service_id="security"):
    _service_id(service_id)
    queue = required("L5_WORK_QUEUE_URL")
    sqs = boto3.client("sqs", region_name=required("AWS_REGION"))

    async def publish(envelope, delay):
        await asyncio.to_thread(sqs.send_message, QueueUrl=queue,
                                MessageBody=json.dumps(envelope, sort_keys=True), DelaySeconds=delay)

    return publish


def provider_session(credentials, region):
    return boto3.Session(aws_access_key_id=credentials["access_key_id"],
                         aws_secret_access_key=credentials["secret_access_key"],
                         aws_session_token=credentials["session_token"], region_name=region)


def require_service(service_id, work):
    if (service_id not in SERVICE_IDS or work.service_id != service_id
            or os.environ.get("L5_SERVICE_ID") not in (None, service_id)):
        raise AuthorityRevoked("installed worker does not own this L5 work")


async def verify_source_receipts(run, claim, sources, service_id):
    require_service(service_id, run)
    admission = run.admission
    snapshots = admission.get("source_snapshots") or []
    if not snapshots or len(snapshots) != len(run.input["sources"]):
        raise AuthorityRevoked("work has no pinned Product sources")
    for snapshot, selected_source in zip(snapshots, run.input["sources"], strict=True):
        if snapshot.get("source_ref") != selected_source:
            raise AuthorityRevoked("work source changed")
        request = {"delegation_id": admission["delegation_id"],
                   "delegation_revision": admission["delegation_version"],
                   "operation_id": run.run_id, "attempt_id": claim.work_id,
                   "lease_owner": claim.owner, "lease_fence": claim.fence}
        receipt = await sources.read_source(run.product_id, run.project_id, snapshot["snapshot_id"], request)
        if receipt != snapshot:
            raise AuthorityRevoked("work source receipt changed")


async def run_provider_session(run, claim, transport, sources, provider_factory, *, service_id, allowed_clients):
    require_service(service_id, run)
    has_sources = bool(run.input.get("sources"))
    admission = run.admission if isinstance(run.admission, dict) else {}
    native_space = admission.get("native_space_ref")
    connection = admission.get("connection_ref")
    source_free_devops = (
        service_id == "devops"
        and run.input.get("capability") in {"investigation", "release_verification"}
        and run.input.get("sources", []) == []
        and not admission.get("source_snapshots")
        and isinstance(native_space, dict)
        and isinstance(connection, dict)
        and native_space.get("record_type") == "agent_space"
        and native_space.get("record_id")
        and (native_space.get("tenant_id"), native_space.get("project_id"), native_space.get("source_connection_id"))
        == (run.tenant_id, run.project_id, connection.get("source_connection_id"))
    )
    if not has_sources and service_id != "finops" and not source_free_devops:
        raise AuthorityRevoked("work has no pinned Product sources")
    scope = admission.get("scope") if isinstance(admission, dict) else None
    if not isinstance(scope, dict) or any(
            scope.get(field) != getattr(run, field) for field in ("tenant_id", "product_id", "project_id")):
        raise AuthorityRevoked("work admission scope changed")
    connection = admission["connection_ref"]
    authority = admission
    if service_id == "devops":
        from l5_services.devops.recovery import readback_authority

        authority = readback_authority(run, getattr(claim, "kind", None), datetime.now(UTC))
    request = {"delegation_id": authority["delegation_id"],
               "delegation_revision": authority["delegation_version"],
               "operation_id": run.run_id, "attempt_id": claim.work_id,
               "lease_owner": claim.owner, "lease_fence": claim.fence,
               "connection_ref": {**connection, "record_type": "credential"}}
    identity = admission["provider_identity"]

    async def verify_broker():
        response = await transport.post("/internal/v1/l5/credential-session", json=request)
        response.raise_for_status()
        result = response.json()
        selected = result.get("provider_identity") or {}
        if (result.get("operation_id") != run.run_id or result.get("attempt_id") != claim.work_id
                or result.get("delegation_revision") != authority["delegation_version"]
                or any(selected.get(field) != identity.get(field) or not identity.get(field)
                       for field in ("account_id", "role_arn", "region"))):
            raise AuthorityRevoked("broker returned another work or provider identity")
        try:
            deadline = datetime.fromisoformat(result["expires_at"])
        except (TypeError, KeyError, ValueError, AttributeError) as error:
            raise AuthorityRevoked("broker session expiry unavailable") from error
        if deadline.tzinfo is None or deadline <= datetime.now(UTC):
            raise AuthorityRevoked("broker session expired")
        return result, deadline

    result, deadline = await verify_broker()
    try:
        provider = provider_factory(result, identity["region"])
    except (KeyError, TypeError, ValueError) as error:
        raise AuthorityRevoked("broker session credentials unavailable") from error
    loop = asyncio.get_running_loop()

    async def verify_current():
        from l5_services.security_recovery import recovery_deadline

        # Late reads use the sealed manifest; the broker checks current source
        # rights without rereading source bytes whose capture lease has expired.
        recovering = recovery_deadline(run, getattr(claim, "kind", None), datetime.now(UTC)) is not None
        if has_sources and not recovering:
            await verify_source_receipts(run, claim, sources, service_id)
        if service_id == "security" or not has_sources:
            await verify_broker()

    class NativeClient:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            method = getattr(self.client, name)
            if not callable(method):
                return method

            def guarded(*args, **kwargs):
                if datetime.now(UTC) >= deadline:
                    raise AuthorityRevoked("broker session expired")
                verification = asyncio.run_coroutine_threadsafe(verify_current(), loop)
                try:
                    verification.result(timeout=35 if service_id == "security" else 12)
                except Exception as error:
                    verification.cancel()
                    raise AuthorityRevoked("current Product work unavailable") from error
                if datetime.now(UTC) >= deadline:
                    raise AuthorityRevoked("broker session expired")
                return method(*args, **kwargs)

            return guarded

    class ScopedSession:
        account_id = identity["account_id"]
        role_arn = identity["role_arn"]
        region = identity["region"]

        def client(self, service, *, config=None):
            if datetime.now(UTC) >= deadline:
                raise AuthorityRevoked("broker session expired")
            if service not in allowed_clients:
                raise AuthorityRevoked("provider client outside service scope")
            options = {"config": config} if config is not None else {}
            return NativeClient(provider.client(service, region_name=self.region, **options))

    return ScopedSession()


async def setup_provider_session(intent, claim, transport, sessions, provider_factory, *, service_id, client_name, allowed_clients=None):
    from fastapi import HTTPException
    from l5_services.setup_authority import setup_authorization

    require_service(service_id, intent)
    if intent.operation_id != claim.operation_id:
        raise AuthorityRevoked("installed worker does not own this setup")
    async with sessions() as session:
        authority = await setup_authorization(session, intent)
        version = await session.get(ConfigurationVersion, (
            intent.tenant_id, intent.configuration_id, intent.request["configuration_revision"]))
        if version is None:
            raise AuthorityRevoked("setup configuration unavailable")
        selected = version.settings.get("devops", {}) if service_id == "devops" else version.settings
        connection = selected.get("hosting_connection_ref" if service_id == "devops" else "connection_ref") or {}
    request = {"delegation_id": authority["delegation_id"],
               "delegation_revision": authority["delegation_revision"],
               "operation_id": intent.operation_id, "attempt_id": claim.work_id,
               "lease_owner": claim.owner, "lease_fence": claim.fence,
               "connection_ref": {**connection, "record_type": "credential"}}

    async def verify():
        async with sessions() as session:
            if await setup_authorization(session, intent) != authority:
                raise AuthorityRevoked("setup continuation changed")
        response = await transport.post("/internal/v1/l5/credential-session", json=request)
        if response.status_code in {401, 403, 404, 409, 410}:
            raise HTTPException(response.status_code, detail={"code": "setup_authority_denied"})
        response.raise_for_status()
        result = response.json()
        verified = result.get("setup_verified") or {}
        identity = result.get("provider_identity") or {}
        if (result.get("operation_id") != intent.operation_id or result.get("attempt_id") != claim.work_id
                or result.get("delegation_revision") != authority["delegation_revision"]
                or verified.get("configuration_id") != intent.configuration_id
                or verified.get("configuration_revision") != intent.request["configuration_revision"]
                or verified.get("scope") != intent.scope or verified.get("connection_ref") != connection
                or verified.get("authorization") != authority
                or any(verified.get(field) != identity.get(field) or not identity.get(field)
                       for field in ("account_id", "role_arn", "region"))):
            raise AuthorityRevoked("setup broker returned another scope")
        try:
            deadline = datetime.fromisoformat(result["expires_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise AuthorityRevoked("setup session expiry unavailable") from error
        if deadline.tzinfo is None or deadline <= datetime.now(UTC):
            raise AuthorityRevoked("setup session expired")
        return result, verified, identity, deadline

    result, verified, identity, deadline = await verify()
    try:
        provider = provider_factory(result, identity["region"])
    except (KeyError, TypeError, ValueError) as error:
        raise AuthorityRevoked("setup session credentials unavailable") from error
    loop = asyncio.get_running_loop()

    class ScopedClient:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            method = getattr(self.client, name)
            if not callable(method):
                return method

            def guarded(*args, **kwargs):
                if datetime.now(UTC) >= deadline:
                    raise AuthorityRevoked("setup session expired")
                verification = asyncio.run_coroutine_threadsafe(verify(), loop)
                try:
                    verification.result(timeout=12)
                except Exception as error:
                    verification.cancel()
                    raise AuthorityRevoked("current setup work unavailable") from error
                return method(*args, **kwargs)

            return guarded

    class ScopedSession:
        def client(self, name, **kwargs):
            if name not in allowed_clients or kwargs:
                raise AuthorityRevoked("setup provider client outside selection")
            return ScopedClient(provider.client(name, region_name=identity["region"]))

    if allowed_clients is not None:
        return verified, identity, ScopedSession(), deadline
    return verified, identity, ScopedClient(provider.client(client_name, region_name=identity["region"]))
