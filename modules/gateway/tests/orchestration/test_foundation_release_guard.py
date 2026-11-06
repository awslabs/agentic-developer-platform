"""Foundation gateway validation reuses native authority without changing ordinary releases."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guard(monkeypatch):
    shared = load("modules/tools/adapters/guard_release.py", "guard_release")
    monkeypatch.setitem(sys.modules, "guard_release", shared)
    return load("modules/gateway/scripts/guard-foundation-release.py", "foundation_guard")


@pytest.fixture
def environment():
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ATTEMPT": "1",
        "ADP_RELEASE_GROUP": "atlassian-foundation",
        "ADP_ENVIRONMENT": "dev",
        "ADP_SOURCE_REVISION": "a" * 40,
        "ADP_DEFINITION_REVISION": "b" * 40,
        "ADP_CORRELATION": "orch:00000000-0000-0000-0000-000000000001",
        "ADP_FOUNDATION_CONNECTION_ROLE_ARN": "arn:aws:iam::000000000000:role/verifier",
        "ADP_DEPLOY_ROLE_ARN": "arn:aws:iam::000000000000:role/adp-dev-gateway-trusted-deployment",
        "ADP_FOUNDATION_GATEWAY_URL": "https://gateway.example.test",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.actions.githubusercontent.com/token",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "private-test-request-token",
    }


class Response:
    status = 200

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return json.dumps(self.value).encode()[:limit]


class Opener:
    def __init__(self, authorized=True):
        self.calls = []
        self.authorized = authorized

    def open(self, request, timeout):
        assert timeout == (15 if not self.calls else 45)
        self.calls.append(request)
        return Response({"value": "header.body.signature"} if len(self.calls) == 1 else {"authorized": self.authorized})


def test_guard_binds_exact_gateway_job(guard, environment):
    opener = Opener()
    guard.check(environment, opener)
    assert len(opener.calls) == 2
    assert "audience=adp-operations-release-job-v1" in opener.calls[0].full_url
    request = opener.calls[1]
    assert request.full_url == "https://gateway.example.test/api/orchestration/operations-release-job/check"
    assert request.get_header("Authorization") == "Bearer header.body.signature"
    assert json.loads(request.data) == {
        "correlation": environment["ADP_CORRELATION"],
        "source_revision": "a" * 40,
        "dispatch_revision": "b" * 40,
        "workflow": "gateway-deploy.yml",
        "role_arn": environment["ADP_FOUNDATION_CONNECTION_ROLE_ARN"],
        "mutation_role_arn": environment["ADP_DEPLOY_ROLE_ARN"],
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("ADP_RELEASE_GROUP", "none"),
        ("ADP_ENVIRONMENT", "prod"),
        ("ADP_MANUAL_SOURCE", "c" * 40),
        ("ADP_PRODUCT_CASES", "api_product_lifecycle"),
        ("ADP_PROOF_PHASE", "stage.identity-proof"),
        ("ADP_PRODUCT_ROLLBACK", "true"),
        ("ADP_CORRELATION", ""),
        ("ADP_SOURCE_REVISION", "main"),
        ("ADP_DEFINITION_REVISION", "main"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_EVENT_NAME", "push"),
        ("ADP_FOUNDATION_CONNECTION_ROLE_ARN", ""),
        ("ADP_DEPLOY_ROLE_ARN", "arn:aws:iam::123456789012:role/adp-dev-gateway-trusted-deployment"),
        ("ADP_FOUNDATION_GATEWAY_URL", "http://gateway.example.test"),
    ],
)
def test_invalid_foundation_inputs_refuse_before_identity_request(guard, environment, key, value):
    environment[key] = value
    opener = Opener()
    with pytest.raises(guard.GuardRefused, match="native_release_guard_refused"):
        guard.check(environment, opener)
    assert not opener.calls


def test_lost_or_refused_authority_cannot_proceed(guard, environment):
    with pytest.raises(guard.GuardRefused, match="native_release_guard_refused") as error:
        guard.check(environment, Opener(authorized=False))
    assert "private-test" not in str(error.value)


def test_workflow_guards_foundation_before_mutation_and_leaves_ordinary_runs_unchanged():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-deploy.yml").read_text())
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["release_group"]["default"] == "none"
    assert inputs["release_group"]["options"] == ["none", "atlassian-foundation"]
    backend = workflow["jobs"]["deploy-backend"]
    assert backend["permissions"]["id-token"] == "write"
    steps = backend["steps"]
    guard_step = next(step for step in steps if step.get("name") == "Validate accepted foundation job and shared lease")
    assert guard_step["if"] == "inputs.release_group == 'atlassian-foundation'"
    assert guard_step["env"]["ADP_DEFINITION_REVISION"] == "${{ github.workflow_sha }}"
    for name in ("ADP_DEPLOY_ROLE_ARN", "ADP_FOUNDATION_CONNECTION_ROLE_ARN", "ADP_FOUNDATION_GATEWAY_URL"):
        assert guard_step["env"][name] == "${{ vars." + name + " }}"
    loader = next(step for step in steps if step.get("name") == "Load maintained deployment guards")
    assert steps.index(loader) < steps.index(guard_step)
    for path in ("modules/gateway/scripts/guard-foundation-release.py", "modules/tools/adapters/guard_release.py"):
        assert f'git show "$ADP_WORKFLOW_REVISION:{path}"' in loader["run"]
    trusted = next(step for step in steps if step.get("uses") == "./.github/actions/trusted-deployment")
    assert steps.index(guard_step) < steps.index(trusted)
    assert "(inputs.release_group == '' || inputs.release_group == 'none')" in workflow["jobs"]["deploy-frontend"]["if"]
    context = next(step for step in steps if step.get("name") == "Record deployment workflow context")
    assert context["env"]["INPUT_RELEASE_GROUP"] == "${{ inputs.release_group || 'none' }}"
