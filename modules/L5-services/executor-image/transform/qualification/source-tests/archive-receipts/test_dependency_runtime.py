"""Exercise the actual dependency selected by the pinned source manifest."""
from pathlib import Path
import tomllib
import packaging
from packaging.version import InvalidVersion, Version
import pytest


def test_runtime_matches_exact_manifest_dependency():
    declared, = tomllib.loads(Path("pyproject.toml").read_text())["project"]["dependencies"]
    assert declared == "packaging==" + packaging.__version__


def test_release_order_and_normalization():
    assert Version("1.0rc1") < Version("1.0") < Version("1.1")
    assert str(Version("v1.0.0")) == "1.0.0"


def test_invalid_version_is_rejected():
    with pytest.raises(InvalidVersion):
        Version("not a version")
