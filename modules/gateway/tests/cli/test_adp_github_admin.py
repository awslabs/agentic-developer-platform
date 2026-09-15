"""Administrator GitHub App configuration, exercised without live GitHub or real credentials.

Covers the journeys in #5183: fresh deployment, new-App registration and its
administrator handoff, existing-App import, incomplete OAuth details, drifted
webhook/permissions, missing ADP and GitHub privileges, resume, and the security
invariants (no secret in argv, output or saved state).
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
import adp_common as common  # noqa: E402

spec = importlib.util.spec_from_file_location("adp_github_admin", CLI / "adp-github-admin.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

APP_ID = "987654"
PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nfixture-key-material\n-----END RSA PRIVATE KEY-----\n"
OAUTH_SECRET = "oauth-client-secret-fixture"
WEBHOOK_SECRET = "webhook-secret-fixture"
CALLBACK = "https://gateway.example.test/auth/github/callback"
SETTINGS_URL = "https://github.com/organizations/SOPHOS-IT/settings/apps/sophos-adp/advanced"

HEALTHY_CONNECTION = {
    "installation_id": 42,
    "account_login": "SOPHOS-IT",
    "verification": {"record_present": True, "tenant_secret_seeded": True, "identity_index_row": True, "reverse_identity_row": True},
}
HEALTHY_PLATFORM = {
    "login_credentials": True,
    "webhook_secret": True,
    "app_webhook_url_matches": True,
    "app_permissions_match": True,
    "app_events_match": True,
    "expected_callback_url": CALLBACK,
    "app_oauth_settings_url": SETTINGS_URL,
    "app_config_warnings": [],
}


class FakeApi:
    """The five endpoints this story consumes; anything else is a test failure."""

    base = "https://gateway.example.test/api"

    def __init__(self, **overrides):
        self.calls = []
        self.app = {"registered": False, "install_ready": False, "login_enabled": False}
        self.connections = []
        self.platform = {}
        self.start = {
            "status": "ready",
            "manifest": {"name": "sophos-adp", "hook_attributes": {"url": "https://hook.example.test"}},
            "post_url": "https://github.com/organizations/SOPHOS-IT/settings/apps/new",
            "state": "state-nonce-fixture",
            "suggested_app_name": "sophos-adp-agent-platform",
        }
        self.manual = {"registered": True, "app_id": APP_ID, "app_slug": "sophos-adp", "login_enabled": True, "warnings": []}
        self.revalidation = {
            "checked": True,
            "app_webhook_url_matches": True,
            "app_permissions_match": True,
            "app_events_match": True,
            "expected_callback_url": CALLBACK,
            "app_oauth_settings_url": SETTINGS_URL,
            "warnings": [],
        }
        self.error = None
        self.__dict__.update(overrides)

    def registered(self, **extra):
        """Mark the deployment as already having an App."""
        self.app = {"registered": True, "install_ready": True, "login_enabled": True, "app_id": APP_ID, "app_slug": "sophos-adp", **extra}
        self.connections = [HEALTHY_CONNECTION]
        self.platform = dict(HEALTHY_PLATFORM)
        return self

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        if self.error:
            raise self.error
        if path == cli.APP + "/status":
            return dict(self.app)
        if path == "/admin/connections":
            return {"connections": self.connections, "platform_verification": self.platform}
        if path == cli.APP + "/register-start":
            return dict(self.start)
        if path == cli.APP + "/register-manual":
            return dict(self.manual)
        if path == cli.APP + "/revalidate":
            return dict(self.revalidation)
        raise AssertionError((method, path))

    @property
    def mutations(self):
        return [path for method, path, _ in self.calls if method != "GET"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    common.write_json(common.config_path(), {"gateway_url": FakeApi.base})
    return tmp_path


def arguments(*argv):
    return cli.parser().parse_args(list(argv))


def secret_file(home, name, payload):
    path = home / name
    common.write_json(path, payload)
    return str(path)


# ---------------------------------------------------------------------------
# Status: three separate readings, and no unearned ready claim
# ---------------------------------------------------------------------------


def test_fresh_deployment_reports_no_app_and_points_at_setup(home):
    api = FakeApi()
    result = cli.run(arguments("status", "--json"), api, interactive=False)
    assert result["status"] == "pending"
    assert result["detail"]["registered"] is False
    assert "adp admin github setup" in result["next_action"]
    assert api.mutations == [], "a status read must not mutate configuration"


def test_status_reports_sign_in_repositories_and_integration_separately(home):
    result = cli.run(arguments("status", "--json"), FakeApi().registered(), interactive=False)
    assert result["status"] == "configured"
    assert set(result["detail"]) >= {"sign_in", "repositories", "agent_integration"}
    assert result["detail"]["sign_in"]["credentials_configured"] is True
    assert result["detail"]["repositories"]["complete_installation_records"] == 1
    assert result["detail"]["agent_integration"]["permissions"] == "ok"


def test_configured_credentials_never_claim_a_verified_login(home):
    result = cli.run(arguments("status", "--json"), FakeApi().registered(), interactive=False)
    # GitHub exposes no readable OAuth callback URL, so a real round trip is the
    # only thing that could prove login — and nothing here performed one.
    assert result["status"] != "verified"
    assert result["detail"]["sign_in"]["login_verified"] is False
    assert result["detail"]["sign_in"]["expected_callback_url"] == CALLBACK
    assert result["detail"]["sign_in"]["app_oauth_settings_url"] == SETTINGS_URL
    assert "Sign in with GitHub once" in result["next_action"]


@pytest.mark.parametrize(
    "app_patch, platform_patch, connections, incomplete",
    [
        ({"login_enabled": False}, {}, [HEALTHY_CONNECTION], "sign in"),
        ({}, {}, [], "repositories"),
        (
            {},
            {},
            [{"installation_id": 7, "verification": {"record_present": True, "tenant_secret_seeded": False, "identity_index_row": True}}],
            "repositories",
        ),
        ({}, {"app_webhook_url_matches": False}, [HEALTHY_CONNECTION], "agent integration"),
        ({}, {"app_permissions_match": False}, [HEALTHY_CONNECTION], "agent integration"),
        ({}, {"webhook_secret": False}, [HEALTHY_CONNECTION], "agent integration"),
    ],
)
def test_any_incomplete_area_prevents_an_overall_ready_claim(home, app_patch, platform_patch, connections, incomplete):
    api = FakeApi().registered(**app_patch)
    api.platform.update(platform_patch)
    api.connections = connections
    result = cli.run(arguments("status", "--json"), api, interactive=False)
    assert result["status"] == "pending"
    assert incomplete in result["next_action"]


def test_unknown_checks_are_reported_as_unknown_not_passed(home):
    api = FakeApi().registered()
    api.platform.update(app_permissions_match=None, app_events_match=None)
    result = cli.run(arguments("status", "--json"), api, interactive=False)
    integration = result["detail"]["agent_integration"]
    assert integration["permissions"] == "unknown" and integration["events"] == "unknown"
    assert result["status"] == "pending", "an undetermined check must not read as configured"


def test_status_survives_an_unreadable_connections_list_without_claiming_health(home):
    api = FakeApi().registered()
    original = api.request

    def request(method, path, body=None, **kwargs):
        if path == "/admin/connections":
            raise common.CliError("Gateway unavailable", "gateway_unavailable")
        return original(method, path, body, **kwargs)

    api.request = request
    result = cli.run(arguments("status", "--json"), api, interactive=False)
    assert result["status"] == "pending"
    assert result["detail"]["agent_integration"]["webhook_url"] == "unknown"


# ---------------------------------------------------------------------------
# Authorization: the server decides, and the CLI reports the right exit code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure, code",
    [
        (common.CliError("Sign in again.", "authentication_required", 2), 2),
        (common.CliError("Administrator privileges required.", "forbidden", 3), 3),
    ],
)
def test_adp_permission_refusals_surface_their_own_exit_code(home, failure, code, capsys):
    api = FakeApi()
    api.error = failure
    monkey = cli.Api
    try:
        cli.Api = lambda: api
        assert cli.main(["status", "--json"]) == code
    finally:
        cli.Api = monkey
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_non_admin_refusal_does_not_attempt_any_registration(home):
    api = FakeApi()
    api.error = common.CliError("Administrator privileges required.", "forbidden", 3)
    with pytest.raises(common.CliError):
        cli.run(arguments("setup", "--new", "--github-org", "SOPHOS-IT", "--json"), api, interactive=False)
    assert api.mutations == []


# ---------------------------------------------------------------------------
# New-App setup: manifest flow, ownership, and the administrator handoff
# ---------------------------------------------------------------------------


def test_new_app_setup_stages_the_manifest_and_stays_resumable(home):
    api = FakeApi()
    result = cli.run(arguments("setup", "--new", "--github-org", "SOPHOS-IT", "--yes", "--json"), api, interactive=False)
    assert result["status"] == "pending", "the App does not exist until a GitHub owner approves it"
    assert api.mutations == [cli.APP + "/register-start"]
    assert api.calls[-1][2] == {"owner_type": "org", "org": "SOPHOS-IT", "visibility": "private"}
    page = Path(result["detail"]["approval_page"])
    assert page.read_text().count(api.start["post_url"]) == 1
    assert api.start["state"] in page.read_text(), "GitHub echoes this nonce back to the callback"
    assert stat.S_IMODE(page.stat().st_mode) == 0o600
    assert common.read_state(cli.NAME)["stage"] == "awaiting_github_approval"


def test_pending_github_owner_approval_is_exit_4_not_a_failure(home, monkeypatch, capsys):
    monkeypatch.setattr(common, "open_browser", lambda url: False)
    api = FakeApi()
    monkeypatch.setattr(cli, "Api", lambda: api)
    assert cli.main(["setup", "--new", "--github-org", "SOPHOS-IT", "--yes", "--json"]) == 4
    result = json.loads(capsys.readouterr().out)
    assert "must approve" in result["next_action"] and "SOPHOS-IT" in result["next_action"]


def test_headless_run_prints_the_page_so_a_browserless_machine_can_finish(home, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(common, "open_browser", lambda url: opened.append(url) or False)
    cli.run(arguments("setup", "--new", "--github-org", "SOPHOS-IT", "--yes", "--json"), FakeApi(), interactive=False)
    assert opened and opened[0].startswith("file://")
    assert "Open this page" in capsys.readouterr().err


def test_adp_organization_is_never_substituted_for_github_app_ownership(home):
    api = FakeApi()
    with pytest.raises(common.CliError) as exc:
        cli.run(arguments("setup", "--new", "--org", "sophos-adp-org", "--yes", "--json"), api, interactive=False)
    assert "--github-org" in str(exc.value)
    assert api.mutations == [], "an ADP org name must never become the GitHub App owner"


def test_personal_app_requires_an_explicit_owner_choice(home, capsys):
    api = FakeApi()
    result = cli.run(arguments("setup", "--new", "--owner", "user", "--yes", "--json"), api, interactive=False)
    assert api.calls[-1][2]["owner_type"] == "user"
    assert "personal GitHub account" in capsys.readouterr().err
    assert "personal" in result["detail"]["app_owner"]


def test_already_registered_app_is_reused_rather_than_duplicated(home, capsys):
    api = FakeApi().registered()
    result = cli.run(arguments("setup", "--new", "--github-org", "SOPHOS-IT", "--yes", "--json"), api, interactive=False)
    assert cli.APP + "/register-start" not in api.mutations
    assert result["detail"]["app_id"] == APP_ID
    assert "already registered" in capsys.readouterr().err


def test_a_registration_that_landed_elsewhere_mid_run_is_reused_not_duplicated(home, capsys):
    """Status read as unregistered, but register-start finds an App: reuse it.

    This is the concurrent-administrator case — a second admin (or the browser
    UI) completed registration between the status read and the manifest request.
    """
    api = FakeApi()
    api.start = {"status": "already_registered", "app_id": APP_ID, "app_slug": "sophos-adp"}
    reads = iter([{"registered": False, "install_ready": False, "login_enabled": False}])

    def status_now():
        return next(reads, {"registered": True, "install_ready": True, "login_enabled": True, "app_id": APP_ID})

    api.connections, api.platform = [HEALTHY_CONNECTION], dict(HEALTHY_PLATFORM)
    original = api.request

    def request(method, path, body=None, **kwargs):
        if path == cli.APP + "/status":
            api.calls.append((method, path, body))
            return status_now()
        return original(method, path, body, **kwargs)

    api.request = request
    result = cli.run(arguments("setup", "--new", "--github-org", "SOPHOS-IT", "--yes", "--json"), api, interactive=False)
    assert result["detail"]["app_id"] == APP_ID
    assert api.mutations == [cli.APP + "/register-start"], "no second App is created"
    assert "already has a GitHub App" in capsys.readouterr().err


def test_interactive_setup_resumes_a_pending_approval_instead_of_starting_over(home, monkeypatch, capsys):
    common.write_state(cli.NAME, {"gateway_url": FakeApi.base, "stage": "awaiting_github_approval", "owner": "SOPHOS-IT"})
    answers = iter(["new", "y"])
    monkeypatch.setattr(cli, "ask", lambda prompt, secret=False: next(answers))
    monkeypatch.setattr(common, "open_browser", lambda url: False)
    api = FakeApi()
    result = cli.run(arguments("setup", "--github-org", "SOPHOS-IT"), api, interactive=True)
    assert "still pending" in capsys.readouterr().err
    assert result["status"] == "pending"


def test_declining_the_owner_confirmation_creates_nothing(home, monkeypatch):
    monkeypatch.setattr(cli, "ask", lambda prompt, secret=False: "n")
    api = FakeApi()
    result = cli.run(arguments("setup", "--new", "--github-org", "SOPHOS-IT"), api, interactive=True)
    assert result["status"] == "pending" and api.mutations == []


def test_dry_run_new_app_changes_nothing(home):
    api = FakeApi()
    result = cli.run(arguments("setup", "--new", "--github-org", "SOPHOS-IT", "--dry-run", "--json"), api, interactive=False)
    assert result["detail"]["would_create"] is True
    assert api.mutations == []
    assert not (home / ".adp/state/github.json").exists()


# ---------------------------------------------------------------------------
# Existing-App import: another GitHub administrator's App
# ---------------------------------------------------------------------------


def credentials(home, **extra):
    key = home / "app.pem"
    key.write_text(PRIVATE_KEY)
    key.chmod(0o600)
    return secret_file(
        home,
        "github-app.json",
        {
            "app_id": APP_ID,
            "private_key_file": str(key),
            "client_id": "Iv1.fixture",
            "client_secret": OAUTH_SECRET,
            "webhook_secret": WEBHOOK_SECRET,
            **extra,
        },
    )


def test_existing_app_import_sends_credentials_and_reports_configured(home):
    api = FakeApi()
    result = cli.run(arguments("setup", "--existing", "--credentials-file", credentials(home), "--json"), api, interactive=False)
    assert api.mutations == [cli.APP + "/register-manual"]
    sent = api.calls[-1][2]
    assert sent["app_id"] == APP_ID and sent["private_key"].strip() == PRIVATE_KEY.strip()
    assert sent["client_secret"] == OAUTH_SECRET and sent["webhook_secret"] == WEBHOOK_SECRET
    assert result["status"] == "configured"
    assert "not proven" in result["next_action"]


def test_import_without_oauth_details_leaves_sign_in_pending(home):
    api = FakeApi()
    api.manual = {
        "registered": True,
        "app_id": APP_ID,
        "app_slug": "sophos-adp",
        "login_enabled": False,
        "warnings": ["OAuth credentials (client_id/client_secret) not provided."],
    }
    path = credentials(home, client_id="", client_secret="")
    result = cli.run(arguments("setup", "--existing", "--credentials-file", path, "--json"), api, interactive=False)
    assert result["status"] == "pending"
    assert result["detail"]["sign_in_credentials_configured"] is False
    assert "OAuth client ID and secret" in result["next_action"]


def test_import_surfaces_webhook_and_permission_warnings_from_the_server(home):
    api = FakeApi()
    api.manual = dict(api.manual, warnings=["Webhook URL points at another deployment", "Missing permission: contents"])
    result = cli.run(arguments("setup", "--existing", "--credentials-file", credentials(home), "--json"), api, interactive=False)
    assert result["detail"]["warnings"] == api.manual["warnings"]


def test_importing_a_different_app_over_a_live_one_is_refused(home):
    api = FakeApi().registered()
    with pytest.raises(common.CliError) as exc:
        cli.run(arguments("setup", "--existing", "--credentials-file", credentials(home, app_id="111111"), "--json"), api, interactive=False)
    assert "would repoint it" in str(exc.value)
    assert api.mutations == [], "a shared App's consumers must not be broken to finish setup"


def test_reimporting_the_same_app_id_is_allowed_for_recovery(home):
    api = FakeApi().registered()
    result = cli.run(arguments("setup", "--existing", "--credentials-file", credentials(home), "--json"), api, interactive=False)
    assert result["status"] == "configured"
    assert api.mutations == [cli.APP + "/register-manual"]


def test_import_reads_credentials_from_stdin_for_a_secret_manager(home, monkeypatch):
    key = home / "app.pem"
    key.write_text(PRIVATE_KEY)
    key.chmod(0o600)
    payload = json.dumps({"app_id": APP_ID, "private_key": PRIVATE_KEY, "client_id": "Iv1.fixture", "client_secret": OAUTH_SECRET})
    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"read": staticmethod(lambda size=-1: payload), "isatty": staticmethod(lambda: False)})())
    api = FakeApi()
    result = cli.run(arguments("setup", "--existing", "--credentials-stdin", "--json"), api, interactive=False)
    assert result["status"] == "configured"
    assert api.calls[-1][2]["private_key"].strip() == PRIVATE_KEY.strip()


def test_world_readable_private_key_is_refused_before_anything_is_sent(home):
    key = home / "app.pem"
    key.write_text(PRIVATE_KEY)
    key.chmod(0o644)
    api = FakeApi()
    with pytest.raises(common.CliError) as exc:
        cli.run(
            arguments(
                "setup",
                "--existing",
                "--credentials-file",
                secret_file(home, "creds.json", {"app_id": APP_ID, "private_key_file": str(key)}),
                "--json",
            ),
            api,
            interactive=False,
        )
    assert exc.value.code == "unsafe_file"
    assert api.mutations == []


def test_import_without_a_private_key_is_a_usage_error(home):
    api = FakeApi()
    with pytest.raises(common.CliError) as exc:
        cli.run(
            arguments("setup", "--existing", "--credentials-file", secret_file(home, "creds.json", {"app_id": APP_ID}), "--json"),
            api,
            interactive=False,
        )
    assert exc.value.exit_code == 1
    assert api.mutations == []


def test_non_interactive_import_requires_an_explicit_credential_source(home):
    with pytest.raises(common.CliError) as exc:
        cli.run(arguments("setup", "--existing", "--json"), FakeApi(), interactive=False)
    assert "--credentials-file" in str(exc.value)


def test_non_interactive_setup_requires_an_explicit_mode(home):
    with pytest.raises(common.CliError) as exc:
        cli.run(arguments("setup", "--json"), FakeApi(), interactive=False)
    assert "--new or --existing" in str(exc.value)


def test_dry_run_import_sends_no_credentials(home):
    api = FakeApi()
    result = cli.run(arguments("setup", "--existing", "--credentials-file", credentials(home), "--dry-run", "--json"), api, interactive=False)
    assert result["detail"]["would_import_app_id"] == APP_ID
    assert api.mutations == []


def test_a_failed_store_is_reported_as_failed_not_configured(home):
    api = FakeApi()
    api.manual = {"registered": False, "app_id": APP_ID, "warnings": []}
    with pytest.raises(common.CliError) as exc:
        cli.run(arguments("setup", "--existing", "--credentials-file", credentials(home), "--json"), api, interactive=False)
    assert exc.value.code == "registration_failed"


# ---------------------------------------------------------------------------
# Revalidation: report drift, never repair it by rotating or repointing
# ---------------------------------------------------------------------------


def test_revalidate_reports_a_clean_app_as_configured(home):
    result = cli.run(arguments("revalidate", "--json"), FakeApi().registered(), interactive=False)
    assert result["status"] == "configured" and result["detail"]["checked"] is True


def test_revalidate_drift_names_the_action_for_the_github_owner(home):
    api = FakeApi().registered()
    api.revalidation = dict(api.revalidation, app_webhook_url_matches=False, warnings=["Webhook URL points elsewhere"])
    result = cli.run(arguments("revalidate", "--json"), api, interactive=False)
    assert result["status"] == "pending"
    assert result["detail"]["webhook_url"] == "mismatch"
    assert "GitHub App owner" in result["next_action"]
    assert api.mutations == [cli.APP + "/revalidate"], "no rotate-key, no disconnect, no repoint"


def test_unreadable_live_config_is_not_reported_as_healthy(home):
    api = FakeApi().registered()
    api.revalidation = {"checked": False, "warnings": ["Could not reach GitHub"], "message": "unavailable"}
    result = cli.run(arguments("revalidate", "--json"), api, interactive=False)
    assert result["status"] == "pending" and result["detail"]["checked"] is False


# ---------------------------------------------------------------------------
# Wizard provider contract (adp admin setup)
# ---------------------------------------------------------------------------


def test_provider_declares_the_contract_the_wizard_expects(home):
    assert (cli.NAME, cli.TITLE) == ("github", "GitHub App")
    assert cli.ORDER >= 10, "the foundation reserves orders below 10"
    assert callable(cli.status) and callable(cli.configure)


def test_provider_status_is_read_only_and_never_prompts(home, monkeypatch):
    monkeypatch.setattr(cli, "ask", lambda *args, **kwargs: pytest.fail("status prompted"))
    api = FakeApi().registered()
    result = cli.status({"api": api, "org": "sophos", "interactive": True})
    assert result["status"] == "configured" and api.mutations == []


def test_provider_configure_in_a_dry_run_or_non_interactive_wizard_only_reads(home, monkeypatch):
    monkeypatch.setattr(cli, "ask", lambda *args, **kwargs: pytest.fail("configure prompted"))
    api = FakeApi()
    for context in ({"api": api, "dry_run": True, "interactive": True}, {"api": api, "interactive": False}):
        assert cli.configure(context)["status"] == "pending"
    assert api.mutations == []


def test_provider_configure_starts_the_manifest_flow_when_interactive(home, monkeypatch):
    answers = iter(["new", "SOPHOS-IT", "y"])
    monkeypatch.setattr(cli, "ask", lambda prompt, secret=False: next(answers))
    monkeypatch.setattr(common, "open_browser", lambda url: False)
    api = FakeApi()
    result = cli.configure({"api": api, "org": "sophos-adp-org", "interactive": True})
    assert result["status"] == "pending"
    assert api.calls[-1][2]["org"] == "SOPHOS-IT", "the GitHub owner is asked for, not derived from the ADP org"


def test_provider_result_always_matches_the_shared_envelope(home):
    for result in (cli.status({"api": FakeApi()}), cli.status({"api": FakeApi().registered()})):
        assert set(result) >= {"status", "command", "detail", "next_action"}
        assert result["status"] in {"configured", "verified", "pending", "failed", "unavailable"}


# ---------------------------------------------------------------------------
# Security invariants
# ---------------------------------------------------------------------------


def test_no_secret_reaches_output_state_or_process_arguments(home, monkeypatch, capsys):
    api = FakeApi()
    monkeypatch.setattr(cli, "Api", lambda: api)
    path = credentials(home)
    assert cli.main(["setup", "--existing", "--credentials-file", path, "--json"]) == 0
    captured = capsys.readouterr()
    saved = json.dumps(common.read_state(cli.NAME)) if (home / ".adp/state/github.json").exists() else ""
    for secret in (PRIVATE_KEY.strip(), OAUTH_SECRET, WEBHOOK_SECRET):
        assert secret not in captured.out and secret not in captured.err
        assert secret not in saved
    # The file path may be an argument; the credential values inside never are.
    assert OAUTH_SECRET not in " ".join(["setup", "--existing", "--credentials-file", path])


def test_saved_state_holds_progress_not_credentials(home, monkeypatch):
    monkeypatch.setattr(common, "open_browser", lambda url: False)
    cli.run(arguments("setup", "--new", "--github-org", "SOPHOS-IT", "--yes", "--json"), FakeApi(), interactive=False)
    saved = common.read_state(cli.NAME)
    assert set(saved) == {"gateway_url", "stage", "owner", "app_name"}
    assert stat.S_IMODE(common.state_path(cli.NAME).stat().st_mode) == 0o600


def test_json_output_is_exactly_one_object_on_stdout(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Api", lambda: FakeApi().registered())
    assert cli.main(["status", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["command"] == "admin github status"
    assert captured.out.count("\n") == 1


def test_usage_errors_exit_1_with_a_safe_json_error(home, capsys):
    assert cli.main(["status", "--not-a-flag", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage_error"


def test_unknown_area_command_is_a_usage_error(home, capsys):
    assert cli.main(["rotate-everything", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_help_names_both_setup_choices_and_the_two_organization_flags(home, capsys):
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["setup", "--help"])
    # argparse re-wraps help text, so compare on a single-spaced rendering.
    text = " ".join(capsys.readouterr().out.split())
    assert "--new" in text and "--existing" in text
    assert "GitHub organization that owns the App (not the ADP organization)" in text
    assert "ADP organization ID or exact name" in text
