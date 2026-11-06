"""Synthetic CLI scope contracts; these cannot prove an installed-client run."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import sys
from pathlib import Path

import pytest

from tests.e2e.cli_uplift import bundle, cases, cleanup, stages

REMOTE = Path(__file__).resolve().parents[1] / "e2e/cli_uplift/remote"
sys.path.insert(0, str(REMOTE))
product_scopes = importlib.import_module("product_scopes")
dispatcher = importlib.import_module("dispatcher")
common = importlib.import_module("common")


class ScopeCli:
    def __init__(self, profiles):
        self.profiles = deepcopy(profiles)
        self.calls = []

    def run(self, arguments):
        self.calls.append(arguments)
        assert arguments[:2] == ["product", "show"]
        project_id = arguments[arguments.index("--project") + 1]
        return 0, {"status": "ok", "detail": deepcopy(self.profiles[project_id])}


@pytest.fixture
def scope_fixture(tmp_path):
    binary = tmp_path / "adp"
    helper = tmp_path / "adp-product.py"
    binary.write_text("pinned installed wrapper")
    helper.write_text("pinned Product command")
    scopes = [
        {
            "project_id": project_id, "name": name, "progress": {"completed": completed},
            "configuration": {
                "basic": {"repositories": [{
                    "tenant_id": "owned-tenant", "project_id": project_id,
                    "source_connection_id": connection_id, "record_type": "repository",
                    "record_id": "repository-" + project_id, "record_revision": "source-etag:1",
                }]},
                "financials": {"release_target_date": plan_date},
            },
        }
        for project_id, name, connection_id, plan_date, completed in (
            ("first", "First scope", "connection-first", "2026-12-01", 0),
            ("second", "Second scope", "connection-second", "2027-12-01", 1),
        )
    ]
    config = {
        "cli_path": str(binary),
        "expected_hashes": {
            name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
            for name in ("adp", "adp-product.py")
        },
        "evaluation_id": "run-one", "org_id": "owned-tenant", "test_user_id": "owner",
        "expected_revision": "a" * 40,
        "product_scopes": {
            "owned_fixture": True, "run_id": "run-one", "tenant_id": "owned-tenant",
            "login_user_id": "owner", "actor_id": "owner", "source_revision": "a" * 40,
            "product_id": "product-one", "product_name": "Planner", "scopes": scopes,
        },
    }
    profiles = {
        scope["project_id"]: {
            "product_id": "product-one", "project_id": scope["project_id"],
            "tenant_id": "owned-tenant", "product": {
                "name": "Planner", "owner_ref": {"kind": "user", "id": "owner"},
            },
            "project": {"id": scope["project_id"], "name": scope["name"]},
            "configuration": deepcopy(scope["configuration"]),
            "progress": deepcopy(scope["progress"]), "revision": 3,
        }
        for scope in scopes
    }
    return config, profiles


def scope_readback(profiles):
    paths = []

    def readback(path):
        paths.append(path)
        if path == "/api/v1/products":
            return 200, {"items": [{"id": "product-one", "project_ids": ["first", "second"]}], "next_cursor": None}
        return 200, deepcopy(profiles[path.rsplit("/", 1)[-1]])

    return readback, paths


def test_installed_driver_selects_each_scope_and_independently_reads_it(scope_fixture):
    config, profiles = scope_fixture
    cli = ScopeCli(profiles)
    readback, paths = scope_readback(profiles)
    evidence = {"transcript": []}
    product_scopes.verify_scope_reads(config, cli, readback, evidence)
    assert [call[call.index("--project") + 1] for call in cli.calls] == ["first", "second", "first"]
    assert paths == [
        "/api/v1/products",
        "/api/v1/products/product-one/projects/first",
        "/api/v1/products/product-one/projects/second",
        "/api/v1/products/product-one/projects/first",
    ]
    assert evidence["success"] is True and len(evidence["api_readback"]) == 3
    assert set(evidence["installed_hashes"]) == {"adp", "adp-product.py"}


@pytest.mark.parametrize("bad_value", [None, "wrong", False])
def test_unowned_or_stale_scope_fixture_cannot_dispatch(scope_fixture, bad_value):
    config, profiles = scope_fixture
    config["product_scopes"]["owned_fixture"] = bad_value
    cli = ScopeCli(profiles)
    with pytest.raises(common.RemoteError, match="run-owned"):
        product_scopes.verify_scope_reads(config, cli, lambda path: pytest.fail("Unexpected API read"), {})
    assert not cli.calls


@pytest.mark.parametrize("changed_field,wrong_value", [
    ("project_id", "first"), ("configuration", {}), ("progress", {"completed": 0}),
    ("product", {"name": "Other", "owner_ref": {"kind": "user", "id": "other"}}),
])
def test_wrong_installed_scope_refuses_success(scope_fixture, changed_field, wrong_value):
    config, profiles = scope_fixture
    cli = ScopeCli(profiles)
    cli.profiles["second"][changed_field] = wrong_value
    readback, _ = scope_readback(profiles)
    with pytest.raises(common.RemoteError, match="independent API"):
        product_scopes.verify_scope_reads(config, cli, readback, {"transcript": []})


def test_new_scope_case_is_selected_by_real_runner_and_dispatcher():
    chosen = cases.suite_cases("case:PR04-C01")
    assert "PR04-C01" in {case.id for case in chosen}
    assert stages.JOURNEY_DRIVERS["PR04-C01"] == "product_scope_isolation"
    assert "product_scope_isolation" in bundle.purposes()
    module, defaults = dispatcher.load("product_scope_isolation")
    assert module is product_scopes and defaults == {"mode": "isolation"}
    denied, defaults = dispatcher.load("product_scope_denials")
    assert denied is product_scopes and defaults == {"mode": "denials"}
    assert stages.JOURNEY_DRIVERS["PR04-C02"] == "product_scope_denials"
    assert "PR04-C02" in {case.id for case in cases.suite_cases("case:PR04-C02")}
    assert callable(module.execute)
    with pytest.raises(ValueError, match="Unknown CLI case"):
        cases.suite_cases("case:PR04-C99")


def test_orchestrator_refuses_false_green_and_retains_scope_attempt(scope_fixture):
    config, profiles = scope_fixture
    cli = ScopeCli(profiles)
    readback, _ = scope_readback(profiles)
    evidence = {"transcript": []}
    product_scopes.verify_scope_reads(config, cli, readback, evidence)
    evidence["cli_events"] = [
        {
            "exit_code": 0, "argv": ["adp", *arguments, "--json"],
            "output": {"status": "ok", "detail": deepcopy(profiles[arguments[-1]])},
        }
        for arguments in cli.calls
    ]
    outcomes = []
    context = {
        "document": {"instance_id": "synthetic-host"},
        "matrix": cases.new_matrix(("case:PR04-C01",)),
        "record": lambda *result: outcomes.append(result),
        "attempt_id": "run-one/1", "expected_hashes": config["expected_hashes"],
        "fault": "none", "transcript": [], "correlation": {},
    }
    stage = stages.journeys_stage(
        {"product_scopes": config["product_scopes"], "expected_revision": config["expected_revision"]},
        {"journey": lambda purpose: lambda instance, state: deepcopy(evidence)},
    )
    stage(context)
    assert outcomes[-1][:2] == ("PR04-C01", cases.PASSED)
    assert context["document"]["product_scope_attempts"][0]["status"] == "passed"

    evidence["api_readback"].pop()
    context["document"]["product_scope_attempts"].clear()
    outcomes.clear()
    stage(context)
    assert outcomes[-1][:2] == ("PR04-C01", cases.FAILED)
    assert context["document"]["product_scope_attempts"][0]["status"] == "failed"


def test_missing_owned_scope_fixture_blocks_before_dispatch():
    outcomes = []
    context = {
        "document": {"instance_id": "synthetic-host"},
        "matrix": cases.new_matrix(("case:PR04-C01",)),
        "record": lambda *result: outcomes.append(result),
    }
    stage = stages.journeys_stage(
        {"product_scopes": {}},
        {"journey": lambda purpose: pytest.fail("No protected fixture; worker must not be dispatched")},
    )
    stage(context)
    assert outcomes == [
        ("PR04-C01", cases.BLOCKED, {"detail": "Protected run-owned two-scope CLI fixture is unavailable"}),
    ]
    assert "product_scope_attempts" not in context["document"]


@pytest.mark.parametrize("field", ["connection", "plan", "progress"])
def test_installed_cli_and_api_agreement_cannot_hide_other_scope_values(scope_fixture, field):
    config, profiles = scope_fixture
    changed = deepcopy(profiles)
    if field == "connection":
        changed["second"]["configuration"]["basic"]["repositories"] = deepcopy(
            profiles["first"]["configuration"]["basic"]["repositories"]
        )
    elif field == "plan":
        changed["second"]["configuration"]["financials"] = deepcopy(
            profiles["first"]["configuration"]["financials"]
        )
    else:
        changed["second"]["progress"] = deepcopy(profiles["first"]["progress"])
    cli = ScopeCli(changed)
    readback, _ = scope_readback(changed)
    with pytest.raises(common.RemoteError, match="protected fixture"):
        product_scopes.verify_scope_reads(config, cli, readback, {"transcript": []})


@pytest.mark.parametrize("field", ["connection_project", "same_connection", "same_plan", "same_progress"])
def test_crossed_scope_fixture_blocks_cli_before_io(scope_fixture, field):
    config, profiles = scope_fixture
    scopes = config["product_scopes"]["scopes"]
    if field == "connection_project":
        scopes[1]["configuration"]["basic"]["repositories"][0]["project_id"] = scopes[0]["project_id"]
    elif field == "same_connection":
        scopes[1]["configuration"]["basic"]["repositories"][0]["source_connection_id"] = (
            scopes[0]["configuration"]["basic"]["repositories"][0]["source_connection_id"]
        )
    elif field == "same_plan":
        scopes[1]["configuration"]["financials"]["release_target_date"] = (
            scopes[0]["configuration"]["financials"]["release_target_date"]
        )
    else:
        scopes[1]["progress"] = deepcopy(scopes[0]["progress"])
    cli = ScopeCli(profiles)
    with pytest.raises(common.RemoteError, match="run-owned"):
        product_scopes.verify_scope_reads(config, cli, lambda path: pytest.fail("Unexpected API read"), {})
    assert not cli.calls


def test_denial_journey_uses_actual_installed_commands_and_independent_readback(scope_fixture):
    config, profiles = scope_fixture
    fixture = config["product_scopes"]
    fixture.update({"editor_user_id": "editor", "foreign_user_id": "foreign",
                    "foreign_tenant_id": "foreign-tenant", "foreign_project_id": "foreign-project"})
    calls = []

    class ActorCli:
        def __init__(self, actor):
            self.actor = actor

        def run(self, arguments, expected=None):
            calls.append((self.actor, arguments))
            if self.actor == "editor" and arguments[1] == "show":
                return 0, {"status": "ok", "detail": deepcopy(profiles[arguments[arguments.index("--project") + 1]])}
            if arguments[1:] in (["list"], ["project", "list"]):
                return 0, {"status": "ok", "detail": {"items": [], "next_cursor": None}}
            error_code = "permission_denied" if arguments[1] == "update" else "not_found"
            return (3 if error_code == "permission_denied" else 5), {
                "status": "failed", "error": {"code": error_code},
            }

    readback, paths = scope_readback(profiles)
    evidence = {}
    product_scopes.verify_scope_denials(config, ActorCli("editor"), ActorCli("foreign"), readback, evidence)
    assert evidence["success"] is True
    assert len(evidence["denials"]) == 8
    assert len(evidence["api_readback"]) == 4
    assert len(paths) == 4
    assert {tuple(arguments[1:3]) for _, arguments in calls if arguments[1] == "project"} == {
        ("project", "bindings"), ("project", "bind"), ("project", "list")}
    assert any(actor == "editor" and args[1] == "update" and args[args.index("--revision") + 1] == "3"
               for actor, args in calls)
    assert any(actor == "editor" and args[1:3] == ["project", "bind"] for actor, args in calls)
    assert any(actor == "foreign" and args[1:3] == ["project", "bindings"] for actor, args in calls)


def test_denial_journey_refuses_an_unexpected_success(scope_fixture):
    config, profiles = scope_fixture
    fixture = config["product_scopes"]
    fixture.update({"editor_user_id": "editor", "foreign_user_id": "foreign",
                    "foreign_tenant_id": "foreign-tenant", "foreign_project_id": "foreign-project"})

    class UnexpectedSuccess:
        def run(self, arguments, expected=None):
            if arguments[1] == "show":
                return 0, {"status": "ok", "detail": deepcopy(profiles[arguments[-1]])}
            return 0, {"status": "ok", "detail": {}}

    readback, _ = scope_readback(profiles)
    with pytest.raises(common.RemoteError, match="denial"):
        product_scopes.verify_scope_denials(config, UnexpectedSuccess(), UnexpectedSuccess(), readback, {})


@pytest.mark.parametrize("invalid", ["missing", "different_ref", "reused", "wrong_owner", "no_sink", "no_deleter"])
def test_denial_stage_blocks_without_verified_recovery(scope_fixture, tmp_path, invalid):
    config, _ = scope_fixture
    fixture = config["product_scopes"]
    fixture.update({"editor_fixture_name": "editor-login", "foreign_fixture_name": "foreign-login",
                    "editor_user_id": "editor", "foreign_user_id": "foreign", "foreign_tenant_id": "foreign-tenant",
                    "foreign_project_id": "foreign-project", "recovery_manifest_ref": "run-one"})
    manifest = cleanup.Manifest(tmp_path / "owned" / "manifest.json", "run-one",
                                on_change=None if invalid == "no_sink" else lambda *_args, **_kwargs: None)
    if invalid != "missing":
        plan = {"evaluation_id": "run-one", "tenant_id": "owned-tenant", "product_id": "product-one",
                "project_ids": ["first", "second"]}
        manifest.record("product_fixture", "run-one", reused=invalid == "reused", detail=plan)
    if invalid == "different_ref":
        fixture["recovery_manifest_ref"] = "unrelated-record"
    outcomes = []
    context = {"document": {"instance_id": "synthetic-host", "session": {
        "org_id": "owned-tenant", "user_id": "unrelated" if invalid == "wrong_owner" else "owner"}},
        "matrix": cases.new_matrix(("case:PR04-C02",)), "record": lambda *result: outcomes.append(result),
        "evaluation_id": "run-one", "manifest": manifest}
    stage = stages.journeys_stage({"product_scopes": fixture}, {
        "deleters": {} if invalid == "no_deleter" else {"product_fixture": lambda *_args: None},
        "journey": lambda _purpose: pytest.fail("Unverified recovery must block worker dispatch")})
    stage(context)
    assert outcomes[0][:2] == ("PR04-C02", cases.BLOCKED)
    assert manifest.read().get("diagnostic_intents") is None


def test_denial_stage_persists_matching_recovery_before_dispatch(scope_fixture, tmp_path):
    config, _ = scope_fixture
    fixture = config["product_scopes"]
    fixture.update({"editor_fixture_name": "editor-login", "foreign_fixture_name": "foreign-login",
                    "editor_user_id": "editor", "foreign_user_id": "foreign", "foreign_tenant_id": "foreign-tenant",
                    "foreign_project_id": "foreign-project", "recovery_manifest_ref": "run-one"})
    stored = []
    manifest = cleanup.Manifest(tmp_path / "owned" / "manifest.json", "run-one",
                                on_change=lambda document, *, critical: stored.append((deepcopy(document), critical)))
    plan = {"evaluation_id": "run-one", "tenant_id": "owned-tenant", "product_id": "product-one",
            "project_ids": ["first", "second"]}
    manifest.record("product_fixture", "run-one", detail=plan)
    outcomes = []
    context = {"document": {"instance_id": "synthetic-host", "session": {
        "org_id": "owned-tenant", "user_id": "owner"}},
        "matrix": cases.new_matrix(("case:PR04-C02",)), "record": lambda *result: outcomes.append(result),
        "evaluation_id": "run-one", "manifest": manifest,
        "attempt_id": "run-one/1", "expected_hashes": config["expected_hashes"],
        "fault": "none", "transcript": [], "correlation": {}}

    def worker(purpose):
        assert purpose == "product_scope_denials"

        def dispatch(_instance, _context):
            assert stored[-1] == (manifest.read(), True)
            assert stored[-1][0]["diagnostic_intents"]["PR04-C02:run-one"] == plan
            return {"success": True, "denials": [], "api_readback": []}

        return dispatch

    stage = stages.journeys_stage({"product_scopes": fixture, "expected_revision": config["expected_revision"]}, {
        "deleters": {"product_fixture": lambda *_args: None}, "journey": worker})
    stage(context)
    assert outcomes[-1][:2] == ("PR04-C02", cases.FAILED)
    assert context["document"]["product_scope_attempts"][0]["status"] == "failed"


def test_denial_stage_does_not_dispatch_when_durable_intent_fails(scope_fixture, tmp_path):
    config, _ = scope_fixture
    fixture = config["product_scopes"]
    fixture.update({"editor_fixture_name": "editor-login", "foreign_fixture_name": "foreign-login",
                    "editor_user_id": "editor", "foreign_user_id": "foreign", "foreign_tenant_id": "foreign-tenant",
                    "foreign_project_id": "foreign-project", "recovery_manifest_ref": "run-one"})
    writes = []

    def store(_document, *, critical):
        writes.append(critical)
        if len(writes) > 1:
            raise OSError("Durable recovery store unavailable")

    manifest = cleanup.Manifest(tmp_path / "owned" / "manifest.json", "run-one", on_change=store)
    manifest.record("product_fixture", "run-one", detail={
        "evaluation_id": "run-one", "tenant_id": "owned-tenant", "product_id": "product-one",
        "project_ids": ["first", "second"],
    })
    context = {"document": {"instance_id": "synthetic-host", "session": {
        "org_id": "owned-tenant", "user_id": "owner"}},
        "matrix": cases.new_matrix(("case:PR04-C02",)), "record": lambda *_args: None,
        "evaluation_id": "run-one", "manifest": manifest}
    stage = stages.journeys_stage({"product_scopes": fixture}, {
        "deleters": {"product_fixture": lambda *_args: None},
        "journey": lambda _purpose: pytest.fail("A failed durable write must prevent dispatch")})
    with pytest.raises(OSError, match="Durable recovery store unavailable"):
        stage(context)
    assert writes == [True, True]


@pytest.mark.parametrize(("command", "effect"), [
    ("update", "ownership_transfer"),
    ("update", "partial_configuration"),
    ("bind", "partial_configuration"),
])
def test_denied_cli_write_with_hidden_effect_fails_independent_readback(scope_fixture, command, effect):
    config, profiles = scope_fixture
    config["product_scopes"].update({
        "editor_user_id": "editor", "foreign_user_id": "foreign",
        "foreign_tenant_id": "foreign-tenant", "foreign_project_id": "foreign-project",
    })
    state = deepcopy(profiles)
    readback, _ = scope_readback(state)

    class ActorCli:
        def __init__(self, actor):
            self.actor = actor

        def run(self, arguments, expected=None):
            if self.actor == "editor" and arguments[1] == "show":
                project_id = arguments[arguments.index("--project") + 1]
                return 0, {"status": "ok", "detail": deepcopy(state[project_id])}
            if arguments[1:] in (["list"], ["project", "list"]):
                return 0, {"status": "ok", "detail": {"items": [], "next_cursor": None}}
            denied_write = self.actor == "editor" and (
                (command == "update" and arguments[1] == "update")
                or (command == "bind" and arguments[1:3] == ["project", "bind"])
            )
            if denied_write:
                if effect == "ownership_transfer":
                    state["first"]["product"]["owner_ref"] = {"kind": "user", "id": "editor"}
                else:
                    state["second"]["configuration"]["financials"]["release_target_date"] = "2029-01-01"
            error_code = "permission_denied" if arguments[1] == "update" else "not_found"
            return (3 if error_code == "permission_denied" else 5), {
                "status": "failed", "error": {"code": error_code},
            }

    with pytest.raises(common.RemoteError, match="Denied Product commands changed durable state"):
        product_scopes.verify_scope_denials(config, ActorCli("editor"), ActorCli("foreign"), readback, {})


@pytest.mark.parametrize("leak", [
    "list_count", "list_top_level_count", "list_cursor", "history_count",
    "history_message", "history_scope_id", "binding_history_count",
])
def test_foreign_cli_count_and_history_metadata_cannot_pass(scope_fixture, leak):
    config, profiles = scope_fixture
    config["product_scopes"].update({
        "editor_user_id": "editor", "foreign_user_id": "foreign",
        "foreign_tenant_id": "foreign-tenant", "foreign_project_id": "foreign-project",
    })
    readback, _ = scope_readback(profiles)

    class ActorCli:
        def __init__(self, actor):
            self.actor = actor

        def run(self, arguments, expected=None):
            if self.actor == "editor" and arguments[1] == "show":
                project_id = arguments[arguments.index("--project") + 1]
                return 0, {"status": "ok", "detail": deepcopy(profiles[project_id])}
            if arguments[1:] in (["list"], ["project", "list"]):
                envelope = {"status": "ok", "detail": {"items": [], "next_cursor": None}}
                if leak == "list_count":
                    envelope["detail"]["total"] = 2
                elif leak == "list_top_level_count":
                    envelope["total"] = 2
                elif leak == "list_cursor":
                    envelope["detail"]["next_cursor"] = "opaque-foreign-count"
                return 0, envelope
            error_code = "permission_denied" if arguments[1] == "update" else "not_found"
            envelope = {"status": "failed", "error": {"code": error_code}}
            if arguments[1] == "history":
                if leak == "history_count":
                    envelope["error"]["total"] = 2
                elif leak == "history_message":
                    envelope["error"]["message"] = "2 matching changes"
                elif leak == "history_scope_id":
                    envelope["error"]["message"] = "product-one has history"
            elif arguments[1:3] == ["project", "bindings"] and leak == "binding_history_count":
                envelope["detail"] = {"total": 2}
            return (3 if error_code == "permission_denied" else 5), envelope

    with pytest.raises(common.RemoteError, match="disclosed"):
        product_scopes.verify_scope_denials(config, ActorCli("editor"), ActorCli("foreign"), readback, {})


@pytest.mark.parametrize(("fault", "reason"), [
    (None, None),
    ("stale_closed", "Unlink failed to preserve"),
    ("rewritten_link", "Rebind changed"),
    ("stale_rebound", "Rebind changed"),
    ("missing_original_project", "Unlink removed the original Project"),
    ("recreated_asset", "Rebind changed"),
])
def test_dated_cli_history_and_owned_recovery_manifest(scope_fixture, tmp_path, fault, reason):
    config, profiles = scope_fixture
    fixture = config["product_scopes"]
    fixture["recovery_manifest_ref"] = "run-one"
    fixture["history"] = {
        "project_id": "second", "relationship_id": "link-original", "linked_revision": 2,
        "valid_from": "2026-02-01T00:00:00Z", "unbound_at": "2026-02-02T00:00:00Z",
        "rebound_at": "2026-02-03T00:00:00Z",
    }
    pushed = []
    manifest = cleanup.Manifest(tmp_path / "owned" / "manifest.json", "run-one",
                                on_change=lambda document, *, critical: pushed.append((deepcopy(document), critical)))
    manifest.record("product_fixture", "run-one", detail={
        "evaluation_id": "run-one", "product_id": "product-one", "project_ids": ["first", "second"],
    })
    manifest.record("adp_connection", "connection-second", reused=True)
    state = {"revision": 3, "bound": True, "history": [{"revision": 2, "relationship_versions": [{
        "relationship_id": "link-original", "relationship_revision": "1",
        "valid_from": "2026-02-01T00:00:00Z",
    }]}]}
    calls = []

    def profile(project_id):
        observed = {**deepcopy(profiles[project_id]), "revision": state["revision"]}
        if fault == "recreated_asset" and project_id == "second" and state["revision"] == 5:
            observed["configuration"] = {"governance": {"criticality": "high"}}
        return observed

    def readback(path):
        if path == "/api/v1/products/product-one/projects/second/project-bindings":
            return 200, {"items": deepcopy(state["history"])}
        if path == "/api/v1/products/product-one/projects/second/bindings":
            return 200, {"items": []}
        if path == "/api/v1/projects":
            items = [] if fault == "missing_original_project" else [{"id": "second", "name": "Second scope"}]
            return 200, {"items": items, "next_cursor": None}
        project_id = path.rsplit("/", 1)[-1]
        if project_id == "second" and not state["bound"]:
            return 404, {"detail": {"code": "not_found"}}
        return 200, profile(project_id)

    class OwnerCli:
        def run(self, arguments):
            calls.append(arguments)
            if arguments[1:3] == ["project", "bindings"]:
                return 0, {"status": "ok", "detail": {"items": deepcopy(state["history"])}}
            if arguments[1:3] == ["project", "unbind"]:
                assert arguments[arguments.index("--revision") + 1] == "3"
                state["bound"] = False
                state["revision"] = 4
                state["history"].append({"revision": 4, "relationship_versions": [{
                    "relationship_id": "link-original", "relationship_revision": "2",
                    "supersedes_revision": "1", "valid_from": "2026-02-01T00:00:00Z",
                    "valid_to": "2026-02-02T00:00:00Z",
                }]})
                if fault == "stale_closed":
                    state["history"][1]["relationship_versions"][0]["valid_to"] = "2026-02-01T00:00:00Z"
                return 0, {"status": "ok", "detail": profile("first")}
            assert arguments[1:3] == ["project", "bind"]
            assert arguments[arguments.index("--revision") + 1] == "4"
            selected = Path(arguments[arguments.index("--input") + 1])
            assert selected.exists() and selected.stat().st_mode & 0o777 == 0o600
            assert selected.read_text() == '{"mode": "select", "project_id": "second"}'
            state["bound"] = True
            state["revision"] = 5
            state["history"].append({"revision": 5, "relationship_versions": [{
                "relationship_id": "link-new", "relationship_revision": "1",
                "valid_from": "2026-02-03T00:00:00Z",
            }]})
            if fault == "rewritten_link":
                state["history"][0]["relationship_versions"][0]["relationship_id"] = "rewritten-original"
            if fault == "stale_rebound":
                state["history"][2]["relationship_versions"][0]["valid_from"] = "2026-02-02T00:00:00Z"
            return 0, {"status": "ok", "detail": profile("second")}

    evidence = {}
    if reason is None:
        product_scopes.verify_scope_history(config, OwnerCli(), readback, evidence)
        assert evidence["success"] is True
        assert [entry["revision"] for entry in evidence["binding_history"]] == [2, 4, 5]
        assert [event["status"] for event in evidence["api_readback"]] == [200, 404, 200]
        assert [args[2] for args in calls] == ["bindings", "unbind", "bindings", "bind", "bindings"]
    else:
        with pytest.raises(common.RemoteError, match=reason):
            product_scopes.verify_scope_history(config, OwnerCli(), readback, evidence)
        assert evidence.get("success") is not True

    removed = []
    ok, results = cleanup.sweep(manifest, {"product_fixture": lambda identifier: removed.append(identifier),
                                           "adp_connection": lambda identifier: pytest.fail("Original connection is reused")})
    assert ok and removed == ["run-one"]
    assert [(result["kind"], result["status"]) for result in results] == [("product_fixture", cleanup.DELETED)]
    assert manifest.read()["resources"][1]["status"] == cleanup.REUSED
    assert profiles["second"]["configuration"]["basic"]["repositories"][0]["source_connection_id"] == "connection-second"
    assert pushed and all(critical for _, critical in pushed[:2])


@pytest.mark.parametrize("fault", [None, "missing", "reused", "wrong_run", "wrong_owner",
                                  "no_deleter", "no_sink", "wrong_history"])
def test_history_stage_requires_owned_recovery_and_independent_evidence(scope_fixture, tmp_path, fault):
    config, _ = scope_fixture
    fixture = config["product_scopes"]
    fixture["recovery_manifest_ref"] = "run-one"
    fixture["history"] = {
        "project_id": "second", "relationship_id": "link-original", "linked_revision": 2,
        "valid_from": "2026-02-01T00:00:00Z", "unbound_at": "2026-02-02T00:00:00Z",
        "rebound_at": "2026-02-03T00:00:00Z",
    }
    if fault == "wrong_history":
        fixture["history"]["project_id"] = "foreign-project"
    stored = []
    manifest = cleanup.Manifest(tmp_path / "owned" / "manifest.json", "run-one",
                                on_change=None if fault == "no_sink" else
                                lambda document, *, critical: stored.append((deepcopy(document), critical)))
    plan = {"evaluation_id": "run-one", "tenant_id": "owned-tenant", "product_id": "product-one",
            "project_ids": ["first", "second"]}
    if fault != "missing":
        manifest.record("product_fixture", "foreign-run" if fault == "wrong_run" else "run-one",
                        reused=fault == "reused", detail=plan)
    outcomes = []
    context = {"document": {"instance_id": "synthetic-host", "session": {
        "org_id": "owned-tenant", "user_id": "unrelated" if fault == "wrong_owner" else "owner"}},
        "matrix": cases.new_matrix(("case:PR04-C03",)), "record": lambda *result: outcomes.append(result),
        "evaluation_id": "run-one", "manifest": manifest, "attempt_id": "run-one/1",
        "fault": "none", "transcript": [], "correlation": {}}

    def worker(purpose):
        if fault is not None:
            pytest.fail("Unowned Product history must not dispatch")
        assert purpose == "product_scope_history"

        def dispatch(_instance, _context):
            assert stored[-1] == (manifest.read(), True)
            assert stored[-1][0]["diagnostic_intents"]["PR04-C03:run-one"] == plan
            return {"success": True, "binding_history": [], "api_readback": []}

        return dispatch

    stage = stages.journeys_stage({"product_scopes": fixture, "expected_revision": config["expected_revision"]}, {
        "deleters": {} if fault == "no_deleter" else {"product_fixture": lambda *_args: None},
        "journey": worker})
    stage(context)
    assert outcomes[-1][:2] == ("PR04-C03", cases.FAILED if fault is None else cases.BLOCKED)
    if fault is None:
        assert context["document"]["product_scope_attempts"][0]["status"] == "failed"
    else:
        assert manifest.read().get("diagnostic_intents") is None


def test_foreign_run_and_original_project_are_not_cleanup_targets(tmp_path):
    manifest = cleanup.Manifest(tmp_path / "owned" / "manifest.json", "owned-run",
                                on_change=lambda _document, *, critical: None)
    manifest.record("product_fixture", "owned-run", detail={"evaluation_id": "owned-run"})
    with pytest.raises(ValueError, match="Unknown resource kind"):
        manifest.record("project", "original-project")
    manifest.record("adp_connection", "original-connection", reused=True)
    manifest.prefix = "another-run"
    manifest.record("product_fixture", "foreign-product", detail={"evaluation_id": "another-run"})
    manifest.prefix = "owned-run"
    deleted = []
    ok, results = cleanup.sweep(manifest, {
        "product_fixture": lambda identifier: deleted.append(identifier),
        "adp_connection": lambda _identifier: pytest.fail("Original connection must not be deleted"),
    })
    assert ok and deleted == ["owned-run"]
    assert [(item["id"], item["status"]) for item in results] == [
        ("owned-run", cleanup.DELETED), ("foreign-product", cleanup.SKIPPED_NOT_OWNED),
    ]
    assert manifest.read()["resources"][1]["status"] == cleanup.REUSED
