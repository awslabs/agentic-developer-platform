"""Offline guards for the assistant harness's Dev Box journey."""

import importlib
import json
from pathlib import Path

import pytest

from tests.e2e.cli_uplift import bundle, cases, config, stages


@pytest.fixture
def journey(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "e2e/cli_uplift/remote"))
    module = importlib.import_module("dev_box_lifecycle")
    monkeypatch.setattr(module.assistant_isolation_clients, "fixture_sessions", lambda _cfg: {"a1": object(), "b1": object()})
    monkeypatch.setattr(module.assistant_isolation_clients, "verify_identities", lambda *_args: None)
    return module, {
        "evaluation_id": "synthetic-run", "work_dir": str(tmp_path),
        "gateway_url": "https://example.invalid",
        "dev_box_lifecycle": {"approved": True, "template": "assistant", "profile": "crew"},
    }


def test_dev_box_case_requires_explicit_fixture_and_shipped_driver():
    assert stages.JOURNEY_DRIVERS["E51"] == "dev_box_lifecycle"
    assert "dev_box_lifecycle" in bundle.purposes()
    matrix = cases.new_matrix(("assistant",))
    blocked = cases.block_missing_fixtures(matrix, {cases.EC2, cases.COGNITO, cases.PLATFORM, cases.ASSISTANT_USERS})
    assert blocked["E51"] == [cases.DEV_BOX_LIFECYCLE]
    assert matrix["E51"]["status"] == cases.BLOCKED


def test_live_fixture_checks_exact_account_region_and_budget():
    from tests.unit.test_cli_uplift import config_fixture

    baseline = config.validate(config_fixture())
    lifecycle = {
        "approved": True, "account": baseline["platform_account"],
        "region": baseline["region"], "template": "assistant", "profile": "crew",
        "approved_budget_usd": 1,
    }
    assert config.validate(config_fixture(dev_box_lifecycle=lifecycle))["dev_box_lifecycle"] == lifecycle
    for alteration in (
        {"account": baseline["destination_account"]},
        {"region": "us-west-2"},
        {"approved_budget_usd": baseline["daily_budget_usd"] + 1},
        {"approved_budget_usd": True},
    ):
        with pytest.raises(config.ConfigError, match="target identity|budget"):
            config.validate(config_fixture(dev_box_lifecycle={**lifecycle, **alteration}))


def test_dev_box_journey_requires_approval_before_any_request(journey, monkeypatch):
    module, config = journey
    config["dev_box_lifecycle"]["approved"] = False
    monkeypatch.setattr(module, "request", lambda *_args, **_kwargs: pytest.fail("Unapproved live mutation"))
    with pytest.raises(module.common.RemoteError, match="approved"):
        module.execute(config, {})


def test_api_only_journey_cannot_report_live_qualification():
    matrix = cases.new_matrix(("assistant",))
    for case_id in matrix:
        if case_id != "E51":
            cases.record(matrix, case_id, cases.BLOCKED)

    context = {
        "document": {"instance_id": "i-synthetic"},
        "matrix": matrix, "fault": "none", "transcript": [], "correlation": {},
        "record": lambda case_id, status, detail: cases.record(matrix, case_id, status, detail),
    }
    ports = {"journey": lambda purpose: (
        lambda _instance, _ctx: {"success": True, "detail": {"two_owners": True}}
    ) if purpose == "dev_box_lifecycle" else None}
    stages.journeys_stage({"region": "us-east-1"}, ports)(context)
    assert matrix["E51"]["status"] == cases.BLOCKED
    assert "provider" in matrix["E51"]["detail"]["reason"]


def test_two_owner_journey_checks_operation_identity_disk_and_release(journey, monkeypatch):
    module, config = journey
    actions = []

    def fake_request(_gateway, session, path, *, method="GET", body=None, key=None):
        if method == "POST":
            actor = "a1" if len(actions) < 6 else "b1"
            action = path.split("/")[-1] if path != "/cloud-desktops" else "setup"
            actions.append((actor, action, key, body))
            return 202, {"desktop_id": "box-" + actor, "operation_id": "op-" + str(len(actions))}
        if "/operations/" in path:
            box, operation = path.split("/")[2], path.split("/")[-1]
            return 200, {"desktop_id": box, "operation_id": operation, "status": "succeeded"}
        if path.startswith("/cloud-desktops/box-"):
            actor = path.split("/")[2].removeprefix("box-")
            if session is sessions["b1" if actor == "a1" else "a1"]:
                return 404, None
            if path.endswith("/resources"):
                deleted = any(entry[0] == actor and entry[1] == "release" for entry in actions)
                return 200, {"items": [{"resource_kind": "workspace_disk", "provider_resource_id": "vol-" + actor,
                                         "encrypted": True, "deletion_state": "verified_deleted" if deleted else "present"}]}
        return 200, {}

    sessions = {"a1": object(), "b1": object()}
    monkeypatch.setattr(module.assistant_isolation_clients, "fixture_sessions", lambda _cfg: sessions)
    monkeypatch.setattr(module, "request", fake_request)
    evidence = {}
    module.execute(config, evidence)
    assert evidence["detail"]["two_owners"] is True
    assert evidence["detail"]["identity_checks"] == []
    assert len(evidence["detail"]["operation_ids"]) == 12
    assert [action for _, action, _, _ in actions] == ["setup", "stop", "start", "stop", "replace", "release"] * 2
    assert all(key for _, _, key, _ in actions)
    with pytest.raises(module.common.RemoteError, match="prior intent"):
        module.execute(config, {})


def test_foreign_owner_access_fails_closed(journey, monkeypatch):
    module, config = journey

    def fake_request(_gateway, _session, path, *, method="GET", **_kwargs):
        if method == "POST":
            return 202, {"desktop_id": "box-one", "operation_id": "op-one"}
        if "/operations/" in path:
            return 200, {"desktop_id": "box-one", "operation_id": "op-one", "status": "succeeded"}
        return 200, {}

    monkeypatch.setattr(module, "request", fake_request)
    with pytest.raises(module.common.RemoteError, match="Another owner"):
        module.execute(config, {})


@pytest.fixture
def host_journey(journey, monkeypatch):
    module, config = journey
    config["assistant_users"] = {
        "a1": {"canonical_user_id": "user-a", "tenant_id": "tenant-a"},
        "b1": {"canonical_user_id": "user-b", "tenant_id": "tenant-b"},
    }
    sessions = {actor: object() for actor in ("a1", "b1")}
    monkeypatch.setattr(module.assistant_isolation_clients, "fixture_sessions", lambda _cfg: sessions)
    operations = []
    approvals = set()
    observations = []
    challenge = "privateenrollmentchallenge" + "0" * 16

    def fake_request(_gateway, session, path, *, method="GET", body=None, key=None):
        if path.endswith("/host-enrollment"):
            actor = path.split("/")[2].removeprefix("box-")
            observations.append((actor, method, len(operations)))
            if session is not sessions[actor]:
                return 404, {"detail": "Not found"}
            if (actor, len(operations)) in approvals:
                return 409, {"detail": "Pending"}
            approvals.add((actor, len(operations)))
            assert body == {"public_key_digest": "a" * 64}
            return 201, {"device_id": "device-" + actor, "challenge": challenge}
        if method == "POST":
            actor = "a1" if session is sessions["a1"] else "b1"
            action = path.split("/")[-1] if path != "/cloud-desktops" else "setup"
            operations.append((actor, action, key))
            return 202, {"desktop_id": "box-" + actor, "operation_id": "op-" + str(len(operations))}
        if "/operations/" in path:
            box, operation = path.split("/")[2], path.split("/")[-1]
            return 200, {"desktop_id": box, "operation_id": operation, "status": "succeeded"}
        actor = path.split("/")[2].removeprefix("box-")
        if session is not sessions[actor]:
            return 404, {"detail": "Not found"}
        if path.endswith("/resources"):
            deleted = any(entry[:2] == (actor, "release") for entry in operations)
            return 200, {"items": [{"resource_kind": "workspace_disk", "provider_resource_id": "vol-" + actor,
                                     "encrypted": True, "deletion_state": "verified_deleted" if deleted else "present"}]}
        return 200, {}

    monkeypatch.setattr(module, "request", fake_request)

    class Probe:
        invalid_status = None
        invalid_variant = None

        def public_key_digest(self, actor, box_id):
            assert box_id == "box-" + actor
            return "a" * 64

        def attest(self, actor, box_id, device_id, supplied):
            assert box_id == "box-" + actor and device_id == "device-" + actor
            assert supplied == challenge
            return "00000000-0000-4000-8000-" + f"{len(approvals):012d}"

        def invoke(self, actor, box_id, run_id, variant):
            assert box_id == "box-" + actor and len(run_id) == 36
            observations.append((actor, variant, len(operations)))
            if variant == "current":
                if self.invalid_variant == variant:
                    return 200, {"root_human_id": "user-other", "tenant_id": "tenant-other"}
                return 200, {"root_human_id": config["assistant_users"][actor]["canonical_user_id"],
                             "tenant_id": config["assistant_users"][actor]["tenant_id"]}
            if self.invalid_variant == variant:
                return self.invalid_status or 200, {}
            return 403, {}

    return module, config, Probe(), operations, observations, challenge


def test_offline_host_identity_scenario_fences_both_generations_without_persisting_proof(host_journey):
    module, config, probe, operations, observations, challenge = host_journey
    evidence = {}
    module.execute(config, evidence, identity_probe=probe)
    assert evidence["detail"]["status"] == "BLOCKED"
    assert evidence["detail"]["identity_checks"] == [
        {"actor": actor, "generation": generation, "verified": True}
        for actor in ("a1", "b1") for generation in ("initial", "replacement")
    ]
    assert len(operations) == 12
    assert len([entry for entry in observations if entry[1] == "POST"]) == 12
    assert len([entry for entry in observations if entry[1] == "stale"]) == 10
    journal = next(Path(config["work_dir"]).glob("dev-box-*.json"))
    assert challenge not in journal.read_text() and challenge not in str(evidence)
    assert "00000000-0000-4000-8000-" not in journal.read_text() and "00000000-0000-4000-8000-" not in str(evidence)
    assert [entry["state"] for entry in json.loads(journal.read_text())["host_identity"]] == ["verified"] * 4


@pytest.mark.parametrize("variant", ["current", "foreign_instance", "foreign_role", "foreign_run", "direct_bedrock", "other_route", "stale"])
def test_offline_host_identity_scenario_rejects_misleading_provider_evidence(host_journey, variant):
    module, config, probe, _operations, _observations, _challenge = host_journey
    probe.invalid_variant = variant
    probe.invalid_status = 200
    with pytest.raises(module.common.RemoteError, match="ownership|approved run|remains authorized"):
        module.execute(config, {}, identity_probe=probe)


@pytest.mark.parametrize("original_status,altered_status,error", [
    (404, 200, "Another owner"),
    (409, 201, "replayed"),
])
def test_offline_host_enrollment_refuses_foreign_owner_or_replay(host_journey, monkeypatch, original_status, altered_status, error):
    module, config, probe, _operations, _observations, _challenge = host_journey
    original = module.request

    def tamper(*args, **kwargs):
        status, receipt = original(*args, **kwargs)
        if args[2].endswith("/host-enrollment") and status == original_status:
            return altered_status, receipt
        return status, receipt

    monkeypatch.setattr(module, "request", tamper)
    with pytest.raises(module.common.RemoteError, match=error):
        module.execute(config, {}, identity_probe=probe)
    journal = next(Path(config["work_dir"]).glob("dev-box-*.json"))
    assert json.loads(journal.read_text())["host_identity"][0]["state"] == "intent"


def test_offline_host_identity_scenario_rejects_scoped_metadata_in_denials(host_journey, monkeypatch):
    module, config, probe, _operations, _observations, challenge = host_journey
    original = module.request

    def leak(*args, **kwargs):
        status, receipt = original(*args, **kwargs)
        if args[2].endswith("/host-enrollment") and status == 404:
            return 404, {"challenge": challenge}
        return status, receipt

    monkeypatch.setattr(module, "request", leak)
    with pytest.raises(module.common.RemoteError, match="disclosed machine scope"):
        module.execute(config, {}, identity_probe=probe)
