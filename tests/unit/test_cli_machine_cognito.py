"""Owned Cognito qualification preserves original identities and private secrets."""

import copy
import importlib.util
import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def scenario(monkeypatch):
    remote = Path(__file__).parents[1] / "e2e/cli_uplift/remote"
    monkeypatch.syspath_prepend(str(remote))
    spec = importlib.util.spec_from_file_location(
        "cognito_scenario", remote / "machine_cognito.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "lost_ack",
        "unknown_provider",
        "secret_rotated",
        "insecure_file",
        "foreign_allowed",
        "ordinary_allowed",
        "wrong_name",
        "retire_failed",
        "initial_save_failed",
        "retirement_save_failed",
        "retired_reminted",
    ],
)
def test_owned_cognito_lifecycle_and_cleanup(scenario, tmp_path, fault):
    plan = {
        "name": "owned-cognito-fixture",
        "registration_id": "original-operation",
        "retirement_id": "original-retire",
        "scopes": ["bedrockgw/invoke"],
    }
    state, saved, calls = {}, [], []
    row = {
        "id": "owned-client",
        "client_id": "owned-client",
        "org_id": "tenant",
        "identity_type": "cognito-client",
        "name": plan["name"],
        "scopes": plan["scopes"],
        "revision": "active-revision",
        "status": "active",
    }
    secret = "private-fixture-secret"
    registrations = []

    def save():
        if fault == "initial_save_failed" and not saved:
            raise OSError("durable sink unavailable")
        if (
            fault == "retirement_save_failed"
            and state["cognito"]["cleanup"] == "retirement_pending"
        ):
            raise OSError("durable sink unavailable")
        saved.append(copy.deepcopy(state))

    def refused(http):
        return 3, {"status": "failed", "error": {"message": f"HTTP {http}"}}

    class Cli:
        def __init__(self, actor):
            self.actor = actor

        def json(self, argv):
            code, payload = self.run(argv)
            assert code == 0
            return payload

        def run(self, argv, **kwargs):
            calls.append((self.actor, argv))
            action = argv[2]
            if action == "show":
                if self.actor != "admin":
                    if fault == self.actor + "_allowed":
                        return 0, {"status": "ok", "detail": copy.deepcopy(row)}
                    return refused(403 if self.actor == "ordinary" else 404)
                result = copy.deepcopy(row)
                if fault == "wrong_name":
                    result["name"] = "unrelated"
                return 0, {"status": "ok", "detail": result}
            if action == "register":
                path = Path(argv[argv.index("--credential-file") + 1])
                if "--dry-run" in argv:
                    return 0, {"status": "dry_run"}
                assert (
                    saved[-1]["cognito"]["phase"] == "registration_attempted"
                    or row["status"] == "retired"
                    or path.exists()
                )
                assert argv[argv.index("--operation-id") + 1] == plan["registration_id"]
                if path.exists():
                    return 1, {"status": "failed", "error": {"code": "cli_error"}}
                path.touch(mode=0o600)
                if row["status"] == "retired" and fault != "retired_reminted":
                    return 4, {"status": "pending", "detail": {"target": row["id"]}}
                registrations.append(argv)
                if fault == "unknown_provider":
                    return 4, {"status": "pending"}
                delivered = secret + (
                    "rotated"
                    if fault == "secret_rotated" and len(registrations) == 2
                    else ""
                )
                path.write_text(
                    json.dumps({"client_id": row["id"], "client_secret": delivered})
                )
                if fault == "insecure_file":
                    os.chmod(path, 0o644)
                if fault == "lost_ack" and len(registrations) == 1:
                    return 5, None
                return 0, {"status": "ok", "detail": copy.deepcopy(row)}
            assert action == "deregister" and argv[3] == row["id"]
            assert argv[argv.index("--operation-id") + 1] == plan["retirement_id"]
            assert saved[-1]["cognito"]["cleanup"] == "retirement_pending"
            assert (
                saved[-1]["cognito"]["retirement_expected_revision"] == row["revision"]
            )
            if fault != "retire_failed":
                row.update(status="retired", revision="retired-revision")
            return 4, {
                "status": "pending"
            }  # Even lost cleanup ACK reconciles by readback.

    def run():
        scenario.execute(
            Cli("admin"),
            Cli("ordinary"),
            Cli("foreign"),
            "tenant",
            "native",
            plan,
            tmp_path,
            state,
            save,
        )

    if fault in (None, "lost_ack"):
        run()
        assert state["cognito"]["phase"] == "complete"
        assert len(state["cognito"]["checks"]) == 4
    else:
        with pytest.raises((scenario.common.RemoteError, OSError)):
            run()
    if fault == "initial_save_failed":
        assert not calls
    elif fault in {"unknown_provider", "wrong_name", "retirement_save_failed"}:
        assert not any(argv[2] == "deregister" for _, argv in calls)
        assert state["cognito"]["cleanup"] in {
            "reconcile_original_registration_operation",
            "retirement_pending",
        }
    elif fault != "retire_failed":
        assert row["status"] == "retired"
    assert secret not in json.dumps(saved)
    assert secret not in json.dumps(calls)
    assert all(argv[2] in {"show", "register", "deregister"} for _, argv in calls)


def test_cognito_plan_is_opt_in_and_contains_stable_cleanup_identity():
    from tests.e2e.cli_uplift.remote.machine_lifecycle_plan import recovery_plan

    config = {
        "evaluation_id": "one",
        "gateway_url": "https://gateway",
        "machine_lifecycle": {"tenant_id": "tenant"},
    }
    assert "cognito" not in recovery_plan(config)
    config["machine_lifecycle"]["cognito_lifecycle"] = True
    first = recovery_plan(config)
    assert first == recovery_plan(config)
    assert first["cognito"]["registration_id"] != first["cognito"]["retirement_id"]
    assert first["cognito"]["name"].startswith("owned-cognito-")


def test_cognito_recovery_intent_is_external_before_ssm(tmp_path):
    from tests.e2e.cli_uplift import cleanup, live
    from tests.e2e.cli_uplift.remote.machine_lifecycle_plan import recovery_plan

    cfg = {
        "evaluation_id": "owned-client",
        "gateway_url": "https://gateway",
        "machine_lifecycle": {"tenant_id": "tenant", "cognito_lifecycle": True},
    }
    cfg["recovery_plan"] = recovery_plan(cfg)
    persisted = []
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        "machine",
        on_change=lambda document, **kwargs: persisted.append(copy.deepcopy(document)),
    )

    class Ssm:
        def json_result(self, *args, **kwargs):
            intent = persisted[-1]["diagnostic_intents"][
                "machine_lifecycle:owned-client"
            ]
            assert intent["cognito"] == cfg["recovery_plan"]["cognito"]
            raise RuntimeError("Instance lost after persistence")

    worker = live._run_worker(Ssm(), {}, lambda *args: None)
    with pytest.raises(RuntimeError, match="Instance lost"):
        worker("i-owned", "machine_lifecycle", cfg, manifest=manifest)


@pytest.mark.parametrize(
    "selection,valid", [(True, True), (False, True), ("true", False), (1, False)]
)
def test_cognito_selection_requires_explicit_boolean(selection, valid):
    from tests.e2e.cli_uplift import config, fixtures

    payload = json.dumps(
        {
            "machine_lifecycle": {
                "tenant_id": "tenant",
                "canonical_user_id": "admin",
                "login_user_id": "admin-login",
                "ordinary_canonical_user_id": "ordinary",
                "ordinary_native_tenant": "native",
                "owned_mutations_authorized": True,
                "cognito_lifecycle": selection,
            }
        }
    )
    if valid:
        assert (
            fixtures.parse(payload)["machine_lifecycle"]["cognito_lifecycle"]
            is selection
        )
    else:
        with pytest.raises(config.ConfigError, match="Cognito lifecycle selection"):
            fixtures.parse(payload)
