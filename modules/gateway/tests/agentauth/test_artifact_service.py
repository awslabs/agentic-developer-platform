"""Real upload route and Moto S3; only the upstream authority boundary is mocked."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import boto3
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from src.agentauth.artifact_keys import artifact_prefix
from src.agentauth.artifact_service import MAX_ARTIFACT_BYTES, artifact_storage, router
from src.agentauth.execution import ExecutionStateError
from src.agentauth.routes import get_agent_runtime, require_agent_transport
from tests.agentauth.test_run_services import GRANT, HEADERS, RECORD

URL = "/internal/v1/agent/self/artifacts/"


@pytest.fixture
async def artifacts():
    with mock_aws():
        storage = boto3.client("s3", region_name="us-east-1")
        for bucket in ("run-logs", "run-fallback"):
            storage.create_bucket(Bucket=bucket)
        runtime = SimpleNamespace(
            env={"AGENT_RUN_LOGS_BUCKET": "run-logs", "AGENT_FALLBACK_BUCKET": "run-fallback"},
            authenticate=Mock(return_value=(SimpleNamespace(uid="pod-one"), "caller", RECORD, GRANT)),
            validate_flow=AsyncMock(),
        )
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_agent_runtime] = lambda: runtime
        app.dependency_overrides[require_agent_transport] = lambda: None
        app.dependency_overrides[artifact_storage] = lambda: storage
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.test") as client:
            yield client, runtime, storage


@pytest.mark.parametrize(
    "kind,bucket,content_type",
    [
        ("transcript", "run-logs", "text/markdown"),
        ("spill", "run-logs", "text/plain"),
        ("comment", "run-fallback", "text/markdown"),
        ("git-changes", "run-fallback", "application/gzip"),
        ("git-manifest", "run-fallback", "text/markdown"),
    ],
)
async def test_content_is_archived_under_server_derived_run_prefix(artifacts, kind, bucket, content_type):
    client, runtime, storage = artifacts
    body = b"own run bytes\x00"
    headers = {**HEADERS, "x-amz-acl": "public-read", "x-amz-meta-tenant": "victim", "content-type": "text/html"}
    first = await client.post(URL + kind, content=body, headers=headers)
    second = await client.post(URL + kind, content=body, headers=headers)
    assert first.status_code == 200 and first.json() == second.json()
    receipt = first.json()
    assert receipt["key"].startswith(artifact_prefix(RECORD) + kind + "/")
    assert receipt["sha256"] == hashlib.sha256(body).hexdigest()
    assert receipt["uri"] == f"s3://{bucket}/{receipt['key']}"
    obj = storage.get_object(Bucket=bucket, Key=receipt["key"])
    assert obj["Body"].read() == body and obj["ContentType"] == content_type and obj["Metadata"] == {}
    assert len(storage.list_objects_v2(Bucket=bucket)["Contents"]) == 1
    assert first.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["transcript?key=victim", "transcript?bucket=victim", "../victim", "credentials", "transcript/other"])
async def test_no_destination_selection(artifacts, path):
    client, _, storage = artifacts
    assert (await client.post(URL + path, content=b"x", headers=HEADERS)).status_code == 404
    assert storage.list_objects_v2(Bucket="run-logs")["KeyCount"] == 0


@pytest.mark.parametrize("missing", list(HEADERS))
async def test_both_proofs_required(artifacts, missing):
    client, runtime, _ = artifacts
    response = await client.post(URL + "transcript", content=b"x", headers={k: v for k, v in HEADERS.items() if k != missing})
    assert response.status_code == 404
    runtime.authenticate.assert_not_called()


@pytest.mark.parametrize("size,expected", [(0, 422), (MAX_ARTIFACT_BYTES, 200), (MAX_ARTIFACT_BYTES + 1, 413)])
async def test_upload_size_is_bounded(artifacts, size, expected):
    client, _, storage = artifacts
    response = await client.post(URL + "spill", content=b"x" * size, headers=HEADERS)
    assert response.status_code == expected
    assert storage.list_objects_v2(Bucket="run-logs")["KeyCount"] == (1 if expected == 200 else 0)


async def test_revocation_while_uploading_refuses_before_s3(artifacts):
    client, runtime, storage = artifacts

    async def chunks():
        yield b"first"
        runtime.authenticate.side_effect = ExecutionStateError("revoked")
        yield b"last"

    assert (await client.post(URL + "transcript", content=chunks(), headers=HEADERS)).status_code == 404
    assert storage.list_objects_v2(Bucket="run-logs")["KeyCount"] == 0


async def test_changed_identity_during_upload_cannot_select_new_destination(artifacts):
    client, runtime, storage = artifacts
    old = runtime.authenticate.return_value
    newer = (*old[:2], replace(RECORD, current_attempt=2), GRANT)
    runtime.authenticate.side_effect = [old, old, newer, newer]
    assert (await client.post(URL + "transcript", content=b"x", headers=HEADERS)).status_code == 404
    assert storage.list_objects_v2(Bucket="run-logs")["KeyCount"] == 0


async def test_expiry_during_s3_does_not_release_receipt(artifacts):
    client, runtime, storage = artifacts
    original = storage.put_object

    def write(**kwargs):
        result = original(**kwargs)
        runtime.authenticate.side_effect = ExecutionStateError("expired")
        return result

    storage.put_object = write
    assert (await client.post(URL + "transcript", content=b"x", headers=HEADERS)).status_code == 404
    # The completed effect is confined to the original run, with no receipt released.
    assert storage.list_objects_v2(Bucket="run-logs")["Contents"][0]["Key"].startswith(artifact_prefix(RECORD))


def test_tenant_run_and_attempt_each_have_disjoint_namespaces():
    records = [RECORD, replace(RECORD, tenant_id="other"), replace(RECORD, invocation_id="other"), replace(RECORD, current_attempt=2)]
    assert len({artifact_prefix(record) for record in records}) == 4


async def test_missing_config_and_storage_errors_are_redacted(artifacts):
    from botocore.exceptions import ClientError

    client, runtime, storage = artifacts
    storage.put_object = Mock(side_effect=ClientError({"Error": {"Code": "Denied", "Message": "secret credential"}}, "PutObject"))
    response = await client.post(URL + "comment", content=b"x", headers=HEADERS)
    assert response.status_code == 503 and "secret" not in response.text
    runtime.env.clear()
    assert (await client.post(URL + "transcript", content=b"x", headers=HEADERS)).status_code == 503


# ---------------------------------------------------------------------------
# The review-result kind: stored like any artifact, and additionally observed (#5146)
# ---------------------------------------------------------------------------


REVIEW_HEADERS = {**HEADERS, "content-type": "application/json"}
REVIEW_EXECUTION = {
    "orchestration_node_id": {"S": "node-one"},
    "orchestration_node_attempt": {"N": "1"},
    "installation_id": {"N": "4242"},
    "persona": {"S": "reviewer"},
}


@pytest.fixture
async def reviews(artifacts, monkeypatch):
    """The upload route with the observer stubbed at ITS boundary, not the route's.

    `observe_review_upload` is replaced, so everything the route does — parsing,
    reading the protected execution row, the persona check, the authority re-check
    ordering and what reaches the receipt — is the real code under test. The observer
    itself is covered against real PostgreSQL in
    `tests/orchestration/test_review_ingest_postgres.py`; stubbing it here keeps this
    file about the transport and avoids a second, weaker copy of those assertions.
    """
    client, runtime, storage = artifacts
    runtime.store = SimpleNamespace(_read=Mock(return_value=dict(REVIEW_EXECUTION)))
    observed = []

    async def observe(record, execution, *, document, reverify, stored_artifact_ref, resolve_artifact_ref):
        observed.append({"record": record, "execution": execution, "document": document, "stored_artifact_ref": stored_artifact_ref})
        await reverify()
        return {"recorded": True, "evidence_ref": "review-result:abc"}

    monkeypatch.setattr("src.agentauth.artifact_service.observe_review_upload", observe)
    return client, runtime, storage, observed


#: Carries fields that name a *different* run on purpose. The upload is untrusted
#: input, so every test that sends a "valid" document sends a hostile one: if any of
#: these were read instead of the authenticated record, the review would be filed
#: against somebody else's execution.
DOCUMENT = {
    "name": "orchestration-review",
    "version": "v1",
    "result_id": "r-1",
    "invocation_id": "run-someone-else",
    "tenant_id": "tenant-two",
}


def _document() -> bytes:
    return json.dumps(DOCUMENT).encode()


class TestTheReviewResultIsStoredAndObserved:
    async def test_the_document_is_stored_and_the_receipt_carries_both_facts(self, reviews):
        client, _, storage, observed = reviews
        response = await client.post(URL + "review-result", content=_document(), headers=REVIEW_HEADERS)

        assert response.status_code == 200
        receipt = response.json()
        # Both halves: the bytes are addressable AND the evidence was recorded. A
        # receipt carrying only one would leave a worker unable to tell a stored-but-
        # unrecorded document from a recorded one.
        assert receipt["key"].startswith(artifact_prefix(RECORD) + "review-result/")
        assert receipt["key"].endswith(".json")
        assert receipt["sha256"] == hashlib.sha256(_document()).hexdigest()
        assert receipt["recorded"] is True
        assert receipt["evidence_ref"] == "review-result:abc"
        assert response.headers["cache-control"] == "no-store"
        # Neither half may quietly overwrite the other. A storage receipt applied
        # last would clobber `recorded`/`refusal`; an observation applied last could
        # clobber the key a worker needs to cite.
        assert set(receipt) == {"key", "uri", "sha256", "recorded", "evidence_ref"}

        obj = storage.get_object(Bucket="run-logs", Key=receipt["key"])
        assert obj["Body"].read() == _document()
        assert obj["ContentType"] == "application/json"

    async def test_the_observer_is_given_protected_state_not_request_fields(self, reviews):
        """Every value the review is checked against comes from the server.

        The document is the only caller-supplied argument. The story, attempt and
        installation come from the DynamoDB execution row the gateway wrote at
        dispatch, and the reviewer identity from the authenticated credential — which
        is what makes the self-review and reviewer-substitution arms meaningful.
        """
        client, runtime, _, observed = reviews
        await client.post(URL + "review-result", content=_document(), headers=REVIEW_HEADERS)

        assert len(observed) == 1
        call = observed[0]
        assert call["record"] is RECORD, "the observer must be handed the authenticated record"
        assert call["execution"] == REVIEW_EXECUTION
        assert call["document"] == DOCUMENT, "the observer must see exactly what was uploaded, hostile fields included"
        # Keyed off the authenticated credential, not the document — which claims to
        # be `run-someone-else` in `tenant-two`. A document-keyed read would fetch
        # another run's dispatch row and file this review against that story.
        runtime.store._read.assert_called_once_with("TENANT#tenant-one", "EXEC#run-one")

    async def test_a_refusal_keeps_the_document_and_reports_the_arm(self, reviews, monkeypatch):
        """HTTP 200 with `recorded: false`, because the upload itself succeeded.

        The stored bytes are the evidence that a review was attempted and refused,
        which an operator needs. A 4xx would tell the worker its document was lost and
        invite it to retry a submission the server already has.
        """
        from src.agentauth.review_upload import ReviewUploadRefusedError

        client, _, storage, _ = reviews

        async def refuse(record, execution, *, document, reverify, **kwargs):
            raise ReviewUploadRefusedError("stale_head", "The review examined a commit that is no longer the head.")

        monkeypatch.setattr("src.agentauth.artifact_service.observe_review_upload", refuse)
        response = await client.post(URL + "review-result", content=_document(), headers=REVIEW_HEADERS)

        assert response.status_code == 200
        assert response.json()["recorded"] is False
        assert response.json()["refusal"] == "stale_head"
        assert "no longer the head" in response.json()["detail"]
        assert storage.list_objects_v2(Bucket="run-logs")["KeyCount"] == 1, "a refused review must keep its document"

    @pytest.mark.parametrize(
        "execution",
        [
            {**REVIEW_EXECUTION, "persona": {"S": "developer"}},
            {k: v for k, v in REVIEW_EXECUTION.items() if k != "orchestration_node_id"},
            {k: v for k, v in REVIEW_EXECUTION.items() if k != "orchestration_node_attempt"},
            {},
        ],
    )
    async def test_only_a_dispatched_reviewer_may_file_review_evidence(self, artifacts, monkeypatch, execution):
        """404, identical to every other authorization failure on this route.

        A developer run filing review evidence about its own work, or a run with no
        engine assignment at all, must not be able to tell itself apart from a run
        that does not exist — that difference is information about runs it does not
        own. Note the real `observe_review_upload` runs here: the persona rule is
        enforced by the code under test, not by the fixture's stub.
        """
        client, runtime, storage = artifacts
        runtime.store = SimpleNamespace(_read=Mock(return_value=execution))
        response = await client.post(URL + "review-result", content=_document(), headers=REVIEW_HEADERS)
        assert response.status_code == 404
        # The bytes still landed: storage is not the authorization boundary, and the
        # upload had already completed when the observation was refused.
        assert storage.list_objects_v2(Bucket="run-logs")["KeyCount"] == 1

    async def test_an_execution_with_no_protected_row_never_reaches_the_observer(self, reviews):
        """No row, so there is nothing to check the review against.

        Asserted against the stubbed observer specifically: the real one would also
        refuse a row it cannot read, but relying on that would mean the route hands
        an empty dict to whatever is downstream and trusts it to notice. The route
        must stop first, because "the row is missing" and "the row says this is not a
        reviewer" are the same answer to the caller and neither may be recorded.
        """
        client, runtime, _, observed = reviews
        runtime.store._read.return_value = {}
        response = await client.post(URL + "review-result", content=_document(), headers=REVIEW_HEADERS)
        assert response.status_code == 404
        assert observed == []

    @pytest.mark.parametrize("body", [b"not json at all", b"[1, 2, 3]", b'"a string"', b"null"])
    async def test_a_body_that_is_not_a_json_object_is_a_422(self, reviews, body):
        """A body defect the authenticated caller can fix, so not a 404."""
        client, _, _, observed = reviews
        response = await client.post(URL + "review-result", content=body, headers=REVIEW_HEADERS)
        assert response.status_code == 422
        assert observed == [], "an unparseable body must not reach the observer"

    @pytest.mark.parametrize("change", ["revoked", "superseded"])
    async def test_the_authority_is_rechecked_before_the_evidence_is_committed(self, artifacts, monkeypatch, change):
        """A credential revoked *or superseded* mid-request leaves no evidence.

        The observer commits, so the re-check has to happen inside its transaction
        rather than after this route returns; asserted by making the stub's `reverify`
        the thing that fails and checking the work after it never happens.

        Both changes are covered because they fail differently. A revoked credential
        makes `live_context` raise on its own, so a re-check that merely *calls* it
        looks correct. A superseded one authenticates perfectly well — it is a valid
        credential for a later attempt — and is caught only by comparing the snapshot
        to the one this request has been operating under. Without that comparison, a
        review of attempt 1 would be recorded under a run that has already moved to
        attempt 2: exactly the revision-binding failure this issue exists to remove.
        """
        client, runtime, storage = artifacts
        runtime.store = SimpleNamespace(_read=Mock(return_value=dict(REVIEW_EXECUTION)))
        after_recheck = []

        async def observe(record, execution, *, document, reverify, stored_artifact_ref, resolve_artifact_ref):
            if change == "revoked":
                runtime.authenticate.side_effect = ExecutionStateError("revoked")
            else:
                old = runtime.authenticate.return_value
                runtime.authenticate.return_value = (*old[:2], replace(RECORD, current_attempt=2), GRANT)
            await reverify()  # must raise
            after_recheck.append(True)
            return {"recorded": True, "evidence_ref": "review-result:abc"}

        monkeypatch.setattr("src.agentauth.artifact_service.observe_review_upload", observe)
        response = await client.post(URL + "review-result", content=_document(), headers=REVIEW_HEADERS)
        assert response.status_code == 404
        assert after_recheck == [], "the observer went on to commit after its authority changed"

    async def test_an_authority_store_failure_is_a_503_not_a_silent_skip(self, artifacts, monkeypatch):
        """The protected row could not be read, so nothing can be validated.

        A 503 rather than storing the document and reporting success: a caller told
        its review was uploaded, with no refusal and no record, would have no reason
        to retry and the evidence would exist nowhere.
        """
        from src.agentauth.store import AuthorityStoreError

        client, runtime, _ = artifacts
        runtime.store = SimpleNamespace(_read=Mock(side_effect=AuthorityStoreError("table adp-exec-dev throttled arn:aws:dynamodb:...")))
        response = await client.post(URL + "review-result", content=_document(), headers=REVIEW_HEADERS)
        assert response.status_code == 503
        assert response.json()["detail"] == "agent authority unavailable"
        assert "dynamodb" not in response.text, "the store's own message must not reach the caller"

    async def test_other_kinds_are_not_observed(self, reviews):
        """The observer is reached by exactly one kind.

        Every other artifact kind must keep the property that made this route simple:
        no database session, no orchestration state. A future kind that started
        reaching the observer by accident would be doing ledger writes from a storage
        path.
        """
        client, runtime, _, observed = reviews
        for kind in ("transcript", "spill", "comment", "git-manifest"):
            assert (await client.post(URL + kind, content=b"x", headers=HEADERS)).status_code == 200
        assert observed == []
        runtime.store._read.assert_not_called()
