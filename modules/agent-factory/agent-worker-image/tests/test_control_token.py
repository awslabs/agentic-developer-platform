"""Tests for the per-run control token and endpoint registration — Issue #3960.

Two properties dominate this file:

1. **The token is a credential minted in the pod.** It comes from a CSPRNG, is
   unique per run, never reaches ``os.environ`` (so no other subprocess inherits
   it), and is removed at terminal teardown. Several tests assert on what is
   *absent* — from logs, from the parent environment — because a leak is invisible
   in a test that only checks the happy path.

2. **Registration failure must be loud and must prevent the listener.** The
   surrounding ``update_status`` swallows every exception, so a control write that
   failed the same way would produce a run the UI offers controls for and no
   signal anywhere. The tests pin the ``None`` return and the error log.

3. **The generation must actually change between attempts.** It is assigned by an
   atomic increment on the invocation row, not read from configuration, because a
   Job retry pod inherits a byte-identical env and any config-derived value would
   be the same constant forever. The tests assert the value is strictly increasing
   across registrations for the same run, not merely that one was parsed.
"""

from __future__ import annotations

import calendar
import logging
import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entrypoint
from lib import invocation_status

# The two writers name their key differently by convention: entrypoint speaks in
# envelope terms (message_id), lib/invocation_status in table terms (event_id).
# Both refer to the same row.
KEY = {"event_id": "msg-abc", "arrived_at": "2026-09-12T10:00:00Z"}
RUN_KEY = ("msg-abc", "2026-09-12T10:00:00Z")


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset the cached DDB client and table name between tests."""
    invocation_status._ddb = None
    invocation_status._table_name = ""
    yield
    invocation_status._ddb = None
    invocation_status._table_name = ""


def _ddb_add_semantics(start: int = 0):
    """An ``update_item`` stub that emulates DynamoDB's atomic ADD on the row.

    Registration no longer takes the generation as an argument — it reads back the
    value DynamoDB assigned. A MagicMock's default return would satisfy any
    assertion about "a generation came back", so the counter lives here: each call
    increments exactly as the real ADD does, which is what makes a retry's
    generation genuinely different from the first attempt's.
    """
    state = {"generation": start}

    def _update_item(**kwargs):
        if "ADD control_generation" in kwargs.get("UpdateExpression", ""):
            state["generation"] += 1
        return {"Attributes": {"control_generation": {"N": str(state["generation"])}}}

    return _update_item


@pytest.fixture
def ddb():
    """A stub DynamoDB client whose update_item calls are inspectable."""
    client = MagicMock()
    client.update_item.side_effect = _ddb_add_semantics()
    with patch.object(invocation_status, "_get_client", return_value=client):
        yield client


# ===========================================================================
# The flag (FR-8.3)
# ===========================================================================


class TestControlFlag:
    @patch.dict(os.environ, {"FEATURE_AGENT_CONTROL_ENABLED": "true"}, clear=False)
    def test_enabled_only_on_exact_true(self):
        assert entrypoint._is_agent_control_enabled() is True

    @pytest.mark.parametrize("value", ["TRUE", "True", "1", "yes", "false", "", "  "])
    def test_disabled_for_near_miss_values(self, value):
        """A typo must not open a control channel."""
        with patch.dict(os.environ, {"FEATURE_AGENT_CONTROL_ENABLED": value}, clear=False):
            assert entrypoint._is_agent_control_enabled() is False

    def test_disabled_when_absent(self):
        with patch.dict(os.environ, {}, clear=True):
            assert entrypoint._is_agent_control_enabled() is False

    @patch.dict(os.environ, {"FEATURE_AGENT_CONTROL_ENABLED": " true "}, clear=False)
    def test_tolerates_surrounding_whitespace(self):
        """Env vars set from YAML pick up stray whitespace; the value is still 'true'."""
        assert entrypoint._is_agent_control_enabled() is True


class TestControlPort:
    @patch.dict(os.environ, {"ADP_CONTROL_PORT": "9123"}, clear=False)
    def test_reads_configured_port(self):
        assert entrypoint._control_port() == 9123

    @pytest.mark.parametrize("value", ["", "abc", "0", "-1", "70000", "8770.5"])
    def test_falls_back_to_default_for_invalid_values(self, value):
        """Never an arbitrary port: the ingress policy names exactly one."""
        with patch.dict(os.environ, {"ADP_CONTROL_PORT": value}, clear=False):
            assert entrypoint._control_port() == 8770

    def test_reads_the_name_terraform_actually_injects(self):
        """The reader's variable name is part of the contract, not an internal detail.

        scaledjob.tf renders ADP_CONTROL_PORT. An earlier revision read
        AGENT_CONTROL_PORT here, so a configured non-default port was ignored: the
        pod bound 8770 while the NetworkPolicy allowed the configured port, and
        nothing anywhere errored — the listener was simply unreachable. Asserted as
        "the other name has no effect" because a test that only checks the correct
        name passes just as well when the code reads both.
        """
        with patch.dict(
            os.environ, {"AGENT_CONTROL_PORT": "9123", "ADP_CONTROL_PORT": ""}, clear=False
        ):
            assert entrypoint._control_port() == 8770


class TestTokenTtl:
    @patch.dict(os.environ, {"ADP_POD_DEADLINE_SECONDS": "3600"}, clear=False)
    def test_uses_the_pod_deadline(self):
        """The credential expires with the process it authenticates."""
        assert entrypoint._control_token_ttl_seconds() == 3600

    @patch.dict(os.environ, {"ADP_POD_DEADLINE_SECONDS": "999999"}, clear=False)
    def test_caps_at_the_absolute_ceiling(self):
        """An unbounded TTL would turn a leaked token into a permanent one.

        The cap still applies when the deadline exceeds it: raising
        agent_pod_deadline_seconds must not quietly extend a credential's life.
        """
        assert entrypoint._control_token_ttl_seconds() == entrypoint.MAX_CONTROL_TOKEN_TTL_SECONDS

    @pytest.mark.parametrize("value", ["", "0", "abc"])
    def test_defaults_to_the_ceiling(self, value):
        with patch.dict(os.environ, {"ADP_POD_DEADLINE_SECONDS": value}, clear=False):
            assert (
                entrypoint._control_token_ttl_seconds() == entrypoint.MAX_CONTROL_TOKEN_TTL_SECONDS
            )

    def test_reads_the_name_terraform_actually_injects(self):
        """Same failure mode as the port: a name only Terraform knows.

        The previous revision read AGENT_RUN_TIMEOUT_SECONDS, which nothing in the
        repo ever set, so every token got the 6h fallback regardless of the pod's
        real deadline. A short-deadline pod therefore left a credential valid for
        hours after it was gone.
        """
        with patch.dict(
            os.environ,
            {"AGENT_RUN_TIMEOUT_SECONDS": "60", "ADP_POD_DEADLINE_SECONDS": ""},
            clear=False,
        ):
            assert (
                entrypoint._control_token_ttl_seconds() == entrypoint.MAX_CONTROL_TOKEN_TTL_SECONDS
            )


# ===========================================================================
# Setup: minting, registration, and what must NOT happen (FR-1.1, FR-1.12)
# ===========================================================================


class TestSetupAgentControl:
    def test_flag_off_writes_nothing_and_sets_no_env(self, ddb):
        """A flag-off run must be indistinguishable from one predating the feature."""
        agent_env = {}
        with patch.dict(os.environ, {"POD_IP": "10.0.1.5"}, clear=True):
            assert entrypoint._setup_agent_control(agent_env, *RUN_KEY) is False

        ddb.update_item.assert_not_called()
        assert agent_env == {}

    def test_missing_pod_ip_refuses_rather_than_binding_everything(self, ddb, caplog):
        """No POD_IP is a hard stop, never a fallback to all interfaces."""
        agent_env = {}
        with patch.dict(
            os.environ,
            {"FEATURE_AGENT_CONTROL_ENABLED": "true", "POD_IP": "", "WEBHOOK_EVENTS_TABLE": "t"},
            clear=True,
        ):
            with caplog.at_level(logging.ERROR):
                assert entrypoint._setup_agent_control(agent_env, *RUN_KEY) is False

        ddb.update_item.assert_not_called()
        assert agent_env == {}
        assert "POD_IP" in caplog.text

    def test_registers_and_populates_child_env(self, ddb):
        agent_env = {}
        with patch.dict(
            os.environ,
            {
                "FEATURE_AGENT_CONTROL_ENABLED": "true",
                "POD_IP": "10.0.1.5",
                "ADP_CONTROL_PORT": "8770",
                "WEBHOOK_EVENTS_TABLE": "webhook-events",
            },
            clear=True,
        ):
            assert entrypoint._setup_agent_control(agent_env, *RUN_KEY) is True

        assert agent_env["ADP_CONTROL_BIND_ADDRESS"] == "10.0.1.5"
        assert agent_env["ADP_CONTROL_PORT"] == "8770"
        assert agent_env["ADP_CONTROL_GENERATION"] == "1"
        assert len(agent_env["ADP_CONTROL_TOKEN"]) >= 32

    def test_the_listener_is_told_the_generation_the_row_assigned(self, ddb):
        """The child env must carry the row's number, not a locally chosen one.

        The gateway reads ``control_generation`` off the row and sends it as a
        header; the listener compares it to what this env var says. If the two
        sources can differ, every command the gateway sends is rejected as stale —
        so the value handed to the child has to be the value the write returned.
        """
        agent_env = {}
        ddb.update_item.side_effect = _ddb_add_semantics(start=6)
        with patch.dict(
            os.environ,
            {
                "FEATURE_AGENT_CONTROL_ENABLED": "true",
                "POD_IP": "10.0.1.5",
                "WEBHOOK_EVENTS_TABLE": "webhook-events",
            },
            clear=True,
        ):
            assert entrypoint._setup_agent_control(agent_env, *RUN_KEY) is True

        assert agent_env["ADP_CONTROL_GENERATION"] == "7"

    def test_a_retry_attempt_gets_a_higher_generation_than_the_one_it_replaces(self, ddb):
        """The property the listener's generation check depends on (FR-1.9).

        `backoffLimit: 2` means retry pods genuinely exist. A retry inherits a
        byte-identical env from the Job template, so anything derived from
        configuration is the same constant on every attempt — which is how the
        previous revision ended up comparing 1 against 1 forever and a command
        aimed at attempt 1 could land on attempt 2. Asserted as strictly
        increasing across two registrations for the SAME run key.
        """
        generations = []
        for _ in range(3):
            agent_env = {}
            with patch.dict(
                os.environ,
                {
                    "FEATURE_AGENT_CONTROL_ENABLED": "true",
                    "POD_IP": "10.0.1.5",
                    "WEBHOOK_EVENTS_TABLE": "webhook-events",
                },
                clear=True,
            ):
                assert entrypoint._setup_agent_control(agent_env, *RUN_KEY) is True
            generations.append(int(agent_env["ADP_CONTROL_GENERATION"]))

        assert generations == sorted(set(generations)), (
            f"each attempt must record a strictly higher generation, got {generations}"
        )
        assert len(set(generations)) == len(generations)

    def test_registration_is_not_trusted_when_no_generation_comes_back(self, ddb, caplog):
        """A write that returns no generation is a failed registration, not a default.

        Falling back to a guess would mean the listener enforces one number while
        the gateway reads another, and every legitimate command would be refused as
        stale — indistinguishable from a replay attack in the logs.
        """
        ddb.update_item.side_effect = None
        ddb.update_item.return_value = {"Attributes": {}}
        agent_env = {}
        with patch.dict(
            os.environ,
            {
                "FEATURE_AGENT_CONTROL_ENABLED": "true",
                "POD_IP": "10.0.1.5",
                "WEBHOOK_EVENTS_TABLE": "webhook-events",
            },
            clear=True,
        ):
            with caplog.at_level(logging.WARNING):
                assert entrypoint._setup_agent_control(agent_env, *RUN_KEY) is False

        assert "ADP_CONTROL_TOKEN" not in agent_env
        assert "ADP_CONTROL_GENERATION" not in agent_env

    def test_token_never_reaches_the_parent_environment(self, ddb):
        """Only the agent child gets the token — not gh, git or the sigv4 proxy."""
        agent_env = {}
        with patch.dict(
            os.environ,
            {
                "FEATURE_AGENT_CONTROL_ENABLED": "true",
                "POD_IP": "10.0.1.5",
                "WEBHOOK_EVENTS_TABLE": "webhook-events",
            },
            clear=True,
        ):
            entrypoint._setup_agent_control(agent_env, *RUN_KEY)
            assert "ADP_CONTROL_TOKEN" not in os.environ

    def test_token_is_unique_per_run(self, ddb):
        """A reused token would let a command for one run authenticate to another."""
        tokens = set()
        for _ in range(20):
            agent_env = {}
            with patch.dict(
                os.environ,
                {
                    "FEATURE_AGENT_CONTROL_ENABLED": "true",
                    "POD_IP": "10.0.1.5",
                    "WEBHOOK_EVENTS_TABLE": "webhook-events",
                },
                clear=True,
            ):
                entrypoint._setup_agent_control(agent_env, *RUN_KEY)
            tokens.add(agent_env["ADP_CONTROL_TOKEN"])

        assert len(tokens) == 20

    def test_token_is_not_logged(self, ddb, caplog):
        """The one place a token would plausibly leak in bulk."""
        agent_env = {}
        with patch.dict(
            os.environ,
            {
                "FEATURE_AGENT_CONTROL_ENABLED": "true",
                "POD_IP": "10.0.1.5",
                "WEBHOOK_EVENTS_TABLE": "webhook-events",
            },
            clear=True,
        ):
            with caplog.at_level(logging.DEBUG):
                entrypoint._setup_agent_control(agent_env, *RUN_KEY)

        assert agent_env["ADP_CONTROL_TOKEN"] not in caplog.text

    def test_failed_registration_does_not_start_a_listener(self, caplog):
        """An unregistered listener is attack surface with no capability.

        Nothing knows its token and the policy cannot route to it, so the child
        env must be left without control variables entirely.
        """
        agent_env = {}
        with patch.object(entrypoint, "register_control_endpoint", return_value=None):
            with patch.dict(
                os.environ,
                {
                    "FEATURE_AGENT_CONTROL_ENABLED": "true",
                    "POD_IP": "10.0.1.5",
                    "WEBHOOK_EVENTS_TABLE": "webhook-events",
                },
                clear=True,
            ):
                with caplog.at_level(logging.ERROR):
                    assert entrypoint._setup_agent_control(agent_env, *RUN_KEY) is False

        assert "ADP_CONTROL_TOKEN" not in agent_env
        assert "registration failed" in caplog.text.lower()

    def test_unexpected_exception_is_contained_and_logged(self, caplog):
        """Control must never abort the run it observes."""
        agent_env = {}
        with patch.object(
            entrypoint, "register_control_endpoint", side_effect=RuntimeError("boom")
        ):
            with patch.dict(
                os.environ,
                {
                    "FEATURE_AGENT_CONTROL_ENABLED": "true",
                    "POD_IP": "10.0.1.5",
                    "WEBHOOK_EVENTS_TABLE": "webhook-events",
                },
                clear=True,
            ):
                with caplog.at_level(logging.ERROR):
                    assert entrypoint._setup_agent_control(agent_env, *RUN_KEY) is False

        assert "boom" in caplog.text

    @pytest.mark.parametrize("deadline", [60, 900, 21600])
    def test_expiry_is_bounded_by_the_pod_deadline(self, ddb, deadline):
        """A token that outlived its pod would authenticate to a reused IP.

        The written timestamp is parsed and compared to the deadline, not merely
        matched against an ISO shape. The shape-only version of this assertion
        passed against an expiry of ``now + 30 days``, which is precisely the bug
        it was supposed to catch: the format is right in both cases and only the
        arithmetic is wrong.
        """
        with patch.dict(
            os.environ,
            {
                "FEATURE_AGENT_CONTROL_ENABLED": "true",
                "POD_IP": "10.0.1.5",
                "ADP_POD_DEADLINE_SECONDS": str(deadline),
                "WEBHOOK_EVENTS_TABLE": "webhook-events",
            },
            clear=True,
        ):
            before = time.time()
            entrypoint._setup_agent_control({}, *RUN_KEY)
            after = time.time()

        values = ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        expiry = values[":e"]["S"]
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", expiry)

        expiry_epoch = calendar.timegm(time.strptime(expiry, "%Y-%m-%dT%H:%M:%SZ"))
        # The bound: no later than the moment the pod itself is killed. One second
        # of slack absorbs the truncation to whole seconds in the format.
        assert expiry_epoch <= after + deadline + 1, (
            f"expiry {expiry} outlives the pod deadline of {deadline}s"
        )
        # And not so early that control is dead for most of a legitimate run.
        assert expiry_epoch >= before + deadline - 1

    def test_expiry_never_exceeds_the_ceiling_even_for_a_long_deadline(self, ddb):
        """Raising agent_pod_deadline_seconds must not extend a leaked credential."""
        with patch.dict(
            os.environ,
            {
                "FEATURE_AGENT_CONTROL_ENABLED": "true",
                "POD_IP": "10.0.1.5",
                "ADP_POD_DEADLINE_SECONDS": "864000",  # 10 days
                "WEBHOOK_EVENTS_TABLE": "webhook-events",
            },
            clear=True,
        ):
            before = time.time()
            entrypoint._setup_agent_control({}, *RUN_KEY)

        expiry = ddb.update_item.call_args.kwargs["ExpressionAttributeValues"][":e"]["S"]
        expiry_epoch = calendar.timegm(time.strptime(expiry, "%Y-%m-%dT%H:%M:%SZ"))
        assert expiry_epoch <= before + entrypoint.MAX_CONTROL_TOKEN_TTL_SECONDS + 1


class TestTeardownAgentControl:
    @pytest.fixture(autouse=True)
    def _clear_pending(self):
        """No teardown pending from another test's registration."""
        entrypoint._pending_control_teardown = None
        yield
        entrypoint._pending_control_teardown = None

    def test_no_writes_when_registration_never_happened(self, ddb):
        """A flag-off run performs no control writes at all, including deletes."""
        entrypoint._teardown_agent_control(*RUN_KEY, was_registered=False)
        ddb.update_item.assert_not_called()

    def test_clears_when_registered(self, ddb):
        entrypoint._pending_control_teardown = RUN_KEY
        with patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False):
            entrypoint._teardown_agent_control(*RUN_KEY, was_registered=True)

        assert "REMOVE" in ddb.update_item.call_args.kwargs["UpdateExpression"]

    def test_teardown_failure_is_contained(self, ddb):
        """Teardown runs on the way out of a completed run; it must not raise."""
        entrypoint._pending_control_teardown = RUN_KEY
        ddb.update_item.side_effect = RuntimeError("ddb down")
        with patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False):
            entrypoint._teardown_agent_control(*RUN_KEY, was_registered=True)


class TestTeardownSurvivesAbnormalExits:
    """The credential must be revoked however the pod ends (FR-1.10).

    The normal call sits on the straight-line path after the agent process exits.
    Everything after that point — PR creation, check-run finalisation, transcript
    upload — makes network calls that can raise, and `activeDeadlineSeconds`
    expiring or a node drain arrives as SIGTERM. Each of those endings skipped
    teardown entirely before this guard existed, leaving a live token and a pod IP
    on the row. Pod IPs are reused, so a stale address eventually names a different
    tenant's pod.
    """

    @pytest.fixture(autouse=True)
    def _clear_pending(self):
        entrypoint._pending_control_teardown = None
        yield
        entrypoint._pending_control_teardown = None

    def test_registration_arms_the_guard(self, ddb):
        with patch.dict(
            os.environ,
            {
                "FEATURE_AGENT_CONTROL_ENABLED": "true",
                "POD_IP": "10.0.1.5",
                "WEBHOOK_EVENTS_TABLE": "webhook-events",
            },
            clear=True,
        ):
            with patch.object(entrypoint.atexit, "register") as mock_atexit:
                with patch.object(entrypoint.signal, "signal") as mock_signal:
                    assert entrypoint._setup_agent_control({}, *RUN_KEY) is True

        assert entrypoint._pending_control_teardown == RUN_KEY
        mock_atexit.assert_called_once_with(entrypoint._revoke_pending_control)
        assert mock_signal.call_args.args[0] == entrypoint.signal.SIGTERM

    def test_a_failed_registration_arms_nothing(self, ddb):
        """No teardown may be scheduled for a registration that never happened."""
        with patch.object(entrypoint, "register_control_endpoint", return_value=None):
            with patch.dict(
                os.environ,
                {
                    "FEATURE_AGENT_CONTROL_ENABLED": "true",
                    "POD_IP": "10.0.1.5",
                    "WEBHOOK_EVENTS_TABLE": "webhook-events",
                },
                clear=True,
            ):
                assert entrypoint._setup_agent_control({}, *RUN_KEY) is False

        assert entrypoint._pending_control_teardown is None

    def test_the_atexit_backstop_clears_the_record(self, ddb):
        """Covers an exception in the post-agent handling: interpreter still exits."""
        entrypoint._pending_control_teardown = RUN_KEY
        with patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False):
            entrypoint._revoke_pending_control()

        assert ddb.update_item.call_args.kwargs["UpdateExpression"].startswith("REMOVE ")

    def test_revocation_happens_at_most_once(self, ddb):
        """The normal call, the atexit hook and a signal must not each write.

        Idempotence is what makes it safe to arm every backstop unconditionally.
        """
        entrypoint._pending_control_teardown = RUN_KEY
        with patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False):
            entrypoint._teardown_agent_control(*RUN_KEY, was_registered=True)
            entrypoint._revoke_pending_control()
            entrypoint._revoke_pending_control()

        assert ddb.update_item.call_count == 1

    def test_sigterm_revokes_then_dies_as_sigterm_would(self, ddb):
        """A deadline kill must revoke inside the grace period, then not change the exit.

        Re-raising with the default disposition rather than calling sys.exit keeps
        the pod's observable termination identical — the ScaledJob's retry
        behaviour depends on it.
        """
        entrypoint._pending_control_teardown = RUN_KEY
        with patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False):
            with patch.object(entrypoint.signal, "signal") as mock_signal:
                with patch.object(entrypoint.os, "kill") as mock_kill:
                    entrypoint._control_sigterm_handler(entrypoint.signal.SIGTERM, None)

        assert ddb.update_item.call_args.kwargs["UpdateExpression"].startswith("REMOVE ")
        mock_signal.assert_called_once_with(entrypoint.signal.SIGTERM, entrypoint.signal.SIG_DFL)
        mock_kill.assert_called_once_with(os.getpid(), entrypoint.signal.SIGTERM)

    def test_an_exception_inside_revocation_cannot_escape(self, caplog):
        """The backstop runs from atexit and from a signal handler.

        ``clear_control_endpoint`` contains its own failures today, so this except
        is defensive — but the two callers are exactly the places where an escaping
        exception is worst: from atexit it prints a traceback over the pod's real
        exit, and from the SIGTERM handler it would skip the re-raise that
        preserves the pod's termination behaviour.
        """
        entrypoint._pending_control_teardown = RUN_KEY
        with patch.object(
            entrypoint, "clear_control_endpoint", side_effect=RuntimeError("ddb unreachable")
        ):
            with caplog.at_level(logging.WARNING):
                entrypoint._revoke_pending_control()

        assert "ddb unreachable" in caplog.text
        assert entrypoint._pending_control_teardown is None

    def test_a_signal_handler_that_cannot_be_installed_is_not_fatal(self, ddb, caplog):
        """Off the main thread signal.signal raises; control must still register."""
        with patch.object(entrypoint.signal, "signal", side_effect=ValueError("not main thread")):
            with patch.dict(
                os.environ,
                {
                    "FEATURE_AGENT_CONTROL_ENABLED": "true",
                    "POD_IP": "10.0.1.5",
                    "WEBHOOK_EVENTS_TABLE": "webhook-events",
                },
                clear=True,
            ):
                with caplog.at_level(logging.WARNING):
                    assert entrypoint._setup_agent_control({}, *RUN_KEY) is True

        # atexit still covers the exception paths.
        assert entrypoint._pending_control_teardown == RUN_KEY


# ===========================================================================
# The DynamoDB record itself
# ===========================================================================


class TestRegisterControlEndpoint:
    GOOD = dict(
        address="10.0.1.5",
        port=8770,
        token="secret-token-value",
        token_expires_at="2026-09-12T16:00:00Z",
    )

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_writes_the_full_record_on_the_real_key(self, ddb):
        assert invocation_status.register_control_endpoint(**KEY, **self.GOOD) == 1

        kwargs = ddb.update_item.call_args.kwargs
        # The base table's key schema is (event_id PK, arrived_at SK). A single-key
        # write would raise at runtime against the real table.
        assert kwargs["Key"] == {
            "event_id": {"S": "msg-abc"},
            "arrived_at": {"S": "2026-09-12T10:00:00Z"},
        }
        values = kwargs["ExpressionAttributeValues"]
        assert values[":a"] == {"S": "10.0.1.5"}
        assert values[":p"] == {"N": "8770"}
        assert values[":t"] == {"S": "secret-token-value"}
        assert values[":v"] == {"N": str(invocation_status.CONTROL_RECORD_VERSION)}

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_the_generation_is_an_atomic_increment_not_a_supplied_value(self, ddb):
        """ADD, not SET, and no generation parameter to pass in.

        A read-then-write would race two attempts of the same message into the same
        generation; a caller-supplied value cannot differ between attempts at all,
        because a Job retry pod inherits an identical env. Asserted on the
        expression itself since neither failure is visible in the return value.
        """
        invocation_status.register_control_endpoint(**KEY, **self.GOOD)

        kwargs = ddb.update_item.call_args.kwargs
        assert "ADD control_generation :one" in kwargs["UpdateExpression"]
        assert "control_generation = " not in kwargs["UpdateExpression"], (
            "control_generation must never be SET — that overwrites the counter "
            "instead of advancing it, which is the inert-check bug all over again."
        )
        assert kwargs["ExpressionAttributeValues"][":one"] == {"N": "1"}
        # The assigned value has to come back from the assigning call: a follow-up
        # read would return whatever a concurrent attempt had incremented it to.
        assert kwargs["ReturnValues"] == "UPDATED_NEW"

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_successive_registrations_return_strictly_increasing_generations(self, ddb):
        first = invocation_status.register_control_endpoint(**KEY, **self.GOOD)
        second = invocation_status.register_control_endpoint(**KEY, **self.GOOD)
        third = invocation_status.register_control_endpoint(**KEY, **self.GOOD)

        assert [first, second, third] == [1, 2, 3]

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_a_write_that_returns_no_generation_is_a_failure(self, ddb, caplog):
        """None, not a guess: a wrong generation refuses every legitimate command."""
        ddb.update_item.side_effect = None
        ddb.update_item.return_value = {"Attributes": {}}
        with caplog.at_level(logging.WARNING):
            assert invocation_status.register_control_endpoint(**KEY, **self.GOOD) is None
        assert caplog.text

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_refuses_to_create_an_orphan_row(self, ddb):
        """Guarded so a control write cannot invent an invocation."""
        invocation_status.register_control_endpoint(**KEY, **self.GOOD)
        assert (
            "attribute_exists(event_id)" in ddb.update_item.call_args.kwargs["ConditionExpression"]
        )

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_does_not_log_the_token(self, ddb, caplog):
        with caplog.at_level(logging.DEBUG):
            invocation_status.register_control_endpoint(**KEY, **self.GOOD)
        assert "secret-token-value" not in caplog.text

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": ""}, clear=False)
    def test_returns_false_without_a_table(self, ddb, caplog):
        """False, not a silent None: the caller must be able to log and meter it."""
        with caplog.at_level(logging.WARNING):
            assert invocation_status.register_control_endpoint(**KEY, **self.GOOD) is None
        ddb.update_item.assert_not_called()
        assert caplog.text

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    @pytest.mark.parametrize(
        "override",
        [
            {"address": ""},
            {"token": ""},
            {"port": 0},
            {"port": -1},
            {"port": "8770"},
        ],
    )
    def test_rejects_invalid_parameters(self, ddb, override):
        """A row that claims registration but cannot be connected to is worse than none."""
        args = {**self.GOOD, **override}
        assert invocation_status.register_control_endpoint(**KEY, **args) is None
        ddb.update_item.assert_not_called()

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_missing_key_returns_false(self, ddb):
        assert (
            invocation_status.register_control_endpoint(
                event_id="", arrived_at="2026-09-12T10:00:00Z", **self.GOOD
            )
            is None
        )
        ddb.update_item.assert_not_called()

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_ddb_failure_returns_false_and_logs(self, ddb, caplog):
        """Contained, unlike update_status, but reported rather than swallowed."""
        ddb.update_item.side_effect = RuntimeError("throughput exceeded")
        with caplog.at_level(logging.WARNING):
            assert invocation_status.register_control_endpoint(**KEY, **self.GOOD) is None
        assert "throughput exceeded" in caplog.text


class TestClearControlEndpoint:
    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_removes_every_control_attribute(self, ddb):
        """Driven off the shared tuple so a new field cannot be left behind.

        A surviving address matters because pod IPs are reused: a stale one points
        at whatever pod holds that IP next.
        """
        assert invocation_status.clear_control_endpoint(**KEY) is True

        expr = ddb.update_item.call_args.kwargs["UpdateExpression"]
        assert expr.startswith("REMOVE ")
        removed = {name.strip() for name in expr[len("REMOVE ") :].split(",")}

        # Pinned literally rather than derived from _CONTROL_ATTRIBUTES. Looping
        # over the module's own tuple makes the test vacuous: deleting a field
        # from the tuple also deletes it from the expectation, so the write and
        # the cleanup could drift apart silently.
        assert removed == {
            "control_version",
            "control_address",
            "control_port",
            "control_token",
            "control_token_expires_at",
            "control_generation",
            "control_registered_at",
        }

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_removes_the_token_and_the_address(self, ddb):
        """The two fields whose survival is actually dangerous.

        Matched as exact comma-separated names, not with ``in``: a substring test
        for ``control_token`` is satisfied by ``control_token_expires_at``, so
        dropping the token field itself would pass unnoticed — which is exactly
        what happened to an earlier version of this assertion.
        """
        invocation_status.clear_control_endpoint(**KEY)
        expr = ddb.update_item.call_args.kwargs["UpdateExpression"]
        removed = {name.strip() for name in expr[len("REMOVE ") :].split(",")}

        assert "control_token" in removed
        assert "control_address" in removed

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_cleanup_covers_every_field_registration_writes(self, ddb):
        """Whatever registration sets, teardown must remove.

        This is the invariant that matters: a field added to the write path and
        forgotten in the cleanup path leaves data on a terminal row. Derived from
        the *write expression* rather than from a constant, so adding a field to
        registration without adding it to cleanup fails here.
        """
        invocation_status.register_control_endpoint(
            **KEY,
            address="10.0.1.5",
            port=8770,
            token="t",
            token_expires_at="2026-09-12T16:00:00Z",
        )
        written_expr = ddb.update_item.call_args.kwargs["UpdateExpression"]
        written = set(re.findall(r"\b(control_[a-z_]+)\b", written_expr))

        ddb.reset_mock()
        invocation_status.clear_control_endpoint(**KEY)
        removed_expr = ddb.update_item.call_args.kwargs["UpdateExpression"]
        removed = {name.strip() for name in removed_expr[len("REMOVE ") :].split(",")}

        assert written, "expected the registration write to set control_* fields"
        assert written <= removed, f"registered but never cleared: {written - removed}"

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": ""}, clear=False)
    def test_no_op_without_a_table(self, ddb):
        assert invocation_status.clear_control_endpoint(**KEY) is False
        ddb.update_item.assert_not_called()

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_failure_returns_false_without_raising(self, ddb):
        ddb.update_item.side_effect = RuntimeError("ddb down")
        assert invocation_status.clear_control_endpoint(**KEY) is False


# ===========================================================================
# Registration must not disturb the existing status path
# ===========================================================================


class TestStatusPathUnaffected:
    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "webhook-events"}, clear=False)
    def test_update_status_writes_no_control_fields(self, ddb):
        """The two writers stay separate: a status transition must not touch control."""
        invocation_status.update_status(**KEY, status="in_progress", run_id="job-1")

        expr = ddb.update_item.call_args.kwargs["UpdateExpression"]
        assert "control_" not in expr
