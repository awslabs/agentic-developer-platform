"""Coverage parallelism must respect container limits, not the host's RAM."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "pytest_resources", Path(__file__).parents[1] / "pytest_resources.py"
)
resources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resources)
GIB = 1024**3


@pytest.mark.parametrize("name", ["memory.max", "memory/memory.limit_in_bytes"])
@pytest.mark.parametrize("gib,expected", [(8, 2), (15, 2), (16, 4), (32, 4)])
def test_container_limit_controls_coverage_workers(tmp_path, name, gib, expected):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(gib * GIB))
    limit = resources.memory_limit(tmp_path, physical=64 * GIB)
    assert limit == gib * GIB
    assert resources.workers(True, limit) == expected


def test_unlimited_container_and_large_v1_sentinel_use_physical_memory(tmp_path):
    (tmp_path / "memory.max").write_text("max")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory/memory.limit_in_bytes").write_text(str(2**63 - 4096))
    assert resources.memory_limit(tmp_path, physical=8 * GIB) == 8 * GIB
    assert resources.workers(False, 8 * GIB) == 4


def test_peak_and_oom_counters_are_retained(tmp_path):
    (tmp_path / "memory.peak").write_text("1234\n")
    (tmp_path / "memory.events").write_text("oom 0\noom_kill 0\n")
    result = resources.report(tmp_path)
    assert result["memory.peak"] == "1234"
    assert result["memory.events"] == "oom 0\noom_kill 0"
