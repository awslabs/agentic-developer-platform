"""Offline contracts for selected-release cleanup without a deployed target."""

import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.e2e.product_onboarding.product_catalog_fixture import record_membership_receipts
from tests.e2e.product_onboarding.product_catalog_live_recovery import run_live_recovery
from tests.e2e.product_onboarding.product_catalog_recovery_scenario import (
    begin_recovery_fixture, pin_recovery_cleanup, pinned_recovery_scenario,
)
from tests.e2e.product_onboarding.test_product_catalog_recovery_scenario import owned_state

SELECTOR = "tests/e2e/product_onboarding/test_v12.py::live_catalog_recovery"
CLEANUP_AT = "2026-10-02T00:00:00Z"
PEER = {"tenant_id": "example-tenant", "product_id": "peer-product", "project_id": "peer-project",
        "portfolio_id": "peer-portfolio"}


def live_fixture(monkeypatch):
    state, release = owned_state()
    for index, entry in enumerate(state.document["catalog"]["portfolios"]):
        record_membership_receipts(state, entry["name"],
                                   {"id": f"owned-portfolio-{index}", "revision": "1", "change_id": f"p-{index}"},
                                   {"id": f"owned-link-{index}", "revision": "1", "change_id": f"l-{index}"})
    begin_recovery_fixture(state, pinned_recovery_scenario(state))
    pin_recovery_cleanup(state, CLEANUP_AT, PEER)
    monkeypatch.setattr("tests.e2e.product_onboarding.product_catalog_live_recovery.check_served_assets",
                        lambda *args: None)
    request = FakeRequest(state)
    provider = SimpleNamespace(state=state, request=request, run_id=state.binding["run_id"],
                               base_url="https://example.invalid", frontend={}, owner={"access_token": "synthetic"})
    observed = {**release, "observation_ref": "independent:runtime"}
    return provider, request, lambda selected: deepcopy(observed)


class FakeRequest:
    def __init__(self, state):
        self.state = state
        self.calls = []
        self.receipts = {}
        self.faults = {}
        self.peer = {"portfolio": {"id": PEER["portfolio_id"], "revision": "1"},
                     "profile": {"product_id": PEER["product_id"], "project_id": PEER["project_id"]},
                     "memberships": {"items": []}}

    @staticmethod
    def response(value):
        return SimpleNamespace(status=200, json=lambda: deepcopy(value))

    def get(self, url, *, headers, timeout):
        assert headers == {"Authorization": "Bearer synthetic"} and timeout == 30_000
        parsed = urlsplit(url)
        path = parsed.path
        params = parse_qs(parsed.query)
        self.calls.append(("get", path, params))
        if path == "/api/auth/me":
            return self.response({"org_id": self.state.binding["tenant_id"],
                                  "user_id": self.faults.get("actor", self.state.binding["actor_id"]),
                                  "account_type": "human", "is_admin": False})
        if path.endswith("/projects/" + PEER["project_id"]):
            return self.response(self.peer["profile"])
        if path.endswith("/" + PEER["portfolio_id"]):
            return self.response(self.peer["portfolio"])
        if path.endswith("/" + PEER["portfolio_id"] + "/memberships"):
            peer = deepcopy(self.peer["memberships"])
            if self.faults.get("peer_change") and len(self.receipts) == 4:
                peer["items"] = [{"relationship_id": "unowned-changed"}]
            return self.response(peer)
        entry = next(item for item in self.state.document["catalog"]["portfolios"]
                     if item["receipts"]["portfolio"]["id"] in path)
        portfolio = entry["receipts"]["portfolio"]
        relationship = entry["receipts"]["relationship"]
        if path.endswith("/memberships"):
            close = self.receipts.get(entry["keys"]["close"])
            if params.get("effective_at") == [CLEANUP_AT]:
                return self.response({"items": [] if close else [{"relationship_id": relationship["id"]}]})
            if self.faults.get("missing_history"):
                return self.response({"items": []})
            if self.faults.get("duplicate_history"):
                return self.response({"items": [{}, {}]})
            return self.response({"items": [{"relationship_id": relationship["id"],
                                             "relationship_revision": close["revision"] if close else "1",
                                             "valid_from": self.state.document["catalog"]["plan"]["clock"],
                                             "valid_to": CLEANUP_AT if close else None,
                                             "product_ref": {"record_id": "owned-product", "project_id": "owned-project",
                                                             "tenant_id": self.state.binding["tenant_id"]}}]})
        retire = self.receipts.get(entry["keys"]["retire"])
        return self.response({"id": portfolio["id"], "revision": retire["revision"] if retire else "1",
                              "data": {"lifecycle_status": "retired" if retire else "active"}})

    def patch(self, url, *, data, headers, timeout):
        assert headers["Authorization"] == "Bearer synthetic" and timeout == 30_000
        key = headers["Idempotency-Key"]
        entry = next(item for item in self.state.document["catalog"]["portfolios"]
                     if key in (item["keys"]["close"], item["keys"]["retire"]))
        operation = "close" if key == entry["keys"]["close"] else "retire"
        original = entry["receipts"]["relationship" if operation == "close" else "portfolio"]
        assert headers["If-Match"] == original["revision"] and data
        self.calls.append(("patch", urlsplit(url).path, key))
        if self.faults.get("interrupt_replay") and len([call for call in self.calls if call[0] == "patch"]) == 2:
            raise ConnectionError("simulated interruption after first commit")
        receipt = self.receipts.setdefault(key, {"id": original["id"], "revision": "2", "change_id": key})
        if self.faults.get("changed_receipt") and len(self.calls) >= 4:
            return self.response({**receipt, "id": "foreign-record"})
        return self.response(receipt)


def test_live_recovery_selector_collects_without_a_target():
    result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", SELECTOR],
                            cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines().count(SELECTOR) == 1


def test_live_recovery_replays_lost_response_and_preserves_peer_and_history(monkeypatch):
    provider, request, observe = live_fixture(monkeypatch)
    release = deepcopy(provider.state.document["catalog_release"])
    result = run_live_recovery(provider, observe, CLEANUP_AT, PEER, provider.state.binding["source_revision"])
    assert result["cleanup_verified"] is True and result["peer_unchanged"] is True
    assert result["attempts"] == 5
    assert provider.state.document["catalog_release"] == release
    assert provider.state.document["catalog"]["cleanup_pending"] is False
    assert [attempt["state"] for attempt in provider.state.document["catalog_recovery"]["attempts"]].count(
        "response_unknown") == 1
    assert len([call for call in request.calls if call[0] == "patch"]) == 5
    assert set(request.receipts) == {entry["keys"][operation]
                                     for entry in provider.state.document["catalog"]["portfolios"]
                                     for operation in ("close", "retire")}
    assert request.peer["portfolio"]["revision"] == "1"


def test_live_recovery_resumes_original_key_after_interrupted_retry(monkeypatch):
    provider, request, observe = live_fixture(monkeypatch)
    request.faults["interrupt_replay"] = True
    revision = provider.state.binding["source_revision"]
    with pytest.raises(ConnectionError, match="simulated interruption"):
        run_live_recovery(provider, observe, CLEANUP_AT, PEER, revision)
    first = provider.state.document["catalog"]["portfolios"][0]
    attempts = provider.state.document["catalog_recovery"]["cleanup_attempts"][first["name"]]["close"]["attempts"]
    assert [item["state"] for item in attempts] == ["response_unknown", "response_unknown"]
    request.faults.clear()
    assert run_live_recovery(provider, observe, CLEANUP_AT, PEER, revision)["cleanup_verified"] is True
    assert [call[2] for call in request.calls if call[0] == "patch"][:3] == [first["keys"]["close"]] * 3
    assert [item["state"] for item in provider.state.document["catalog_recovery"]["attempts"]].count(
        "response_unknown") == 2


def test_live_recovery_rejects_changed_durable_key_on_restart(monkeypatch):
    provider, request, observe = live_fixture(monkeypatch)
    request.faults["interrupt_replay"] = True
    revision = provider.state.binding["source_revision"]
    with pytest.raises(ConnectionError):
        run_live_recovery(provider, observe, CLEANUP_AT, PEER, revision)
    first = provider.state.document["catalog"]["portfolios"][0]
    first["keys"]["close"] = "foreign-key"
    request.faults.clear()
    with pytest.raises(ValueError, match="idempotency key"):
        run_live_recovery(provider, observe, CLEANUP_AT, PEER, revision)
    assert len([call for call in request.calls if call[0] == "patch"]) == 2


@pytest.mark.parametrize("fault,expected", [
    ("actor", "actor differs"), ("missing_history", "history differs"),
    ("duplicate_history", "history differs"), ("changed_receipt", "owned receipt"),
    ("peer_change", "unowned peer"),
])
def test_live_recovery_fails_closed_and_retains_attempts(monkeypatch, fault, expected):
    provider, request, observe = live_fixture(monkeypatch)
    request.faults[fault] = "foreign-editor" if fault == "actor" else True
    with pytest.raises((ValueError, AssertionError), match=expected):
        run_live_recovery(provider, observe, CLEANUP_AT, PEER, provider.state.binding["source_revision"])
    assert provider.state.document["catalog"]["cleanup_pending"] is True
    if fault == "actor":
        assert not request.receipts
    else:
        assert provider.state.document["catalog_recovery"]["cleanup_attempts"]


def test_live_recovery_refuses_missing_observer_foreign_peer_or_changed_release(monkeypatch):
    provider, request, observe = live_fixture(monkeypatch)
    revision = provider.state.binding["source_revision"]
    for reader, peer, source, expected in (
        (None, PEER, revision, "selected release observer"),
        (observe, {**PEER, "tenant_id": "foreign"}, revision, "unowned peer"),
        (observe, PEER, "f" * 40, "selected source"),
        (lambda _: {**observe({}), "schema_head": "other"}, PEER, revision, "schema differs"),
    ):
        with pytest.raises(ValueError, match=expected):
            run_live_recovery(provider, reader, CLEANUP_AT, peer, source)
    assert not request.receipts
