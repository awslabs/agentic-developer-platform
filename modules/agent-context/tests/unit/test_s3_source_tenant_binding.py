"""A shared IAM role and bucket do not authorize another customer's source."""

import sys
import importlib.util
from unittest.mock import Mock
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))
from scope import IngestionScope
from s3_source_guard import check_s3_source

BUCKET = "adp-context-data"
TENANT = IngestionScope(visibility="tenant", tenant_id="team-a")
PERSONAL = IngestionScope(visibility="personal", owner_sub="alice")


@pytest.mark.parametrize("scope", [None, IngestionScope(), TENANT, PERSONAL])
@pytest.mark.parametrize(
    "key",
    [
        "tenants/team-b/docs/private.pdf",
        "users/bob/docs/private.pdf",
        "tenants/team-ab/docs/private.pdf",
        "users/alice-other/docs/private.pdf",
        "tenants/team-a/../team-b/docs/private.pdf",
        "tenants/team-a/%2e%2e/team-b/docs/private.pdf",
    ],
)
def test_other_owner_is_refused_even_with_broad_operator_allowlist(scope, key):
    decision = check_s3_source(
        f"s3://{BUCKET}/{key}",
        default_bucket=BUCKET,
        allowlist_raw=f"{BUCKET}/tenants,{BUCKET}/users",
        scope=scope,
    )
    assert not decision.allowed
    assert decision.reason_code == "scope_not_allowed"


@pytest.mark.parametrize(
    ("scope", "key"),
    [
        (TENANT, "tenants/team-a/docs/sprint.pdf"),
        (PERSONAL, "users/alice/docs/private.pdf"),
    ],
)
def test_legitimate_owned_source_is_preserved(scope, key):
    decision = check_s3_source(f"s3://{BUCKET}/{key}", default_bucket=BUCKET, scope=scope)
    assert decision.allowed
    assert decision.key == key


@pytest.mark.parametrize("scope", [None, IngestionScope(), TENANT, PERSONAL])
def test_unlabelled_objects_are_not_made_public_by_the_default_bucket(scope):
    assert not check_s3_source(
        f"s3://{BUCKET}/docs/unlabelled.pdf", default_bucket=BUCKET, scope=scope
    ).allowed


@pytest.mark.parametrize(
    "scope",
    [
        IngestionScope(visibility="tenant", tenant_id="team-a/../team-b"),
        IngestionScope(visibility="tenant", tenant_id="team-a%2f..%2fteam-b"),
        IngestionScope(visibility="personal", owner_sub="alice/../bob"),
    ],
)
def test_malformed_scope_cannot_construct_another_owner_root(scope):
    assert not check_s3_source(
        f"s3://{BUCKET}/tenants/team-b/docs/private.pdf", default_bucket=BUCKET, scope=scope
    ).allowed


def test_explicit_shared_source_remains_prefix_bounded():
    kwargs = dict(default_bucket=BUCKET, allowlist_raw=f"{BUCKET}/content/catalog", scope=TENANT)
    assert check_s3_source(f"s3://{BUCKET}/content/catalog/handbook.md", **kwargs).allowed
    assert not check_s3_source(
        f"s3://{BUCKET}/content/catalog-private/secrets.md", **kwargs
    ).allowed


def test_whole_bucket_allowlist_is_not_an_ownership_decision():
    assert not check_s3_source(
        "s3://external-bucket/private.pdf", allowlist_raw="external-bucket"
    ).allowed


@pytest.mark.parametrize(
    ("key", "allowed"),
    [
        ("tenants/team-a/docs/sprint.pdf", True),
        ("tenants/team-b/docs/private.pdf", False),
        ("users/bob/docs/private.pdf", False),
        ("docs/unlabelled.pdf", False),
    ],
)
def test_document_fetch_binds_scope_before_opening_s3(monkeypatch, tmp_path, key, allowed):
    script = Path(__file__).resolve().parents[2] / "images" / "ingestion" / "ingest-doc.py"
    spec = importlib.util.spec_from_file_location("ingest_doc_source_review", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "settings", SimpleNamespace(s3_bucket_name=BUCKET, s3_source_allowlist="")
    )
    for name, value in TENANT.to_env().items():
        monkeypatch.setenv(name, value)
    s3 = Mock()
    factory = Mock(return_value=s3)
    monkeypatch.setattr("boto3.client", factory)
    result = module._fetch_from_s3(f"s3://{BUCKET}/{key}", str(tmp_path))
    if allowed:
        assert result == str(tmp_path / "sprint.pdf")
        s3.download_file.assert_called_once_with(BUCKET, key, result)
    else:
        assert result is None
        factory.assert_not_called()
