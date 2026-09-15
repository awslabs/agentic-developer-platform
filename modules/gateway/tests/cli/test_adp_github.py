"""Exercise `adp github` against a fake ADP API — never a live GitHub App or real credentials.

Issue #5184. The behaviours under test are the ones an ordinary user depends on:
reuse an installation they already authorized, refuse to call a stored snapshot
proof of access, refuse a DIFFERENT repository than the one requested, stay
completable without a browser, and never ask for platform app secrets.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "cli/adp-github.py"
spec = importlib.util.spec_from_file_location("adp_github_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

REPO = "SOPHOS-IT/project"
INSTALL_URL = "https://github.com/apps/adp-agent-platform/installations/new?state=nonce-fixture"


def connection(
    *,
    installation_id: int = 4242,
    account_login: str = "SOPHOS-IT",
    repositories: list[str] | None = None,
    repositories_live: bool | None = True,
    tenant_id: str | None = "tenant-sophos",
    tenant_name: str | None = "SOPHOS-IT",
    verification: dict | None = None,
) -> dict:
    """One entry shaped like GET /admin/connections returns it."""
    checks = {
        "record_present": True,
        "tenant_secret_seeded": True,
        "identity_index_row": True,
        "reverse_identity_row": True,
        "repositories_live": repositories_live,
    }
    checks.update(verification or {})
    return {
        "provider": "github",
        "installation_id": installation_id,
        "account_login": account_login,
        "account_type": "Organization",
        "repository_selection": "selected",
        "repository_count": len(repositories or []),
        "repositories": ["SOPHOS-IT/project"] if repositories is None else repositories,
        "configure_url": f"https://github.com/settings/installations/{installation_id}",
        "manage_url": f"https://github.com/organizations/{account_login}/settings/installations/{installation_id}",
        "can_manage": True,
        "tenant_id": tenant_id,
        "tenant_name": tenant_name,
        "is_active_tenant": True,
        "verification": checks,
    }


class FakeApi:
    """Records every call, so a test can assert what was NOT sent as well."""

    base = "https://gateway.example.test/api"

    def __init__(self, connections=None, install_start=None):
        self.calls: list[tuple[str, str, object]] = []
        self.connections = connections if connections is not None else []
        self.install_start = install_start if install_start is not None else {"install_url": INSTALL_URL, "state_token": "nonce-fixture"}

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        if path == cli.CONNECTIONS:
            return {"connections": self.connections}
        if path == cli.INSTALL_START:
            if isinstance(self.install_start, Exception):
                raise self.install_start
            return self.install_start
        raise AssertionError(f"unexpected call: {method} {path}")

    @property
    def paths(self):
        return [path for _, path, _ in self.calls]


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    """Redirect ~/.adp/state so no test touches the developer's own state."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def never_open_a_browser(monkeypatch):
    """A test suite must never launch a real browser; record intent instead."""
    opened: list[str] = []
    monkeypatch.setattr(cli.common, "open_browser", lambda url: opened.append(url) or True)
    return opened


def run(argv, api):
    return cli.run(cli.parser().parse_args(argv), api)


# ---------------------------------------------------------------------------
# --repo parsing: a locally rejectable shape, rejected locally
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "project", "owner/", "/name", "owner/name/extra", "owner name", "../etc/passwd", "owner/na me"])
def test_repo_must_be_owner_slash_name(value):
    with pytest.raises(cli.CliError) as caught:
        cli.parse_repo(value)
    assert caught.value.exit_code == 1


def test_repo_accepts_dots_and_dashes():
    assert cli.parse_repo("SOPHOS-IT/my.project_v2") == ("SOPHOS-IT", "my.project_v2")


# ---------------------------------------------------------------------------
# connect: first install
# ---------------------------------------------------------------------------


def test_connect_with_no_existing_installation_starts_one_and_reports_pending(never_open_a_browser):
    api = FakeApi()
    result = run(["connect", "--repo", REPO], api)

    assert result["status"] == "pending"
    assert result["detail"]["install_url"] == INSTALL_URL
    assert result["detail"]["requested_repository"] == REPO
    # Exit 4 — waiting on a human, not broken.
    assert cli.common.emit(result) == 4
    assert never_open_a_browser == [INSTALL_URL]
    assert cli.INSTALL_START in api.paths


def test_connect_never_sends_the_repository_to_install_start():
    """install-start takes no repository, and the CLI must not invent a body for it.

    Repository selection happens on GitHub's screen. Sending a repo here would
    imply the server could honour it, which it cannot.
    """
    api = FakeApi()
    run(["connect", "--repo", REPO], api)

    (start,) = [(method, path, body) for method, path, body in api.calls if path == cli.INSTALL_START]
    assert start[2] == {}


def test_connect_saves_a_resumable_request_without_credentials():
    api = FakeApi()
    run(["connect", "--repo", REPO], api)

    saved = cli.common.read_state(cli.NAME)
    assert saved["requested_repository"] == REPO
    assert saved["gateway_url"] == api.base
    blob = json.dumps(saved).casefold()
    for forbidden in ("token", "secret", "private_key", "password", "client_secret"):
        assert forbidden not in blob


def test_connect_state_file_is_private():
    api = FakeApi()
    run(["connect", "--repo", REPO], api)
    assert cli.common.state_path(cli.NAME).stat().st_mode & 0o077 == 0


# ---------------------------------------------------------------------------
# connect: browserless / non-interactive
# ---------------------------------------------------------------------------


def test_connect_no_browser_prints_the_url_and_does_not_open_one(capsys, never_open_a_browser):
    api = FakeApi()
    result = run(["connect", "--repo", REPO, "--no-browser"], api)

    assert never_open_a_browser == []
    assert INSTALL_URL in capsys.readouterr().err
    assert result["status"] == "pending"
    assert INSTALL_URL in result["next_action"]


def test_connect_dry_run_changes_nothing():
    api = FakeApi()
    result = run(["connect", "--repo", REPO, "--dry-run"], api)

    assert result["status"] == "pending"
    assert cli.INSTALL_START not in api.paths
    assert cli.common.read_state(cli.NAME) == {}


# ---------------------------------------------------------------------------
# connect: reuse, no duplicates
# ---------------------------------------------------------------------------


def test_connect_reuses_an_installation_that_already_grants_the_repository():
    api = FakeApi(connections=[connection()])
    result = run(["connect", "--repo", REPO], api)

    assert result["status"] == "verified"
    assert result["detail"]["reused_existing_installation"] is True
    assert result["detail"]["repository_access"] == "verified"
    # No second install was started.
    assert cli.INSTALL_START not in api.paths
    assert cli.common.emit(result) == 0


def test_reuse_matches_repository_case_insensitively():
    api = FakeApi(connections=[connection(repositories=["sophos-it/PROJECT"])])
    result = run(["connect", "--repo", REPO], api)

    assert result["status"] == "verified"
    assert cli.INSTALL_START not in api.paths


def test_connect_clears_a_stale_pending_request_once_the_repository_is_connected():
    api = FakeApi()
    run(["connect", "--repo", REPO], api)
    assert cli.common.read_state(cli.NAME)["requested_repository"] == REPO

    api.connections = [connection()]
    result = run(["connect", "--repo", REPO], api)

    assert result["status"] == "verified"
    assert cli.common.read_state(cli.NAME) == {}


# ---------------------------------------------------------------------------
# The core honesty property: a snapshot is not proof
# ---------------------------------------------------------------------------


def test_a_snapshot_backed_repository_list_is_configured_not_verified():
    """GitHub was unreachable, so the list is last-known configuration.

    Reporting `verified` here would be claiming current access from stored
    metadata — exactly what this story forbids.
    """
    api = FakeApi(connections=[connection(repositories_live=False)])
    result = run(["connect", "--repo", REPO], api)

    assert result["status"] == "configured"
    assert result["detail"]["repository_access"] == "unproven"
    assert result["detail"]["repositories_verified_live"] is False
    assert "could not confirm" in result["next_action"]
    # Not a failure, but not success either.
    assert cli.common.emit(result) == 0


def test_unknown_provenance_is_also_unproven():
    api = FakeApi(connections=[connection(repositories_live=None)])
    result = run(["status", "--repo", REPO], api)

    assert result["status"] == "configured"
    assert result["detail"]["repository_access"] == "unproven"


def test_verified_requires_a_live_read():
    api = FakeApi(connections=[connection(repositories_live=True)])
    assert run(["status", "--repo", REPO], api)["status"] == "verified"


# ---------------------------------------------------------------------------
# Wrong repository: a different install cannot satisfy the request
# ---------------------------------------------------------------------------


def test_a_different_repository_on_the_same_owner_does_not_satisfy_the_request():
    api = FakeApi(connections=[connection(repositories=["SOPHOS-IT/other-project"])])
    result = run(["connect", "--repo", REPO], api)

    assert result["status"] == "pending"
    assert result["detail"]["repository_access"] == "not_granted"
    assert "SOPHOS-IT/project" in result["next_action"]
    # Sent to repository management for the install they already have, not to a
    # second installation.
    assert cli.INSTALL_START not in api.paths
    assert cli.common.emit(result) == 4


def test_an_installation_on_a_different_owner_does_not_satisfy_the_request():
    api = FakeApi(connections=[connection(account_login="OTHER-ORG", repositories=["OTHER-ORG/project"])])
    result = run(["connect", "--repo", REPO], api)

    assert result["status"] == "pending"
    # A genuinely new install is needed for this owner.
    assert cli.INSTALL_START in api.paths
    assert result["detail"]["install_url"] == INSTALL_URL


def test_status_reports_a_missing_repository_on_a_connected_owner_precisely():
    api = FakeApi(connections=[connection(repositories=["SOPHOS-IT/other-project"])])
    result = run(["status", "--repo", REPO], api)

    assert result["status"] == "pending"
    assert "not among its authorized repositories" in result["next_action"]
    assert result["detail"]["repository_access"] == "not_granted"


# ---------------------------------------------------------------------------
# Pending owner approval, and resuming it
# ---------------------------------------------------------------------------


def test_pending_owner_approval_is_reported_with_a_specific_next_step():
    api = FakeApi()
    result = run(["connect", "--repo", REPO], api)

    assert result["status"] == "pending"
    assert "approval" in result["next_action"]
    assert "adp github connect --repo SOPHOS-IT/project" in result["next_action"]


def test_status_resumes_a_saved_request_after_approval_is_still_pending():
    api = FakeApi()
    run(["connect", "--repo", REPO], api)

    result = run(["status", "--repo", REPO], api)
    assert result["status"] == "pending"
    assert INSTALL_URL in result["next_action"]


def test_rerunning_connect_while_pending_does_not_request_app_secrets():
    api = FakeApi()
    run(["connect", "--repo", REPO], api)
    run(["connect", "--repo", REPO], api)

    bodies = [body for _, path, body in api.calls if path == cli.INSTALL_START]
    assert bodies == [{}, {}]
    for body in bodies:
        assert body == {}


def test_a_saved_request_survives_a_transient_install_start_failure():
    api = FakeApi()
    run(["connect", "--repo", REPO], api)

    api.install_start = cli.CliError("ADP could not be reached", "gateway_unavailable")
    result = run(["connect", "--repo", REPO], api)

    assert result["status"] == "pending"
    assert INSTALL_URL in result["next_action"]


def test_a_transient_failure_with_no_saved_request_is_reported_as_a_failure():
    api = FakeApi(install_start=cli.CliError("ADP could not be reached", "gateway_unavailable"))
    with pytest.raises(cli.CliError):
        run(["connect", "--repo", REPO], api)


# ---------------------------------------------------------------------------
# Missing platform GitHub App: explain the dependency, do not act as admin
# ---------------------------------------------------------------------------


def test_missing_platform_app_is_unavailable_and_names_the_administrator_step():
    api = FakeApi(install_start=cli.CliError("ADP returned HTTP 503 (http_error). Check status before retrying.", "http_error"))
    result = run(["connect", "--repo", REPO], api)

    assert result["status"] == "unavailable"
    assert result["detail"]["platform_github_app"] == "not_configured"
    assert "administrator" in result["next_action"]
    # Exit 4: a pending external action, not a broken command.
    assert cli.common.emit(result) == 4


def test_missing_platform_app_never_asks_the_user_for_app_credentials():
    api = FakeApi(install_start=cli.CliError("ADP returned HTTP 503 (http_error).", "http_error"))
    result = run(["connect", "--repo", REPO], api)

    blob = json.dumps(result).casefold()
    for forbidden in ("private key", "private_key", "client secret", "client_secret", "webhook secret", "app id"):
        assert forbidden not in blob


# ---------------------------------------------------------------------------
# Signed in / repository access / agent integration are three different things
# ---------------------------------------------------------------------------


def test_status_distinguishes_sign_in_from_repository_access():
    api = FakeApi(connections=[])
    result = run(["status"], api)

    # The authenticated read succeeded, so the session is proven...
    assert result["detail"]["signed_in"] is True
    # ...but nothing is connected, which is a separate fact.
    assert result["status"] == "pending"
    assert result["detail"]["connection_count"] == 0


def test_agent_integration_never_claims_a_run_passed():
    api = FakeApi(connections=[connection()])
    result = run(["status", "--repo", REPO], api)

    integration = result["detail"]["agent_integration"]
    assert integration["state"] == "configured"
    assert integration["agent_run_observed"] is False
    assert "does not prove" in integration["note"]


def test_agent_integration_reports_incomplete_when_a_check_failed():
    api = FakeApi(connections=[connection(verification={"tenant_secret_seeded": False})])
    result = run(["status", "--repo", REPO], api)

    integration = result["detail"]["agent_integration"]
    assert integration["state"] == "incomplete"
    assert integration["checks"]["tenant_credentials_seeded"] is False


def test_agent_integration_is_unknown_rather_than_failed_when_a_check_errored():
    """A check that could not be determined is not a check that failed."""
    api = FakeApi(connections=[connection(verification={"identity_index_row": None})])
    result = run(["status", "--repo", REPO], api)

    assert result["detail"]["agent_integration"]["state"] == "unknown"


# ---------------------------------------------------------------------------
# Tenant scoping: --org and --repo cannot widen access
# ---------------------------------------------------------------------------


def test_org_filter_only_narrows_the_servers_own_list():
    api = FakeApi(connections=[connection(), connection(installation_id=99, account_login="OTHER", tenant_id="tenant-other", tenant_name="OTHER")])
    result = run(["status", "--org", "tenant-other"], api)

    assert result["detail"]["connection_count"] == 1
    assert result["detail"]["connections"][0]["installation_id"] == 99


def test_org_filter_cannot_invent_a_connection_the_server_did_not_return():
    api = FakeApi(connections=[connection()])
    result = run(["status", "--org", "tenant-someone-else"], api)

    assert result["detail"]["connection_count"] == 0
    assert result["status"] == "pending"


def test_no_request_carries_a_tenant_argument():
    """Tenant scoping is the server's decision; the CLI must not parameterize it."""
    api = FakeApi(connections=[connection()])
    run(["status", "--org", "tenant-sophos"], api)
    run(["connect", "--repo", REPO, "--org", "tenant-sophos"], api)

    for _, path, body in api.calls:
        assert "tenant" not in path
        assert body in (None, {})


def test_connect_for_an_unauthorized_repository_reports_pending_not_success():
    """A repo the caller cannot see is indistinguishable from an unconnected one.

    Either way the honest answer is "not connected", never access.
    """
    api = FakeApi(connections=[connection()])
    result = run(["connect", "--repo", "someone-else/private-repo"], api)

    assert result["status"] == "pending"
    assert result["detail"]["requested_repository"] == "someone-else/private-repo"


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


def test_json_output_is_exactly_one_object_on_stdout(capsys):
    api = FakeApi(connections=[connection()])
    result = run(["status", "--repo", REPO], api)
    cli.common.emit(result, as_json=True)

    out, _ = capsys.readouterr()
    parsed = json.loads(out)
    assert parsed["command"] == "github status"
    assert parsed["status"] == "verified"
    assert set(parsed) == {"status", "command", "detail", "next_action"}


def test_status_detail_carries_stable_ids_and_no_secrets():
    api = FakeApi(connections=[connection()])
    result = run(["status", "--repo", REPO], api)

    assert result["detail"]["installation_id"] == 4242
    assert result["detail"]["tenant_id"] == "tenant-sophos"
    blob = json.dumps(result).casefold()
    for forbidden in ("secret", "private_key", "password", "authorization", "bearer"):
        assert forbidden not in blob


def test_unknown_flag_is_a_usage_error():
    with pytest.raises(cli.CliError) as caught:
        cli.parser().parse_args(["connect", "--repo", REPO, "--not-a-flag"])
    assert caught.value.exit_code == 1


def test_connect_requires_a_repository():
    with pytest.raises(cli.CliError) as caught:
        cli.parser().parse_args(["connect"])
    assert caught.value.exit_code == 1


def test_status_without_a_repository_lists_every_connection():
    api = FakeApi(connections=[connection(), connection(installation_id=77, account_login="OTHER", repositories=["OTHER/thing"])])
    result = run(["status"], api)

    assert result["status"] == "configured"
    assert [row["installation_id"] for row in result["detail"]["connections"]] == [4242, 77]


# ---------------------------------------------------------------------------
# main(): exit codes a script can branch on
# ---------------------------------------------------------------------------


def test_main_maps_authentication_failure_to_exit_2(monkeypatch, capsys):
    def unauthenticated():
        raise cli.CliError("Sign in with adp login.", "authentication_required", 2)

    monkeypatch.setattr(cli, "Api", unauthenticated)
    assert cli.main(["status", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "authentication_required"


def test_main_maps_authorization_failure_to_exit_3(monkeypatch):
    def forbidden():
        raise cli.CliError("Not authorized.", "forbidden", 3)

    monkeypatch.setattr(cli, "Api", forbidden)
    assert cli.main(["status"]) == 3


def test_main_reports_a_usage_error_as_exit_1(monkeypatch):
    monkeypatch.setattr(cli, "Api", lambda: FakeApi())
    assert cli.main(["connect", "--repo", "not-a-repo"]) == 1


def test_a_malformed_repo_is_a_usage_error_even_with_no_gateway_configured(monkeypatch, capsys):
    """A typo in --repo must not be reported as a gateway problem.

    Found by running the real dispatcher on a machine with no config: building
    the transport before validating the argument made `--repo not-a-repo` fail
    with "No valid gateway configured" and exit 2, sending the user off to
    reinstall the CLI over a fixable typo. The caller's own argument is checked
    first, so the message names the real mistake.
    """

    def no_gateway():
        raise cli.CliError("No valid gateway configured.", "configuration_error", 2)

    monkeypatch.setattr(cli, "Api", no_gateway)

    assert cli.main(["connect", "--repo", "not-a-repo", "--json"]) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "usage_error"
    assert "owner/name" in error["message"]


def test_main_returns_4_while_an_approval_is_pending(monkeypatch):
    monkeypatch.setattr(cli, "Api", lambda: FakeApi())
    assert cli.main(["connect", "--repo", REPO, "--no-browser"]) == 4


def test_main_returns_0_when_access_is_verified(monkeypatch):
    monkeypatch.setattr(cli, "Api", lambda: FakeApi(connections=[connection()]))
    assert cli.main(["status", "--repo", REPO]) == 0
