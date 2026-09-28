"""Vault CLI and real request-schema boundaries."""

import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
spec = importlib.util.spec_from_file_location("vault_cli", CLI / "adp-vault.py")
vault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault)
ID = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def isolate_advisory_discovery(monkeypatch):
    # Serializer tests exercise the operation transport; discovery's own suite
    # covers its wire/cache behavior. Never read an operator session here.
    monkeypatch.setattr(vault.common, "ensure_can_mutate", lambda *a, **kw: None)


def args(*words):
    return vault.parser().parse_args(list(words))


def add(*extra):
    return args("credential", "add", "--service", "example", "--label", "owned", "--type", "api_key", "--operation-id", ID, *extra)


def row():
    return {
        "id": ID,
        "service": "example",
        "label": "owned",
        "credential_type": "api_key",
        "scope": "user",
        "created_at": "2026-09-25T00:00:00Z",
        "updated_at": None,
        "strict": False,
    }


def test_add_preserves_multiline_and_uses_real_create_schema(monkeypatch):
    from src.auth.vault_schemas import CredentialCreate

    client = Mock()
    client.request.return_value = {**row(), "value": "must-not-print", "secret_arn": "must-not-print"}
    secret = "line one\nline two\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret))
    result = vault.execute(add("--value-stdin", "--yes"), client)
    method, path, body = client.request.call_args.args
    assert (method, path) == ("PUT", "/auth/credentials/" + ID)
    assert CredentialCreate.model_validate(body).value == secret
    assert "must-not-print" not in json.dumps(result)
    assert secret not in json.dumps(result)


def test_dry_run_reads_no_secret_and_performs_no_mutation(monkeypatch):
    client = Mock()
    monkeypatch.setattr(vault, "read_value", Mock(side_effect=AssertionError("secret read")))
    assert vault.execute(add("--dry-run"), client)["status"] == "dry_run"
    client.request.assert_not_called()


@pytest.mark.parametrize("kind", ["public", "symlink", "directory"])
def test_secret_file_protection(tmp_path, kind):
    path = tmp_path / "value"
    path.write_text("synthetic-secret")
    path.chmod(0o600)
    if kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    else:
        path = tmp_path
    with pytest.raises((vault.common.CliError, OSError)):
        vault.read_value(add("--value-file", str(path)))


def test_update_uses_revision_adapter_and_patch_semantics():
    from src.auth.vault_schemas import CredentialMetadataUpdate

    client = Mock()
    client.request.side_effect = [[row()], {**row(), "label": "new"}]
    vault.execute(args("credential", "update", ID, "--label", "new", "--expected-revision", row()["created_at"], "--yes"), client)
    method, path, body = client.request.call_args.args
    assert method == "PATCH" and path.endswith("/metadata")
    assert set(body) == {"label", "expected_revision"}
    assert CredentialMetadataUpdate.model_validate(body).label == "new"


def test_lost_acknowledgement_never_reposts(monkeypatch):
    client = Mock()
    client.request.side_effect = vault.common.CliError("unknown", "unknown_mutation_outcome", 4)
    monkeypatch.setattr(sys, "stdin", io.StringIO("synthetic-secret"))
    with pytest.raises(vault.common.CliError) as error:
        vault.execute(add("--value-stdin", "--yes"), client)
    assert error.value.exit_code == 4
    assert client.request.call_count == 1


@pytest.mark.parametrize("response", [{}, {"id": "other", "credential_type": "api_key", "scope": "user"}])
def test_unbound_ack_is_unknown(response):
    with pytest.raises(vault.common.CliError) as error:
        vault.mutation_metadata(response, ID)
    assert error.value.exit_code == 4


def test_unverified_identity_resume_is_read_only_pending():
    client = Mock()
    client.request.return_value = [
        {"id": ID, "provider": "github", "provider_user_id": "123", "verification_method": "self_asserted", "verified_at": None}
    ]
    result = vault.execute(args("identity", "link", "--provider", "github", "--provider-user-id", "123", "--resume"), client)
    assert result["status"] == "pending"
    assert client.request.call_args.args[0] == "GET"


def test_unknown_secret_argument_not_echoed(capsys):
    assert vault.main(["credential", "add", "--value", "do-not-print-this-secret", "--json"]) == 1
    assert "do-not-print-this-secret" not in capsys.readouterr().out


def test_identity_unbound_ack_is_unknown_not_success():
    client = Mock()
    client.request.return_value = {"provider": "github", "provider_user_id": "other"}
    with pytest.raises(vault.common.CliError) as error:
        vault.execute(args("identity", "link", "--provider", "github", "--provider-user-id", "123", "--yes"), client)
    assert error.value.code == "unknown_mutation_outcome"
    assert error.value.exit_code == 4


def test_interrupt_has_structured_unknown_outcome(monkeypatch, capsys):
    monkeypatch.setattr(vault.common, "Api", Mock())
    monkeypatch.setattr(vault, "execute", Mock(side_effect=KeyboardInterrupt))
    assert vault.main(["credential", "list", "--json"]) == 130
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "pending"
    assert result["detail"]["outcome"] == "unknown"
