"""Controlled installed-client denial execution against a protected two-scope fixture."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding import product_v04_cli, product_v04_denials

pytest_plugins = ("tests.e2e.product_onboarding.test_product_v04_cli",)


@pytest.fixture
def denied_scope(installed_scope):
    provider, fixture, evidence, source, profiles, calls, commands = installed_scope
    revoked = evidence.root / "revoked.flag"
    bad_revoked = evidence.root / "wrong-revoked.flag"
    bad_admin = evidence.root / "wrong-admin.flag"
    foreign = {
        "product_id": "foreign-product",
        "project_id": "foreign-project",
        "tenant_id": "foreign-tenant",
        "product": {"owner_ref": {"kind": "user", "id": "outsider"}},
    }
    for profile in profiles.values():
        profile["product"]["purpose"] = "Verify ordinary editor scope access"
        profile["revision"] = 3
    (evidence.root / "profiles.json").write_text(json.dumps(profiles))
    (source / "adp").write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"profiles = json.load(open({str(evidence.root / 'profiles.json')!r}))\n"
        f"revoked = os.path.exists({str(revoked)!r})\n"
        f"bad_revoked = os.path.exists({str(bad_revoked)!r})\n"
        f"bad_admin = os.path.exists({str(bad_admin)!r})\n"
        "args = sys.argv[2:]\n"
        "actor = json.load(open(os.environ['HOME']+'/.bedrock-gateway/tokens.json'))['access_token']\n"
        "if '--project' in args: project = args[args.index('--project')+1]\n"
        "else: project = None\n"
        "status, code, detail, error = 'ok', 0, {}, None\n"
        "if actor == 'editor-secret' and revoked:\n"
        "    status, code, error = 'failed', 3, 'tenant_membership_revoked'\n"
        "    if bad_revoked: code, error = 5, 'not_found'\n"
        "elif actor == 'foreign-secret' and (args[0] in ('show', 'history') or args[:2] == ['project','bindings']):\n"
        "    status, code, error = 'failed', 5, 'not_found'\n"
        "elif args[0] == 'update':\n"
        "    status, code, error = 'failed', 3, 'permission_denied'\n"
        "    if bad_admin: status, code, error = 'ok', 0, None\n"
        "elif args[:2] == ['project','bind']:\n"
        "    status, code, error = 'failed', 5, 'not_found'\n"
        "elif args[0] == 'show': detail = profiles[project]\n"
        "elif args[0] == 'list': detail = {'items': [{'id':'foreign-product','project_ids':['foreign-project']}], 'next_cursor': None}\n"
        "elif args[:2] == ['project','list']: detail = {'items': [{'id':'foreign-project','product_id':'foreign-product'}], 'next_cursor': None}\n"
        "else: raise SystemExit(1)\n"
        "with open("
        + repr(str(commands))
        + ", 'a') as output: output.write(actor+':'+str(args[:2])+'\\n')\n"
        "print(json.dumps({'status':status, 'detail': detail if status=='ok' else None, 'error': {'code':error} if error else None}))\n"
        "raise SystemExit(code)\n"
    )
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

        def text(self):
            return self.body().decode()

    def get(url, **kwargs):
        actor = (
            kwargs.get("headers", {}).get("Authorization", "").removeprefix("Bearer ")
        )
        calls.append((url, actor))
        if url.endswith("/api/cli/install.sh"):
            return Response(200, (source / "install.sh").read_bytes())
        if url.endswith("/api/v1/projects") or url.endswith(
            "/api/v1/projects?limit=100"
        ):
            if actor == "editor-secret" and revoked.exists():
                return Response(403, {"detail": {"error": "tenant_membership_revoked"}})
            return Response(
                200,
                {
                    "items": [
                        {"id": "foreign-project", "product_id": "foreign-product"}
                    ],
                    "next_cursor": None,
                },
            )
        if url.endswith("/api/v1/products?limit=50"):
            if actor == "editor-secret" and revoked.exists():
                return Response(403, {"detail": {"error": "tenant_membership_revoked"}})
            return Response(
                200,
                {
                    "items": [
                        {"id": "foreign-product", "project_ids": ["foreign-project"]}
                    ],
                    "next_cursor": None,
                },
            )
        if url.endswith("/api/v1/products"):
            return Response(
                200,
                {"items": [{"id": "product-one", "project_ids": ["first", "second"]}]},
            )
        if url.endswith("/api/v1/products/foreign-product/projects/foreign-project"):
            return Response(200, foreign)
        if url.rsplit("/", 1)[-1] in profiles:
            if actor == "foreign-secret":
                return Response(404, {"detail": {"error": "not_found"}})
            if actor == "editor-secret" and revoked.exists():
                return Response(403, {"detail": {"error": "tenant_membership_revoked"}})
            return Response(200, profiles[url.rsplit("/", 1)[-1]])
        raise AssertionError("Unexpected API route")

    provider.request = SimpleNamespace(get=get)
    provider.editor = {
        **provider.owner,
        "access_token": "editor-secret",
        "actor_id": "editor",
    }
    provider.nonmember = {
        **provider.owner,
        "access_token": "foreign-secret",
        "tenant_id": "foreign-tenant",
        "actor_id": "outsider",
    }
    provider.binding = {"editor_id": "editor", "editor_team_id": "editor-team"}
    state = SimpleNamespace(
        document={
            "scope": {"cleanup_pending": True, "team_share": {"team_id": "editor-team"}}
        }
    )
    state.save = lambda update: setattr(state, "document", update)
    provider.state = state
    transitions = []

    def revoke(_state):
        assert not revoked.exists()
        revoked.touch()
        transitions.append("revoke")

    def restore(_state):
        assert revoked.exists()
        revoked.unlink()
        transitions.append("restore")

    provider.editor_membership = SimpleNamespace(revoke=revoke, restore=restore)
    product_v04_cli.run_isolation(provider, fixture, evidence)
    return (
        provider,
        fixture,
        evidence,
        calls,
        commands,
        transitions,
        bad_revoked,
        bad_admin,
    )


def test_installed_denial_cases_run_with_independent_api_and_restored_member(
    denied_scope,
):
    (
        provider,
        fixture,
        evidence,
        calls,
        commands,
        transitions,
        bad_revoked,
        bad_admin,
    ) = denied_scope
    product_v04_denials.run_denials(provider, fixture, evidence)
    assert transitions == ["revoke", "restore"]
    assert len(evidence.cli_attempts["AC-02"]) == 1
    attempt = evidence.cli_attempts["AC-02"][0]
    assert attempt["result"] == "pass" and attempt["exit_code"] == 0
    cli = json.loads(Path(attempt["artifact_ref"]).read_text())
    assert cli["result"] == "pass" and len(cli["events"]) >= 20
    assert "foreign-secret" not in json.dumps(cli)
    assert any(event["exit_code"] == 3 for event in cli["events"])
    assert any(event["exit_code"] == 5 for event in cli["events"])
    assert any(
        "editor-secret" in command for command in commands.read_text().splitlines()
    )
    assert len(json.loads(Path(attempt["readback_ref"]).read_text())) >= 8
    assert set(provider.state.document["scope"]["cli_denial_intents"]) == {
        "admin",
        "bind",
    }
    product_v04_cli.write_cli_report(evidence)
    report = json.loads((evidence.root / "product-v04-cli-results.json").read_text())
    assert report["cases"]["AC-02"]["attempts"][0]["result"] == "pass"
    assert report["cleanup"]["status"] == "incomplete"


@pytest.mark.parametrize("bad_actor", ["revoked", "admin"])
def test_bad_cli_denial_keeps_intents_and_restores_membership(denied_scope, bad_actor):
    (
        provider,
        fixture,
        evidence,
        calls,
        commands,
        transitions,
        bad_revoked,
        bad_admin,
    ) = denied_scope
    (bad_revoked if bad_actor == "revoked" else bad_admin).touch()
    with pytest.raises(AssertionError, match="denial"):
        product_v04_denials.run_denials(provider, fixture, evidence)
    assert evidence.cli_attempts["AC-02"][0]["result"] == "interrupted"
    assert set(provider.state.document["scope"]["cli_denial_intents"]) == {
        "admin",
        "bind",
    }
    assert transitions == (["revoke", "restore"] if bad_actor == "revoked" else [])


def test_missing_foreign_project_fails_before_denied_write(denied_scope):
    (
        provider,
        fixture,
        evidence,
        calls,
        commands,
        transitions,
        bad_revoked,
        bad_admin,
    ) = denied_scope
    original = provider.request.get

    def empty_foreign(url, **kwargs):
        response = original(url, **kwargs)
        if url.endswith("/api/v1/projects"):
            response.payload["items"] = []
        return response

    provider.request.get = empty_foreign
    with pytest.raises(ValueError, match="foreign actor has no"):
        product_v04_denials.run_denials(provider, fixture, evidence)
    assert "cli_denial_intents" not in provider.state.document["scope"]
    assert evidence.cli_attempts["AC-02"][0]["result"] == "interrupted"
    assert transitions == []


def test_foreign_api_disclosure_fails_even_with_expected_denial_status(denied_scope):
    (
        provider,
        fixture,
        evidence,
        calls,
        commands,
        transitions,
        bad_revoked,
        bad_admin,
    ) = denied_scope
    original = provider.request.get

    def leak(url, **kwargs):
        response = original(url, **kwargs)
        if (
            kwargs.get("headers", {}).get("Authorization") == "Bearer foreign-secret"
            and response.status == 404
        ):
            response.payload["detail"]["name"] = fixture["product_name"]
        return response

    provider.request.get = leak
    with pytest.raises(AssertionError, match="disclosed"):
        product_v04_denials.run_denials(provider, fixture, evidence)
    assert evidence.cli_attempts["AC-02"][0]["result"] == "interrupted"
    assert "cli_denial_intents" not in provider.state.document["scope"]


def test_cli_denied_write_intent_replay_rejects_changed_original(denied_scope):
    (
        provider,
        fixture,
        evidence,
        calls,
        commands,
        transitions,
        bad_revoked,
        bad_admin,
    ) = denied_scope
    original = product_v04_denials.journal_denial(provider, fixture, "admin", 3)
    assert product_v04_denials.journal_denial(provider, fixture, "admin", 3) == original
    with pytest.raises(ValueError, match="original intent"):
        product_v04_denials.journal_denial(provider, fixture, "admin", 4)
    assert provider.state.document["scope"]["cli_denial_intents"]["admin"] == original


@pytest.mark.parametrize("leak", ["owned_item", "count", "cursor"])
def test_foreign_listing_disclosure_or_partial_page_fails_closed(denied_scope, leak):
    provider, fixture, evidence, _, _, transitions, _, _ = denied_scope
    original = provider.request.get

    def leaked_list(url, **kwargs):
        response = original(url, **kwargs)
        if url.endswith("/api/v1/products?limit=50"):
            if leak == "owned_item":
                response.payload["items"].append({"id": fixture["product_id"]})
            elif leak == "count":
                response.payload["total_count"] = 2
            else:
                response.payload["next_cursor"] = "unreviewed-page"
        return response

    provider.request.get = leaked_list
    with pytest.raises(AssertionError, match="listing disclosed"):
        product_v04_denials.run_denials(provider, fixture, evidence)
    assert evidence.cli_attempts["AC-02"][0]["result"] == "interrupted"
    assert "cli_denial_intents" not in provider.state.document["scope"]
    assert transitions == []


def test_cli_list_count_must_match_independent_api(denied_scope, monkeypatch):
    provider, fixture, evidence, _, _, transitions, _, _ = denied_scope
    original = product_v04_denials.installed_cli

    def wrong_count(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[4] == ["list"]:
            return {**result, "total_count": 2}
        return result

    monkeypatch.setattr(product_v04_denials, "installed_cli", wrong_count)
    with pytest.raises(AssertionError, match="differs from independent"):
        product_v04_denials.run_denials(provider, fixture, evidence)
    assert evidence.cli_attempts["AC-02"][0]["result"] == "interrupted"
    assert transitions == []


def test_revoked_listing_failure_restores_original_membership(denied_scope):
    provider, fixture, evidence, _, _, transitions, _, _ = denied_scope
    original = provider.request.get

    def leaked_revoked(url, **kwargs):
        response = original(url, **kwargs)
        if (
            url.endswith("/api/v1/projects?limit=100")
            and kwargs.get("headers", {}).get("Authorization") == "Bearer editor-secret"
            and transitions == ["revoke"]
        ):
            response.status = 200
            response.payload = {"items": [{"id": fixture["scopes"][0]["project_id"]}]}
        return response

    provider.request.get = leaked_revoked
    with pytest.raises(AssertionError, match="Revoked tenant read"):
        product_v04_denials.run_denials(provider, fixture, evidence)
    assert transitions == ["revoke", "restore"]
    assert evidence.cli_attempts["AC-02"][0]["result"] == "interrupted"
    assert set(provider.state.document["scope"]["cli_denial_intents"]) == {
        "admin",
        "bind",
    }
