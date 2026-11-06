"""External review rules must be qualified before a worker publishes a repair."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.agentauth.github_operation_routes import _perform
from src.agentauth.github_operations import GitHubOperation, OperationRefusedError
from src.agentauth.github_provider import GitHubProvider, ProviderNotFoundError
from src.agentauth.operations_repair import ensure_repair_readiness, qualify_repair_readiness
from tests.agentauth.test_github_provider import Recorder, _assignment
from tests.orchestration.test_report_dispatch import engine, prepare, reporting, session, session_factory  # noqa: F401


@pytest.fixture
def rules_provider():
    branch = {"name": "main", "protected": False}
    rules = []
    protection = {"required_pull_request_reviews": None}
    overrides = {
        "GET /repos/acme/widgets/branches/main/protection": lambda: httpx.Response(200, json=protection),
        "GET /repos/acme/widgets/branches/main": lambda: httpx.Response(200, json=branch),
        "GET /repos/acme/widgets/rules/branches/main": lambda: httpx.Response(200, json=rules),
    }
    return Recorder(overrides=overrides), branch, rules, protection


@pytest.mark.parametrize(
    "requirement",
    [
        "classic",
        "code_owner",
        "classic_last_push",
        "ruleset",
        "ruleset_owner",
        "ruleset_threads",
        "unknown_rule",
        "ruleset_last_push",
        "malformed_threads",
        "missing_rules",
        "missing_protection",
        "malformed_review",
        "too_many_rules",
    ],
)
async def test_repair_readiness_refuses_external_approval_or_unknown_rules(rules_provider, requirement):
    recorder, branch, rules, protection = rules_provider
    if requirement in {"classic", "code_owner", "classic_last_push", "missing_protection", "malformed_review"}:
        branch["protected"] = True
    if requirement == "classic":
        protection["required_pull_request_reviews"] = {"required_approving_review_count": 1, "require_code_owner_reviews": False}
    elif requirement == "code_owner":
        protection["required_pull_request_reviews"] = {"required_approving_review_count": 0, "require_code_owner_reviews": True}
    elif requirement == "classic_last_push":
        protection["required_pull_request_reviews"] = {
            "required_approving_review_count": 0,
            "require_code_owner_reviews": False,
            "require_last_push_approval": True,
        }
    elif requirement == "malformed_review":
        protection["required_pull_request_reviews"] = {"required_approving_review_count": "0"}
    elif requirement.startswith("ruleset") or requirement == "malformed_threads":
        rules.append(
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": int(requirement == "ruleset"),
                    "require_code_owner_review": requirement == "ruleset_owner",
                    "required_review_thread_resolution": "true" if requirement == "malformed_threads" else requirement == "ruleset_threads",
                    "require_last_push_approval": requirement == "ruleset_last_push",
                },
            }
        )
    elif requirement == "unknown_rule":
        rules.append({"type": "unrecognized_reviewer_gate"})
    elif requirement == "missing_rules":
        recorder.overrides["GET /repos/acme/widgets/rules/branches/main"] = httpx.Response(403, json={"message": "refused"})
    elif requirement == "too_many_rules":
        rules.extend({"type": "required_status_checks"} for _ in range(100))
    elif requirement == "missing_protection":
        recorder.overrides["GET /repos/acme/widgets/branches/main/protection"] = httpx.Response(404, json={"message": "not found"})
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(recorder.handler)) as client:
        async with GitHubProvider(token="test-token", assignment=_assignment(), client=client) as provider:
            with pytest.raises((OperationRefusedError, ProviderNotFoundError)):
                await qualify_repair_readiness(provider, "main")
    assert not any(method != "GET" for method, _, _ in recorder.requests)
    assert any("?per_page=100" in path for _, path, _ in recorder.requests if "/rules/branches/" in path)


@pytest.mark.parametrize("protected", [False, True])
async def test_repair_readiness_accepts_no_external_review(rules_provider, protected):
    recorder, branch, _, protection = rules_provider
    branch["protected"] = protected
    protection["required_pull_request_reviews"] = {"required_approving_review_count": 0, "require_code_owner_reviews": False}
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(recorder.handler)) as client:
        async with GitHubProvider(token="test-token", assignment=_assignment(), client=client) as provider:
            await qualify_repair_readiness(provider, "main")
    assert not any(method != "GET" for method, _, _ in recorder.requests)


async def test_readiness_is_recorded_once_for_accepted_scope(session, session_factory, rules_provider, monkeypatch):  # noqa: F811
    node = await prepare(session)
    await session.commit()
    recorder, _, _, _ = rules_provider
    assignment = replace(_assignment(), tenant_id=node.org_id, node_id=node.id, accepted_plan_version=1)
    authorized = SimpleNamespace(assignment=assignment, policy=SimpleNamespace(policy_hash="b" * 64))
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session_factory)
    token = AsyncMock(return_value="test-token")
    monkeypatch.setattr("src.agentauth.github_operation_service.installation_token", token)

    original_provider = GitHubProvider

    def provider(*, token, assignment):
        client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(recorder.handler))
        return original_provider(token=token, assignment=assignment, client=client)

    monkeypatch.setattr("src.agentauth.github_provider.GitHubProvider", provider)
    reauthorize = AsyncMock(return_value=authorized)
    await ensure_repair_readiness(authorized, reauthorize)
    first_calls = len(recorder.requests)
    await ensure_repair_readiness(authorized, reauthorize)
    assert first_calls == len(recorder.requests) and token.await_count == 1
    assert token.await_args.kwargs["permissions"] == {"metadata": "read", "administration": "read", "contents": "read"}
    changed = SimpleNamespace(assignment=replace(assignment, accepted_plan_version=2), policy=authorized.policy)
    await ensure_repair_readiness(changed, AsyncMock(return_value=changed))
    assert token.await_count == 2


@pytest.mark.parametrize(
    "operation",
    [
        GitHubOperation.VALIDATE_REPAIR_EDIT,
        GitHubOperation.PUBLISH_COMMIT,
        GitHubOperation.UPSERT_PULL_REQUEST,
        GitHubOperation.MERGE_PULL_REQUEST,
    ],
)
async def test_external_review_refuses_repair_before_publication(session, session_factory, rules_provider, monkeypatch, operation):  # noqa: F811
    node = await prepare(session)
    await session.commit()
    recorder, branch, _, protection = rules_provider
    branch["protected"] = True
    protection["required_pull_request_reviews"] = {"required_approving_review_count": 1, "require_code_owner_reviews": False}
    assignment = replace(_assignment(), tenant_id=node.org_id, node_id=node.id, accepted_plan_version=1)
    authorized = SimpleNamespace(assignment=assignment, policy=SimpleNamespace(policy_hash="b" * 64), operations_repair=True)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session_factory)
    readiness_token = AsyncMock(return_value="test-token")
    monkeypatch.setattr("src.agentauth.github_operation_service.installation_token", readiness_token)
    publication_token = AsyncMock(side_effect=AssertionError("repair publication cannot start"))
    monkeypatch.setattr("src.agentauth.github_operation_routes.installation_token", publication_token)

    original_provider = GitHubProvider

    def provider(*, token, assignment):
        client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(recorder.handler))
        return original_provider(token=token, assignment=assignment, client=client)

    monkeypatch.setattr("src.agentauth.github_provider.GitHubProvider", provider)
    with pytest.raises(OperationRefusedError, match="external approval"):
        await _perform(SimpleNamespace(operation=operation), authorized, AsyncMock(return_value=authorized), now=None)
    readiness_token.assert_awaited_once()
    publication_token.assert_not_awaited()
    assert all(method == "GET" for method, _, _ in recorder.requests)


@pytest.mark.parametrize("case", ["unavailable", "protected", "public", "unknown_visibility", "generic_403", "unauthorized", "server_error"])
async def test_unavailable_rules_require_explicit_private_unprotected_provider_evidence(rules_provider, case):
    recorder, branch, _, _ = rules_provider
    from tests.agentauth.test_github_provider import REPOSITORY_ID

    repository = {"id": REPOSITORY_ID, "default_branch": "main", "private": True}
    if case == "public":
        repository["private"] = False
    elif case == "unknown_visibility":
        repository.pop("private")
    branch["protected"] = case == "protected"
    recorder.overrides["GET /repos/acme/widgets"] = httpx.Response(200, json=repository)
    message = "Upgrade to GitHub Pro or make this repository public to enable this feature."
    if case == "generic_403":
        message = "Resource not accessible by integration"
    status = 401 if case == "unauthorized" else 503 if case == "server_error" else 403
    recorder.overrides["GET /repos/acme/widgets/rules/branches/main"] = httpx.Response(status, json={"message": message})
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(recorder.handler)) as client:
        async with GitHubProvider(token="test-token", assignment=_assignment(), client=client) as provider:
            if case == "unavailable":
                await qualify_repair_readiness(provider, "main")
            else:
                with pytest.raises(OperationRefusedError):
                    await qualify_repair_readiness(provider, "main")
    assert all(method == "GET" for method, _, _ in recorder.requests)
