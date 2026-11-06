"""Policy denial retains the owned setup until fresh permission readback permits cleanup."""

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from tests.e2e.tools import atlassian_readback_grant as readback
from tests.e2e.tools.atlassian_integration import (
    SetupActionLedger,
    live_connect,
    run_live_connect,
)
from tests.e2e.tools.atlassian_policy_observation import digest
from tests.e2e.tools.atlassian_setup_observer import IndependentSetupObserver
from tests.e2e.tools.test_atlassian_readback_grant import (
    MARKER,
    configuration as grant_configuration,
    provider,
)
from tests.e2e.tools.test_atlassian_setup_live import configuration, private


@pytest.fixture
def setup_policy(tmp_path, monkeypatch):
    provider.__wrapped__(monkeypatch)
    _, config = configuration(tmp_path)
    grant = grant_configuration("person-one")
    config.update(
        {
            key: grant[key]
            for key in ("account", "region", "adapter_label", "adapter_role_arn")
        }
    )
    config.update(
        site_id="fixture-site",
        expected_subject="person-one",
        policy_required=True,
        provider_name=grant["provider_name"],
        workload_name=grant["workload_name"],
    )
    root = tmp_path / "setup"
    config["policy_observation"] = {
        "tenant_id": "tenant-one",
        "grant_file": private(root / "grant.json", json.dumps(grant)),
        "probes": {
            name: {"cloudId": "fixture-site"}
            for name in (
                "get_jira_issue",
                "search_jira",
                "get_confluence_page",
                "search_confluence",
            )
        },
        "expected_results": {
            name: "f" * 64
            for name in (
                "get_jira_issue",
                "search_jira",
                "get_confluence_page",
                "search_confluence",
            )
        },
        "checks": {
            "read": {
                "path": "/ex/jira/fixture-site/rest/api/3/issue/101",
                "expected_result_sha256": digest(
                    {"id": "101", "fields": {"summary": MARKER, "description": None}}
                ),
                "ledger_file": str(root / "read.json"),
            },
            "search": {
                "path": "/ex/jira/fixture-site/rest/api/3/search/jql?jql=project%3DTEST&maxResults=1",
                "expected_result_sha256": digest({"issues": [], "isLast": True}),
                "ledger_file": str(root / "search.json"),
            },
        },
    }
    observer = IndependentSetupObserver(config)
    observer.client_sha256 = "a" * 64
    state = {"status": "allowed", "change": None}
    original = readback.ReadMCPTransport.send

    def send(self, method, params, token):
        if (
            state["status"] == "blocked"
            and method == "tools/call"
            and params["name"] in readback.TOOLS.values()
        ):
            permission = (
                "search" if params["name"] == readback.TOOLS["search"] else "read"
            )
            return {
                "error": {
                    "message": f"Access denied: Your organization admin has not authorized the {permission} permission."
                }
            }
        return original(self, method, params, token)

    monkeypatch.setattr(readback.ReadMCPTransport, "send", send)

    def post(self, url, **kwargs):
        assert url.endswith("/policy/observe")
        assert kwargs["json"]["connection_revision"] == "revision-1"
        result = {
            "status": state["status"],
            "evidence_sha256": "d" * 64,
            "observed_at_ms": int(time.time() * 1000),
            "configuration_sha256": "b" * 64,
            "connection_revision": "revision-1",
            "site_id": "fixture-site",
            "owner_id": "person-one",
            "tenant_id": "tenant-one",
            "permission": "read-search",
        }
        if state["change"] == "stale":
            result["observed_at_ms"] -= 300001
        elif state["change"] == "future":
            result["observed_at_ms"] += 10000
        elif state["change"]:
            result[state["change"]] = "foreign"
        return httpx.Response(200, json=result)

    monkeypatch.setattr(httpx.Client, "post", post)
    return observer, config, state


def test_effective_observation_does_not_fabricate_human_approval_and_refreshes_revocation(
    setup_policy,
):
    observer, config, state = setup_policy
    for status in ("blocked", "allowed", "blocked"):
        state["status"] = status
        event = observer._policy_action({"revision": "revision-1"})
        assert event["action"] == "policy_observation"
        assert event["actor"] == "machine" and event["source"] == "provider"
        assert event["status"] == ("observed" if status == "allowed" else "blocked")
    assert "access-person-one" not in json.dumps(event)


def test_protected_workflow_materializes_policy_paths_in_the_owned_directory(
    setup_policy, tmp_path
):
    from pathlib import Path

    from tests.e2e.tools.atlassian_setup_binding import (
        LOCAL_FILES,
        SESSION_ENV,
        bind_setup,
    )
    from tests.e2e.tools.atlassian_setup_live import load_setup

    observer, config, state = setup_policy
    manifest = {key: value for key, value in config.items() if key not in LOCAL_FILES}
    manifest = json.loads(json.dumps(manifest))
    policy = manifest["policy_observation"]
    policy["grant"] = json.loads(Path(policy.pop("grant_file")).read_text())
    for check in policy["checks"].values():
        check.pop("ledger_file")
    sessions = {key: "private-session" for key in SESSION_ENV}
    path = bind_setup(
        json.dumps(manifest),
        sessions,
        directory=tmp_path,
        gateway_url=config["gateway_url"],
    )
    _, bound = load_setup(path, gateway_url=config["gateway_url"])
    assert Path(bound["policy_observation"]["grant_file"]).parent == Path(path).parent
    assert all(
        Path(check["ledger_file"]).parent == Path(path).parent
        for check in bound["policy_observation"]["checks"].values()
    )
    assert "private-session" not in Path(path).read_text()
    policy["checks"]["read"]["ledger_file"] = "/tmp/foreign"
    with pytest.raises(ValueError, match="local file"):
        bind_setup(
            json.dumps(manifest),
            sessions,
            directory=tmp_path,
            gateway_url=config["gateway_url"],
        )


@pytest.mark.parametrize(
    "change",
    ["owner_id", "tenant_id", "site_id", "connection_revision", "stale", "future"],
)
def test_setup_policy_rejects_foreign_and_stale_results(setup_policy, change):
    observer, config, state = setup_policy
    state["change"] = change
    with pytest.raises(ValueError, match="stale or belongs"):
        observer._policy_action({"revision": "revision-1"})


def test_policy_denial_preserves_original_grant_then_resumes_and_cleans(setup_policy):
    observer, config, state = setup_policy
    ledger = SetupActionLedger(
        config["ledger_file"],
        run_id=config["run_id"],
        owner_id=config["owner_id"],
        fixture_kind="fresh",
    )
    calls = []
    driver = SimpleNamespace(
        isolated=True,
        authorized=True,
        deployed_target=True,
        setup=lambda **kwargs: calls.append("setup"),
        resume=lambda **kwargs: calls.append("resume"),
        cleanup=lambda **kwargs: calls.append("cleanup"),
    )

    def actions(**kwargs):
        events = [
            {
                "action": action,
                "actor": "machine" if action in ledger.MACHINE_ACTIONS else "human",
                "status": "observed",
                "evidence_sha256": "e" * 64,
                "source": {
                    "dcr_registration": "identity",
                    "identity_provider_setup": "identity",
                    "consent": "browser",
                }.get(action, "gateway"),
                "run_id": config["run_id"],
                "owner_id": config["owner_id"],
            }
            for action in sorted(ledger.MACHINE_ACTIONS | {"consent", "site_selection"})
        ]
        return events + [observer._policy_action({"revision": "revision-1"})]

    observing = SimpleNamespace(
        independent=True, read_actions=actions, cleaned=lambda **kwargs: True
    )
    state["status"] = "blocked"
    first = run_live_connect(
        ledger, policy_required=True, driver=driver, observer=observing
    )
    assert first["status"] == "blocked" and first["cleanup"] == "pending-policy"
    assert calls == ["setup"]
    state["status"] = "allowed"
    reopened = SetupActionLedger(ledger.path, **ledger.identity)
    second = run_live_connect(
        reopened, policy_required=True, driver=driver, observer=observing
    )
    assert second["status"] == "pass" and second["cleanup"] == "confirmed"
    assert calls == ["setup", "resume", "cleanup"]
    assert not any(
        action["action"] == "policy_approval" for action in reopened.state()["actions"]
    )
    reopened.record("policy_approval", actor="human", status="observed", evidence_sha256="c" * 64)
    reopened.record("policy_observation", actor="machine", status="blocked", evidence_sha256="d" * 64)
    assert live_connect(reopened, policy_required=True)["status"] == "blocked"
