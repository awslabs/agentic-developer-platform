"""Artifact upload and task-scoped download: evidence in, evidence out.

Two properties dominate this surface, and each rules out the obvious shortcut.

**No presigned URL, in either direction.** The contract forbids it structurally —
``artifact_upload_response.download_url`` is ``{"not": {}}``, a field that can never
validate — because a presigned URL is a bearer token for an object that outlives the
credential that obtained it, survives revocation and leaves no record of who read
what. Every response body here is validated against that schema, so an
implementation that added one would fail a test rather than merely violate a comment.

**The gateway derives ownership and location.** The caller supplies a content type, a
digest, a length and bytes; owner, tenant, artifact ID and storage key are all
server-derived. A caller-supplied key is a path-traversal surface and an
authorization bypass at once.

Covers T6-AC03 (artifact scope and binding enforcement, cross-task and nonowner
refusal) and T6-AC05 (durable, correctly scoped result references).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from src.tasks import artifacts as artifacts_module
from src.tasks.limits import MAX_INPUT_ARTIFACT_BYTES

from .conftest import (
    ARTIFACT,
    ARTIFACTS_ONLY,
    NO_SCOPES,
    OTHER_ARTIFACT,
    OTHER_TASK,
    OTHER_TENANT,
    READ_ONLY,
    SAME_TENANT_OTHER,
    TASK,
    make_artifact,
    make_record,
)

UPLOAD = "/v1/task-artifacts"
ERROR_SCHEMA = "errors.schema.json#/$defs/error_response"
UPLOAD_REQUEST_SCHEMA = "public-api.schema.json#/$defs/artifact_upload_request"
UPLOAD_RESPONSE_SCHEMA = "public-api.schema.json#/$defs/artifact_upload_response"

CONTENT = b"pool acquisition timed out after 30s\n"


def metadata(content: bytes = CONTENT, **overrides) -> dict:
    """The contract's ``artifact_upload_request``, computed over ``content``.

    Derived from the bytes rather than hardcoded, so a test that varies the content
    cannot accidentally assert the digest check by forgetting to update a literal.
    """
    body = {
        "schema_version": "1.0",
        "content_type": "text/plain",
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "content_length": len(content),
        "filename": "checkout-api-logs.txt",
    }
    return {**body, **overrides}


def parts(content: bytes = CONTENT, body: dict | None = None) -> dict:
    """Keyword arguments posting the contract metadata beside raw bytes.

    The bytes travel in their own part rather than base64-encoded inside the
    metadata, because ``artifact_upload_request`` is ``additionalProperties: false``
    and declares no content field — a JSON body carrying the content could not
    validate against the contract at all.
    """
    document = metadata(content) if body is None else body
    return {
        "data": {artifacts_module.METADATA_PART: json.dumps(document)},
        "files": {artifacts_module.CONTENT_PART: ("artifact.bin", content, "application/octet-stream")},
    }


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


async def test_the_upload_metadata_is_exactly_the_frozen_request_schema(contract) -> None:
    """The document this file sends is the contract's, not an approximation.

    Validated before it is used anywhere else, so every assertion below is about
    what happens to a *conforming* request. A fixture that quietly drifted from the
    schema would make the whole file prove things about a body no real client sends.
    """
    assert contract(metadata(), UPLOAD_REQUEST_SCHEMA) == []


async def test_an_upload_stores_the_bytes_and_returns_a_contract_reference(client, store, contract) -> None:
    """T6-AC05: 201 with an immutable reference, and the bytes are retrievable."""
    response = await client.post(UPLOAD, **parts())

    assert response.status_code == 201
    body = response.json()
    assert contract(body, UPLOAD_RESPONSE_SCHEMA) == []
    assert body["content_sha256"] == hashlib.sha256(CONTENT).hexdigest()
    assert body["content_type"] == "text/plain"
    assert body["version"] == 1

    stored = store.artifacts[body["artifact_id"]]
    assert store.blobs[stored.storage_key] == CONTENT


async def test_no_response_ever_carries_a_download_url(client) -> None:
    """The forbidden field, asserted on the wire rather than trusted to absence.

    ``download_url`` is ``{"not": {}}`` in the contract, so the schema check above
    already rejects it. This states the property directly because it is the one field
    whose accidental addition would read as a convenience feature rather than as a
    security regression.
    """
    body = (await client.post(UPLOAD, **parts())).json()

    assert "download_url" not in body
    assert "X-Amz-Signature" not in json.dumps(body)


async def test_the_gateway_derives_owner_tenant_and_storage_key(client, store) -> None:
    """T6-AC03: ownership comes from the credential, and the key is not caller data.

    The key embeds *hashed* tenant and principal, so the prefix partitioning the
    design asks for does not publish who owns what to anyone with bucket listing —
    access logs and inventory reports included.
    """
    response = await client.post(UPLOAD, **parts())

    stored = store.artifacts[response.json()["artifact_id"]]
    assert (stored.tenant_id, stored.owner_principal_id) == ("org-alpha", "svc-alpha")
    assert stored.task_id is None, "an upload is not yet bound to a task"
    assert "org-alpha" not in stored.storage_key and "svc-alpha" not in stored.storage_key
    assert stored.storage_key.startswith("tasks/")
    assert stored.storage_key.endswith(f"/{stored.artifact_id}/1")


async def test_an_unclaimed_upload_has_a_definite_end(client) -> None:
    """Unclaimed uploads expire; unowned bytes do not live forever.

    Recorded at upload rather than left to the route that binds an artifact to a
    task, because an artifact that is never referenced would otherwise have no expiry
    at all — which is exactly the case the 24-hour rule exists for.
    """
    body = (await client.post(UPLOAD, **parts())).json()

    assert body["expires_at"] > body["created_at"]


@pytest.mark.parametrize(
    "field",
    ["tenant_id", "owner_principal_id", "storage_key", "bucket", "download_url", "task_id", "expires_at"],
)
async def test_a_caller_may_not_attempt_a_server_owned_field(client, store, contract, field) -> None:
    """T6-AC03: forbidden fields are refused, not silently ignored.

    Ignoring them would leave a caller believing it had set an owner, a location or
    an expiry that the gateway actually derived itself — and a caller that believes
    it set an owner has a different model of who can read its evidence than the
    gateway does.
    """
    response = await client.post(UPLOAD, **parts(body=metadata(**{field: "attacker-supplied"})))

    assert response.status_code == 400
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert field in response.json()["message"]
    assert store.artifacts == {}


async def test_content_that_disagrees_with_its_digest_is_refused(client, store, contract) -> None:
    """The digest is checked rather than trusted, which is why it is required.

    An investigator's report cites artifacts by ID and digest as evidence. If stored
    bytes could differ from the digest recorded beside them, every such citation
    would be unverifiable — and the corruption would be permanent and invisible.

    The substituted content is the same length as the real content, so the length
    comparison cannot be what produces this refusal.
    """
    other = b"x" * len(CONTENT)
    response = await client.post(UPLOAD, **parts(body=metadata(content=other)))

    assert response.status_code == 400
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert "content_sha256" in response.json()["message"]
    assert store.artifacts == {}


async def test_a_digest_refusal_does_not_reveal_the_computed_hash(client) -> None:
    """Echoing the real digest would make this route a hashing oracle.

    A caller could submit bytes it is guessing at and read back their SHA-256, which
    is a service the upload route has no reason to provide.
    """
    other = b"x" * len(CONTENT)
    response = await client.post(UPLOAD, **parts(body=metadata(content=other)))

    assert hashlib.sha256(CONTENT).hexdigest() not in json.dumps(response.json())


async def test_a_length_that_disagrees_with_the_content_is_refused(client, contract) -> None:
    """Named as a length mismatch, though the digest alone would also catch it.

    "You sent fewer bytes than you said" is actionable; "your hash is wrong" sends
    the caller looking at the wrong thing.
    """
    response = await client.post(UPLOAD, **parts(body=metadata(content_length=len(CONTENT) - 1)))

    assert response.status_code == 400
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert "content_length" in response.json()["message"]


@pytest.mark.parametrize("length", [0, -1, "37", 37.0, True], ids=["zero", "negative", "string", "float", "bool"])
async def test_a_non_positive_integer_length_is_a_metadata_error(client, length) -> None:
    """A malformed length is reported as metadata, not as a content mismatch.

    ``True`` is included because it is an ``int`` subclass: a naive
    ``isinstance(value, int)`` check would accept it as the length 1 and then report
    the caller's bytes as mismatched rather than its metadata as wrong.
    """
    response = await client.post(UPLOAD, **parts(body=metadata(content_length=length)))

    assert response.status_code == 400
    assert "content_length must be" in response.json()["message"]


@pytest.mark.parametrize("digest", ["", "not-hex" * 9, "A" * 64, "a" * 63, "a" * 65], ids=["empty", "nonhex", "uppercase", "short", "long"])
async def test_a_malformed_digest_is_refused_as_metadata_not_as_a_mismatch(client, digest) -> None:
    """A malformed digest is a metadata error, and saying so is the point.

    Reporting it as a content mismatch would send a caller hunting a corrupted
    upload when the real problem is the shape of the field it sent. Uppercase is
    included because it is the plausible mistake: the contract's digest is lowercase
    hex, and an uppercase one is a correct hash in the wrong encoding.
    """
    response = await client.post(UPLOAD, **parts(body=metadata(content_sha256=digest)))

    assert response.status_code == 400
    assert "content_sha256 must be" in response.json()["message"]


@pytest.mark.parametrize("content_type", ["text/html", "application/octet-stream", "image/png", ""], ids=["html", "binary", "image", "empty"])
async def test_an_unpermitted_content_type_is_refused(client, store, contract, content_type) -> None:
    """Investigator v1 accepts ``text/plain`` and ``application/json`` only.

    ``text/html`` is the case that matters: these bytes can be served back to a
    browser, and narrowing the accepted set at upload is the first of two defences
    against that. The download headers are the second.
    """
    response = await client.post(UPLOAD, **parts(body=metadata(content_type=content_type)))

    assert response.status_code == 400
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert store.artifacts == {}


async def test_an_empty_artifact_is_refused_as_an_impossible_length(client, store) -> None:
    """Zero bytes is not evidence, and a conforming client cannot even describe it.

    The contract's ``content_length`` minimum is 1, so an honest empty upload is
    refused on its metadata. Asserting *which* refusal matters here: it is the
    evidence that the length floor is doing this work, which is what lets the content
    check below rely on it instead of repeating it.
    """
    response = await client.post(UPLOAD, **parts(content=b""))

    assert response.status_code == 400
    assert "content_length must be" in response.json()["message"]
    assert store.artifacts == {}


async def test_empty_content_behind_a_plausible_length_is_refused_as_a_mismatch(client, store) -> None:
    """A dishonest empty upload is caught by the length comparison, not by a floor.

    This is the case that matters: metadata describing real content, with no bytes
    behind it. Storing it would put zero bytes under a digest for content that
    exists — permanently wrong evidence, with no signal to anyone. Together with the
    test above, the two refusals cover empty content from both directions, which is
    why ``check_content`` needs no separate emptiness branch of its own.
    """
    response = await client.post(UPLOAD, **parts(content=b"", body=metadata()))

    assert response.status_code == 400
    assert "content_length does not match" in response.json()["message"]
    assert store.artifacts == {}


async def test_an_oversize_artifact_is_refused(client, store, contract) -> None:
    """413 at the 256 KiB per-artifact bound, before anything is stored."""
    oversize = b"x" * (MAX_INPUT_ARTIFACT_BYTES + 1)

    response = await client.post(UPLOAD, **parts(content=oversize))

    assert response.status_code == 413
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert store.artifacts == {}


async def test_a_declared_oversize_frame_is_refused_before_it_is_parsed(client, contract) -> None:
    """The ``Content-Length`` claim is a cheap early refusal.

    Not the only check — a chunked request carries no length, and the actual bytes
    are bounded again after reading — but it means the common case of an oversize
    upload is rejected without assembling it. The body here is deliberately
    unparseable multipart: if it were being parsed, the answer would be a 400.
    """
    response = await client.post(
        UPLOAD,
        content=b"x" * 32,
        headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(MAX_INPUT_ARTIFACT_BYTES * 4),
        },
    )

    assert response.status_code == 413
    assert contract(response.json(), ERROR_SCHEMA) == []


@pytest.mark.parametrize("omit", [artifacts_module.METADATA_PART, artifacts_module.CONTENT_PART])
async def test_a_missing_part_is_refused(client, store, omit) -> None:
    """Both parts are required explicitly.

    A missing content part must not become an empty artifact, and a missing metadata
    part must not become a stored blob with no declared type or digest — an artifact
    nothing can verify.
    """
    sent = parts()
    sent["data"].pop(omit, None)
    sent["files"].pop(omit, None)

    response = await client.post(UPLOAD, **sent)

    assert response.status_code == 400
    assert artifacts_module.METADATA_PART in response.json()["message"]
    assert store.artifacts == {}


async def test_a_json_body_is_refused_on_its_content_type(client, contract) -> None:
    """The refusal names the transport, not just the missing parts.

    The metadata document alone cannot carry content and still validate, so a client
    that posts it as the whole body is making a reasonable mistake — and "you need a
    metadata part and a content part" is the wrong thing to tell it, because it is
    already sending that field set. It needs to be told the encoding is wrong.

    This also pins a real distinction rather than a cosmetic one: Starlette parses a
    JSON body as an *empty* form rather than raising, so a route that dropped the
    content-type check would still answer 400 — by the missing-parts path, with a
    message that sends a JSON client looking for fields it already supplied.
    """
    response = await client.post(UPLOAD, json=metadata())

    assert response.status_code == 400
    assert contract(response.json(), ERROR_SCHEMA) == []
    message = response.json()["message"]
    assert "multipart/form-data" in message
    assert artifacts_module.METADATA_PART in message and artifacts_module.CONTENT_PART in message


@pytest.mark.parametrize("raw", ["[1, 2, 3]", '"just a string"', "not json at all", ""], ids=["array", "string", "garbage", "empty"])
async def test_metadata_that_is_not_a_json_object_is_refused(client, store, raw) -> None:
    sent = parts()
    sent["data"][artifacts_module.METADATA_PART] = raw

    response = await client.post(UPLOAD, **sent)

    assert response.status_code == 400
    assert store.artifacts == {}


async def test_duplicate_metadata_fields_are_refused(client, store) -> None:
    """Two parsers disagreeing about which duplicate wins is how a validated value
    and a used value come apart. Refusing is the only answer that cannot be
    inconsistent — here the second ``content_type`` is the unpermitted one, so
    "last wins" and "first wins" differ in whether the upload is allowed at all.
    """
    sent = parts()
    sent["data"][artifacts_module.METADATA_PART] = (
        '{"schema_version": "1.0", "content_type": "text/plain", "content_type": "text/html",'
        f' "content_sha256": "{hashlib.sha256(CONTENT).hexdigest()}", "content_length": {len(CONTENT)}}}'
    )

    response = await client.post(UPLOAD, **sent)

    assert response.status_code == 400
    assert store.artifacts == {}


async def test_an_oversize_filename_is_refused(client) -> None:
    response = await client.post(UPLOAD, **parts(body=metadata(filename="f" * 257)))

    assert response.status_code == 400
    assert "filename" in response.json()["message"]


async def test_a_wrong_schema_version_is_refused(client) -> None:
    """A version mismatch is a refusal, not a best-effort interpretation."""
    response = await client.post(UPLOAD, **parts(body=metadata(schema_version="2.0")))

    assert response.status_code == 400
    assert "schema_version" in response.json()["message"]


# ---------------------------------------------------------------------------
# Upload authorization and availability
# ---------------------------------------------------------------------------


async def test_upload_requires_the_artifacts_scope(client, caller, store, contract) -> None:
    caller[0] = READ_ONLY

    response = await client.post(UPLOAD, **parts())

    assert response.status_code == 403
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "disallowed_scope"
    assert store.artifacts == {}


async def test_upload_does_not_require_the_read_scope(client, caller, store) -> None:
    """An upload is not a read.

    Requiring read scope would force a producer that only ever supplies evidence to
    hold a credential that can also enumerate task state — a strictly larger grant
    than the job needs.
    """
    caller[0] = ARTIFACTS_ONLY

    response = await client.post(UPLOAD, **parts())

    assert response.status_code == 201
    assert len(store.artifacts) == 1


async def test_an_unauthorized_upload_is_refused_before_the_bytes_are_read(client, caller, store) -> None:
    """Scope is checked before the frame is parsed.

    Otherwise an unauthorized caller could make the gateway buffer and hash 256 KiB
    per request — work it is entitled to none of — and the refusal would cost more
    than the success.
    """
    caller[0] = READ_ONLY

    response = await client.post(
        UPLOAD,
        content=b"not multipart at all",
        headers={"Content-Type": "multipart/form-data; boundary=x"},
    )

    assert response.status_code == 403, "an authorization refusal must not be preempted by a parse error"
    assert store.artifacts == {}


async def test_an_upload_storage_outage_is_a_503(client, store, contract) -> None:
    """An outage denies; it never reports a success it did not achieve."""
    store.fail = True

    response = await client.post(UPLOAD, **parts())

    assert response.status_code == 503
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "prerequisite_unavailable"


async def test_upload_is_refused_when_the_surface_is_disabled(client, monkeypatch, store) -> None:
    """Design section 11: the flag defaults false, so the route mounts and refuses."""
    monkeypatch.setenv("ADP_TASK_API_READ_ENABLED", "false")

    response = await client.post(UPLOAD, **parts())

    assert response.status_code == 503
    assert store.artifacts == {}


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def bind(store, *, artifact_id: str = ARTIFACT, content: bytes = CONTENT, **overrides):
    """Seed an artifact already bound to a task, with its bytes present."""
    record = make_artifact(
        artifact_id=artifact_id,
        content_sha256=hashlib.sha256(content).hexdigest(),
        content_length=len(content),
        storage_key=f"tasks/t/p/{artifact_id}/1",
        **overrides,
    )
    store.put_artifact(record=record, content=content)
    return record


async def test_the_owner_downloads_the_exact_bytes(client, store) -> None:
    bind(store)

    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{ARTIFACT}")

    assert response.status_code == 200
    assert response.content == CONTENT


async def test_the_download_is_a_download_not_a_document(client, store) -> None:
    """T6-AC05: caller-supplied bytes cannot execute in the gateway's origin.

    A ``text/plain`` artifact whose content a browser chose to sniff as HTML would
    run as a document on this origin. ``attachment`` plus ``nosniff`` together make
    that impossible; either alone leaves a path. ``no-store`` keeps an
    authorization-scoped read out of every shared cache in between.
    """
    record = bind(store)

    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{ARTIFACT}")

    assert response.headers["content-disposition"] == "attachment"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-adp-content-sha256"] == record.content_sha256


async def test_the_digest_travels_with_the_bytes(client, store) -> None:
    """A consumer can verify what it received against what a report cited.

    Without the header it would need a second authorized request just to learn the
    digest, which makes verification something callers skip.
    """
    bind(store)

    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{ARTIFACT}")

    assert response.headers["x-adp-content-sha256"] == hashlib.sha256(response.content).hexdigest()


@pytest.mark.parametrize("identity", [SAME_TENANT_OTHER, OTHER_TENANT], ids=["same-tenant-nonowner", "cross-tenant"])
async def test_a_nonowner_cannot_download(client, store, contract, caller, identity) -> None:
    """T6-AC03: v1 grants no implicit same-tenant access to another service's evidence."""
    bind(store)
    caller[0] = identity

    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{ARTIFACT}")

    assert response.status_code == 404
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "not_found"


async def test_an_artifact_owned_by_another_principal_is_not_readable_through_a_shared_task(client, store, contract) -> None:
    """The artifact's own owner is checked, not just the task's.

    Checking only task ownership would make every artifact bound to a task readable
    by whoever owns the task, regardless of who uploaded it — so a future route that
    let a second principal attach evidence would silently widen who can read it.
    """
    bind(store, owner_principal_id="svc-beta")

    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{ARTIFACT}")

    assert response.status_code == 404
    assert contract(response.json(), ERROR_SCHEMA) == []


async def test_an_artifact_cannot_be_read_through_another_task(client, store, contract) -> None:
    """T6-AC03: the binding is exact, not merely "some task the caller owns".

    Without this, an artifact ID legitimately readable through one task would be
    readable through every other task the caller owns — which turns a task-scoped
    reference into a tenant-wide one and breaks the scoping AC05 requires.
    """
    store.put_task(make_record(task_id=OTHER_TASK))
    bind(store)

    response = await client.get(f"/v1/tasks/{OTHER_TASK}/artifacts/{ARTIFACT}")

    assert response.status_code == 404
    assert contract(response.json(), ERROR_SCHEMA) == []


async def test_an_unbound_upload_is_not_readable_through_any_task(client, store, contract) -> None:
    """An uploaded but unclaimed artifact has no task to be read through yet.

    ``task_id`` is null until admission binds it, and a null binding matches no task
    — so the download route refuses rather than treating "not yet bound" as "bound
    to whatever you asked for".
    """
    upload = await client.post(UPLOAD, **parts())
    artifact_id = upload.json()["artifact_id"]

    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{artifact_id}")

    assert response.status_code == 404
    assert contract(response.json(), ERROR_SCHEMA) == []


async def test_an_absent_artifact_is_indistinguishable_from_an_unauthorized_one(client, store) -> None:
    """A distinguishable refusal is an existence oracle for other principals' evidence.

    Both requests here come from a caller that legitimately owns the task, so the
    two refusals are produced by *different* checks — the artifact's owner and the
    artifact's absence — and the assertion is that they are nonetheless identical.
    """
    bind(store, owner_principal_id="svc-beta")

    unauthorized = await client.get(f"/v1/tasks/{TASK}/artifacts/{ARTIFACT}")
    absent = await client.get(f"/v1/tasks/{TASK}/artifacts/{OTHER_ARTIFACT}")

    assert unauthorized.status_code == absent.status_code == 404
    assert {k: v for k, v in unauthorized.json().items() if k != "request_id"} == {k: v for k, v in absent.json().items() if k != "request_id"}


@pytest.mark.parametrize("identity", [READ_ONLY, ARTIFACTS_ONLY, NO_SCOPES], ids=["read-only", "artifacts-only", "no-scopes"])
async def test_download_requires_both_scopes(client, store, contract, caller, identity) -> None:
    """Reading through a task needs read *and* the artifact surface's own scope.

    Both, because the two grants answer different questions: read-and-ownership says
    this task is yours, and the artifacts scope says this credential is meant to
    touch artifact bytes at all.
    """
    bind(store)
    caller[0] = identity

    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{ARTIFACT}")

    assert response.status_code == 403
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "disallowed_scope"


async def test_a_scope_refusal_is_not_downgraded_to_a_not_found(client, store, caller) -> None:
    """A missing scope is the caller's own credential, not a hidden resource.

    Answering 404 here would tell a caller holding the wrong credential that its
    artifact does not exist, which is both false and unactionable — it would go
    looking for lost evidence instead of fixing its scopes.
    """
    bind(store)
    caller[0] = READ_ONLY

    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{ARTIFACT}")

    assert response.status_code == 403


async def test_a_download_storage_outage_denies_rather_than_reporting_absence(client, store, contract) -> None:
    """An outage is 503, never 404 — telling a caller its evidence is gone is worse."""
    bind(store)
    store.fail = True

    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{ARTIFACT}")

    assert response.status_code == 503
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "prerequisite_unavailable"


async def test_missing_bytes_behind_a_present_record_is_a_503_not_empty_content(client, store, contract) -> None:
    """A record without its bytes is an inconsistency, not an empty artifact.

    Returning 200 with nothing would hand a consumer zero bytes under a digest that
    describes real content: verification would fail with no indication of why.
    """
    record = bind(store)
    store.blobs.pop(record.storage_key)

    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{ARTIFACT}")

    assert response.status_code == 503
    assert contract(response.json(), ERROR_SCHEMA) == []


async def test_download_is_refused_when_the_surface_is_disabled(client, store, monkeypatch) -> None:
    bind(store)
    monkeypatch.setenv("ADP_TASK_API_READ_ENABLED", "false")

    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{ARTIFACT}")

    assert response.status_code == 503


async def test_the_round_trip_preserves_bytes_exactly(client, store) -> None:
    """Upload then download, with content chosen to break a text-mangling path.

    CRLF, a NUL byte and multi-byte UTF-8 all survive. An artifact is evidence, and
    a transport that normalised line endings or re-encoded would silently invalidate
    the digest a report cites it by — while still looking like a working round trip.
    """
    awkward = "line\r\nline\x00é\U0001f600".encode()
    upload = await client.post(UPLOAD, **parts(content=awkward))
    artifact_id = upload.json()["artifact_id"]

    # Binding is T2/T3's route; here the stored record is bound directly so the
    # download path can be exercised against bytes this suite actually uploaded.
    store.artifacts[artifact_id] = dataclasses.replace(store.artifacts[artifact_id], task_id=TASK)

    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{artifact_id}")

    assert response.content == awkward
    assert response.headers["x-adp-content-sha256"] == hashlib.sha256(awkward).hexdigest()


async def test_generated_html_is_an_authenticated_download_with_inert_headers(client, store):
    content = b"<!doctype html><html><body>Report</body></html>"
    bind(store, content=content, content_type="text/html")
    response = await client.get(f"/v1/tasks/{TASK}/artifacts/{ARTIFACT}")
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-disposition"] == f'attachment; filename="{ARTIFACT}.html"'
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
