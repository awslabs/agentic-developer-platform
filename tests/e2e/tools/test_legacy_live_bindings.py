"""Controlled live-driver protocols, never actual external-provider acceptance."""

from copy import deepcopy
from contextlib import contextmanager
import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.e2e.tools import legacy_live_bindings as legacy
from tests.e2e.tools import atlassian_authority_scenario as authority
from tests.e2e.tools.test_atlassian_authority_scenario import (
    transcript,
    with_call,
    BINDING,
    GATEWAY,
    ACCOUNT,
    REGION,
    ROLE,
)

TARGET = {
    "gateway": "a" * 40,
    "gateway_image": "example/gateway@sha256:" + "b" * 64,
    "facade_image": "example/facade@sha256:" + "c" * 64,
}


def spec(connector):
    fixture = {
        "github": {},
        "jira": {
            "site_id": "site-a",
            "issue_id": "10001",
            "issue_key": "ADP-1",
            "summary": "Owned fixture",
        },
        "slack": {
            "team_id": "T12345678",
            "user_id": "U12345678",
            "channel_id": "C12345678",
            "message": "ADP isolated acceptance",
            "run_marker": "12345678-1234-4123-8123-123456789abc",
            "cleanup_authorized": True,
        },
    }[connector]
    provider = "atlassian" if connector == "jira" else connector
    return {
        "owner_id": "owner-a",
        "model": "fixture-model",
        "bounds": dict(authority.BOUNDS),
        "fixture": fixture,
        "grant": {
            "account": ACCOUNT,
            "region": REGION,
            "environment": "dev",
            "adapter_role_arn": ROLE,
            "adapter_label": "oidc-adapter",
            "workload_name": "adp-tools-dev-oauth-user",
            "provider_name": "adp-tools-dev-" + provider + "-user",
            "scopes": {
                "github": ["read:user"],
                "jira": ["read:jira-work", "read:me"],
                "slack": ["users:read", "chat:write", "channels:history"],
            }[connector],
            "provider_account_id": {
                "github": "123",
                "jira": "account-a",
                "slack": "U12345678",
            }[connector],
        },
    }


def manifest(cell=None):
    cells = dict.fromkeys(legacy.CELLS)
    if cell:
        cells[cell] = spec(cell.split(":")[0])
    return {
        "binding": dict(BINDING),
        "target": dict(TARGET),
        "gateway_url": GATEWAY,
        "tenant_id": "tenant-a",
        "cells": cells,
    }


def binding():
    return dict(
        binding=BINDING,
        target=TARGET,
        gateway_url=GATEWAY,
        account=ACCOUNT,
        region=REGION,
        reader_role=ROLE,
    )


def bind(tmp_path, doc):
    return legacy.bind_legacy(
        json.dumps(doc),
        json.dumps(
            {cell: "private-refresh" for cell, spec in doc["cells"].items() if spec}
        ),
        directory=tmp_path,
        **binding(),
    )


def catalogue(connector):
    return {
        "tools": [
            {"name": key, "inputSchema": deepcopy(item["inputSchema"])}
            for key, item in legacy.BASELINE.items()
            if key.startswith(connector + "___")
        ]
    }


@pytest.mark.parametrize(
    "fault",
    ("binding", "role", "plugin", "foreign_scope", "post_without_cleanup", "path"),
)
def test_binding_rejects_foreign_authority_and_arbitrary_configuration_before_files(
    tmp_path, fault
):
    doc = manifest("slack:cli")
    selected = doc["cells"]["slack:cli"]
    if fault == "binding":
        doc["binding"]["run_id"] = "foreign"
    elif fault == "role":
        selected["grant"]["adapter_role_arn"] = "other"
    elif fault == "plugin":
        selected["module"] = "private.operator.plugin"
    elif fault == "foreign_scope":
        selected["grant"]["scopes"].append("admin")
    elif fault == "post_without_cleanup":
        selected["fixture"]["cleanup_authorized"] = False
    else:
        selected["fixture"]["channel_id"] = "../../other"
    with pytest.raises(ValueError):
        bind(tmp_path, doc)
    assert list(tmp_path.iterdir()) == []


def test_missing_provider_bindings_preserve_nine_cells_without_calls(
    tmp_path, monkeypatch
):
    doc = manifest()
    path = bind(tmp_path, doc)
    monkeypatch.setattr(
        legacy.LegacyCell,
        "run",
        lambda *_: pytest.fail("missing fixture caused side effects"),
    )
    report = legacy.collect_legacy(path, client_factory=None, **binding())
    qualified = legacy.qualify_legacy(report, manifest=doc)
    assert len(qualified["cases"]) == 9 and qualified["full_ac04"] == "pending"
    assert all(row["observable_parity"] == "pending" for row in qualified["cases"])
    promoted = deepcopy(report)
    promoted["full_ac04"] = "pass"
    with pytest.raises(ValueError):
        legacy.qualify_legacy(promoted, manifest=doc)


@pytest.mark.parametrize("connector", legacy.CONNECTORS)
def test_actual_provider_sdk_and_http_paths_use_bound_user_and_never_retain_tokens(
    monkeypatch, connector
):
    selected = spec(connector)
    sdk, requests = [], []

    def aws(config, role, service, operation, *, input_value):
        sdk.append((role, service, operation, input_value))
        return (
            {"workloadAccessToken": "private-workload"}
            if operation.startswith("get-workload")
            else {"accessToken": "private-provider"}
        )

    monkeypatch.setattr(legacy, "_aws", aws)
    provider = legacy.LegacyProvider(connector, selected)

    def request(token, origin, path, **kwargs):
        assert token == "private-provider"
        requests.append((origin, path, kwargs))
        if path == "/user":
            return {"id": 123, "login": "owned-user"}, {"x-oauth-scopes": "read:user"}
        if path == "/me":
            return {"account_id": "account-a"}, {}
        if path == "/oauth/token/accessible-resources":
            return [{"id": "site-a", "scopes": ["read:jira-work", "read:me"]}], {}
        if "/issue/" in path:
            return {
                "id": "10001",
                "key": "ADP-1",
                "fields": {"summary": "Owned fixture"},
            }, {}
        if path == "/api/auth.test":
            return {"ok": True, "user_id": "U12345678", "team_id": "T12345678"}, {
                "x-oauth-scopes": "users:read,chat:write,channels:history"
            }
        if path == "/api/users.info":
            return {"ok": True, "user": {"id": "U12345678", "name": "owned-user"}}, {}
        pytest.fail(path)

    monkeypatch.setattr(provider, "request", request)
    result = provider.snapshot()
    assert (
        result["source"] == "direct-legacy-provider" and result["owner_id"] == "owner-a"
    )
    assert "private-" not in json.dumps(result)
    assert sdk[0][3]["userId"] == "owner-a"
    assert (
        sdk[1][3]["resourceCredentialProviderName"]
        == selected["grant"]["provider_name"]
    )
    assert len(requests) == {"github": 1, "jira": 3, "slack": 2}[connector]


def profile(connector):
    return {
        "github": {"id": 123, "login": "owned-user"},
        "jira": {"id": "10001", "key": "ADP-1", "fields": {"summary": "Owned fixture"}},
        "slack": {"id": "U12345678", "name": "owned-user"},
    }[connector]


def runtime(
    tmp_path,
    monkeypatch,
    cell,
    *,
    post_loss=False,
    delete_loss=False,
    after_failure=False,
):
    doc = manifest(cell)
    path = bind(tmp_path, doc)
    connector, client = cell.split(":")
    root = path.parent / cell.replace(":", "-")
    driver = legacy.LegacyCell(root, cell, doc, client_factory=None)
    operations = root / "runtime" / ".bedrock-gateway" / "tool-operations"
    operations.mkdir(mode=0o700, parents=True)
    environ = {
        "HOME": str(operations.parent.parent),
        "BG_CONFIG_DIR": str(operations.parent),
    }
    monkeypatch.setattr(
        driver, "start", lambda: (Path("/private/adp"), environ, {"adp": "a" * 64})
    )
    state = {
        "effects": [],
        "posts": [],
        "deletes": 0,
        "closes": [],
        "status_reads": [],
        "receipts": {},
        "snapshots": 0,
    }

    def api(route):
        if route.endswith("/me"):
            return {"user_id": "owner-a", "org_id": "tenant-a"}
        reference = route.split("/operations/", 1)[1].split("?", 1)[0]
        return state["receipts"][reference]

    monkeypatch.setattr(driver, "api", api)

    def snapshot():
        state["snapshots"] += 1
        if after_failure and state["snapshots"] == 2:
            raise ValueError("provider read unavailable after client")
        return {
            "source": "direct-legacy-provider",
            "connector": connector,
            "owner_id": "owner-a",
            "identity": {
                "account_id": doc["cells"][cell]["grant"]["provider_account_id"],
                "scopes": doc["cells"][cell]["grant"]["scopes"],
            },
            "profile": profile(connector),
        }

    monkeypatch.setattr(driver.provider, "snapshot", snapshot)
    monkeypatch.setattr(
        driver.provider, "messages", lambda marker, **_: deepcopy(state["posts"])
    )

    def delete(marker, ts, **kwargs):
        state["deletes"] += 1
        state["posts"] = []
        if delete_loss and state["deletes"] == 1:
            raise TimeoutError("delete response lost after effect")

    monkeypatch.setattr(driver.provider, "delete", delete)

    def operation(tool, arguments):
        state["effects"].append(tool)
        reference, execution = str(uuid4()), str(uuid4())
        if tool.endswith("post_message"):
            value = {
                "channel_id": arguments["channel"],
                "user_id": "U12345678",
                "text": arguments["text"],
                "run_marker": arguments["run_marker"],
                "ts": "1792990000.123456",
            }
            state["posts"] = [
                {
                    "channel": arguments["channel"],
                    "user": "U12345678",
                    "text": arguments["text"],
                    "ts": value["ts"],
                }
            ]
        else:
            value = profile(connector)
        receipt = {
            "operation_ref": reference,
            "request_id": reference,
            "execution_id": execution,
            "status": "confirmed",
            "result_status": "success",
            "result": {
                "result": {"content": [{"type": "text", "text": json.dumps(value)}]}
            },
        }
        state["receipts"][reference] = receipt
        legacy._write(
            operations / (reference + ".json"),
            {
                "request_id": reference,
                "tool": tool,
                "arguments": arguments,
                "status": "confirmed",
                "execution_id": execution,
            },
        )
        if post_loss and tool.endswith("post_message"):
            raise TimeoutError("post response lost after effect")
        return receipt

    def mcp(method, params=None):
        if method == "tools/list":
            return catalogue(connector)
        tool, arguments = params["name"], params["arguments"]
        if tool == "adp___operation_status":
            state["status_reads"].append(arguments["operation_ref"])
            value = state["receipts"][arguments["operation_ref"]]
        elif tool == "adp___execution_close":
            state["closes"].append(arguments["operation_ref"])
            value = {
                "execution_id": state["receipts"][arguments["operation_ref"]][
                    "execution_id"
                ],
                "state": "closed",
                "cleanup_status": "confirmed",
            }
        else:
            value = operation(tool, arguments)
        return {"content": [{"type": "text", "text": json.dumps(value)}]}

    monkeypatch.setattr(driver, "mcp", mcp)

    def native(client, installed, environ, probe, spec):
        packet = operation(probe["tool"], probe["arguments"])
        raw = with_call(
            transcript(client, packet), client, probe["tool"], probe["arguments"]
        )
        observed = legacy.native_observation(
            client,
            raw,
            tool=probe["tool"],
            arguments=probe["arguments"],
            bounds=spec["bounds"],
        )
        return raw, observed

    monkeypatch.setattr(legacy, "invoke_native", native)
    return driver, state, doc


@pytest.mark.parametrize("cell", legacy.CELLS)
def test_actual_client_provider_receipt_and_cleanup_campaign_for_all_nine_cells(
    tmp_path, monkeypatch, cell
):
    driver, state, doc = runtime(tmp_path, monkeypatch, cell)
    result = driver.run()
    assert result["status"] == "pass", result
    qualified = legacy.qualify_cell(result, manifest=doc)
    assert (
        qualified["observable_parity"] == "pass" and qualified["full_ac04"] == "pending"
    )
    effects = len(state["effects"])
    assert effects == (2 if cell.startswith("slack:") else 1)
    assert len(state["closes"]) == effects
    assert driver.run() == result and len(state["effects"]) == effects
    if cell.startswith("slack:"):
        assert state["deletes"] == 1 and state["posts"] == []


@pytest.mark.parametrize("client", legacy.CLIENTS)
def test_lost_post_response_recovers_only_original_status_and_cleanup_without_reposting(
    tmp_path, monkeypatch, client
):
    driver, state, doc = runtime(
        tmp_path, monkeypatch, "slack:" + client, post_loss=True
    )
    first = driver.run()
    assert first["status"] == "pending" and first["cleanup"] == "pending"
    assert len(state["posts"]) == 1 and state["deletes"] == 0
    resumed = driver.run()
    assert resumed["status"] == "pending" and resumed["cleanup"] == "confirmed"
    assert state["deletes"] == 1 and not state["posts"] and len(state["effects"]) == 2
    assert len(state["status_reads"]) == 1
    with pytest.raises(ValueError):
        legacy.qualify_cell(resumed, manifest=doc)


def test_lost_delete_reply_uses_independent_absence_and_does_not_repeat_deletion(
    tmp_path, monkeypatch
):
    driver, state, doc = runtime(tmp_path, monkeypatch, "slack:cli", delete_loss=True)
    first = driver.run()
    assert first["status"] == "pending" and state["deletes"] == 1 and not state["posts"]
    second = driver.run()
    assert second["status"] == "pass", second
    assert state["deletes"] == 1 and len(state["effects"]) == 2


def test_fresh_driver_resumes_original_uncertain_post_without_reposting(
    tmp_path, monkeypatch
):
    original, state, doc = runtime(tmp_path, monkeypatch, "slack:cli", post_loss=True)
    assert original.run()["cleanup"] == "pending"
    recovered = legacy.LegacyCell(
        original.root, original.cell, doc, client_factory=None
    )
    # Only external boundaries are shared. Recovery reads the journal from disk.
    for name in ("start", "api", "mcp"):
        monkeypatch.setattr(recovered, name, getattr(original, name))
    recovered.provider = original.provider
    row = recovered.run()
    assert row["cleanup"] == "confirmed" and row["status"] == "pending"
    assert len(state["effects"]) == 2 and state["deletes"] == 1
    assert len(state["status_reads"]) == 1


def test_runtime_reopens_original_home_and_refuses_a_missing_original(
    tmp_path, monkeypatch
):
    doc = manifest("github:cli")
    path = bind(tmp_path, doc)
    root = path.parent / "github-cli"
    calls = []

    @contextmanager
    def installed(*args, recovery_root, recovery_id):
        calls.append(("install", recovery_id))
        home = recovery_root / recovery_id
        home.mkdir(mode=0o700)
        yield Path("/private/adp"), {"HOME": str(home)}, {"adp": "a" * 64}

    @contextmanager
    def resumed(*args, recovery_root, recovery_id):
        calls.append(("resume", recovery_id))
        yield (
            Path("/private/adp"),
            {"HOME": str(recovery_root / recovery_id)},
            {"adp": "a" * 64},
        )

    monkeypatch.setattr(legacy, "installed_clients", installed)
    monkeypatch.setattr(legacy, "resume_installed_clients", resumed)
    monkeypatch.setattr(legacy, "import_session", lambda *args: None)
    first = legacy.LegacyCell(root, "github:cli", doc, client_factory=None)
    first.start()
    first.close()
    second = legacy.LegacyCell(root, "github:cli", doc, client_factory=None)
    second.start()
    second.close()
    assert calls == [("install", calls[0][1]), ("resume", calls[0][1])]
    (root / calls[0][1]).rmdir()
    third = legacy.LegacyCell(root, "github:cli", doc, client_factory=None)
    with pytest.raises(ValueError, match="Original runtime directory unavailable"):
        third.start()
    assert len(calls) == 2


def test_slack_marker_uses_the_selected_adapter_fixture(tmp_path, monkeypatch):
    driver, state, doc = runtime(tmp_path, monkeypatch, "slack:cli")
    row = driver.run()
    marker = doc["cells"]["slack:cli"]["fixture"]["run_marker"]
    assert row["status"] == "pass" and row["marker"] == marker
    doc["cells"]["slack:cli"]["fixture"]["run_marker"] = str(uuid4())
    with pytest.raises(ValueError, match="Original Slack marker required"):
        legacy.qualify_cell(row, manifest=doc)


def test_post_readback_failure_resumes_without_another_client_read(
    tmp_path, monkeypatch
):
    driver, state, _ = runtime(tmp_path, monkeypatch, "github:cli", after_failure=True)
    first = driver.run()
    assert first["status"] == "pending" and first["cleanup"] == "confirmed"
    assert driver.run()["status"] == "pass" and state["effects"] == [
        "github___get_user"
    ]


@pytest.mark.parametrize(
    "field", ("profile", "identity", "receipt", "transcript", "close", "catalogue")
)
def test_independent_qualification_rejects_substituted_observations(
    tmp_path, monkeypatch, field
):
    driver, _, doc = runtime(tmp_path, monkeypatch, "github:codex")
    row = driver.run()
    assert row["status"] == "pass"
    if field == "profile":
        row["after"]["profile"]["id"] = 999
    elif field == "identity":
        row["before"]["identity"]["account_id"] = "foreign"
    elif field == "receipt":
        row["steps"]["read"]["independent_receipt"]["result_status"] = "error"
    elif field == "transcript":
        row["steps"]["read"]["transcript"] = "{}"
    elif field == "close":
        row["steps"]["read"]["close"]["cleanup_status"] = "pending"
    else:
        row["listing"]["tools"][0]["inputSchema"]["properties"]["owner"] = {
            "type": "string"
        }
    with pytest.raises((ValueError, KeyError)):
        legacy.qualify_cell(row, manifest=doc)


@pytest.mark.parametrize("fault", ("foreign_owner", "duplicate", "incomplete"))
def test_actual_slack_cleanup_refuses_foreign_ambiguous_or_incomplete_history(
    monkeypatch, fault
):
    selected = spec("slack")
    provider = legacy.LegacyProvider("slack", selected)
    marker = str(uuid4())
    rows = [
        {
            "user": "U12345678",
            "text": selected["fixture"]["message"] + f" [run:{marker}]",
            "ts": "1792990000.123456",
        }
    ]
    if fault == "foreign_owner":
        rows[0]["user"] = "U99999999"
    elif fault == "duplicate":
        rows.append(deepcopy(rows[0]))
    monkeypatch.setattr(provider, "with_token", lambda callback: callback("private"))
    monkeypatch.setattr(provider, "_identity", lambda _: ({}, None))
    writes = []

    def request(token, origin, path, **kwargs):
        if path == "/api/chat.delete":
            writes.append(kwargs)
            pytest.fail("unqualified history permitted a delete")
        return {"ok": True, "has_more": fault == "incomplete", "messages": rows}, {}

    monkeypatch.setattr(provider, "request", request)
    with pytest.raises(ValueError):
        provider.delete(marker, "1792990000.123456", oldest="1792989900")
    assert writes == []


def test_actual_slack_delete_calls_provider_once_and_preserves_exact_owner_binding(
    monkeypatch,
):
    selected = spec("slack")
    provider = legacy.LegacyProvider("slack", selected)
    marker, ts = str(uuid4()), "1792990000.123456"
    monkeypatch.setattr(provider, "with_token", lambda callback: callback("private"))
    monkeypatch.setattr(provider, "_identity", lambda _: ({}, None))
    requests = []

    def request(token, origin, path, **kwargs):
        requests.append((path, kwargs))
        if path.endswith("history"):
            return {
                "ok": True,
                "messages": [
                    {
                        "user": "U12345678",
                        "text": selected["fixture"]["message"] + f" [run:{marker}]",
                        "ts": ts,
                    }
                ],
            }, {}
        assert kwargs["body"] == {"channel": "C12345678", "ts": ts}
        return {"ok": True, "channel": "C12345678", "ts": ts}, {}

    monkeypatch.setattr(provider, "request", request)
    provider.delete(marker, ts, oldest="1792989900")
    assert [path for path, _ in requests] == [
        "/api/conversations.history",
        "/api/chat.delete",
    ]


def test_collect_keeps_other_cells_when_proxy_cleanup_fails(tmp_path, monkeypatch):
    doc = manifest("github:cli")
    path = bind(tmp_path, doc)
    monkeypatch.setattr(
        legacy.LegacyCell,
        "run",
        lambda self: {"cell": self.cell, "status": "pending", "reason": "unavailable"},
    )

    def failed_close(self):
        raise ValueError("owned proxy termination not confirmed")

    monkeypatch.setattr(legacy.LegacyCell, "close", failed_close)
    result = legacy.collect_legacy(path, client_factory=None, **binding())
    assert len(result["cases"]) == 9
    assert result["cases"][0]["reason"] == "original-proxy-cleanup-unconfirmed"
    assert legacy.qualify_legacy(result, manifest=doc)["full_ac04"] == "pending"
