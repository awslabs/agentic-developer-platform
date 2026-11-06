"""Prove early selection preserves tests and avoids importing other shards."""

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from tests.ci_sharding import partition_files


@pytest.mark.parametrize("shards", [3, 8, 16])
def test_balancing_is_complete_deterministic_and_keeps_cli_together(shards):
    files = ["tests/cli/test_a.py", "tests/cli/test_b.py", "tests/test_heavy.py", "tests/test_new.py", "tests/test_light.py"]
    files += [f"tests/test_new_{index}.py" for index in range(30)]
    durations = {"tests/test_heavy.py::test_slow": 100, "tests/test_light.py::test_quick": 1}
    plan = partition_files(files, durations, shards)
    assert all(plan)
    assert Counter(file for shard in plan for file in shard) == Counter(files)
    assert plan == partition_files(list(reversed(files)), durations, shards)
    assert any({"tests/cli/test_a.py", "tests/cli/test_b.py"} <= set(shard) for shard in plan)


@pytest.mark.parametrize("bad_duration", [-1, float("nan"), float("inf"), "10"])
def test_invalid_duration_fails_instead_of_losing_tests(bad_duration):
    with pytest.raises(ValueError):
        partition_files(["tests/test_a.py"], {"tests/test_a.py::test_a": bad_duration}, 2)


@pytest.mark.parametrize("workers", [0, 2])
def test_real_pytest_partitions_before_import_and_preserves_markers_and_new_tests(tmp_path, workers):
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = live_only: live test\n")
    directory = tmp_path / "tests"
    directory.mkdir()
    for name in ("a", "b", "new", "explicit"):
        destination = tmp_path if name == "explicit" else directory
        (destination / f"test_{name}.py").write_text(
            "import os, pathlib, pytest\n"
            f"pathlib.Path(os.environ['IMPORT_LOG']).open('a').write('{name}\\n')\n"
            "@pytest.mark.parametrize('value', [1, 2, 3])\n"
            "def test_values(value): assert value > 0\n"
            "@pytest.mark.live_only\n"
            "def test_live(): raise AssertionError('must be deselected')\n"
        )
    (directory / "test_ignored.py").write_text("raise AssertionError('explicitly ignored')\n")
    (tmp_path / ".test_durations").write_text(json.dumps({"tests/test_a.py::test_values[1]": 10}))
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "IMPORT_LOG": str(tmp_path / "imports.log"),
    }
    ids, imported = [], []
    for shard in (None, 1, 2):
        output = tmp_path / f"collected-{shard}.json"
        timing = tmp_path / f"timings-{shard}.json"
        (tmp_path / "imports.log").write_text("")
        args = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.ci_sharding",
            "tests/",
            "test_explicit.py",
            "-q",
            "-m",
            "not live_only",
            "--ignore-glob=tests/test_ignored.py",
            f"--ci-collection-output={output}",
            f"--ci-timing-output={timing}",
        ]
        if shard is not None:
            args += ["--ci-file-shards=2", f"--ci-file-shard={shard}"]
        if workers:
            args += ["-p", "xdist.plugin", "-n", str(workers), "--dist", "loadfile"]
        result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        ids.append(json.loads(output.read_text()))
        imported.append(set((tmp_path / "imports.log").read_text().splitlines()))
        assert set(json.loads(timing.read_text())) == set(ids[-1])
    assert len(ids[0]) == 12
    assert Counter(ids[1] + ids[2]) == Counter(ids[0])
    assert not imported[1] & imported[2]
    assert imported[1] | imported[2] == imported[0]
