"""Workspace-provisioning wheel must include every resource the Dockerfile asserts.

Issue #6028 (S21/S14 continuation): the executor Dockerfile's build-time assertion
checks three installed resources after ``pip install workspace-provisioning[worker]``:

  1. ``_data/workspaces/.terraform.lock.hcl`` — the Terraform dependency lock
  2. ``_data/workspaces/scripts/apply_workspace_plan.py`` — the apply-plan safety script
  3. ``_data/crds.yaml`` — the CRD manifest

The first CodeBuild run (36099419327) failed because ``.terraform.lock.hcl`` is a dotfile,
and the ``*`` glob in pyproject.toml's ``package-data`` does not match dotfiles in Python's
standard glob. The staging script copies it correctly; setuptools excludes it silently.

These tests build a real wheel from the staged vendor directory, install it into an
isolated temporary environment, and verify every asserted resource exists and carries the
correct source content. The lock-file test fails on the original ``_data/workspaces/*``
glob that omitted the explicit dotfile entry.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
import venv
from pathlib import Path

import pytest

MODULE_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = MODULE_ROOT / "pyproject.toml"
STAGE_SCRIPT = MODULE_ROOT / "src/superplane-api/scripts/stage-domain-auth.sh"
VENDOR_ROOT = MODULE_ROOT / "src/superplane-api/vendor/workspace-provisioning"

# Source locations that stage-domain-auth.sh copies into _data/.
SOURCE_LOCK = MODULE_ROOT / "infra/workspaces/.terraform.lock.hcl"
SOURCE_APPLY = MODULE_ROOT / "infra/workspaces/scripts/apply_workspace_plan.py"
SOURCE_CRDS = MODULE_ROOT / "src/superplane-controller/deploy/crds.yaml"

# The three resources the Dockerfile asserts (executor/Dockerfile line 49).
ASSERTED_RESOURCES = (
    "workspace_provisioning/_data/workspaces/.terraform.lock.hcl",
    "workspace_provisioning/_data/workspaces/scripts/apply_workspace_plan.py",
    "workspace_provisioning/_data/crds.yaml",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="module")
def staged_vendor() -> Path:
    """Run the staging script once for the module, returning the vendor directory."""
    result = subprocess.run(
        ["bash", str(STAGE_SCRIPT)],
        cwd=str(MODULE_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"stage-domain-auth.sh failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert VENDOR_ROOT.is_dir(), f"staging did not create {VENDOR_ROOT}"
    return VENDOR_ROOT


@pytest.fixture(scope="module")
def built_wheel(staged_vendor: Path) -> Path:
    """Build a wheel from the staged workspace-provisioning package."""
    with tempfile.TemporaryDirectory(prefix="wp-wheel-") as wheel_dir:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--no-index",
                "--wheel-dir",
                wheel_dir,
                ".",
            ],
            cwd=str(staged_vendor),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"wheel build failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        wheels = list(Path(wheel_dir).glob("workspace_provisioning-*.whl"))
        assert len(wheels) == 1, f"expected 1 wheel, found {len(wheels)}: {wheels}"
        yield wheels[0]


@pytest.fixture(scope="module")
def wheel_contents(built_wheel: Path) -> dict[str, bytes]:
    """Extract every file in the wheel into a name-to-bytes mapping."""
    contents: dict[str, bytes] = {}
    with zipfile.ZipFile(built_wheel) as zf:
        for name in zf.namelist():
            contents[name] = zf.read(name)
    return contents


class TestDockerfileAssertedResources:
    """Every resource the Dockerfile's build-time assertion checks must be in the wheel."""

    @pytest.mark.parametrize("resource", ASSERTED_RESOURCES)
    def test_asserted_resource_is_present_in_wheel(
        self, wheel_contents: dict[str, bytes], resource: str
    ) -> None:
        assert resource in wheel_contents, (
            f"{resource} is missing from the built wheel; "
            f"pyproject.toml package-data may not match this file"
        )
        assert len(wheel_contents[resource]) > 0, (
            f"{resource} is in the wheel but empty"
        )


@pytest.mark.parametrize(
    "relative",
    (
        "member_credentials/__init__.py",
        "member_credentials/issuer.py",
        "member_credentials/projection.py",
        "credential_controller/__init__.py",
        "credential_controller/__main__.py",
        "credential_controller/registry.py",
        "credential_controller/renewal.py",
        "credential_controller/transports.py",
    ),
)
def test_installed_credential_packages_are_in_the_built_wheel(wheel_contents, relative):
    path = "workspace_provisioning/" + relative
    assert wheel_contents.get(path) == (MODULE_ROOT / path).read_bytes()
    assert not any(
        name.startswith("workspace_provisioning/tests/") for name in wheel_contents
    )


class TestWheelResourceContentIntegrity:
    """Packaged resources must match the maintained source byte-for-byte."""

    def test_terraform_lock_matches_source(
        self, wheel_contents: dict[str, bytes]
    ) -> None:
        resource = "workspace_provisioning/_data/workspaces/.terraform.lock.hcl"
        assert resource in wheel_contents, f"{resource} missing from wheel"
        assert SOURCE_LOCK.is_file(), f"source lock not found at {SOURCE_LOCK}"
        assert _sha256(wheel_contents[resource]) == _sha256(SOURCE_LOCK.read_bytes()), (
            ".terraform.lock.hcl in the wheel differs from the maintained source at "
            f"{SOURCE_LOCK}"
        )

    def test_apply_plan_script_matches_source(
        self, wheel_contents: dict[str, bytes]
    ) -> None:
        resource = (
            "workspace_provisioning/_data/workspaces/scripts/apply_workspace_plan.py"
        )
        assert resource in wheel_contents, f"{resource} missing from wheel"
        assert SOURCE_APPLY.is_file(), f"source script not found at {SOURCE_APPLY}"
        assert _sha256(wheel_contents[resource]) == _sha256(
            SOURCE_APPLY.read_bytes()
        ), (
            "apply_workspace_plan.py in the wheel differs from the maintained source at "
            f"{SOURCE_APPLY}"
        )

    def test_crds_manifest_matches_source(
        self, wheel_contents: dict[str, bytes]
    ) -> None:
        resource = "workspace_provisioning/_data/crds.yaml"
        assert resource in wheel_contents, f"{resource} missing from wheel"
        assert SOURCE_CRDS.is_file(), f"source CRDs not found at {SOURCE_CRDS}"
        assert _sha256(wheel_contents[resource]) == _sha256(SOURCE_CRDS.read_bytes()), (
            "crds.yaml in the wheel differs from the maintained source at "
            f"{SOURCE_CRDS}"
        )


class TestDotfileGlobRegression:
    """Guard against the specific failure: ``*`` not matching dotfiles.

    The pyproject.toml must explicitly name ``.terraform.lock.hcl`` because
    setuptools' ``*`` glob does not match files starting with ``.``. If the
    explicit entry is removed, this test and
    ``test_asserted_resource_is_present_in_wheel[../.terraform.lock.hcl]``
    must both fail.
    """

    def test_pyproject_explicitly_names_the_terraform_lock(self) -> None:
        text = PYPROJECT.read_text(encoding="utf-8")
        assert ".terraform.lock.hcl" in text, (
            "pyproject.toml does not explicitly name .terraform.lock.hcl in "
            "package-data; the * glob does not match dotfiles and the file "
            "will be silently excluded from the wheel"
        )

    def test_staged_lock_exists_before_wheel_build(self, staged_vendor: Path) -> None:
        """The staging script must place the lock file; the wheel build must keep it."""
        lock = (
            staged_vendor
            / "workspace_provisioning/_data/workspaces/.terraform.lock.hcl"
        )
        assert lock.is_file(), (
            "stage-domain-auth.sh did not create .terraform.lock.hcl in the "
            "staged vendor directory"
        )


@pytest.fixture(scope="module")
def installed_resources(built_wheel: Path) -> dict[str, str]:
    """Install the built artifact without dependency/network/source fallback."""
    with tempfile.TemporaryDirectory(prefix="wp-installed-") as directory:
        root = Path(directory)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / "bin/python"
        install = subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                str(built_wheel),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert install.returncode == 0, f"wheel install failed: {install.stderr}"
        # -I ignores PYTHONPATH/user site and the current directory. Both cwd and
        # sys.prefix are outside the source tree; assert provenance as well as bytes.
        script = """
import hashlib, importlib.resources, json, pathlib, sys
import workspace_provisioning
origin = pathlib.Path(workspace_provisioning.__file__).resolve()
assert origin.is_relative_to(pathlib.Path(sys.prefix).resolve()), 'source-tree import'
root = importlib.resources.files('workspace_provisioning')
resources = json.loads(sys.argv[1])
result = {}
for resource in resources:
    item = root.joinpath(resource.removeprefix('workspace_provisioning/'))
    assert item.is_file(), 'installed wheel resource missing: ' + resource
    data = item.read_bytes()
    assert data, 'installed wheel resource empty: ' + resource
    result[resource] = hashlib.sha256(data).hexdigest()
print(json.dumps(result))
"""
        probe = subprocess.run(
            [str(python), "-I", "-c", script, json.dumps(ASSERTED_RESOURCES)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert probe.returncode == 0, f"installed resource probe failed: {probe.stderr}"
        yield json.loads(probe.stdout)


@pytest.mark.parametrize(
    "resource,source",
    tuple(
        zip(ASSERTED_RESOURCES, (SOURCE_LOCK, SOURCE_APPLY, SOURCE_CRDS), strict=True)
    ),
)
def test_installed_wheel_resources_match_maintained_source(
    installed_resources, resource, source
):
    assert installed_resources[resource] == _sha256(source.read_bytes())
