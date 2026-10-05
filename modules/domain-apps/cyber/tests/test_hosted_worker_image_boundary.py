"""The base image stays independent of Cyber's optional runtime layer."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def test_base_worker_excludes_optional_domain_assets():
    dockerfile = (ROOT / "modules/agent-factory/agent-worker-image/Dockerfile").read_text()
    assert "COPY modules/domain-apps/" not in dockerfile
    assert "COPY --chown=agent:agent modules/domain-apps/" not in dockerfile
    assert "ADP_TASK_INCLUDE_CYBER=false" in dockerfile


def test_cyber_module_builds_and_publishes_its_runtime_layer():
    module = ROOT / "modules/domain-apps/cyber"
    dockerfile = (module / "agent/Dockerfile.hosted-worker").read_text()
    assert "COPY --from=cyber-task-builder" in dockerfile
    assert "modules/domain-apps/cyber/agent/skills/" in dockerfile
    assert "modules/domain-apps/cyber/tools/cyber_tools/" in dockerfile
    assert (module / "scripts/publish-hosted-worker.sh").is_file()
    terraform = (module / "infra/hosted-integration/main.tf").read_text()
    assert 'resource "aws_ecr_repository" "hosted_worker"' in terraform
    assert 'output "worker_image"' in terraform


def test_base_gateway_does_not_publish_cyber_broker_settings():
    configmap = (ROOT / "modules/gateway/k8s/configmap.yaml").read_text()
    for key in ("CYBER_SAMPLE_BUCKET", "CYBER_TRIAGE_QUEUE", "CYBER_STATIC_QUEUE", "CYBER_RESULTS_TABLE"):
        assert key not in configmap
    assert (ROOT / "modules/domain-apps/cyber/scripts/configure-gateway.sh").is_file()
