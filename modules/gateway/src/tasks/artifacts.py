"""``POST /v1/task-artifacts`` and ``GET /v1/tasks/{id}/artifacts/{artifact_id}``.

Artifacts are how evidence enters and leaves a task: a caller uploads logs or
configuration for an investigator to read, and the investigator's findings
reference the artifacts they came from. T6 owns both additive routes (design
section 5).

Two properties shape this module, and each rules out the obvious shortcut.

**No presigned URL, in either direction.** The contract forbids it structurally —
``artifact_upload_response.download_url`` is ``{"not": {}}``, a field that can
never validate — and there is a rejected fixture for a response that includes one.
The reason is in section 5: bytes "stream through the gateway with periodic access
checks". A presigned URL is a bearer token for an object, valid for its whole
lifetime, that survives revocation of the credential that obtained it and leaves
no record of who read what. Streaming through the gateway costs bandwidth and buys
an access decision per read.

**The gateway derives the storage location.** The caller supplies a content type,
a digest and bytes. Owner, tenant, artifact ID and key are all server-derived, and
the key embeds hashed tenant and principal (section 5:
``tasks/<tenant-hash>/<principal-hash>/<artifact-id>/<version>``). A caller-supplied
key is a path-traversal surface and an authorization bypass at once; there is a
rejected fixture for a caller-supplied owner for the same reason.

Design reference: implementation-design.md section 5;
``public-api.schema.json#/$defs/artifact_upload_request``/``artifact_upload_response``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database import get_db
from src.tasks import authz, errors, http
from src.tasks.events import SCHEMA_VERSION, format_timestamp, utc_now
from src.tasks.limits import (
    MAX_INPUT_ARTIFACT_BYTES,
    PERMITTED_ARTIFACT_CONTENT_TYPES,
    UNCLAIMED_UPLOAD_EXPIRY_HOURS,
)
from src.tasks.routes import get_store
from src.tasks.store import ArtifactRecord, TaskStoreError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["task-api"])

#: ``base64`` inflates by 4/3, and the upload route accepts a JSON body carrying
#: the bytes encoded. Bounding the encoded form separately means an oversize
#: upload is refused on its framing rather than after a full decode.
MAX_ENCODED_ARTIFACT_BYTES = (MAX_INPUT_ARTIFACT_BYTES * 4 // 3) + 1024


def storage_key(*, tenant_id: str, principal_id: str, artifact_id: str, version: int) -> str:
    """Derive ``tasks/<tenant-hash>/<principal-hash>/<artifact-id>/<version>``.

    Hashed rather than literal, because a tenant ID or canonical principal ID in
    an object key is customer-identifying data readable by anyone with bucket
    listing — including in access logs and inventory reports. The hash keeps the
    prefix partitioning the design wants without publishing who owns what.

    Truncated to 32 hex characters: this is a partition key, not a security
    boundary (authorization is the ``TASK_ARTIFACT`` binding, checked on every
    read), and 128 bits is far past any collision concern for a per-tenant prefix.
    """
    tenant_hash = hashlib.sha256(tenant_id.encode()).hexdigest()[:32]
    principal_hash = hashlib.sha256(principal_id.encode()).hexdigest()[:32]
    return f"tasks/{tenant_hash}/{principal_hash}/{artifact_id}/{version}"


def decode_content(body: dict) -> bytes:
    """Decode and verify the uploaded bytes against the caller's own digest.

    The digest is checked here rather than trusted, and that check is the whole
    point of requiring it. An investigator's report cites artifacts by ID and
    digest as evidence; if the stored bytes could differ from the digest recorded
    alongside them, every such citation would be unverifiable. Checking on the way
    in means a corrupted upload is a refusal instead of a permanently
    mis-attributed piece of evidence.
    """
    encoded = body.get("content_base64")
    if not isinstance(encoded, str) or not encoded:
        raise errors.invalid_request("content_base64 is required.")
    if len(encoded) > MAX_ENCODED_ARTIFACT_BYTES:
        raise errors.payload_too_large(f"An input artifact may not exceed {MAX_INPUT_ARTIFACT_BYTES} bytes.")

    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise errors.invalid_request("content_base64 is not valid base64.") from None

    if not content:
        raise errors.invalid_request("An artifact may not be empty.")
    if len(content) > MAX_INPUT_ARTIFACT_BYTES:
        raise errors.payload_too_large(f"An input artifact may not exceed {MAX_INPUT_ARTIFACT_BYTES} bytes.")

    declared_length = body.get("content_length")
    if declared_length != len(content):
        raise errors.invalid_request("content_length does not match the supplied content.")

    digest = hashlib.sha256(content).hexdigest()
    if body.get("content_sha256") != digest:
        # The computed digest is deliberately not returned. Echoing it would turn
        # this route into an oracle that confirms the hash of arbitrary bytes the
        # caller is guessing at.
        raise errors.invalid_request("content_sha256 does not match the supplied content.")

    return content


def check_upload_metadata(body: dict) -> None:
    """Reject unknown fields and unpermitted content types.

    The unknown-field check mirrors the schema's ``additionalProperties: false``,
    and it is what stops a caller from *attempting* the forbidden fields: a body
    carrying ``tenant_id``, ``owner_principal_id`` or a storage key is refused
    rather than having those fields quietly ignored. Ignoring them would leave a
    caller believing it had set an owner the gateway actually derived itself.
    """
    permitted = {"schema_version", "content_type", "content_sha256", "content_length", "content_base64", "filename"}
    unknown = sorted(set(body) - permitted)
    if unknown:
        raise errors.invalid_request(f"Fields not permitted on an artifact upload: {', '.join(unknown)}")
    if body.get("schema_version") != SCHEMA_VERSION:
        raise errors.invalid_request("schema_version must be 1.0.")
    if body.get("content_type") not in PERMITTED_ARTIFACT_CONTENT_TYPES:
        raise errors.invalid_request("Investigator v1 accepts text/plain and application/json only.")
    filename = body.get("filename")
    if filename is not None and (not isinstance(filename, str) or len(filename) > 256):
        raise errors.invalid_request("filename must be a string of at most 256 characters.")


@router.post("/v1/task-artifacts")
@http.contract_errors
async def upload_artifact(request: Request, db: AsyncSession = Depends(get_db)):
    """Store bytes and return their immutable reference.

    Requires the artifacts scope only. Read scope is not required because an
    upload is not a read, and demanding it would force a producer that only ever
    supplies evidence to hold a credential that can also read task state.
    """
    http.require_flag(http.FLAG_READ)
    context, scopes = authz.authenticate(request)
    caller = await authz.resolve_caller(context, scopes, db)
    caller.require(authz.SCOPE_ARTIFACTS)

    raw = await request.body()
    if len(raw) > MAX_ENCODED_ARTIFACT_BYTES + 4096:
        raise errors.payload_too_large(f"An input artifact may not exceed {MAX_INPUT_ARTIFACT_BYTES} bytes.")

    try:
        body = http.parse_json_object(raw)
    except ValueError:
        raise errors.invalid_request("The request body is not a JSON object.") from None

    check_upload_metadata(body)
    content = decode_content(body)

    now = utc_now()
    artifact_id = f"art_{uuid.uuid4()}"
    record = ArtifactRecord(
        artifact_id=artifact_id,
        version=1,
        tenant_id=caller.tenant_id,
        owner_principal_id=caller.principal_id,
        content_type=body["content_type"],
        content_sha256=body["content_sha256"],
        content_length=len(content),
        created_at=format_timestamp(now),
        # Unclaimed uploads expire after 24 hours; a referenced artifact receives
        # the task's retention, applied at admission by the route that binds it.
        # Recorded at upload so an artifact that is never attached to a task has a
        # definite end rather than living forever as unowned bytes.
        expires_at=format_timestamp(now + timedelta(hours=UNCLAIMED_UPLOAD_EXPIRY_HOURS)),
        task_id=None,
        storage_key=storage_key(tenant_id=caller.tenant_id, principal_id=caller.principal_id, artifact_id=artifact_id, version=1),
    )

    try:
        stored = get_store().put_artifact(record=record, content=content)
    except TaskStoreError:
        logger.warning("Task API artifact upload failed: storage unavailable", exc_info=True)
        raise errors.prerequisite_unavailable("Artifact storage is unavailable.") from None

    return http.ok(
        {
            "schema_version": SCHEMA_VERSION,
            "artifact_id": stored.artifact_id,
            "version": stored.version,
            "content_sha256": stored.content_sha256,
            "content_type": stored.content_type,
            "created_at": stored.created_at,
            "expires_at": stored.expires_at,
            "request_id": http.request_id(request),
        },
        status=201,
    )


@router.get("/v1/tasks/{task_id}/artifacts/{artifact_id}")
@http.contract_errors
async def download_artifact(task_id: str, artifact_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Stream artifact bytes through the gateway after authorizing the binding.

    ``authorize_artifact`` requires read *and* artifacts scope, task ownership, and
    the exact artifact-to-task binding — so an artifact ID readable through one
    task cannot be read through another the caller also owns.

    ``Content-Disposition: attachment`` with ``X-Content-Type-Options: nosniff``
    because these bytes are caller-supplied and may be served back to a browser. A
    ``text/plain`` artifact whose content a browser chose to sniff as HTML would
    execute in the gateway's origin; the two headers together make the response a
    download rather than a document.
    """
    http.require_flag(http.FLAG_READ)
    context, scopes = authz.authenticate(request)
    caller = await authz.resolve_caller(context, scopes, db)

    store = get_store()
    artifact = authz.authorize_artifact(caller, store, task_id=task_id, artifact_id=artifact_id)

    try:
        content = store.read_artifact(record=artifact)
    except TaskStoreError:
        logger.warning("Task API artifact read failed: storage unavailable", exc_info=True)
        raise errors.prerequisite_unavailable("Artifact storage is unavailable.") from None

    return Response(
        content=content,
        media_type=artifact.content_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            # The digest travels with the bytes so a consumer can verify what it
            # received against what the investigator's report cited, without a
            # second authorized request to fetch the metadata.
            "X-Adp-Content-Sha256": artifact.content_sha256,
        },
    )
