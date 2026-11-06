"""Controlled B03 selection and correlated deployment checks, not live evidence."""

import json
import os
import subprocess
import sys
from pathlib import Path
from contextlib import contextmanager
from importlib import import_module
from types import SimpleNamespace

import pytest
import yaml

from tests.e2e.product_onboarding.product_v04_selection import (
    main,
    require_v04_inventory,
    selectors,
)

ROOT = Path(__file__).resolve().parents[3]
BROWSER = ROOT / ".github/workflows/e2e-new-ui-playwright.yml"
DEPLOY = ROOT / ".github/workflows/gateway-frontend-deploy.yml"
B03 = "live_scope_isolation,live_scope_denials,live_scope_history_cleanup"


def run_browser_selection(
    tmp_path,
    *,
    source="a" * 40,
    preview="on",
    b03="true",
    v11="false",
    release="17",
    attempt="1",
    workflow="b" * 40,
):
    steps = yaml.safe_load(BROWSER.read_text())["jobs"]["e2e-new-ui"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "browser-run")
    executable = tmp_path / "python"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:3] == ['-m', 'pytest']:\n"
        "    Path('pytest-arguments.json').write_text(json.dumps(sys.argv[3:]))\n"
        "else:\n"
        "    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
    )
    executable.chmod(0o700)
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PYTHONPATH": str(ROOT),
            "PRODUCT_B03_MODE": b03,
            "PRODUCT_V11_MODE": v11,
            "PRODUCT_EXPECTED_REVISION": source,
            "E2E_NEW_UI_EXPECTED_FLAG": preview,
            "PRODUCT_RELEASE_RUN_ID": release,
            "PRODUCT_RELEASE_ATTEMPT": attempt,
            "PRODUCT_RELEASE_WORKFLOW_REVISION": workflow,
        },
    )
    arguments = tmp_path / "pytest-arguments.json"
    selected = json.loads(arguments.read_text()) if arguments.exists() else []
    return result, selected[: selected.index("-p")] if selected else []


def test_b03_selects_only_real_api_and_browser_routes(tmp_path):
    result, selected = run_browser_selection(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert selected == list(selectors())


@pytest.mark.parametrize(
    "updates",
    [
        {"source": "unknown"},
        {"preview": "off"},
        {"release": ""},
        {"attempt": ""},
        {"workflow": "wrong"},
        {"v11": "true"},
    ],
)
def test_b03_does_not_run_against_an_unmatched_release(tmp_path, updates):
    result, selected = run_browser_selection(tmp_path, **updates)
    assert result.returncode != 0
    assert selected == []


@pytest.mark.parametrize(
    "inventory",
    [
        [],
        list(selectors())[:-1],
        list(reversed(selectors())),
        list(selectors()) + [selectors()[0]],
        list(selectors()[:-1]) + [selectors()[0]],
        None,
    ],
)
def test_b03_rejects_missing_reordered_duplicate_or_unrelated_cases(inventory):
    with pytest.raises(ValueError, match="B03 browser/API inventory"):
        require_v04_inventory(inventory)


def test_b03_collects_exact_real_routes(tmp_path):
    inventory = tmp_path / "inventory.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *selectors(),
            "-p",
            "regression_pytest",
            "--collect-only",
            "-q",
            "--disable-warnings",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "E2E_NEW_UI_ENABLED": "0",
            "REGRESSION_INVENTORY": str(inventory),
            "PYTHONPATH": f"{ROOT / '.github/scripts'}:{ROOT}",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert require_v04_inventory(json.loads(inventory.read_text())) == selectors()
    assert main(["--inventory", str(inventory)]) == 0
    assert "4 tests collected" in result.stdout


def test_b03_fixed_engine_and_correlated_workflow_binding():
    from pydantic import ValidationError, TypeAdapter
    from src.orchestration.product_case_selection import (
        PRODUCT_CASE_BINDINGS,
        ProductCases,
    )

    deploy = yaml.safe_load(DEPLOY.read_text())
    browser = yaml.safe_load(BROWSER.read_text())
    assert PRODUCT_CASE_BINDINGS[B03] == (
        "gateway-frontend",
        "gateway-frontend-deploy.yml",
    )
    assert (
        B03 in deploy[True]["workflow_dispatch"]["inputs"]["product_cases"]["options"]
    )
    assert (
        deploy["jobs"]["product-browser"]["with"]["product_scope"]
        == "${{ inputs.product_cases == '" + B03 + "' }}"
    )
    assert (
        deploy["jobs"]["product-browser"]["with"]["product_release_run_id"]
        == "${{ github.run_id }}"
    )
    assert (
        deploy["jobs"]["product-browser"]["with"]["product_release_attempt"]
        == "${{ github.run_attempt }}"
    )
    assert (
        deploy["jobs"]["product-browser"]["with"]["product_release_workflow_revision"]
        == "${{ github.workflow_sha }}"
    )
    assert browser[True]["workflow_call"]["inputs"]["product_scope"]["default"] is False
    assert "product_scope" not in browser[True]["workflow_dispatch"]["inputs"]
    assert "PRODUCT_B03_MODE" in browser["jobs"]["e2e-new-ui"]["env"]
    checkout = next(
        step
        for step in browser["jobs"]["e2e-new-ui"]["steps"]
        if step.get("name") == "Checkout"
    )
    assert (
        checkout["with"]["ref"] == "${{ github.sha }}"
    )
    assert checkout["with"]["persist-credentials"] is False
    ancestor = next(
        step
        for step in browser["jobs"]["e2e-new-ui"]["steps"]
        if step.get("name") == "Require selected Product source on reviewed main"
    )
    assert 'git merge-base --is-ancestor "$PRODUCT_EXPECTED_REVISION" HEAD' in ancestor["run"]
    assert "!inputs.product_scope" in next(
        step["if"]
        for step in browser["jobs"]["e2e-new-ui"]["steps"]
        if step.get("id") == "bind-product"
    )
    steps = browser["jobs"]["e2e-new-ui"]["steps"]
    binder = next(step for step in steps if step.get("id") == "b03-bind")
    gate = next(
        step
        for step in steps
        if step.get("name") == "Expose B03 only after complete private publication"
    )
    assert (
        steps.index(binder)
        < next(
            index
            for index, step in enumerate(steps)
            if step.get("id") == "upload-results"
        )
        < steps.index(gate)
    )
    assert (
        "product_v04_publish" in binder["run"]
        and "inputs.product_scope" in binder["if"]
    )
    assert (
        "review_status" in gate["run"]
        and "manifest-version" in gate["env"]["B03_MANIFEST_VERSION"]
    )
    assert TypeAdapter(ProductCases).validate_python(B03) == B03
    with pytest.raises(ValidationError):
        TypeAdapter(ProductCases).validate_python(B03 + ",unrelated")


def test_b03_scope_fixture_invokes_protected_producer_and_restores_on_exit(
    tmp_path, monkeypatch
):
    provider_module = import_module("tests.e2e.new_ui.product_scope_fixture")
    fixture_module = import_module("tests.e2e.new_ui.conftest")
    calls = []
    provider = SimpleNamespace(
        state=SimpleNamespace(binding={"run_id": "product-4-1"}),
        request=object(),
        base_url="https://example.invalid",
        owner={"actor_id": "owner"},
        run_id="product-4-1",
        binding={"run_id": "product-4-1"},
        scope_inputs=[{"selection": 1}],
    )

    @contextmanager
    def owned_fixture(case, record):
        calls.append(("open", case))
        try:
            yield {"product_id": "owned", "project_id": "first"}
        finally:
            calls.append(("cleanup", case))

    provider.fixture = owned_fixture
    monkeypatch.setenv("PRODUCT_B03_MODE", "true")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "test-results").mkdir()

    def produce(*args):
        assert (
            args[0] is provider.state
            and args[4]["product_name"] == "eval-product-product-4-1-scope"
        )
        assert args[5] == provider.scope_inputs
        calls.append(("produce", "scope"))

    def discover(*args):
        assert args[1] == provider.base_url
        assert args[3] == {"product_id": "owned", "project_id": "first"}
        assert args[4] == provider.scope_inputs
        calls.append(("discover", "scope"))

    monkeypatch.setattr(provider_module, "prepare_scope_fixture", produce)
    monkeypatch.setattr(provider_module, "verify_scope_sources", discover)
    monkeypatch.setattr(
        provider_module,
        "load_scope_fixture",
        lambda path, binding: {"product_id": "owned"},
    )
    request = SimpleNamespace(
        config=SimpleNamespace(
            product_scope_evidence=SimpleNamespace(bind=lambda owned: None)
        )
    )
    iterator = fixture_module.product_scope_fixture.__wrapped__(provider, request)
    assert next(iterator) == {"product_id": "owned"}
    with pytest.raises(StopIteration):
        next(iterator)
    assert calls == [("open", "scope"), ("discover", "scope"), ("produce", "scope"), ("cleanup", "scope")]


@pytest.mark.parametrize(
    "selection,environment,correlation,expected",
    [
        (B03, "dev", "c" * 64, True),
        (B03, "prod", "c" * 64, False),
        (B03, "dev", "", False),
        (B03 + ",unrelated", "dev", "c" * 64, False),
        (
            "live_browser_create,live_browser_conflict_denial,live_browser_evidence",
            "dev",
            "c" * 64,
            True,
        ),
        ("none", "dev", "", True),
    ],
)
def test_frontend_guard_admits_only_fixed_correlated_inventory(
    selection, environment, correlation, expected
):
    steps = yaml.safe_load(DEPLOY.read_text())["jobs"]["publish"]["steps"]
    script = next(
        step["run"]
        for step in steps
        if step["name"] == "Guard engine delivery revisions"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ADP_CORRELATION": correlation,
            "ADP_SOURCE": "a" * 40 if correlation else "",
            "ADP_DEFINITION": "b" * 40 if correlation else "",
            "ACTUAL_DEFINITION": "b" * 40,
            "INPUT_PRODUCT_CASES": selection,
            "INPUT_ENVIRONMENT": environment,
        },
    )
    assert (result.returncode == 0) is expected


def test_b03_selector_command_and_missing_inventory(tmp_path, capsys):
    assert main(["--selectors"]) == 0
    assert capsys.readouterr().out.splitlines() == list(selectors())
    with pytest.raises(FileNotFoundError):
        main(["--inventory", str(tmp_path / "absent.json")])
