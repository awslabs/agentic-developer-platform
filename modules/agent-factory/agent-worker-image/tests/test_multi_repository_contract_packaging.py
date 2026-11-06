"""Verify the shipped worker artifact reads both shared contract versions."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def test_worker_selfcheck_from_isolated_artifact(tmp_path):
    stage = tmp_path / "app"
    (stage / "lib").mkdir(parents=True)
    for name in ("contract_selfcheck.py", "review_result.py"):
        shutil.copyfile(
            ROOT / "modules/agent-factory/agent-worker-image/lib" / name, stage / "lib" / name
        )
    for name in ("orchestration-review", "multi-repository-evaluation"):
        shutil.copytree(ROOT / "contracts" / name, stage / "contracts" / name)
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "TMPDIR", "LD_LIBRARY_PATH"}
    }
    env["PYTHONPATH"] = str(stage)
    command = [sys.executable, "-m", "lib.contract_selfcheck"]

    def check():
        return subprocess.run(command, cwd=stage, env=env, capture_output=True, text=True)

    result = check()
    assert result.returncode == 0, result.stderr
    fixture_path = stage / "contracts/multi-repository-evaluation/v1/evaluation-receipt.golden.json"
    fixture = json.loads(fixture_path.read_text())
    fixture["expected_specification_digest"] = "0" * 64
    fixture_path.write_text(json.dumps(fixture))
    result = check()
    assert result.returncode != 0 and "specification digest drift" in result.stderr
    fixture_path.unlink()
    result = check()
    assert (
        result.returncode != 0
        and "does not carry the complete multi-repository-evaluation" in result.stderr
    )
