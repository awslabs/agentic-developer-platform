import importlib.util
import io
import json
import time
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = load("external", "examples/task-api/client.py")
readiness = load("readiness", "scripts/task-api/check-readiness.py")
packager = load("packager", "scripts/task-api/package-evidence.py")


class Response(io.BytesIO):
    headers = {}


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_submit_retry_reuses_exact_key_and_content(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda _: None)
    opener = Opener(
        [urllib.error.URLError("disconnected"), Response(b'{"task_id":"same"}')]
    )
    api = client.Client("https://example.test", "secret", opener=opener)
    fixture = json.loads(
        (
            ROOT / "docs/task-api/contracts/v1/fixtures/valid/submit-request.json"
        ).read_text()
    )
    fixture.pop("$fixture")
    assert api.submit(fixture, "stable-key")["task_id"] == "same"
    first, second = opener.requests
    assert first.data == second.data
    assert (
        first.get_header("Idempotency-key")
        == second.get_header("Idempotency-key")
        == "stable-key"
    )
    assert first.full_url == "https://example.test/v1/tasks"


def test_conflict_is_not_retried():
    error = urllib.error.HTTPError(
        "https://example.test", 409, "conflict", {}, io.BytesIO(b"{}")
    )
    opener = Opener([error])
    with pytest.raises(client.APIError) as raised:
        client.Client("https://example.test", "secret", opener=opener).submit({}, "key")
    assert raised.value.status == 409
    assert len(opener.requests) == 1


def test_oauth_uses_form_and_basic_without_exposing_secret():
    opener = Opener([Response(b'{"access_token":"token","expires_in":900}')])
    api = client.Client(
        "https://example.test",
        token_url="https://auth.test/oauth2/token",
        client_id="id",
        client_secret="secret",
        opener=opener,
    )
    api.authenticate()
    api.authenticate()
    assert len(opener.requests) == 1
    assert b"client_credentials" in opener.requests[0].data
    assert b"secret" not in opener.requests[0].data


def test_sse_comments_multiline_and_bound():
    raw = b': heartbeat\n\nid: t:1\nevent: progress\ndata: {"a":\ndata: 1}\n\n'
    assert list(client.parse_sse(io.BytesIO(raw), time.monotonic() + 1)) == [
        {"id": "t:1", "event": "progress", "data": {"a": 1}}
    ]
    with pytest.raises(ValueError, match="bound"):
        list(client.parse_sse(io.BytesIO(b"x" * 65537), time.monotonic() + 1))


def test_sse_reconnect_preserves_cursor_and_stops_terminal(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda _: None)
    opener = Opener(
        [
            Response(b"id: t:1\nevent: progress\ndata: {}\n\n"),
            Response(b"id: t:2\nevent: task.completed\ndata: {}\n\n"),
        ]
    )
    api = client.Client("https://example.test", "secret", opener=opener)
    assert len(list(api.events("task", seconds=1))) == 2
    assert opener.requests[1].get_header("Last-event-id") == "t:1"


def test_upload_is_multipart_and_not_retried(tmp_path):
    path = tmp_path / "evidence.txt"
    path.write_text("evidence")
    opener = Opener([urllib.error.URLError("uncertain")])
    with pytest.raises(urllib.error.URLError):
        client.Client("https://example.test", "secret", opener=opener).upload(
            path, "text/plain"
        )
    assert len(opener.requests) == 1
    assert b'name="metadata"' in opener.requests[0].data
    assert b'name="content"' in opener.requests[0].data


def test_artifact_digest_mismatch_fails():
    opener = Opener([Response(b"changed")])
    with pytest.raises(ValueError, match="digest"):
        client.Client("https://example.test", "secret", opener=opener).artifact(
            "task", "artifact"
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "https://user:secret@example.test",
        "https://example.test?token=secret",
    ],
)
def test_credentials_cannot_cross_unsafe_endpoint(url):
    with pytest.raises(ValueError):
        client.Client(url, "secret")


def test_empty_readiness_and_legacy_consumers_block(tmp_path):
    failures = readiness.inspect(
        {
            "worker_digest": "sha256:" + "a" * 64,
            "queue_consumers": [
                {
                    "name": "old-pod",
                    "source": "pod",
                    "task_capable": False,
                    "image_digest": "sha256:" + "b" * 64,
                }
            ],
        },
        tmp_path,
    )
    assert any("incompatible" in item for item in failures)
    assert any("enumeration incomplete" in item for item in failures)


def test_evidence_never_accepts_empty_or_mock_live_pass(tmp_path):
    manifest = {
        field: "test"
        for field in (
            "environment",
            "account_id",
            "source_sha",
            "image_digest",
            "evaluator",
            "started_at",
            "finished_at",
            "owned_resources",
            "cleanup",
        )
    }
    manifest.update(schema_version="1.0", bounds={"max_tasks": 1, "max_total_usd": 1})
    with pytest.raises(ValueError, match="Empty"):
        packager.package(manifest, tmp_path)
    manifest["results"] = [
        {"criterion_id": "V4-01", "outcome": "PASS", "lane": "mocked"}
    ]
    with pytest.raises(ValueError, match="Live PASS"):
        packager.package(manifest, tmp_path)


def test_readiness_example_is_blocked_without_crashing():
    path = ROOT / "docs/task-api/readiness-inventory.example.json"
    assert readiness.inspect(json.loads(path.read_text()), path.parent)


def test_retained_jobs_and_generic_authority_cannot_be_ignored(tmp_path):
    failures = readiness.inspect(
        {
            "flags": {"ADP_AGENT_AUTHORITY_ENABLED": True},
            "old_jobs_drained_or_proven_nonrestartable": False,
        },
        tmp_path,
    )
    assert any("old shared-queue jobs" in item for item in failures)
    assert any("generic agent authority" in item for item in failures)


def test_bound_old_worker_requires_complete_nonrestart_proof(tmp_path):
    import hashlib

    proof = tmp_path / "proof.json"
    proof.write_text("{}")
    consumer = {
        "name": "old",
        "source": "job",
        "image_digest": "old",
        "task_capable": False,
        "can_receive_new_work": False,
        "restart_disabled": True,
        "acquisition_complete": True,
        "one_acquisition_proven": True,
        "disposition_evidence": {
            "path": "proof.json",
            "sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
        },
    }
    inventory = {"queue_consumers": [consumer]}
    assert not any(
        x.startswith("incompatible or unverified")
        for x in readiness.inspect(inventory, tmp_path)
    )
    consumer["one_acquisition_proven"] = False
    assert any(
        x.startswith("incompatible or unverified")
        for x in readiness.inspect(inventory, tmp_path)
    )


def test_readiness_requires_actual_admission_flag_and_complete_rollout(tmp_path):
    failures = readiness.inspect(
        {
            "flags": {"ADP_TASK_API_SUBMIT_ENABLED": False},
            "gateway_rollout": {
                "desired": 7,
                "updated": 6,
                "ready": 7,
                "verified_image_ready_pods": 6,
            },
        },
        tmp_path,
    )
    assert any("canonical task flags" in x for x in failures)
    assert any("rollout has not converged" in x for x in failures)
