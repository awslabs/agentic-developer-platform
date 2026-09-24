"""Issue constraints reach SkyPilot without an agent picking a machine."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).parents[1] / "agent/skills/skypilot/scripts/capacity_task.py"
spec = importlib.util.spec_from_file_location("skypilot_capacity_task", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def task(**overrides):
    return builder.capacity_task(
        **{
            "name": "issue-123-capacity",
            "gpus": ["H100:1", "A100-80GB:1"],
            "nodes": 2,
            "disk_gb": 100,
            "hold_seconds": 900,
            **overrides,
        }
    )


def test_requirement_only_request_leaves_placement_to_skypilot():
    result = task(cpus=4, memory_gb=32)
    assert result["num_nodes"] == 2
    resources = result["resources"]
    assert resources["cpus"] == "4+"
    assert resources["memory"] == "32+"
    assert resources["any_of"] == [
        {"accelerators": {"H100": 1}},
        {"accelerators": {"A100-80GB": 1}},
    ]
    for key in ("instance_type", "cloud", "region", "image_id", "ordered"):
        assert key not in json.dumps(resources)
    assert result["run"] == "sleep 900"
    assert not {"setup", "envs", "file_mounts", "workdir", "service"} & result.keys()


def test_allowed_clouds_remain_alternatives_in_one_allocation():
    result = task(clouds=["aws", "nebius"], spot=True)
    resources = result["resources"]
    assert resources["use_spot"] is True
    choices = {
        (item["cloud"], tuple(item["accelerators"].items()))
        for item in resources["any_of"]
    }
    assert choices == {
        (cloud, ((gpu, 1),))
        for cloud in ("aws", "nebius")
        for gpu in ("H100", "A100-80GB")
    }
    assert result["num_nodes"] == 2  # Not multiplied by alternative count.


def test_explicit_mixed_providers_have_distinct_tasks_for_one_eks_workflow():
    aws = task(name="issue-123-aws", clouds=["aws"], region="us-west-2")
    nebius = task(name="issue-123-nebius", clouds=["nebius"], region="eu-north1")
    assert aws["name"] != nebius["name"]
    assert {c["cloud"] for c in aws["resources"]["any_of"]} == {"aws"}
    assert {c["cloud"] for c in nebius["resources"]["any_of"]} == {"nebius"}


@pytest.mark.parametrize(
    "change",
    [
        {"name": "bad;run-command"},
        {"name": "BadName"},
        {"gpus": []},
        {"gpus": ["H100:0"]},
        {"gpus": ["H100:1;uname"]},
        {"gpus": ["H100:1", "H100:1"]},
        {"nodes": 0},
        {"nodes": True},
        {"disk_gb": 0},
        {"hold_seconds": 0},
        {"hold_seconds": 86401},
        {"hold_seconds": "900; true"},
        {"clouds": ["unreviewed"]},
        {"clouds": [{}]},
        {"clouds": ["aws", "aws"]},
        {"region": "us-west-2"},
        {"clouds": ["aws", "nebius"], "region": "us-west-2"},
        {"cpus": 0},
        {"memory_gb": False},
        {"spot": "false"},
    ],
)
def test_invalid_or_ambiguous_constraints_are_refused(change):
    with pytest.raises(builder.InvalidRequest):
        task(**change)


def test_cli_emits_task_json_without_provider_clients(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-I",  # No installed ADP package or current-directory imports needed.
            str(SCRIPT),
            "--name",
            "issue-123",
            "--gpu",
            "H100:1",
            "--nodes",
            "1",
            "--disk-gb",
            "100",
            "--hold-seconds",
            "600",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    value = json.loads(result.stdout)
    assert value["resources"] == {
        "accelerators": {"H100": 1},
        "disk_size": 100,
        "use_spot": False,
    }
    assert not list(tmp_path.iterdir())
