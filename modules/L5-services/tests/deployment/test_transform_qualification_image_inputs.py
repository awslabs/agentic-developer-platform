"""Keep the reviewed offline qualification bundle and image policy coherent."""

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "executor-image/transform/qualification"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_policy_pins_actual_reviewed_bundle_and_offline_dependencies():
    policy = json.loads((ROOT / "check-policy.json").read_text())
    bundle = json.loads((ROOT / "fixture-bundle.json").read_text())
    assert policy["bundle_sha256"] == digest(ROOT / "fixture-bundle.json")
    assert policy["lock_sha256"] == digest(ROOT / "wheelhouse-lock.json")
    for name, sha in bundle["image_files"].items():
        assert digest(ROOT / name) == sha
    for project, files in bundle["source_tests"].items():
        for name, sha in files.items():
            assert digest(ROOT / "source-tests" / project / name) == sha


def test_offline_wheels_have_real_hashes_and_cover_all_pinned_requirements():
    wheels = json.loads((ROOT / "wheelhouse-lock.json").read_text())["wheels"]
    sums = (ROOT / "wheelhouse.sha256").read_text()
    requirements = (ROOT / "requirements.lock").read_text()
    assert len(wheels) == len({item["filename"] for item in wheels}) == 6
    for item in wheels:
        assert re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
        assert item["sha256"] + "  " + item["filename"] + "\n" in sums
        assert item["filename"].endswith("py3-none-any.whl")
        assert item["url"].startswith("https://files.pythonhosted.org/")
    installed = {(item["name"], item["version"]): item for item in wheels}
    for name, version in re.findall(r"^([a-zA-Z0-9_-]+)==([^\s]+)", requirements, re.MULTILINE):
        assert installed[name, version]["sha256"] in requirements
    assert {("packaging", "24.2"), ("packaging", "25.0")} <= set(installed)


def test_base_recipe_uses_reviewed_single_manifest_toolchains_and_offline_check_install():
    base = (ROOT / "Dockerfile.base").read_text()
    assert len(re.findall(r"^FROM docker.io/library/[^\s]+@sha256:[a-f0-9]{64}", base, re.MULTILINE)) == 2
    assert "apt-get" not in base
    check = (ROOT / "Dockerfile.check-base").read_text()
    assert "sha256sum --check" in check
    assert all("--no-index" in command for command in check.split("&&") if "pip install" in command)
    policy = json.loads((ROOT / "check-policy.json").read_text())
    assert [command["minimum_test_count"] for command in policy["commands"]] == [6, 3]
    assert "--test-reporter=tap" in policy["commands"][1]["argv"]
