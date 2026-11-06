"""Offline build selection and staging for the shared evaluation contract."""

import fnmatch
import re
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]


def test_contract_only_changes_select_gateway_and_worker_builds():
    contract_path = "contracts/multi-repository-evaluation/v1/models.py"
    for name, event in (
        ("gateway-ci.yml", "pull_request"),
        ("gateway-ci.yml", "push"),
        ("gateway-deploy.yml", "push"),
        ("agent-worker-image.yml", "pull_request"),
        ("agent-worker-image.yml", "push"),
    ):
        document = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        patterns = document[True][event]["paths"]
        assert any(fnmatch.fnmatchcase(contract_path, pattern) for pattern in patterns), (name, event)
    deployment = (ROOT / ".github/workflows/gateway-deploy.yml").read_text()
    backend_filter = re.search(r"grep -qE \'([^\']+)\'; then\n\s+BACKEND=true", deployment)
    assert backend_filter is not None
    assert re.search(backend_filter.group(1), contract_path)
    assert not re.search(backend_filter.group(1), "docs/architecture.md")


def test_gateway_staging_copies_shared_validator_and_fixture(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "modules/gateway/scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "modules/gateway/scripts/stage-contracts.sh", scripts / "stage-contracts.sh")
    for name in ("orchestration-review", "orchestration-evaluation", "multi-repository-evaluation"):
        shutil.copytree(ROOT / "contracts" / name, repo / "contracts" / name)
    for source in ("projection.py", "results.py"):
        destination = repo / "modules/insights" / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "modules/insights" / source, destination)
    identity = repo / "modules/harness/jobs/harness_jobs/identity.py"
    identity.parent.mkdir(parents=True)
    identity.write_text("")
    (repo / "modules/agent-factory/rules/personas").mkdir(parents=True)
    observer = repo / "modules/tools/adapters/runtime_evidence.py"
    observer.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "modules/tools/adapters/runtime_evidence.py", observer)
    jira_contract = repo / "modules/tools/adp_tools/jira_context.py"
    jira_contract.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "modules/tools/adp_tools/jira_context.py", jira_contract)
    for name in (
        "bootstrap-worker-61fcfc.json",
        "bootstrap-management-worker-0025522e.json",
        "bootstrap-reader-worker-1eb49977.json",
        "bootstrap-reader-worker-c8f44a0b.json",
    ):
        qualification = Path("modules/domain-apps/superplane/installation/evidence") / name
        (repo / qualification).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / qualification, repo / qualification)
    for package in ("workspace_bootstrap/superplane_bootstrap", "contracts/superplane_contracts"):
        relative = Path("modules/domain-apps/superplane") / package
        shutil.copytree(ROOT / relative, repo / relative, ignore=shutil.ignore_patterns("__pycache__"))
    script = scripts / "stage-contracts.sh"
    subprocess.run(["bash", str(script)], check=True, capture_output=True, text=True)
    for name in ("models.py", "evaluation-receipt.golden.json"):
        relative = Path("multi-repository-evaluation/v1") / name
        assert (repo / "modules/gateway/contracts" / relative).read_bytes() == (repo / "contracts" / relative).read_bytes()
    (repo / "contracts/multi-repository-evaluation/v1/evaluation-receipt.golden.json").unlink()
    failed = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert failed.returncode != 0
    assert "incomplete multi-repository-evaluation" in failed.stderr
