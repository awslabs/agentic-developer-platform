"""UI-equivalent CLI storage actions, dry runs and private transfers."""

import json
import urllib.error
from unittest.mock import Mock

import pytest

from tests.cli.test_adp_knowledge import ASSET_ID, client, k, run  # noqa: F401

PRODUCT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BASE = f"/agent-context/products/{PRODUCT}/storage"


def invoke(api_client, *args):
    return run(api_client, "knowledge", "storage", *args, "--product", PRODUCT)


def test_configure_dry_run_and_revision(client):  # noqa: F811
    args = ("configure", "--encryption", "customer", "--revision", "3", "--kms-key-arn", "product-key")
    assert invoke(client, *args, "--dry-run", "--yes")["status"] == "preview"
    client.request.assert_not_called()
    client.request.return_value = {"product_id": PRODUCT, "revision": 4, "encryption": "customer", "kms_key_arn": "product-key"}
    assert invoke(client, *args, "--yes")["status"] == "ok"
    client.request.assert_called_once_with("PUT", BASE, {"revision": 3, "encryption": "customer", "kms_key_arn": "product-key"})


def test_upload_and_resume_never_print_signed_url(client, monkeypatch, tmp_path):  # noqa: F811
    path = tmp_path / "guide.md"
    path.write_bytes(b"guide")
    transfer = Mock(return_value=b"")
    monkeypatch.setattr(k.adp_context_storage, "transfer", transfer)
    client.request.side_effect = [
        {"product_id": PRODUCT, "upload_id": ASSET_ID, "url": "https://s3.example/?signature=secret", "headers": {}},
        k.common.CliError("failed", "unavailable", 5),
    ]
    with pytest.raises(k.common.CliError) as error:
        invoke(client, "upload", "--file", str(path), "--yes")
    assert ASSET_ID in str(error.value) and "secret" not in str(error.value)
    transfer.assert_called_once()
    client.request.side_effect = None
    client.request.return_value = {"product_id": PRODUCT, "id": ASSET_ID, "status": "queued"}
    result = invoke(client, "complete", "--upload-id", ASSET_ID, "--yes")
    assert result["status"] == "pending" and "url" not in json.dumps(result)
    assert client.request.call_args.args == ("POST", BASE + f"/uploads/{ASSET_ID}/complete")


def test_foreign_responses_and_existing_download_file_are_refused(client, tmp_path):  # noqa: F811
    client.request.return_value = {"product_id": "other"}
    with pytest.raises(k.common.CliError):
        invoke(client, "show")
    target = tmp_path / "private.md"
    target.write_text("keep")
    with pytest.raises(k.common.CliError):
        invoke(client, "download", "--asset-id", ASSET_ID, "--output", str(target))
    assert target.read_text() == "keep"


def test_transfer_omits_auth_and_scrubs_provider_errors(monkeypatch):
    opener = Mock()
    opener.open.side_effect = urllib.error.URLError("https://s3.example/?signature=secret")
    monkeypatch.setattr(k.adp_context_storage.urllib.request, "build_opener", lambda *a: opener)
    with pytest.raises(k.common.CliError) as error:
        k.adp_context_storage.transfer("https://s3.example/?signature=secret", payload=b"hi", headers={"Content-Type": "text/plain"})
    assert "secret" not in str(error.value)
    request = opener.open.call_args.args[0]
    assert request.get_method() == "PUT" and request.get_header("Authorization") is None
