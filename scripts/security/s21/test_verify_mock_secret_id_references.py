"""Resource classification requires exact mock/provider identifier flow."""

import hashlib
import json

import pytest
import verify_mock_secret_id_references as verifier

REFERENCE = "arn:synthetic-fixture"
SOURCE = """from unittest.mock import MagicMock
from src.shared.services.secrets_manager import SecretsManagerHelper
import pytest
@pytest.fixture
def mock_sm_client():
    client = MagicMock()
    return client
@pytest.fixture
def helper(mock_sm_client):
    return SecretsManagerHelper(client=mock_sm_client)
def test_reference(helper, mock_sm_client):
    helper.get_secret_at_version("arn:synthetic-fixture", "version")
    mock_sm_client.get_secret_value.assert_called_once_with(SecretId="arn:synthetic-fixture", VersionId="version")
"""
RECORD = {
    "line": 13,
    "test_function": "test_reference",
    "helper_method": "get_secret_at_version",
    "provider_method": "get_secret_value",
}
SERVICE = """class SecretsManagerHelper:
    def __init__(self, *, client=None):
        self._client = client or boto3.client("secretsmanager")
    def get_secret_at_version(self, secret_arn, version_id):
        return self._client.get_secret_value(SecretId=secret_arn, VersionId=version_id)
    def delete_secret(self, secret_arn, *, force=True):
        kwargs: dict = {"SecretId": secret_arn}
        if force:
            kwargs["ForceDeleteWithoutRecovery"] = True
        self._client.delete_secret(**kwargs)
"""


def test_exact_fixture_and_provider_argument_pass():
    verifier.verify_context(SOURCE, RECORD, REFERENCE)
    verifier.verify_consumer(SERVICE, "get_secret_at_version")
    verifier.verify_consumer(SERVICE, "delete_secret")


@pytest.mark.parametrize(
    "before,after",
    [
        ("from unittest.mock", "from unrelated.module"),
        ("client = MagicMock()", "client = live_client()"),
        ("return client", "return live_client()"),
        (
            "SecretsManagerHelper(client=mock_sm_client)",
            "SecretsManagerHelper(client=live_client())",
        ),
        (
            "def test_reference(helper, mock_sm_client):",
            "def test_reference(helper, mock_sm_client, MagicMock):",
        ),
        ("helper.get_secret_at_version", "helper.update_secret"),
        (
            "get_secret_value.assert_called_once_with",
            "other_method.assert_called_once_with",
        ),
        ("SecretId=", "SecretString="),
        (
            'SecretId="arn:synthetic-fixture"',
            'SecretId="arn:synthetic-fixture" + suffix',
        ),
    ],
)
def test_fixture_shape_without_flow_is_refused(before, after):
    with pytest.raises((ValueError, KeyError)):
        verifier.verify_context(SOURCE.replace(before, after), RECORD, REFERENCE)


@pytest.mark.parametrize(
    "prefix",
    [
        "MagicMock = replacement\n",
        "from unrelated.module import MagicMock\n",
        "from unrelated.module import *\n",
    ],
)
def test_conflicting_constructor_bindings_refused(prefix):
    with pytest.raises(ValueError):
        verifier.verify_context(prefix + SOURCE, dict(RECORD, line=14), REFERENCE)


@pytest.mark.parametrize(
    "before,after,method",
    [
        (
            "self._client = client or",
            "self._client = other or",
            "get_secret_at_version",
        ),
        ("SecretId=secret_arn", "SecretString=secret_arn", "get_secret_at_version"),
        ("SecretId=secret_arn", "SecretId=other", "get_secret_at_version"),
        ('{"SecretId": secret_arn}', '{"SecretString": secret_arn}', "delete_secret"),
        ('kwargs["ForceDeleteWithoutRecovery"]', 'kwargs["SecretId"]', "delete_secret"),
        ("delete_secret(**kwargs)", "delete_secret(**other)", "delete_secret"),
    ],
)
def test_consumer_reference_binding_cannot_change(before, after, method):
    with pytest.raises(ValueError):
        verifier.verify_consumer(SERVICE.replace(before, after), method)


def test_exact_line_and_complete_candidate_required():
    with pytest.raises(ValueError):
        verifier.verify_context(SOURCE, dict(RECORD, line=12), REFERENCE)
    with pytest.raises(ValueError):
        verifier.verify_context(SOURCE, RECORD, REFERENCE + "-changed")


@pytest.fixture
def private_fixture(tmp_path, monkeypatch):
    digest = hashlib.sha1(REFERENCE.encode()).hexdigest()
    record = dict(
        RECORD,
        file=verifier.TEST_FILE,
        selector=f"detect-secrets|{verifier.TEST_FILE}|ri=0",
        detector="Secret Keyword",
        candidate_hash_prefix=digest[:16],
    )
    scan = {
        "results": {
            verifier.TEST_FILE: [
                {"line_number": 13, "type": "Secret Keyword", "hashed_secret": digest}
            ]
        }
    }
    audit = {
        "results": [
            {"filename": verifier.TEST_FILE, "lines": [13], "secrets": REFERENCE}
        ]
    }
    receipt = {
        "source_revision": "a" * 40,
        "verified_delta": 1,
        "verified_records": [record],
    }
    paths = [tmp_path / name for name in ("scan.json", "audit.json", "receipt.json")]
    for path, data in zip(paths, [scan, audit, receipt]):
        path.write_text(json.dumps(data))
    monkeypatch.setattr(
        verifier,
        "git",
        lambda _root, _show, revision: (
            SOURCE if revision.endswith(verifier.TEST_FILE) else SERVICE
        ),
    )
    return paths


def test_complete_original_scan_and_audit_join(private_fixture, tmp_path):
    verifier.verify(tmp_path, *private_fixture)


@pytest.mark.parametrize(
    "mutation", ["wrong_full_audit_candidate", "wrong_line", "wrong_index", "duplicate"]
)
def test_private_join_mismatch_refused(private_fixture, tmp_path, mutation):
    _scan_path, audit_path, receipt_path = private_fixture
    if mutation == "wrong_full_audit_candidate":
        data = json.loads(audit_path.read_text())
        data["results"][0]["secrets"] += "-different"
        audit_path.write_text(json.dumps(data))
    else:
        data = json.loads(receipt_path.read_text())
        if mutation == "wrong_line":
            data["verified_records"][0]["line"] = 12
        if mutation == "wrong_index":
            data["verified_records"][0]["selector"] = (
                f"detect-secrets|{verifier.TEST_FILE}|ri=1"
            )
        if mutation == "duplicate":
            data["verified_records"] *= 2
            data["verified_delta"] = 2
        receipt_path.write_text(json.dumps(data))
    with pytest.raises((ValueError, KeyError, IndexError)):
        verifier.verify(tmp_path, *private_fixture)
