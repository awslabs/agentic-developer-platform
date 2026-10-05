"""Background change probes must not bypass ingestion source authorization."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock


def load_publisher(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "images/ingestion/publish-ingestion.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("publisher_probe_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_url_change_probe_refuses_metadata_before_network(monkeypatch):
    import requests

    publisher = load_publisher(monkeypatch)
    head = Mock(return_value=Mock(status_code=200, headers={}))
    monkeypatch.setattr(requests, "head", head)
    assert publisher._url_has_changed("http://169.254.169.254/latest/meta-data/", {})
    head.assert_not_called()


def test_document_change_probe_refuses_foreign_owner_before_s3(monkeypatch):
    publisher = load_publisher(monkeypatch)
    monkeypatch.setattr(publisher.settings, "s3_bucket_name", "data-bucket")
    client = Mock(side_effect=AssertionError("foreign metadata read"))
    monkeypatch.setattr(publisher.boto3, "client", client)
    assert publisher._doc_has_changed("s3://data-bucket/tenants/other/private.pdf", {})
    client.assert_not_called()
