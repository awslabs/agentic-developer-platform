"""A staged Gateway must load the same cleanup implementation without a checkout."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from src.internal.domain_closed_cleanup_source import source_digest


def test_staged_native_source_is_complete_and_matches_checkout(tmp_path):
    gateway = Path(__file__).resolve().parents[2]
    subprocess.run([str(gateway / "scripts/stage-contracts.sh")], check=True, capture_output=True)
    shutil.copytree(gateway / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(gateway / "contracts", tmp_path / "contracts")
    script = tmp_path / "src/internal/domain_closed_cleanup_source.py"
    result = subprocess.run([sys.executable, "-I", "-B", str(script)], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout)["service_source_sha256"] == source_digest()
    (tmp_path / "contracts/superplane-bootstrap/superplane_bootstrap/closed_cleanup_run.py").unlink()
    refused = subprocess.run([sys.executable, "-I", "-B", str(script)], capture_output=True, text=True)
    assert refused.returncode != 0
