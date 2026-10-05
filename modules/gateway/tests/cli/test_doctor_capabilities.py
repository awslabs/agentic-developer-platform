"""`adp capabilities` must tell the user WHICH side the problem is on — Issue #5621.

CLI-08-AC-01. The acceptance criterion is not "capabilities are listed"; it is
that five situations stay distinguishable all the way to the user's screen:

* the deployment does not offer the operation (their CLI or gateway is too old)
* it offers it but the module is switched off
* it is on, but this caller is not permitted
* everything is permitted but a dependency is not ready
* it could not be determined

Collapsing any two produces a confidently wrong diagnosis — telling an authorized
administrator they lack a permission, or telling somebody a feature is broken when
it is merely disabled. Each test below pins one of those confusions shut.

The second half covers the rule with real consequences: a definitively unavailable
mutation is refused BEFORE the request is sent, while anything unproven is sent
anyway and left to the server. Both halves of that are load-bearing. Refusing on
unproven evidence would block legitimate work during an outage; sending on
definitive evidence would attempt a write the user's own tooling knew was doomed.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "cli/adp-doctor.py"
SPEC = importlib.util.spec_from_file_location("adp_doctor", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)

# The helper's own CliError class identity. Re-importing adp_common would give a
# DIFFERENT class object, and pytest.raises would then miss the real exception.
common = cli.common
CliError = common.CliError

YES, NO, UNKNOWN = cli.YES, cli.NO, cli.UNKNOWN


def operation(operation_id="flows.approve.write", *, supported=YES, enabled=YES, permitted=YES, ready=YES, mutates=True):
    return {
        "id": operation_id,
        "supported": supported,
        "enabled": enabled,
        "permitted": permitted,
        "ready": ready,
        "mutates": mutates,
        "summary": "Approve a decision the engine is waiting on",
    }


def document(*operations, schema_version=None, release=""):
    return {
        "schema_version": schema_version or cli.SUPPORTED_SCHEMA_VERSIONS[0],
        "gateway": {"state": YES if release else UNKNOWN, "release": release, "source": "GATEWAY_RELEASE" if release else ""},
        "tenant": {"org_id": "org-alpha"},
        "operations": list(operations) or [operation()],
    }


class Recorder:
    """A fake transport that records every path it was asked for.

    The recording is the point: several tests below assert that NOTHING was sent
    beyond discovery, and that can only be proven by inspecting the call log.
    """

    def __init__(self, payload=None, error=None):
        self.payload, self.error = payload, error
        self.paths: list[str] = []

    def __call__(self, method, path, body=None, **kwargs):
        self.paths.append(path)
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """A private HOME with a configured gateway, so no cache crosses tests."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config_dir = tmp_path / ".bedrock-gateway"
    config_dir.mkdir(mode=0o700, exist_ok=True)
    config = config_dir / "config.json"
    config.write_text(json.dumps({"gateway_url": "https://gw.example.com"}))
    config.chmod(0o600)
    # adp_common caches the resolved deployment at module level; clear it so each
    # test resolves against its own HOME rather than the first test's.
    monkeypatch.setattr(common, "_deployment", common._UNRESOLVED, raising=False)
    monkeypatch.setattr(
        common, "authenticated_scope", lambda token=None: {"identity": "identity-a", "tenant": "org-alpha", "tenant_claim": "org-alpha"}
    )


# --- the five states stay distinguishable ------------------------------------


def test_an_operation_the_server_never_mentions_is_unsupported() -> None:
    """Absence is the answer, and it is the ONLY one an old server can give.

    A gateway that predates an operation cannot describe it — it has never heard
    of the name. So silence must read as "not available here" rather than as an
    error or, far worse, as available.
    """
    assert cli.blocking_reason(cli.find(document(), "some.future.operation")) == "unsupported_operation"


def test_a_disabled_module_is_reported_as_disabled_not_as_a_denial() -> None:
    reason = cli.blocking_reason(operation(enabled=NO))
    assert reason == "feature_disabled"
    assert "administrator to enable" in cli.SUBCODE_MESSAGES[reason]


def test_a_denial_is_reported_as_a_denial_not_as_disabled() -> None:
    assert cli.blocking_reason(operation(permitted=NO)) == "permission_denied"


def test_an_unready_dependency_is_neither_disabled_nor_denied() -> None:
    reason = cli.blocking_reason(operation(ready=NO))
    assert reason == "dependency_pending"
    assert cli.SUBCODES[reason] == 4, "a pending dependency is retryable, not a failure"


def test_unknown_on_any_axis_is_not_treated_as_a_block() -> None:
    """The collapse that silently denies people access.

    UNKNOWN means nobody established an answer. Rendering it as a block would
    refuse work the user is entitled to, citing evidence that does not exist.
    """
    for axis in ("enabled", "permitted", "ready"):
        assert cli.blocking_reason(operation(**{axis: UNKNOWN})) is None, f"{axis}=unknown must not block"


def test_rendering_states_unconfirmed_rather_than_claiming_available() -> None:
    """ "Available" is a promise; an unconfirmed axis cannot make it."""
    lines = cli.render_capabilities(document(operation(ready=UNKNOWN)))
    assert "unconfirmed" in lines[0]

    confident = cli.render_capabilities(document(operation()))
    assert "available" in confident[0] and "unconfirmed" not in confident[0]


@pytest.mark.parametrize(
    ("axis", "expected"),
    [("enabled", "switched off"), ("permitted", "not permitted"), ("ready", "not ready")],
)
def test_each_blocked_state_renders_its_own_distinct_wording(axis, expected) -> None:
    """Distinct causes must not share one message, or the CLI has told the user nothing."""
    assert expected in cli.render_capabilities(document(operation(**{axis: NO})))[0]


# --- the CLI refuses doomed mutations, but only on definitive evidence --------


def test_a_definitively_unavailable_mutation_is_refused_before_sending() -> None:
    """Nothing but discovery may reach the network."""
    client = Recorder(document(operation(enabled=NO)))

    with pytest.raises(CliError) as caught:
        cli.ensure_can_mutate("flows.approve.write", request=client)

    assert caught.value.code == "feature_disabled"
    assert "Nothing was sent" in str(caught.value)
    assert client.paths == [cli.CAPABILITIES_PATH], "no request beyond discovery may be sent"


def test_an_unknown_axis_does_not_block_the_mutation() -> None:
    """The server stays the authority; the CLI reports what it could not confirm."""
    evidence = cli.ensure_can_mutate("flows.approve.write", request=Recorder(document(operation(permitted=UNKNOWN))))

    assert evidence["checked"] is True
    assert evidence["unknown"] == ["permitted"]


def test_an_unreachable_gateway_does_not_block_the_mutation() -> None:
    """Absent discovery must not become a local veto.

    With no evidence at all the CLI proceeds and lets the server decide. Refusing
    here would turn a discovery outage into a total loss of function.
    """
    client = Recorder(error=CliError("down", "gateway_unavailable"))

    evidence = cli.ensure_can_mutate("flows.approve.write", request=client)

    assert evidence == {"checked": False, "reason": "", "source": "unavailable"}


def test_an_unknown_operation_is_refused_rather_than_sent_hopefully() -> None:
    """An operation this deployment never reported is definitive evidence.

    This is the "never fall back to an older unsafe mutation" rule: the CLI does
    not guess an older endpoint or send anyway — it reports the operation as
    unavailable and sends nothing.
    """
    client = Recorder(document(operation("something.else.read", mutates=False)))

    with pytest.raises(CliError) as caught:
        cli.ensure_can_mutate("flows.approve.write", request=client)

    assert caught.value.code == "unsupported_operation"
    assert client.paths == [cli.CAPABILITIES_PATH]


# --- version and schema handling ---------------------------------------------


def test_an_old_gateway_without_the_route_is_named_as_such() -> None:
    """A 404 on discovery is a fact about the server, reported as one.

    Exit 4, not 5: the CLI is fine and the user's next step is an upgrade, so
    this must not present as a local failure.
    """
    client = Recorder(error=CliError("not found", "http_error", 5, status_code=404))

    with pytest.raises(CliError) as caught:
        cli.fetch(request=client)

    assert caught.value.code == "server_too_old"
    assert caught.value.exit_code == 4


def test_an_unreadable_schema_version_is_refused_not_parsed_hopefully() -> None:
    """Reading an unknown contract is how a client invents an answer.

    A future server could rename or re-mean any axis. Parsing it anyway risks
    reporting "permitted" from a document that never said so, so the CLI stops
    and tells the user to update.
    """
    with pytest.raises(CliError) as caught:
        cli.fetch(request=Recorder(document(schema_version="1999-01-01")))

    assert caught.value.code == "schema_unsupported"


def test_malformed_tenant_shape_is_refused_before_it_can_cross_cache_scope() -> None:
    malformed = document()
    malformed["tenant"] = "org-alpha"

    with pytest.raises(CliError) as caught:
        cli.fetch(request=Recorder(malformed))

    assert caught.value.code == "invalid_response"


def test_discovery_uses_one_token_snapshot_for_cache_scope_and_transport(monkeypatch) -> None:
    token = "header.payload.signature"
    calls = []
    monkeypatch.setattr(common, "access_token", lambda: token)
    monkeypatch.setattr(
        common,
        "authenticated_scope",
        lambda supplied=None: {"identity": "identity-a", "tenant": "org-alpha", "tenant_claim": "org-alpha"}
        if supplied == token
        else pytest.fail("cache scope did not use the transport token"),
    )

    def recording(method, path, body=None, **kwargs):
        calls.append(kwargs.get("token"))
        return document()

    cli.fetch(refresh=True, request=recording)

    assert calls == [token]


def test_an_unrecognised_axis_value_is_refused_not_coerced() -> None:
    """Coercion in either direction is a wrong answer stated confidently."""
    malformed = document()
    malformed["operations"][0]["permitted"] = "probably"

    with pytest.raises(CliError) as caught:
        cli.fetch(request=Recorder(malformed))

    assert caught.value.code == "invalid_response"


def test_an_unestablished_release_is_reported_as_unknown() -> None:
    """Never an empty string a user could read as "old"."""
    document_without_release = document()
    rendered = json.dumps(cli.redact(document_without_release))

    assert document_without_release["gateway"]["state"] == UNKNOWN
    assert "release" in rendered


# --- the cache cannot serve another identity's answer ------------------------


def test_the_cache_is_used_on_a_second_read() -> None:
    client = Recorder(document())

    cli.fetch(request=client)
    _, source = cli.fetch(request=client)

    assert source == "cache"
    assert client.paths == [cli.CAPABILITIES_PATH], "the cached read must not re-request"


def test_refresh_bypasses_the_cache() -> None:
    client = Recorder(document())

    cli.fetch(request=client)
    _, source = cli.fetch(refresh=True, request=client)

    assert source == "gateway"
    assert len(client.paths) == 2


def test_a_cached_document_from_another_deployment_is_not_reused(monkeypatch) -> None:
    """The key covers deployment, gateway and identity — so none can cross.

    A shared machine that switches deployments (or users) must never be told
    about the permissions of the previous one.
    """
    client = Recorder(document())
    cli.fetch(request=client)

    other_key = {
        "deployment_id": "other",
        "deployment": "other",
        "gateway": "https://other.example.com/api",
        "identity": "identity-a",
        "tenant": "org-alpha",
    }
    scope = {"identity": "identity-a", "tenant": "org-alpha", "tenant_claim": "org-alpha"}
    monkeypatch.setattr(common, "capability_cache_context", lambda token=None: (other_key, scope))
    _, source = cli.fetch(request=client)

    assert source == "gateway", "a different deployment's cache entry must be ignored"


def test_same_deployment_relogin_never_reuses_another_identity_or_tenant(monkeypatch) -> None:
    client = Recorder(document())
    cli.fetch(request=client)

    monkeypatch.setattr(
        common, "authenticated_scope", lambda token=None: {"identity": "identity-b", "tenant": "org-beta", "tenant_claim": "org-beta"}
    )
    client.payload = {**document(), "tenant": {"org_id": "org-beta"}}
    second, source = cli.fetch(request=client)

    assert source == "gateway"
    assert second["tenant"]["org_id"] == "org-beta"
    assert client.paths == [cli.CAPABILITIES_PATH, cli.CAPABILITIES_PATH]


def test_a_stale_cache_entry_is_refetched(monkeypatch) -> None:
    """A capability answer must expire; permissions change without warning."""
    client = Recorder(document())
    cli.fetch(request=client)

    monkeypatch.setattr(cli.time, "time", lambda: 10**9 + cli.CACHE_TTL_SECONDS + 1)
    _, source = cli.fetch(request=client)

    assert source == "gateway"


def test_a_cache_entry_claiming_to_be_from_the_future_is_not_trusted(monkeypatch) -> None:
    """A moved clock must not extend a cached answer's life indefinitely."""
    client = Recorder(document())
    cli.fetch(request=client)

    monkeypatch.setattr(cli.time, "time", lambda: 0)
    _, source = cli.fetch(request=client)

    assert source == "gateway"


# --- the command reaches no mutating endpoint -------------------------------


def test_capabilities_only_ever_issues_a_get() -> None:
    """AC-01 requires discovery to change nothing. Proven from the call log."""
    client = Recorder(document())
    methods: list[str] = []

    def recording(method, path, body=None, **kwargs):
        methods.append(method)
        return client(method, path, body, **kwargs)

    cli.fetch(refresh=True, request=recording)

    assert methods == ["GET"]


def test_json_operation_filter_selects_only_the_requested_operation(capsys):
    args = cli.parser().parse_args(["capabilities", "--operation", "flows.approve.write", "--json"])
    cli.emit_capabilities(document(operation(), operation("other.read", mutates=False)), args)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert [row["id"] for row in json.loads(lines[0])["detail"]["operations"]] == ["flows.approve.write"]


def test_json_unknown_operation_is_refused(capsys):
    args = cli.parser().parse_args(["capabilities", "--operation", "missing", "--json"])
    with pytest.raises(CliError) as caught:
        cli.emit_capabilities(document(), args)
    assert caught.value.code == "unsupported_operation"
    assert capsys.readouterr().out == ""
