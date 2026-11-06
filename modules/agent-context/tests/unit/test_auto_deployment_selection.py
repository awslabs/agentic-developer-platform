"""Keep ingestion maintenance from rewriting unrelated live services."""

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location(
    "selection", ROOT / "modules/agent-context/scripts/select-auto-deployment.py"
)
selection = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selection)


def payload(*names):
    jobs = [{"name": name, "status": "completed", "conclusion": "success"} for name in names]
    return {"total_count": len(jobs), "jobs": jobs}


@pytest.mark.parametrize("names,expected", [
    (("Detect Changed Images", "Build ingestion"), False),
    (("Detect Changed Images", "Build deepwiki"), False),
    (("Detect Changed Images",), False),
    (("Build context-mcp",), True),
    (("Build ingestion", "Build deepwiki"), True),
    (tuple(sorted(selection.BUILD_JOBS)), True),
])
def test_select_actual_built_services(names, expected):
    assert selection.requires_full_deployment(payload(*names)) is expected


@pytest.mark.parametrize("mutation", ["incomplete", "unknown", "failed", "running"])
def test_uncertain_inventory_cannot_deploy(mutation):
    data = payload("Build ingestion")
    if mutation == "incomplete":
        data["total_count"] += 1
    elif mutation == "unknown":
        data["jobs"][0]["name"] = "Build unknown"
    elif mutation == "failed":
        data["jobs"][0]["conclusion"] = "failure"
    else:
        data["jobs"][0]["status"] = "in_progress"
    with pytest.raises(ValueError):
        selection.requires_full_deployment(data)


def test_full_rollout_requires_selection_but_scoped_promotion_remains_independent():
    workflow = yaml.safe_load((ROOT / ".github/workflows/agent-context-deploy.yml").read_text())
    jobs = workflow["jobs"]
    assert jobs["deploy"]["needs"] == "select-deployment"
    assert "needs.select-deployment.outputs.full_deployment == 'true'" in jobs["deploy"]["if"]
    assert jobs["select-deployment"]["permissions"] == {"contents": "read", "actions": "read"}
    assert "environment" not in jobs["select-deployment"]
    for name in ("promote-ingestion", "promote-deepwiki"):
        assert "needs" not in jobs[name]
        assert "github.event_name == 'workflow_dispatch'" in jobs[name]["if"]
