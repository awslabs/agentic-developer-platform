"""Run actual staging and the isolated image selfcheck without cloud access."""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
CHECK = Path("src/orchestration/adapter_observer_selfcheck.py")


@pytest.fixture
def staged(tmp_path):
    repo = tmp_path / "repo"
    gateway = repo / "modules/gateway"
    for relative in (
        "modules/gateway/scripts/stage-contracts.sh",
        "modules/tools/adapters/runtime_evidence.py",
        "modules/tools/adp_tools/jira_context.py",
        "modules/harness/jobs/harness_jobs/identity.py",
        "modules/insights/projection.py",
        "modules/insights/results.py",
        "modules/domain-apps/superplane/installation/evidence/bootstrap-worker-61fcfc.json",
        "modules/domain-apps/superplane/installation/evidence/bootstrap-management-worker-0025522e.json",
        "modules/domain-apps/superplane/installation/evidence/bootstrap-reader-worker-1eb49977.json",
        "modules/domain-apps/superplane/installation/evidence/bootstrap-reader-worker-c8f44a0b.json",
        "modules/gateway/" + str(CHECK),
    ):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    (repo / "modules/agent-factory/rules/personas").mkdir(parents=True)
    for name in ("orchestration-review", "orchestration-evaluation", "multi-repository-evaluation"):
        shutil.copytree(ROOT / "contracts" / name, repo / "contracts" / name)
    for package in ("workspace_bootstrap/superplane_bootstrap", "contracts/superplane_contracts"):
        relative = Path("modules/domain-apps/superplane") / package
        shutil.copytree(ROOT / relative, repo / relative, ignore=shutil.ignore_patterns("__pycache__"))
    # An application sentinel in the owning tree must never enter the gateway.
    app = repo / "modules/tools/adapters/tool_adapters"
    app.mkdir()
    (app / "server.py").write_text("raise RuntimeError('application_must_not_ship')\n")
    subprocess.run(["bash", str(gateway / "scripts/stage-contracts.sh")], check=True, capture_output=True, text=True)
    return gateway


def selfcheck(gateway):
    return subprocess.run([sys.executable, "-I", "-B", "-S", str(gateway / CHECK)], cwd=gateway, capture_output=True, text=True, timeout=15)


def test_actual_staging_and_isolated_build_gate(staged):
    source = staged / "contracts/tool-adapters/runtime_evidence.py"
    assert source.read_bytes() == (ROOT / "modules/tools/adapters/runtime_evidence.py").read_bytes()
    assert {path.name for path in source.parent.iterdir()} == {"runtime_evidence.py", "runtime_evidence.sha256"}
    result = selfcheck(staged)
    assert result.returncode == 0, result.stderr
    assert "selfcheck passed" in result.stdout
    assert not list(staged.rglob("__pycache__"))
    dockerfile = (ROOT / "modules/gateway/Dockerfile").read_text()
    copy = dockerfile.index("COPY contracts/ contracts/")
    gate = dockerfile.index("RUN python -I -B -S " + str(CHECK))
    assert copy < gate < dockerfile.index("USER 65532:65532")


def test_gateway_loader_uses_actual_staged_owner_source(staged, monkeypatch):
    from src.orchestration import deployment_runtime_adapters as adapters

    monkeypatch.setattr(adapters, "__file__", str(staged / "src/orchestration/deployment_runtime_adapters.py"))
    adapters._observer.cache_clear()
    try:
        source = staged / "contracts/tool-adapters/runtime_evidence.py"
        assert adapters._observer_path() == source
        assert Path(adapters._observer().__file__) == source
    finally:
        adapters._observer.cache_clear()


@pytest.mark.parametrize("missing", ["runtime_evidence.py", "runtime_evidence.sha256"])
def test_image_gate_rejects_incomplete_staging(staged, missing):
    (staged / "contracts/tool-adapters" / missing).unlink()
    result = selfcheck(staged)
    assert result.returncode == 1
    assert "staging must contain" in result.stderr


def test_image_gate_rejects_changed_bytes(staged):
    source = staged / "contracts/tool-adapters/runtime_evidence.py"
    source.write_text(source.read_text() + "\n# altered after staging\n")
    result = selfcheck(staged)
    assert result.returncode == 1
    assert "checksum mismatch" in result.stderr


@pytest.mark.parametrize("dependency", ["tool_adapters.server", "src.app", "sqlalchemy"])
def test_isolated_import_refuses_service_or_third_party_dependency(staged, dependency):
    source = staged / "contracts/tool-adapters/runtime_evidence.py"
    source.write_text(source.read_text() + f"\nimport {dependency}\n")
    (source.parent / "runtime_evidence.sha256").write_text(hashlib.sha256(source.read_bytes()).hexdigest() + "  runtime_evidence.py\n")
    result = selfcheck(staged)
    assert result.returncode == 1
    assert "No module named" in result.stderr


def test_repeated_staging_replaces_old_content_and_drops_extras(staged):
    destination = staged / "contracts/tool-adapters"
    (destination / "runtime_evidence.py").write_text("stale")
    (destination / "application.py").write_text("unwanted")
    subprocess.run(["bash", str(staged / "scripts/stage-contracts.sh")], check=True, capture_output=True, text=True)
    result = selfcheck(staged)
    assert result.returncode == 0, result.stderr
    assert not (destination / "application.py").exists()


def test_staging_refuses_missing_owner_source(staged):
    (staged.parent / "tools/adapters/runtime_evidence.py").unlink()
    result = subprocess.run(["bash", str(staged / "scripts/stage-contracts.sh")], capture_output=True, text=True)
    assert result.returncode != 0
    assert selfcheck(staged).returncode == 1
