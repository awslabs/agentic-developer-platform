"""Offline selector tests do not assert a deployed peer is available."""

import hashlib
import subprocess
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.e2e.cli_uplift.test_product_onboarding_full import unavailable_case
from tests.e2e.new_ui.test_product_resume import installed_cli
from tests.e2e.product_onboarding import test_v10

SOURCE = "a" * 40
REQUIREMENTS = "b" * 40


def selected(monkeypatch):
    fixture, reads = unavailable_case()
    fixture.update(owned_fixture=True, source_revision=SOURCE, run_id="owned-peer-12345",
                   frontend_release={"verified": True}, evidence_context={"deployment": {
                       "observed": {"product_onboarding_enabled": True}}})
    for actor in ("owner", "observer", "foreign", "revoked"):
        fixture[actor] = {"access_token": "synthetic-" + actor}
    fixture["owner"].update(id_token="synthetic-id", expires_in="3600", tenant_id=fixture["tenant_id"])
    hashes = {name: character * 64 for name, character in
              (("adp", "c"), ("adp-onboard.py", "d"), ("adp-product.py", "e"))}
    cleanup_calls = []
    effect_calls = []
    peer_calls = []

    def observe_effects():
        effect_calls.append(1)
        return deepcopy(reads["effects_before" if len(effect_calls) == 1 else "effects_after"])

    def get(url, *, headers, timeout):
        assert headers == {"Authorization": "Bearer synthetic-observer"}
        assert timeout == 30_000
        if url.endswith("/changes?limit=100"):
            document = {"items": reads["lineage_before" if not peer_calls else "lineage_after"],
                        "next_cursor": None}
        elif url.endswith("/revisions/1"):
            document = reads["historical_before" if not peer_calls else "historical_after"]
        else:
            pytest.fail("Unexpected history request")
        return SimpleNamespace(status=200, json=lambda: deepcopy(document))

    def post(url, *, headers, timeout):
        assert timeout == 30_000
        assert headers["If-Profile-Revision"] == "2"
        assert headers["Idempotency-Key"].startswith("deferred_")
        peer = url.rsplit("/", 1)[1]
        actor = headers["Authorization"].removeprefix("Bearer synthetic-")
        unknown = "/no-such-product/" in url
        peer_calls.append((peer, actor, unknown))
        if actor == "owner":
            observed = reads["refusals"][peer][1 if peer == "context" and
                                                   peer_calls.count((peer, actor, False)) > 1 else 0]
        else:
            observed = reads["unknowns" if unknown else "denials"][actor][peer]
        return SimpleNamespace(status=observed["status"], json=lambda: deepcopy(observed["body"]))

    record = SimpleNamespace(cli_events=[])
    run = {"fixture": fixture, "provider": SimpleNamespace(request=SimpleNamespace(get=get, post=post)),
           "base_url": "https://example.invalid", "binary": "adp", "checksums": hashes,
           "expected_cli_hashes": hashes, "deployment_source_revision": SOURCE,
           "requirements_revision": REQUIREMENTS, "record": record, "page": object(),
           "create_ui_session": lambda: deepcopy(reads["ui_session"]),
           "observe_effects": observe_effects,
           "cleanup": lambda: cleanup_calls.append(1) or {
               "run_id": fixture["run_id"], "source_revision": SOURCE, "status": "verified",
               "owned_absent": True, "shared_unchanged": True, "receipt_ref": "private:cleanup",
           }}
    read_profiles = []
    read_sessions = []

    def read_profile(*_args):
        read_profiles.append(1)
        return deepcopy(reads["profile_before" if len(read_profiles) == 1 else "profile_after"])

    def read_session(*_args):
        read_sessions.append(1)
        return deepcopy(reads["session_before" if len(read_sessions) == 1 else "session_after"])

    def run_installed(_binary, checksums, _owner, _origin, arguments, *, record, command="onboard"):
        assert checksums == hashes
        record.cli_events.append({"command": ["adp", command, *arguments], "exit_code": 0,
                                  "installed_cli_sha256": hashes["adp"]})
        if command == "product":
            assert arguments == ["show", "--product", "owned-product", "--project", "owned-project"]
            return deepcopy(reads["cli_profile"])
        assert arguments == ["resume", "--product", "owned-product", "--project", "owned-project",
                             "--session", "owned-session"]
        return deepcopy(reads["cli_resumed"])

    monkeypatch.setattr(test_v10, "check_served_assets", lambda *_args: None)
    monkeypatch.setattr(test_v10, "read_profile", read_profile)
    monkeypatch.setattr(test_v10, "read_session", read_session)
    monkeypatch.setattr(test_v10, "installed_cli", run_installed)
    return run, reads, cleanup_calls, peer_calls, effect_calls


def test_peer_selector_exercises_distinct_actors_and_readbacks(monkeypatch):
    run, _reads, cleanup, peers, effects = selected(monkeypatch)
    result = test_v10.run_cli_peer_unavailable(run, SOURCE, REQUIREMENTS)
    assert result["status"] == "unavailable" and result["cleanup"]["status"] == "verified"
    assert len(peers) == 11 and len(effects) == 2 and cleanup == [1]
    assert peers[:3] == [("context", "owner", False), ("context", "owner", False),
                         ("l5", "owner", False)]
    assert {actor for _peer, actor, _unknown in peers} == {"owner", "foreign", "revoked"}
    assert [event["command"][1] for event in run["record"].cli_events] == ["product", "onboard"]


@pytest.mark.parametrize("fault", ("missing_product_hash", "wrong_release_pin", "same_actor",
                                    "wrong_source", "wrong_owner", "no_effect_oracle", "no_cleanup"))
def test_peer_selector_refuses_unpinned_or_missing_authority(monkeypatch, fault):
    run, _reads, cleanup, peers, _effects = selected(monkeypatch)
    if fault == "missing_product_hash":
        run["checksums"].pop("adp-product.py")
    elif fault == "wrong_release_pin":
        run["expected_cli_hashes"] = {**run["checksums"], "adp": "f" * 64}
    elif fault == "same_actor":
        run["fixture"]["revoked"] = run["fixture"]["owner"]
    elif fault == "wrong_source":
        run["deployment_source_revision"] = "f" * 40
    elif fault == "wrong_owner":
        run["fixture"]["owner"]["tenant_id"] = "foreign-tenant"
    elif fault == "no_effect_oracle":
        run["observe_effects"] = None
    else:
        run["cleanup"] = None
    with pytest.raises(ValueError, match="pinned installed clients"):
        test_v10.run_cli_peer_unavailable(run, SOURCE, REQUIREMENTS)
    assert cleanup == [] and peers == []


@pytest.mark.parametrize("fault", ("accepted_peer", "invented_receipt", "changed_lineage", "leaked_denial"))
def test_peer_selector_rejects_false_success_and_always_cleans_up(monkeypatch, fault):
    run, reads, cleanup, _peers, _effects = selected(monkeypatch)
    if fault == "accepted_peer":
        reads["refusals"]["l5"][0]["status"] = 202
    elif fault == "invented_receipt":
        reads["effects_after"]["receipts"] = ["invented"]
    elif fault == "changed_lineage":
        reads["lineage_after"][0]["revision"] = 3
    else:
        reads["denials"]["foreign"]["context"]["body"] = {"detail": {"secret": "scope"}}
    with pytest.raises(AssertionError):
        test_v10.run_cli_peer_unavailable(run, SOURCE, REQUIREMENTS)
    assert cleanup == [1]


def test_peer_selector_rejects_failed_cleanup(monkeypatch):
    run, _reads, cleanup, _peers, _effects = selected(monkeypatch)
    run["cleanup"] = lambda: cleanup.append(1) or {"status": "failed"}
    with pytest.raises(AssertionError, match="cleanup"):
        test_v10.run_cli_peer_unavailable(run, SOURCE, REQUIREMENTS)
    assert cleanup == [1]


def test_peer_live_selector_is_collectable_and_requires_real_fixture(monkeypatch):
    selector = "tests/e2e/product_onboarding/test_v10.py::live_cli_peer_unavailable"
    result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", selector],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines().count(selector) == 1
    monkeypatch.setenv("PRODUCT_V10_LIVE_ENABLED", "1")
    monkeypatch.setenv("PRODUCT_V10_SOURCE_REVISION", SOURCE)
    monkeypatch.setenv("PRODUCT_V10_REQUIREMENTS_REVISION", REQUIREMENTS)
    request = SimpleNamespace(getfixturevalue=lambda _: pytest.fail("No authorized peer fixture"))
    with pytest.raises(pytest.fail.Exception, match="No authorized peer fixture"):
        test_v10.live_cli_peer_unavailable(request)


def test_installed_product_show_checks_product_helper_hash_and_json(tmp_path):
    binary = tmp_path / "adp"
    binary.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"status":"ok","detail":{"peer_availability":{}}}\'\n')
    binary.chmod(0o700)
    (tmp_path / "adp-product.py").write_text("pinned helper\n")
    hashes = {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
              for name in ("adp", "adp-product.py")}
    actor = {"access_token": "synthetic-token", "id_token": "synthetic-id", "expires_in": "3600",
             "tenant_id": "owned-tenant"}
    record = SimpleNamespace(cli_events=[])
    assert installed_cli(binary, hashes, actor, "https://example.invalid", ["show"],
                         command="product", record=record) == {"peer_availability": {}}
    assert record.cli_events[0]["command"] == ["adp", "product", "show", "--json"]
    (tmp_path / "adp-product.py").write_text("changed helper\n")
    with pytest.raises(ValueError, match="checksums"):
        installed_cli(binary, hashes, actor, "https://example.invalid", ["show"], command="product")
