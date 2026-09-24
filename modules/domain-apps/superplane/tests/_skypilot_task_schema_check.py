"""Explicit remote check against the pinned SkyPilot parser, without cloud I/O.

Kept outside default test collection: the ordinary domain environment does not
install SkyPilot. Domain CI runs this explicitly in a separate pinned venv.
"""

import copy
import importlib.util
from pathlib import Path

import pytest
import sky


@pytest.mark.parametrize(
    "clouds", [[], ["aws"], ["nebius"], ["aws", "nebius", "lambda"]]
)
def test_actual_skypilot_parser_preserves_resource_choices(clouds):
    path = Path(__file__).parents[1] / "agent/skills/skypilot/scripts/capacity_task.py"
    spec = importlib.util.spec_from_file_location("capacity_task_native", path)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    raw = builder.capacity_task(
        name="issue-123",
        gpus=["H100:1", "A100-80GB:1"],
        nodes=2,
        disk_gb=100,
        hold_seconds=900,
        clouds=clouds,
        cpus=4,
        memory_gb=32,
    )
    parsed = sky.Task.from_yaml_config(copy.deepcopy(raw))
    assert parsed.num_nodes == 2
    assert parsed.run == "sleep 900"
    assert len(parsed.resources) == 2 * max(1, len(clouds))
    for resource in parsed.resources:
        assert resource.instance_type is None
        assert resource.region is None
        assert resource.accelerators in ({"H100": 1}, {"A100-80GB": 1})
        assert resource.cpus == "4+"
        assert resource.memory == "32+"
        if clouds:
            assert str(resource.cloud).lower() in clouds
        else:
            assert resource.cloud is None
