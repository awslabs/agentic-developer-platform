"""Deterministic command tests for PMM-05 without a deployment or model call."""

from __future__ import annotations

import importlib.util
import json
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "cli/adp-models.py"
SPEC = importlib.util.spec_from_file_location("adp_models", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)

CANONICAL = "global.anthropic.claude-opus-5"


def mapping(saved=None, revision=None):
    return {
        "principal_kind": "human",
        "principal_id": "user-1",
        "entries": [
            {
                "persona_key": "architect",
                "saved_model_id": saved,
                "effective_model_id": saved or "global.anthropic.claude-sonnet-4-6",
                "source": "principal-mapping" if saved else "system-default",
                "status": "configured" if saved else "not-configured",
                "revision": revision,
            }
        ],
    }


def catalogue(*, selectable=True, reason=None, aliases=None):
    row = {
        "canonical_model_id": CANONICAL,
        "model_family": "Opus",
        "canonical_version": "5",
        "selectable": selectable,
        "reason": reason,
        "compatibility_class": "claude-agent-sdk",
        "evidence": {"account_id": "123", "region": "eu-west-2"} if selectable else None,
    }
    if aliases is not None:
        row["aliases"] = aliases
    return {"persona_key": "architect", "compatibility_class": "claude-agent-sdk", "models": [row]}


class StubApi:
    machine = False

    def __init__(self, before=None, model_catalogue=None):
        self.before = before or mapping()
        self.catalogue = model_catalogue or catalogue()
        self.calls = []

    def request(self, method, path, body=None, **_kwargs):
        self.calls.append((method, path, body))
        if path.endswith("/catalog?persona_key=architect"):
            return self.catalogue
        if path.endswith("/manageable-service-principals"):
            return {"principals": [{"canonical_principal_id": "sp-1", "manageable": True}]}
        if "/explain/" in path:
            return {
                "persona_key": "architect",
                "effective_model_id": "global.anthropic.claude-sonnet-4-6",
                "source": "system-default",
                "status": "not-configured",
            }
        if method == "GET":
            return self.before
        if method == "PUT":
            return {
                "persona_key": "architect",
                "effective_model_id": CANONICAL,
                "source": "principal-mapping",
                "status": "configured",
                "revision": 1,
            }
        if method == "DELETE":
            return {
                "persona_key": "architect",
                "effective_model_id": "global.anthropic.claude-sonnet-4-6",
                "source": "system-default",
                "status": "not-configured",
            }
        raise AssertionError((method, path))


def parse(*argv):
    return cli.parser().parse_args(list(argv))


def test_catalog_is_persona_scoped() -> None:
    client = StubApi()
    result = cli.run(parse("catalog", "--persona", "architect"), client)
    assert result["detail"]["compatibility_class"] == "claude-agent-sdk"
    assert client.calls == [("GET", "/me/persona-models/catalog?persona_key=architect", None)]


def test_catalog_without_persona_is_usage_error() -> None:
    with pytest.raises(cli.CliError) as raised:
        parse("catalog")
    assert raised.value.exit_code == 1


@pytest.mark.parametrize("flag", ["--user", "--user-id", "--principal", "--org"])
def test_self_commands_accept_no_caller_or_tenant_identifier(flag) -> None:
    with pytest.raises(cli.CliError) as raised:
        parse("mappings", "list", flag, "other")
    assert raised.value.exit_code == 1


def test_admin_list_uses_only_the_canonical_service_principal_path() -> None:
    client = StubApi()
    cli.run(parse("mappings", "list", "--service-principal", "sp / 1"), client)
    assert client.calls[0][1] == "/service-principals/sp%20%2F%201/persona-models"


def test_service_principal_discovery_uses_server_authority() -> None:
    client = StubApi()
    result = cli.run(parse("service-principals", "list"), client)
    assert result["detail"]["principals"][0]["canonical_principal_id"] == "sp-1"


def test_set_passes_the_observed_revision() -> None:
    client = StubApi(before=mapping("global.anthropic.claude-opus-4-8", 7))
    result = cli.run(parse("mappings", "set", "--persona", "architect", "--model", CANONICAL, "--yes"), client)
    puts = [call for call in client.calls if call[0] == "PUT"]
    assert puts == [("PUT", "/me/persona-models/architect", {"model": CANONICAL, "expected_revision": 7})]
    assert result["detail"]["changed"] is True


def test_identical_set_is_idempotent_without_a_write() -> None:
    client = StubApi(before=mapping(CANONICAL, 3))
    result = cli.run(parse("mappings", "set", "--persona", "architect", "--model", CANONICAL, "--yes"), client)
    assert not any(method == "PUT" for method, _path, _body in client.calls)
    assert result["detail"]["changed"] is False


def test_identical_reset_is_idempotent_without_a_write() -> None:
    client = StubApi(before=mapping())
    result = cli.run(parse("mappings", "reset", "--persona", "architect", "--yes"), client)
    assert not any(method == "DELETE" for method, _path, _body in client.calls)
    assert result["detail"]["changed"] is False


def test_dry_run_uses_catalogue_and_never_writes() -> None:
    client = StubApi()
    result = cli.run(parse("mappings", "set", "--persona", "architect", "--model", CANONICAL, "--dry-run"), client)
    assert result["detail"]["canonical_model_id"] == CANONICAL
    assert result["detail"]["effective_destination"] == {"account_id": "123", "region": "eu-west-2"}
    assert result["detail"]["dry_run"] is True
    assert not any(method in {"PUT", "DELETE"} for method, _path, _body in client.calls)


def test_server_published_alias_is_supported_without_a_local_registry() -> None:
    client = StubApi(model_catalogue=catalogue(aliases=["opus5"]))
    result = cli.run(parse("mappings", "set", "--persona", "architect", "--model", "opus5", "--dry-run"), client)
    assert result["detail"]["canonical_model_id"] == CANONICAL


def test_missing_alias_metadata_is_an_explicit_contract_gap() -> None:
    client = StubApi(model_catalogue=catalogue())
    with pytest.raises(cli.CliError) as raised:
        cli.run(parse("mappings", "set", "--persona", "architect", "--model", "opus5", "--dry-run"), client)
    assert raised.value.code == "dry_run_unavailable"
    assert raised.value.exit_code == 4
    assert not hasattr(cli, "PERSONA_MODEL_ALIASES")


def test_unpublished_alias_can_be_validated_by_the_authoritative_write_endpoint() -> None:
    client = StubApi(model_catalogue=catalogue())
    result = cli.run(parse("mappings", "set", "--persona", "architect", "--model", "opus5", "--yes"), client)
    put = next(call for call in client.calls if call[0] == "PUT")
    assert put[2]["model"] == "opus5"
    assert result["detail"]["effective_model_id"] == CANONICAL


def test_admin_dry_run_refuses_instead_of_using_the_human_destination() -> None:
    client = StubApi()
    with pytest.raises(cli.CliError) as raised:
        cli.run(
            parse(
                "mappings",
                "set",
                "--service-principal",
                "sp-1",
                "--persona",
                "architect",
                "--model",
                CANONICAL,
                "--dry-run",
            ),
            client,
        )
    assert raised.value.code == "dry_run_unavailable"
    assert raised.value.exit_code == 4
    assert not any(method in {"PUT", "DELETE"} for method, _path, _body in client.calls)


@pytest.mark.parametrize("reason", ["probing_disabled", "evidence_stale"])
def test_waitable_catalogue_refusal_is_exit_four(reason) -> None:
    client = StubApi(model_catalogue=catalogue(selectable=False, reason=reason))
    with pytest.raises(cli.CliError) as raised:
        cli.run(parse("mappings", "set", "--persona", "architect", "--model", CANONICAL, "--dry-run"), client)
    assert raised.value.code == reason
    assert raised.value.exit_code == 4


def test_noninteractive_write_requires_yes(monkeypatch) -> None:
    client = StubApi()
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(cli.CliError) as raised:
        cli.run(parse("mappings", "set", "--persona", "architect", "--model", CANONICAL), client)
    assert raised.value.exit_code == 1
    assert not any(method == "PUT" for method, _path, _body in client.calls)


def test_concurrent_change_before_write_is_refused() -> None:
    class Racing(StubApi):
        reads = 0

        def request(self, method, path, body=None, **kwargs):
            if method == "GET" and path == "/me/persona-models":
                self.reads += 1
                if self.reads == 2:
                    self.before = mapping("global.anthropic.claude-opus-4-8", 9)
            return super().request(method, path, body, **kwargs)

    client = Racing()
    with pytest.raises(cli.CliError) as raised:
        cli.run(parse("mappings", "set", "--persona", "architect", "--model", CANONICAL, "--yes"), client)
    assert raised.value.code == "revision_conflict"
    assert not any(method == "PUT" for method, _path, _body in client.calls)


def test_409_rereads_and_reports_revision_without_echoing_body() -> None:
    class Conflicting(StubApi):
        def request(self, method, path, body=None, **kwargs):
            if method == "PUT":
                self.before = mapping("global.anthropic.claude-opus-4-8", 12)
                raise cli.HttpError(409)
            return super().request(method, path, body, **kwargs)

    with pytest.raises(cli.CliError) as raised:
        cli.run(parse("mappings", "set", "--persona", "architect", "--model", CANONICAL, "--yes"), Conflicting())
    assert raised.value.code == "revision_conflict"
    assert "12 was current as of the re-read" in str(raised.value)


def test_signed_path_refuses_targeting_another_principal() -> None:
    client = StubApi()
    client.machine = True
    with pytest.raises(cli.CliError) as raised:
        cli.run(parse("mappings", "list", "--service-principal", "sp-2"), client)
    assert raised.value.exit_code == 1
    assert client.calls == []


def test_machine_endpoint_must_be_execute_api(monkeypatch) -> None:
    monkeypatch.setenv(cli.MACHINE_ENDPOINT_ENV, "https://gateway.example.com")
    with pytest.raises(cli.CliError) as raised:
        cli.ModelsApi()
    assert raised.value.code == "invalid_machine_endpoint"


def test_signed_path_refuses_static_credentials(monkeypatch) -> None:
    import botocore.credentials
    import botocore.session

    monkeypatch.setenv(cli.MACHINE_ENDPOINT_ENV, "https://abc.execute-api.eu-west-2.amazonaws.com/dev")
    monkeypatch.setattr(
        botocore.session.Session,
        "get_credentials",
        lambda _self: botocore.credentials.Credentials("AKIASTATIC", "secret"),
    )
    client = cli.ModelsApi()
    with pytest.raises(cli.CliError) as raised:
        client.request("GET", "/me/persona-models")
    assert raised.value.code == "static_credentials_refused"


def test_signed_path_prefixes_agent_and_adds_sigv4(monkeypatch) -> None:
    import botocore.credentials
    import botocore.session

    credentials = botocore.credentials.RefreshableCredentials.create_from_metadata(
        metadata={
            "access_key": "ASIATEMPORARY",
            "secret_key": "temporary-secret",
            "token": "temporary-session-token",
            "expiry_time": "2099-01-01T00:00:00Z",
        },
        refresh_using=lambda: {},
        method="assume-role",
    )
    monkeypatch.setenv(cli.MACHINE_ENDPOINT_ENV, "https://abc.execute-api.eu-west-2.amazonaws.com/dev")
    monkeypatch.setattr(botocore.session.Session, "get_credentials", lambda _self: credentials)
    client = cli.ModelsApi()
    sent = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"principal_id":"sp-1","entries":[]}'

    class Opener:
        def open(self, request, timeout):
            sent.append((request, timeout))
            return Response()

    client.opener = Opener()
    result = client.request("GET", "/me/persona-models")
    request = sent[0][0]
    assert result["principal_id"] == "sp-1"
    assert request.full_url == "https://abc.execute-api.eu-west-2.amazonaws.com/dev/agent/me/persona-models"
    assert request.get_header("Authorization").startswith("AWS4-HMAC-SHA256 ")
    assert "X-Caller-Identity" not in request.headers


def test_safe_http_error_extracts_only_a_reason_code() -> None:
    body = BytesIO(json.dumps({"detail": {"reason": "probing_disabled", "message": "secret detail"}}).encode())
    error = urllib.error.HTTPError("https://x", 422, "", {}, body)
    result = cli._safe_http_error(error)
    assert result.code == "probing_disabled"
    assert result.exit_code == 4
    assert "secret detail" not in str(result)


def test_json_success_is_one_parseable_document(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "ModelsApi", StubApi)
    code = cli.main(["mappings", "list", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["detail"]["principal_id"] == "user-1"
    assert captured.err == ""


def test_json_usage_failure_is_one_parseable_document(capsys) -> None:
    code = cli.main(["mappings", "list", "--user", "other", "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.out)["error"]["code"] == "usage_error"


@pytest.mark.parametrize("flag", ["--api-key", "--token", "--secret", "--password"])
def test_secret_arguments_are_refused_without_echoing_the_value(flag, capsys) -> None:
    secret = "do-not-echo-this"
    code = cli.main(["mappings", "list", flag, secret, "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.out)["error"]["code"] == "secret_in_argv"
    assert secret not in captured.out + captured.err
