"""Headless transport coverage cannot be confused with full UI acceptance."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from journey_driver import HEADLESS_ENTRIES, validate_driver
from installed_journeys import validate_runs
import test_installed_journeys as installed_fixtures
import test_selector as selector_fixtures

installed_journeys = installed_fixtures.installed_journeys
selector_case = selector_fixtures.selector_case

SPEC = importlib.util.spec_from_file_location("devops_headless", Path(__file__).resolve().parents[3] / "scripts/qualify-devops-headless.py")
headless = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(headless)


@pytest.fixture
def headless_case(installed_journeys, monkeypatch):
    manifest, path, directory, calls, provider_calls, runs, tasks = installed_journeys
    manifest["authorization"]["scenarios"] = ["headless"]
    manifest["installed_journeys"]["runs"].pop("ui")
    manifest.pop("readback_channels")
    manifest.pop("ingress_policy")
    for key in ("browser_storage_state", "provider_hostname"):
        manifest["journey_driver"].pop(key)
    manifest["journey_driver"]["inputs"].pop("ui")
    path.write_text(json.dumps(manifest))
    from journey_driver import FirstPartyDriver
    monkeypatch.setattr(FirstPartyDriver, "run_ui", lambda *args: pytest.fail("browser must never start"))
    return manifest, path, directory, calls, provider_calls, runs, tasks


def test_headless_reads_existing_product_and_native_results_without_actions_or_ui(headless_case):
    manifest, path, directory, calls, provider_calls, _, _ = headless_case
    source = manifest["release"]["source_sha"]
    assert headless.preflight(source, path, directory) == manifest
    assert list(directory.iterdir()) == []
    assert headless.run(source, path, directory) == 0
    saved = directory / (manifest["authorization"]["run_id"] + "-headless")
    report = json.loads((saved / "progress.json").read_text())
    assert report["completed"] == ["existing-product-and-native-results"]
    assert report["read_entries"] == list(HEADLESS_ENTRIES)
    assert report["provider_work_submitted"] is False
    assert "fresh-api-cli-mcp-execution" in report["pending"]
    assert report["qualified"] is False and report["status"] == "partial"
    assert "native-ui-sso-arrival" in report["pending"] and "cleanup" in report["pending"]
    assert not list(saved.glob("*qualification*")) and not (saved / "delivery-evidence.json").exists()
    assert all("browser-launch" not in str(request.url) and request.method == "GET" for request in calls)
    assert len(provider_calls) == 3 and "task-ui" not in provider_calls
    assert all(p.stat().st_mode & 0o077 == 0 for p in saved.rglob("*"))
    before = len(calls)
    with pytest.raises(FileExistsError):
        headless.run(source, path, directory)
    assert len(calls) == before


def test_full_journey_validation_still_requires_ui_and_four_native_runs(headless_case):
    manifest, *_ = headless_case
    with pytest.raises(ValueError):
        validate_driver(manifest)
    with pytest.raises(ValueError):
        validate_runs({entry: f"run-{entry}" for entry in HEADLESS_ENTRIES})


@pytest.mark.parametrize("change", [
    lambda manifest: manifest["authorization"].update(scenarios=["journeys"]),
    lambda manifest: manifest["authorization"].update(scenarios="not-headless"),
    lambda manifest: manifest["installed_journeys"].update(started_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat()),
    lambda manifest: manifest["authorization"].update(source_sha="f" * 40),
    lambda manifest: manifest["limits"]["investigation"].update(max_spend_usd="NaN"),
    lambda manifest: manifest["limits"]["investigation"].update(deadline_seconds=9999),
    lambda manifest: manifest["installed_journeys"].update(runs={"product": "old"}),
    lambda manifest: manifest["installed_journeys"].update(token_env="DEVOPS_PRODUCT_TOKEN"),
])
def test_headless_rejects_bad_authority_bounds_or_shared_reader_before_effect(headless_case, change):
    manifest, path, directory, calls, *_ = headless_case
    change(manifest)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        headless.run(manifest["release"]["source_sha"], path, directory)
    assert calls == [] and list(directory.iterdir()) == []


def test_headless_failure_retains_selection_and_fixed_actionable_message(headless_case):
    manifest, path, directory, _, _, runs, _ = headless_case
    runs["product"]["scope"]["tenant_id"] = "foreign-tenant"
    assert headless.run(manifest["release"]["source_sha"], path, directory) == 1
    saved = directory / (manifest["authorization"]["run_id"] + "-headless")
    report = json.loads((saved / "progress.json").read_text())
    assert report["status"] == "failed" and report["qualified"] is False
    assert "do not resubmit" in report["next_action"]
    assert "foreign-tenant" not in (saved / "progress.json").read_text()
    assert (saved / "manifest.json").exists()
    assert not list(saved.glob("*-journey-actions.jsonl"))
