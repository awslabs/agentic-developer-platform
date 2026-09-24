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

**The bytes travel beside the metadata, not inside it.** The upload is
``multipart/form-data`` with a ``metadata`` part that is exactly
``public-api.schema.json#/$defs/artifact_upload_request`` and a ``content`` part
carrying the raw bytes. That document is ``additionalProperties: false`` and
declares no content field, so a JSON body with the bytes base64-encoded inside it
cannot validate against the contract at all — an upload route shaped that way would
be unreachable for a conforming client. Multipart also keeps the design's "binary
upload" binary: base64 inflates by 4/3, so encoding a 256 KiB artifact would put a
third of the frame budget into transport overhead.

Design reference: implementation-design.md section 5;
``public-api.schema.json#/$defs/artifact_upload_request``/``artifact_upload_response``.
"""

from __future__ import annotations

import hashlib
import logging
import re
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
from src.tasks.read_store import ArtifactRecord, TaskStoreError
from src.tasks.routes import get_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["task-api"])

#: The whole multipart frame: the artifact, plus room for the metadata part and
#: MIME boundaries. Bounding the frame separately from the artifact means an
#: oversize upload is refused on its framing, before the parts are assembled.
MAX_UPLOAD_FRAME_BYTES = MAX_INPUT_ARTIFACT_BYTES + 8192

#: Part names, fixed by this route rather than negotiable. Named constants because
#: the refusal message below quotes them, and a caller debugging a 400 needs the
#: message to match what the parser actually looks for.
METADATA_PART = "metadata"
CONTENT_PART = "content"

#: ``common.schema.json#/$defs/sha256_digest``. Checked as a shape before the
#: content is hashed, so a caller sending an uppercase or truncated digest is told
#: its digest is malformed rather than that its bytes do not match.
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def storage_key(*, tenant_id: str, principal_id: str, artifact_id: str, version: int) -> str:
    """Use T1's canonical artifact key, also used by acceptance bindings."""
    from src.tasks.dynamo_read_store import artifact_object_key

    return artifact_object_key(tenant_id, principal_id, artifact_id, version)


def check_content(body: dict, content: bytes) -> None:
    """Verify the uploaded bytes against the caller's own declared digest and length.

    The digest is checked rather than trusted, and that check is the whole point of
    requiring it. An investigator's report cites artifacts by ID and digest as
    evidence; if the stored bytes could differ from the digest recorded alongside
    them, every such citation would be unverifiable. Checking on the way in means a
    corrupted upload is a refusal instead of a permanently mis-attributed piece of
    evidence.

    Length is compared before the digest so a truncated upload is named as a length
    mismatch. Both would be caught by the digest alone, but "you sent fewer bytes
    than you said" is actionable and "your hash is wrong" sends the caller looking at
    the wrong thing.

    There is deliberately no separate empty-content branch. ``check_upload_metadata``
    requires ``content_length`` to be at least 1 and the equality below requires the
    bytes to match it, so zero bytes cannot reach storage by either route. A third
    check would be unreachable, and an unreachable check is worse than none: no test
    can distinguish it from a no-op, so it reads as a guarantee nothing is enforcing.
    """
    if len(content) > MAX_INPUT_ARTIFACT_BYTES:
        raise errors.payload_too_large(f"An input artifact may not exceed {MAX_INPUT_ARTIFACT_BYTES} bytes.")

    if body.get("content_length") != len(content):
        raise errors.invalid_request("content_length does not match the supplied content.")

    if body.get("content_sha256") != hashlib.sha256(content).hexdigest():
        # The computed digest is deliberately not returned. Echoing it would turn
        # this route into an oracle that confirms the hash of arbitrary bytes the
        # caller is guessing at.
        raise errors.invalid_request("content_sha256 does not match the supplied content.")


def check_upload_metadata(body: dict) -> None:
    """Reject unknown fields and unpermitted content types.

    The unknown-field check mirrors the schema's ``additionalProperties: false``,
    and it is what stops a caller from *attempting* the forbidden fields: a body
    carrying ``tenant_id``, ``owner_principal_id`` or a storage key is refused
    rather than having those fields quietly ignored. Ignoring them would leave a
    caller believing it had set an owner the gateway actually derived itself.

    ``content_length`` is required and typed here rather than only compared against
    the bytes, because a missing or non-integer value would otherwise fail that
    comparison and be reported as a mismatch — telling the caller its bytes were
    wrong when its metadata was.
    """
    permitted = {"schema_version", "content_type", "content_sha256", "content_length", "filename"}
    unknown = sorted(set(body) - permitted)
    if unknown:
        raise errors.invalid_request(f"Fields not permitted on an artifact upload: {', '.join(unknown)}")
    if body.get("schema_version") != SCHEMA_VERSION:
        raise errors.invalid_request("schema_version must be 1.0.")
    if body.get("content_type") not in PERMITTED_ARTIFACT_CONTENT_TYPES:
        raise errors.invalid_request("Investigator v1 accepts text/plain and application/json only.")

    declared = body.get("content_length")
    if not isinstance(declared, int) or isinstance(declared, bool) or declared < 1:
        raise errors.invalid_request("content_length must be a positive integer.")

    digest = body.get("content_sha256")
    if not isinstance(digest, str) or not SHA256_PATTERN.match(digest):
        raise errors.invalid_request("content_sha256 must be a 64-character lowercase hex digest.")

    filename = body.get("filename")
    if filename is not None and (not isinstance(filename, str) or len(filename) > 256):
        raise errors.invalid_request("filename must be a string of at most 256 characters.")


async def parse_upload(request: Request) -> tuple[dict, bytes]:
    """Split the multipart upload into its contract metadata and its raw bytes.

    Bounded before it is parsed. ``Content-Length`` is a claim and a chunked request
    carries none, so the declared size is checked first as a cheap refusal and the
    actual bytes are checked again after reading — a limit enforced only on the
    header is a limit a client can opt out of.

    Both parts are required explicitly. A missing ``content`` part is a 400 rather
    than an empty artifact, because storing zero bytes under a digest the caller
    computed over real content would be a permanently wrong piece of evidence, and
    the caller would have no signal that anything went wrong.
    """
    declared = request.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_FRAME_BYTES:
        raise errors.payload_too_large(f"An input artifact may not exceed {MAX_INPUT_ARTIFACT_BYTES} bytes.")

    if not (request.headers.get("Content-Type") or "").startswith("multipart/form-data"):
        raise errors.invalid_request(f"An artifact upload must be multipart/form-data with {METADATA_PART} and {CONTENT_PART} parts.")

    try:
        form = await request.form(max_part_size=MAX_UPLOAD_FRAME_BYTES)
    except Exception:
        # Starlette raises on a malformed or oversize multipart frame. The parser's
        # own message is not returned: it can quote the offending bytes, which here
        # are caller-supplied artifact content.
        logger.info("Task API artifact upload refused: multipart frame could not be parsed")
        raise errors.invalid_request("The artifact upload could not be parsed as multipart/form-data.") from None

    try:
        metadata_part = form.get(METADATA_PART)
        content_part = form.get(CONTENT_PART)

        if metadata_part is None or content_part is None:
            raise errors.invalid_request(f"An artifact upload requires a {METADATA_PART} part and a {CONTENT_PART} part.")

        content = await content_part.read() if hasattr(content_part, "read") else str(content_part).encode()
        if len(content) > MAX_INPUT_ARTIFACT_BYTES:
            raise errors.payload_too_large(f"An input artifact may not exceed {MAX_INPUT_ARTIFACT_BYTES} bytes.")

        raw_metadata = await metadata_part.read() if hasattr(metadata_part, "read") else str(metadata_part).encode()
        try:
            body = http.parse_json_object(raw_metadata)
        except ValueError:
            raise errors.invalid_request(f"The {METADATA_PART} part is not a JSON object.") from None
    finally:
        # Starlette spools large parts to temporary files; without this a rejected
        # upload leaves them behind, so the refusal path would leak disk on exactly
        # the requests an attacker can repeat cheaply.
        await form.close()

    return body, content


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

    body, content = await parse_upload(request)
    check_upload_metadata(body)
    check_content(body, content)

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
        store = get_store()
        store.require_policy(tenant=caller.tenant_id, principal=caller.principal_id, persona="agent-task-investigator")
        stored = store.put_artifact(record=record, content=content)
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
