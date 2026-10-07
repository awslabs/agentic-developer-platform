"""Immutable chat artifacts; object locations never act as download authority."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Annotated

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, Identifier, _decode, _encode, _launch_json
from src.agentauth.chat_history_store import ChatHistoryStore, history_version
from src.agentauth.chat_storage import authority_checks, snapshot_condition
from src.agentauth.run_credential import CredentialError, _key
from src.orchestration.chat_data_migration import _matches_owner_fields, _owner_fields, _safe_segment, _safe_session

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
ARTIFACT_TTL = 30 * 86400
ArtifactId = Annotated[str, Field(pattern=r"^art_[a-f0-9]{12,64}$")]
Filename = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^/\\\x00-\x1f\x7f]+$")]
ContentType = Annotated[str, Field(max_length=128, pattern=r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:; charset=[A-Za-z0-9_.-]+)?$")]


class ChatArtifactConflictError(Exception):
    """The idempotency key, object bytes or transaction fence changed."""


class ChatArtifactMissingError(Exception):
    """An authorized artifact's catalog record or object is missing or expired."""


class ChatArtifactInputError(Exception):
    """The bounded upload does not match its declared encoding or checksum."""


class ArtifactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: Identifier
    session_id: Identifier
    idempotency_key: Identifier
    filename: Filename
    content_type: ContentType
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_base64: str = Field(min_length=4, max_length=4 * ((MAX_ARTIFACT_BYTES + 2) // 3))
    supersedes: ArtifactId | None = None


class ArtifactList(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: Identifier
    session_id: Identifier
    content_type: ContentType | None = None
    filename: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=100, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)


class _ListCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    after: str = Field(max_length=512, pattern=r"^art#[^#]+#art_[a-f0-9]{12,64}$")
    issued_at: int = Field(ge=1)
    expires_at: int = Field(ge=1)


class _Artifact(BaseModel):
    id: ArtifactId
    filename: Filename
    content_type: ContentType = Field(alias="contentType")
    size_bytes: int = Field(ge=1, le=MAX_ARTIFACT_BYTES, alias="sizeBytes")
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: str = Field(alias="createdAt")
    source: str = Field(pattern=r"^(agent|user)$")
    supersedes: ArtifactId | None = None


def artifact_prefix(header: dict, session_id: str) -> str:
    tenant, team, user = (header[field] for field in ("tenantId", "teamId", "ownerUserId"))
    if not _safe_session(session_id) or not all(_safe_segment(value) for value in (tenant, user)) or not (team == "" or _safe_segment(team)):
        raise ChatAuthorizationRefusedError("artifact owner path unavailable")
    return f"o/{tenant}/t/{team or '~personal'}/u/{user}/s/{session_id}/"


class ChatArtifactStore:
    def __init__(self, authority, capabilities, table, storage, bucket: str, clock):
        self.authority, self.table, self.storage, self.bucket, self.clock = authority, table, storage, bucket, clock
        self.history = ChatHistoryStore(authority.context_table, capabilities)

    def _owned(self, token, run_id, session_id):
        now = self.clock()
        launch = self.history.capabilities.verify(token, run_id=run_id, session_id=session_id, operation="artifact.create", now=now)
        _, header = self.history._authorize(token, run_id, session_id, "artifact.create", now)
        if (header["tenantId"], header["teamId"], header["ownerUserId"]) != (launch.tenant_id, launch.team_id, launch.user_id):
            raise ChatAuthorizationRefusedError("artifact write ownership refused")
        return launch, header

    def _check(self, row, header, session_id):
        owner = (header["tenantId"], header["teamId"], header["ownerUserId"])
        prefix = artifact_prefix(header, session_id)
        path = row.get("s3Key")
        if (
            row.get("PK") != header["PK"]
            or not _matches_owner_fields(row, owner)
            or any(row.get(field) != expected for field, expected in zip(("org_id", "team_id", "user_id"), owner, strict=True))
            or not isinstance(path, str)
            or not path.startswith(prefix)
            or len(path) <= len(prefix)
            or ".." in path
            or "\\" in path
        ):
            raise ChatAuthorizationRefusedError("artifact ownership unavailable")
        artifact = _Artifact.model_validate(row).model_dump(by_alias=True, exclude_none=True)
        if row.get("SK") != f"art#{artifact['createdAt']}#{artifact['id']}":
            raise ChatAuthorizationRefusedError("artifact catalog binding refused")
        ttl = row.get("ttl")
        if isinstance(ttl, bool) or ttl is None or int(ttl) != ttl:
            raise ChatAuthorizationUnavailableError("artifact retention unavailable")
        if ttl <= self.clock():
            raise ChatArtifactMissingError("artifact expired")
        if row.get("scanStatus", "not_scanned") not in {"not_scanned", "clean"}:
            raise ChatAuthorizationRefusedError("artifact quarantined")
        return artifact

    def _load(self, artifact_id, session_id, header):
        pointer = self.history._get(session_id, f"artifact-id#{artifact_id}")
        if pointer is not None:
            self.history._check_row(pointer, header)
            sort_key = pointer.get("catalogKey")
            if not isinstance(sort_key, str) or not sort_key.startswith("art#"):
                raise ChatAuthorizationUnavailableError("artifact locator unavailable")
            row = self.table.get_item(Key={"PK": header["PK"], "SK": sort_key}, ConsistentRead=True).get("Item")
        else:
            query = {
                "KeyConditionExpression": Key("PK").eq(header["PK"]) & Key("SK").begins_with("art#"),
                "FilterExpression": Attr("id").eq(artifact_id),
                "ConsistentRead": True,
                "Limit": 100,
            }
            matches = []
            for _page in range(10):
                page = self.table.query(**query)
                matches.extend(page.get("Items", []))
                if not page.get("LastEvaluatedKey"):
                    break
                query["ExclusiveStartKey"] = page["LastEvaluatedKey"]
            else:
                raise ChatAuthorizationUnavailableError("legacy artifact lookup incomplete")
            if len(matches) > 1:
                raise ChatAuthorizationRefusedError("artifact reference ambiguous")
            row = matches[0] if matches else None
        if row is None:
            raise ChatArtifactMissingError("artifact catalog missing")
        self._check(row, header, session_id)
        if row["id"] != artifact_id:
            raise ChatAuthorizationRefusedError("artifact locator mismatch")
        return row

    def _bytes(self, path):
        try:
            response = self.storage.get_object(Bucket=self.bucket, Key=path)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                raise ChatArtifactMissingError("artifact object missing") from None
            raise
        stream = response["Body"]
        try:
            if response["ContentLength"] > MAX_ARTIFACT_BYTES:
                raise ChatAuthorizationUnavailableError("artifact object exceeds bound")
            content = stream.read(MAX_ARTIFACT_BYTES + 1)
        finally:
            stream.close()
        if len(content) != response["ContentLength"] or len(content) > MAX_ARTIFACT_BYTES:
            raise ChatAuthorizationUnavailableError("artifact object incomplete")
        return content, response

    def _reference(self, row, header, session_id, run_id):
        artifact = self._check(row, header, session_id)
        artifact.update(
            url=f"/v1/chat/data/artifact/{session_id}/{row['id']}?run_id={run_id}",
            urlExpiresAt=datetime.fromtimestamp(int(row["ttl"]), UTC).isoformat(),
            scanStatus=row.get("scanStatus", "not_scanned"),
        )
        return artifact

    def _list_binding(self, launch, header, request):
        scope = json.dumps(
            {
                "resource": self.history._resource_digest(header),
                "version": history_version(header),
                "acl": sorted(header.get("aclUserIds", [])),
                "ttl": int(header["ttl"]),
            },
            sort_keys=True,
        )
        return hashlib.sha256((_launch_json(launch) + scope + request.model_dump_json(exclude={"cursor"})).encode()).hexdigest()

    def _list_mac(self, body):
        try:
            return _encode(hmac.new(_key(self.history.capabilities.env), f"chat-artifact-page-v1.{body}".encode(), hashlib.sha256).digest())
        except CredentialError:
            raise ChatAuthorizationUnavailableError("artifact cursor signing unavailable") from None

    def list_page(self, token, request: ArtifactList):
        now = self.clock()
        launch, header = self.history._authorize(token, request.run_id, request.session_id, "artifact.read", now)
        binding = self._list_binding(launch, header, request)
        query = {
            "KeyConditionExpression": Key("PK").eq(header["PK"]) & Key("SK").begins_with("art#"),
            "ConsistentRead": True,
            "ScanIndexForward": False,
            "Limit": request.limit,
        }
        if request.cursor:
            try:
                body, signature = request.cursor.split(".")
                if not hmac.compare_digest(self._list_mac(body), signature):
                    raise ValueError
                claims = _ListCursor.model_validate_json(_decode(body))
                if claims.binding != binding or not claims.issued_at <= now < claims.expires_at <= min(claims.issued_at + 300, launch.expires_at):
                    raise ValueError
                query["ExclusiveStartKey"] = {"PK": header["PK"], "SK": claims.after}
            except (ValueError, TypeError, UnicodeError):
                raise ChatAuthorizationRefusedError("artifact cursor refused") from None
        page = self.table.query(**query)
        rows, missing = [], []
        for row in page.get("Items", []):
            try:
                self._check(row, header, request.session_id)
            except ChatArtifactMissingError:
                missing.append(row["id"])
                continue
            if request.content_type is not None and row["contentType"] != request.content_type:
                continue
            if request.filename is not None and request.filename not in row["filename"]:
                continue
            current = self.table.get_item(Key={"PK": header["PK"], "SK": row["SK"]}, ConsistentRead=True).get("Item")
            if current is None:
                missing.append(row["id"])
                continue
            if current != row:
                self._check(current, header, request.session_id)
                raise ChatArtifactConflictError("artifact changed during listing")
            rows.append(current)
        now = self.clock()
        launch, current_header = self.history._authorize(token, request.run_id, request.session_id, "artifact.read", now)
        if self._list_binding(launch, current_header, request) != binding:
            raise ChatArtifactConflictError("artifact listing scope changed")
        entries = []
        for row in rows:
            try:
                entries.append(self._reference(row, current_header, request.session_id, request.run_id))
            except ChatArtifactMissingError:
                missing.append(row["id"])
        cursor = None
        if continuation := page.get("LastEvaluatedKey"):
            if continuation.get("PK") != header["PK"]:
                raise ChatAuthorizationUnavailableError("artifact page unavailable")
            claims = _ListCursor(binding=binding, after=continuation["SK"], issued_at=now, expires_at=min(now + 300, launch.expires_at))
            body = _encode(claims.model_dump_json().encode())
            cursor = f"{body}.{self._list_mac(body)}"
        return {
            "status": "partial" if cursor or missing else "ok" if entries else "empty",
            "entries": entries,
            "next_cursor": cursor,
            "observed_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "coverage": {"source": "session_artifacts", "complete": not bool(cursor or missing), "missing_source_ids": missing},
        }

    def download(self, token, *, run_id, session_id, artifact_id):
        _, header = self.history._authorize(token, run_id, session_id, "artifact.read", self.clock())
        row = self._load(artifact_id, session_id, header)
        content, response = self._bytes(row["s3Key"])
        if (
            len(content) != row["sizeBytes"]
            or hashlib.sha256(content).hexdigest() != row["checksum"]
            or response["ContentType"] != row["contentType"]
        ):
            raise ChatAuthorizationUnavailableError("artifact object integrity refused")
        _, current_header = self.history._authorize(token, run_id, session_id, "artifact.read", self.clock())
        current = self._load(artifact_id, session_id, current_header)
        if row != current:
            raise ChatArtifactConflictError("artifact changed during download")
        return content, self._reference(current, current_header, session_id, run_id)

    def _receipt(self, request, header, receipt_key, digest):
        receipt = self.history._get(request.session_id, receipt_key)
        if receipt is None:
            return None
        self.history._check_row(receipt, header)
        if receipt.get("requestDigest") != digest:
            raise ChatArtifactConflictError("artifact idempotency key reused")
        row = self._load(receipt["artifactId"], request.session_id, header)
        return self._reference(row, header, request.session_id, request.run_id)

    def create(self, token, request: ArtifactCreate):
        launch, header = self._owned(token, request.run_id, request.session_id)
        try:
            content = base64.b64decode(request.content_base64, validate=True)
        except ValueError:
            raise ChatArtifactInputError("invalid artifact encoding") from None
        if not 0 < len(content) <= MAX_ARTIFACT_BYTES or hashlib.sha256(content).hexdigest() != request.content_sha256:
            raise ChatArtifactInputError("artifact content or checksum invalid")
        payload = request.model_dump(exclude={"content_base64", "run_id"})
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt_key = "artifact-write#" + hashlib.sha256(request.idempotency_key.encode()).hexdigest()
        if receipt := self._receipt(request, header, receipt_key, digest):
            return receipt
        superseded = self._load(request.supersedes, request.session_id, header) if request.supersedes else None
        artifact_id = (
            "art_" + hashlib.sha256(f"{header['PK']}#{launch.tenant_id}#{launch.user_id}#{request.idempotency_key}".encode()).hexdigest()[:32]
        )
        path = artifact_prefix(header, request.session_id) + f"gateway/out/{artifact_id}"
        stamp = datetime.fromtimestamp(self.clock(), UTC).isoformat()
        metadata = {"artifact-id": artifact_id, "request-digest": digest, "created-at": stamp}
        try:
            self.storage.put_object(Bucket=self.bucket, Key=path, Body=content, ContentType=request.content_type, Metadata=metadata, IfNoneMatch="*")
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
                raise
            existing, response = self._bytes(path)
            metadata = response.get("Metadata", {})
            if (
                existing != content
                or response["ContentType"] != request.content_type
                or metadata.get("artifact-id") != artifact_id
                or metadata.get("request-digest") != digest
            ):
                raise ChatArtifactConflictError("artifact object already has another request") from None
            stamp = metadata["created-at"]
        created = datetime.fromisoformat(stamp)
        if created.tzinfo is None:
            raise ChatAuthorizationUnavailableError("artifact timestamp unavailable")
        launch, header = self._owned(token, request.run_id, request.session_id)
        owner = _owner_fields((launch.tenant_id, launch.team_id, launch.user_id))
        provenance = {**owner, "PK": header["PK"], "runId": launch.run_id, "leaseGeneration": launch.lease_generation}
        row = {
            **provenance,
            "SK": f"art#{stamp}#{artifact_id}",
            "id": artifact_id,
            "filename": request.filename,
            "contentType": request.content_type,
            "sizeBytes": len(content),
            "checksum": request.content_sha256,
            "createdAt": stamp,
            "source": "agent",
            "scanStatus": "not_scanned",
            "s3Key": path,
            "ttl": int(created.timestamp()) + ARTIFACT_TTL,
        }
        if superseded:
            row["supersedes"] = superseded["id"]
        self._check(row, header, request.session_id)
        pointer = {**provenance, "SK": f"artifact-id#{artifact_id}", "catalogKey": row["SK"]}
        receipt = {**provenance, "SK": receipt_key, "requestDigest": digest, "artifactId": artifact_id}
        transaction = [
            {"Put": {"TableName": table, "Item": _encoded(item), "ConditionExpression": "attribute_not_exists(PK)"}}
            for table, item in ((self.table.name, row), (self.history.table.name, pointer), (self.history.table.name, receipt))
        ]
        if superseded:
            transaction.append(
                {
                    "ConditionCheck": {
                        "TableName": self.table.name,
                        "Key": _encoded({"PK": header["PK"], "SK": superseded["SK"]}),
                        **snapshot_condition(superseded),
                    }
                }
            )
        try:
            self.authority.store.client.transact_write_items(TransactItems=transaction + authority_checks(self.authority, launch, self.clock()))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in error.response.get("CancellationReasons", [])
            ):
                _, latest = self._owned(token, request.run_id, request.session_id)
                if receipt := self._receipt(request, latest, receipt_key, digest):
                    return receipt
                raise ChatArtifactConflictError("artifact publication changed; retry after rereading") from None
            raise ChatAuthorizationUnavailableError("artifact publication unavailable") from None
        return self._reference(row, header, request.session_id, request.run_id)
