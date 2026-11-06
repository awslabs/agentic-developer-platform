"""Controlled gateway API orchestration; no fixture stands in for live consent."""

import json
import hashlib
from pathlib import Path

import httpx
import pytest

from tests.e2e.tools.atlassian_setup_live import CONNECTION, GatewaySetupDriver, load_setup
from tests.e2e.tools.atlassian_setup_observer import IndependentSetupObserver


def private(path, content):
    path = Path(path)
    path.write_text(content)
    path.chmod(0o600)
    return str(path)


def configuration(tmp_path):
    directory = tmp_path / "setup"
    directory.mkdir(mode=0o700)
    config = {
        "version": 1,
        "gateway_url": "https://gateway.example.test",
        "run_id": "run-one",
        "owner_id": "person-one",
        "fixture_kind": "fresh",
        "policy_required": False,
        "ledger_file": str(directory / "ledger.json"),
        "admin_token_file": private(directory / "admin.token", "admin-session"),
        "owner_token_file": private(directory / "owner.token", "owner-session"),
        "observer_token_file": private(
            directory / "observer.token", "observer-session"
        ),
        "consent_file": str(directory / "consent.json"),
        "authorization_file": str(directory / "authorization.json"),
        "attempt_file": str(directory / "attempt.json"),
        "site_id": "site-one",
        "account": "123456789012",
        "region": "us-east-1",
        "core_label": "test-core",
        "core_role_arn": "arn:aws:iam::123456789012:role/test-core",
        "adapter_label": "test-adapter",
        "adapter_role_arn": "arn:aws:iam::123456789012:role/test-adapter",
        "plan_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/example/connections/atlassian-mcp/config-aabbcc",
        "client_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/example/connections/atlassian-mcp/client-aabbcc",
        "provider_name": "adp-tools-example-atlassian-mcp-user",
        "workload_name": "adp-tools-example-atlassian-mcp-oauth-user",
        "metadata_sha256": "a" * 64,
        "subject_schema_sha256": "b" * 64,
        "sites_schema_sha256": "c" * 64,
        "expected_subject": "provider-user-one",
    }
    path = private(directory / "config.json", json.dumps(config))
    return path, config


def call_setup(driver):
    driver.setup(
        run_id="run-one",
        owner_id="person-one",
        fixture_kind="fresh",
        policy_required=False,
        timeout_seconds=120,
    )


def test_private_manifest_cannot_be_code_or_change_target(tmp_path):
    path, config = configuration(tmp_path)
    fixture, loaded = load_setup(path, gateway_url=config["gateway_url"])
    assert fixture["ledger"].identity["owner_id"] == "person-one"
    assert loaded == config
    with pytest.raises(ValueError):
        load_setup(path, gateway_url="https://different.example.test")
    private(
        path, json.dumps({**config, "driver_module": "tests.e2e.tools.private_module"})
    )
    with pytest.raises(ValueError):
        load_setup(path, gateway_url=config["gateway_url"])
    private(path, json.dumps(config))
    (tmp_path / "setup" / "admin.token").chmod(0o644)
    with pytest.raises(ValueError):
        load_setup(path, gateway_url=config["gateway_url"])


def test_protected_role_paths_cannot_alias_an_assumed_role(tmp_path):
    path, config = configuration(tmp_path)
    config["adapter_role_arn"] = (
        "arn:aws:iam::123456789012:role/unrelated-path/test-adapter"
    )
    private(path, json.dumps(config))
    with pytest.raises(ValueError, match="Selected AWS connections"):
        load_setup(path, gateway_url=config["gateway_url"])


def test_shared_runner_uses_only_protected_data_not_private_python(
    tmp_path, monkeypatch
):
    from tests.e2e.tools.runner import private_atlassian_setup_bindings

    path, config = configuration(tmp_path)
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_SETUP_FILE", path)
    monkeypatch.setenv(
        "ADP_TOOLS_ATLASSIAN_SETUP_MODULE",
        "tests.e2e.tools.private_atlassian_setup_fake",
    )
    fixture_factory, driver_factory, observer = private_atlassian_setup_bindings(
        config["gateway_url"]
    )
    assert fixture_factory()["ledger"].identity["owner_id"] == config["owner_id"]
    assert isinstance(driver_factory(None), GatewaySetupDriver)
    assert isinstance(observer, IndependentSetupObserver)
    assert private_atlassian_setup_bindings("https://foreign.example.test") == (
        None,
        None,
        None,
    )
    monkeypatch.delenv("ADP_TOOLS_ATLASSIAN_SETUP_FILE")
    assert private_atlassian_setup_bindings(config["gateway_url"]) == (None, None, None)


def test_real_gateway_routes_require_ready_status_and_isolate_existing_grants(
    tmp_path, monkeypatch
):
    path, config = configuration(tmp_path)
    _, config = load_setup(path, gateway_url=config["gateway_url"])
    driver = GatewaySetupDriver(config)
    requests = []

    def unavailable(method, route, **kwargs):
        requests.append((method, route))
        return (
            {"status": "setup_required"}
            if method == "GET"
            else {"status": "setup_required"}
        )

    monkeypatch.setattr(driver, "_api", unavailable)
    with pytest.raises(ValueError, match="compatibility or tenant policy"):
        call_setup(driver)
    assert requests == [
        ("GET", "/api/auth/connections/atlassian-mcp/status"),
        ("POST", "/api/admin/connections/atlassian-mcp/prepare"),
        ("GET", "/api/auth/connections/atlassian-mcp/status"),
    ]
    requests.clear()
    monkeypatch.setattr(
        driver,
        "_api",
        lambda method, route, **kwargs: requests.append((method, route))
        or {
            "status": "ready",
            "connection": {"revision": "someone-else"},
        },
    )
    with pytest.raises(ValueError, match="Existing user grant"):
        call_setup(driver)
    assert len(requests) == 1


@pytest.mark.parametrize("pending_observation", [False, True])
def test_consent_completion_site_revision_and_owned_disconnect(tmp_path, monkeypatch, pending_observation):
    path, config = configuration(tmp_path)
    _, config = load_setup(path, gateway_url=config["gateway_url"])
    driver = GatewaySetupDriver(config)
    requests = []
    observations = 0
    monkeypatch.setattr("tests.e2e.tools.atlassian_setup_live.time.sleep", lambda _: None)

    def gateway(method, route, **kwargs):
        nonlocal observations
        requests.append((method, route, kwargs))
        if route.endswith("/status"):
            state = driver._attempt()
            return {
                "status": "consent_required",
                **(
                    {
                        "connection": {
                            "id": "connection-one",
                            "revision": state["revision"],
                            "selected_site_id": "site-one"
                            if state["phase"] == "selected"
                            else None,
                        }
                    }
                    if state and state["phase"] in {"completed", "selected"}
                    else {}
                ),
            }
        if route.endswith("/start"):
            return {
                "state": "nonce",
                "session_uri": "session",
                "authorization_url": "https://auth.example.test/consent",
            }
        if "/attempts/" in route:
            if pending_observation and observations < 2:
                return {"status": "observing"}
            return {"status": "completed", "id": "connection-one", "revision": "initial-revision"}
        if route.endswith("/complete"):
            observations += 1
            if observations == 1:
                raise ValueError("Adapter unavailable until configured")
            return {"id": "connection-one", "revision": "initial-revision"}
        if route.endswith("/select-site"):
            return {"revision": "selected-revision", "selected_site_id": "site-one"}
        return {"status": "disconnected"}

    monkeypatch.setattr(driver, "_api", gateway)
    call_setup(driver)
    assert (
        json.loads(Path(config["authorization_file"]).read_text())["state"] == "nonce"
    )
    assert driver.connection_revision == "selected-revision"
    assert requests[2] == ("GET", CONNECTION + "/attempts/" + hashlib.sha256(b"nonce").hexdigest(), {})
    assert not Path(config["consent_file"]).exists()
    assert next(arguments["body"] for _, route, arguments in requests if route.endswith("/select-site")) == {
        "connection_revision": "initial-revision",
        "site_id": "site-one",
    }
    driver.cleanup(run_id="run-one", owner_id="person-one", fixture_kind="fresh")
    assert requests[-1][2]["body"] == {"connection_revision": "selected-revision"}
    assert observations == (2 if pending_observation else 0)


def test_wrong_return_or_failed_auth_never_completes_consent(tmp_path, monkeypatch):
    path, config = configuration(tmp_path)
    _, config = load_setup(path, gateway_url=config["gateway_url"])
    private(
        config["consent_file"],
        json.dumps({"run_id": "another-run", "state": "nonce", "returned": True}),
    )
    driver = GatewaySetupDriver(config)
    requests = []

    def gateway(method, route, **kwargs):
        requests.append(route)
        if route.endswith("/status"):
            return {"status": "ready"}
        if method == "DELETE":
            return {"status": "disconnected"}
        return {
            "state": "nonce",
            "session_uri": "session",
            "authorization_url": "https://auth.example.test/consent",
        }

    monkeypatch.setattr(driver, "_api", gateway)
    with pytest.raises(ValueError, match="expired or changed"):
        call_setup(driver)
    assert not any(route.endswith("/complete") for route in requests)
    with pytest.raises(ValueError, match="cannot be blanket-disconnected"):
        driver.cleanup(run_id="run-one", owner_id="person-one", fixture_kind="fresh")
    assert not any(route == "/api/auth/connections/atlassian-mcp" for route in requests)

    class DeniedClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def request(self, *_args, **_kwargs):
            return httpx.Response(403, json={"detail": "private"})

    monkeypatch.setattr(httpx, "Client", lambda **_: DeniedClient())
    with pytest.raises(ValueError, match="refused"):
        GatewaySetupDriver(config)._api(
            "GET", "/api/auth/connections/atlassian-mcp/status"
        )


def test_delayed_browser_return_and_restart_reconcile_without_restarting_identity(
    tmp_path, monkeypatch
):
    path, config = configuration(tmp_path)
    _, config = load_setup(path, gateway_url=config["gateway_url"])
    first = GatewaySetupDriver(config)
    requests = []
    returned = False

    def gateway(method, route, **kwargs):
        requests.append((method, route))
        if route.endswith("/status"):
            state = first._attempt()
            return {
                "status": "ready",
                **(
                    {
                        "connection": {
                            "id": "connection-one",
                            "revision": state["revision"],
                            "selected_site_id": "site-one"
                            if state["phase"] == "selected"
                            else None,
                        }
                    }
                    if state and state["phase"] in {"completed", "selected"}
                    else {}
                ),
            }
        if route.endswith("/start"):
            return {
                "state": "late-nonce",
                "session_uri": "opaque-session",
                "authorization_url": "https://auth.example.test/consent",
            }
        if "/attempts/" in route:
            return {"status": "completed", "id": "connection-one", "revision": "initial-revision"} if returned else {"status": "pending"}
        if route.endswith("/select-site"):
            return {"revision": "selected-revision", "selected_site_id": "site-one"}
        return {"status": "disconnected"}

    monkeypatch.setattr(first, "_api", gateway)
    monkeypatch.setattr(
        "tests.e2e.tools.atlassian_setup_live.time.sleep",
        lambda _: (_ for _ in ()).throw(TimeoutError()),
    )
    with pytest.raises(TimeoutError):
        call_setup(first)
    state = json.loads(Path(config["attempt_file"]).read_text())
    assert state["phase"] == "pending" and state["session_uri"] == "opaque-session"
    assert (
        json.loads(Path(config["authorization_file"]).read_text())["state"]
        == "late-nonce"
    )
    returned = True
    restarted = GatewaySetupDriver(config)
    monkeypatch.setattr(restarted, "_api", gateway)
    restarted.resume(
        run_id="run-one",
        owner_id="person-one",
        fixture_kind="fresh",
        policy_required=False,
        timeout_seconds=120,
    )
    assert restarted.connection_revision == "selected-revision"
    assert sum(route.endswith("/start") for _, route in requests) == 1
    restarted.cleanup(run_id="run-one", owner_id="person-one", fixture_kind="fresh")
    assert json.loads(Path(config["attempt_file"]).read_text())["phase"] == "cleaned"


def test_uncertain_identity_start_and_foreign_attempt_block_replay_and_cleanup(
    tmp_path, monkeypatch
):
    path, config = configuration(tmp_path)
    _, config = load_setup(path, gateway_url=config["gateway_url"])
    first = GatewaySetupDriver(config)
    requests = []

    def gateway(method, route, **kwargs):
        requests.append((method, route))
        if route.endswith("/start"):
            raise TimeoutError("lost response")
        return {"status": "ready"}

    monkeypatch.setattr(first, "_api", gateway)
    with pytest.raises(TimeoutError):
        call_setup(first)
    assert first._attempt()["phase"] == "starting"
    resumed = GatewaySetupDriver(config)
    monkeypatch.setattr(resumed, "_api", gateway)
    with pytest.raises(ValueError, match="do not replay"):
        call_setup(resumed)
    with pytest.raises(ValueError, match="cannot be blanket-disconnected"):
        resumed.cleanup(run_id="run-one", owner_id="person-one", fixture_kind="fresh")
    assert sum(route.endswith("/start") for _, route in requests) == 1
    record = first._attempt()
    record["owner_id"] = "another-person"
    first._save_attempt(record)
    with pytest.raises(ValueError, match="unrelated"):
        call_setup(GatewaySetupDriver(config))
