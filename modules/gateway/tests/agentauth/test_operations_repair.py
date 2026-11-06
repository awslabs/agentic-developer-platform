import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from src.agentauth.github_operations import OperationRefusedError, RepairScopeMissingError
from src.agentauth.github_provider import GitHubProvider, ProviderConflictError, ProviderUnavailableError
from src.agentauth.operations_repair import merge_repair, repair_status
from tests.agentauth.test_github_provider import BRANCH, DEFAULT_HEAD, HEAD, REPOSITORY_ID, Counter, Recorder, _assignment


@pytest.fixture
def provider_data():
    pull = {
        "number": 7,
        "state": "open",
        "draft": False,
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": HEAD, "ref": BRANCH, "repo": {"id": REPOSITORY_ID}},
        "base": {"sha": DEFAULT_HEAD, "ref": "main", "repo": {"id": REPOSITORY_ID}},
    }
    checks = {"total_count": 1, "check_runs": [{"name": "tests", "head_sha": HEAD, "status": "completed", "conclusion": "success"}]}
    files = [{"filename": "src/service.py"}]
    rec = Recorder(
        overrides={
            "GET /repos/acme/widgets/pulls/7/files": lambda: httpx.Response(200, json=files),
            "GET /repos/acme/widgets/pulls/7": lambda: httpx.Response(200, json=pull),
            f"GET /repos/acme/widgets/commits/{HEAD}/check-runs": lambda: httpx.Response(200, json=checks),
            f"GET /repos/acme/widgets/commits/{HEAD}/status": httpx.Response(200, json={"total_count": 0, "state": "pending"}),
            "GET /repos/acme/widgets/git/ref/heads/main": lambda: httpx.Response(
                200, json={"object": {"sha": "d" * 40 if pull["merged"] else DEFAULT_HEAD}}
            ),
            "PUT /repos/acme/widgets/pulls/7/merge": httpx.Response(200, json={"merged": True, "sha": "d" * 40}),
            "GET /repos/acme/widgets/commits/" + "d" * 40: httpx.Response(
                200, json={"sha": "d" * 40, "parents": [{"sha": DEFAULT_HEAD}, {"sha": HEAD}]}
            ),
        }
    )
    return rec, pull, checks, files


def authorization():
    return SimpleNamespace(
        operations_repair=True,
        policy=SimpleNamespace(
            permits=lambda action: True,
            repair_paths=["src/service.py", "src/deploy.py", ".github/workflows/deploy.yml"],
            repair_required_checks=["tests"],
        ),
        workflow_allowlist={"deploy.yml"},
    )


def test_repository_scope_allows_unlisted_implementation_files_but_not_implicit_authority():
    from src.agentauth.operations_repair import check_paths

    allowed = authorization()
    allowed.policy.repair_paths = []
    with pytest.raises(RepairScopeMissingError, match="not accepted"):
        check_paths(["src/new_runtime.py"], allowed)
    allowed.policy.repair_scope = "repository"
    check_paths(["src/new_runtime.py", "tests/new_runtime_test.py", ".github/workflows/deploy.yml"], allowed)
    allowed.policy.repair_scope = None
    allowed.policy.repair_paths = ["src/service.py"]
    with pytest.raises(OperationRefusedError, match="outside"):
        check_paths(["src/new_runtime.py"], allowed)


def test_empty_change_set_with_accepted_scope_does_not_claim_scope_is_missing():
    from src.agentauth.operations_repair import check_paths

    with pytest.raises(OperationRefusedError) as failure:
        check_paths([], authorization())
    assert type(failure.value) is OperationRefusedError


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/outside.py",
        "src/../outside.py",
        "src\\outside.py",
        "src//outside.py",
        ".git/config",
        ".adp/accepted.json",
        ".github/actions/custom/action.yml",
        ".github/workflows/other.yml",
        "src/credentials.py",
        "src/approval.py",
        "src/policy.py",
        "src/token.py",
        "src/file\n.py",
        "x" * 1025,
    ],
)
def test_repository_repair_scope_preserves_sensitive_paths_and_workflow_boundary(path):
    from src.agentauth.operations_repair import check_paths

    allowed = authorization()
    allowed.policy.repair_paths = []
    allowed.policy.repair_scope = "repository"
    with pytest.raises(OperationRefusedError):
        check_paths([path], allowed)


@asynccontextmanager
async def provider(rec):
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(rec.handler)) as client:
        async with GitHubProvider(token="not-a-real-token", assignment=_assignment(), client=client) as gh:
            yield gh


async def test_operations_merges_after_checks_without_reviewer(provider_data):
    rec, _, _, _ = provider_data
    async with provider(rec) as gh:
        result = await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
    assert result == {"merged": True, "sha": "d" * 40}
    assert [body for method, path, body in rec.requests if method == "PUT"] == [{"sha": HEAD, "merge_method": "merge"}]


def advanced_main(provider_data):
    rec, pull, checks, files = provider_data
    main = "f" * 40
    pull["mergeable_state"] = "behind"
    rec.overrides["GET /repos/acme/widgets/git/ref/heads/main"] = httpx.Response(200, json={"object": {"sha": main}})
    rec.overrides[f"GET /repos/acme/widgets/compare/{DEFAULT_HEAD}...{main}"] = httpx.Response(
        200, json={"status": "ahead", "merge_base_commit": {"sha": DEFAULT_HEAD}}
    )
    return rec, pull, checks, files, main


async def test_clean_checked_repair_merges_when_main_advanced_without_restarting_ci(provider_data):
    rec, pull, _, _, _ = advanced_main(provider_data)
    pull["mergeable_state"] = "clean"
    async with provider(rec) as gh:
        observed = await repair_status(gh, 7, expected_head=HEAD, required_checks=["tests"])
        assert observed["base"] != observed["base_head"]
        assert observed["ready"] is True and observed["checks_verified"] is True
        assert observed["blockers"] == [] and "next_action" not in observed
        result = await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
    assert result == {"merged": True, "sha": "d" * 40}
    assert [(path, body) for method, path, body in rec.requests if method == "PUT"] == [
        ("/repos/acme/widgets/pulls/7/merge", {"sha": HEAD, "merge_method": "merge"})
    ]


async def test_clean_repair_rechecks_current_main_before_merging(provider_data):
    rec, pull, _, _, _ = advanced_main(provider_data)
    pull["mergeable_state"] = "clean"

    async def moved():
        rec.overrides["GET /repos/acme/widgets/git/ref/heads/main"] = httpx.Response(200, json={"object": {"sha": "a" * 40}})

    async with provider(rec) as gh:
        with pytest.raises(ProviderConflictError, match="before merge"):
            await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), moved)
    assert not any(method == "PUT" for method, _, _ in rec.requests)


@pytest.mark.parametrize("current_base", [False, True])
async def test_agent_refreshes_own_branch_then_requires_new_head_checks(provider_data, current_base):
    rec, pull, checks, _, main = advanced_main(provider_data)
    if current_base:
        pull["base"]["sha"] = main
    refreshed_head = "e" * 40

    def updated():
        rec.branch_head = pull["head"]["sha"] = refreshed_head
        pull["base"]["sha"] = main
        pull["mergeable_state"] = "clean"
        checks["check_runs"] = [{"name": "tests", "head_sha": refreshed_head, "status": "queued", "conclusion": None}]
        return httpx.Response(202, json={"message": "Updating pull request branch"})

    rec.overrides["PUT /repos/acme/widgets/pulls/7/update-branch"] = updated
    rec.overrides[f"GET /repos/acme/widgets/commits/{refreshed_head}/check-runs"] = lambda: httpx.Response(200, json=checks)
    rec.overrides[f"GET /repos/acme/widgets/commits/{refreshed_head}/status"] = httpx.Response(200, json={"total_count": 0, "state": "pending"})
    async with provider(rec) as gh:
        observed = await repair_status(gh, 7, expected_head=HEAD, required_checks=["tests"])
        assert observed["ready"] is False
        assert "Call repair_merge" in observed["next_action"]
        assert "Waiting alone will not refresh" in observed["next_action"]
        assert not any(method == "PUT" for method, _, _ in rec.requests)
        result = await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
        assert result["merged"] is False and result["reason"] == "repair_branch_update_requested"
        assert [body for method, path, body in rec.requests if method == "PUT"] == [{"expected_head_sha": HEAD}]
        with pytest.raises(ProviderConflictError):
            await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
        pending = await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=refreshed_head), authorization(), Counter())
        assert pending["merged"] is False and {item["code"] for item in pending["blockers"]} == {"check_pending"}
        assert "next_action" not in pending
        assert not any(path.endswith("/merge") for method, path, _ in rec.requests if method == "PUT")
        checks["check_runs"][0].update(status="completed", conclusion="success")
        merged = await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=refreshed_head), authorization(), Counter())
        assert merged["merged"] is True
    assert [body for method, path, body in rec.requests if method == "PUT"] == [
        {"expected_head_sha": HEAD},
        {"sha": refreshed_head, "merge_method": "merge"},
    ]


@pytest.mark.parametrize("invalid", ["scope", "ancestry", "checks", "changed_head", "revoked", "repair_not_allowed"])
async def test_branch_refresh_preserves_repair_authority(provider_data, invalid):
    rec, pull, checks, files, main = advanced_main(provider_data)
    auth = authorization()
    if invalid == "scope":
        files.append({"filename": "secrets/credentials.txt"})
    elif invalid == "ancestry":
        rec.overrides[f"GET /repos/acme/widgets/compare/{DEFAULT_HEAD}...{main}"] = httpx.Response(200, json={"status": "diverged"})
    elif invalid == "checks":
        checks["check_runs"][0]["conclusion"] = "failure"
    elif invalid == "changed_head":
        rec.branch_head = "e" * 40
    elif invalid == "repair_not_allowed":
        auth.policy.permits = lambda action: action.value != "repair"

    async with provider(rec) as gh:
        if invalid in {"scope", "revoked"}:
            with pytest.raises((OperationRefusedError, ProviderConflictError)):
                await merge_repair(
                    gh, SimpleNamespace(pull_number=7, expected_head=HEAD), auth, Counter(fail_after=0 if invalid == "revoked" else None)
                )
        else:
            assert (await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), auth, Counter()))["merged"] is False
    assert not any(method == "PUT" for method, _, _ in rec.requests)


async def test_changed_default_branch_during_refresh_never_updates(provider_data):
    rec, _, _, _, _ = advanced_main(provider_data)

    async def moved():
        rec.overrides["GET /repos/acme/widgets/git/ref/heads/main"] = httpx.Response(200, json={"object": {"sha": "a" * 40}})

    async with provider(rec) as gh:
        with pytest.raises(ProviderConflictError, match="before refresh"):
            await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), moved)
    assert not any(method == "PUT" for method, _, _ in rec.requests)


async def test_pending_legacy_status_names_actual_dependency_and_still_blocks_merge(provider_data):
    rec, _, _, _ = provider_data
    rec.overrides[f"GET /repos/acme/widgets/commits/{HEAD}/status"] = httpx.Response(
        200,
        json={
            "total_count": 1,
            "state": "pending",
            "statuses": [{"context": "Product rollback baseline compatibility", "state": "pending"}],
        },
    )
    async with provider(rec) as gh:
        status = await repair_status(gh, 7, expected_head=HEAD, required_checks=["tests"])
        result = await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
    assert status["status_contexts"] == [{"context": "Product rollback baseline compatibility", "state": "pending"}]
    assert not status["checks_verified"] and not status["ready"]
    assert status["blockers"] == [{"code": "commit_status_pending"}]
    assert status["mergeable_state"] == "clean"
    assert result["merged"] is False
    assert not any(method == "PUT" for method, _, _ in rec.requests)


async def test_agent_can_distinguish_failed_and_missing_required_checks(provider_data):
    rec, _, checks, _ = provider_data
    checks["check_runs"][0]["conclusion"] = "failure"
    async with provider(rec) as gh:
        status = await repair_status(gh, 7, required_checks=["tests", "integration"])
    assert status["blockers"] == [
        {"code": "required_check_missing", "name": "integration"},
        {"code": "check_failed", "name": "tests"},
    ]
    assert not status["ready"]


@pytest.mark.parametrize(
    "failure", ["failure", "cancelled", "in_progress", "wrong_head", "no_checks", "required_missing", "protected", "branch_moved"]
)
async def test_not_ready_never_merges(provider_data, failure):
    rec, pull, checks, _ = provider_data
    if failure in {"failure", "cancelled"}:
        checks["check_runs"][0]["conclusion"] = failure
    elif failure == "in_progress":
        checks["check_runs"][0]["status"] = failure
    elif failure == "wrong_head":
        checks["check_runs"][0]["head_sha"] = "e" * 40
    elif failure == "no_checks":
        checks.update(total_count=0, check_runs=[])
    elif failure == "required_missing":
        checks["check_runs"][0]["name"] = "unrelated-green-check"
    elif failure == "protected":
        pull["mergeable_state"] = "blocked"
    elif failure == "branch_moved":
        rec.branch_head = "e" * 40
    async with provider(rec) as gh:
        result = await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
    assert result["merged"] is False
    assert not any(method == "PUT" for method, _, _ in rec.requests)


@pytest.mark.parametrize("change", ["base", "checks", "claim"])
async def test_pre_merge_recheck_refuses_changed_base_checks_or_claim(provider_data, change):
    rec, pull, checks, _ = provider_data

    async def reauthorize():
        if change == "base":
            pull["base"]["sha"] = "e" * 40
        elif change == "checks":
            checks["check_runs"][0]["conclusion"] = "failure"
        else:
            raise OperationRefusedError("claim expired")

    async with provider(rec) as gh:
        with pytest.raises((OperationRefusedError, ProviderConflictError)):
            await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), reauthorize)
    assert not any(method == "PUT" for method, _, _ in rec.requests)


@pytest.mark.parametrize(
    "updated",
    [
        "name: deploy\non: [push, pull_request_target]\npermissions: {contents: read}\n"
        "jobs: {deploy: {runs-on: self-hosted, steps: [{run: 'echo fixed'}]}}\n",
        "name: deploy\non: push\npermissions: write-all\njobs: {deploy: {runs-on: self-hosted, steps: [{run: 'echo fixed'}]}}\n",
        "name: deploy\non: push\npermissions: {contents: read}\n"
        "jobs: {deploy: {runs-on: self-hosted, steps: [{run: 'echo ${{ secrets.TOKEN }}'}]}}\n",
    ],
)
async def test_workflow_repair_refuses_expanded_authority(provider_data, updated):
    rec, _, _, files = provider_data
    files[:] = [{"filename": ".github/workflows/deploy.yml"}]
    original = "name: deploy\non: push\npermissions: {contents: read}\njobs: {deploy: {runs-on: self-hosted, steps: [{run: 'echo original'}]}}\n"
    responses = iter([original, updated])

    def workflow():
        content = next(responses)
        return httpx.Response(
            200,
            json={"type": "file", "encoding": "base64", "size": len(content), "content": base64.b64encode(content.encode()).decode()},
        )

    rec.overrides["GET /repos/acme/widgets/contents/.github/workflows/deploy.yml"] = workflow
    async with provider(rec) as gh:
        with pytest.raises(OperationRefusedError, match="workflow"):
            await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
    assert not any(method == "PUT" for method, _, _ in rec.requests)


@pytest.mark.parametrize("main_advanced", [False, True])
async def test_scoped_workflow_repair_can_change_step_without_expanding_authority(provider_data, main_advanced):
    rec, _, _, files = provider_data
    current_main = DEFAULT_HEAD
    if main_advanced:
        rec, pull, _, files, current_main = advanced_main(provider_data)
        pull["mergeable_state"] = "clean"
    files[:] = [{"filename": ".github/workflows/deploy.yml"}]
    original = "name: deploy\non: push\npermissions: {contents: read}\njobs: {deploy: {runs-on: self-hosted, steps: [{run: 'echo original'}]}}\n"
    updated = original.replace("echo original", "echo fixed")
    responses = iter([original, updated, original, updated])

    def workflow():
        content = next(responses)
        encoded = base64.b64encode(content.encode()).decode()
        return httpx.Response(200, json={"type": "file", "encoding": "base64", "size": len(content), "content": encoded[:12] + "\n" + encoded[12:]})

    rec.overrides["GET /repos/acme/widgets/contents/.github/workflows/deploy.yml"] = workflow
    async with provider(rec) as gh:
        result = await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
    assert result == {"merged": True, "sha": "d" * 40}
    assert len([method for method, _, _ in rec.requests if method == "PUT"]) == 1
    assert [path for method, path, _ in rec.requests if method == "GET" and "/contents/" in path] == [
        f"/repos/acme/widgets/contents/.github/workflows/deploy.yml?ref={revision}" for revision in (current_main, HEAD, current_main, HEAD)
    ]


@pytest.mark.parametrize("expression", ["secrets.TOKEN", "secrets['TOKEN']", "secrets [ 'TOKEN' ]", "secrets . TOKEN", "SECRETS['TOKEN']", "secrets"])
@pytest.mark.parametrize("remove", [False, True])
async def test_merge_refuses_credential_context_in_any_spelling(provider_data, expression, remove):
    recorder, _, _, files = provider_data
    files[:] = [{"filename": ".github/workflows/deploy.yml"}]
    original = "on: push\njobs:\n  deploy:\n    runs-on: self-hosted\n    steps:\n      - run: echo original\n"
    updated = original.replace("echo original", "echo ${{ " + expression + " }}")
    if remove:
        original, updated = updated, original
    documents = iter([original, updated, original, updated])

    def workflow():
        content = next(documents).encode()
        return httpx.Response(200, json={"type": "file", "encoding": "base64", "size": len(content), "content": base64.b64encode(content).decode()})

    recorder.overrides["GET /repos/acme/widgets/contents/.github/workflows/deploy.yml"] = workflow
    async with provider(recorder) as github:
        with pytest.raises(OperationRefusedError, match="credential authority"):
            await merge_repair(github, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
    assert all(method == "GET" for method, _, _ in recorder.requests)


@pytest.mark.parametrize("operation", ["validate_repair_edit", "publish_commit"])
@pytest.mark.parametrize(
    "change",
    ["trigger", "permissions", "secret", "bracket_secret", "condition", "remove_condition", "failure", "shell", "delete", "symlink", "safe"],
)
@pytest.mark.parametrize("padding", [0, 40000])
async def test_workflow_authority_is_checked_before_publication(monkeypatch, operation, change, padding):
    from src.agentauth.github_operation_routes import OperationRequest, _perform

    original = {
        "on": {"push": {"branches": ["main"]}},
        "permissions": {"contents": "read"},
        "jobs": {"deploy": {"runs-on": "self-hosted", "steps": [{"run": "echo original"}]}},
    }
    if change == "remove_condition":
        original["jobs"]["deploy"]["steps"][0]["if"] = "${{ inputs.release_permit != '' }}"
    baseline = yaml.safe_dump(original).encode()
    if change == "trigger":
        original["on"] = "push"
    elif change == "permissions":
        original["permissions"] = "write-all"
    elif change in {"secret", "bracket_secret"}:
        expression = "secrets.NEW_CREDENTIAL" if change == "secret" else "secrets [ 'NEW_CREDENTIAL' ]"
        original["jobs"]["deploy"]["steps"][0]["run"] = "echo ${{ " + expression + " }}"
    elif change == "condition":
        original["jobs"]["deploy"]["steps"][0]["if"] = "${{ always() }}"
    elif change == "remove_condition":
        del original["jobs"]["deploy"]["steps"][0]["if"]
    elif change == "failure":
        original["jobs"]["deploy"]["steps"][0]["continue-on-error"] = True
    elif change == "shell":
        original["jobs"]["deploy"]["steps"][0]["shell"] = "bash {0}"
    else:
        original["jobs"]["deploy"]["steps"][0]["run"] = "echo fixed"
    proposed = yaml.safe_dump(original).encode() + (b"#" + b"x" * padding + b"\n" if padding else b"")
    recorder = Recorder(
        overrides={
            "GET /repos/acme/widgets/contents/.github/workflows/deploy.yml": httpx.Response(
                200, json={"type": "file", "encoding": "base64", "size": len(baseline), "content": base64.b64encode(baseline).decode()}
            )
        }
    )
    token = AsyncMock(return_value="test-token")
    monkeypatch.setattr("src.agentauth.github_operation_routes.installation_token", token)
    monkeypatch.setattr("src.agentauth.github_operation_routes.GitHubProvider", lambda **kwargs: provider(recorder))
    monkeypatch.setattr("src.agentauth.operations_repair.ensure_repair_readiness", AsyncMock())
    authorized = authorization()
    authorized.assignment = _assignment()
    request = OperationRequest(
        operation=operation,
        expected_head=HEAD,
        message="Repair deployment",
        changes=[
            {
                "path": ".github/workflows/deploy.yml",
                "content_base64": None if change == "delete" else base64.b64encode(proposed).decode(),
                "deleted": change == "delete",
                "mode": "120000" if change == "symlink" else "100644",
            }
        ],
    )
    current = AsyncMock(return_value=authorized)
    if change == "safe":
        result = await _perform(request, authorized, current, now=datetime.now(UTC))
        assert result.get("commit_sha") if operation == "publish_commit" else result.get("sha256")
        if operation == "publish_commit":
            assert any(method == "PATCH" and "/git/refs/heads/" in path for method, path, _ in recorder.requests)
        else:
            assert token.await_args.kwargs["permissions"]["contents"] == "read"
            assert set(token.await_args.kwargs["permissions"].values()) == {"read"}
    else:
        with pytest.raises(OperationRefusedError):
            await _perform(request, authorized, current, now=datetime.now(UTC))
    if change != "safe" or operation == "validate_repair_edit":
        assert all(method == "GET" for method, _, _ in recorder.requests)
    if change not in {"delete", "symlink"}:
        assert any(path.endswith(f"deploy.yml?ref={DEFAULT_HEAD}") for _, path, _ in recorder.requests)


async def test_changed_head_and_unlisted_workflow_refused(provider_data):
    rec, _, _, files = provider_data
    async with provider(rec) as gh:
        with pytest.raises(ProviderConflictError):
            await repair_status(gh, 7, expected_head="e" * 40)
        files[:] = [{"filename": ".github/workflows/unrelated.yml"}]
        with pytest.raises(OperationRefusedError):
            await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
    assert not any(method == "PUT" for method, _, _ in rec.requests)


async def test_response_loss_reconciles_merged_pr_without_second_merge(provider_data):
    rec, pull, _, _ = provider_data
    pull.update(merged=True, merge_commit_sha="d" * 40, state="closed")
    pull["base"]["sha"] = "d" * 40
    async with provider(rec) as gh:
        result = await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
    assert result == {"merged": True, "sha": "d" * 40}
    assert not any(method == "PUT" for method, _, _ in rec.requests)


async def test_lost_merge_response_reconciles_exact_tested_pr(provider_data):
    rec, pull, _, _ = provider_data

    def response_lost():
        pull.update(merged=True, merge_commit_sha="d" * 40, state="closed")
        pull["base"]["sha"] = "d" * 40
        raise httpx.ReadTimeout("response lost")

    rec.overrides["PUT /repos/acme/widgets/pulls/7/merge"] = response_lost
    async with provider(rec) as gh:
        with pytest.raises(ProviderUnavailableError):
            await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
        assert await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter()) == {
            "merged": True,
            "sha": "d" * 40,
        }
    assert len([method for method, _, _ in rec.requests if method == "PUT"]) == 1


@pytest.mark.parametrize("contains_merge", [True, False])
async def test_reconciled_merge_checks_current_default_branch_ancestry(provider_data, contains_merge):
    rec, pull, _, _ = provider_data
    pull.update(merged=True, merge_commit_sha="d" * 40, state="closed")
    rec.overrides["GET /repos/acme/widgets/git/ref/heads/main"] = httpx.Response(200, json={"object": {"sha": "e" * 40}})
    rec.overrides["GET /repos/acme/widgets/compare/" + "d" * 40] = httpx.Response(
        200,
        json={"status": "ahead" if contains_merge else "diverged", "merge_base_commit": {"sha": "d" * 40}},
    )
    async with provider(rec) as gh:
        if contains_merge:
            assert (await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter()))["sha"] == "d" * 40
        else:
            with pytest.raises(OperationRefusedError, match="current base"):
                await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
    assert not any(method == "PUT" for method, _, _ in rec.requests)


@pytest.mark.parametrize("failure", ["head", "checks", "parent"])
async def test_reconciled_merge_refuses_changed_identity_or_failed_checks(provider_data, failure):
    rec, pull, checks, _ = provider_data
    pull.update(merged=True, merge_commit_sha="d" * 40, state="closed")
    if failure == "head":
        pull["head"]["sha"] = "e" * 40
    elif failure == "checks":
        checks["check_runs"][0]["conclusion"] = "failure"
    else:
        rec.overrides["GET /repos/acme/widgets/commits/" + "d" * 40] = httpx.Response(
            200, json={"sha": "d" * 40, "parents": [{"sha": DEFAULT_HEAD}, {"sha": "e" * 40}]}
        )
    async with provider(rec) as gh:
        with pytest.raises((OperationRefusedError, ProviderConflictError)):
            await merge_repair(gh, SimpleNamespace(pull_number=7, expected_head=HEAD), authorization(), Counter())
    assert not any(method == "PUT" for method, _, _ in rec.requests)


def test_workflow_repairs_stay_in_operations_allowlist_without_global_deploy():
    from src.agentauth.operations_repair import check_paths
    from src.orchestration.execution_policy import Action

    allowed = authorization()
    allowed.policy.permits = lambda action: action in {Action.OPERATE, Action.REPAIR, Action.MERGE}
    check_paths([".github/workflows/deploy.yml", "src/deploy.py"], allowed)
    with pytest.raises(OperationRefusedError):
        check_paths([".github/workflows/other.yml"], allowed)
    with pytest.raises(OperationRefusedError):
        check_paths([".github/actions/new-action/action.yml"], allowed)
    allowed.policy.permits = lambda action: action is Action.OPERATE
    with pytest.raises(OperationRefusedError):
        check_paths([".github/workflows/deploy.yml"], allowed)


def test_identity_proof_candidate_paths_require_exact_accepted_repair_scope():
    import json
    from pathlib import Path

    from src.agentauth.operations_repair import check_paths
    from src.orchestration.execution_policy import Action

    root = Path(__file__).resolve().parents[4]
    candidate = json.loads((root / "docs/tools/atlassian-mcp/repair-scope-8446.json").read_text())
    paths = candidate["repair_paths"]
    allowed = authorization()
    allowed.policy.repair_paths = paths
    allowed.workflow_allowlist = set(candidate["workflow_allowlist"])
    allowed.policy.permits = lambda action: action in {Action.OPERATE, Action.REPAIR, Action.MERGE}
    check_paths(paths, allowed)
    for refused in (
        "tests/e2e/tools/",
        "tests/e2e/tools/unreviewed.py",
        ".github/workflows/tools-ci.yml",
        ".adp/tasks/8446.json",
    ):
        with pytest.raises(OperationRefusedError):
            check_paths([refused], allowed)
    allowed.policy.permits = lambda action: action in {Action.OPERATE, Action.REPAIR}
    with pytest.raises(OperationRefusedError):
        check_paths([".github/workflows/gateway-deploy.yml"], allowed)


@pytest.mark.parametrize(
    "path",
    [
        "src/unapproved.py",
        ".github/workflows/other.yml",
        ".github/actions/deploy/action.yml",
        "src/../src/deploy.py",
        "src\\deploy.py",
        ".adp/policy.json",
        "src/credential_policy.py",
    ],
)
def test_repair_edit_refuses_paths_outside_scope_and_authority_files(path):
    from src.agentauth.operations_repair import check_paths

    allowed = authorization()
    if path != "src/unapproved.py":
        allowed.policy.repair_paths.append(path)
    with pytest.raises(OperationRefusedError):
        check_paths([path], allowed)


@pytest.mark.parametrize("size", [14, 65536, 256 * 1024, 256 * 1024 + 1])
async def test_edit_validation_rechecks_current_operations_authority_without_a_provider_token(monkeypatch, size):
    import base64
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from src.agentauth.github_operation_routes import OperationRequest, _perform

    async def no_token(**_kwargs):
        raise AssertionError("validation must not mint a GitHub token")

    monkeypatch.setattr("src.agentauth.github_operation_routes.installation_token", no_token)
    monkeypatch.setattr("src.agentauth.operations_repair.ensure_repair_readiness", AsyncMock())
    authorized = authorization()
    authorized.assignment = SimpleNamespace(repository="acme/widgets", branch="agent/issue-1")
    content = b"#" + b"x" * (size - 2) + b"\n"
    request = OperationRequest(
        operation="validate_repair_edit",
        repository="acme/widgets",
        changes=[{"path": "src/deploy.py", "content_base64": base64.b64encode(content).decode()}],
    )
    current = AsyncMock(return_value=authorized)
    if size > 256 * 1024:
        with pytest.raises(OperationRefusedError, match="exceeds"):
            await _perform(request, authorized, current, now=datetime.now(UTC))
        return
    receipt = await _perform(request, authorized, current, now=datetime.now(UTC))
    import hashlib

    assert receipt == {"path": "src/deploy.py", "sha256": hashlib.sha256(content).hexdigest()}
    current.assert_awaited_once()
    current.return_value = SimpleNamespace(assignment=authorized.assignment)
    with pytest.raises(OperationRefusedError, match="scope changed"):
        await _perform(request, authorized, current, now=datetime.now(UTC))
    authorized.operations_repair = False
    with pytest.raises(OperationRefusedError, match="operations story"):
        await _perform(request, authorized, current, now=datetime.now(UTC))


@pytest.mark.parametrize("operation", ["read_repository", "merge_pull_request"])
@pytest.mark.parametrize("status_state", ["success", "failure"])
async def test_repair_route_can_read_commit_status_without_bypassing_failure(provider_data, monkeypatch, operation, status_state):
    """GitHub requires statuses:read separately from checks:read, even with no statuses."""
    from src.agentauth import github_operation_routes as routes

    rec, _, _, _ = provider_data
    rec.overrides[f"GET /repos/acme/widgets/commits/{HEAD}/status"] = httpx.Response(200, json={"total_count": 1, "state": status_state})
    authorized = authorization()
    authorized.assignment = _assignment()
    minted = []

    async def mint(**kwargs):
        minted.append(kwargs["permissions"])
        return "gateway-held-test-token"

    def handler(request):
        if request.url.path.endswith("/status") and minted[-1].get("statuses") != "read":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        return rec.handler(request)

    @asynccontextmanager
    async def scoped_provider(**kwargs):
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler)) as client:
            async with GitHubProvider(**kwargs, client=client) as gh:
                yield gh

    monkeypatch.setattr(routes, "installation_token", mint)
    monkeypatch.setattr(routes, "GitHubProvider", scoped_provider)
    monkeypatch.setattr("src.agentauth.operations_repair.ensure_repair_readiness", AsyncMock())
    result = await routes._perform(
        routes.OperationRequest(operation=operation, pull_number=7, expected_head=HEAD),
        authorized,
        AsyncMock(return_value=authorized),
        now=datetime.now(UTC),
    )
    assert minted[0]["statuses"] == "read"
    if operation == "read_repository":
        assert all(value == "read" for value in minted[0].values())
        assert result["repair"]["checks_verified"] is (status_state == "success")
        assert not any(method == "PUT" for method, _, _ in rec.requests)
    else:
        assert result["merge"]["merged"] is (status_state == "success")
        assert any(method == "PUT" for method, _, _ in rec.requests) is (status_state == "success")


@pytest.mark.parametrize("mutate", [False, True])
@pytest.mark.parametrize("status_state", ["success", "failure", "pending"])
async def test_continuation_can_read_commit_status_without_bypassing_failure(provider_data, monkeypatch, mutate, status_state):
    from src.orchestration import operations_continuation as continuation

    rec, _, _, _ = provider_data
    rec.overrides[f"GET /repos/acme/widgets/commits/{HEAD}/status"] = httpx.Response(200, json={"total_count": 1, "state": status_state})
    authorized = authorization()
    authorized.assignment = _assignment()
    state = SimpleNamespace(authorized=authorized, binding=SimpleNamespace(org_id="tenant", installation_id=42, repo="acme/widgets"))
    minted = []

    async def mint(**kwargs):
        minted.append(kwargs["permissions"])
        return "gateway-held-test-token"

    def response(request):
        if request.url.path.endswith("/status") and minted[-1].get("statuses") != "read":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        return rec.handler(request)

    @asynccontextmanager
    async def scoped_provider(**kwargs):
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(response)) as client:
            async with GitHubProvider(**kwargs, client=client) as gh:
                yield gh

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(commit=AsyncMock())

    handler = continuation.OperationsContinuationHandler(factory, services=SimpleNamespace(authorize=AsyncMock(return_value=state)))
    monkeypatch.setattr(handler, "state", AsyncMock(return_value=state))
    monkeypatch.setattr(continuation, "installation_token", mint)
    monkeypatch.setattr(continuation, "GitHubProvider", scoped_provider)
    monkeypatch.setattr(continuation, "qualify_repair_readiness", AsyncMock())
    record = AsyncMock()
    monkeypatch.setattr(continuation, "record_repair_merge", record)
    result = await handler.merge(None, {"pull_number": 7, "head": HEAD}, mutate=mutate)
    assert minted[0]["statuses"] == "read"
    if not mutate:
        assert all(value == "read" for value in minted[0].values())
        assert result["checks_verified"] is (status_state == "success")
        assert not any(method == "PUT" for method, _, _ in rec.requests)
        record.assert_not_awaited()
    else:
        assert minted[0]["workflows"] == "write"
        assert result["merged"] is (status_state == "success")
        assert any(method == "PUT" for method, _, _ in rec.requests) is (status_state == "success")
        assert record.await_count == (1 if status_state == "success" else 0)
