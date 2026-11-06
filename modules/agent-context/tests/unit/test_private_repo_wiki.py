"""Both ingestion paths pass the existing installation credential to DeepWiki."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

INGESTION = Path(__file__).resolve().parents[2] / "images" / "ingestion"
sys.path.insert(0, str(INGESTION))


@pytest.mark.parametrize("script", ["ingest-repo.py", "refresh-repos.py"])
@pytest.mark.parametrize("private", [False, True])
def test_wiki_clone_receives_pod_credential_without_logging_it(monkeypatch, caplog, script, private):
    spec = importlib.util.spec_from_file_location("wiki_credential_" + script, INGESTION / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(Path, "is_file", lambda _: private)
    monkeypatch.setattr(Path, "read_text", lambda _: "fixture-installation-secret\n")
    post = Mock(return_value=Mock(status_code=200, text="source-grounded wiki " * 100))
    monkeypatch.setattr(module.requests, "post", post)
    assert module.deepwiki_generate("example/project")
    payload = post.call_args.kwargs["json"]
    if private:
        assert payload["token"] == "fixture-installation-secret"
    else:
        assert "token" not in payload
    assert payload["repo_url"] == "https://github.com/example/project"
    assert post.call_args.kwargs["allow_redirects"] is False
    post.return_value = Mock(status_code=500, text="clone failure fixture-installation-secret")
    assert module.deepwiki_generate("example/project") is None
    assert "fixture-installation-secret" not in caplog.text
