"""No UI/provider access: manual facts cannot replace native result or denial oracles."""

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from test_qualification_driver import driver_case, live_context, live_acceptance, portable_selection, recovery_selection, selector_case
from journey_driver import FirstPartyDriver
from manual_ui import ManualDevops, ManualEvidencePending, validate_manual
from recovery_selector import HTTPRecoveryTarget, authorize_recovery


@pytest.fixture
def manual_case(driver_case, monkeypatch):
    driver, manifest, path, directory = driver_case
    users = [user["id"] for user in manifest["recovery"]["users"]]
    manifest["manual_ui"] = {"mode": "authenticated-human", "preparer_id": "owner", "observer_ids": users,
        "ui_observer_id": users[0], "ui_receipt": "original-ui-receipt-0001", "timeout_seconds": 60, "poll_seconds": 1}
    manifest["journey_driver"].pop("browser_storage_state")
    manifest["journey_driver"]["provider_hostname"] = "selected-space.aidevops.global.app.aws"
    manifest["recovery"]["browser"].pop("storage_states")
    manifest["recovery"]["browser"]["provider_hostname"] = manifest["journey_driver"]["provider_hostname"]
    monkeypatch.setenv("L5_BROWSER_AUTOMATION", "disabled")
    monkeypatch.setenv("ADP_CORRELATION", "orch:11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("ADP_SOURCE_REVISION", manifest["release"]["source_sha"])
    monkeypatch.setenv("ADP_DEFINITION_REVISION", "d" * 40)
    monkeypatch.setenv("GITHUB_SHA", "d" * 40)
    monkeypatch.setenv("ADP_CONTRACT_DIGEST", "e" * 64)
    path.write_text(json.dumps(manifest))
    return driver, manifest, path, directory


def configuration(manifest):
    return {"configuration_id": "selected-configuration", "revision": manifest["journey_driver"]["inputs"]["ui"]["configuration_revision"],
            "scope": manifest["journey_driver"]["scope"]}


def test_manual_preflight_needs_no_browser_state_and_preserves_original_setup(manual_case):
    driver, manifest, path, directory = manual_case
    assert driver.preflight(manifest["release"]["source_sha"], path, directory)[1] == manifest
    assert list(directory.iterdir()) == []
    assert authorize_recovery(manifest, manifest["release"]["source_sha"], directory) == directory


@pytest.mark.parametrize("boundary", ["same_user", "foreign_user", "duplicate_receipt", "fixture_verdict", "unbounded"])
def test_manual_selection_rejects_ambiguous_or_unbounded_evidence(manual_case, boundary):
    _, manifest, _, _ = manual_case
    selection = manifest["manual_ui"]
    if boundary == "same_user": selection["observer_ids"][1] = selection["observer_ids"][0]
    elif boundary == "foreign_user": selection["ui_observer_id"] = "foreign"
    elif boundary == "duplicate_receipt": selection["ui_receipt"] = manifest["journey_driver"]["receipts"]["product"]
    elif boundary == "fixture_verdict": selection["passed"] = True
    else: selection["timeout_seconds"] = 0
    with pytest.raises(ValueError): validate_manual(manifest)


def collected(manifest, request):
    selection = request["selection"]
    human = {"challenge_id": "same-challenge", "snapshot_sha256": "a" * 64, "actor_id": request["observer_id"],
             "observed": {"observation": "submitted", "submitted_run_id": "ui-run"}}
    if selection["subject"] == "native_ui":
        human["observed"] = {"observation": "displayed", "displayed_native_hostname": manifest["journey_driver"]["provider_hostname"], "launch_receipt_id": "issued-receipt"}
    if request["evidence"] == "effect":
        return {"effect": {"challenge_id": "same-challenge", "snapshot_sha256": "a" * 64, "outcome": "returned", "events": [{"http_status": 202, "returned_run_id": "ui-run"}]}}
    return {"observation": human}


@pytest.mark.parametrize("boundary", [None, "wrong_actor", "mismatch", "wrong_host", "missing_launch", "lost_submit"])
def test_submission_collects_exact_effect_human_and_sso_without_browser(manual_case, monkeypatch, boundary):
    _, manifest, _, directory = manual_case
    selections = []
    def collect(journal, requests, **kwargs):
        request = requests[0]; selections.append(request)
        facts = collected(manifest, request)
        if "observation" in facts:
            human = facts["observation"]
            if boundary == "wrong_actor": human["actor_id"] = "foreign"
            elif boundary == "mismatch": human["observed"]["observation"] = "mismatch"
            elif boundary == "wrong_host": human["observed"]["displayed_native_hostname"] = "foreign.aidevops.global.app.aws"
            elif boundary == "missing_launch": human["observed"]["launch_receipt_id"] = None
        elif boundary == "lost_submit": facts["effect"]["outcome"] = "unknown"
        return [SimpleNamespace(facts=facts)]
    monkeypatch.setattr("manual_ui.collect_by_scope", collect)
    monkeypatch.setattr("journey_driver.subprocess.run", lambda *args, **kwargs: pytest.fail("browser started"))
    target = FirstPartyDriver(manifest, directory, deadline=time.monotonic() + 60, max_requests=8)
    monkeypatch.setattr(target, "request", lambda *args, **kwargs: configuration(manifest))
    try:
        if boundary:
            with pytest.raises(ValueError): target.run_ui()
        else:
            assert target.run_ui() == {"run_id": "ui-run", "receipt": manifest["manual_ui"]["ui_receipt"], "native_handoff": True}
            assert [(row["evidence"], row["selection"]["subject"]) for row in selections] == [
                ("effect", "run_submission"), ("human", "run_submission"), ("human", "native_ui")]
    finally: target.close()


def test_missing_human_evidence_is_actionable_and_never_restarts_native_work(manual_case, monkeypatch):
    _, manifest, _, directory = manual_case
    monkeypatch.setattr("manual_ui.collect_by_scope", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(ManualEvidencePending, match="do not repeat native work"):
        ManualDevops(manifest, directory, deadline=time.monotonic()+60).submission(configuration(manifest))


@pytest.mark.parametrize("kind,status", [("missing",401),("invalid",401),("foreign",404),("throttled",429),("logged_out",401),("revoked",403)])
def test_manual_recovery_denials_are_real_http_without_following_redirects(manual_case, monkeypatch, kind, status):
    _, manifest, _, directory = manual_case
    monkeypatch.setattr("recovery_selector.subprocess.run", lambda *args, **kwargs: pytest.fail("browser started"))
    target = HTTPRecoveryTarget(manifest, directory, deadline=time.monotonic()+60, max_requests=100)
    seen=[]
    def handle(request):
        seen.append(request)
        return httpx.Response(status, json={"detail": "denied"})
    target.client.close(); target.client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
    user = manifest["recovery"]["users"][0]
    scope = manifest["recovery"]["foreign_scope"] if kind == "foreign" else manifest["recovery"]["scope"]
    try:
        assert target.launch(user["credential_ref"], scope, kind, "original-attempt:"+kind) == {"status": status}
        assert len(seen)==1 and seen[0].method == "POST" and seen[0].url.path.endswith("/browser-launch")
        assert ("authorization" not in seen[0].headers) == (kind == "missing")
    finally: target.close()


def test_two_original_human_arrivals_are_read_without_repeating_federation(manual_case, monkeypatch):
    _, manifest, _, directory = manual_case
    seen, observers = [], []
    monkeypatch.setattr("recovery_selector.subprocess.run", lambda *args, **kwargs: pytest.fail("browser started"))
    def collect(journal, requests, **kwargs):
        observers.append(requests[0]["observer_id"])
        return [SimpleNamespace(facts=collected(manifest, requests[0]))]
    monkeypatch.setattr("manual_ui.collect_by_scope", collect)
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=configuration(manifest))
    target = HTTPRecoveryTarget(manifest, directory, deadline=time.monotonic()+60, max_requests=100)
    target.client.close(); target.client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
    try:
        for index, user in enumerate(manifest["recovery"]["users"]):
            assert target.launch(user["credential_ref"], manifest["recovery"]["scope"], "allowed", f"original-sso:{index}")["status"] == 200
        assert observers == manifest["manual_ui"]["observer_ids"]
        assert len(seen) == 2 and all(request.method == "GET" and request.url.path.endswith("/configuration") for request in seen)
        assert seen[0].headers["authorization"] != seen[1].headers["authorization"]
    finally:
        target.close()
