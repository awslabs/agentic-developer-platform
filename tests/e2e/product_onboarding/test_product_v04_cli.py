"""Controlled B03 installed-client checks; a synthetic executable is not live evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding import product_v04_cli


@pytest.fixture
def installed_scope(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "command-manifest.json").write_text('{"scope":"test"}')
    (source / "adp-product.py").write_text("# test helper\n")
    profiles = {}
    scopes = []
    for project_id, connection, target, count in (
        ("first", "connection-first", "2026-12-01", 0),
        ("second", "connection-second", "2027-12-01", 1),
    ):
        configuration = {
            "basic": {
                "repositories": [
                    {
                        "tenant_id": "owned-tenant",
                        "project_id": project_id,
                        "source_connection_id": connection,
                        "record_type": "repository",
                        "record_id": "repository-" + project_id,
                        "record_revision": "source-etag:1",
                    }
                ]
            },
            "financials": {"release_target_date": target},
        }
        progress = {"completed": count}
        scopes.append(
            {
                "project_id": project_id,
                "name": project_id,
                "configuration": configuration,
                "progress": progress,
            }
        )
        profiles[project_id] = {
            "tenant_id": "owned-tenant",
            "product_id": "product-one",
            "project_id": project_id,
            "product": {
                "name": "Planner",
                "owner_ref": {"kind": "user", "id": "owner"},
            },
            "project": {"id": project_id, "name": project_id},
            "configuration": configuration,
            "progress": progress,
        }
    profile_path = tmp_path / "profiles.json"
    profile_path.write_text(json.dumps(profiles))
    commands = tmp_path / "cli-commands.txt"
    executable = source / "adp"
    executable.write_text(
        "#!/usr/bin/env python3\nimport json, sys\n"
        f"with open({str(commands)!r}, 'a') as output: output.write(sys.argv[sys.argv.index('--project')+1]+'\\n')\n"
        f"print(json.dumps({{'status':'ok','detail':json.load(open({str(profile_path)!r}))[sys.argv[sys.argv.index('--project')+1]]}}))\n"
    )
    executable.chmod(0o755)
    installer = source / "install.sh"
    installer.write_text(
        'CLI_FILES="adp adp-product.py command-manifest.json"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in --prefix) prefix="$2"; shift 2 ;; *) shift ;; esac\n'
        "done\n"
        'mkdir -p "$prefix"\n'
        f'cp {shlex.quote(str(source))}/adp {shlex.quote(str(source))}/adp-product.py {shlex.quote(str(source))}/command-manifest.json "$prefix"\n'
    )
    monkeypatch.setattr(product_v04_cli, "CLI_SOURCE", source)
    fixture = {
        "run_id": "product-42-1",
        "source_revision": "a" * 40,
        "tenant_id": "owned-tenant",
        "actor_id": "owner",
        "product_id": "product-one",
        "product_name": "Planner",
        "owned_fixture": True,
        "scopes": scopes,
    }
    calls = []

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

    def get(url, **kwargs):
        calls.append(url)
        if url.endswith("/api/cli/install.sh"):
            return Response(200, installer.read_bytes())
        if url.endswith("/api/v1/products"):
            return Response(
                200,
                {"items": [{"id": "product-one", "project_ids": ["first", "second"]}]},
            )
        return Response(200, profiles[url.rsplit("/", 1)[-1]])

    provider = SimpleNamespace(
        base_url="https://example.invalid",
        request=SimpleNamespace(get=get),
        owner={
            "access_token": "owner-secret",
            "id_token": "owner-id",
            "refresh_token": "",
            "expires_in": 3600,
            "tenant_id": "owned-tenant",
            "actor_id": "owner",
        },
    )
    evidence = SimpleNamespace(
        run_id="product-42-1",
        source="a" * 40,
        workflow="b" * 40,
        run=42,
        attempt=1,
        root=tmp_path,
        context={
            "identities": {
                "source_revision": "a" * 40,
                "requirements_revision": "c" * 40,
                "cli_checksum": hashlib.sha256(
                    (source / "command-manifest.json").read_bytes()
                ).hexdigest(),
            }
        },
    )
    return provider, fixture, evidence, source, profiles, calls, commands


def test_installed_cli_reads_both_owned_scopes_against_separate_api(installed_scope):
    provider, fixture, evidence, source, profiles, calls, commands = installed_scope
    product_v04_cli.run_isolation(provider, fixture, evidence)
    assert commands.read_text().splitlines() == ["first", "second", "first"]
    assert calls[0].endswith("/api/cli/install.sh")
    assert sum(url.endswith("/api/v1/products") for url in calls) == 1
    attempt = evidence.cli_attempts["AC-01"][0]
    assert attempt["result"] == "pass" and attempt["exit_code"] == 0
    assert len(json.loads(Path(attempt["readback_ref"]).read_text())) == 4
    assert len(json.loads(Path(attempt["artifact_ref"]).read_text())["events"]) == 3
    assert (
        attempt["installed_cli_sha256"]
        == hashlib.sha256((source / "adp").read_bytes()).hexdigest()
    )
    product_v04_cli.write_cli_report(evidence)
    report = json.loads((evidence.root / "product-v04-cli-results.json").read_text())
    assert report["cases"]["AC-01"]["attempts"][0]["result"] == "pass"
    assert report["cleanup"]["status"] == "incomplete"
    assert (evidence.root / "product-v04-cli-results.json").stat().st_mode & 0o077 == 0


def test_install_refuses_source_manifest_mismatch_before_fetch(installed_scope):
    provider, fixture, evidence, source, profiles, calls, commands = installed_scope
    evidence.context["identities"]["cli_checksum"] = "f" * 64
    with pytest.raises(ValueError, match="signed backend"):
        product_v04_cli.run_isolation(provider, fixture, evidence)
    assert not calls and not commands.exists()
    assert evidence.cli_attempts["AC-01"][0]["result"] == "interrupted"


def test_install_refuses_stale_served_script_before_execution(installed_scope):
    provider, fixture, evidence, source, profiles, calls, commands = installed_scope
    provider.request = SimpleNamespace(
        get=lambda *args, **kwargs: SimpleNamespace(status=200, body=lambda: b"stale")
    )
    with pytest.raises(ValueError, match="served installer"):
        product_v04_cli.run_isolation(provider, fixture, evidence)
    assert not commands.exists()
    assert evidence.cli_attempts["AC-01"][0]["result"] == "interrupted"


def test_mixed_scope_response_is_retained_as_failed_attempt(installed_scope):
    provider, fixture, evidence, source, profiles, calls, commands = installed_scope
    profiles["second"]["progress"] = {"completed": 0}
    with pytest.raises(AssertionError, match="completed"):
        product_v04_cli.run_isolation(provider, fixture, evidence)
    assert commands.read_text().splitlines() == ["first", "second"]
    attempt = evidence.cli_attempts["AC-01"][0]
    assert attempt["result"] == "interrupted"
    assert (
        json.loads(Path(attempt["artifact_ref"]).read_text())["result"] == "interrupted"
    )


@pytest.mark.parametrize("call_status", ["passed", "failed"])
def test_scope_fixture_runs_cli_before_retirement_only_after_browser_cases(
    monkeypatch, installed_scope, call_status
):
    from contextlib import contextmanager
    from tests.e2e.new_ui import conftest as browser_fixtures
    from tests.e2e.new_ui import product_scope_fixture as scope_fixture_module

    provider, fixture, evidence, source, profiles, calls, commands = installed_scope
    events = []

    @contextmanager
    def owned(*_args):
        events.append("create")
        yield {
            "product_id": fixture["product_id"],
            "project_id": fixture["scopes"][0]["project_id"],
        }
        events.append("retire")

    provider.fixture = owned
    provider.scope_inputs = [
        {"selection": {"kind": "repository", "provider": "github",
                       "connection_ref": {"kind": "github_installation", "id": scope["configuration"]["basic"]["repositories"][0]["source_connection_id"]},
                       "namespace": "example-org", "native_id": scope["configuration"]["basic"]["repositories"][0]["record_id"]},
         "record_revision": "b" * 40,
         "release_target_date": scope["configuration"]["financials"]["release_target_date"]}
        for scope in fixture["scopes"]
    ]

    def authorized_source(url, **kwargs):
        if "/sources?" in url:
            from urllib.parse import parse_qs, urlsplit

            connection_id = parse_qs(urlsplit(url).query)["connection_id"][0]
            selections = [item["selection"] for item in provider.scope_inputs
                          if item["selection"]["connection_ref"]["id"] == connection_id]
            assert kwargs["headers"]["Authorization"] == "Bearer owner-secret"
            return SimpleNamespace(status=200, json=lambda: {"items": [
                {"connection_ref": selection["connection_ref"], "provider": "github",
                 "coverage": "complete", "resources": [{"kind": "repository", "namespace": selection["namespace"],
                                                "native_id": selection["native_id"]}]}
                for selection in selections
            ]})
        return original_get(url, **kwargs)

    original_get = provider.request.get
    provider.request.get = authorized_source
    provider.run_id = fixture["run_id"]
    provider.state = SimpleNamespace()
    provider.binding = {"run_id": fixture["run_id"]}
    monkeypatch.setattr(
        scope_fixture_module, "prepare_scope_fixture", lambda *args: None
    )
    monkeypatch.setattr(
        scope_fixture_module, "load_scope_fixture", lambda *args: fixture
    )
    monkeypatch.setattr(
        product_v04_cli,
        "run_isolation",
        lambda selected, owned, report: events.append("cli"),
    )
    from tests.e2e.product_onboarding import product_v04_denials

    monkeypatch.setattr(
        product_v04_denials,
        "run_denials",
        lambda selected, owned, report: events.append("denials"),
    )
    from tests.e2e.product_onboarding import product_v04_history

    monkeypatch.setattr(
        product_v04_history,
        "run_history",
        lambda selected, owned, report: events.append("history"),
    )
    monkeypatch.setenv("PRODUCT_B03_MODE", "true")
    evidence.bind = lambda owned: None
    evidence.records = {
        "a": SimpleNamespace(phases={"call": "passed"}),
        "b": SimpleNamespace(phases={"call": call_status}),
    }
    request = SimpleNamespace(config=SimpleNamespace(product_scope_evidence=evidence))
    steps = browser_fixtures.product_scope_fixture.__wrapped__(provider, request)
    assert next(steps) == fixture
    with pytest.raises(StopIteration):
        next(steps)
    assert events == (
        ["create", "cli", "denials", "history", "retire"]
        if call_status == "passed"
        else ["create", "retire"]
    )


def test_changed_installed_helper_refuses_mixed_release(installed_scope):
    provider, fixture, evidence, source, profiles, calls, commands = installed_scope
    installer = source / "install.sh"
    installer.write_text(
        installer.read_text() + 'echo stale >> "$prefix/adp-product.py"\n'
    )
    with pytest.raises(ValueError, match="installed client differs"):
        product_v04_cli.run_isolation(provider, fixture, evidence)
    assert not commands.exists()
    assert evidence.cli_attempts["AC-01"][0]["result"] == "interrupted"


def test_failed_installed_command_keeps_private_redacted_attempt(installed_scope):
    provider, fixture, evidence, source, profiles, calls, commands = installed_scope
    (source / "adp").write_text(
        "#!/usr/bin/env python3\nprint('owner-secret')\nraise SystemExit(3)\n"
    )
    with pytest.raises(AssertionError, match="inspect its private attempt"):
        product_v04_cli.run_isolation(provider, fixture, evidence)
    attempt = evidence.cli_attempts["AC-01"][0]
    assert attempt["result"] == "interrupted"
    artifact = Path(attempt["artifact_ref"])
    assert artifact.stat().st_mode & 0o077 == 0
    assert "owner-secret" not in artifact.read_text()
    assert "[REDACTED]" in artifact.read_text()
    product_v04_cli.write_cli_report(evidence)
    assert (
        json.loads((evidence.root / "product-v04-cli-results.json").read_text())[
            "cases"
        ]["AC-01"]["attempts"][0]["result"]
        == "interrupted"
    )
