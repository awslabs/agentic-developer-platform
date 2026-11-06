"""Inherited checklists cannot become authority or hide protected PR content."""

import base64
import copy
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from src.agentauth.github_operations import OperationRefusedError
from src.agentauth.github_provider import ProviderConflictError
from src.agentauth.operations_progress_board import MAX_BOARD_BYTES, verify_progress_board
from src.agentauth.operations_repair import merge_repair
from src.orchestration.models import OrchestrationFlow, OrchestrationNode
from tests.agentauth import test_operations_repair as repair_tests
from tests.agentauth.test_github_provider import HEAD, Counter, Recorder, _assignment
from tests.agentauth.test_operations_repair import advanced_main, authorization, provider

provider_data = repair_tests.provider_data

BOARD_PATH = ".adp/tasks/5223.json"
BODY = SimpleNamespace(pull_number=7, expected_head=HEAD)


def board(version=2):
    return {
        "version": version,
        "issue": 5223,
        "tasks": [
            {"id": "source", "kind": "code", "status": "done", "title": "Prior implementation", "commits": ["c" * 40]},
            {"id": "verify", "kind": "test", "status": "in_progress", "title": "Verify repair", "covers": ["source"]},
        ],
    }


@pytest.fixture
async def story(db_session_factory, monkeypatch):
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: db_session_factory)
    async with db_session_factory() as session:
        session.add(OrchestrationFlow(id="flow-1", org_id="org-a", slug="repair", title="Repair"))
        node = OrchestrationNode(
            id="node-1",
            org_id="org-a",
            flow_id="flow-1",
            epic_ref="epic",
            wave_ref="wave",
            node_ref="story",
            kind="story",
            title="Repair",
            issue_ref="5223",
            repository_identity="acme/widgets",
            provider_repository_id=987654,
        )
        session.add(node)
        await session.commit()
        yield session, node


def install_board(rec, files, value=None, raw=None):
    raw = json.dumps(board() if value is None else value).encode() if raw is None else raw
    digest = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    entry = {"filename": BOARD_PATH, "status": "modified", "sha": digest}
    files.append(entry)
    commit = {"sha": HEAD, "tree": {"sha": "1" * 40}}
    trees = [
        {"sha": "1" * 40, "truncated": False, "tree": [{"path": ".adp", "mode": "040000", "type": "tree", "sha": "2" * 40}]},
        {"sha": "2" * 40, "truncated": False, "tree": [{"path": "tasks", "mode": "040000", "type": "tree", "sha": "3" * 40}]},
        {"sha": "3" * 40, "truncated": False, "tree": [{"path": "5223.json", "mode": "100644", "type": "blob", "sha": digest}]},
    ]
    blob = {"sha": digest, "encoding": "base64", "size": len(raw), "content": base64.b64encode(raw).decode()}
    rec.overrides[f"GET /repos/acme/widgets/git/commits/{HEAD}"] = lambda: httpx.Response(200, json=commit)
    for tree in trees:
        rec.overrides[f"GET /repos/acme/widgets/git/trees/{tree['sha']}"] = lambda tree=tree: httpx.Response(200, json=tree)
    rec.overrides[f"GET /repos/acme/widgets/git/blobs/{digest}"] = lambda: httpx.Response(200, json=blob)
    return entry, commit, trees, blob


def allowed():
    result = authorization()
    result.assignment = _assignment()
    return result


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("state", ["ready", "behind", "merged"])
async def test_mixed_board_and_source_merge_refresh_and_readback(provider_data, story, state, version):
    rec, pull, _, files = provider_data
    original = board(version)
    snapshot = copy.deepcopy(original)
    install_board(rec, files, original)
    if state == "behind":
        advanced_main(provider_data)
        rec.overrides["PUT /repos/acme/widgets/pulls/7/update-branch"] = httpx.Response(202, json={})
    elif state == "merged":
        pull.update(merged=True, merge_commit_sha="d" * 40, state="closed")
    async with provider(rec) as gh:
        result = await merge_repair(gh, BODY, allowed(), Counter())
    assert result["merged"] is (state != "behind")
    assert original == snapshot  # Completed task metadata is neither reset nor converted to acceptance evidence.
    mutations = [(method, path, body) for method, path, body in rec.requests if method != "GET"]
    if state == "merged":
        assert mutations == []
    elif state == "behind":
        assert result["reason"] == "repair_branch_update_requested"
        assert mutations == [("PUT", "/repos/acme/widgets/pulls/7/update-branch", {"expected_head_sha": HEAD})]
    else:
        assert mutations == [("PUT", "/repos/acme/widgets/pulls/7/merge", {"sha": HEAD, "merge_method": "merge"})]
    reads = [path for method, path, _ in rec.requests if "/git/commits/" in path]
    assert reads and all(path.endswith(HEAD) for path in reads)


@pytest.mark.parametrize("state", ["ready", "behind", "merged"])
async def test_board_only_is_not_a_source_repair(provider_data, story, state):
    rec, pull, _, files = provider_data
    files.clear()
    install_board(rec, files)
    if state == "behind":
        advanced_main(provider_data)
    elif state == "merged":
        pull.update(merged=True, merge_commit_sha="d" * 40)
    async with provider(rec) as gh:
        with pytest.raises(OperationRefusedError, match="progress_only"):
            await merge_repair(gh, BODY, allowed(), Counter())
    assert all(method == "GET" for method, _, _ in rec.requests)


@pytest.mark.parametrize(
    "field,value",
    [
        ("org_id", "other"),
        ("kind", "gate"),
        ("repository_identity", "other/repo"),
        ("repository_identity", None),
        ("provider_repository_id", 555),
        ("issue_ref", "5224"),
        ("issue_ref", "05223"),
        ("issue_ref", None),
    ],
)
async def test_server_identity_must_match(provider_data, story, field, value):
    session, node = story
    setattr(node, field, value)
    await session.commit()
    rec, _, _, files = provider_data
    install_board(rec, files)
    async with provider(rec) as gh:
        with pytest.raises(OperationRefusedError, match="matching server story"):
            await merge_repair(gh, BODY, allowed(), Counter())
    assert all(method == "GET" for method, _, _ in rec.requests)


@pytest.mark.parametrize("field,value", [("branch", "agent/issue-999"), ("node_id", "absent"), ("repository", "other/repo")])
async def test_protected_assignment_cannot_be_substituted(provider_data, story, field, value):
    rec, _, _, files = provider_data
    install_board(rec, files)
    auth = allowed()
    auth.assignment = _assignment(**{field: value})
    async with provider(rec) as gh:
        with pytest.raises(OperationRefusedError, match="assignment differs"):
            await merge_repair(gh, BODY, auth, Counter())
    assert all(method == "GET" for method, _, _ in rec.requests)


@pytest.mark.parametrize("mutation", ["rename", "removed", "copied", "other_issue", "other_adp", "case", "traversal", "second_board"])
async def test_no_other_protected_diff_is_exempt(provider_data, story, mutation):
    rec, _, _, files = provider_data
    entry, _, _, _ = install_board(rec, files)
    if mutation == "rename":
        entry.update(status="renamed", previous_filename="src/service.py")
    elif mutation in {"removed", "copied"}:
        entry["status"] = mutation
    elif mutation == "second_board":
        files.append(dict(entry))
    else:
        entry["filename"] = {
            "other_issue": ".adp/tasks/999.json",
            "other_adp": ".adp/policy.json",
            "case": ".ADP/tasks/5223.json",
            "traversal": "src/../.adp/tasks/5223.json",
        }[mutation]
    async with provider(rec) as gh:
        with pytest.raises(OperationRefusedError):
            await merge_repair(gh, BODY, allowed(), Counter())
    assert all(method == "GET" for method, _, _ in rec.requests)


@pytest.mark.parametrize(
    "mutation",
    [
        "commit",
        "tree",
        "truncated",
        "missing",
        "duplicate",
        "symlink",
        "executable",
        "submodule",
        "parent_symlink",
        "diff_sha",
        "blob_sha",
        "integrity",
        "size",
        "oversize",
        "encoding",
        "base64",
    ],
)
async def test_independent_provider_object_verification(provider_data, mutation):
    rec, _, _, files = provider_data
    entry, commit, trees, blob = install_board(rec, files)
    if mutation == "commit":
        commit["sha"] = "e" * 40
    elif mutation == "tree":
        trees[1]["sha"] = "e" * 40
    elif mutation == "truncated":
        trees[1]["truncated"] = True
    elif mutation == "missing":
        trees[2]["tree"].clear()
    elif mutation == "duplicate":
        trees[2]["tree"].append(dict(trees[2]["tree"][0]))
    elif mutation in {"symlink", "executable", "submodule"}:
        trees[2]["tree"][0]["mode"] = {"symlink": "120000", "executable": "100755", "submodule": "160000"}[mutation]
    elif mutation == "parent_symlink":
        trees[0]["tree"][0].update(type="blob", mode="120000")
    elif mutation == "diff_sha":
        entry["sha"] = "e" * 40
    elif mutation == "blob_sha":
        blob["sha"] = "e" * 40
    elif mutation == "integrity":
        blob["content"] = base64.b64encode(b"x" * blob["size"]).decode()
    elif mutation == "size":
        blob["size"] += 1
    elif mutation == "oversize":
        blob["size"] = MAX_BOARD_BYTES + 1
    elif mutation == "encoding":
        blob["encoding"] = "utf-8"
    elif mutation == "base64":
        blob["content"] = "not base64!"
    async with provider(rec) as gh:
        with pytest.raises(OperationRefusedError):
            await verify_progress_board(gh, entry, head=HEAD, issue=5223)
    assert all(method == "GET" for method, _, _ in rec.requests)


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "boolean_version",
        "issue",
        "boolean_issue",
        "empty",
        "too_many",
        "duplicate_id",
        "status",
        "kind",
        "title",
        "covers",
        "files",
        "note",
        "attempt",
        "parent",
        "ambiguous_parent",
        "utf8",
        "duplicate_key",
        "nan",
        "json",
        "deep",
    ],
)
async def test_invalid_board_schema_cannot_be_classified(provider_data, mutation):
    rec, _, _, files = provider_data
    value = board()
    raw = None
    if mutation in {"version", "boolean_version", "issue", "boolean_issue"}:
        field = mutation.removeprefix("boolean_")
        value[field] = True if mutation.startswith("boolean_") else 999
    elif mutation == "empty":
        value["tasks"] = []
    elif mutation == "too_many":
        value["tasks"] = [{"id": f"t{i}", "kind": "code", "status": "done", "title": "task"} for i in range(1001)]
    elif mutation == "duplicate_id":
        value["tasks"][1]["id"] = "source"
    elif mutation in {"status", "kind", "title", "covers", "files", "note", "attempt", "parent"}:
        field = "planStep" if mutation == "parent" else mutation
        value["tasks"][0][field] = {
            "status": "accepted",
            "kind": "approval",
            "title": "bad\ntitle",
            "covers": ["absent"],
            "files": "src/a.py",
            "note": {},
            "attempt": True,
            "parent": {"id": "step"},
        }[mutation]
    elif mutation == "ambiguous_parent":
        for i, task in enumerate(value["tasks"]):
            task["planStep"] = {"id": "step", "title": str(i)}
    else:
        raw = {
            "utf8": b"\xff",
            "duplicate_key": b'{"version":1,"version":2}',
            "nan": b'{"x":NaN}',
            "json": b"broken",
            "deep": b"[" * 2000 + b"]" * 2000,
        }[mutation]
    entry, _, _, _ = install_board(rec, files, value, raw)
    async with provider(rec) as gh:
        with pytest.raises(OperationRefusedError, match="content is invalid"):
            await verify_progress_board(gh, entry, head=HEAD, issue=5223)


@pytest.mark.parametrize("state", ["ready", "behind"])
@pytest.mark.parametrize("mutation", ["mode", "status", "head", "issue"])
async def test_revalidation_does_not_reuse_board_classification(provider_data, story, state, mutation):
    rec, pull, _, files = provider_data
    entry, _, trees, _ = install_board(rec, files)
    if state == "behind":
        advanced_main(provider_data)

    async def changed():
        if mutation == "mode":
            trees[2]["tree"][0]["mode"] = "120000"
        elif mutation == "status":
            entry["status"] = "removed"
        elif mutation == "head":
            rec.branch_head = pull["head"]["sha"] = "e" * 40
        else:
            session, node = story
            node.issue_ref = "999"
            await session.commit()

    async with provider(rec) as gh:
        with pytest.raises((OperationRefusedError, ProviderConflictError)):
            await merge_repair(gh, BODY, allowed(), changed)
    assert all(method == "GET" for method, _, _ in rec.requests)


@pytest.mark.parametrize("path", ["src/unapproved.py", "src/credentials.py", ".github/workflows/unlisted.yml", ".adp/accepted.json"])
async def test_board_cannot_hide_other_unauthorized_paths(provider_data, story, path):
    rec, _, _, files = provider_data
    files[0]["filename"] = path
    install_board(rec, files)
    async with provider(rec) as gh:
        with pytest.raises(OperationRefusedError):
            await merge_repair(gh, BODY, allowed(), Counter())
    assert all(method == "GET" for method, _, _ in rec.requests)


async def test_done_tasks_do_not_replace_failing_checks(provider_data, story):
    rec, _, checks, files = provider_data
    value = board()
    for task in value["tasks"]:
        task["status"] = "done"
    install_board(rec, files, value)
    checks["check_runs"][0]["conclusion"] = "failure"
    async with provider(rec) as gh:
        result = await merge_repair(gh, BODY, allowed(), Counter())
    assert result["merged"] is False
    assert {item["code"] for item in result["blockers"]} >= {"check_failed"}
    assert all(method == "GET" for method, _, _ in rec.requests)


@pytest.mark.parametrize("change", ["command", "permissions"])
async def test_inherited_board_preserves_allowlisted_workflow_authority(provider_data, story, change):
    rec, _, _, files = provider_data
    files[0]["filename"] = ".github/workflows/deploy.yml"
    install_board(rec, files)
    original = {
        "on": {"workflow_dispatch": {}},
        "permissions": {"contents": "read"},
        "jobs": {"deploy": {"runs-on": "self-hosted", "steps": [{"run": "echo original"}]}},
    }
    proposed = copy.deepcopy(original)
    if change == "command":
        proposed["jobs"]["deploy"]["steps"][0]["run"] = "echo fixed"
    else:
        proposed["permissions"] = "write-all"
    contents = [yaml.safe_dump(value).encode() for value in (original, proposed)] * 2

    def workflow():
        raw = contents.pop(0)
        return httpx.Response(200, json={"type": "file", "encoding": "base64", "size": len(raw), "content": base64.b64encode(raw).decode()})

    rec.overrides["GET /repos/acme/widgets/contents/.github/workflows/deploy.yml"] = workflow
    async with provider(rec) as gh:
        if change == "command":
            assert (await merge_repair(gh, BODY, allowed(), Counter()))["merged"] is True
        else:
            with pytest.raises(OperationRefusedError, match="workflow authority changed"):
                await merge_repair(gh, BODY, allowed(), Counter())
            assert all(method == "GET" for method, _, _ in rec.requests)


@pytest.mark.parametrize("operation", ["validate_repair_edit", "publish_commit"])
async def test_valid_board_edit_and_publication_remain_forbidden(monkeypatch, operation):
    from src.agentauth.github_operation_routes import OperationRequest, _perform

    auth = allowed()
    auth.policy.repair_scope = "repository"
    rec = Recorder()
    token = AsyncMock(return_value="test-token")
    monkeypatch.setattr("src.agentauth.github_operation_routes.installation_token", token)
    monkeypatch.setattr("src.agentauth.github_operation_routes.GitHubProvider", lambda **kwargs: provider(rec))
    monkeypatch.setattr("src.agentauth.operations_repair.ensure_repair_readiness", AsyncMock())
    request = OperationRequest(
        operation=operation,
        expected_head=HEAD,
        message="Save progress",
        changes=[
            {
                "path": BOARD_PATH,
                "content_base64": base64.b64encode(json.dumps(board()).encode()).decode(),
            }
        ],
    )
    with pytest.raises(OperationRefusedError, match="approval or credential policy"):
        await _perform(request, auth, AsyncMock(return_value=auth), now=datetime.now(UTC))
    assert rec.requests == []
    if operation == "validate_repair_edit":
        token.assert_not_called()
