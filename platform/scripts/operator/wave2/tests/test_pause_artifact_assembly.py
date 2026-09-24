"""Exercise the runtime JSON → assembler CLI → expiry artifact handoff."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "21-assemble-pause-artifacts.py"


@pytest.mark.parametrize("annotation_count", [0, 1])
def test_expiry_observations_survive_cli(tmp_path, annotation_count):
    measured = {
        "auto_resumed": True,
        "annotation_count": annotation_count,
        "extra_assistant_turn": None,
        "neutral_annotation": False,
        "resolved_before_release": True,
        "pod_killed": None,
        "idle_retry_fired": False,
        "exit_watchdog_fired": False,
        "heartbeats_during_pause": 2,
        "paused_distinguishable_from_stalled": True,
        "spill_output_preserved": True,
        "held_hook_timeout": {"state": "running", "safety_release_used": False},
        "deadline_clamp": {"granted_ms": 240000},
        "cancellation": {"held_work_admitted": False},
        "missing_launcher_inputs": ["pod_killed"],
    }
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps({
        "sdk_version": "0.3.220",
        "reports": [{"name": "expiry observation", "ok": False}],
        "pause_expiry": measured,
    }))
    output = tmp_path / "artifacts"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--raw", str(raw), "--out-dir", str(output)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1  # incomplete observations remain incomplete
    artifact = json.loads((output / "pause_expiry.json").read_text())
    provenance = artifact.pop("_provenance")
    assert artifact == measured
    assert provenance["experiments_passed"] == 0
    assert "pause_expiry.pod_killed" in result.stderr
    assert "pause_expiry.extra_assistant_turn" in result.stderr
