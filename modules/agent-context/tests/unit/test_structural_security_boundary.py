"""Structural content must have its own provenance and pass authorization."""

import importlib.util
import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))
from scope import IngestionScope
from door import server
from door.acl import CallerPrincipal
from door.structural_backend import load_code_index


def load_ingestion():
    path = Path(__file__).resolve().parents[2] / "images" / "ingestion" / "ingest-repo.py"
    spec = importlib.util.spec_from_file_location("ingest_repo_security_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "index",
    [
        {"repo_id": "other/secret", "symbols": []},
        {"symbols": []},
        {"repo_id": "org/service", "repo": "other/secret", "symbols": []},
    ],
)
async def test_structural_index_cannot_inherit_a_permitted_request(index):
    s3 = MagicMock()
    s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
    s3.get_object.side_effect = lambda **_: {"Body": BytesIO(json.dumps(index).encode())}
    assert (
        await load_code_index(
            "org/service", s3_client=s3, bucket="test", prefix="content/code-indexes"
        )
        == {}
    )
    s3.list_objects_v2.assert_not_called()


@pytest.mark.parametrize("verb", ["understand", "impact"])
async def test_unauthorized_structural_request_does_not_load_debug_bytes(monkeypatch, verb):
    acl = MagicMock()
    acl.get_allowed_repos.return_value = {"org/service"}
    s3 = MagicMock()
    s3.get_object.return_value = {
        "Body": BytesIO(b'{"repo":"other/secret","symbols":[{"name":"SECRET"}]}')
    }
    monkeypatch.setattr(server.state, "s3_client", s3)
    monkeypatch.setattr(server.state, "acl_store", acl)
    monkeypatch.setattr(server.config, "s3_bucket", "offline-test")
    backend = AsyncMock(return_value=[])
    monkeypatch.setattr(server, verb, backend)
    caller = CallerPrincipal(github_login="alice", tenant_id="team-a")
    result = await getattr(server, f"_handle_{verb}")({"target": "other/secret::password"}, caller)
    assert "_debug" not in result
    assert "SECRET" not in json.dumps(result)
    backend.assert_not_awaited()
    s3.get_object.assert_not_called()


@pytest.mark.parametrize("failure", ["connection", "ownership"])
def test_ingestion_refuses_before_cloning_when_acl_registration_fails(monkeypatch, failure):
    module = load_ingestion()
    for name, value in IngestionScope(visibility="tenant", tenant_id="team-a").to_env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(module, "S3ContentStore", Mock())
    monkeypatch.setattr(module, "resolve_allowed_principals", Mock(return_value=["alice"]))
    clone = Mock()
    monkeypatch.setattr(module, "git_clone", clone)
    conn = Mock()
    monkeypatch.setattr(
        "db.get_connection",
        Mock(
            side_effect=RuntimeError("offline") if failure == "connection" else None,
            return_value=conn,
        ),
    )
    monkeypatch.setattr(
        "db.ensure_repo_exists", Mock(side_effect=RuntimeError("ownership conflict"))
    )
    with pytest.raises(RuntimeError, match="refusing ingestion"):
        module.ingest_repo("org/service")
    clone.assert_not_called()


async def test_scoped_index_written_by_ingestion_is_readable_with_its_actual_repo_field(
    monkeypatch, tmp_path
):
    module = load_ingestion()
    monkeypatch.setattr(module, "CODE_INDEX_DIR", str(tmp_path / "code-indexes"))
    scope = IngestionScope(visibility="tenant", tenant_id="team-a")
    content = {"repo": "org/service", "symbols": [{"name": "connect"}]}
    assert module._write_code_index_to_filesystem(
        json.dumps(content), "org-service", "org/service", scope=scope
    )
    key = "tenants/team-a/code-indexes/org-service.json"
    assert json.loads((tmp_path / key).read_text()) == content
    s3 = MagicMock()
    s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
    s3.get_object.side_effect = lambda **kw: {"Body": BytesIO((tmp_path / kw["Key"]).read_bytes())}
    result = await load_code_index(
        "org/service", s3_client=s3, bucket="offline-test", prefix="tenants/team-a/code-indexes"
    )
    assert result == content
    assert s3.get_object.call_count == 1
