"""Run the A1/A2/B1 synthetic fixture against the unchanged sandbox launcher."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_consecutive_turns_create_separate_pods_and_discard_mock_canaries():
    result = subprocess.run(
        ["node", "-r", str(ROOT / "agent/node_modules/ts-node/register"),
         str(ROOT / "tests/k8s/chat_warm_isolation.cjs")],
        cwd=ROOT / "agent", capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
