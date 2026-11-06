"""Controlled installed CLI and independent API history/recovery checks."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding import product_v04_cli, product_v04_history

pytest_plugins = ("tests.e2e.product_onboarding.test_product_v04_cli",)


@pytest.fixture
def history_scope(installed_scope):
    provider, fixture, evidence, source, profiles, calls, commands = installed_scope
    for profile in profiles.values():
        profile["revision"] = 3
    initial = {
        "relationship_id": "original-link",
        "relationship_type": "product_project",
        "from_ref": {"record_id": fixture["product_id"]},
        "to_ref": {"record_id": fixture["scopes"][1]["project_id"]},
        "relationship_revision": "1",
        "valid_from": "2026-01-01T00:00:00Z",
    }
    state_file = evidence.root / "binding.json"
    state_file.write_text(
        json.dumps(
            {
                "revision": 3,
                "bound": True,
                "history": [{"revision": 2, "relationship_versions": [initial]}],
            }
        )
    )
    profile_file = evidence.root / "profiles.json"
    profile_file.write_text(json.dumps(profiles))
    fail_bind = evidence.root / "fail-bind"
    lose_unbind = evidence.root / "lose-unbind"
    lose_bind = evidence.root / "lose-bind"
    (source / "adp").write_text(f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state_path = Path({str(state_file)!r})
profiles = json.loads(Path({str(profile_file)!r}).read_text())
state = json.loads(state_path.read_text())
args = sys.argv[2:]
actor = json.loads(Path(os.environ['HOME'], '.bedrock-gateway', 'tokens.json').read_text())['access_token']
if actor != 'owner-secret': raise SystemExit(8)
with open({str(commands)!r}, 'a') as output: output.write(' '.join(args)+'\\n')
status, code = 'ok', 0
if args[:2] == ['project', 'bindings']:
    detail = {{'items': state['history']}}
elif args[:2] == ['project', 'unbind']:
    assert int(args[args.index('--revision')+1]) == 3 and state['bound']
    assert '--yes' in args and '--idempotency-key' in args
    state['revision'], state['bound'] = 4, False
    old = dict(state['history'][0]['relationship_versions'][0])
    old.update(relationship_revision='2', supersedes_revision='1', valid_to='2026-01-02T00:00:00Z')
    state['history'].append({{'revision': 4, 'relationship_versions': [old]}})
    state_path.write_text(json.dumps(state))
    detail = dict(profiles['first'], revision=4)
    if Path({str(lose_unbind)!r}).exists(): status, code = 'failed', 5
elif args[:2] == ['project', 'bind']:
    assert int(args[args.index('--revision')+1]) == 4 and not state['bound']
    assert json.loads(Path(args[args.index('--input')+1]).read_text()) == {{'mode':'select','project_id':'second'}}
    assert '--idempotency-key' in args
    if Path({str(fail_bind)!r}).exists():
        status, code, detail = 'failed', 5, {{}}
    else:
        state['revision'], state['bound'] = 5, True
        old = dict(state['history'][0]['relationship_versions'][0])
        old.update(relationship_id='new-link', relationship_revision='1', valid_from='2026-01-03T00:00:00Z')
        state['history'].append({{'revision': 5, 'relationship_versions': [old]}})
        state_path.write_text(json.dumps(state))
        detail = dict(profiles['second'], revision=5)
        if Path({str(lose_bind)!r}).exists(): status, code = 'failed', 5
else:
    raise SystemExit(1)
print(json.dumps({{'status':status, 'detail':detail if status == 'ok' else {{}},
                  'error': {{'code':'request_timeout'}} if status == 'failed' else None}}))
raise SystemExit(code)
""")
    (source / "adp").chmod(0o755)

    class Response:
        def __init__(self, status, payload):
            self.status, self.payload = status, payload

        def body(self):
            return (
                self.payload
                if isinstance(self.payload, bytes)
                else json.dumps(self.payload).encode()
            )

        def json(self):
            return self.payload

    fail_compensation = evidence.root / "fail-compensation"

    def get(url, **kwargs):
        calls.append(url)
        state = json.loads(state_file.read_text())
        if url.endswith("/api/cli/install.sh"):
            return Response(200, (source / "install.sh").read_bytes())
        if url.endswith("/api/v1/projects"):
            return Response(
                200,
                {
                    "items": [
                        {
                            "id": "second",
                            "name": "second",
                            "product_id": "product-one" if state["bound"] else None,
                        }
                    ],
                    "next_cursor": None,
                },
            )
        if url.endswith("/project-bindings"):
            return Response(200, {"items": deepcopy(state["history"])})
        project_id = url.rsplit("/", 1)[-1]
        if project_id == "second" and not state["bound"]:
            return Response(404, {"detail": {"code": "not_found"}})
        if project_id in profiles:
            return Response(
                200, {**profiles[project_id], "revision": state["revision"]}
            )
        raise AssertionError("Unknown independent Project readback")

    def post(url, headers, data, **kwargs):
        assert url.endswith("/api/v1/products/product-one/projects")
        assert (
            headers["Idempotency-Key"]
            == provider.state.document["scope"]["cli_history"]["rebind_key"]
        )
        assert headers["If-Profile-Revision"] == "4"
        assert data == {"mode": "select", "project_id": "second"}
        if fail_compensation.exists():
            return Response(503, {"detail": {"code": "unavailable"}})
        state = json.loads(state_file.read_text())
        assert state["bound"] is False and state["revision"] == 4
        old = dict(state["history"][0]["relationship_versions"][0])
        old.update(
            relationship_id="new-link",
            relationship_revision="1",
            valid_from="2026-01-03T00:00:00Z",
        )
        state["history"].append({"revision": 5, "relationship_versions": [old]})
        state.update(bound=True, revision=5)
        state_file.write_text(json.dumps(state))
        return Response(200, {**profiles["second"], "revision": 5})

    provider.request = SimpleNamespace(get=get, post=post)
    provider.state = SimpleNamespace(
        binding={"run_id": fixture["run_id"]},
        document={
            "scope": {
                "cleanup_pending": True,
                "product": {"product_id": fixture["product_id"], "project_id": "first"},
                "receipts": {"second_project": {"project_id": "second"}},
            }
        },
    )
    provider.state.save = lambda value: setattr(provider.state, "document", value)
    evidence.cli_attempts = {
        "AC-01": [
            {
                "result": "pass",
                "installed_cli_sha256": hashlib.sha256(
                    (source / "adp").read_bytes()
                ).hexdigest(),
            }
        ],
        "AC-02": [{"result": "pass"}],
    }
    return (
        provider,
        fixture,
        evidence,
        state_file,
        calls,
        commands,
        fail_bind,
        fail_compensation,
        lose_unbind,
        lose_bind,
    )


def test_installed_history_preserves_original_link_and_separate_api(history_scope):
    provider, fixture, evidence, state_file, calls, commands, *_ = history_scope
    product_v04_history.run_history(provider, fixture, evidence)
    assert provider.state.document["scope"]["cli_history"]["status"] == "restored"
    state = json.loads(state_file.read_text())
    assert [item["revision"] for item in state["history"]] == [2, 4, 5]
    assert state["bound"] and state["revision"] == 5
    assert any("project unbind" in line for line in commands.read_text().splitlines())
    assert any("project bind" in line for line in commands.read_text().splitlines())
    assert evidence.cli_attempts["AC-03"][0]["result"] == "pass"
    assert evidence.api_attempts["AC-03"][0]["result"] == "pass"
    assert (
        len(
            json.loads(
                Path(evidence.api_attempts["AC-03"][0]["readback_ref"]).read_text()
            )
        )
        >= 10
    )
    product_v04_cli.write_cli_report(evidence)
    result = json.loads((evidence.root / "product-v04-cli-results.json").read_text())
    assert len(result["cases"]["AC-03"]["api_attempts"]) == 1
    assert result["cleanup"]["status"] == "incomplete"


@pytest.mark.parametrize("lost", ["unbind", "bind"])
def test_lost_command_response_reconciles_exact_history(history_scope, lost):
    (
        provider,
        fixture,
        evidence,
        state_file,
        calls,
        commands,
        _,
        _,
        lose_unbind,
        lose_bind,
    ) = history_scope
    (lose_unbind if lost == "unbind" else lose_bind).touch()
    product_v04_history.run_history(provider, fixture, evidence)
    assert evidence.cli_attempts["AC-03"][0]["result"] == "pass"
    assert any(
        event["exit_code"] == 5
        for event in json.loads(
            Path(evidence.cli_attempts["AC-03"][0]["artifact_ref"]).read_text()
        )["events"]
    )
    assert [
        item["revision"] for item in json.loads(state_file.read_text())["history"]
    ] == [2, 4, 5]


def test_failed_compensation_retains_claim_and_interrupted_attempt(history_scope):
    (
        provider,
        fixture,
        evidence,
        state_file,
        calls,
        commands,
        fail_bind,
        fail_compensation,
        *_,
    ) = history_scope
    fail_bind.touch()
    fail_compensation.touch()
    with pytest.raises(AssertionError, match="compensation was refused"):
        product_v04_history.run_history(provider, fixture, evidence)
    claim = provider.state.document["scope"]["cli_history"]
    assert claim["status"] == "pending" and claim["rebind_key"]
    assert [
        item["revision"] for item in json.loads(state_file.read_text())["history"]
    ] == [2, 4]
    assert evidence.cli_attempts["AC-03"][0]["result"] == "interrupted"
    assert Path(evidence.cli_attempts["AC-03"][0]["artifact_ref"]).is_file()
    fail_compensation.unlink()
    product_v04_history.restore_history(provider)
    assert provider.state.document["scope"]["cli_history"]["status"] == "restored"
    assert [
        item["revision"] for item in json.loads(state_file.read_text())["history"]
    ] == [2, 4, 5]
    assert evidence.cli_attempts["AC-03"][0]["result"] == "interrupted"


def test_interrupted_setup_does_not_remove_original_history(history_scope, monkeypatch):
    provider, fixture, evidence, state_file, calls, commands, *_ = history_scope

    def interrupted(*_args):
        raise RuntimeError("installer unavailable")

    monkeypatch.setattr(product_v04_history, "install_served_cli", interrupted)
    with pytest.raises(RuntimeError, match="installer unavailable"):
        product_v04_history.run_history(provider, fixture, evidence)
    assert provider.state.document["scope"]["cli_history"]["status"] == "not_mutated"
    assert [
        item["revision"] for item in json.loads(state_file.read_text())["history"]
    ] == [2]
    assert evidence.cli_attempts["AC-03"][0]["result"] == "interrupted"


def test_completed_history_cannot_repeat_or_erase_previous_attempt(history_scope):
    provider, fixture, evidence, state_file, calls, commands, *_ = history_scope
    product_v04_history.run_history(provider, fixture, evidence)
    initial = commands.read_text()
    with pytest.raises(ValueError, match="already compensated"):
        product_v04_history.run_history(provider, fixture, evidence)
    assert commands.read_text() == initial
    assert [entry["result"] for entry in evidence.cli_attempts["AC-03"]] == [
        "pass",
        "interrupted",
    ]
    assert Path(evidence.cli_attempts["AC-03"][0]["artifact_ref"]).is_file()


def test_recovery_restores_history_before_owned_retirement(history_scope, monkeypatch):
    from tests.e2e.new_ui import product_fixture_provider, product_scope_cleanup

    (
        provider,
        fixture,
        evidence,
        state_file,
        calls,
        commands,
        fail_bind,
        fail_compensation,
        *_,
    ) = history_scope
    fail_bind.touch()
    fail_compensation.touch()
    with pytest.raises(AssertionError, match="compensation was refused"):
        product_v04_history.run_history(provider, fixture, evidence)
    fail_compensation.unlink()
    provider.state.document["cases"] = {
        "scope": {"document": {"request": {}}, "before": {}}
    }
    order = []

    def prepare(selected, case):
        order.append("prepare")
        assert selected.state.document["scope"]["cli_history"]["status"] == "restored"
        assert json.loads(state_file.read_text())["bound"] is True

    monkeypatch.setattr(product_scope_cleanup, "prepare_scope_retirement", prepare)
    monkeypatch.setattr(
        product_scope_cleanup,
        "confirm_scope_retirement",
        lambda selected, receipt: order.append("confirm"),
    )
    monkeypatch.setattr(
        product_fixture_provider,
        "FixtureStore",
        lambda state, case: SimpleNamespace(load=lambda run_id: ({"request": {}}, {})),
    )
    provider.editor_membership = None
    provider.membership = SimpleNamespace(
        restore=lambda state: order.append("membership")
    )
    provider.observer = provider.owner
    provider.run_id = fixture["run_id"]
    provider.run = lambda case, document: SimpleNamespace(
        retire=lambda *args: {"fixture_removed": True}
    )
    cleanup = product_fixture_provider.BrowserFixtureProvider.cleanup(
        provider, "scope", provider.request
    )
    assert cleanup["fixture_removed"] is True
    assert order == ["prepare", "membership", "confirm"]
    assert evidence.cli_attempts["AC-03"][0]["result"] == "interrupted"


def test_stale_closed_revision_retains_durable_claim_and_failed_attempt(history_scope):
    provider, fixture, evidence, state_file, calls, commands, *_ = history_scope
    original = provider.request.get

    def stale_history(url, **kwargs):
        response = original(url, **kwargs)
        if url.endswith("/project-bindings") and len(response.payload["items"]) == 2:
            response.payload["items"][-1]["relationship_versions"][0]["valid_to"] = (
                "2025-01-01T00:00:00Z"
            )
        return response

    provider.request.get = stale_history
    with pytest.raises(AssertionError, match="did not close"):
        product_v04_history.run_history(provider, fixture, evidence)
    claim = provider.state.document["scope"]["cli_history"]
    assert claim["status"] == "pending"
    assert [
        item["revision"] for item in json.loads(state_file.read_text())["history"]
    ] == [2, 4]
    assert evidence.cli_attempts["AC-03"][0]["result"] == "interrupted"


def test_lost_compensation_response_reconciles_original_link(history_scope):
    provider, fixture, evidence, state_file, calls, commands, fail_bind, _, *_ = (
        history_scope
    )
    fail_bind.touch()
    original = provider.request.post

    def lost_response(*args, **kwargs):
        original(*args, **kwargs)
        return SimpleNamespace(status=503)

    provider.request.post = lost_response
    with pytest.raises(
        AssertionError, match="Rebind changed the original relationship history"
    ):
        product_v04_history.run_history(provider, fixture, evidence)
    assert provider.state.document["scope"]["cli_history"]["status"] == "restored"
    assert [
        row["revision"] for row in json.loads(state_file.read_text())["history"]
    ] == [2, 4, 5]
    assert evidence.cli_attempts["AC-03"][0]["result"] == "interrupted"


def test_original_project_asset_changed_prevents_compensation(history_scope):
    provider, fixture, evidence, state_file, calls, commands, *_ = history_scope
    original = provider.request.get

    def changed_asset(url, **kwargs):
        response = original(url, **kwargs)
        if (
            url.endswith("/api/v1/projects")
            and not json.loads(state_file.read_text())["bound"]
        ):
            response.payload["items"][0]["name"] = "different-project"
        return response

    provider.request.get = changed_asset
    with pytest.raises(AssertionError, match="original Project asset"):
        product_v04_history.run_history(provider, fixture, evidence)
    claim = provider.state.document["scope"]["cli_history"]
    assert claim["status"] == "pending"
    assert claim["project_name"] == fixture["scopes"][1]["name"]
    assert evidence.cli_attempts["AC-03"][0]["result"] == "interrupted"


def test_recovery_rejects_claim_for_another_project(history_scope, monkeypatch):
    provider, fixture, evidence, state_file, calls, commands, *_ = history_scope

    def interrupted(*_args):
        raise RuntimeError("installer unavailable")

    monkeypatch.setattr(product_v04_history, "install_served_cli", interrupted)
    with pytest.raises(RuntimeError, match="installer unavailable"):
        product_v04_history.run_history(provider, fixture, evidence)
    original = deepcopy(provider.state.document["scope"]["cli_history"])
    provider.state.document["scope"]["cli_history"]["project_id"] = "other-project"
    with pytest.raises(ValueError, match="unowned fixture"):
        product_v04_history.restore_history(provider)
    assert [
        entry["revision"] for entry in json.loads(state_file.read_text())["history"]
    ] == [2]
    assert original["project_id"] == fixture["scopes"][1]["project_id"]
