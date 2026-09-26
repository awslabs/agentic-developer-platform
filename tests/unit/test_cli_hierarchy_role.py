"""D03 role transitions use owned scope and restore before any hierarchy cleanup."""

import copy
import importlib.util
import json
from pathlib import Path
import urllib.error

import pytest


@pytest.fixture
def role_script(monkeypatch):
    remote = Path(__file__).parents[1] / "e2e/cli_uplift/remote"
    monkeypatch.syspath_prepend(str(remote))
    spec = importlib.util.spec_from_file_location(
        "role_scenario", remote / "hierarchy_role.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config(tmp_path):
    from tests.e2e.cli_uplift.remote.hierarchy_plan import recovery_plan

    cfg = {
        "evaluation_id": "role-test",
        "gateway_url": "https://gateway.example",
        "expected_revision": "a" * 40,
        "work_dir": str(tmp_path),
        "hierarchy_lifecycle": {
            "owned_mutations_authorized": True,
            "login_user_id": "admin",
            "canonical_user_id": "canonical-admin",
            "ordinary_login_user_id": "ordinary-native",
            "ordinary_canonical_user_id": "ordinary",
            "ordinary_native_tenant": "native",
            "tenant_id": "tenant",
            "role_transition": True,
            "exclusive_ordinary_fixture": True,
            "role_scope_release": "a" * 40,
        },
    }
    cfg["recovery_plan"] = recovery_plan(cfg)
    return cfg


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "lost_promotion_ack",
        "foreign_allowed",
        "owned_denied",
        "old_lease_stale",
        "restore_cas_failure",
        "role_drift",
        "native_drift",
        "initial_save_failure",
    ],
)
def test_role_authority_and_guarded_restoration(
    role_script, tmp_path, monkeypatch, fault
):
    cfg = config(tmp_path)
    plan = cfg["recovery_plan"]
    intent = plan["role_transition"]
    row = copy.deepcopy(plan["restore"])
    revision = [0]
    mutations, requests = [], []
    state = {"checks": []}
    native_calls = [0]

    def member():
        return {"revision": str(revision[0]), "resource": copy.deepcopy(row)}

    class Cli:
        def __init__(self, actor):
            self.actor = actor

        def json(self, args):
            if args == ["capabilities", "--refresh"]:
                return {"detail": {"gateway": {"state": "yes", "release": "a" * 40}}}
            if self.actor == "native":
                native_calls[0] += 1
                return {
                    "detail": {
                        "principal_id": "ordinary-native",
                        "tenant_id": "other"
                        if fault == "native_drift" and native_calls[0] > 1
                        else "native",
                    }
                }
            assert self.actor == "admin"
            if "--org" in args and args[args.index("--org") + 1] == "native":
                assert (
                    args[:3] == ["admin", "member", "remove"]
                    and args[-1] == "--dry-run"
                )
                return {
                    "detail": {
                        "before": {
                            "resource": copy.deepcopy(plan["restore"]),
                            "revision": "native-unchanged",
                        }
                    }
                }
            expected = args[args.index("--expected-revision") + 1]
            assert expected == str(revision[0])
            if "--dry-run" in args:
                return {"status": "dry_run"}
            assert "--yes" in args
            journal_path = tmp_path / (
                "hierarchy-role-" + intent["department_id"] + ".json"
            )
            durable = json.loads(journal_path.read_text())
            assert durable["attempts"][-1]["command"] == args[:-1]
            if args[1] == "member":
                role = args[args.index("--role") + 1]
                assert role in {"member", "dept_admin"}
                if role == "member" and fault == "restore_cas_failure":
                    raise role_script.common.RemoteError("CAS changed")
                row["role"] = role
                mutations.append(role)
                revision[0] += 1
                if role == "dept_admin" and fault == "lost_promotion_ack":
                    raise role_script.common.RemoteError("Lost acknowledgement")
                if role == "dept_admin" and fault == "role_drift":
                    row["role"] = "org_admin"
            else:
                assert args[1:3] == ["team", "members"]
                assert args[args.index("--team") + 1] == intent["team_id"]
                if args[3] == "add":
                    assert "--primary" in args
                    row["team_id"] = intent["team_id"]
                    row["teams"] = [
                        {
                            "team_id": intent["team_id"],
                            "role": "member",
                            "is_primary": True,
                        }
                    ]
                else:
                    assert (
                        row["role"] == "member"
                    )  # Demotion must precede team removal.
                    row["team_id"], row["teams"] = "", []
                mutations.append(args[3])
                revision[0] += 1
            return {"status": "ok"}

        def run(self, args, **kwargs):
            assert self.actor == "ordinary"
            department = args[args.index("--department") + 1]
            allowed = (
                row["role"] == "dept_admin" and department == intent["department_id"]
            )
            if row["role"] == "dept_admin" and fault == "foreign_allowed":
                allowed = True
            if fault == "owned_denied":
                allowed = False
            return (
                (0, {"status": "ok"})
                if allowed
                else (3, {"status": "failed", "error": {"http_status": 403}})
            )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def urlopen(request, **kwargs):
        assert request.headers["Authorization"] == "Bearer retained-private-token"
        requests.append(request.full_url)
        allowed = (
            row["role"] == "dept_admin"
            and "/" + intent["department_id"] + "/" in request.full_url
            and fault != "old_lease_stale"
        )
        if not allowed:
            raise urllib.error.HTTPError(request.full_url, 403, "Denied", {}, None)
        return Response()

    monkeypatch.setattr(role_script.urllib.request, "urlopen", urlopen)
    if fault == "initial_save_failure":
        monkeypatch.setattr(
            role_script.os,
            "fsync",
            lambda fd: (_ for _ in ()).throw(OSError("Cannot persist recovery")),
        )
    if fault:
        with pytest.raises((role_script.common.RemoteError, OSError)):
            role_script.exercise(
                cfg,
                state,
                Cli("admin"),
                Cli("ordinary"),
                Cli("native"),
                member,
                "retained-private-token",
            )
    else:
        role_script.require_release(cfg, Cli("admin"))
        role_script.exercise(
            cfg,
            state,
            Cli("admin"),
            Cli("ordinary"),
            Cli("native"),
            member,
            "retained-private-token",
        )
        assert len(state["checks"]) == 1
        assert [
            check["observed_http_status"]
            for check in state["role_transition"]["retained_lease_checks"]
        ] == [403, 200, 403, 403]
    if fault in {"restore_cas_failure", "role_drift"}:
        assert state["role_restoration"] == "pending"
        assert row["teams"] and "remove" not in mutations
    else:
        assert row == plan["restore"]
    if fault == "initial_save_failure":
        assert mutations == []
    elif fault == "native_drift":
        assert state["role_restoration"] == "pending"
    elif fault not in {"restore_cas_failure", "role_drift"}:
        assert state["role_restoration"] == "verified"
    assert "retained-private-token" not in json.dumps(state)
    assert not any("revoke" in str(item) for item in mutations)


@pytest.mark.parametrize("fault", ["missing", "wrong_expected", "wrong_live"])
def test_role_release_gate_refuses_before_mutations(role_script, tmp_path, fault):
    cfg = config(tmp_path)
    if fault == "missing":
        del cfg["hierarchy_lifecycle"]["role_scope_release"]
    if fault == "wrong_expected":
        cfg["expected_revision"] = "b" * 40

    class Admin:
        def json(self, args):
            assert args == ["capabilities", "--refresh"]
            return {"detail": {"gateway": {"state": "yes", "release": "b" * 40}}}

    with pytest.raises(role_script.common.RemoteError):
        role_script.require_release(cfg, Admin())


def test_role_plan_and_dispatch_are_explicit(tmp_path):
    from tests.e2e.cli_uplift import fixtures
    from tests.e2e.cli_uplift.remote.hierarchy_plan import recovery_plan

    cfg = config(tmp_path)
    assert (
        fixtures.parse(json.dumps({"hierarchy_lifecycle": cfg["hierarchy_lifecycle"]}))[
            "hierarchy_lifecycle"
        ]
        == cfg["hierarchy_lifecycle"]
    )
    assert (
        cfg["recovery_plan"]["role_transition"]["department_id"]
        != cfg["recovery_plan"]["role_transition"]["other_department_id"]
    )
    del cfg["hierarchy_lifecycle"]["role_scope_release"]
    with pytest.raises(ValueError):
        fixtures.validate_fixture("hierarchy_lifecycle", cfg["hierarchy_lifecycle"])
    del cfg["hierarchy_lifecycle"]["role_transition"]
    assert "role_transition" not in recovery_plan(cfg)


def test_role_recovery_intent_is_external_before_ssm(tmp_path):
    from tests.e2e.cli_uplift import cleanup, live

    cfg = config(tmp_path)
    persisted = []
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        "role",
        on_change=lambda document, **kwargs: persisted.append(copy.deepcopy(document)),
    )

    class Ssm:
        def json_result(self, *args, **kwargs):
            intent = persisted[-1]["diagnostic_intents"][
                "hierarchy_lifecycle:role-test"
            ]
            assert intent["role_transition"] == cfg["recovery_plan"]["role_transition"]
            assert intent["restore"] == {
                "membership_status": "active",
                "role": "member",
                "team_id": "",
                "teams": [],
            }
            raise RuntimeError("Instance lost after intent persistence")

    worker = live._run_worker(Ssm(), {}, lambda *args: None)
    with pytest.raises(RuntimeError, match="Instance lost"):
        worker("i-owned", "hierarchy_lifecycle", cfg, manifest=manifest)
