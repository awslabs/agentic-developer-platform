"""Source policy at worker launch and background publisher boundaries."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

INGESTION = Path(__file__).resolve().parents[2] / "images/ingestion"
sys.path.insert(0, str(INGESTION))

# The deployed scripts are modules at the ingestion image's /app root.
import url_fetch  # noqa: E402
from scope import IngestionScope  # noqa: E402
from source_admission import SourceAdmissionError, validate_source  # noqa: E402


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_source_review", INGESTION / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("kind", "source"), [
    ("url", "http://169.254.169.254/latest/meta-data"),
    ("doc", "s3://data-bucket/tenants/team-b/docs/secret.pdf"),
    ("repo", "org/repo/../../elsewhere"),
    ("unregistered", "https://example.com/"),
])
def test_worker_rejects_before_subprocess_or_status_write(monkeypatch, kind, source):
    worker = load_script("sqs-worker")
    monkeypatch.setattr(worker, "SQS_QUEUE_URL", "https://queue.example")
    monkeypatch.setattr(worker.settings, "s3_bucket_name", "data-bucket")
    monkeypatch.setattr(worker, "receive_sqs_message", lambda: ({
        "content_type": kind, "source": source,
        "scope": {"visibility": "tenant", "tenant_id": "team-a"},
    }, "receipt"))
    launch, status = Mock(), Mock()
    monkeypatch.setattr(worker, "_run_subprocess", launch)
    monkeypatch.setattr(worker, "update_dynamo_status", status)
    with pytest.raises(SystemExit) as exc:
        worker.main()
    assert exc.value.code == 1
    launch.assert_not_called()
    status.assert_not_called()


@pytest.mark.parametrize(("kind", "source"), [
    ("repo", "org/repo"),
    ("repo", "git@github.com:org/repo.git"),
    ("url", "https://93.184.216.34/page"),
    ("doc", "https://93.184.216.34/manual.pdf"),
    ("doc", "s3://data-bucket/tenants/team-a/docs/file.pdf"),
])
def test_known_source_types_retain_permitted_scope(kind, source):
    validate_source(kind, source, IngestionScope(visibility="tenant", tenant_id="team-a"), default_bucket="data-bucket")


def test_internal_inventory_publisher_retains_account_identifiers():
    validate_source("infra", "123456789012:ReadOnlyRole:us-east-1,eu-west-2", IngestionScope(), allow_infra=True)
    with pytest.raises(SourceAdmissionError):
        validate_source("infra", "123456789012", IngestionScope())


def test_publisher_head_follows_guarded_redirects_and_preserves_etag(monkeypatch):
    publisher = load_script("publish-ingestion")
    transport = Mock(return_value=url_fetch.FetchResponse(
        url="https://93.184.216.34/page", status_code=200, headers={"etag": "existing"}, content=b""
    ))
    monkeypatch.setattr(url_fetch, "_transport_fetch", transport)
    assert not publisher._url_has_changed("https://93.184.216.34/page", {"last_etag": "existing"})
    assert transport.call_args.args[0] == "HEAD"
    transport.reset_mock()
    transport.return_value = url_fetch.FetchResponse(
        url="https://93.184.216.34/page", status_code=302, headers={"location": "http://127.0.0.1/private"}, content=b""
    )
    assert publisher._url_has_changed("https://93.184.216.34/page", {})
    assert transport.call_count == 1


def test_publisher_cannot_probe_foreign_s3_metadata(monkeypatch):
    publisher = load_script("publish-ingestion")
    monkeypatch.setattr(publisher.settings, "s3_bucket_name", "data-bucket")
    client = Mock(side_effect=AssertionError("foreign S3 metadata must not be fetched"))
    monkeypatch.setattr(publisher.boto3, "client", client)
    assert publisher._doc_has_changed("s3://data-bucket/tenants/foreign/private.pdf", {})
    client.assert_not_called()


def test_file_publisher_denies_before_change_probe_or_publish(monkeypatch):
    publisher = load_script("publish-ingestion")
    monkeypatch.setattr(publisher, "parse_source_file", lambda *_: [("http://169.254.169.254/latest", None, {})])
    state, send = Mock(), Mock()
    monkeypatch.setattr(publisher, "get_dynamo_state", state)
    monkeypatch.setattr(publisher, "publish_message", send)
    assert publisher.publish("url", "unused") == {"total": 1, "enqueued": 0, "skipped": 0, "errors": 1}
    state.assert_not_called()
    send.assert_not_called()
