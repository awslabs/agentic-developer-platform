"""The status/control subcommands of adp-trigger (#5028 AC1, AC2, AC5, AC7).

Two things are being proven here, and the first matters more than it looks:

1. **The dispatch form still works.** ``TestBackwardCompatibility`` re-asserts
   the original invocation shape. A CLI that gained subcommands by requiring one
   would break every existing caller and every prompt that says
   ``adp-trigger --persona``, and that breakage would show up as agents silently
   failing to summon each other rather than as a test failure.

2. **The new paths present a credential, not an environment variable.** The
   identity assertions in ``TestIdentityIsTheCredential`` are the CLI half of
   AC2: ``ADP_MESSAGE_ID`` is writable by the worker, so a request that carries it
   as identity is a request the worker can forge.

``tests/test_adp_trigger.py`` covers the original dispatch path in detail and is
left alone.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adp_trigger import client as trigger_client  # noqa: E402

TRIGGER_URL = "https://api123.execute-api.us-east-1.amazonaws.com/dev/agent/trigger"
CONTROL_BASE = "https://api123.execute-api.us-east-1.amazonaws.com/dev/agent"

# A syntactically plausible credential. Structure only — this is not a real
# token and would fail its MAC check against any real key, which is precisely why
# it is safe to commit.
FAKE_CREDENTIAL = "adpr1.eyJ2IjoiYWRwcjEifQ.bm90LWEtcmVhbC1tYWM"


def header(req, name: str) -> str | None:
    """Case-insensitive header lookup on a ``urllib`` Request.

    ``Request.add_header`` stores names through ``str.capitalize()``, so a
    multi-word name like ``X-Adp-Run-Credential`` is stored as
    ``X-adp-run-credential`` and ``get_header("X-Adp-Run-Credential")`` returns
    None even though the header is present and will be transmitted. Looking it up
    exactly is a test artifact that reads as a missing header; this helper matches
    the way HTTP itself treats names (RFC 7230: case-insensitive), which is also
    how both API Gateway and Starlette read them at the far end.
    """
    target = name.lower()
    for key, value in req.header_items():
        if key.lower() == target:
            return value
    return None


def test_bind_wave_uses_authenticated_route_without_identity_claims(sent, monkeypatch, capsys):
    from adp_trigger.__main__ import main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adp-trigger",
            "bind-wave",
            "--epic",
            "epic-3959",
            "--wave",
            "wave-1",
            "--orchestrator",
            "44",
            "--evaluation",
            "45",
        ],
    )
    main()
    assert len(sent) == 1
    assert sent[0].full_url.endswith("/agent/waves")
    assert header(sent[0], trigger_client.CREDENTIAL_HEADER) == FAKE_CREDENTIAL
    assert json.loads(sent[0].data) == {
        "repo": "aws-e/adp",
        "epic_ref": "epic-3959",
        "wave_ref": "wave-1",
        "orchestrator_issue": 44,
        "evaluation_issue": 45,
    }


@pytest.mark.parametrize("number", ["0", "-1", "invalid", "45"])
def test_bind_wave_invalid_issue_is_local_usage_error(number, sent, monkeypatch):
    from adp_trigger.__main__ import main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adp-trigger",
            "bind-wave",
            "--epic",
            "epic-3959",
            "--wave",
            "wave-1",
            "--orchestrator",
            number,
            "--evaluation",
            "45",
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert sent == []


@pytest.fixture(autouse=True)
def _pod_env(monkeypatch):
    monkeypatch.setenv("ADP_CORRELATION_ID", "corr-abc-123")
    monkeypatch.setenv("ADP_MESSAGE_ID", "msg-xyz-789")
    monkeypatch.setenv("ADP_CHAIN_DEPTH", "1")
    monkeypatch.setenv("ADP_TRIGGER_ENDPOINT", TRIGGER_URL)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("GITHUB_REPOSITORY", "aws-e/adp")
    monkeypatch.setenv(trigger_client.CREDENTIAL_ENV, FAKE_CREDENTIAL)
    monkeypatch.delenv(trigger_client.CREDENTIAL_FILE_ENV, raising=False)
    monkeypatch.delenv(trigger_client.CONTROL_ENDPOINT_ENV, raising=False)


@pytest.fixture
def sent(monkeypatch):
    """Capture the request the CLI would send, without a network.

    Returns a list that receives the ``urllib`` Request object. Signing runs for
    real against dummy credentials, so the SigV4 assertions below are about the
    actual signed request rather than about a mock's arguments.
    """
    frozen = MagicMock()
    frozen.access_key = "AKIAIOSFODNN7EXAMPLE"
    frozen.secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    frozen.token = None
    creds = MagicMock()
    creds.get_frozen_credentials.return_value = frozen

    requests = []

    def fake_urlopen(req, timeout=None):
        requests.append(req)
        resp = MagicMock()
        resp.read.return_value = json.dumps({"run_id": "run-7", "state": "running"}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with (
        patch("botocore.session.get_session") as session,
        patch("adp_trigger.client.urlopen", fake_urlopen),
    ):
        session.return_value.get_credentials.return_value = creds
        yield requests


# ---------------------------------------------------------------------------
# The endpoint the credential is sent to
# ---------------------------------------------------------------------------


class TestControlBaseUrl:
    def test_explicit_configuration_wins(self, monkeypatch):
        monkeypatch.setenv(
            trigger_client.CONTROL_ENDPOINT_ENV, "https://control.example.com/agent/"
        )
        assert trigger_client.control_base_url() == "https://control.example.com/agent"

    def test_derived_from_the_trigger_endpoint_when_unset(self):
        """So the CLI works in an environment that has not added a second variable."""
        assert trigger_client.control_base_url() == CONTROL_BASE

    def test_derivation_never_rewrites_the_host(self):
        """A suffix removal cannot redirect the credential somewhere else."""
        assert trigger_client.control_base_url().startswith(
            "https://api123.execute-api.us-east-1.amazonaws.com/"
        )

    def test_an_unrecognized_trigger_endpoint_is_refused_not_appended_to(self, monkeypatch):
        """Appending to a base we do not recognize is how a credential leaks."""
        monkeypatch.setenv("ADP_TRIGGER_ENDPOINT", "https://elsewhere.example.com/hook")
        with pytest.raises(SystemExit) as exc:
            trigger_client.control_base_url()
        assert exc.value.code == 2

    def test_no_endpoint_at_all_exits_2(self, monkeypatch):
        monkeypatch.delenv("ADP_TRIGGER_ENDPOINT")
        with pytest.raises(SystemExit) as exc:
            trigger_client.control_base_url()
        assert exc.value.code == 2


class TestCredentialIsRequired:
    def test_status_without_a_credential_exits_2(self, monkeypatch):
        monkeypatch.delenv(trigger_client.CREDENTIAL_ENV)
        with pytest.raises(SystemExit) as exc:
            trigger_client.send_status("run-7")
        assert exc.value.code == 2

    def test_control_without_a_credential_exits_2(self, monkeypatch):
        monkeypatch.delenv(trigger_client.CREDENTIAL_ENV)
        with pytest.raises(SystemExit) as exc:
            trigger_client.send_control("run-7", "pause", {"command_id": "c1"})
        assert exc.value.code == 2

    def test_the_error_explains_that_dispatch_is_unaffected(self, monkeypatch, capsys):
        """A missing credential must not read as "adp-trigger is broken"."""
        monkeypatch.delenv(trigger_client.CREDENTIAL_ENV)
        with pytest.raises(SystemExit):
            trigger_client.send_status("run-7")
        err = capsys.readouterr().err
        assert "--persona dispatch is unaffected" in err

    def test_dispatch_still_works_with_no_credential_provisioned(self, monkeypatch, sent):
        """The whole reason the credential check is not in get_config."""
        monkeypatch.delenv(trigger_client.CREDENTIAL_ENV)
        trigger_client.send_trigger(
            trigger_client.build_body(persona="reviewer", issue=42, repo="aws-e/adp")
        )
        assert sent[0].full_url == TRIGGER_URL


class TestCredentialFile:
    def test_atomic_replacement_is_used_by_same_process(self, monkeypatch, tmp_path, sent):
        credential_file = tmp_path / "credential"
        credential_file.write_text(FAKE_CREDENTIAL + "\n")
        monkeypatch.setenv(trigger_client.CREDENTIAL_FILE_ENV, str(credential_file))
        monkeypatch.setenv(trigger_client.CREDENTIAL_ENV, "stale-environment-token")
        trigger_client.send_status("run-7")

        replacement = tmp_path / "replacement"
        renewed = FAKE_CREDENTIAL + "-renewed"
        replacement.write_text(renewed + "\r\n")
        replacement.replace(credential_file)
        trigger_client.send_control("run-7", "pause", {"command_id": "cmd-1"})

        assert [header(req, trigger_client.CREDENTIAL_HEADER) for req in sent] == [
            FAKE_CREDENTIAL,
            renewed,
        ]
        assert json.loads(sent[1].data) == {"command_id": "cmd-1"}

    @pytest.mark.parametrize(
        "contents",
        [b"", b"\n", b"x" * 4097, b"secret\nInjected: header", b"secret\x00", b"\xff"],
    )
    def test_invalid_file_never_falls_back_or_sends(
        self, contents, monkeypatch, tmp_path, sent, capsys
    ):
        credential_file = tmp_path / "credential"
        credential_file.write_bytes(contents)
        monkeypatch.setenv(trigger_client.CREDENTIAL_FILE_ENV, str(credential_file))
        with pytest.raises(SystemExit) as exc:
            trigger_client.send_status("run-7")
        assert exc.value.code == 2
        assert not sent
        err = capsys.readouterr().err
        assert "ADP_RUN_CREDENTIAL_FILE" in err
        assert "secret" not in err
        assert FAKE_CREDENTIAL not in err
        assert str(credential_file) not in err

    @pytest.mark.parametrize("kind", ["missing", "empty-path", "directory", "fifo"])
    def test_unreadable_file_fails_closed(self, kind, monkeypatch, tmp_path, sent):
        credential_file = tmp_path / "credential"
        if kind == "directory":
            credential_file.mkdir()
        elif kind == "fifo":
            os.mkfifo(credential_file)
        monkeypatch.setenv(
            trigger_client.CREDENTIAL_FILE_ENV,
            "" if kind == "empty-path" else str(credential_file),
        )
        with pytest.raises(SystemExit) as exc:
            trigger_client.send_status("run-7")
        assert exc.value.code == 2
        assert not sent

    def test_timeout_reconciliation_reads_replacement_credential(self, monkeypatch, tmp_path, sent):
        credential_file = tmp_path / "credential"
        credential_file.write_text(FAKE_CREDENTIAL)
        monkeypatch.setenv(trigger_client.CREDENTIAL_FILE_ENV, str(credential_file))
        original_urlopen = trigger_client.urlopen
        renewed = FAKE_CREDENTIAL + "-renewed"

        def expire_during_control(req, timeout=None):
            response = original_urlopen(req, timeout=timeout)
            if req.get_method() == "POST":
                replacement = tmp_path / "replacement"
                replacement.write_text(renewed)
                replacement.replace(credential_file)
                raise TimeoutError()
            return response

        monkeypatch.setattr(trigger_client, "urlopen", expire_during_control)
        with pytest.raises(trigger_client.ControlOutcomeUnknown) as exc:
            trigger_client.send_control("run-7", "pause", {"command_id": "cmd-1"})
        assert exc.value.result["command_id"] == "cmd-1"
        assert exc.value.result["command_status"] == "unknown"
        assert [req.get_method() for req in sent] == ["POST", "GET"]
        assert [header(req, trigger_client.CREDENTIAL_HEADER) for req in sent] == [
            FAKE_CREDENTIAL,
            renewed,
        ]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestIdentityIsTheCredential:
    """AC2, at the CLI seam."""

    def test_status_presents_the_credential_header(self, sent):
        trigger_client.send_status("run-7")
        assert header(sent[0], trigger_client.CREDENTIAL_HEADER) == FAKE_CREDENTIAL

    def test_control_presents_the_credential_header(self, sent):
        trigger_client.send_control("run-7", "pause", {"command_id": "cmd-1"})
        assert header(sent[0], trigger_client.CREDENTIAL_HEADER) == FAKE_CREDENTIAL

    def test_the_credential_is_covered_by_the_sigv4_signature(self, sent):
        """A header added after signing is one an intermediary can replace."""
        trigger_client.send_status("run-7")
        signed = header(sent[0], "Authorization")
        assert "AWS4-HMAC-SHA256" in signed
        assert trigger_client.CREDENTIAL_HEADER.lower() in signed.lower()

    def test_the_control_body_carries_no_caller_identity(self):
        """The body is data; the header is identity."""
        body = trigger_client.build_control_body(command_id="cmd-1", reason="budget review")
        assert set(body) == {"command_id", "reason"}

    def test_a_rewritten_message_id_does_not_change_the_request(self, monkeypatch, sent):
        """The concrete version of AC2: env rewriting buys nothing here.

        ``ADP_MESSAGE_ID`` is what today's dispatch route trusts as
        ``parent_invocation_id``. On the status path it is not consulted at all, so
        a worker that rewrites it sends a byte-identical request.
        """
        trigger_client.send_status("run-7")
        honest = (sent[0].full_url, header(sent[0], trigger_client.CREDENTIAL_HEADER), sent[0].data)

        sent.clear()
        monkeypatch.setenv("ADP_MESSAGE_ID", "inv-somebody-elses-run")
        trigger_client.send_status("run-7")
        rewritten = (
            sent[0].full_url,
            header(sent[0], trigger_client.CREDENTIAL_HEADER),
            sent[0].data,
        )

        assert honest == rewritten

    def test_dispatch_still_sends_message_id_as_parent(self):
        """Unchanged on purpose: that IS the /agent/trigger contract.

        Pinned so the compatibility decision is explicit rather than looking like
        an oversight next to the tests above.
        """
        body = trigger_client.build_body(persona="reviewer", issue=1, repo="o/r")
        assert body["parent_invocation_id"] == "msg-xyz-789"


class TestRequestShape:
    def test_status_is_a_get_with_the_run_in_the_query(self, sent):
        trigger_client.send_status("run-7")
        assert sent[0].get_method() == "GET"
        assert sent[0].full_url == f"{CONTROL_BASE}/status?run=run-7"
        assert sent[0].data is None

    def test_control_puts_the_action_in_the_path(self, sent):
        """One source for the action, so the envelope binds what the route dispatched on."""
        trigger_client.send_control("run-7", "pause", {"command_id": "cmd-1"})
        assert sent[0].get_method() == "POST"
        assert sent[0].full_url == f"{CONTROL_BASE}/control/run-7/pause"

    def test_a_run_id_with_url_metacharacters_is_escaped(self, sent):
        """A run ID is caller-supplied text, so it is escaped rather than interpolated."""
        trigger_client.send_control("run/../admin?x=1", "pause", {"command_id": "cmd-1"})
        assert "/control/run%2F..%2Fadmin%3Fx%3D1/pause" in sent[0].full_url

    def test_a_run_id_with_metacharacters_is_escaped_in_the_query_too(self, sent):
        trigger_client.send_status("run 7&admin=1")
        assert sent[0].full_url == f"{CONTROL_BASE}/status?run=run+7%26admin%3D1"

    def test_the_control_body_is_json(self, sent):
        trigger_client.send_control(
            "run-7", "steer", {"command_id": "cmd-1", "instruction": "focus on tests"}
        )
        assert json.loads(sent[0].data.decode()) == {
            "command_id": "cmd-1",
            "instruction": "focus on tests",
        }


class TestBuildControlBody:
    def test_command_id_only(self):
        assert trigger_client.build_control_body(command_id="cmd-1") == {"command_id": "cmd-1"}

    def test_omits_absent_optional_fields(self):
        body = trigger_client.build_control_body(command_id="cmd-1", instruction=None, reason=None)
        assert "instruction" not in body
        assert "reason" not in body

    def test_carries_instruction_and_reason_when_given(self):
        body = trigger_client.build_control_body(
            command_id="cmd-1", instruction="do x", reason="because"
        )
        assert body == {"command_id": "cmd-1", "instruction": "do x", "reason": "because"}


class TestUnsupportedVerbExitCode:
    def test_a_501_exits_3_not_1(self, monkeypatch):
        """ "Not built here" is not the same as "refused", and scripts must be able to tell."""
        from urllib.error import HTTPError

        frozen = MagicMock()
        frozen.access_key = "AKIAIOSFODNN7EXAMPLE"
        frozen.secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        frozen.token = None
        creds = MagicMock()
        creds.get_frozen_credentials.return_value = frozen

        body = MagicMock()
        body.read.return_value = b'{"detail": "pause is not implemented in this deployment"}'
        error = HTTPError(f"{CONTROL_BASE}/control/run-7/pause", 501, "Not Implemented", {}, body)

        def raise_501(req, timeout=None):
            raise error

        with (
            patch("botocore.session.get_session") as session,
            patch("adp_trigger.client.urlopen", raise_501),
        ):
            session.return_value.get_credentials.return_value = creds
            with pytest.raises(SystemExit) as exc:
                trigger_client.send_control("run-7", "pause", {"command_id": "cmd-1"})

        assert exc.value.code == trigger_client.EXIT_UNSUPPORTED

    def test_a_404_exits_1(self, monkeypatch):
        from urllib.error import HTTPError

        frozen = MagicMock()
        frozen.access_key = "AKIAIOSFODNN7EXAMPLE"
        frozen.secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        frozen.token = None
        creds = MagicMock()
        creds.get_frozen_credentials.return_value = frozen

        body = MagicMock()
        body.read.return_value = b'{"detail": "not found"}'
        error = HTTPError(f"{CONTROL_BASE}/status", 404, "Not Found", {}, body)

        def raise_404(req, timeout=None):
            raise error

        with (
            patch("botocore.session.get_session") as session,
            patch("adp_trigger.client.urlopen", raise_404),
        ):
            session.return_value.get_credentials.return_value = creds
            with pytest.raises(SystemExit) as exc:
                trigger_client.send_status("run-7")

        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def _status_response():
    response = MagicMock()
    response.read.return_value = json.dumps(
        {"run_id": "run-7", "state": "paused", "generation": 2}
    ).encode()
    response.__enter__.return_value = response
    return response


class TestControlTimeout:
    @pytest.mark.parametrize("failure", ["connect", "read", "wrapped", "gateway"])
    def test_timeout_keeps_command_unknown_and_performs_one_signed_read(
        self, sent, monkeypatch, failure
    ):
        private_instruction = "private instruction that must not be echoed"
        body = {"command_id": "cmd-original", "instruction": private_instruction}

        def transport(req, timeout=None):
            sent.append(req)
            if req.get_method() == "GET":
                return _status_response()
            if failure == "read":
                response = _status_response()
                response.read.side_effect = TimeoutError("read timed out")
                return response
            if failure == "wrapped":
                raise URLError(TimeoutError("connect timed out"))
            if failure == "gateway":
                raise HTTPError(req.full_url, 504, "Gateway Timeout", {}, None)
            raise TimeoutError("connect timed out")

        monkeypatch.setattr(trigger_client, "urlopen", transport)
        with pytest.raises(trigger_client.ControlOutcomeUnknown) as exc:
            trigger_client.send_control("run-7", "steer", body)

        result = exc.value.result
        assert result["command_id"] == "cmd-original"
        assert result["command_status"] == "unknown"
        # A run state, including a later generation, cannot prove this command applied.
        assert result["status_lookup"]["state"] == "paused"
        assert result["status_lookup"]["generation"] == 2
        assert [req.get_method() for req in sent] == ["POST", "GET"]
        assert json.loads(sent[0].data) == body
        assert sent[1].full_url == f"{CONTROL_BASE}/status?run=run-7"
        for request in sent:
            assert header(request, trigger_client.CREDENTIAL_HEADER) == FAKE_CREDENTIAL
            assert "AWS4-HMAC-SHA256" in header(request, "Authorization")
        assert private_instruction not in json.dumps(result)
        assert FAKE_CREDENTIAL not in json.dumps(result)

    @pytest.mark.parametrize("failure", ["timeout", "unreachable", "malformed"])
    def test_failed_lookup_preserves_unknown_and_does_not_retry(self, sent, monkeypatch, failure):
        def transport(req, timeout=None):
            sent.append(req)
            if req.get_method() == "POST" or failure == "timeout":
                raise TimeoutError("timed out")
            if failure == "unreachable":
                raise URLError("connection refused")
            response = _status_response()
            response.read.return_value = b"not json"
            return response

        monkeypatch.setattr(trigger_client, "urlopen", transport)
        with pytest.raises(trigger_client.ControlOutcomeUnknown) as exc:
            trigger_client.send_control("run-7", "pause", {"command_id": "cmd-original"})
        assert exc.value.result["command_id"] == "cmd-original"
        assert exc.value.result["command_status"] == "unknown"
        assert exc.value.result["status_lookup"] is None
        assert [req.get_method() for req in sent] == ["POST", "GET"]

    @pytest.mark.parametrize("status,exit_code", [(404, 1), (501, trigger_client.EXIT_UNSUPPORTED)])
    def test_definite_refusals_do_not_trigger_reconciliation(
        self, sent, monkeypatch, status, exit_code
    ):
        def transport(req, timeout=None):
            sent.append(req)
            raise HTTPError(req.full_url, status, "Refused", {}, None)

        monkeypatch.setattr(trigger_client, "urlopen", transport)
        with pytest.raises(SystemExit) as exc:
            trigger_client.send_control("run-7", "pause", {"command_id": "cmd-original"})
        assert exc.value.code == exit_code
        assert [req.get_method() for req in sent] == ["POST"]

    def test_cli_emits_unknown_json_and_distinct_exit_code(self, sent, monkeypatch, capsys):
        from adp_trigger.__main__ import main

        def transport(req, timeout=None):
            sent.append(req)
            if req.get_method() == "POST":
                raise TimeoutError("timed out")
            return _status_response()

        monkeypatch.setattr(trigger_client, "urlopen", transport)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "adp-trigger",
                "control",
                "--run",
                "run-7",
                "--action",
                "pause",
                "--command-id",
                "cmd-original",
            ],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == trigger_client.EXIT_UNKNOWN == 4
        output = json.loads(capsys.readouterr().out)
        assert output["command_status"] == "unknown"
        assert output["command_id"] == "cmd-original"
        assert [req.get_method() for req in sent] == ["POST", "GET"]


class _Cli:
    """Runs the CLI as a subprocess, the way an agent actually invokes it."""

    @staticmethod
    def run(args: list[str], env_override: dict | None = None) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "ADP_CORRELATION_ID": "corr-test",
            "ADP_MESSAGE_ID": "msg-test",
            "ADP_CHAIN_DEPTH": "2",
            "ADP_TRIGGER_ENDPOINT": TRIGGER_URL,
            "ADP_RUN_CREDENTIAL": FAKE_CREDENTIAL,
            "GITHUB_REPOSITORY": "aws-e/adp",
            "AWS_REGION": "us-east-1",
            "PYTHONPATH": os.path.join(os.path.dirname(__file__), ".."),
        }
        for key, value in (env_override or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return subprocess.run(
            [sys.executable, "-m", "adp_trigger"] + args,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )


class TestBackwardCompatibility:
    """The dispatch form must be unchanged. A regression here breaks every caller."""

    def test_persona_dispatch_needs_no_subcommand_word(self):
        result = _Cli.run(["--persona", "reviewer", "--issue", "42"])
        # Exit 2 would mean a usage or environment rejection — i.e. the form
        # stopped being accepted. Anything else means it was accepted and the
        # request was attempted.
        assert result.returncode != 2, result.stderr

    def test_a_persona_literally_named_status_is_still_a_dispatch(self):
        """Subcommand detection must not shadow a flag value."""
        result = _Cli.run(["--persona", "status", "--issue", "42"])
        assert result.returncode != 2, result.stderr

    def test_missing_persona_still_exits_2_with_the_same_message(self):
        result = _Cli.run(["--issue", "42"])
        assert result.returncode == 2
        assert "--persona is required" in result.stderr

    def test_a_non_numeric_issue_still_exits_2(self):
        result = _Cli.run(["--persona", "reviewer", "--issue", "abc"])
        assert result.returncode == 2
        assert "must be a number" in result.stderr

    def test_unknown_flags_are_still_rejected(self):
        result = _Cli.run(["--persona", "reviewer", "--issue", "1", "--unknown", "x"])
        assert result.returncode == 2
        assert "unknown argument" in result.stderr

    def test_help_still_exits_2_and_prints_usage(self):
        result = _Cli.run(["--help"])
        assert result.returncode == 2
        assert "Usage:" in result.stderr


class TestStatusSubcommandUsage:
    def test_status_without_run_exits_2(self):
        result = _Cli.run(["status"])
        assert result.returncode == 2
        assert "requires --run" in result.stderr

    def test_status_with_an_unknown_flag_exits_2(self):
        result = _Cli.run(["status", "--run", "r1", "--persona", "reviewer"])
        assert result.returncode == 2
        assert "unknown argument" in result.stderr

    def test_status_without_a_credential_exits_2_with_an_explanation(self):
        result = _Cli.run(["status", "--run", "r1"], env_override={"ADP_RUN_CREDENTIAL": None})
        assert result.returncode == 2
        assert "ADP_RUN_CREDENTIAL" in result.stderr

    def test_a_flag_missing_its_value_exits_2(self):
        result = _Cli.run(["status", "--run"])
        assert result.returncode == 2
        assert "requires a value" in result.stderr

    def test_a_flag_swallowing_the_next_flag_exits_2(self):
        """ "--run --action pause" must not request a run named "--action"."""
        result = _Cli.run(["control", "--run", "--action", "pause"])
        assert result.returncode == 2
        assert "requires a value" in result.stderr


class TestControlSubcommandUsage:
    def test_control_requires_run(self):
        result = _Cli.run(["control", "--action", "pause", "--command-id", "c1"])
        assert result.returncode == 2
        assert "requires --run" in result.stderr

    def test_control_requires_action(self):
        result = _Cli.run(["control", "--run", "r1", "--command-id", "c1"])
        assert result.returncode == 2
        assert "requires --action" in result.stderr

    def test_control_requires_command_id(self):
        result = _Cli.run(["control", "--run", "r1", "--action", "pause"])
        assert result.returncode == 2
        assert "--command-id" in result.stderr

    def test_the_command_id_error_explains_idempotency(self):
        """An operator told only "required" would generate a fresh one per retry."""
        result = _Cli.run(["control", "--run", "r1", "--action", "pause"])
        assert "idempotency" in result.stderr

    def test_an_unknown_action_is_a_local_usage_error(self):
        result = _Cli.run(["control", "--run", "r1", "--action", "pasue", "--command-id", "c1"])
        assert result.returncode == 2
        assert "--action must be one of" in result.stderr

    def test_steer_requires_an_instruction(self):
        result = _Cli.run(["control", "--run", "r1", "--action", "steer", "--command-id", "c1"])
        assert result.returncode == 2
        assert "requires --instruction" in result.stderr

    def test_an_instruction_on_a_non_steer_verb_is_rejected_not_dropped(self):
        result = _Cli.run(
            [
                "control",
                "--run",
                "r1",
                "--action",
                "pause",
                "--command-id",
                "c1",
                "--instruction",
                "do x",
            ]
        )
        assert result.returncode == 2
        assert "only valid with --action steer" in result.stderr

    def test_an_unknown_command_word_exits_2(self):
        result = _Cli.run(["controll", "--run", "r1"])
        assert result.returncode == 2
        assert "unknown command" in result.stderr

    @pytest.mark.parametrize("action", ["pause", "resume", "abort"])
    def test_a_well_formed_command_passes_local_validation(self, action):
        """It will be refused or 501'd remotely; what matters is that it got sent.

        Exit 2 is the local-rejection code, so "not 2" is the assertion that local
        validation accepted the invocation.
        """
        result = _Cli.run(["control", "--run", "r1", "--action", action, "--command-id", "c1"])
        assert result.returncode != 2, result.stderr

    def test_a_well_formed_steer_passes_local_validation(self):
        result = _Cli.run(
            [
                "control",
                "--run",
                "r1",
                "--action",
                "steer",
                "--command-id",
                "c1",
                "--instruction",
                "focus on tests",
            ]
        )
        assert result.returncode != 2, result.stderr


class TestUsageText:
    def test_usage_documents_all_three_forms(self):
        err = _Cli.run(["--help"]).stderr
        assert "--persona PERSONA" in err
        assert "status --run" in err
        assert "control --run" in err

    def test_usage_documents_the_exit_codes(self):
        """Including 3, which is the only way a caller learns a verb is unbuilt."""
        err = _Cli.run(["--help"]).stderr
        assert "Exit codes:" in err
        assert "3 verb not" in err

    def test_usage_names_the_credential_requirement(self):
        err = _Cli.run(["--help"]).stderr
        assert "ADP_RUN_CREDENTIAL" in err

    def test_usage_does_not_promise_control_verbs_work(self):
        """The help text must not advertise behaviour this deployment lacks.

        Guarded because usage text is the one place where an aspirational sentence
        would read as a shipped feature.
        """
        err = _Cli.run(["--help"]).stderr.lower()
        for overpromise in ("pauses the run", "aborts the run", "will pause", "will abort"):
            assert overpromise not in err


def test_enabled_spawn_uses_verified_route_and_no_environment_identity(monkeypatch):
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_AGENT_CONTROL_ENDPOINT", "https://gateway.example/internal/v1/agent")
    monkeypatch.setenv("ADP_RUN_CREDENTIAL", FAKE_CREDENTIAL)
    monkeypatch.delenv("ADP_RUN_CREDENTIAL_FILE", raising=False)
    monkeypatch.delenv("ADP_CORRELATION_ID", raising=False)
    monkeypatch.delenv("ADP_MESSAGE_ID", raising=False)
    body = trigger_client.build_body("reviewer", 42, "org/repo", "review approved work")
    assert set(body) == {"persona", "target", "reason", "request_id"}
    assert trigger_client.build_body("reviewer", 42, "org/repo", "review approved work") == body
    monkeypatch.setenv("ADP_MESSAGE_ID", "forged-another-worker")
    assert trigger_client.build_body("reviewer", 42, "org/repo", "review approved work") == body
    with patch.object(trigger_client, "_send", return_value={"status": "accepted"}) as send:
        trigger_client.send_trigger(body)
    send.assert_called_once_with(
        "POST",
        "https://gateway.example/internal/v1/agent/dispatch",
        body=body,
        extra_headers={trigger_client.CREDENTIAL_HEADER: FAKE_CREDENTIAL},
    )
