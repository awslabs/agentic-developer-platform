"""D05 owns only canonical metadata; uncertainty and drift cannot widen cleanup."""

import copy
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def scenario(monkeypatch):
    remote = Path(__file__).parents[1] / "e2e/cli_uplift/remote"
    monkeypatch.syspath_prepend(str(remote))
    spec = importlib.util.spec_from_file_location(
        "machine_scenario", remote / "machine_lifecycle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    return {
        "login_user_id": "verified-admin-login-subject",
        "canonical_user_id": "admin-canonical",
        "tenant_id": "selected",
        "ordinary_canonical_user_id": "ordinary-canonical",
        "ordinary_native_tenant": "native",
        "owned_mutations_authorized": True,
    }


def config(tmp_path):
    return {
        "evaluation_id": "owned-machine-test",
        "gateway_url": "https://gateway.example",
        "machine_lifecycle": fixture(),
        "test_user_id": fixture()["login_user_id"],
        "org_id": "native",
        "cli_path": "adp",
        "work_dir": str(tmp_path),
        "region": "us-east-1",
        "sts_endpoint": "https://sts.us-east-1.amazonaws.com",
    }


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "unknown_registration",
        "alias_failure",
        "replay_failure",
        "foreign_alias",
        "wrong_ordinary",
        "wrong_admin",
    ],
)
def test_owned_lifecycle_has_exact_cleanup_and_never_revokes_reusable_session(
    scenario, monkeypatch, tmp_path, fault
):
    cfg = config(tmp_path)
    cfg["recovery_plan"] = scenario.recovery_plan(cfg)
    plan = cfg["recovery_plan"]
    principal = "a3fafb8c-7416-45c2-9cef-e7d1a0d7b7ed"
    state = {"row": None, "version": 0, "calls": []}

    def fail(http, code="operation_failed"):
        return {"status": "failed", "error": {"http_status": http, "code": code}}

    def changed():
        state["version"] += 1
        state["row"]["revision"] = str(state["version"]).zfill(64)

    class Cli:
        def __init__(self, binary, env, transcript, **kwargs):
            self.actor = "ordinary" if Path(env["HOME"]).name == "ordinary" else "admin"
            self.tenant = env["ADP_TENANT"]
            assert env["BG_CONFIG_DIR"].startswith(env["HOME"])

        def json(self, args, **kwargs):
            state["calls"].append((self.actor, list(args)))
            if args[:3] == ["models", "mappings", "list"]:
                target = (
                    args[args.index("--service-principal") + 1]
                    if "--service-principal" in args
                    else fixture()["ordinary_canonical_user_id"]
                    if self.actor == "ordinary"
                    else fixture()["canonical_user_id"]
                )
                if (
                    fault == "wrong_ordinary"
                    and self.actor == "ordinary"
                    or fault == "wrong_admin"
                    and self.actor == "admin"
                ):
                    target = "foreign"
                return {
                    "status": "ok",
                    "detail": {"tenant_id": self.tenant, "principal_id": target},
                }
            if args[:2] == ["access", "status"]:
                return {
                    "status": "ok",
                    "detail": {
                        "tenant_id": args[-1],
                        "status": "approved",
                        "spend_eligibility": "not_evaluated",
                    },
                }
            if args[:3] == ["admin", "session", "revoke-user"]:
                assert "--dry-run" in args and "--yes" not in args
                if self.actor == "ordinary":
                    return fail(403)
                return {
                    "status": "preview",
                    "detail": {
                        "user_id": fixture()["ordinary_canonical_user_id"],
                        "org": fixture()["tenant_id"],
                        "credential_families": ["gateway_jwt"],
                        "cognito_sessions_revoked": False,
                        "revision": "family-revision",
                    },
                }
            assert args[:2] == ["admin", "service-principal"]
            if self.actor == "ordinary":
                return fail(403)
            if self.tenant != fixture()["tenant_id"]:
                return fail(404)
            action = args[2]
            if "--dry-run" in args:
                return {"status": "dry_run"}
            if action == "register":
                if args[args.index("--operation-id") + 1] == plan["duplicate_id"]:
                    return fail(422)
                if fault == "unknown_registration":
                    return {
                        "status": "pending",
                        "detail": {"operation_id": plan["registration_id"]},
                    }
                if fault == "replay_failure" and state["row"] is not None:
                    assert evidence["detail"]["principal_id"] == principal
                    raise scenario.common.RemoteError("Replay unavailable")
                if state["row"] is None:
                    state["row"] = {
                        "canonical_service_principal_id": principal,
                        "tenant_id": fixture()["tenant_id"],
                        "display_name": plan["display_name"],
                        "status": "active",
                        "aliases": [
                            {
                                "id": "alias-primary",
                                **plan["aliases"][0],
                                "is_active": True,
                            }
                        ],
                    }
                    changed()
                return {"status": "ok", "detail": copy.deepcopy(state["row"])}
            if action == "show":
                return {"status": "ok", "detail": copy.deepcopy(state["row"])}
            expected = args[args.index("--expected-revision") + 1]
            if expected != state["row"]["revision"]:
                return fail(409, "revision_conflict")
            if action == "alias-add":
                if fault == "alias_failure":
                    return {"status": "pending"}
                alias = {
                    "id": "alias-secondary",
                    **plan["aliases"][1],
                    "is_active": True,
                }
                if fault == "foreign_alias":
                    alias["alias_id"] = "someone-elses-alias"
                state["row"]["aliases"].append(alias)
                changed()
            elif action == "alias-remove":
                row_id = args[args.index("--alias-row-id") + 1]
                next(row for row in state["row"]["aliases"] if row["id"] == row_id)[
                    "is_active"
                ] = False
                changed()
            elif action == "status":
                if state["row"]["status"] == "retired":
                    return fail(422)
                state["row"]["status"] = args[args.index("--status") + 1]
                changed()
            return {"status": "ok", "detail": copy.deepcopy(state["row"])}

        def run(self, args, **kwargs):
            value = self.json(args, **kwargs)
            return (
                0
                if value["status"] == "ok"
                else 4
                if value["status"] == "pending"
                else 5
            ), value

    monkeypatch.setattr(scenario.common, "Cli", Cli)
    monkeypatch.setattr(scenario.common, "session_tokens", lambda _: {})
    monkeypatch.setattr(scenario, "ordinary_tokens", lambda *args: {})
    monkeypatch.setattr(scenario, "_write_session", lambda *args: None)
    evidence = {"transcript": []}
    if fault:
        with pytest.raises(scenario.common.RemoteError):
            scenario.execute(cfg, evidence)
    else:
        scenario.execute(cfg, evidence)
        assert len(evidence["detail"]["checks"]) == 6
    if fault in {None, "alias_failure", "replay_failure"}:
        assert state["row"]["status"] == "retired"
        assert all(not row["is_active"] for row in state["row"]["aliases"])
        assert (
            evidence["detail"]["cleanup"] == "retired_aliases_revoked_history_retained"
        )
    elif fault == "foreign_alias":
        assert state["row"]["status"] == "active"
        assert not any(
            args[2] == "alias-remove"
            for _, args in state["calls"]
            if args[:2] == ["admin", "service-principal"]
        )
    else:
        assert state["row"] is None
    assert not any(
        "--yes" in args
        for _, args in state["calls"]
        if args[:2] == ["admin", "session"]
    )
    assert not any(
        "request" in args for _, args in state["calls"] if args[0] == "access"
    )


@pytest.mark.parametrize(
    "fault", ["instance_loss", "changed_alias", "no_sink", "no_manifest"]
)
def test_caller_manifest_retains_original_registration_and_aliases_before_ssm(
    scenario, tmp_path, fault
):
    from tests.e2e.cli_uplift import cleanup, live
    from tests.e2e.cli_uplift.ports import PortError

    cfg = config(tmp_path)
    cfg["recovery_plan"] = scenario.recovery_plan(cfg)
    retained, calls = [], []

    def persist(document, **kwargs):
        if fault == "no_sink":
            raise RuntimeError("External sink unavailable")
        retained.append(copy.deepcopy(document))

    manifest = cleanup.Manifest(
        tmp_path / "manifest.json", "machine", on_change=persist
    )

    class Ssm:
        def json_result(self, *args, **kwargs):
            calls.append("ssm")
            assert retained[-1]["diagnostic_intents"][
                "machine_lifecycle:owned-machine-test"
            ] == scenario.recovery_plan(cfg)
            raise RuntimeError("Worker lost")

    if fault == "changed_alias":
        cfg["recovery_plan"]["aliases"][0]["alias_id"] = "foreign"
    worker = live._run_worker(Ssm(), {}, lambda *args: calls.append("install"))
    with pytest.raises((RuntimeError, ValueError, PortError)):
        worker(
            "i-owned",
            "machine_lifecycle",
            cfg,
            manifest=None if fault == "no_manifest" else manifest,
        )
    assert calls == (["install", "ssm"] if fault == "instance_loss" else [])


def test_explicit_fixture_and_report_contract_include_diagnostic_only():
    from tests.e2e.cli_uplift import cases, fixtures, report

    value = fixture()
    fixtures.validate_fixture("machine_lifecycle", value)
    assert cases.BY_ID["D05"] not in cases.suite_cases("nightly")
    assert cases.BY_ID["D05"] not in cases.suite_cases("full")
    assert (
        "machine-lifecycle"
        in report.load_schema()["properties"]["suites"]["items"]["enum"]
    )
    value["ordinary_canonical_user_id"] = value["canonical_user_id"]
    with pytest.raises(ValueError):
        fixtures.validate_fixture("machine_lifecycle", value)
