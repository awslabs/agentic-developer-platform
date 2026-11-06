"""Offline contract for the deployable V12 membership readback selector."""

import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding.product_catalog_fixture import (
    begin_membership_fixture, pinned_membership_scenario, publish_membership_fixture,
    record_membership_baseline, record_membership_receipts,
)
from tests.e2e.product_onboarding.product_catalog_live_membership import verify_live_membership
from tests.e2e.product_onboarding.test_product_catalog_fixture import LocalState

ROOT = Path(__file__).resolve().parents[3]
SELECTOR = "tests/e2e/product_onboarding/test_v12.py::live_catalog_membership"


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return deepcopy(self.body)


def owned_run(tmp_path):
    binding = {"run_id": "owned-run", "source_revision": "a" * 40,
               "tenant_id": "owned-tenant", "actor_id": "owned-editor", "owner_subject": "owned-subject"}
    state = LocalState(binding)
    plan = pinned_membership_scenario(binding["source_revision"], "b" * 40, binding["run_id"],
                                      "2026-10-01T00:00:00Z")
    begin_membership_fixture(state, plan, {"product_id": "owned-product", "project_id": "owned-project",
                                           "product_name": "Owned Product"})
    baseline = {"product_id": "owned-product", "project_id": "owned-project", "name": "Owned Product",
                "purpose": "Original purpose", "intended_users": ["Maintainers"]}
    record_membership_baseline(state, baseline)
    for index, name in enumerate(("Commerce", "Shared services")):
        record_membership_receipts(state, name,
                                   {"id": f"portfolio-{index}", "revision": "1", "change_id": f"p-{index}"},
                                   {"id": f"link-{index}", "revision": "1", "change_id": f"m-{index}"})
    path = tmp_path / "catalog.json"
    tmp_path.chmod(0o700)
    publish_membership_fixture(state, path)
    responses = {"/api/auth/me": {"org_id": binding["tenant_id"], "user_id": binding["actor_id"],
                                   "account_type": "human", "is_admin": False},
                 "/api/v1/products/owned-product/projects/owned-project": {
                     "product_id": baseline["product_id"], "project_id": baseline["project_id"],
                     "product": {key: baseline[key] for key in ("name", "purpose", "intended_users")}}}
    for index, name in enumerate(("Commerce", "Shared services")):
        portfolio = state.document["catalog"]["portfolios"][index]
        portfolio_id = portfolio["receipts"]["portfolio"]["id"]
        responses[f"/api/v1/catalog/portfolios/{portfolio_id}"] = {
            "id": portfolio_id, "revision": "1", "data": {"name": name}}
        responses[f"/api/v1/catalog/portfolios/{portfolio_id}/memberships"] = {"items": [{
            "portfolio_ref": {"record_id": portfolio_id},
            "product_ref": {"record_id": "owned-product", "project_id": "owned-project", "tenant_id": "owned-tenant"},
            "relationship_id": f"link-{index}", "relationship_revision": "1", "valid_from": plan["clock"],
        }]}
    calls = []

    def get(url, *, headers, timeout):
        calls.append((url, headers))
        return Response(responses[url.removeprefix("https://catalog.example.invalid")])

    provider = SimpleNamespace(state=state, binding=binding, run_id=binding["run_id"],
                               owner={"access_token": "synthetic-token"}, frontend={"verified": True},
                               request=SimpleNamespace(get=get))
    return provider, path, responses, calls


def test_membership_live_selector_collects_but_does_not_run_without_explicit_target(monkeypatch):
    monkeypatch.delenv("PRODUCT_CATALOG_LIVE_ENABLED", raising=False)
    collection = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", SELECTOR],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert collection.returncode == 0, collection.stdout + collection.stderr
    assert collection.stdout.splitlines().count(SELECTOR) == 1


def test_membership_live_selector_refuses_missing_selected_release_before_network(monkeypatch):
    environment = dict(os.environ)
    environment["PRODUCT_CATALOG_LIVE_ENABLED"] = "1"
    environment.pop("PRODUCT_EXPECTED_REVISION", None)
    execution = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", SELECTOR],
                               cwd=ROOT, capture_output=True, text=True, timeout=30, env=environment)
    assert execution.returncode != 0
    assert "requires PRODUCT_EXPECTED_REVISION" in execution.stdout


def test_membership_live_readback_uses_owned_receipts_and_original_product(tmp_path, monkeypatch):
    provider, path, _, calls = owned_run(tmp_path)
    seen = []
    monkeypatch.setattr("tests.e2e.product_onboarding.product_catalog_live_membership.check_served_assets",
                        lambda *args: seen.append(args))
    result = verify_live_membership(provider, "https://catalog.example.invalid", path,
                                    provider.binding["source_revision"])
    assert result == {"run_id": provider.run_id, "source_revision": provider.binding["source_revision"],
                      "memberships": {"Commerce": "link-0", "Shared services": "link-1"},
                      "product_unchanged": True}
    assert len(seen) == 1 and len(calls) == 6
    assert all(headers == {"Authorization": "Bearer synthetic-token"} for _, headers in calls)


def test_membership_baseline_cannot_be_rewritten_after_relationship_receipts(tmp_path):
    provider, _, _, _ = owned_run(tmp_path)
    original = provider.state.document["catalog"]["profile_before"]
    assert record_membership_baseline(provider.state, original) == original
    with pytest.raises(ValueError, match="differs from protected intent"):
        record_membership_baseline(provider.state, {**original, "purpose": "Changed after relationship"})
    del provider.state.document["catalog"]["profile_before"]
    with pytest.raises(ValueError, match="must precede relationship writes"):
        record_membership_baseline(provider.state, original)


@pytest.mark.parametrize("fault", ["wrong_actor", "edited_product", "duplicate_link", "altered_receipt", "missing_baseline"])
def test_membership_live_readback_rejects_foreign_or_stale_state_before_reporting_success(tmp_path, monkeypatch, fault):
    provider, path, responses, _ = owned_run(tmp_path)
    monkeypatch.setattr("tests.e2e.product_onboarding.product_catalog_live_membership.check_served_assets",
                        lambda *args: None)
    if fault == "wrong_actor":
        responses["/api/auth/me"]["org_id"] = "other-tenant"
    elif fault == "edited_product":
        responses["/api/v1/products/owned-product/projects/owned-project"]["product"]["purpose"] = "Changed"
    elif fault == "duplicate_link":
        rows = responses["/api/v1/catalog/portfolios/portfolio-0/memberships"]["items"]
        rows.append(deepcopy(rows[0]))
    elif fault == "altered_receipt":
        provider.state.document["catalog"]["portfolios"][0]["receipts"]["relationship"]["id"] = "changed"
    else:
        del provider.state.document["catalog"]["profile_before"]
    with pytest.raises((AssertionError, ValueError)):
        verify_live_membership(provider, "https://catalog.example.invalid", path,
                               provider.binding["source_revision"])
