"""CLI foundation tests using the installed helper protocol and fake providers."""

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
import adp_common as common  # noqa: E402

spec = importlib.util.spec_from_file_location("adp_admin", CLI / "adp-admin.py")
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    common.write_json(common.config_path(), {"gateway_url": "https://adp.example/api", "other": "preserved"})
    return tmp_path


def test_native_login_saves_existing_session_format(home, monkeypatch):
    secret_file = home / "credentials.json"
    common.write_json(secret_file, {"username": "admin", "password": "password-secret", "software_token_mfa_code": "123456"})
    calls = []

    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        if path.endswith("password"):
            return {"challenge": "SOFTWARE_TOKEN_MFA", "continuation": "continuation-secret"}
        if path.endswith("challenge"):
            return {
                "access_token": "access-secret",
                "id_token": "id-secret",
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
                "client_id": "cli-client",
                "user_pool_id": "pool",
                "region": "us-east-1",
            }
        return {"verified": True, "user_id": "admin"}

    result = admin.login(admin.parser().parse_args(["login", "--credentials-file", str(secret_file)]), SimpleNamespace(request=request))
    assert result["status"] == "verified"
    assert "secret" not in json.dumps(result)
    tokens = common.read_private_json(home / ".bedrock-gateway/tokens.json")
    config = common.read_private_json(common.config_path())
    assert tokens["refresh_token"] == "refresh-secret"
    assert tokens["expires_at"] > 0
    assert config["refresh_via"] == "gateway" and config["other"] == "preserved"
    assert calls[-1][3]["token"] == "access-secret"
    assert calls[1][2]["responses"] == {"SOFTWARE_TOKEN_MFA_CODE": "123456"}
    assert not (home / ".bedrock-gateway/refresh.lock").exists()


def test_non_admin_login_does_not_replace_session(home):
    common.write_json(home / "credentials.json", {"username": "user", "password": "secret"})

    def request(method, path, body=None, **kwargs):
        if path.endswith("password"):
            return {"access_token": "token"}
        raise common.CliError("Admin required", "forbidden", 3)

    with pytest.raises(common.CliError) as failure:
        admin.login(admin.parser().parse_args(["login", "--credentials-file", str(home / "credentials.json")]), SimpleNamespace(request=request))
    assert failure.value.exit_code == 3
    assert not (home / ".bedrock-gateway/tokens.json").exists()


def test_scripted_login_does_not_prompt_without_inputs(home, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(common.CliError, match="protected|non-interactive"):
        admin.login(admin.parser().parse_args(["login"]), None)


def test_credentials_file_must_be_private(home):
    path = home / "credentials.json"
    path.write_text('{"password":"secret"}')
    path.chmod(0o644)
    with pytest.raises(common.CliError, match="0600"):
        common.read_private_json(path)
    path.chmod(0o600)
    link = home / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        common.read_private_json(link)


@pytest.mark.parametrize(
    "url", ["http://remote.example/api", "https://user:password@host/api", "https://host/api?token=secret", "https://host/api#fragment"]
)
def test_gateway_rejects_unsafe_origins(home, url):
    common.write_json(common.config_path(), {"gateway_url": url})
    with pytest.raises(common.CliError):
        common.Api()


def test_http_error_and_redirect_never_leak_secrets(home, monkeypatch):
    import io

    monkeypatch.setattr(common, "access_token", lambda: "private-token")
    api = common.Api()

    def reject(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://adp.example", 403, "secret-message", {}, io.BytesIO(b'{"detail":{"error":"forbidden","message":"private-token"}}')
        )

    monkeypatch.setattr(api.opener, "open", reject)
    with pytest.raises(common.CliError) as failure:
        api.request("GET", "/admin")
    assert failure.value.exit_code == 3
    assert "private-token" not in str(failure.value)
    with pytest.raises(common.CliError, match="redirected"):
        common.NoRedirect().redirect_request(None, None, 302, None, None, "https://attacker.example")


def test_setup_reports_missing_providers_without_claiming_success(home, monkeypatch):
    monkeypatch.setattr(common, "load_provider", lambda filename: None)
    api = SimpleNamespace(base="https://adp.example/api", request=lambda *args: {"org_id": "org"})
    result = admin.setup(admin.parser().parse_args(["setup", "--json"]), api)
    assert result["status"] == "pending"
    assert all(step["status"] == "unavailable" for step in result["detail"]["steps"])


def test_setup_checks_all_statuses_first_and_resumes_only_missing_steps(home, monkeypatch):
    calls = []

    def provider(name, state):
        def status(ctx):
            calls.append(("status", name, ctx["org"]))
            return common.envelope(state, name)

        def configure(ctx):
            calls.append(("configure", name, ctx["org"]))
            return common.envelope("pending", name, next_action="Ask the AWS administrator.")

        return SimpleNamespace(status=status, configure=configure)

    providers = {"adp-bedrock.py": provider("bedrock", "pending"), "adp-github-admin.py": provider("github", "verified")}
    monkeypatch.setattr(common, "load_provider", lambda filename: providers[filename])
    api = SimpleNamespace(base="https://adp.example/api", request=lambda *args: {"org_id": "org"})
    result = admin.setup(admin.parser().parse_args(["setup", "--json"]), api)
    assert calls == [("status", "bedrock", "org"), ("status", "github", "org"), ("configure", "bedrock", "org")]
    assert result["status"] == "pending"
    assert common.read_state("admin")["gateway_url"] == api.base


def test_setup_dry_run_does_not_write_state_or_configure(home, monkeypatch):
    def forbidden(ctx):
        pytest.fail("dry-run invoked configure")

    monkeypatch.setattr(
        common, "load_provider", lambda filename: SimpleNamespace(status=lambda ctx: common.envelope("pending", filename), configure=forbidden)
    )
    api = SimpleNamespace(base="https://adp.example/api", request=lambda *args: {"org_id": "org"})
    admin.setup(admin.parser().parse_args(["setup", "--dry-run", "--json"]), api)
    assert not (home / ".adp/state/admin.json").exists()


def test_state_writes_are_private_and_round_trip(home):
    common.write_state("fixture", {"destination_id": "destination"})
    path = common.state_path("fixture")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert common.read_state("fixture") == {"destination_id": "destination"}


def test_json_errors_are_one_safe_object_on_stdout(home, capsys):
    result = admin.main(["login", "--json", "--bad-option"])
    output = capsys.readouterr()
    assert result == 1
    assert json.loads(output.out)["error"]["code"] == "usage_error"
    assert not output.err


def test_installed_admin_help_preserves_existing_dispatch(run_adp):
    result = run_adp(["admin", "--help"])
    assert result.returncode == 0
    assert "login" in result.stdout and "setup" in result.stdout
