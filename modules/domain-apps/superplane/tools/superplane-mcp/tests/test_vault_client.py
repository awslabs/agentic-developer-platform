"""The vault client reaches the vault over HTTP and imports nothing internal.

Issue #5047 (U7), EPIC #4910. R7, client half.

Two of these tests are **structural** rather than behavioural, and that is the
point of the file:

* `test_client_imports_no_gateway_internal_module` walks the import graph. The
  issue asks for an import-graph assertion "not a comment", because "we consume the
  HTTP boundary" is a sentence that stays true in a docstring while a later edit
  quietly adds `from src.auth.vault_service import ...`.
* `test_metadata_fields_match_the_gateways_response_model` parses the gateway's own
  `vault_schemas.py`. This is the **fixture provenance** rule: fixtures derived from
  the client's own expected shape would make the client and its fixtures
  self-consistently wrong, and every test would pass against a client that disagrees
  with the real vault.

## Why this file is here and not in `superplane/tests/`

The issue proposes `modules/domain-apps/superplane/tests/test_vault_client.py`, but
the client it tests ships inside the U4 tool surface, and `superplane-domain-ci.yml`
gates that surface with `--cov=superplane_mcp --cov-fail-under=85` run from the
surface root over `tests/` only. `--cov` on a package reports every module under it,
including ones no test imported — so a suite for `vault_client.py` placed in the
module-wide `tests/` directory would leave the largest module in the package at 0%
in the run that gates it, and the gate would fail while the code was in fact tested.
Same directory as the code it covers is what the lane's own structure requires.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import re

import pytest
from superplane_mcp import vault_client as vault_client_module
from superplane_mcp.vault_client import (
    CREDENTIAL_METADATA_FIELDS,
    EXACT_BINDING_IS_MOCKED,
    VaultClient,
    VaultClientError,
)

# Repo root, from this file: tests -> superplane-mcp -> tools -> superplane ->
# domain-apps -> modules -> <root>.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[6]
_VAULT_SCHEMAS = _REPO_ROOT / "modules/gateway/src/auth/vault_schemas.py"
_VAULT_ROUTES = _REPO_ROOT / "modules/gateway/src/auth/vault_routes.py"
_CLIENT_SOURCE = (
    pathlib.Path(__file__).resolve().parent.parent / "superplane_mcp/vault_client.py"
)

# A test-only string shaped like a real AWS secret access key. It authenticates
# nothing; it exists so the "value is never logged" assertions run against a
# realistic shape.
FAKE_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

BASE_URL = "https://gateway.example.invalid"


# ---------------------------------------------------------------------------
# Fixture provenance — derived from the gateway's own response model
# ---------------------------------------------------------------------------


def _credential_response_fields() -> set[str]:
    """Read `CredentialResponse`'s field names out of the gateway's source.

    Parsed with `ast` rather than imported: importing the gateway's schema module
    pulls in FastAPI, SQLAlchemy and the gateway's settings, which is exactly the
    dependency this client exists not to have. Parsing gets the real field names
    with no import.
    """
    tree = ast.parse(_VAULT_SCHEMAS.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "CredentialResponse":
            return {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(
        "CredentialResponse not found in the gateway's vault_schemas.py"
    )


@pytest.fixture
def vault_credential_body() -> dict:
    """One vault credential response, keyed by the gateway's real field names.

    Built from `CredentialResponse`'s actual fields, so this fixture cannot drift
    into agreeing with the client instead of with the vault.
    """
    fields = _credential_response_fields()
    body = {
        "id": "cred-abc123",
        "service": "aws",
        "label": "prod-account",
        "credential_type": "aws_access_key",
        "scope": "user",
        "scopes": None,
        "expires_at": None,
        "last_used_at": None,
        "strict": False,
        "created_at": "2026-09-17T12:00:00Z",
        "updated_at": None,
    }
    missing = fields - set(body)
    assert not missing, (
        f"the gateway's CredentialResponse has field(s) this fixture does not model: "
        f"{sorted(missing)} — update the fixture from the real model"
    )
    return {k: v for k, v in body.items() if k in fields}


def test_metadata_fields_match_the_gateways_response_model() -> None:
    """The client's field list must be a subset of the vault's real response.

    Catches the drift that would otherwise be invisible: the client reading a field
    the vault does not return, with hand-written fixtures that supply it anyway.
    """
    real = _credential_response_fields()
    unknown = set(CREDENTIAL_METADATA_FIELDS) - real
    assert not unknown, (
        f"client expects field(s) the vault does not return: {sorted(unknown)}"
    )


def test_vault_response_model_returns_no_value_and_no_arn() -> None:
    """The premise this client depends on: the vault's response is metadata only.

    Asserted here rather than assumed, because the client's safety argument rests on
    it — if `CredentialResponse` ever gained a `value` or `arn` field, this client
    would start carrying secret material without a single line of it changing.
    """
    fields = _credential_response_fields()
    for banned in ("value", "arn", "secret", "secret_arn", "secret_string"):
        assert banned not in fields, f"the vault response model now exposes {banned!r}"


def _router_routes() -> set[tuple[str, str]]:
    """(method, path) for every route the vault router declares.

    Parsed from the `@router.<method>("<path>")` decorators rather than matched as
    text, so reformatting the gateway's source cannot break these tests and a
    substring cannot accidentally satisfy them.
    """
    tree = ast.parse(_VAULT_ROUTES.read_text())
    routes: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "router"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            ):
                routes.add((decorator.func.attr.upper(), decorator.args[0].value))
    return routes


def test_the_consumed_endpoints_exist_on_the_vault_router() -> None:
    """Every path this client calls must exist on the vault's router.

    A client naming an endpoint the vault does not serve fails at runtime with a 404
    that only a live environment would reveal — this turns that into a unit failure.
    The `/auth` prefix is asserted too, since the client's paths are relative to it.
    """
    assert 'APIRouter(prefix="/auth"' in _VAULT_ROUTES.read_text()
    routes = _router_routes()
    for expected in (
        ("POST", "/credentials"),
        ("GET", "/credentials"),
        ("DELETE", "/credentials/{credential_id}"),
    ):
        assert expected in routes, f"the vault router no longer serves {expected}"


def test_the_vault_serves_no_get_by_id_route() -> None:
    """The premise of the client-side filter in `resolve_exact()`.

    If the vault ever adds `GET /credentials/{credential_id}`, the mock should be
    replaced by a real single-credential read — so this fails to say so, rather than
    the filter staying in place unnoticed.
    """
    assert ("GET", "/credentials/{credential_id}") not in _router_routes()


# ---------------------------------------------------------------------------
# The import-graph assertion (issue: "not a comment")
# ---------------------------------------------------------------------------

# Gateway-internal modules this client must never import. Named individually
# rather than as a prefix match, so the failure message says which one appeared.
_FORBIDDEN_IMPORTS = (
    "vault_service",
    "credential_resolver",
    "vault_routes",
    "vault_schemas",
    "secrets_manager",
    "src.auth",
    "src.shared",
    "sqlalchemy",
    "boto3",
    "fastapi",
)


def _imported_names(path: pathlib.Path) -> set[str]:
    """Every module name imported by a source file, via its AST."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_client_imports_no_gateway_internal_module() -> None:
    """The client consumes the vault's HTTP API and imports no gateway internals.

    This is the structural half of "does not import vault_service.py or
    credential_resolver.py internals into another process". `sqlalchemy`, `boto3`
    and `fastapi` are in the forbidden list too: importing any of them is how a
    "thin HTTP client" acquires direct storage or provider access without anyone
    deciding to give it that.
    """
    imported = _imported_names(_CLIENT_SOURCE)
    for name in imported:
        for forbidden in _FORBIDDEN_IMPORTS:
            assert not (name == forbidden or name.startswith(f"{forbidden}.")), (
                f"vault_client.py imports {name!r}: the vault must be consumed over "
                f"HTTP, not by importing gateway internals into this process"
            )


def test_client_reaches_the_vault_only_over_http() -> None:
    """Positively: the transport is an HTTP one, so the check above is meaningful.

    Without this, deleting every import would satisfy the negative assertion.
    """
    imported = _imported_names(_CLIENT_SOURCE)
    assert {"urllib.request", "urllib.error"} <= imported


# ---------------------------------------------------------------------------
# Recorded mock (acceptance-split rule 5)
# ---------------------------------------------------------------------------


def test_exact_binding_is_recorded_as_a_mock() -> None:
    """B's exact-credential binding does not exist, and the client says so.

    A mock returning plausible values with no marker is indistinguishable from a
    real reading to whoever consumes it.
    """
    assert EXACT_BINDING_IS_MOCKED is True


def test_b_still_has_no_exact_binding_api() -> None:
    """The premise of the mock, asserted against B's real source.

    When B grows `resolve_by_id`/`resolve_exact`, this test fails — which is the
    intended signal to replace the mock rather than leave it recorded forever.
    """
    resolver = _REPO_ROOT / "modules/gateway/src/shared/services/credential_resolver.py"
    tree = ast.parse(resolver.read_text())
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "resolve_by_id" not in methods
    assert "resolve_exact" not in methods
    # And the mechanism the issue says is *not* exact binding is still what exists.
    assert "resolve" in methods


def test_resolve_exact_reports_its_provenance(vault_credential_body: dict) -> None:
    """A caller cannot take the credential without also receiving the mock marker."""
    client, _ = _client([(200, [vault_credential_body])])
    credential, provenance = client.resolve_exact("cred-abc123")
    assert credential is not None
    assert credential.credential_id == "cred-abc123"
    assert provenance["exact_binding"] == "mock"
    assert "client-side filter" in provenance["mechanism"]


def test_resolve_exact_returns_none_for_an_unknown_id(
    vault_credential_body: dict,
) -> None:
    client, _ = _client([(200, [vault_credential_body])])
    credential, provenance = client.resolve_exact("cred-nope")
    assert credential is None
    assert provenance["exact_binding"] == "mock"


def test_resolve_exact_requires_a_credential_id() -> None:
    client, _ = _client([])
    with pytest.raises(ValueError):
        client.resolve_exact("  ")


# ---------------------------------------------------------------------------
# Request construction and response handling
# ---------------------------------------------------------------------------


def _client(responses: list[tuple[int, object]]) -> tuple[VaultClient, list[dict]]:
    """A client whose transport replays `responses` and records every call."""
    calls: list[dict] = []
    queue = list(responses)

    def transport(method, url, body, headers):
        calls.append(
            {"method": method, "url": url, "body": body, "headers": dict(headers)}
        )
        if not queue:
            raise AssertionError(f"unexpected extra vault call: {method} {url}")
        return queue.pop(0)

    return VaultClient(BASE_URL, lambda: "test-token", transport=transport), calls


def test_register_posts_the_value_to_the_vault_endpoint(
    vault_credential_body: dict,
) -> None:
    """The one value-carrying call goes to POST /auth/credentials and nowhere else."""
    client, calls = _client([(201, vault_credential_body)])
    credential = client.register_provider_credential(
        service="aws",
        label="prod-account",
        credential_type="aws_access_key",
        value=FAKE_SECRET,
    )
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == f"{BASE_URL}/auth/credentials"
    assert credential.credential_id == "cred-abc123"
    assert credential.service == "aws"


def test_register_returns_metadata_with_no_value_attribute(
    vault_credential_body: dict,
) -> None:
    """The returned object has no attribute through which a secret could travel."""
    client, _ = _client([(201, vault_credential_body)])
    credential = client.register_provider_credential(
        service="aws", label="prod", credential_type="aws_access_key", value=FAKE_SECRET
    )
    for banned in ("value", "arn", "secret"):
        assert not hasattr(credential, banned)
    assert FAKE_SECRET not in repr(credential)


def test_register_does_not_log_the_value(
    vault_credential_body: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """acc. 4: the value is not in a log line, including on the success path."""
    client, _ = _client([(201, vault_credential_body)])
    with caplog.at_level(logging.DEBUG):
        client.register_provider_credential(
            service="aws",
            label="prod",
            credential_type="aws_access_key",
            value=FAKE_SECRET,
        )
    assert FAKE_SECRET not in caplog.text


def test_a_vault_failure_does_not_echo_the_request_body() -> None:
    """An error message must not carry the value that was just submitted.

    A validation error echoing its input is the classic way a secret reaches a log,
    and an exception string reaches logs and agent transcripts alike.
    """
    client, _ = _client([(422, {"detail": f"invalid value: {FAKE_SECRET}"})])
    with pytest.raises(VaultClientError) as exc:
        client.register_provider_credential(
            service="aws",
            label="prod",
            credential_type="aws_access_key",
            value=FAKE_SECRET,
        )
    assert FAKE_SECRET not in str(exc.value)
    assert "422" in str(exc.value)


def test_register_requires_a_value() -> None:
    client, calls = _client([])
    with pytest.raises(ValueError):
        client.register_provider_credential(
            service="aws", label="prod", credential_type="aws_access_key", value=""
        )
    assert calls == []


def test_list_credentials_returns_metadata(vault_credential_body: dict) -> None:
    client, calls = _client([(200, [vault_credential_body])])
    credentials = client.list_credentials()
    assert len(credentials) == 1
    assert credentials[0].label == "prod-account"
    assert credentials[0].owner_scope == "user"
    assert calls[0]["method"] == "GET"


def test_list_credentials_passes_the_scope_filter(vault_credential_body: dict) -> None:
    client, calls = _client([(200, [vault_credential_body])])
    client.list_credentials(scope="domain_app")
    assert calls[0]["url"].endswith("/auth/credentials?scope=domain_app")


def test_list_credentials_handles_an_empty_vault() -> None:
    client, _ = _client([(200, [])])
    assert client.list_credentials() == ()


def test_list_credentials_rejects_a_non_list_body() -> None:
    client, _ = _client([(200, {"unexpected": "shape"})])
    with pytest.raises(VaultClientError, match="not a list"):
        client.list_credentials()


def test_a_response_missing_required_fields_is_refused() -> None:
    """A malformed vault response fails loudly rather than yielding a blank id."""
    client, _ = _client([(200, [{"service": "aws", "label": "prod"}])])
    with pytest.raises(VaultClientError, match="missing required field"):
        client.list_credentials()


def test_register_refuses_a_bodyless_response() -> None:
    client, _ = _client([(201, None)])
    with pytest.raises(VaultClientError, match="no credential body"):
        client.register_provider_credential(
            service="aws",
            label="prod",
            credential_type="aws_access_key",
            value=FAKE_SECRET,
        )


def test_every_call_carries_the_bearer_token(vault_credential_body: dict) -> None:
    """The vault requires Cognito JWT on these endpoints."""
    client, calls = _client([(200, [vault_credential_body])])
    client.list_credentials()
    assert calls[0]["headers"]["authorization"] == "Bearer test-token"


def test_the_token_is_read_per_call() -> None:
    """A refreshed token must be picked up by a long-lived client."""
    tokens = iter(["first", "second"])
    calls: list[dict] = []

    def transport(method, url, body, headers):
        calls.append(dict(headers))
        return 200, []

    client = VaultClient(BASE_URL, lambda: next(tokens), transport=transport)
    client.list_credentials()
    client.list_credentials()
    assert calls[0]["authorization"] == "Bearer first"
    assert calls[1]["authorization"] == "Bearer second"


def test_revoke_calls_delete_on_the_credential(vault_credential_body: dict) -> None:
    """Revocation is its own call — the separate later step after a rotation."""
    client, calls = _client([(204, None)])
    client.revoke_credential("cred-abc123")
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"] == f"{BASE_URL}/auth/credentials/cred-abc123"


def test_revoke_requires_a_credential_id() -> None:
    client, calls = _client([])
    with pytest.raises(ValueError):
        client.revoke_credential("")
    assert calls == []


def test_rotation_does_not_revoke_the_old_credential(
    vault_credential_body: dict,
) -> None:
    """acc. 5, from the client's side: registering a replacement deletes nothing.

    The contract's `rotate()` returns the old reference as superseded, and there is
    no client method combining registration with revocation — so no single call can
    produce the window where neither credential serves.
    """
    client, calls = _client([(201, vault_credential_body)])
    client.register_provider_credential(
        service="aws",
        label="replacement",
        credential_type="aws_access_key",
        value=FAKE_SECRET,
    )
    assert [call["method"] for call in calls] == ["POST"]
    assert not any(call["method"] == "DELETE" for call in calls)


def test_client_exposes_no_combined_rotate_and_delete() -> None:
    """Asserted as an absence: a convenience method here would reintroduce the window."""
    for banned in (
        "rotate",
        "rotate_and_revoke",
        "replace_credential",
        "get_value",
        "read_secret",
    ):
        assert not hasattr(VaultClient, banned)


def test_base_url_is_required() -> None:
    with pytest.raises(ValueError):
        VaultClient("", lambda: "t")


def test_trailing_slash_in_base_url_does_not_double_up(
    vault_credential_body: dict,
) -> None:
    calls: list[str] = []

    def transport(method, url, body, headers):
        calls.append(url)
        return 200, []

    VaultClient(f"{BASE_URL}/", lambda: "t", transport=transport).list_credentials()
    assert calls[0] == f"{BASE_URL}/auth/credentials"


# ---------------------------------------------------------------------------
# The default transport
#
# Covered because it is the code that actually reaches the vault, and it carries an
# acceptance-4 property of its own: it must not read an HTTPError's body. Exercised
# by monkeypatching `urlopen`, so nothing here opens a socket and the offline lane
# stays offline.
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    """Minimal stand-in for the context manager `urlopen` returns."""

    def __init__(self, status: int, raw: bytes) -> None:
        self.status = status
        self._raw = raw

    def read(self, *_args) -> bytes:
        return self._raw

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, result) -> list:
    """Replace `urlopen` with a fake, recording the request it was handed."""
    seen: list = []

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        if isinstance(result, Exception):
            raise result
        return result

    from types import SimpleNamespace

    monkeypatch.setattr(
        vault_client_module.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=fake_urlopen),
    )
    return seen


def test_default_transport_sends_method_headers_and_body(
    monkeypatch: pytest.MonkeyPatch, vault_credential_body: dict
) -> None:
    """The real transport builds the request the vault expects."""
    import json as _json

    seen = _patch_urlopen(
        monkeypatch, _FakeHTTPResponse(201, _json.dumps(vault_credential_body).encode())
    )
    client = VaultClient(BASE_URL, lambda: "tok")
    credential = client.register_provider_credential(
        service="aws", label="prod", credential_type="aws_access_key", value=FAKE_SECRET
    )
    assert credential.credential_id == "cred-abc123"
    request = seen[0]
    assert request.method == "POST"
    assert request.full_url == f"{BASE_URL}/auth/credentials"
    assert request.get_header("Authorization") == "Bearer tok"
    assert request.data is not None


def test_default_transport_does_not_read_an_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """acc. 4 in the transport: an HTTP error's body is never read.

    The vault's 422 body could echo the value that was just submitted. This asserts
    the body is not even consumed, so it cannot reach the exception message: reading
    it and then choosing not to format it would be one careless edit from a leak.
    """
    read_attempts: list[str] = []

    class ExplodingBody:
        def read(self, *_args: object) -> bytes:
            read_attempts.append("read")
            return f"invalid value: {FAKE_SECRET}".encode()

        def close(self) -> None:
            # HTTPError adopts `fp` and closes it during cleanup. Without this the
            # test passes but emits an unraisable AttributeError from tempfile's
            # finalizer, which would later look like a failure in whatever test
            # happened to run next.
            return None

    error = vault_client_module.urllib.error.HTTPError(
        url=f"{BASE_URL}/auth/credentials",
        code=422,
        msg="Unprocessable",
        hdrs=None,
        fp=ExplodingBody(),
    )
    _patch_urlopen(monkeypatch, error)
    client = VaultClient(BASE_URL, lambda: "tok")
    with pytest.raises(VaultClientError) as exc:
        client.list_credentials()
    assert FAKE_SECRET not in str(exc.value)
    assert read_attempts == [], "the transport read the error body; it must not"


def test_default_transport_reports_an_unreachable_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection failure is a VaultClientError, not a raw URLError."""
    _patch_urlopen(
        monkeypatch, vault_client_module.urllib.error.URLError("name resolution failed")
    )
    client = VaultClient(BASE_URL, lambda: "tok")
    with pytest.raises(VaultClientError, match="unreachable"):
        client.list_credentials()


def test_default_transport_handles_an_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """204 on DELETE carries no body, which must not be a JSON decode error."""
    _patch_urlopen(monkeypatch, _FakeHTTPResponse(204, b""))
    VaultClient(BASE_URL, lambda: "tok").revoke_credential("cred-abc123")


def test_default_transport_refuses_a_non_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTML error page from a proxy must fail clearly, not as a decode traceback."""
    _patch_urlopen(monkeypatch, _FakeHTTPResponse(200, b"<html>502 Bad Gateway</html>"))
    client = VaultClient(BASE_URL, lambda: "tok")
    with pytest.raises(VaultClientError, match="non-JSON"):
        client.list_credentials()


def test_no_hardcoded_environment_in_the_client() -> None:
    """No account id, region-qualified host or environment baked into the source."""
    source = _CLIENT_SOURCE.read_text()
    assert not re.search(r"\b\d{12}\b", source), (
        "a 12-digit account id appears in the client"
    )
    assert "amazonaws.com" not in source


@pytest.mark.parametrize(
    "url",
    [
        "http://vault.example/api",
        "https://user:pass@vault.example/api",
        "https://vault.example/api?x=y",
        "https://vault.example/api#fragment",
    ],
)
def test_credential_transport_requires_a_clean_https_endpoint(url):
    with pytest.raises(ValueError):
        VaultClient(url, lambda: "token")


def test_redirect_handler_refuses_to_forward_credentials():
    handler = vault_client_module._RefuseRedirects()
    request = vault_client_module.urllib.request.Request(
        BASE_URL, headers={"Authorization": "Bearer private"}
    )
    assert (
        handler.redirect_request(
            request, None, 302, "redirect", {}, "https://elsewhere.example"
        )
        is None
    )


def test_unsafe_metadata_never_reaches_response(vault_credential_body):
    from dataclasses import replace

    credential = VaultClient._reference_from(vault_credential_body)
    with pytest.raises(VaultClientError, match="unsafe"):
        replace(credential, label="AKIAIOSFODNN7EXAMPLE")


def test_error_reason_is_not_republished(monkeypatch):
    _patch_urlopen(monkeypatch, vault_client_module.urllib.error.URLError(FAKE_SECRET))
    with pytest.raises(VaultClientError) as caught:
        VaultClient(BASE_URL, lambda: "token").list_credentials()
    assert FAKE_SECRET not in str(caught.value)
