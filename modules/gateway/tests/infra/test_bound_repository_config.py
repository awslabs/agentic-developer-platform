"""Fresh deployment wiring must not supply a global repository to bound flows."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/gateway-deploy.yml",
        "platform/scripts/deploy-all.sh",
        "modules/gateway/infra/main.tf",
        "modules/gateway/infra/variables.tf",
        "modules/gateway/infra/release-configuration.tf",
        "modules/gateway/infra/modules/orchestration-tick/main.tf",
        "modules/gateway/infra/modules/orchestration-tick/variables.tf",
        "modules/gateway/k8s/configmap.yaml",
        "modules/gateway/scripts/stage1-runtime-readback.py",
        "config/release-defaults/preserved-inputs.json",
    ],
)
def test_fresh_install_and_renderer_do_not_require_global_repository(path):
    source = (ROOT / path).read_text()
    assert "BG_ORCH_DISPATCH_REPO" not in source
    assert "orchestration_dispatch_repo" not in source
    assert "orchestration-dispatch-repo" not in source


def test_configmap_keeps_queue_transport_without_repository_target():
    config = yaml.safe_load((ROOT / "modules/gateway/k8s/configmap.yaml").read_text())
    assert "BG_ORCH_DISPATCH_REPO" not in config["data"]
    assert "AGENT_DISPATCH_QUEUE_URL" in config["data"]
    tick = (ROOT / "modules/gateway/infra/modules/orchestration-tick/main.tf").read_text()
    assert "BG_ORCH_DISPATCH_QUEUE_URL    = var.agent_submit_queue_url" in tick
    assert "BG_ORCH_DISPATCH_MAX_PER_TICK = tostring(var.dispatch_max_per_tick)" in tick
