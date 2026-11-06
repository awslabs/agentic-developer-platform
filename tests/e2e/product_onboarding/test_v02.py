"""Product V02 scenario binding; local collection is not live browser evidence."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
REQUIREMENTS = ROOT / "docs/product-onboarding/delivery/verification-contract.md"
SCENARIOS = {
    "SCN-01": "tests/e2e/new_ui/test_product_profile.py::test_product_create",
    "SCN-02": "tests/e2e/new_ui/test_product_profile.py::test_product_conflict_denial",
    "SCN-03": "tests/e2e/new_ui/test_product_profile.py::test_product_evidence",
}


def pinned_browser_case(source_revision, requirements_digest, scenario_id="SCN-01", selector=None):
    if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ValueError("Source revision must be a full commit SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", requirements_digest):
        raise ValueError("Requirements digest must be SHA-256")
    expected = SCENARIOS.get(scenario_id)
    if expected is None or (selector is not None and selector != expected):
        raise ValueError("Product scenario must select its reviewed served-browser case")
    return {
        "scenario_id": scenario_id,
        "selector": expected,
        "source_revision": source_revision,
        "requirements_sha256": requirements_digest,
        "status": "not_run",
    }


def collect_pinned_case(tmp_path, scenario_id):
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    requirements = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    record = pinned_browser_case(source, requirements, scenario_id)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", record["selector"]],
        cwd=ROOT, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines().count(record["selector"]) == 1, result.stdout
    output = tmp_path / (scenario_id + "-case.json")
    output.write_text(json.dumps(record, sort_keys=True) + "\n")
    assert json.loads(output.read_text()) == record
    assert record["status"] == "not_run"
    return record


def live_browser_create(tmp_path):
    """SCN-01: pin creation selection without claiming a live pass."""
    record = collect_pinned_case(tmp_path, "SCN-01")
    with pytest.raises(ValueError):
        pinned_browser_case(record["source_revision"], record["requirements_sha256"],
                            "SCN-01", SCENARIOS["SCN-02"])


def live_browser_conflict_denial(tmp_path):
    """SCN-02: require exact case selection and distinct, owned peer fixtures."""
    from tests.e2e.new_ui.test_product_profile import validate_conflict_fixture

    record = collect_pinned_case(tmp_path, "SCN-02")
    run_id = "product-42-1"
    intent_key = "product-create-" + "a" * 64
    document = {
        "run_id": run_id, "tenant_id": "owned-tenant", "actor_id": "owner-actor",
        "source_revision": record["source_revision"], "idempotency_key": intent_key,
        "request": {"name": "eval-product-" + run_id, "purpose": "Owned fixture",
                    "intended_users": ["Maintainers"], "starting_point": "idea", "project": {"mode": "create"}},
        "receipt": {"product_id": "owned-product", "project_id": "owned-project"},
    }
    manifest = {key: document[key] for key in ("run_id", "tenant_id", "actor_id", "source_revision", "idempotency_key")}
    manifest.update(document["receipt"])

    class LocalStore:
        def load(self, selected):
            assert selected == run_id
            return document, manifest

        def save(self, selected, value, manifest=None):
            raise AssertionError("Local fixture must not mutate live state")

    fixture = {
        "run_id": run_id, "tenant_id": "owned-tenant", "actor_id": "owner-actor",
        "source_revision": record["source_revision"], "product_id": "owned-product",
        "project_id": "owned-project", "intent_key": intent_key,
        "state_store": LocalStore(), "observer": {"tenant_id": "owned-tenant", "access_token": "observer-token"},
        "owner": {"access_token": "owner-token", "tenant_id": "owned-tenant", "actor_id": "owner-actor"},
        "nonmember": {"access_token": "other-token", "tenant_id": "foreign-tenant"},
        "revoked": {"access_token": "owner-token", "actor_id": "owner-actor"},
        "revoke": lambda: None, "protected_cleanup": lambda: None,
    }
    validate_conflict_fixture(fixture, fixture["owner"], record["source_revision"])
    with pytest.raises(AssertionError):
        validate_conflict_fixture({**fixture, "nonmember": fixture["owner"]},
                                  fixture["owner"], record["source_revision"])
    with pytest.raises(AssertionError):
        validate_conflict_fixture({**fixture, "source_revision": "e" * 40},
                                  fixture["owner"], record["source_revision"])
    with pytest.raises(AssertionError):
        validate_conflict_fixture({**fixture, "protected_cleanup": None},
                                  fixture["owner"], record["source_revision"])
    with pytest.raises(ValueError):
        pinned_browser_case(record["source_revision"], record["requirements_sha256"],
                            "SCN-02", SCENARIOS["SCN-01"])


def live_browser_evidence(tmp_path):
    """SCN-03: select the real Product evidence case; live status remains not_run here."""
    record = collect_pinned_case(tmp_path, "SCN-03")
    assert record["status"] == "not_run"
    with pytest.raises(ValueError):
        pinned_browser_case(record["source_revision"], record["requirements_sha256"],
                            "SCN-03", SCENARIOS["SCN-02"])
