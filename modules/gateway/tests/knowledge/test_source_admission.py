"""Exercise registration and queue admission before data writes or dispatch."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from src.knowledge import dispatch, routes
from src.knowledge.schemas import AssetCreateRequest, BulkCommitRequest
from src.knowledge.source_admission import SourceAdmissionError, admit_source
from src.knowledge.type_registry import ASSET_TYPE_REGISTRY


@pytest.mark.parametrize("name", ["scope.py", "s3_source_guard.py", "url_denylist.py", "source_admission.py"])
def test_gateway_policy_matches_ingestion_source(name):
    root = Path(__file__).resolve().parents[4]

    def implementation(path):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {"scope", "s3_source_guard", "url_denylist"}:
                node.level = 0  # Separate Python packages require different imports.
        return ast.dump(tree, include_attributes=False)

    assert implementation(root / "modules/gateway/src/knowledge/source_policy" / name) == implementation(
        root / "modules/agent-context/images/ingestion" / name
    )


@pytest.mark.parametrize("source", ["http://169.254.169.254/latest/meta-data", "http://2130706433/", "https://user:secret@example.com/"])
async def test_registration_refuses_url_before_database_write(source):
    db = AsyncMock()
    caller = SimpleNamespace(org_id="team-a", user_id="alice", is_admin=True)
    with pytest.raises(HTTPException) as exc:
        await routes.register_asset(AssetCreateRequest(asset_type="url", source_ref=source, scope="tenant"), db, db, caller)
    assert exc.value.status_code == 400
    db.execute.assert_not_called()
    db.commit.assert_not_called()


async def test_bulk_refuses_unsafe_source_before_any_insert():
    db = AsyncMock()
    caller = SimpleNamespace(org_id="team-a", user_id="alice", is_admin=True)
    body = BulkCommitRequest(
        scope="tenant",
        items=[
            {"asset_type": "url", "source_ref": "https://docs.example.com/good"},
            {"asset_type": "url", "source_ref": "http://169.254.169.254/private"},
        ],
    )
    with pytest.raises(HTTPException) as exc:
        await routes.bulk_commit(body, db, db, caller)
    assert exc.value.status_code == 400
    db.execute.assert_not_called()
    db.commit.assert_not_called()


async def test_retry_dispatch_refuses_unsafe_source_before_sqs(monkeypatch):
    sqs = Mock()
    monkeypatch.setattr(dispatch, "get_sqs_client", sqs)
    assert not await dispatch.dispatch_ingestion("asset", "url", "http://127.0.0.1/", "team-a", None, None, AsyncMock())
    sqs.assert_not_called()


async def test_source_ownership_and_legitimate_sources(monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_S3_BUCKET", "data-bucket")
    await admit_source("url", "https://docs.example.com/page", "team-a", None)
    await admit_source("doc", "s3://data-bucket/tenants/team-a/docs/a.pdf", "team-a", None)
    await admit_source("doc", "s3://data-bucket/users/alice/docs/a.pdf", "team-a", "alice")
    for source in ("s3://data-bucket/tenants/team-b/docs/a.pdf", "s3://data-bucket/users/bob/docs/a.pdf", "s3://data-bucket/unlabelled.pdf"):
        with pytest.raises(SourceAdmissionError):
            await admit_source("doc", source, "team-a", None)
    with pytest.raises(SourceAdmissionError):
        await admit_source("url", "https://docs.example.com/page", None, None)


async def test_unregistered_validator_denies_even_matching_pattern(monkeypatch):
    monkeypatch.setitem(ASSET_TYPE_REGISTRY, "new_type", {"source_ref_pattern": r"^https://"})
    with pytest.raises(SourceAdmissionError):
        await admit_source("new_type", "https://docs.example.com/page", "team-a", None)
    monkeypatch.setitem(ASSET_TYPE_REGISTRY, "new_type", {})
    with pytest.raises(SourceAdmissionError):
        await admit_source("new_type", "https://docs.example.com/page", "team-a", None)
