"""The pinned Transform release keeps a fail-closed installed readback entry point.

The release publishes immutable executor images and verifies their installation
only in a separate readback. Operations runs that readback through the script's
command line, so its refusal path belongs to the release contract: an unknown
mode, an unpinned revision or another service's descriptor must fail without
leaving a receipt that a later rollup could read as installed evidence.
"""

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
PUBLISHER = ROOT / "modules/L5-services/scripts/release-validation-images.py"
REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
REFUSAL = "Validation image release or installed readback failed"
IMAGES = {
    "service": REGISTRY + "/validation-service@sha256:" + "e" * 64,
    "checks": REGISTRY + "/validation-check@sha256:" + "f" * 64,
}


def readback(arguments, directory):
    return subprocess.run(
        [sys.executable, str(PUBLISHER), *arguments],
        cwd=str(directory), capture_output=True, text=True, timeout=120,
    )


def descriptor(directory, **changes):
    manifest = directory / "validation-images.json"
    manifest.write_text(json.dumps({
        "schema_version": 1, "phase": "artifacts", "service": "transform",
        "source_revision": "a" * 40, "run_id": 456, "images": IMAGES, **changes,
    }))
    return manifest


def test_unknown_release_mode_is_refused_without_a_receipt(tmp_path):
    receipt = tmp_path / "installed-images-receipt.json"
    result = readback(["--qualified", str(descriptor(tmp_path)), str(receipt)], tmp_path)
    assert result.returncode != 0
    assert REFUSAL in result.stderr
    assert not receipt.exists()


def test_installed_readback_refuses_an_unpinned_source_revision(tmp_path):
    receipt = tmp_path / "installed-images-receipt.json"
    manifest = descriptor(tmp_path, source_revision="main")
    result = readback(["--installed", str(manifest), str(receipt)], tmp_path)
    assert result.returncode != 0
    assert REFUSAL in result.stderr
    assert not receipt.exists()


def test_installed_readback_refuses_another_service_descriptor(tmp_path):
    receipt = tmp_path / "installed-images-receipt.json"
    manifest = descriptor(tmp_path, service="security")
    result = readback(["--installed", str(manifest), str(receipt)], tmp_path)
    assert result.returncode != 0
    assert REFUSAL in result.stderr
    assert not receipt.exists()


def test_installed_readback_refuses_an_unpublished_phase(tmp_path):
    receipt = tmp_path / "installed-images-receipt.json"
    manifest = descriptor(tmp_path, phase="installed-readback")
    result = readback(["--installed", str(manifest), str(receipt)], tmp_path)
    assert result.returncode != 0
    assert REFUSAL in result.stderr
    assert not receipt.exists()
