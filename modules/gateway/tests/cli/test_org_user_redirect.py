"""`org` and `user` must redirect, not administer (Issue #5039).

The design retires Superplane's own organization/user/SSO administration: ADP
already owns those surfaces. Porting the upstream verbs as *working* commands
would have preserved a second place to invite a user, change a role or configure
SSO — two systems of record for the same authorization facts, drifting apart.

So these verbs are deliberately inert. What the tests pin:

* no request is issued — not even a read;
* the exit code is 4 (`unavailable`), never 0, so a script cannot mistake the
  redirect for a change that was applied;
* upstream invocations with their original flags still reach the redirect rather
  than dying on an argparse usage error, because a user typing the command they
  used yesterday should be told where the administration moved to.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "cli/adp-superplane.py"
spec = importlib.util.spec_from_file_location("adp_superplane_redirect", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

# The upstream command surface, verbatim from the pinned reference CLI. Each of
# these must land on the redirect.
UPSTREAM_ORG = [
    ["org", "show"],
    ["org", "update", "--name", "Acme"],
    ["org", "sso"],
    ["org", "sso-configure", "--provider", "okta", "--domain", "acme.test"],
    ["org", "sso-disable"],
]
UPSTREAM_USER = [
    ["user", "invite", "--email", "someone@acme.test", "--role", "admin"],
    ["user", "list"],
    ["user", "update-role", "user-42", "--role", "viewer"],
    ["user", "remove", "user-42"],
]


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


class ForbiddenApi:
    """Any call at all is a failure: a redirect must administer nothing."""

    def request(self, method, path, body=None, **kwargs):
        raise AssertionError(f"a redirected verb must issue no request, got {method} {path}")


def run_helper(args, home: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )


@pytest.mark.parametrize("verb", ["org", "user"])
def test_the_verb_performs_no_request(verb) -> None:
    result = cli.run(cli.parser().parse_args([verb]), ForbiddenApi())

    assert result["status"] == "unavailable"
    assert result["detail"]["performed"] == "nothing"


@pytest.mark.parametrize("verb", ["org", "user"])
def test_the_verb_names_where_the_administration_lives(verb) -> None:
    result = cli.redirect(verb)

    assert result["detail"]["redirected_to"].startswith("/settings/")
    assert "ADP" in result["next_action"]


@pytest.mark.parametrize("argv", UPSTREAM_ORG + UPSTREAM_USER, ids=lambda a: " ".join(a))
def test_an_upstream_invocation_reaches_the_redirect(argv, private_home) -> None:
    """The old command must be answered with the redirect, not a usage error."""
    result = run_helper(argv, private_home)

    assert result.returncode == 4, result.stdout + result.stderr
    assert "settings" in (result.stdout + result.stderr)


@pytest.mark.parametrize("argv", UPSTREAM_ORG + UPSTREAM_USER, ids=lambda a: " ".join(a))
def test_no_upstream_invocation_ever_exits_zero(argv, private_home) -> None:
    """Exit 0 would tell a script the administration happened."""
    assert run_helper(argv, private_home).returncode != 0


@pytest.mark.parametrize("verb", ["org", "user"])
def test_the_redirect_is_machine_readable(verb, private_home) -> None:
    result = run_helper([verb, "--json"], private_home)
    payload = json.loads(result.stdout)

    assert payload["status"] == "unavailable"
    assert payload["detail"]["performed"] == "nothing"


@pytest.mark.parametrize("verb", ["org", "user"])
def test_the_redirect_needs_no_session(verb, private_home, monkeypatch) -> None:
    """It performs nothing, so demanding a login to be told where to go is wrong."""
    result = run_helper([verb, "show"], private_home)

    assert result.returncode == 4
    assert "sign in" not in (result.stdout + result.stderr).lower()


def test_the_helper_defines_no_administration_endpoints() -> None:
    """No org/user write path may survive in the code, even unused."""
    source = SCRIPT.read_text()

    for endpoint in ("/orgs/current", "/users/invite", "/users/", "/orgs/current/sso"):
        assert endpoint not in source, f"{endpoint} would be a second administration surface"


def test_the_verb_is_discoverable_in_help(private_home) -> None:
    """A user who cannot find the verb cannot learn where it moved."""
    result = run_helper(["--help"], private_home)

    assert "org" in result.stdout
    assert "user" in result.stdout


def test_quota_addresses_a_workspace_not_the_organization() -> None:
    """Org-level quota is org administration, which this CLI does not do."""
    source = SCRIPT.read_text()

    assert "/orgs/current/quota" not in source
