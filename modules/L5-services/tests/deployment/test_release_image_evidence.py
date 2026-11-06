"""Shared releases retain executor-image evidence for every selected service.

The shared L5 artifact workflow retains one evidence set per publication and
treats a missing evidence path as an error. Only the Transform release
publishes executor images, so every other service has to record an explicit
empty image disposition next to its release descriptor; otherwise the strict
retention step discards the whole release evidence set for that service.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load("release_context", "release_context.py")
artifacts = load("l5_release_artifacts_images", "release-artifacts.py")

RESULT = {
    "schema_version": 1,
    "phase": "artifacts",
    "service": "finops",
    "source_revision": "a" * 40,
    "run_id": 456,
    "artifacts": {
        "wheel": {"key": "l5/security/x/y", "version_id": "v1", "sha256": "c" * 64},
        "archive": {"key": "l5/security/x/z", "version_id": "v2", "sha256": "d" * 64},
    },
}


@pytest.mark.parametrize("service", ["security", "devops", "finops", "well_architected"])
def test_shared_services_record_no_published_executor_image(service):
    assert artifacts.image_evidence({**RESULT, "service": service}) == {
        "schema_version": 1,
        "phase": "artifacts",
        "service": service,
        "source_revision": "a" * 40,
        "run_id": 456,
        "wheel_sha256": "c" * 64,
        "archive_sha256": "d" * 64,
        "images_published": False,
        "images": {},
    }


def test_transform_keeps_its_own_published_image_descriptor():
    assert artifacts.image_evidence({**RESULT, "service": "transform"}) is None


def test_image_evidence_is_written_privately_beside_the_release_descriptor(tmp_path):
    destination = tmp_path / "release-artifacts.json"
    images = artifacts.image_evidence(RESULT)
    artifacts.write_private(str(destination), RESULT)
    artifacts.write_private(str(destination.with_name("validation-images.json")), images)
    retained = tmp_path / "validation-images.json"
    assert json.loads(retained.read_bytes()) == images
    assert json.loads(destination.read_bytes()) == RESULT
    assert retained.stat().st_mode & 0o077 == 0
