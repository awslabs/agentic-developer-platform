"""Knowledge CLI safety contracts; no live configuration or provider calls."""

import importlib.util
import json
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

CLI = Path(__file__).parents[2] / "cli"
spec = importlib.util.spec_from_file_location("knowledge_cli", CLI / "adp-knowledge.py")
k = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(k.common, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(k.common, "authenticated_scope", lambda: {"identity": "alice", "tenant": "tenant-a"})
    monkeypatch.setattr(k.common, "ensure_can_mutate", Mock())
    return Mock(base="https://example.test/api")


def run(client, *args):
    return k.execute(k.parser().parse_args(list(args)), client)


def source(tmp_path):
    path = tmp_path / "sources.json"
    body = {"scope": "personal", "items": [{"asset_type": "url", "source_ref": "https://example.test/guide"}]}
    path.write_text(json.dumps(body))
    return path, body


def preview(client, tmp_path):
    path, body = source(tmp_path)
    client.request.return_value = {"valid": body["items"], "rejected": [], "duplicates": [], "quota_ok": True}
    result = run(client, "knowledge", "bulk", "preview", "--file", str(path))
    return path, body, result["detail"]


def test_bulk_commit_uses_reviewed_receipt_and_does_not_dispatch_twice(client, tmp_path):
    path, body, detail = preview(client, tmp_path)
    path.write_text('{"items": [{"source_ref":"https://changed.test"}]}')
    client.request.return_value = {"created": 1, "assets": [{"id": "asset", "status": "queued"}]}
    args = ("knowledge", "bulk", "commit", "--preview-id", detail["preview_id"], "--expect-hash", detail["hash"], "--yes")
    result = run(client, *args)
    assert result["status"] == "pending"
    client.request.assert_called_with("POST", k.ASSETS + "/bulk/commit", body)
    calls = client.request.call_count
    assert run(client, *args)["detail"] == result["detail"]
    assert client.request.call_count == calls


@pytest.mark.parametrize("change", ["hash", "actor", "tenant", "gateway"])
def test_commit_scope_or_hash_change_refused(client, tmp_path, monkeypatch, change):
    _, _, detail = preview(client, tmp_path)
    if change in {"actor", "tenant"}:
        monkeypatch.setattr(k.common, "authenticated_scope", lambda: {"identity": "bob" if change == "actor" else "alice", "tenant": "tenant-b"})
    if change == "gateway":
        client.base = "https://other.test/api"
    client.request.reset_mock()
    with pytest.raises(k.common.CliError, match="hash or authenticated scope"):
        run(
            client,
            "knowledge",
            "bulk",
            "commit",
            "--preview-id",
            detail["preview_id"],
            "--expect-hash",
            "wrong" if change == "hash" else detail["hash"],
            "--yes",
        )
    client.request.assert_not_called()


def test_unknown_bulk_response_is_not_retried(client, tmp_path):
    _, _, detail = preview(client, tmp_path)
    client.request.side_effect = k.common.CliError("lost", "unknown_mutation_outcome", 4)
    args = ("knowledge", "bulk", "commit", "--preview-id", detail["preview_id"], "--expect-hash", detail["hash"], "--yes")
    with pytest.raises(k.common.CliError):
        run(client, *args)
    calls = client.request.call_count
    with pytest.raises(k.common.CliError, match="unknown"):
        run(client, *args)
    assert client.request.call_count == calls


@pytest.mark.parametrize(
    "url", ["https://user:secret@example.test", "https://example.test?q=secret", "http://example.test", "https://example.test/#secret"]
)
def test_credential_sources_refused(url):
    with pytest.raises(k.common.CliError):
        k.source_item({"asset_type": "url", "source_ref": url})


def test_status_requires_real_successful_stages():
    value = {"asset_id": "asset", "status": "queued", "stages": [], "run_status": None}
    assert k.status_result(value)["status"] == "pending"
    value.update(status="indexed", run_id="run", run_status="succeeded", stages=[{"stage": "clone", "status": "verified"}])
    assert k.status_result(value)["detail"]["usable"] is True
    value["stages"][0]["status"] = "failed"
    assert k.status_result(value)["detail"]["usable"] is False
    value["run_status"] = "failed"
    assert k.status_result(value)["status"] == "failed"


def test_output_redacts_free_text_source_and_artifact_credentials():
    value = k.safe(
        {
            "id": "asset",
            "source_ref": "https://u:secret@example.test?q=token",
            "last_error": "secret",
            "metadata": {"token": "secret"},
            "stages": [{"stage": "clone", "error": "secret", "artifact_ref": "s3://private/key?secret"}],
        }
    )
    assert "secret" not in json.dumps(value)
    assert value["last_error_present"] is True
    assert value["stages"][0]["artifact_ref_sha256"]


def test_delete_preview_has_no_request_and_describes_soft_removal(client):
    result = run(client, "knowledge", "delete", str(uuid.uuid4()))
    assert "artifacts retained" in result["detail"]["effect"]
    client.request.assert_not_called()


def test_reindex_passes_stable_key_to_canonical_api(client):
    asset, key = str(uuid.uuid4()), str(uuid.uuid4())
    client.request.return_value = {"id": asset, "status": "registered"}
    assert run(client, "knowledge", "reindex", asset, "--key", key, "--yes")["status"] == "pending"
    client.request.assert_called_once_with("POST", k.ASSETS + "/" + asset + "/reindex?request_id=" + key)


def test_admin_reads_use_existing_guarded_endpoint(client):
    client.request.return_value = {"items": [], "page": 1}
    run(client, "admin", "indexing", "list")
    client.request.assert_called_once_with("GET", "/admin/indexing/runs?page=1&page_size=20")


def test_add_receipt_deduplicates_and_rejects_changed_input(client, tmp_path):
    key = str(uuid.uuid4())
    body = {"asset_type": "url", "source_ref": "https://example.test"}
    client.request.return_value = {"id": "asset", "status": "queued"}
    k.mutation(client, key, body, "POST", k.ASSETS)
    k.mutation(client, key, body, "POST", k.ASSETS)
    client.request.assert_called_once()
    with pytest.raises(k.common.CliError, match="different inputs"):
        k.mutation(client, key, {**body, "source_ref": "https://changed.test"}, "POST", k.ASSETS)
