"""Detached requirement mapping and provider-state completion boundaries."""

# ruff: noqa: F811 -- imported pytest fixture
import hashlib

import httpx
import pytest

from src.agentauth.github_operations import OperationRefusedError
from src.agentauth.task_completion_service import required_acceptance
from src.agentauth.task_repository_publication import observe_task_change
from src.tasks.store import TaskStoreError
from tests.agentauth.test_task_repository_policy import BINDING
from tests.agentauth.test_task_repository_publication import TASK, publish, scope  # noqa: F401

CRITERION = "The service returns the expected result."
KEY = hashlib.sha256(("0\0" + CRITERION).encode()).hexdigest()


def test_only_explicit_trusted_detached_check_can_certify_a_requirement():
    binding = {"acceptance_checks": {KEY: "unit"}, "validation_checks": [{"name": "unit", "argv": ["/opt/adp-checks/unit"]}]}
    assert required_acceptance(binding, [CRITERION]) == {KEY: "unit"}
    for command in (["pytest"], ["/bin/sh", "test.sh"], ["/opt/adp-checks/../untrusted"], ["/work/test"]):
        binding["validation_checks"][0]["argv"] = command
        with pytest.raises(TaskStoreError, match="detached check"):
            required_acceptance(binding, [CRITERION])


@pytest.mark.parametrize("criteria", [[], [""], ["Another criterion"], [CRITERION, "Extra requirement"]])
def test_missing_changed_or_additional_requirements_are_not_silently_certified(criteria):
    binding = {"acceptance_checks": {KEY: "unit"}, "validation_checks": [{"name": "unit", "argv": ["/opt/adp-checks/unit"]}]}
    with pytest.raises(TaskStoreError):
        required_acceptance(binding, criteria)


@pytest.mark.asyncio
async def test_completion_observes_exact_open_pr_with_read_only_credentials(scope):
    receipt = await publish(scope)
    count = len(scope.requests)
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(scope.handle)) as client:
        result = await observe_task_change(
            db=None,
            tenant="tenant",
            task_id=TASK,
            frozen={"alias": "application", "binding": BINDING},
            receipt=receipt,
            reauthorize=scope.authorize,
            provider_client=client,
        )
    assert result == receipt
    assert all(method == "GET" for method, path in scope.requests[count:])
    assert scope.token.await_args.kwargs["permissions"] == {"contents": "read", "pull_requests": "read", "metadata": "read"}


@pytest.mark.parametrize("fault", ["head", "tree", "closed", "draft", "fork", "base", "url"])
@pytest.mark.asyncio
async def test_provider_drift_cannot_be_certified(scope, fault):
    receipt = await publish(scope)
    if fault == "head":
        scope.pull["head"]["sha"] = "0" * 40
    elif fault == "tree":
        scope.commit["tree"]["sha"] = "0" * 40
    elif fault == "closed":
        scope.pull["state"] = "closed"
    elif fault == "draft":
        scope.pull["draft"] = True
    elif fault == "fork":
        scope.pull["head"]["repo"]["id"] = 789
    elif fault == "base":
        scope.pull["base"]["ref"] = "other"
    else:
        scope.pull["html_url"] = "https://example.invalid"
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(scope.handle)) as client:
        with pytest.raises(OperationRefusedError):
            await observe_task_change(
                db=None,
                tenant="tenant",
                task_id=TASK,
                frozen={"alias": "application", "binding": BINDING},
                receipt=receipt,
                reauthorize=scope.authorize,
                provider_client=client,
            )
