from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[5]
BUILD_WORKFLOWS = (
    ".github/workflows/arc-runner-build.yml",
    ".github/workflows/chat-agent-deploy.yml",
    ".github/workflows/cyber-worker-build.yml",
    ".github/workflows/gateway-infra-apply.yml",
    ".github/workflows/pyjwt-layer-build.yml",
    ".github/workflows/psycopg2-layer-build.yml",
    ".github/workflows/superplane-api-build.yml",
    ".github/workflows/superplane-controller-build.yml",
    ".github/workflows/superplane-monitor-build.yml",
)


@pytest.mark.parametrize("workflow", BUILD_WORKFLOWS)
def test_build_workflow_does_not_mutate_terraform_owned_project(workflow: str) -> None:
    text = (REPO_ROOT / workflow).read_text()

    assert "batch-get-projects" in text
    assert "codebuild-role" not in text
    assert "create-project" not in text
    assert "update-project" not in text
    assert "--service-role" not in text


def test_deploy_all_has_no_administrator_codebuild_bootstrap() -> None:
    text = (REPO_ROOT / "platform/scripts/deploy-all.sh").read_text()

    assert "ensure_codebuild_role" not in text
    assert "codebuild-role" not in text
    assert "AdministratorAccess" not in text


def test_chat_and_gateway_builds_publish_to_distinct_repositories() -> None:
    chat_buildspec = (REPO_ROOT / "codebuild/bs-chat-agent.yml").read_text()
    gateway_buildspec = (REPO_ROOT / "codebuild/bs-agent-gateway.yml").read_text()
    codebuild_module = (
        REPO_ROOT / "platform/infra/modules/codebuild/main.tf"
    ).read_text()

    assert 'ECR_REPO="adp-chat-agent"' in chat_buildspec
    assert 'ECR_REPO="adp-agent-gateway"' in gateway_buildspec
    assert 'ecr_repos      = ["adp-chat-agent"]' in codebuild_module
    assert 'ecr_repos      = ["adp-agent-gateway"]' in codebuild_module
