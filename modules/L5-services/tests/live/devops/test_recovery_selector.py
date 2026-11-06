"""Offline SCN-02 authorization, journal and fault-selector checks."""

import json
import os
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from recovery_selector import (
    ACTIONS,
    READBACKS,
    HTTPRecoveryTarget,
    authorize_recovery,
    qualify_recovery,
)
from test_recovery import FakeReader, FakeTarget, recovery_case
from test_selector import FakeReader as FakeReleaseReader
from test_selector import offline_policy

pytest_plugins = ("test_selector",)


@pytest.fixture
def recovery_selection(selector_case, monkeypatch):
    manifest, path, directory, prior_reader = selector_case
    manifest["authorization"]["scenarios"].append("recovery")
    manifest["recovery"] = recovery_case.__wrapped__()["recovery"]
    manifest["recovery"]["users"] = [
        {"id": "user-one", "credential_ref": "DEVOPS_USER_ONE_TOKEN"},
        {"id": "user-two", "credential_ref": "DEVOPS_USER_TWO_TOKEN"}]
    states = {}
    for index, name in enumerate(("DEVOPS_USER_ONE_TOKEN", "DEVOPS_USER_TWO_TOKEN")):
        monkeypatch.setenv(name, f"different-human-{index}")
        state = path.parent / f"private-user-{index}.json"
        state.write_text("{}")
        state.chmod(0o600)
        states[f"user-{('one', 'two')[index]}"] = str(state)
    manifest["recovery"]["browser"] = {
        "gateway_url": manifest["journey_driver"]["gateway_url"],
        "provider_hostname": manifest["journey_driver"]["provider_hostname"],
        "configuration_revision": 1, "storage_states": states}
    scope = manifest["recovery"]["scope"]
    binding = manifest["recovery"]["binding_id"]
    manifest["ingress_policy"]["launch_path"] = (
        f"/api/v1/products/{scope['product_id']}/projects/{scope['project_id']}"
        f"/services/devops/bindings/{binding}/browser-launch")
    for label, names in (("action", ACTIONS), ("readback", READBACKS)):
        manifest["recovery"][label + "_channels"] = {
            name: {"source": name, "url": f"https://{label}-{name.replace('_', '-')}.example.org/",
                   "token_env": f"DEVOPS_{label.upper()}_{name.upper()}_TOKEN"}
            for name in names}
        for name in names:
            monkeypatch.setenv(f"DEVOPS_{label.upper()}_{name.upper()}_TOKEN", f"private-{label}-{name}-token")
    path.write_text(json.dumps(manifest))
    return manifest, path, directory, prior_reader


class FakeAdapter:
    def __init__(self, manifest, directory, *, deadline, max_requests):
        self.driver = FakeTarget()
        self.reader = FakeReader(self.driver)
        self.closed = False

    def close(self):
        self.closed = True

    def launch(self, credential, scope, kind, attempt_ref):
        synthetic_credential = "SECRET_USER_ONE" if credential == "DEVOPS_USER_ONE_TOKEN" else "SECRET_USER_TWO"
        return self.driver.launch(synthetic_credential, scope, kind, attempt_ref)

    def __getattr__(self, name):
        target = self.driver if hasattr(self.driver, name) else self.reader
        return getattr(target, name)


def test_recovery_selector_requires_private_bound_authorization_and_retains_receipt(recovery_selection):
    manifest, path, directory, prior_reader = recovery_selection
    manifest["readback_channels"].pop("ingress")
    path.write_text(json.dumps(manifest))

    class NoHTTPIngress(FakeReleaseReader):
        def read_ingress(self, target):
            pytest.fail("recovery must use the selected AWS ingress policy")

    adapters = []
    def factory(fixture, evidence_dir, **kwargs):
        adapter = FakeAdapter(fixture, evidence_dir, **kwargs)
        adapters.append(adapter)
        return adapter
    result = qualify_recovery(manifest["release"]["source_sha"], path, directory,
                              target_factory=factory,
                              release_reader_factory=lambda fixture, **kwargs: NoHTTPIngress(
                                  fixture, prior_reader, **kwargs),
                              policy_reader=offline_policy,
                              now=datetime(2026, 10, 28, tzinfo=UTC))
    assert result == manifest["authorization"]["run_id"]
    assert adapters[0].closed
    evidence = json.loads((directory / f"devops-{result}-recovery.json").read_text())
    assert evidence["cleanup"]["owned_absent"] is True
    assert set(evidence["recovery"]) == {"worker", "event", "reporting"}


def test_recovery_selector_denied_policy_has_no_reader_or_action(recovery_selection):
    manifest, path, directory, _ = recovery_selection
    manifest["readback_channels"].pop("ingress")
    path.write_text(json.dumps(manifest))
    calls = []

    def reject_policy(fixture, **kwargs):
        calls.append("policy")
        return {"aws_account": fixture["ingress_policy"]["account_id"],
                "function_urls": ["https://public.example.org"],
                "public_launchers": [], "verified_functions": ["example-sso"]}

    with pytest.raises(ValueError, match="deployed ingress policy was not verified"):
        qualify_recovery(manifest["release"]["source_sha"], path, directory,
                         target_factory=lambda *args, **kwargs: pytest.fail("action constructed on denial"),
                         release_reader_factory=lambda *args, **kwargs: pytest.fail("reader constructed on denial"),
                         policy_reader=reject_policy, now=datetime(2026, 10, 28, tzinfo=UTC))
    assert calls == ["policy"]
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("mutate", [
    lambda manifest, monkeypatch: manifest["recovery"].update(authorized_faults=False),
    lambda manifest, monkeypatch: manifest["recovery"]["readback_channels"].pop("provider_calls"),
    lambda manifest, monkeypatch: monkeypatch.setenv("DEVOPS_READBACK_WORK_TOKEN", "private-action-revoke-token"),
    lambda manifest, monkeypatch: monkeypatch.delenv("DEVOPS_USER_ONE_TOKEN"),
    lambda manifest, monkeypatch: manifest["recovery"].update(foreign_scope=manifest["recovery"]["scope"]),
    lambda manifest, monkeypatch: manifest["recovery"]["browser"].update(gateway_url="https://foreign.example.org"),
    lambda manifest, monkeypatch: manifest["recovery"]["browser"].update(configuration_revision=0),
    lambda manifest, monkeypatch: manifest["recovery"]["browser"]["storage_states"].update(
        {"user-two": manifest["recovery"]["browser"]["storage_states"]["user-one"]}),
    lambda manifest, monkeypatch: manifest["ingress_policy"].update(launch_path="/api/v1/foreign/browser-launch"),
])
def test_recovery_selector_fails_before_faults_without_isolated_authority(recovery_selection, monkeypatch, mutate):
    manifest, _path, directory, _prior_reader = recovery_selection
    mutate(manifest, monkeypatch)
    with pytest.raises(ValueError):
        authorize_recovery(manifest, manifest["release"]["source_sha"], directory,
                           now=datetime(2026, 10, 28, tzinfo=UTC))
    assert list(directory.iterdir()) == []


def test_recovery_launch_uses_first_party_browser_without_returning_ticket(recovery_selection, monkeypatch):
    manifest, _path, directory, _ = recovery_selection
    calls = []

    def browser(command, **options):
        payload = json.loads(options["input"])
        calls.append((command, payload))
        status = 200 if payload["kind"] == "allowed" else 401
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": status}).encode())

    monkeypatch.setattr("recovery_selector.subprocess.run", browser)
    adapter = HTTPRecoveryTarget(manifest, directory, deadline=time.monotonic() + 15, max_requests=60)
    try:
        scope = manifest["recovery"]["scope"]
        assert adapter.launch("DEVOPS_USER_ONE_TOKEN", scope, "allowed", "attempt-allowed") == {
            "status": 200, "scope": scope}
        assert adapter.launch("DEVOPS_USER_ONE_TOKEN", scope, "missing", "attempt-missing") == {"status": 401}
        assert adapter.launch("DEVOPS_USER_ONE_TOKEN", scope, "invalid", "attempt-invalid") == {"status": 401}
        assert all(command[0] == "node" and command[1].endswith("browser_recovery.mjs")
                   for command, _ in calls)
        assert calls[0][1]["token"] is None
        assert calls[1][1]["token"] is None
        assert calls[0][1]["storage_state"] is not None
        assert calls[1][1]["storage_state"] is None
        assert calls[2][1]["storage_state"] is None
        assert calls[2][1]["token"] == "invalid"
        assert calls[0][1]["gateway_url"] == manifest["journey_driver"]["gateway_url"]
        journal = (directory / f"devops-{manifest['authorization']['run_id']}-recovery.jsonl").read_text()
        assert len(journal.splitlines()) == 6
        assert "different-human" not in journal and "SigninToken" not in journal
        with pytest.raises(ValueError, match="selected recovery scope"):
            adapter.launch("DEVOPS_USER_ONE_TOKEN", manifest["recovery"]["foreign_scope"],
                           "allowed", "attempt-foreign")
        assert len(calls) == 3
    finally:
        adapter.close()
    with pytest.raises(FileExistsError):
        HTTPRecoveryTarget(manifest, directory, deadline=time.monotonic() + 15, max_requests=60)


@pytest.mark.parametrize("kind,status", [("foreign", 404), ("throttled", 429), ("revoked", 403), ("logged_out", 401)])
@pytest.mark.parametrize("user_index", [0, 1])
def test_recovery_denials_present_selected_user_token(recovery_selection, monkeypatch, kind, status, user_index):
    manifest, _path, directory, _ = recovery_selection
    user = manifest["recovery"]["users"][user_index]
    token = os.environ[user["credential_ref"]]

    def browser(command, **options):
        payload = json.loads(options["input"])
        assert payload["token"] == token
        assert payload["storage_state"] == manifest["recovery"]["browser"]["storage_states"][user["id"]]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": status}).encode())

    monkeypatch.setattr("recovery_selector.subprocess.run", browser)
    adapter = HTTPRecoveryTarget(manifest, directory, deadline=time.monotonic() + 15, max_requests=60)
    scope = manifest["recovery"]["foreign_scope" if kind == "foreign" else "scope"]
    try:
        assert adapter.launch(user["credential_ref"], scope, kind, "authenticated-denial") == {"status": status}
        journal = (directory / f"devops-{manifest['authorization']['run_id']}-recovery.jsonl").read_text()
        assert token not in journal
    finally:
        adapter.close()


def test_recovery_browser_failure_retains_uncertain_journal_without_ticket(recovery_selection, monkeypatch):
    manifest, _path, directory, _ = recovery_selection
    monkeypatch.setattr("recovery_selector.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout=b"", stderr=b"private-provider-url"))
    adapter = HTTPRecoveryTarget(manifest, directory, deadline=time.monotonic() + 15, max_requests=60)
    try:
        with pytest.raises(ValueError, match="reconcile privately"):
            adapter.launch("DEVOPS_USER_ONE_TOKEN", manifest["recovery"]["scope"], "allowed", "attempt-allowed")
        journal = (directory / f"devops-{manifest['authorization']['run_id']}-recovery.jsonl").read_text()
        assert '"phase": "uncertain"' in journal
        assert "private-provider-url" not in journal
    finally:
        adapter.close()


@pytest.mark.parametrize("response", [
    {"status": 200, "launch_url": "https://signin.aws.amazon.com/federation?SigninToken=private"},
    {"status": 302},
    {"status": "200"},
])
def test_recovery_browser_never_accepts_extra_or_unbounded_output(recovery_selection, monkeypatch, response):
    manifest, _, directory, _ = recovery_selection
    monkeypatch.setattr("recovery_selector.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(response).encode()))
    adapter = HTTPRecoveryTarget(manifest, directory, deadline=time.monotonic() + 15, max_requests=60)
    try:
        with pytest.raises(ValueError, match="reconcile privately"):
            adapter.launch("DEVOPS_USER_ONE_TOKEN", manifest["recovery"]["scope"], "allowed", "attempt-allowed")
        journal = (directory / f"devops-{manifest['authorization']['run_id']}-recovery.jsonl").read_text()
        assert '"phase": "uncertain"' in journal
        assert "SigninToken" not in journal
    finally:
        adapter.close()


def test_uncertain_submission_is_journaled_without_retry(recovery_selection):
    import httpx
    manifest, _path, directory, _prior_reader = recovery_selection
    adapter = HTTPRecoveryTarget(manifest, directory, deadline=time.monotonic() + 15, max_requests=60)
    adapter.client.close()
    adapter.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("ack uncertain"))), follow_redirects=False)
    try:
        with pytest.raises(TimeoutError):
            adapter.submit(manifest["recovery"]["scope"], "worker", manifest["recovery"]["receipts"]["worker"])
    finally:
        adapter.close()
    entries = [json.loads(line) for line in (directory / f"devops-{manifest['authorization']['run_id']}-recovery.jsonl").read_text().splitlines()]
    assert [entry["phase"] for entry in entries] == ["prepared", "uncertain"]
    assert entries[0]["receipt"] == entries[1]["receipt"]


def test_cleanup_has_bounded_grace_after_operation_deadline(recovery_selection):
    import httpx
    manifest, _path, directory, _prior_reader = recovery_selection
    adapter = HTTPRecoveryTarget(manifest, directory, deadline=time.monotonic() - 1, max_requests=50)
    adapter.client.close()
    adapter.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(204)), follow_redirects=False)
    try:
        with pytest.raises(ValueError, match="deadline"):
            adapter.submit(manifest["recovery"]["scope"], "worker", manifest["recovery"]["receipts"]["worker"])
        adapter.cleanup_routes(manifest["recovery"]["scope"], manifest["recovery"]["owned_routes"])
        assert adapter.cleanup_phase
    finally:
        adapter.close()
    entries = (directory / f"devops-{manifest['authorization']['run_id']}-recovery.jsonl").read_text().splitlines()
    assert [json.loads(line)["phase"] for line in entries] == ["prepared", "acknowledged"]


def test_recovery_logout_requires_no_custom_channel_or_credential(recovery_selection, monkeypatch):
    manifest, _path, directory, _ = recovery_selection
    manifest["recovery"]["action_channels"].pop("logout", None)
    monkeypatch.delenv("DEVOPS_ACTION_LOGOUT_TOKEN", raising=False)
    assert authorize_recovery(manifest, manifest["release"]["source_sha"], directory,
                              now=datetime(2026, 10, 28, tzinfo=UTC)) == directory


@pytest.mark.parametrize("user_index", [0, 1])
@pytest.mark.parametrize("trailing_slash", [False, True])
def test_recovery_logout_uses_frontend_api_route_and_presented_user_token(
        recovery_selection, user_index, trailing_slash):
    import httpx
    manifest, _path, directory, _ = recovery_selection
    origin = manifest["recovery"]["browser"]["gateway_url"].rstrip("/")
    manifest["recovery"]["browser"]["gateway_url"] = origin + ("/" if trailing_slash else "")
    user = manifest["recovery"]["users"][user_index]
    token = os.environ[user["credential_ref"]]
    calls = []
    gateway_calls = []

    def gateway(request):
        gateway_calls.append(request)
        assert request.url.path == "/auth/logout"
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer " + token
        assert request.content == b""
        return httpx.Response(200, json={"message": "Logout successful"})

    def frontend(request):
        calls.append(request)
        if not request.url.path.startswith("/api/"):
            return httpx.Response(403 if request.method not in {"GET", "HEAD", "OPTIONS"} else 200)
        return gateway(httpx.Request(request.method,
                                     request.url.copy_with(path=request.url.path.removeprefix("/api")),
                                     headers=request.headers, content=request.content))

    adapter = HTTPRecoveryTarget(manifest, directory, deadline=time.monotonic() + 15, max_requests=60)
    adapter.client.close()
    adapter.client = httpx.Client(transport=httpx.MockTransport(frontend), follow_redirects=False)
    try:
        adapter.logout(user["credential_ref"])
        assert adapter.calls == len(calls) == len(gateway_calls) == 1
        assert str(calls[0].url) == origin + "/api/auth/logout"
        assert os.environ[user["credential_ref"]] == token
    finally:
        adapter.close()
    journal = (directory / f"devops-{manifest['authorization']['run_id']}-recovery.jsonl").read_text()
    assert [json.loads(line)["phase"] for line in journal.splitlines()] == ["prepared", "acknowledged"]
    assert token not in journal


@pytest.mark.parametrize("failure", ["redirect", "unauthorized", "unavailable", "malformed",
                                    "wrong-contract", "oversized", "timeout", "connection"])
def test_recovery_logout_failure_stops_without_retry_and_retains_uncertainty(recovery_selection, failure):
    import httpx
    manifest, _path, directory, _ = recovery_selection
    calls = []

    def gateway(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private detail")
        if failure == "connection":
            raise httpx.ConnectError("private detail")
        return {
            "redirect": httpx.Response(302, headers={"Location": "https://foreign.example.org/private"}),
            "unauthorized": httpx.Response(401, json={"detail": "unauthorized"}),
            "unavailable": httpx.Response(503, json={"detail": "session_check_unavailable"}),
            "malformed": httpx.Response(200, content=b"private detail"),
            "wrong-contract": httpx.Response(204),
            "oversized": httpx.Response(200, content=b"x" * (1024 * 1024 + 1)),
        }[failure]

    adapter = HTTPRecoveryTarget(manifest, directory, deadline=time.monotonic() + 15, max_requests=60)
    adapter.client.close()
    adapter.client = httpx.Client(transport=httpx.MockTransport(gateway), follow_redirects=False)
    try:
        with pytest.raises((ValueError, TimeoutError), match="reconcile private"):
            adapter.logout("DEVOPS_USER_ONE_TOKEN")
        assert len(calls) == 1
    finally:
        adapter.close()
    journal = (directory / f"devops-{manifest['authorization']['run_id']}-recovery.jsonl").read_text()
    assert [json.loads(line)["phase"] for line in journal.splitlines()] == ["prepared", "uncertain"]
    assert "private detail" not in journal
    assert "foreign.example.org" not in journal


@pytest.mark.parametrize("failure", ["foreign-user", "deadline", "cleanup-reserve"])
def test_recovery_logout_denies_unselected_user_or_exhausted_bound_before_effect(recovery_selection, failure):
    import httpx
    manifest, _path, directory, _ = recovery_selection
    adapter = HTTPRecoveryTarget(manifest, directory,
                                 deadline=time.monotonic() + (-1 if failure == "deadline" else 15),
                                 max_requests=60)
    if failure == "cleanup-reserve":
        adapter.calls = 55
    adapter.client.close()
    adapter.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: pytest.fail("denied logout reached Gateway")), follow_redirects=False)
    try:
        with pytest.raises(ValueError):
            adapter.logout("DEVOPS_ACTION_REVOKE_TOKEN" if failure == "foreign-user" else "DEVOPS_USER_ONE_TOKEN")
    finally:
        adapter.close()
    assert (directory / f"devops-{manifest['authorization']['run_id']}-recovery.jsonl").read_text() == ""
