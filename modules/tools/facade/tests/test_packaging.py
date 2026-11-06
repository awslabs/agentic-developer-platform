import os
import shutil
import subprocess
import sys
import sysconfig
import textwrap
import venv
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("editable", [False, True], ids=["wheel", "editable"])
def test_installed_facade_imports_shared_validator_without_source_path(tmp_path, editable):
    projects = []
    for source, package in ((TOOLS, "adp_tools"), (TOOLS / "facade", "tools_facade")):
        project = tmp_path / package
        project.mkdir()
        shutil.copy2(source / "pyproject.toml", project)
        shutil.copytree(source / package, project / package, ignore=shutil.ignore_patterns("__pycache__"))
        projects.append(project)

    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    environment["AWS_EC2_METADATA_DISABLED"] = "true"

    def run(*command):
        completed = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120)
        assert completed.returncode == 0, completed.stdout + completed.stderr

    installation = tmp_path / "installed"
    venv.EnvBuilder(with_pip=True).create(installation)
    site_packages = next(installation.glob("lib/python*/site-packages"))
    (site_packages / "dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n")
    python = str(installation / "bin/python")
    if editable:
        packages = [argument for project in projects for argument in ("-e", str(project))]
    else:
        wheels = tmp_path / "wheels"
        run(sys.executable, "-I", "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
            "--no-index", "--wheel-dir", str(wheels), *(str(project) for project in projects))
        packages = [str(wheel) for wheel in wheels.glob("*.whl")]
        assert len(packages) == 2
    run(python, "-I", "-m", "pip", "install", "--no-deps", "--no-build-isolation", "--no-index",
        "--ignore-installed", *packages)
    run(python, "-I", "-c", textwrap.dedent("""
        import sys
        import sysconfig
        from importlib.metadata import requires
        from pathlib import Path

        import adp_tools.search_input
        import tools_facade.server
        from pydantic import ValidationError

        assert any(requirement.startswith("adp-tools-shared==") for requirement in requires("adp-tools-facade"))
        for module, project in ((adp_tools.search_input, "adp_tools"), (tools_facade.server, "tools_facade")):
            expected = Path.cwd() / project if sys.argv[1] == "True" else Path(sysconfig.get_path("purelib"))
            assert Path(module.__file__).is_relative_to(expected), module.__file__
        validator = adp_tools.search_input.SearchInput
        assert tools_facade.server.SearchInput is validator
        assert validator(query="bounded search").query == "bounded search"
        for invalid in (" ", "x" * 201):
            try:
                validator(query=invalid)
            except ValidationError:
                pass
            else:
                raise AssertionError("invalid query was accepted")
    """), str(editable))
