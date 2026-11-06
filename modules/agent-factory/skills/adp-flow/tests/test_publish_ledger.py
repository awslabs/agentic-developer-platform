"""Exercise issue publication through a local GitHub CLI stub, without network writes."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

PUBLISH_PATH = Path(__file__).resolve().parents[1] / "scripts" / "publish.py"
SPEC = importlib.util.spec_from_file_location("adp_flow_publish", PUBLISH_PATH)
PUBLISH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PUBLISH)

STUB = (Path(__file__).resolve().parent / "fixtures/gh_stub.py").read_text(encoding="utf-8")


@pytest.fixture
def backlog(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "gh"
    executable.write_text(STUB)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    state_path = tmp_path / "state.json"
    monkeypatch.setenv("GH_STUB_STATE", str(state_path))
    issues = [
        {"alias": "epic", "kind": "epic", "repository": "example/home", "title": "Epic", "body": "Epic body"},
        {"alias": "s1", "kind": "story", "repository": "example/code", "title": "First", "body": "First body"},
        {"alias": "s2", "kind": "story", "repository": "example/home", "title": "Second", "body": "Second body", "depends_on": ["s1"]},
        {"alias": "e1", "kind": "eval", "repository": "example/home", "title": "Evaluate one", "body": "Evaluate"},
        {"alias": "e2", "kind": "eval", "repository": "example/home", "title": "Evaluate two", "body": "Evaluate"},
    ]
    (tmp_path / "backlog.json").write_text(json.dumps({"issues": issues}))
    remote = [{"repo": item["repository"], "number": index + 100, "title": item["title"],
               "url": f"https://github.com/{item['repository']}/issues/{index + 100}"}
              for index, item in enumerate(issues[:3])]
    state_path.write_text(json.dumps({"remote": remote, "creates": 0, "next_number": 200, "links": {}}))
    ledger = {"issues": {item["alias"]: {"repository": item["repository"], "number": index + 100,
                                   "title": item["title"], "node_id": f"NODE-{item['repository']}-{index + 100}",
                                   "url": remote[index]["url"], "linked": False}
                         for index, item in enumerate(issues[:2])}}
    (tmp_path / "story-mapping.json").write_text(json.dumps(ledger))
    return tmp_path, state_path


def test_partial_ledger_and_exact_title_match_do_not_duplicate(backlog):
    directory, state_path = backlog
    ledger = PUBLISH.publish(directory)
    state = json.loads(state_path.read_text())
    assert set(ledger["issues"]) == {"epic", "s1", "s2", "e1", "e2"}
    assert ledger["issues"]["s2"]["number"] == 102
    assert state["creates"] == 2
    assert all(ledger["issues"][alias]["linked"] for alias in ("s1", "s2", "e1", "e2"))
    assert PUBLISH.publish(directory) == ledger
    assert json.loads(state_path.read_text())["creates"] == 2


def test_interrupted_publication_resumes_from_ledger(backlog):
    directory, state_path = backlog
    state = json.loads(state_path.read_text())
    state["fail_once"] = True
    state_path.write_text(json.dumps(state))
    with pytest.raises(Exception):
        PUBLISH.publish(directory)
    checkpoint = json.loads((directory / "story-mapping.json").read_text())
    assert set(checkpoint["issues"]) == {"epic", "s1", "s2", "e1"}
    ledger = PUBLISH.publish(directory)
    assert len(ledger["issues"]) == 5
    assert json.loads(state_path.read_text())["creates"] == 2


def test_failed_node_lookup_resumes_journal_without_search_visibility(backlog):
    directory, state_path = backlog
    state = json.loads(state_path.read_text())
    state["fail_node_once"] = True
    state["hide_created_from_search"] = True
    state_path.write_text(json.dumps(state))
    with pytest.raises(Exception):
        PUBLISH.publish(directory)
    checkpoint = json.loads((directory / "story-mapping.json").read_text())
    assert set(checkpoint["issues"]) == {"epic", "s1", "s2", "e1"}
    entry = checkpoint["issues"]["e1"]
    assert entry["repository"] == "example/home"
    assert entry["title"] == "Evaluate one"
    assert entry["number"] == 200
    assert entry["url"] == "https://github.com/example/home/issues/200"
    assert entry["created_at"]
    assert not entry.get("node_id")
    assert json.loads(state_path.read_text())["creates"] == 1
    ledger = PUBLISH.publish(directory)
    assert len(ledger["issues"]) == 5
    assert ledger["issues"]["e1"]["number"] == 200
    assert ledger["issues"]["e1"]["node_id"] == "NODE-example/home-200"
    assert ledger["issues"]["e1"]["created_at"] == entry["created_at"]
    assert all(ledger["issues"][alias]["linked"] for alias in ("s1", "s2", "e1", "e2"))
    assert json.loads(state_path.read_text())["creates"] == 2
