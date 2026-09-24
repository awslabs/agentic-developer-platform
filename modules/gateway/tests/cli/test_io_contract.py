"""The I/O and secret-handling contract of `adp superplane` (Issue #5039).

Four properties a script or another agent depends on:

**JSON on stdout, everything else on stderr.** A single interleaved stream cannot
be parsed. These tests capture the two streams SEPARATELY and parse stdout whole
— an assertion on combined output would pass even if progress text were mixed in.

**`--api-key VALUE` is refused.** A secret in argv is in the shell's history file
and was visible in `ps` to every other user on the machine. By the time a warning
could print, the leak has happened, so the flag is rejected outright.

**Stable exit codes.** From the shared CLI contract: 0 ok, 1 usage, 2 auth,
4 pending/unavailable, 5 failed, 130 interrupted.

**Cancellation honesty.** Ctrl-C stops the local process only. It does not cancel
work the domain API already accepted and does not release a provider resource
that may still be billing, and the message must say so rather than implying a
clean stop.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "cli/adp-superplane.py"
spec = importlib.util.spec_from_file_location("adp_superplane_io", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

SECRET = "super-secret-provider-key"


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def run_helper(args, home: Path, stdin: str | None = None):
    """Run the helper as a real process, so the two streams are genuinely separate."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        input=stdin,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )


# --- secrets must never come from argv ---------------------------------------


@pytest.mark.parametrize("flag", ["--api-key", "--token", "--secret", "--oauth-token", "--password"])
def test_a_secret_flag_is_rejected(flag, private_home) -> None:
    result = run_helper(["provider", "add", "--name", "n", "--provider", "nebius", flag, SECRET], private_home)

    assert result.returncode == 1
    assert SECRET not in result.stdout, "the rejection must not echo the value back"


@pytest.mark.parametrize("form", ["--api-key=" + SECRET, "--api-key"])
def test_both_spellings_are_rejected(form, private_home) -> None:
    """`--api-key=V` and `--api-key V` must both be refused."""
    result = run_helper(["provider", "add", "--name", "n", "--provider", "nebius", form], private_home)

    assert result.returncode == 1
    assert "history" in result.stderr.lower() or "history" in result.stdout.lower()


def test_the_rejection_names_the_safe_alternatives(private_home) -> None:
    result = run_helper(["provider", "add", "--name", "n", "--provider", "nebius", "--api-key", SECRET], private_home)
    combined = result.stdout + result.stderr

    assert "--stdin" in combined
    assert "prompted" in combined.lower()


def test_the_rejection_happens_before_any_network_call(monkeypatch) -> None:
    """Refused at parse time, so no gateway or token is resolved first."""

    def explode():
        raise AssertionError("no token should be fetched for a rejected argument")

    monkeypatch.setattr(cli.common, "access_token", explode)
    with pytest.raises(cli.CliError) as raised:
        cli.reject_secret_arguments(["provider", "add", "--api-key", SECRET])

    assert raised.value.code == "secret_in_argv"
    assert raised.value.exit_code == 1


def test_a_provider_value_is_never_placed_in_a_request_query(private_home, monkeypatch) -> None:
    """The value goes in the idempotent PUT body, never into a query string."""
    monkeypatch.setattr(cli, "read_provider_value", lambda from_stdin, prompt: SECRET)
    sent: list[tuple[str, str, object]] = []

    class RecordingApi:
        base = "https://gateway.example.test/api"

        def request(self, method, path, body=None, **kwargs):
            sent.append((method, path, body))
            if method == "PUT":
                return {
                    "id": path.rsplit("/", 1)[-1],
                    "service": body["service"],
                    "label": body["label"],
                    "credential_type": body["credential_type"],
                    "scope": "user",
                }
            return {
                "id": "11111111-2222-4333-8444-555555555555",
                "adp_credential_id": credential_id,
                "name": "n",
                "provider": "nebius",
                "credential_type": "api_key",
            }

    credential_id = "66666666-7777-4888-8999-aaaaaaaaaaaa"
    claims = {"sub": "user", "custom:org_id": "org"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    monkeypatch.setattr(cli.common, "access_token", lambda: f"header.{payload}.signature")
    monkeypatch.setattr(cli.common, "deployment_stamp", lambda: {"deployment_id": "test", "deployment": "test"})
    monkeypatch.setattr(cli.common, "gateway_url", lambda: "https://gateway.example.test/api")
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: uuid.UUID(credential_id))
    cli.run(cli.parser().parse_args(["provider", "add", "--name", "n", "--provider", "nebius", "--yes"]), RecordingApi())

    assert all(SECRET not in path for _, path, _ in sent)
    vault_calls = [body for method, path, body in sent if method == "PUT" and path == f"{cli.VAULT_CREDENTIALS}/{credential_id}"]
    assert vault_calls and vault_calls[0]["value"] == SECRET
    # Only the returned id crosses into domain metadata, under the field name the
    # domain actually declares. `credential_id` was this helper's own invention and
    # is not a field of RegisterCredentialRequest, so the registration it named was
    # a 422 (#5637); the field is `adp_credential_id`.
    domain_calls = [body for _, path, body in sent if path.startswith(cli.API_BASE)]
    assert domain_calls and domain_calls[0]["adp_credential_id"] == credential_id
    assert all(SECRET not in json.dumps(body) for body in domain_calls)


# --- stream separation -------------------------------------------------------


def test_json_output_is_parseable_on_stdout_alone(private_home) -> None:
    """The smoke-check shape: stdout must parse whole, with progress on stderr."""
    result = run_helper(["workspace", "use", "ws-1", "--json"], private_home)

    assert result.returncode == 0
    assert isinstance(json.loads(result.stdout), dict)


def test_progress_and_warnings_go_to_stderr(capsys) -> None:
    cli.progress("creating something")
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "creating something" in captured.err


def test_an_error_leaves_stdout_parseable_in_json_mode(private_home) -> None:
    """Even a failure must not put prose on stdout when --json was asked for."""
    result = run_helper(["workspace", "describe", "--json"], private_home)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "workspace_not_selected"


# --- exit codes --------------------------------------------------------------


def test_success_exits_zero(private_home) -> None:
    assert run_helper(["workspace", "use", "ws-1"], private_home).returncode == 0


def test_a_usage_error_exits_one(private_home) -> None:
    assert run_helper(["workspace", "create"], private_home).returncode == 1


def test_a_redirected_verb_exits_four_not_zero(private_home) -> None:
    """`unavailable`, so a script cannot read the redirect as work performed."""
    assert run_helper(["org", "update", "--name", "x"], private_home).returncode == 4


def test_an_unreachable_gateway_does_not_look_like_success(private_home) -> None:
    result = run_helper(["workspace", "list"], private_home)

    assert result.returncode != 0


# --- cancellation honesty ----------------------------------------------------


def test_cancelling_states_that_nothing_remote_was_cancelled() -> None:
    message = cli.CANCELLED.lower()

    assert "did not cancel" in message
    assert "did not release" in message
    assert "provider" in message


def test_an_interrupt_reports_the_cancellation_caveat(monkeypatch, capsys) -> None:
    """Ctrl-C must produce the honest message and 130, not a bare traceback."""

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "parser", interrupt)
    code = cli.main(["workspace", "list"])
    captured = capsys.readouterr()

    assert code == 130
    assert "did not cancel" in (captured.out + captured.err).lower()


def test_the_cancellation_message_points_at_how_to_check() -> None:
    """An honest warning is only actionable if it says where to look."""
    assert "events" in cli.CANCELLED
