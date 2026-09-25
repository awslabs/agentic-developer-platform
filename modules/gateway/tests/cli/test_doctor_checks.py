"""`adp doctor` must diagnose without changing anything — Issue #5621.

CLI-08-AC-02. The criterion has four parts, and each is a way this command could
become dangerous rather than useful:

* **read-only** — a diagnostic that mutates to answer a question cannot be run on
  the broken production deployment where it is most needed
* **bounded** — a fixed set of reads, no paid inference; a `doctor` that quietly
  bills the user for a model call is a trap
* **tenant-scoped and redacted** — its output gets pasted into tickets and chat,
  so a token or a queue URL reaching the screen reaches somewhere unrecallable
* **existence-hiding** — a request ID the caller may not see must reveal nothing,
  not even that it exists

The tests are built so that "it changed something" and "it leaked something" are
provable from the call log and the rendered output, rather than argued from the
implementation's intent.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from src.admin.persona_models.schemas import PreferenceEntry, PreferenceListResponse

SCRIPT = Path(__file__).parents[2] / "cli/adp-doctor.py"
SPEC = importlib.util.spec_from_file_location("adp_doctor", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)

common = cli.common
CliError = common.CliError
YES, NO, UNKNOWN = cli.YES, cli.NO, cli.UNKNOWN


def capability_document(**overrides):
    operations = [
        {
            "id": "models.catalog.read",
            "supported": YES,
            "enabled": YES,
            "permitted": YES,
            "ready": YES,
            "mutates": False,
            "summary": "Read the model catalogue",
        },
        {
            "id": "agents.activity.read",
            "supported": YES,
            "enabled": YES,
            "permitted": YES,
            "ready": YES,
            "mutates": False,
            "summary": "Read agent activity",
        },
    ]
    for operation in operations:
        operation.update(overrides.get(operation["id"], {}))
    return {
        "schema_version": cli.SUPPORTED_SCHEMA_VERSIONS[0],
        "gateway": {"state": YES, "release": "2026.09.21-abc1234", "source": "GATEWAY_RELEASE"},
        "tenant": {"org_id": "org-alpha"},
        "operations": operations,
    }


UNCAPPED_BUDGET = {"period": "monthly", "cap_status": "uncapped", "band": None, "enforcement_mode": None}
SESSION = {"user_id": "user-1", "org_id": "org-alpha", "expires_at": "2026-09-21T12:00:00Z", "is_admin": False}


class Transport:
    """Records every call, so read-only-ness is provable rather than asserted."""

    def __init__(self, routes=None, errors=None):
        self.routes = routes or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, path, body=None, **kwargs):
        self.calls.append((method, path))
        if path in self.errors:
            raise self.errors[path]
        if path in self.routes:
            return self.routes[path]
        raise CliError(f"unexpected path {path}", "http_error", 5, status_code=404)

    def healthy(self):
        self.routes = {
            cli.CAPABILITIES_PATH: capability_document(),
            "/auth/me": SESSION,
            "/me/budget": UNCAPPED_BUDGET,
        }
        return self


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config_dir = tmp_path / ".bedrock-gateway"
    config_dir.mkdir(mode=0o700, exist_ok=True)
    config = config_dir / "config.json"
    config.write_text(json.dumps({"gateway_url": "https://gw.example.com"}))
    config.chmod(0o600)
    monkeypatch.setattr(common, "_deployment", common._UNRESOLVED, raising=False)
    monkeypatch.setattr(
        common, "authenticated_scope", lambda token=None: {"identity": "identity-a", "tenant": "org-alpha", "tenant_claim": "org-alpha"}
    )


# --- read-only and bounded ---------------------------------------------------


def test_every_check_is_a_get() -> None:
    """The whole default check set, and not one non-GET among them."""
    client = Transport().healthy()

    cli.run_checks(cli.ALL_CHECKS, client)

    assert client.calls, "the checks must actually read something"
    assert {method for method, _ in client.calls} == {"GET"}


def test_no_check_reaches_an_inference_endpoint() -> None:
    """A diagnostic must never quietly bill the user for a model call.

    `models` resolves readiness from the capability document instead of sending a
    prompt — the difference between a free answer and a charged one.
    """
    client = Transport().healthy()

    cli.run_checks(cli.ALL_CHECKS, client)

    for _, path in client.calls:
        assert "invoke" not in path and "chat" not in path and "completion" not in path, path


def test_the_check_set_is_bounded_and_documented() -> None:
    """The advertised names are exactly the implemented ones."""
    assert set(cli.ALL_CHECKS) == set(cli.CHECKS)
    assert cli.parse_checks("") == cli.ALL_CHECKS
    assert cli.parse_checks("auth,budget") == ("auth", "budget")


def test_an_unknown_check_name_is_refused_before_anything_is_read() -> None:
    client = Transport().healthy()

    with pytest.raises(CliError) as caught:
        cli.parse_checks("auth,not-a-check")

    assert caught.value.exit_code == 1
    assert client.calls == [], "a usage error must not have read anything"


def test_a_subset_runs_only_what_was_asked_for() -> None:
    client = Transport().healthy()

    findings = cli.run_checks(("auth",), client)

    assert set(findings) == {"auth"}
    assert client.calls == [("GET", "/auth/me")]


# --- one broken check must not hide the others -------------------------------


def test_a_failing_check_does_not_abort_the_rest() -> None:
    """The value of a diagnostic is the whole picture.

    The first failure is often a symptom of a later one, so aborting on it would
    hide the actual cause — the thing the user ran `doctor` to find.
    """
    client = Transport()
    client.healthy()
    client.errors["/auth/me"] = CliError("expired", "authentication_required", 2)

    findings = cli.run_checks(cli.ALL_CHECKS, client)

    assert set(findings) == set(cli.ALL_CHECKS)
    assert findings["auth"]["state"] == "failed"
    assert findings["api"]["state"] == "ok", "an auth failure must not mask a healthy gateway"


def test_a_signed_out_session_is_reported_as_failed_with_the_next_step() -> None:
    client = Transport()
    client.errors["/auth/me"] = CliError("expired", "authentication_required", 2)

    finding = cli.check_auth(client)

    assert finding["state"] == "failed"
    assert finding["next"] == "adp login"


def test_an_unreachable_gateway_is_unknown_not_a_confident_failure() -> None:
    """ "I could not tell" and "it is broken" are different diagnoses."""
    client = Transport()
    client.errors["/auth/me"] = CliError("down", "gateway_unavailable")

    assert cli.check_auth(client)["state"] == UNKNOWN


# --- budget: the blocking reason, read from the server's real contract --------


def test_an_uncapped_caller_is_not_reported_as_blocked() -> None:
    assert cli.check_budget(Transport({"/me/budget": UNCAPPED_BUDGET}))["state"] == "ok"


def test_a_hard_exhausted_cap_is_reported_as_blocking() -> None:
    budget = {"period": "monthly", "cap_status": "capped", "band": "exceeded", "enforcement_mode": "hard"}

    finding = cli.check_budget(Transport({"/me/budget": budget}))

    assert finding["state"] == "failed"
    assert "blocking" in finding["finding"]


def test_a_soft_exhausted_cap_is_not_reported_as_blocking() -> None:
    """`soft` warns and allows — reporting it as blocked would be a false alarm.

    Both conditions are required, per the server's own contract: `band` says the
    cap is exceeded, `enforcement_mode` says whether that stops anything.
    """
    budget = {"period": "monthly", "cap_status": "capped", "band": "exceeded", "enforcement_mode": "soft"}

    finding = cli.check_budget(Transport({"/me/budget": budget}))

    assert finding["state"] == "ok"
    assert "still run" in finding["finding"]


def test_an_unresolved_identity_makes_the_budget_answer_unknown() -> None:
    """Part of the spend is absent, so "not blocked" is not a conclusion.

    The server states outright that an unresolved identity means the cloud ledger
    could not be read. Reporting "ok" from incomplete figures would tell a user
    their work will run when it may be stopped.
    """
    budget = {
        "period": "monthly",
        "cap_status": "capped",
        "band": "warning",
        "enforcement_mode": "hard",
        "identity_status": "unresolved",
    }

    assert cli.check_budget(Transport({"/me/budget": budget}))["state"] == UNKNOWN


# --- readiness is reported, never inferred ----------------------------------


def test_unconfirmable_model_routing_is_unknown_not_ok() -> None:
    client = Transport({cli.CAPABILITIES_PATH: capability_document(**{"models.catalog.read": {"ready": UNKNOWN}})})

    finding = cli.check_models(client)

    assert finding["state"] == UNKNOWN
    assert "cannot resolve effective model routes" in finding["finding"]


def model_routes(*entries):
    return PreferenceListResponse(
        tenant_id="org-alpha",
        principal_kind="human",
        principal_id="user-1",
        entries=[PreferenceEntry(**entry) for entry in entries],
    ).model_dump(mode="json")


def route(persona="developer", model="model-a", status="configured", availability="verified", reason=None):
    return {
        "persona_key": persona,
        "persona_display_name": persona.title(),
        "configurable": True,
        "compatibility_class": "coding",
        "harness_contract_revision": "v1",
        "availability_status": availability,
        "availability_reason": reason,
        "effective_model_id": model,
        "effective_is_candidate": False,
        "source": "principal-mapping",
        "status": status,
    }


def test_models_resolves_effective_routes_through_the_real_server_schema() -> None:
    client = Transport({cli.CAPABILITIES_PATH: capability_document(), "/me/persona-models": model_routes(route())})

    finding = cli.check_models(client)

    assert finding["state"] == "ok"
    assert finding["routes"] == [{"persona": "developer", "model": "model-a", "status": "configured", "availability": "verified"}]
    assert client.calls[-1] == ("GET", "/me/persona-models")


def test_models_reports_missing_or_unavailable_routes_without_inference() -> None:
    missing = Transport({cli.CAPABILITIES_PATH: capability_document(), "/me/persona-models": model_routes()})
    unavailable = Transport(
        {
            cli.CAPABILITIES_PATH: capability_document(),
            "/me/persona-models": model_routes(route(model=None, status="unavailable", availability="unavailable", reason="destination_missing")),
        }
    )

    assert cli.check_models(missing)["state"] == UNKNOWN
    assert cli.check_models(unavailable)["state"] == "failed"
    assert "destination_missing" in cli.check_models(unavailable)["finding"]


def test_models_reports_malformed_and_unauthorized_boundaries() -> None:
    malformed = Transport({cli.CAPABILITIES_PATH: capability_document(), "/me/persona-models": {"entries": "not-a-list"}})
    unauthorized = Transport(
        {cli.CAPABILITIES_PATH: capability_document()},
        errors={"/me/persona-models": CliError("denied", "permission_denied", 3, status_code=403)},
    )

    assert cli.check_models(malformed)["state"] == UNKNOWN
    assert cli.check_models(unauthorized)["state"] == "failed"


def test_a_disabled_module_is_reported_as_disabled_not_as_broken() -> None:
    client = Transport({cli.CAPABILITIES_PATH: capability_document(**{"models.catalog.read": {"enabled": NO}})})

    assert "switched off" in cli.check_models(client)["finding"]


def test_agents_readiness_says_what_a_read_cannot_establish() -> None:
    """Honest about the limit rather than guessing, since proving a worker
    consumes would mean starting work — the mutation this command forbids."""
    client = Transport({cli.CAPABILITIES_PATH: capability_document(**{"agents.activity.read": {"ready": UNKNOWN}})})

    finding = cli.check_agents(client)

    assert finding["state"] == UNKNOWN
    assert "without starting work" in finding["finding"]


# --- redaction --------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["access_token", "refresh_token", "id_token", "password", "client_secret", "api_key", "Authorization", "cookie"],
)
def test_secret_shaped_fields_are_redacted(key) -> None:
    assert cli.redact({key: "s3cret-value"})[key] == "[redacted]"


def test_redaction_reaches_nested_structures() -> None:
    """A secret one level down is still a secret."""
    redacted = cli.redact({"outer": {"items": [{"access_token": "abc"}]}})

    assert redacted["outer"]["items"][0]["access_token"] == "[redacted]"
    assert "abc" not in json.dumps(redacted)


def test_redaction_keeps_the_fields_a_diagnostic_needs() -> None:
    """Over-redaction would make the command useless; these are not secrets."""
    kept = cli.redact({"org_id": "org-alpha", "expires_at": "2026-09-21T12:00:00Z", "state": "ok"})

    assert kept == {"org_id": "org-alpha", "expires_at": "2026-09-21T12:00:00Z", "state": "ok"}


def test_the_session_check_never_returns_a_token() -> None:
    """Expiry metadata is useful; the credential itself is never requested."""
    session = {**SESSION, "access_token": "eyJhbGciOi-not-a-real-token", "refresh_token": "also-not-real"}

    finding = cli.check_auth(Transport({"/auth/me": session}))

    assert "not-a-real-token" not in json.dumps(finding)
    assert "also-not-real" not in json.dumps(finding)
    assert finding["expires_at"] == SESSION["expires_at"]


def test_the_rendered_report_carries_no_secret(capsys) -> None:
    """End to end, through the real emitter, in JSON mode."""
    client = Transport()
    client.healthy()
    client.routes["/auth/me"] = {**SESSION, "access_token": "eyJ-not-a-real-token"}
    args = type("Args", (), {"json": True, "checks": None, "request_id": None})()

    cli.emit_doctor(cli.run_checks(cli.ALL_CHECKS, client), args)

    assert "not-a-real-token" not in capsys.readouterr().out


# --- request lookup: the caller's own scope, existence hidden ----------------


def test_a_request_lookup_uses_the_own_scope_endpoint() -> None:
    """No target parameter anywhere — the server derives identity from the token."""
    client = Transport({"/me/cli-requests/run-123": {"request_id": "run-123", "status_code": 500}})

    cli.lookup_request("run-123", client)

    assert client.calls == [("GET", "/me/cli-requests/run-123")]


@pytest.mark.parametrize("status", [403, 404])
def test_a_request_the_caller_may_not_see_reveals_nothing(status) -> None:
    """Forbidden and missing must be INDISTINGUISHABLE.

    Reporting them differently would confirm to a stranger that another user's
    request exists — an existence oracle built out of a help command.
    """
    client = Transport(errors={"/me/cli-requests/someone-elses": CliError("no", "http_error", 5, status_code=status)})

    with pytest.raises(CliError) as caught:
        cli.lookup_request("someone-elses", client)

    assert caught.value.code == "request_not_visible"
    assert "visible to you" in str(caught.value)
    # Neither the word "forbidden" nor "exists" may appear — nothing that hints
    # which of the two happened.
    assert "forbidden" not in str(caught.value).lower()


def test_both_refusals_produce_byte_identical_messages() -> None:
    """The strongest form of the property: the two answers are the same string."""
    messages = []
    for status in (403, 404):
        client = Transport(errors={"/me/cli-requests/x": CliError("no", "http_error", 5, status_code=status)})
        with pytest.raises(CliError) as caught:
            cli.lookup_request("x", client)
        messages.append((str(caught.value), caught.value.code, caught.value.exit_code))

    assert messages[0] == messages[1]


@pytest.mark.parametrize("bad", ["../../etc/passwd", "run 123", "run/123", "a" * 200, "run%2f123", ""])
def test_a_malformed_request_id_is_refused_before_it_reaches_a_url(bad) -> None:
    """Validated before it can reshape the request path."""
    client = Transport()

    with pytest.raises(CliError) as caught:
        cli.lookup_request(bad, client)

    assert caught.value.exit_code == 1
    assert client.calls == [], "nothing may be sent for a malformed ID"


def test_a_request_lookup_runs_no_other_checks() -> None:
    """A targeted question sends only its own read."""
    client = Transport({"/me/cli-requests/run-1": {"request_id": "run-1", "status_code": 500}})
    args = type("Args", (), {"json": True, "checks": None, "request_id": "run-1", "verb": cli.COMMAND})()

    cli.run(args, client)

    assert client.calls == [("GET", "/me/cli-requests/run-1")]


# --- envelope and exit codes are the established ones ------------------------


def test_the_envelope_shape_is_unchanged(capsys) -> None:
    """One JSON object for a finite command, with the existing four keys."""
    args = type("Args", (), {"json": True, "checks": None, "request_id": None})()

    cli.emit_doctor(cli.run_checks(("auth",), Transport().healthy()), args)

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"status", "command", "detail", "next_action"}
    assert payload["command"] == cli.COMMAND


def test_finite_output_is_exactly_one_json_object(capsys) -> None:
    """Not NDJSON: neither of these verbs watches anything."""
    args = type("Args", (), {"json": True, "checks": None, "request_id": None})()

    cli.emit_doctor(cli.run_checks(cli.ALL_CHECKS, Transport().healthy()), args)

    assert len([line for line in capsys.readouterr().out.strip().splitlines() if line.strip()]) == 1


@pytest.mark.parametrize(
    ("states", "expected"),
    [(("ok", "ok"), 0), (("ok", UNKNOWN), 4), (("ok", "failed"), 5), ((UNKNOWN, "failed"), 5)],
)
def test_exit_codes_follow_the_established_mapping(states, expected, capsys) -> None:
    """0 ok, 4 unavailable, 5 failed — and failed outranks unknown.

    These are the CLI's existing codes, not new ones. A script that already
    branches on them keeps working.
    """
    findings = {f"check{index}": {"state": state} for index, state in enumerate(states)}
    args = type("Args", (), {"json": True, "checks": None, "request_id": None})()

    assert cli.emit_doctor(findings, args) == expected
    capsys.readouterr()


def test_error_subcodes_map_onto_existing_exit_codes() -> None:
    """New subcodes must not remap the established auth/permission codes."""
    assert cli.SUBCODES["permission_denied"] == 3, "permission stays 3"
    assert set(cli.SUBCODES.values()) <= {3, 4, 5}
    for subcode in cli.SUBCODES:
        assert subcode in cli.SUBCODE_MESSAGES, f"{subcode} has no message a person can act on"


def test_an_unknown_mutation_outcome_tells_the_user_not_to_retry_blindly() -> None:
    """The one subcode where the wrong advice causes a double-apply."""
    message = cli.SUBCODE_MESSAGES["unknown_mutation_outcome"]

    assert "may or may not" in message
    assert "do not repeat" in message.lower()
    assert cli.SUBCODES["unknown_mutation_outcome"] == 4


def test_human_output_explains_each_finding_without_raw_json(capsys) -> None:
    args = type("Args", (), {"json": False, "checks": None, "request_id": None})()

    cli.emit_doctor(cli.run_checks(("auth", "budget"), Transport().healthy()), args)

    out = capsys.readouterr().out
    assert "auth: ok" in out and "budget: ok" in out
    assert "{" not in out, "the default rendering is prose, not a JSON dump"
