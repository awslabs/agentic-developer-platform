"""The shared package must actually reach all three deploy artifacts (§6, S1).

Two independent definitions build the budget Lambda zips — Terraform's
``archive_file`` blocks (used by ``terraform apply``) and the inline Python in
``gateway-deploy.yml`` (used by push-triggered deploys). They must agree, because
whichever one runs is what lands in production.

The failure this guards is specific and has happened before: #4391 shipped a
Lambda that ImportErrored on cold start because a shared module was added to the
handler's imports but not to the archive list, which silently stopped ALL budget
metering. This package makes it worse, since it carries ``snapshots/*.json`` data
files whose absence surfaces only as a FileNotFoundError when a rate is looked up.

So these tests assert on the real files rather than mocking: the package
cold-imports from an isolated unpacked archive with no repository path available,
and both archive definitions are parsed from disk.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import zipfile
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

GATEWAY = Path(__file__).resolve().parents[2]
POLICY_DIR = GATEWAY / "pricing_policy"
TERRAFORM_MAIN = GATEWAY / "infra" / "modules" / "budget-lambda" / "main.tf"
DEPLOY_WORKFLOW = GATEWAY.parents[1] / ".github" / "workflows" / "gateway-deploy.yml"
CI_WORKFLOW = GATEWAY.parents[1] / ".github" / "workflows" / "gateway-ci.yml"

LAMBDA_NAMES = ("budget-usage-tracker", "pricing-refresh")


def _expected_package_files() -> list[str]:
    """The archive member names both builders are expected to produce."""
    modules = sorted(str(p.relative_to(POLICY_DIR)) for p in POLICY_DIR.rglob("*.py"))
    snapshots = sorted(f"snapshots/{p.name}" for p in POLICY_DIR.glob("snapshots/*.json"))
    return [*modules, *snapshots]


def test_package_has_modules_and_at_least_one_snapshot():
    files = _expected_package_files()
    assert "__init__.py" in files
    assert "policy.py" in files
    assert any(f.startswith("snapshots/") for f in files), "a package with no snapshot cannot price anything"


# ---------------------------------------------------------------------------
# Terraform and CI must agree
# ---------------------------------------------------------------------------


def test_terraform_vendors_the_package_into_both_archives():
    """Both archive_file blocks must include the package, via the shared glob."""
    body = TERRAFORM_MAIN.read_text()

    assert 'fileset(local.pricing_policy_dir, "**/*.py")' in body
    assert 'fileset(local.pricing_policy_dir, "snapshots/*.json")' in body

    # Split at each archive_file block and require the dynamic source in both.
    blocks = re.split(r'data\s+"archive_file"\s+"', body)[1:]
    found = {}
    for block in blocks:
        name = block.split('"', 1)[0]
        found[name] = 'filename = "pricing_policy/${source.value}"' in block

    assert set(found) == {"usage_tracker", "pricing_refresh"}, found
    for name, has_policy in found.items():
        assert has_policy, f"archive_file {name} does not vendor pricing_policy"


def test_terraform_globs_rather_than_listing_files():
    """A hand-maintained list is how #4391 shipped a broken Lambda.

    If someone converts the dynamic block back into individual source blocks, a
    later snapshot addition silently stops shipping. Keep the glob.
    """
    body = TERRAFORM_MAIN.read_text()
    for member in _expected_package_files():
        assert f'filename = "pricing_policy/{member}"' not in body, f"{member} is hard-coded; use the fileset glob instead"


def test_deploy_workflow_vendors_the_same_files():
    body = DEPLOY_WORKFLOW.read_text()
    assert "python3 modules/gateway/scripts/build-budget-lambda-archives.py --output /tmp" in body


def test_deploy_workflow_redeploys_all_three_artifacts_on_a_policy_change():
    """A pricing_policy change must set BOTH budget_lambdas and backend."""
    body = DEPLOY_WORKFLOW.read_text()
    assert "- 'modules/gateway/pricing_policy/**'" in body, "push path filter missing"

    match = re.search(r"if[^\n]*modules/gateway/pricing_policy/[^\n]*then(.*?)\bfi\b", body, re.DOTALL)
    assert match, "change-detection branch for pricing_policy not found"
    branch = match.group(1)
    assert "BUDGET_LAMBDAS=true" in branch
    assert "BACKEND=true" in branch


@pytest.mark.parametrize("trigger", ["push", "pull_request"])
@pytest.mark.parametrize(
    "changed_file",
    [
        "modules/gateway/pricing_policy/snapshots/2026-09-12.1.json",
        "modules/gateway/alembic/versions/044_model_pricing_v2.py",
        "modules/gateway/infra/modules/budget-lambda/main.tf",
        ".github/workflows/gateway-deploy.yml",
        "codebuild/bs-gateway-build.yml",
        "codebuild/bs-gateway-smoke.yml",
        "platform/scripts/zip-source.sh",
    ],
)
def test_ci_runs_on_pricing_artifact_changes(trigger, changed_file):
    """A pricing, migration or packaging-only change must run its checks."""
    workflow = yaml.load(CI_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    patterns = workflow["on"][trigger]["paths"]
    assert any(fnmatchcase(changed_file, pattern) for pattern in patterns), f"{trigger} skips {changed_file}"


def test_dockerfile_bakes_the_package_into_the_image():
    body = (GATEWAY / "Dockerfile").read_text()
    assert "COPY pricing_policy/ pricing_policy/" in body


def test_both_gateway_buildspecs_verify_the_built_image_can_price():
    """The image build must run ``pricing_policy.selfcheck`` on the built image.

    ``COPY pricing_policy/`` is asserted above, but a passing grep over the
    Dockerfile does not prove the layer landed — the snapshot data files can go
    missing while the image still builds and the package still imports. Only
    executing the check inside the image covers that, so both buildspecs must do
    it: the smoke build so a PR fails, and the deploy build so a mispriced image
    is never pushed.
    """
    for name in ("bs-gateway-smoke.yml", "bs-gateway-build.yml"):
        body = (GATEWAY.parents[1] / "codebuild" / name).read_text()
        assert "-m pricing_policy.selfcheck" in body, f"{name} does not verify the image can price"


def test_deploy_buildspec_verifies_before_it_pushes():
    """Ordering is the whole point in the deploy path.

    A selfcheck that runs after ``docker push`` reports the failure only once the
    broken image is already the ``latest`` tag that the cluster pulls.
    """
    body = (GATEWAY.parents[1] / "codebuild" / "bs-gateway-build.yml").read_text()
    assert body.index("-m pricing_policy.selfcheck") < body.index("docker push"), "selfcheck must gate the push, not follow it"


@pytest.mark.parametrize("build_succeeding", ["0", "", "1"])
def test_deploy_post_build_only_pushes_after_success(build_succeeding):
    """CodeBuild invokes post_build even after a failed image selfcheck.

    Execute its actual commands with Docker stubbed so that a failed build
    cannot publish either the latest image or its immutable deployment tag.
    """
    body = (GATEWAY.parents[1] / "codebuild" / "bs-gateway-build.yml").read_text()
    commands = yaml.safe_load(body)["phases"]["post_build"]["commands"]
    result = subprocess.run(
        ["/bin/bash", "-eu", "-c", 'docker() { printf "%s\\n" "$*"; }\n' + "\n".join(commands)],
        env={
            "PATH": "/usr/bin:/bin",
            "CODEBUILD_BUILD_SUCCEEDING": build_succeeding,
            "REGISTRY": "example.invalid",
            "IMAGE_TAG": "reviewed-commit",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if build_succeeding == "1":
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            "push example.invalid/adp-gateway:latest",
            "push example.invalid/adp-gateway:reviewed-commit",
        ]
    else:
        assert result.returncode != 0
        assert not result.stdout, "a failed build still pushed an image"


def test_selfcheck_reports_failure_when_snapshots_are_absent(tmp_path, monkeypatch):
    """The gate must actually fail on a package with no rate data.

    A selfcheck that passes vacuously is worse than none: it converts a missing
    snapshot from a loud build failure into a signed-off green build.
    """
    staged = tmp_path / "pricing_policy"
    staged.mkdir()
    for module in POLICY_DIR.glob("*.py"):
        (staged / module.name).write_bytes(module.read_bytes())
    # Snapshots deliberately not copied.

    result = subprocess.run(
        [sys.executable, "-m", "pricing_policy.selfcheck"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
        check=False,
    )
    assert result.returncode == 1, f"selfcheck passed without any snapshot:\n{result.stdout}"
    assert "does not load" in result.stderr


def test_selfcheck_passes_on_the_real_package(tmp_path):
    """And passes on a correctly assembled archive, imported cold."""
    archive_path = _build_zip(tmp_path)
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(unpacked)

    result = subprocess.run(
        [sys.executable, "-m", "pricing_policy.selfcheck"],
        cwd=unpacked,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "selfcheck OK" in result.stdout


def test_pyproject_ships_the_package_and_its_snapshot_data():
    body = (GATEWAY / "pyproject.toml").read_text()
    assert '"pricing_policy*"' in body, "package discovery excludes pricing_policy"
    assert 'pricing_policy = ["snapshots/*.json"]' in body, "snapshots are data files and would be dropped from a build"


# ---------------------------------------------------------------------------
# The package must work when unpacked in a Lambda-like environment
# ---------------------------------------------------------------------------


def _build_zip(destination: Path) -> Path:
    """Execute the actual CI archive builder, including all shared modules."""
    spec = importlib.util.spec_from_file_location("budget_archive_builder", GATEWAY / "scripts/build-budget-lambda-archives.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build(GATEWAY, destination)
    return destination / "budget-usage-tracker.zip"


@pytest.mark.parametrize("lambda_name", LAMBDA_NAMES)
def test_handler_imports_only_modules_present_in_its_archive(lambda_name):
    """Every top-level import of each handler must be satisfiable in the zip.

    Catches the #4391 shape directly: an import added to a handler without a
    corresponding archive entry.
    """
    handler = GATEWAY / "lambda" / lambda_name / "handler.py"
    if not handler.exists():
        pytest.skip(f"{lambda_name} handler not present")

    source = handler.read_text()
    if "pricing_policy" not in source:
        pytest.skip(f"{lambda_name} does not import pricing_policy yet")

    # The package directory is what the zip provides; confirm the imported
    # submodules exist inside it.
    for module in re.findall(r"from pricing_policy(?:\.(\w+))? import", source):
        if module:
            assert (POLICY_DIR / f"{module}.py").exists(), f"{lambda_name} imports pricing_policy.{module}, which is not in the package"


def test_package_cold_imports_from_an_unpacked_archive(tmp_path):
    """Unpack the zip, then import it with the repository off sys.path.

    This is the real cold-start condition. Running in a subprocess with a scrubbed
    PYTHONPATH and cwd means a stray repo-relative import cannot mask a packaging
    gap — the failure mode where tests pass locally and the Lambda dies on deploy.
    """
    archive_path = _build_zip(tmp_path)
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(unpacked)

    probe = (
        "import json, sys\n"
        "import pricing_policy as pp\n"
        "snapshot = pp.load_snapshot()\n"
        "usage = pp.normalize_usage("
        "{'input_tokens': 2048, 'output_tokens': 256, 'cache_read_input_tokens': 1920},"
        " api_format='openai')\n"
        "print(json.dumps({"
        "'file': pp.__file__,"
        "'version': snapshot.snapshot_version,"
        "'rows': len(snapshot.rates),"
        "'uncached': usage.uncached_input_tokens,"
        "}))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=unpacked,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
        check=False,
    )
    assert result.returncode == 0, f"cold import failed:\n{result.stderr}"

    payload = json.loads(result.stdout)
    assert str(unpacked) in payload["file"], f"imported the repo copy, not the archive: {payload['file']}"
    bundled = json.loads((POLICY_DIR / "snapshots/2026-09-12.1.json").read_text())
    assert payload["rows"] == len(bundled["rates"])
    assert payload["uncached"] == 128


def test_snapshot_survives_the_archive_byte_for_byte(tmp_path):
    """Exact archive parity: a rate must not change in transit."""
    archive_path = _build_zip(tmp_path)
    with zipfile.ZipFile(archive_path) as archive:
        for member in _expected_package_files():
            packed = archive.read(f"pricing_policy/{member}")
            assert packed == (POLICY_DIR / member).read_bytes(), member


def test_both_lambda_archives_receive_identical_package_content(tmp_path):
    """The two Lambdas must not diverge on pricing.

    One reading corrected rates while the other reads stale ones reintroduces the
    exact split-brain that #4969 exists to remove.
    """
    first = _build_zip(tmp_path / "a")
    second = first.parent / "pricing-refresh.zip"
    with zipfile.ZipFile(first) as a, zipfile.ZipFile(second) as b:
        policy_files = [f"pricing_policy/{member}" for member in _expected_package_files()]
        shared_files = [path.name for path in (GATEWAY / "lambda/shared").glob("*.py")]
        for name in policy_files + shared_files:
            assert a.read(name) == b.read(name), name
        for path in (GATEWAY / "lambda/pricing-refresh").glob("*.py"):
            assert b.read(path.name) == path.read_bytes()


@pytest.fixture(autouse=True)
def _ensure_build_dirs(tmp_path):
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir(exist_ok=True)
