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


def mapping(
    saved=None,
    revision=None,
    *,
    compatibility_class="claude-agent-sdk",
    harness_contract_revision="server-harness-revision",
    class_default_status="candidate",
):
    return {
        "tenant_id": "org-1",
        "principal_kind": "human",
        "principal_id": "user-1",
        "entries": [
            {
                "persona_key": "architect",
                "compatibility_class": compatibility_class,
                "harness_contract_revision": harness_contract_revision,
                "saved_model_id": saved,
                "effective_model_id": saved or "global.anthropic.claude-sonnet-4-6",
                "effective_is_candidate": saved is None and class_default_status == "candidate",
                "source": "principal-mapping" if saved else "system-default",
                "status": "configured" if saved else "not-configured",
                "revision": revision,
                "class_default_status": class_default_status,
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
    return {"tenant_id": "org-1", "persona_key": "architect", "compatibility_class": "claude-agent-sdk", "models": [row]}


class StubApi:
    machine = False
    caller_mode = "human/bearer"

    def __init__(self, before=None, model_catalogue=None):
        self.before = before or mapping()
        self.before.setdefault("tenant_id", "org-1")
        self.catalogue = model_catalogue or catalogue()
        self.catalogue.setdefault("tenant_id", "org-1")
        self.calls = []

    def request(self, method, path, body=None, **_kwargs):
        self.calls.append((method, path, body))
        if path.endswith("/catalog?persona_key=architect"):
            return self.catalogue
        if path.endswith("/manageable-service-principals"):
            return {"tenant_id": "org-1", "principals": [{"canonical_service_principal_id": "sp-1", "manageable": True}]}
        if "/explain/" in path:
            return {
                "tenant_id": "org-1",
                "persona_key": "architect",
                "compatibility_class": "claude-agent-sdk",
                "harness_contract_revision": "server-harness-revision",
                "effective_model_id": "global.anthropic.claude-sonnet-4-6",
                "effective_is_candidate": True,
                "source": "system-default",
                "status": "not-configured",
                "default_model_id": "global.anthropic.claude-sonnet-4-6",
                "default_source": "claude-agent-sdk",
                "class_default_status": "candidate",
            }
        if method == "GET":
            return self.before
        if method == "PUT":
            return {
                "tenant_id": "org-1",
                "persona_key": "architect",
                "compatibility_class": "claude-agent-sdk",
                "harness_contract_revision": "server-harness-revision",
                "effective_model_id": CANONICAL,
                "effective_is_candidate": False,
                "source": "principal-mapping",
                "status": "configured",
                "revision": 1,
                "default_source": "claude-agent-sdk",
                "class_default_status": "candidate",
            }
        if method == "DELETE":
            return {
                "tenant_id": "org-1",
                "persona_key": "architect",
                "compatibility_class": "claude-agent-sdk",
                "harness_contract_revision": "server-harness-revision",
                "effective_model_id": "global.anthropic.claude-sonnet-4-6",
                "effective_is_candidate": True,
                "source": "system-default",
                "status": "not-configured",
                "removed": True,
                "default_model_id": "global.anthropic.claude-sonnet-4-6",
                "default_source": "claude-agent-sdk",
                "class_default_status": "candidate",
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


def test_ac05e_list_preserves_server_owned_class_and_candidate_status() -> None:
    client = StubApi(
        before=mapping(
            compatibility_class="future-sdk",
            harness_contract_revision="future-server-revision",
            class_default_status="candidate",
        )
    )
    result = cli.run(parse("mappings", "list"), client)
    entry = result["detail"]["entries"][0]

    assert entry["compatibility_class"] == "future-sdk"
    assert entry["harness_contract_revision"] == "future-server-revision"
    assert entry["effective_is_candidate"] is True
    assert entry["class_default_status"] == "candidate"
    assert entry["class_default_status"] != "proven"


def test_service_projection_uses_the_explicit_canonical_field() -> None:
    client = StubApi(before={"canonical_service_principal_id": "sp-1", "principal_kind": "service_account", "entries": mapping()["entries"]})
    result = cli.run(
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
    assert result["detail"]["principal_id"] == "sp-1"


def test_service_principal_discovery_uses_server_authority() -> None:
    client = StubApi()
    result = cli.run(parse("service-principals", "list"), client)
    assert result["detail"]["principals"][0]["canonical_service_principal_id"] == "sp-1"


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


def test_reset_passes_the_observed_revision_in_the_delete_body() -> None:
    client = StubApi(before=mapping(CANONICAL, 8))
    result = cli.run(parse("mappings", "reset", "--persona", "architect", "--yes"), client)
    deletes = [call for call in client.calls if call[0] == "DELETE"]
    assert deletes == [("DELETE", "/me/persona-models/architect", {"expected_revision": 8})]
    assert result["detail"]["tenant_id"] == "org-1"
    assert result["detail"]["changed"] is True


def test_reset_explains_unready_default_without_exposing_runtime_details(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "ModelsApi", lambda: StubApi(before=mapping(CANONICAL, 8)))
    code = cli.main(["mappings", "reset", "--persona", "architect", "--yes"])
    captured = capsys.readouterr()
    rendered = captured.out.lower()

    assert code == 0
    assert "default for this persona (not ready)" in rendered
    assert "harness_contract_revision" not in rendered
    assert "compatibility_class" not in rendered
    assert "candidate" not in rendered
    assert "the platform default" not in rendered


def test_reset_reports_unchanged_when_another_reset_won() -> None:
    class ConcurrentResetWinner(StubApi):
        def request(self, method, path, body=None, **kwargs):
            result = super().request(method, path, body, **kwargs)
            if method == "DELETE":
                return {**result, "removed": False}
            return result

    client = ConcurrentResetWinner(before=mapping(CANONICAL, 8))
    result = cli.run(parse("mappings", "reset", "--persona", "architect", "--yes"), client)
    assert result["detail"]["removed"] is False
    assert result["detail"]["changed"] is False


def test_reset_409_rereads_and_reports_revision_without_echoing_body() -> None:
    class Conflicting(StubApi):
        def request(self, method, path, body=None, **kwargs):
            if method == "DELETE":
                self.before = mapping("global.anthropic.claude-opus-4-8", 12)
                raise cli.HttpError(409)
            return super().request(method, path, body, **kwargs)

    with pytest.raises(cli.CliError) as raised:
        cli.run(
            parse("mappings", "reset", "--persona", "architect", "--yes"),
            Conflicting(before=mapping(CANONICAL, 8)),
        )
    assert raised.value.code == "revision_conflict"
    assert "12 was current as of the re-read" in str(raised.value)


def test_inconsistent_tenant_context_refuses_before_mutation() -> None:
    client = StubApi(model_catalogue={**catalogue(), "tenant_id": "org-other"})
    with pytest.raises(cli.CliError) as raised:
        cli.run(parse("mappings", "set", "--persona", "architect", "--model", CANONICAL, "--yes"), client)
    assert raised.value.code == "invalid_response"
    assert not any(method in {"PUT", "DELETE"} for method, _path, _body in client.calls)


def test_inconsistent_post_write_tenant_does_not_claim_no_write_occurred() -> None:
    class WrongMutationTenant(StubApi):
        def request(self, method, path, body=None, **kwargs):
            result = super().request(method, path, body, **kwargs)
            if method == "PUT":
                return {**result, "tenant_id": "org-other"}
            return result

    client = WrongMutationTenant()
    with pytest.raises(cli.CliError) as raised:
        cli.run(parse("mappings", "set", "--persona", "architect", "--model", CANONICAL, "--yes"), client)
    assert raised.value.code == "invalid_response"
    assert any(method == "PUT" for method, _path, _body in client.calls)
    assert "Verify mapping state" in str(raised.value)
    assert "no write" not in str(raised.value).lower()


def test_dry_run_uses_catalogue_and_never_writes() -> None:
    client = StubApi()
    result = cli.run(parse("mappings", "set", "--persona", "architect", "--model", CANONICAL, "--dry-run"), client)
    assert result["detail"]["canonical_model_id"] == CANONICAL
    assert result["detail"]["effective_destination"] == {"account_id": "123", "region": "eu-west-2"}
    assert result["detail"]["dry_run"] is True
    assert result["detail"]["tenant_id"] == "org-1"
    assert not any(method in {"PUT", "DELETE"} for method, _path, _body in client.calls)


def test_server_published_alias_is_supported_without_a_local_registry() -> None:
    client = StubApi(model_catalogue=catalogue(aliases=["opus5"]))
    result = cli.run(parse("mappings", "set", "--persona", "architect", "--model", "opus5", "--dry-run"), client)
    assert result["detail"]["canonical_model_id"] == CANONICAL


def test_missing_alias_metadata_is_an_explicit_contract_gap() -> None:
    client = StubApi(model_catalogue=catalogue())
    with pytest.raises(cli.CliError) as raised:
        cli.run(parse("mappings", "set", "--persona", "architect", "--model", "opus5", "--dry-run"), client)
    assert raised.value.code == "unknown_model"
    assert raised.value.exit_code == 5
    assert not hasattr(cli, "PERSONA_MODEL_ALIASES")


def test_unpublished_alias_is_refused_without_a_write() -> None:
    client = StubApi(model_catalogue=catalogue())
    with pytest.raises(cli.CliError) as raised:
        cli.run(parse("mappings", "set", "--persona", "architect", "--model", "opus5", "--yes"), client)
    assert raised.value.code == "unknown_model"
    assert not any(method == "PUT" for method, _path, _body in client.calls)


def test_admin_dry_run_uses_the_target_catalogue_not_human_self() -> None:
    client = StubApi()
    result = cli.run(
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
    assert result["detail"]["dry_run"] is True
    assert any(path == "/service-principals/sp-1/persona-models/catalog?persona_key=architect" for _method, path, _body in client.calls)
    assert not any(path == "/me/persona-models/catalog?persona_key=architect" for _method, path, _body in client.calls)
    assert not any(method in {"PUT", "DELETE"} for method, _path, _body in client.calls)


@pytest.mark.parametrize("reason", ["probing_disabled", "evidence_stale"])
def test_waitable_catalogue_refusal_is_exit_four(reason) -> None:
    client = StubApi(model_catalogue=catalogue(selectable=False, reason=reason))
    with pytest.raises(cli.CliError) as raised:
        cli.run(parse("mappings", "set", "--persona", "architect", "--model", CANONICAL, "--dry-run"), client)
    assert raised.value.code == reason
    assert raised.value.exit_code == 4


@pytest.mark.parametrize("action", ["set", "reset"])
def test_stale_marked_2xx_write_is_unavailable_and_requires_readback(action, monkeypatch, capsys) -> None:
    class StaleWrite(StubApi):
        def request(self, method, path, body=None, **kwargs):
            result = super().request(method, path, body, **kwargs)
            if method in {"PUT", "DELETE"}:
                return {**result, "status": "stale"}
            return result

    before = mapping(CANONICAL, 8) if action == "reset" else mapping()
    monkeypatch.setattr(cli, "ModelsApi", lambda: StaleWrite(before=before))
    argv = ["mappings", action, "--persona", "architect", "--yes"]
    if action == "set":
        argv.extend(["--model", CANONICAL])

    code = cli.main(argv)
    captured = capsys.readouterr()

    assert code == 4
    assert captured.out.splitlines()[0].startswith(f"models mappings {action}: unavailable")
    assert "The saved state is unknown." in captured.out
    assert "saved state is unknown" in captured.out
    assert "adp models mappings list" in captured.out


def test_stale_marked_2xx_write_json_uses_unavailable_status(monkeypatch, capsys) -> None:
    class StaleWrite(StubApi):
        def request(self, method, path, body=None, **kwargs):
            result = super().request(method, path, body, **kwargs)
            return {**result, "status": "stale"} if method == "PUT" else result

    monkeypatch.setattr(cli, "ModelsApi", StaleWrite)
    code = cli.main(["mappings", "set", "--persona", "architect", "--model", CANONICAL, "--yes", "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert code == 4
    assert result["status"] == "unavailable"
    assert result["detail"]["changed"] is None
    assert result["detail"]["reason"] == "evidence_stale"
    assert "saved state is unknown" in result["next_action"]
    assert captured.err == ""


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


def test_human_mutation_output_names_organization_and_account_on_first_line(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "ModelsApi", StubApi)
    code = cli.main(["mappings", "set", "--persona", "architect", "--model", CANONICAL, "--dry-run"])
    captured = capsys.readouterr()
    first_line = captured.out.splitlines()[0]
    assert code == 0
    assert "account user-1" in first_line
    assert "organization org-1" in first_line
    assert "bearer" not in first_line
    assert captured.err == ""


def test_signed_machine_mutation_names_service_account_on_first_line(monkeypatch, capsys) -> None:
    class SignedMachineStub(StubApi):
        machine = True
        caller_mode = "machine/SigV4"

        def __init__(self):
            super().__init__(
                before={
                    **mapping(CANONICAL, 3),
                    "principal_kind": "service_account",
                    "principal_id": "sp-canonical-1",
                }
            )

    monkeypatch.setattr(cli, "ModelsApi", SignedMachineStub)
    code = cli.main(["mappings", "reset", "--persona", "architect", "--yes"])
    captured = capsys.readouterr()
    first_line = captured.out.splitlines()[0]

    assert code == 0
    assert "service account sp-canonical-1" in first_line
    assert "organization org-1" in first_line
    assert "SigV4" not in first_line
    assert captured.err == ""


def test_json_usage_failure_is_one_parseable_document(capsys) -> None:
    code = cli.main(["mappings", "list", "--user", "other", "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.out)["error"]["code"] == "usage_error"


@pytest.mark.parametrize(
    "flag",
    [
        "--api-key",
        "--token",
        "--secret",
        "--password",
        "--aws-secret-access-key",
        "--aws_access_key_id",
        "--client-secret",
        "--oauth_client_secret_file",
    ],
)
def test_secret_arguments_are_refused_without_echoing_the_value(flag, capsys) -> None:
    secret = "do-not-echo-this"
    code = cli.main(["mappings", "list", flag, secret, "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.out)["error"]["code"] == "secret_in_argv"
    assert secret not in captured.out + captured.err


def test_equals_form_secret_argument_is_refused_without_echoing_the_value(capsys) -> None:
    secret = "do-not-echo-this"
    code = cli.main(["mappings", "list", f"--client-secret={secret}", "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.out)["error"]["code"] == "secret_in_argv"
    assert secret not in captured.out + captured.err


@pytest.mark.parametrize("argv", [["mappings", "list"], ["explain", "--persona", "architect"]])
def test_owner_retirement_warning_is_rendered_for_list_and_explain(argv, monkeypatch, capsys):
    warning = "Saved model is retired; choose a verified model."

    class WarningApi(StubApi):
        def request(self, method, path, body=None, **kwargs):
            result = super().request(method, path, body, **kwargs)
            entries = result.get("entries", [result])
            for entry in entries:
                entry.update(warnings=[warning], model_lifecycle="retired", availability_status="unavailable")
            return result

    monkeypatch.setattr(cli, "ModelsApi", WarningApi)
    assert cli.main(argv) == 0
    assert warning in capsys.readouterr().out
