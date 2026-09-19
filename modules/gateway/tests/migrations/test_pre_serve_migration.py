"""A failed expansion cannot publish the new serving template."""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location("pre_serve_migration", ROOT / "modules/gateway/scripts/migrate-before-rollout.py")
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


def deployment():
    return yaml.safe_load((ROOT / "modules/gateway/k8s/deployment.yaml").read_text())


def test_job_uses_release_config_without_joining_service_or_starting_server():
    source = deployment()
    job = script.migration_job(source, "registry/gateway:reviewed", "adp-gateway", "migration-test")
    pod = job["spec"]["template"]
    container = pod["spec"]["containers"][0]
    assert container["image"] == "registry/gateway:reviewed"
    assert container["command"] == ["env", "PYTHONPATH=/app", "alembic"]
    assert container["args"] == ["upgrade", "head"]
    assert container["envFrom"] == source["spec"]["template"]["spec"]["containers"][0]["envFrom"]
    assert pod["spec"]["serviceAccountName"] == "gateway-service"
    assert "app" not in pod["metadata"]["labels"]
    assert "ports" not in container and "readinessProbe" not in container
    assert job["spec"]["backoffLimit"] == 0
    assert source == deployment()


@pytest.mark.parametrize("condition,raises", [("Failed", True), ("Complete", False)])
def test_job_failure_stops_before_any_serving_mutation(monkeypatch, condition, raises):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if "--dry-run=client" in args:
            return json.dumps(deployment())
        if "get" in args:
            return json.dumps({"status": {"conditions": [{"type": condition, "status": "True"}]}})
        return ""

    monkeypatch.setattr(script, "command", command)
    if raises:
        with pytest.raises(RuntimeError, match="Serving release was not changed"):
            script.migrate("rendered.yaml", "registry/gateway:reviewed", "adp-gateway")
    else:
        script.migrate("rendered.yaml", "registry/gateway:reviewed", "adp-gateway")
    assert not any("apply" in call or "set" in call or "delete" in call for call in calls)


def test_workflow_gates_serving_and_engine_updates_on_migrations():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-deploy.yml").read_text())
    steps = workflow["jobs"]["deploy-backend"]["steps"]
    rollout = next(s["run"] for s in steps if s.get("name") == "Migrate schema then roll out new image")
    assert "set -euo pipefail" in rollout
    assert rollout.index("migrate-before-rollout.py") < rollout.index("kubectl apply")
    assert next(i for i, s in enumerate(steps) if s.get("name") == "Migrate schema then roll out new image") < next(
        i for i, s in enumerate(steps) if s.get("name") == "Update the scheduled orchestration engine"
    )
