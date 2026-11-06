"""Select one of two image-pinned dependency versions before test collection."""
import os
from pathlib import Path
import sys
import tomllib


def pytest_configure(config):
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    requirement, = project["dependencies"]
    allowed = {"packaging==24.2": "24.2", "packaging==25.0": "25.0"}
    if requirement not in allowed:
        raise ValueError("fixture dependency must match a selected pinned version")
    directory = Path(os.environ.get("TRANSFORM_FIXTURE_DEPENDENCIES", "/opt/transform-check-dependencies")) / ("packaging-" + allowed[requirement])
    if not (directory / "packaging/__init__.py").is_file():
        raise ValueError("selected dependency is absent from the offline check image")
    for key in list(sys.modules):
        if key == "packaging" or key.startswith("packaging."):
            del sys.modules[key]
    sys.path.insert(0, str(directory))
