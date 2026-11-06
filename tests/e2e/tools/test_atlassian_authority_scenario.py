"""Controlled protocol observations, never actual provider-live acceptance."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.tools import atlassian_authority_scenario as authority
from tests.e2e.tools.test_atlassian_readback_grant import configuration


BINDING = {
    "source_revision": "a" * 40,
    "definition_revision": "b" * 40,
    "run_id": "101",
    "run_attempt": "1",
    "release_sha256": "c" * 64,
}
GATEWAY = "https://gateway.example.test"
ACCOUNT, REGION = "123456789012", "us-east-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/adp-dev-atlassian-mcp-observer-adapter"
REFERENCE = "11111111-1111-4111-8111-111111111111"
TOOL = "atlassian___get_jira_issue"
ARGUMENTS = {"cloudId": "site-b", "issueIdOrKey": "10001"}


def response(status="rejected", message="Gateway rejected tool request (HTTP 403)"):
    return {
        "status": status,
        "message": message,
        "operation_ref": REFERENCE,
        "recovery": "Check the original operation.",
    }


def transcript(client, result=None):
    packet = {
        "content": [{"type": "text", "text": json.dumps(result or response())}],
        "isError": True,
    }
    usage = {"input_tokens": 10, "output_tokens": 4}
    if client == "claude-code":
        events = [
            {
                "type": "assistant",
                "message": {
                    "usage": usage,
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "mcp__adp-tools__" + TOOL,
                            "input": ARGUMENTS,
                        }
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "is_error": True,
                            "content": packet["content"],
                        }
                    ]
                },
            },
            {"type": "result", "is_error": False},
        ]
    else:
        events = [
            {
                "type": "item.completed",
                "item": {
                    "id": "tool-1",
                    "type": "mcp_tool_call",
                    "server": "adp-tools",
                    "tool": TOOL,
                    "arguments": ARGUMENTS,
                    "status": "completed",
                    "result": packet,
                },
            },
            {"type": "turn.completed", "usage": usage},
        ]
    return "\n".join(json.dumps(event) for event in events)


def manifest():
    clients = {}
    for client, suffix in zip(authority.CLIENTS, ("a", "b"), strict=True):
        grant = configuration("provider-" + suffix)
        grant["site_ids"] = ["site-" + suffix]
        clients[client] = {
            "owner_id": "owner-" + suffix,
            "model": "fixture-model",
            "bounds": dict(authority.BOUNDS),
            "grant": grant,
            "probes": dict.fromkeys(authority.CASES),
        }
    clients["claude-code"]["probes"]["foreign_site"] = {
        "tool": TOOL,
        "arguments": dict(ARGUMENTS),
        "subject_client": "codex",
        "site_id": "site-b",
        "provider_id": "10001",
        "marker": "adp-8454-" + "a" * 16,
    }
    return {
        "binding": dict(BINDING),
        "gateway_url": GATEWAY,
        "tenant_id": "tenant-a",
        "clients": clients,
    }


def bind(tmp_path, doc=None):
    return authority.bind_authority(
        json.dumps(doc or manifest()),
        json.dumps({"claude-code": "private-a", "codex": "private-b"}),
        directory=tmp_path,
        binding=BINDING,
        gateway_url=GATEWAY,
        account=ACCOUNT,
        region=REGION,
        reader_role=ROLE,
    )


def denial_row(client="claude-code"):
    doc = manifest()
    other = "codex" if client == "claude-code" else "claude-code"
    if client == "codex":
        # Keep the same requested fixture but exchange the synthetic actor roles.
        doc["clients"]["codex"]["grant"]["site_ids"] = ["site-a"]
        doc["clients"]["claude-code"]["grant"]["site_ids"] = ["site-b"]
        doc["clients"][client]["probes"]["foreign_site"] = {
            **doc["clients"]["claude-code"]["probes"]["foreign_site"],
            "subject_client": other,
        }
    probe = doc["clients"][client]["probes"]["foreign_site"]
    before = {
        "source": "direct-atlassian-provider",
        "owner_id": doc["clients"][other]["owner_id"],
        "account_id": doc["clients"][other]["grant"]["provider_account_id"],
        "sites": [{"site_id": "site-b", "scopes": ["read:jira:agent-interface"]}],
        "site_id": "site-b",
        "provider_id": "10001",
        "content_sha256": "d" * 64,
    }
    raw = transcript(client)
    observed = {
        "principal": {
            "user_id": doc["clients"][client]["owner_id"],
            "org_id": "tenant-a",
        },
        "connection": {"status": "ready", "connection": {"sites": ["site-a"]}},
        "before": before,
        "after": deepcopy(before),
        "installed_hashes": {"adp": "e" * 64},
        "listing": {"tools": []},
        "transcript": raw,
        "native": authority.native_observation(
            client, raw, tool=TOOL, arguments=ARGUMENTS, bounds=authority.BOUNDS
        ),
    }
    row = {
        "schema": "atlassian-authority-case/v1",
        "binding": dict(BINDING),
        "client": client,
        "variant": "foreign_site",
        "status": "pending",
        "reason": "fixture",
        "probe": probe,
        "observations": observed,
        "phase": "invoked",
    }
    return doc, row


@pytest.mark.parametrize("client", authority.CLIENTS)
def test_native_parser_preserves_real_error_packet_instead_of_treating_error_as_success(
    client,
):
    result = authority.native_observation(
        client,
        transcript(client),
        tool=TOOL,
        arguments=ARGUMENTS,
        bounds=authority.BOUNDS,
    )
    assert result["response"] == response()
    assert result["source"] == client + "-native-mcp" and result["model_requests"] == 1


@pytest.mark.parametrize("client", authority.CLIENTS)
@pytest.mark.parametrize(
    "fault", ("wrong_tool", "wrong_arguments", "duplicate", "model_error", "usage")
)
def test_native_parser_rejects_wrong_calls_model_errors_and_unbounded_usage(
    client, fault
):
    events = [json.loads(line) for line in transcript(client).splitlines()]
    call = (
        events[0]["message"]["content"][0]
        if client == "claude-code"
        else events[0]["item"]
    )
    if fault == "wrong_tool":
        call["name" if client == "claude-code" else "tool"] = "other"
    elif fault == "wrong_arguments":
        call["input" if client == "claude-code" else "arguments"] = {}
    elif fault == "duplicate":
        events.insert(0, deepcopy(events[0]))
    elif fault == "model_error":
        if client == "claude-code":
            events[-1]["is_error"] = True
        else:
            events[-1]["type"] = "turn.failed"
    else:
        usage = (
            events[0]["message"]["usage"]
            if client == "claude-code"
            else events[-1]["usage"]
        )
        usage["input_tokens"] = True
    with pytest.raises(ValueError):
        authority.native_observation(
            client,
            "\n".join(json.dumps(row) for row in events),
            tool=TOOL,
            arguments=ARGUMENTS,
            bounds=authority.BOUNDS,
        )


@pytest.mark.parametrize("client", authority.CLIENTS)
def test_qualifier_uses_actual_owner_site_and_unchanged_provider_readback(client):
    doc, row = denial_row(client)
    assert authority.qualify_denial(row, manifest=doc)["status"] == "pass"
    row["observations"]["after"]["content_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        authority.qualify_denial(row, manifest=doc)


@pytest.mark.parametrize(
    "status,message",
    (
        ("unknown", "Gateway rejected tool request (HTTP 403)"),
        ("rejected", "Gateway rejected tool request (HTTP 401)"),
        ("rejected", "Gateway rejected tool request (HTTP 404)"),
        ("unknown", "Session expired; run adp login"),
        ("confirmed", "Gateway rejected tool request (HTTP 403)"),
    ),
)
def test_authentication_failure_timeout_and_status_only_inputs_never_prove_denial(
    status, message
):
    doc, row = denial_row()
    raw = transcript("claude-code", response(status, message))
    row["observations"].update(
        transcript=raw,
        native=authority.native_observation(
            "claude-code", raw, tool=TOOL, arguments=ARGUMENTS, bounds=authority.BOUNDS
        ),
    )
    with pytest.raises(ValueError):
        authority.qualify_denial(row, manifest=doc)
    row["observations"] = {"status": "pass"}
    with pytest.raises(ValueError):
        authority.qualify_denial(row, manifest=doc)


def test_binding_generates_private_data_and_sessions_without_loading_executable_plugins(
    tmp_path,
):
    path = bind(tmp_path)
    assert json.loads(path.read_text()) == manifest()
    assert path.stat().st_mode & 0o077 == 0
    for client in authority.CLIENTS:
        token = path.parent / client / "session.token"
        assert token.stat().st_mode & 0o077 == 0
        assert "private-" not in path.read_text()


@pytest.mark.parametrize(
    "fault", ("revision", "extra", "owner", "reader", "partial", "tool")
)
def test_invalid_binding_creates_no_files(tmp_path, fault):
    doc = manifest()
    if fault == "revision":
        doc["binding"]["source_revision"] = "f" * 40
    elif fault == "extra":
        doc["module"] = "tests.e2e.tools.private_authority"
    elif fault == "owner":
        doc["clients"]["codex"]["owner_id"] = "owner-a"
    elif fault == "reader":
        doc["clients"]["codex"]["grant"]["adapter_role_arn"] = "other"
    elif fault == "partial":
        doc["clients"]["codex"]["probes"].pop("expired")
    else:
        doc["clients"]["claude-code"]["probes"]["foreign_site"]["tool"] = (
            "browser___start"
        )
    with pytest.raises(ValueError):
        bind(tmp_path, doc)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("client", authority.CLIENTS)
def test_real_invocation_entrypoint_uses_installed_client_and_literal_input(
    monkeypatch, client
):
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=transcript(client).encode())

    monkeypatch.setattr(authority, "run_bounded", run)
    probe = {"tool": TOOL, "arguments": ARGUMENTS}
    raw, observed = authority.invoke_native(
        client,
        Path("/private/adp"),
        {"HOME": "/private/home"},
        probe,
        {"model": "fixture-model", "bounds": dict(authority.BOUNDS)},
    )
    assert raw and observed["response"]["status"] == "rejected"
    command, options = seen[0]
    assert command[:2] == [
        "/private/adp",
        "claude" if client == "claude-code" else "codex",
    ]
    assert (
        options["timeout"] == 120
        and options["max_output_bytes"] == authority.BOUNDS["max_response_bytes"]
    )
    assert (
        json.dumps(ARGUMENTS, separators=(",", ":")).encode() in options["input_data"]
    )


def test_missing_consent_preserves_all_sixteen_cases_without_install_or_provider_calls(
    tmp_path, monkeypatch
):
    doc = manifest()
    for spec in doc["clients"].values():
        spec["probes"] = dict.fromkeys(authority.CASES)
    path = bind(tmp_path, doc)
    campaign = authority.AuthorityCampaign(
        path,
        binding=BINDING,
        gateway_url=GATEWAY,
        account=ACCOUNT,
        region=REGION,
        reader_role=ROLE,
        client_factory=None,
    )
    monkeypatch.setattr(
        campaign,
        "_start",
        lambda *_: pytest.fail("unavailable fixture started a client"),
    )
    report = authority.collect_authority(campaign)
    assert len(report["cases"]) == 16 and report["status"] == "pending"
    assert all(row["reason"] == "missing-provider-fixture" for row in report["cases"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("cloudId", "unrelated"),
        ("issueIdOrKey", "20002"),
        ("id", "10001"),
        ("extra", "unknown"),
    ],
)
def test_native_resource_cannot_differ_from_independent_provider_fixture(
    tmp_path, field, value
):
    doc = manifest()
    doc["clients"]["claude-code"]["probes"]["foreign_site"]["arguments"][field] = value
    with pytest.raises(ValueError):
        bind(tmp_path, doc)
    assert list(tmp_path.iterdir()) == []


def with_call(raw, client, tool, arguments):
    events = [json.loads(line) for line in raw.splitlines()]
    call = (
        events[0]["message"]["content"][0]
        if client == "claude-code"
        else events[0]["item"]
    )
    call["name" if client == "claude-code" else "tool"] = (
        "mcp__adp-tools__" + tool if client == "claude-code" else tool
    )
    call["input" if client == "claude-code" else "arguments"] = arguments
    return "\n".join(json.dumps(event) for event in events)


def set_native(row, packet, tool=None, arguments=None):
    client = row["client"]
    probe = row.get("native_probe", row["probe"])
    tool, arguments = tool or probe["tool"], arguments or probe["arguments"]
    raw = with_call(transcript(client, packet), client, tool, arguments)
    row["observations"].update(
        transcript=raw,
        native=authority.native_observation(
            client, raw, tool=tool, arguments=arguments, bounds=authority.BOUNDS
        ),
    )


@pytest.mark.parametrize("client", authority.CLIENTS)
def test_foreign_owner_requires_real_owner_receipt_backend_denial_and_closed_execution(
    client,
):
    doc, row = denial_row(client)
    doc["clients"][client]["probes"]["foreign_owner"] = row["probe"]
    row["variant"] = "foreign_owner"
    subject = doc["clients"][row["probe"]["subject_client"]]
    row["native_probe"] = {
        "tool": "adp___operation_status",
        "arguments": {"operation_ref": REFERENCE},
    }
    set_native(row, response(message=authority.OWNER_DENIAL))
    receipt = {
        "status": "confirmed",
        "result_status": "success",
        "operation_ref": REFERENCE,
        "request_id": REFERENCE,
        "execution_id": "22222222-2222-4222-8222-222222222222",
    }
    observed = row["observations"]
    observed.update(
        owner_receipt=receipt,
        owner_read=receipt,
        owner_principal={"user_id": subject["owner_id"], "org_id": doc["tenant_id"]},
        foreign_receipt_status=404,
        owner_operation={
            **receipt,
            "tool": row["probe"]["tool"],
            "arguments": row["probe"]["arguments"],
        },
        execution_cleanup={
            "execution_id": receipt["execution_id"],
            "state": "closed",
            "cleanup_status": "confirmed",
        },
    )
    assert authority.qualify_denial(row, manifest=doc)["status"] == "pass"
    for key, invalid in (
        ("foreign_receipt_status", 401),
        ("owner_read", {}),
        ("execution_cleanup", {}),
    ):
        changed = deepcopy(row)
        changed["observations"][key] = invalid
        with pytest.raises(ValueError):
            authority.qualify_denial(changed, manifest=doc)


@pytest.mark.parametrize("client", authority.CLIENTS)
def test_policy_denial_needs_exact_provider_error_and_separate_unchanged_readback(
    client,
):
    doc, row = denial_row(client)
    doc["clients"][client]["probes"]["policy_denied"] = row["probe"]
    row["variant"] = "policy_denied"
    policy_reply = {
        "result": {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "Access denied: Your organization admin has not authorized the read permission.",
                }
            ],
        }
    }
    packet = {
        "status": "confirmed",
        "result_status": "error",
        "operation_ref": REFERENCE,
        "execution_id": "22222222-2222-4222-8222-222222222222",
        "result": policy_reply,
    }
    set_native(row, packet)
    spec = doc["clients"][client]
    row["observations"].update(
        policy={
            **row["observations"]["before"],
            "owner_id": spec["owner_id"],
            "account_id": spec["grant"]["provider_account_id"],
            "content_sha256": None,
            "policy_reply": policy_reply,
        },
        execution_cleanup={
            "execution_id": packet["execution_id"],
            "state": "closed",
            "cleanup_status": "confirmed",
        },
    )
    assert authority.qualify_denial(row, manifest=doc)["status"] == "pass"
    for key in ("before", "after"):
        row["observations"][key]["content_sha256"] = None
    with pytest.raises(ValueError):
        authority.qualify_denial(row, manifest=doc)


def campaign_for(tmp_path, monkeypatch, doc=None):
    from contextlib import contextmanager

    path = bind(tmp_path, doc)
    campaign = authority.AuthorityCampaign(
        path,
        binding=BINDING,
        gateway_url=GATEWAY,
        account=ACCOUNT,
        region=REGION,
        reader_role=ROLE,
        client_factory=None,
    )
    native_calls = []
    installed = Path("/private/adp")

    def start(client):
        directory = path.parent / client / "runtime" / ".bedrock-gateway"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return (
            installed,
            {"HOME": str(directory.parent), "BG_CONFIG_DIR": str(directory)},
            {"adp": "e" * 64},
        )

    monkeypatch.setattr(campaign, "_start", start)

    def api(client, route):
        if route.endswith("/me"):
            return {
                "user_id": campaign.config["clients"][client]["owner_id"],
                "org_id": "tenant-a",
            }
        if route.endswith("/status"):
            return {"status": "ready", "connection": {"sites": ["site-a"]}}
        return {
            "user_id": "owner-a",
            "tenant_id": "tenant-a",
            "tools": [
                {"name": "atlassian.get_jira_issue", "ready": True},
                {"name": "atlassian.update_jira_issue", "reason": "not_allowed"},
            ],
        }

    monkeypatch.setattr(campaign, "_api", api)

    @contextmanager
    def client_factory(*args, **kwargs):
        yield SimpleNamespace(request=lambda *_: {"tools": []})

    campaign.client_factory = client_factory

    def native(client, installed, env, probe, spec):
        native_calls.append(probe)
        raw = with_call(transcript(client), client, probe["tool"], probe["arguments"])
        return raw, authority.native_observation(
            client,
            raw,
            tool=probe["tool"],
            arguments=probe["arguments"],
            bounds=spec["bounds"],
        )

    monkeypatch.setattr(authority, "invoke_native", native)

    def snapshot(grant, probe, **kwargs):
        return {
            "source": "direct-atlassian-provider",
            "owner_id": grant.owner_id,
            "account_id": grant.config["provider_account_id"],
            "sites": [
                {"site_id": probe["site_id"], "scopes": ["read:jira:agent-interface"]}
            ],
            "site_id": probe["site_id"],
            "provider_id": probe["provider_id"],
            "content_sha256": "d" * 64,
        }

    monkeypatch.setattr(authority, "provider_snapshot", snapshot)
    return campaign, native_calls, snapshot


def test_actual_campaign_resumes_post_native_read_without_reissuing_model_or_tool(
    tmp_path, monkeypatch
):
    campaign, calls, snapshot = campaign_for(tmp_path, monkeypatch)
    count = 0

    def read(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise ValueError("temporary readback failure after native call")
        return snapshot(*args, **kwargs)

    monkeypatch.setattr(authority, "provider_snapshot", read)
    first = campaign.run("claude-code", "foreign_site")
    assert (
        first["status"] == "pending" and first["phase"] == "invoked" and len(calls) == 1
    )
    # Reopen the campaign, losing all in-memory state.
    other, _, _ = campaign_for(tmp_path, monkeypatch)
    other.path, other.config = campaign.path, campaign.config
    monkeypatch.setattr(
        authority,
        "invoke_native",
        lambda *_: pytest.fail("reissued original native call"),
    )
    resumed = other.run("claude-code", "foreign_site")
    assert resumed["status"] == "pass" and resumed["phase"] == "verified"
    assert len(calls) == 1


def test_interrupted_native_call_is_retained_without_a_new_attempt(
    tmp_path, monkeypatch
):
    campaign, calls, _ = campaign_for(tmp_path, monkeypatch)

    def lost(*_):
        raise TimeoutError("native response lost")

    monkeypatch.setattr(authority, "invoke_native", lost)
    first = campaign.run("claude-code", "foreign_site")
    assert first["phase"] == "invoking" and first["status"] == "pending"
    monkeypatch.setattr(
        authority,
        "invoke_native",
        lambda *_: pytest.fail("replayed uncertain operation"),
    )
    assert campaign.run("claude-code", "foreign_site") == first


def owned_manifest():
    doc = manifest()
    spec = doc["clients"]["claude-code"]
    record = {
        "tenant_id": "tenant-a",
        "owner_id": "owner-a",
        "run_id": "101",
        "site_id": "site-a",
        "project_key": "ADP",
        "operation_id": "33333333-3333-4333-8333-333333333333",
        "operation_ids": ["33333333-3333-4333-8333-333333333333"],
        "marker": "adp-jira-" + "x" * 43,
        "issue_ids": ["10001"],
        "create_status": "confirmed",
        "cleanup_status": "pending",
    }
    cleanup = {
        "enabled": True,
        "tenant_id": "tenant-a",
        "site_id": "site-a",
        "project_key": "ADP",
        "delete_call": {
            "schema_sha256": "1" * 64,
            "arguments": {
                "cloudId": {"fixture": "site_id"},
                "issueIdOrKey": {"fixture": "provider_id"},
            },
        },
        "projects_call": {
            "schema_sha256": "2" * 64,
            "arguments": {"cloudId": {"fixture": "site_id"}},
        },
    }
    spec["owned_fixture"] = {"record": record, "cleanup": cleanup}
    spec["probes"]["read_only"] = {
        "tool": "atlassian___update_jira_issue",
        "arguments": {
            "cloudId": "site-a",
            "issueIdOrKey": "10001",
            "fields": {"summary": record["marker"] + " authority denial probe"},
        },
        "subject_client": "claude-code",
        "site_id": "site-a",
        "provider_id": "10001",
        "marker": "adp-8454-" + "a" * 16,
    }
    return doc


@pytest.mark.parametrize(
    "fault", ("foreign_subject", "unowned", "other_operation", "marker_loss")
)
def test_write_probe_rejects_unowned_or_ambiguous_fixture_before_any_effect(
    tmp_path, fault
):
    doc = owned_manifest()
    spec = doc["clients"]["claude-code"]
    if fault == "foreign_subject":
        spec["probes"]["read_only"]["subject_client"] = "codex"
    elif fault == "unowned":
        spec["owned_fixture"]["record"]["owner_id"] = "owner-b"
    elif fault == "other_operation":
        spec["owned_fixture"]["record"]["operation_ids"].append("uncertain")
    else:
        spec["probes"]["read_only"]["arguments"]["fields"]["summary"] = (
            "removed ownership marker"
        )
    with pytest.raises(ValueError):
        bind(tmp_path, doc)


@pytest.mark.parametrize("lose_after", (False, True))
def test_owned_write_denial_uses_real_ledger_cleanup_and_survives_readback_failure(
    tmp_path, monkeypatch, lose_after
):
    campaign, calls, snapshot = campaign_for(tmp_path, monkeypatch, owned_manifest())
    issues, deleted = {}, []

    class Provider:
        def __init__(self, *args, **kwargs):
            pass

        def bind(self, record):
            self.record = record

        def authorize(self):
            pass

        def read_issue(self, issue_id, **kwargs):
            return issues.get(issue_id)

        def read_issues(self, **kwargs):
            return {"complete": True, "issues": list(issues.values())}

        def delete_owned_issue(self, issue_id, **ownership):
            assert ownership == {
                k: self.record[k]
                for k in (
                    "tenant_id",
                    "owner_id",
                    "run_id",
                    "site_id",
                    "project_key",
                    "marker",
                )
            }
            deleted.append(issue_id)
            del issues[issue_id]

    record = campaign.config["clients"]["claude-code"]["owned_fixture"]["record"]
    issues["10001"] = {
        k: record[k]
        for k in ("tenant_id", "owner_id", "run_id", "site_id", "project_key", "marker")
    }
    issues["10001"]["id"] = "10001"
    monkeypatch.setattr(authority, "JiraFixtureProvider", Provider)
    n = 0

    def read(*args, **kwargs):
        nonlocal n
        n += 1
        if lose_after and n == 2:
            raise ValueError("lost independent read")
        return snapshot(*args, **kwargs)

    monkeypatch.setattr(authority, "provider_snapshot", read)
    result = campaign.run("claude-code", "read_only")
    assert deleted == ["10001"] and not issues and len(calls) == 1
    if lose_after:
        assert result["status"] == "pending"
        # A read after deletion cannot reconstruct the missing pre-cleanup
        # observation. Keep this acceptance row pending, without replaying writes.
        assert "after" not in result["observations"]
    else:
        assert result["status"] == "pass", result
        assert (
            result["observations"]["fixture_cleanup"]["record"]["cleanup_status"]
            == "confirmed"
        )


@pytest.mark.parametrize("initial_unknown", [False, True])
def test_foreign_owner_campaign_creates_one_original_receipt_and_closes_as_owner(
    tmp_path, monkeypatch, initial_unknown
):
    doc = manifest()
    spec = doc["clients"]["claude-code"]
    spec["probes"]["foreign_owner"] = deepcopy(spec["probes"]["foreign_site"])
    campaign, calls, _ = campaign_for(tmp_path, monkeypatch, doc)
    receipt = {
        "operation_ref": REFERENCE,
        "request_id": REFERENCE,
        "execution_id": "22222222-2222-4222-8222-222222222222",
        "status": "confirmed",
        "result_status": "success",
        "result": {"fixture": "owned"},
    }
    mcp_calls = []

    def mcp(client, tool, arguments):
        mcp_calls.append((client, tool, arguments))
        assert client == "codex"
        if tool == "adp___execution_close":
            return {
                "execution_id": receipt["execution_id"],
                "state": "closed",
                "cleanup_status": "confirmed",
            }
        _, env, _ = campaign._start(client)
        directory = Path(env["BG_CONFIG_DIR"]) / "tool-operations"
        directory.mkdir(mode=0o700, exist_ok=True)
        authority._write(
            directory / (REFERENCE + ".json"),
            {
                **receipt,
                "tool": spec["probes"]["foreign_owner"]["tool"],
                "arguments": spec["probes"]["foreign_owner"]["arguments"],
                "binding": {"mode": "bearer"},
            },
        )
        if initial_unknown and tool != "adp___operation_status":
            return {"operation_ref": REFERENCE, "status": "unknown"}
        return receipt

    monkeypatch.setattr(campaign, "_mcp", mcp)
    api = campaign._api
    monkeypatch.setattr(
        campaign,
        "_api",
        lambda client, path: receipt if "/operations/" in path else api(client, path),
    )
    monkeypatch.setattr(campaign, "_http", lambda *_: SimpleNamespace(status_code=404))

    def native(client, installed, environ, probe, spec):
        calls.append(probe)
        assert probe == {
            "tool": "adp___operation_status",
            "arguments": {"operation_ref": REFERENCE},
        }
        copied = json.loads(
            (
                Path(environ["BG_CONFIG_DIR"])
                / "tool-operations"
                / (REFERENCE + ".json")
            ).read_text()
        )
        assert copied["request_id"] == REFERENCE and copied["tool"] == TOOL
        raw = with_call(
            transcript(client, response(message=authority.OWNER_DENIAL)),
            client,
            probe["tool"],
            probe["arguments"],
        )
        return raw, authority.native_observation(
            client,
            raw,
            tool=probe["tool"],
            arguments=probe["arguments"],
            bounds=spec["bounds"],
        )

    monkeypatch.setattr(authority, "invoke_native", native)
    result = campaign.run("claude-code", "foreign_owner")
    if initial_unknown:
        assert result["status"] == "pending"
        result = campaign.run("claude-code", "foreign_owner")
    assert result["status"] == "pass", result
    assert len(calls) == 1 and len(mcp_calls) == (3 if initial_unknown else 2)
    assert sum(tool == TOOL for _, tool, _ in mcp_calls) == 1
    assert campaign.run("claude-code", "foreign_owner") == result
    assert len(calls) == 1 and len(mcp_calls) == (3 if initial_unknown else 2)


def test_provider_snapshot_captures_exact_mcp_policy_denial_without_inventing_content(
    monkeypatch,
):
    reply = {
        "result": {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "Access denied: Your organization admin has not authorized the read permission.",
                }
            ],
        }
    }
    transport = SimpleNamespace(send=lambda *_: reply)
    session = SimpleNamespace(token="never-record", transport=transport)

    def get(_):
        session.transport.send("tools/call", {"name": "getJiraIssue"}, session.token)
        raise ValueError("normalized provider error")

    session.get = get
    grant = SimpleNamespace(
        owner_id="owner-a",
        config={
            "provider_account_id": "provider-a",
            "subject_schema_sha256": "a" * 64,
            "sites_schema_sha256": "b" * 64,
        },
        with_access_token=lambda _, callback: callback(session),
    )
    monkeypatch.setattr(
        authority,
        "collect_observation",
        lambda *_: (
            "provider-a",
            [SimpleNamespace(site_id="site-b", scopes=["read:jira:agent-interface"])],
            None,
        ),
    )
    probe = manifest()["clients"]["claude-code"]["probes"]["foreign_site"]
    value = authority.provider_snapshot(grant, probe, capture_policy=True)
    assert value["content_sha256"] is None and value["policy_reply"] == reply
    assert "never-record" not in json.dumps(value) and session.transport is transport
    with pytest.raises(ValueError):
        authority.provider_snapshot(grant, probe)


def test_unexpected_successful_read_is_closed_but_never_counts_as_denial(
    tmp_path, monkeypatch
):
    campaign, calls, _ = campaign_for(tmp_path, monkeypatch)
    packet = {
        "operation_ref": REFERENCE,
        "execution_id": "22222222-2222-4222-8222-222222222222",
        "status": "confirmed",
        "result_status": "success",
    }

    def native(client, installed, environ, probe, spec):
        calls.append(probe)
        raw = with_call(
            transcript(client, packet), client, probe["tool"], probe["arguments"]
        )
        return raw, authority.native_observation(
            client,
            raw,
            tool=probe["tool"],
            arguments=probe["arguments"],
            bounds=spec["bounds"],
        )

    monkeypatch.setattr(authority, "invoke_native", native)
    closes = []

    def mcp(client, tool, arguments):
        closes.append((client, tool, arguments))
        return {
            "execution_id": packet["execution_id"],
            "state": "closed",
            "cleanup_status": "confirmed",
        }

    monkeypatch.setattr(campaign, "_mcp", mcp)
    row = campaign.run("claude-code", "foreign_site")
    assert row["status"] == "pending" and len(closes) == 1 and len(calls) == 1
    campaign.run("claude-code", "foreign_site")
    assert len(closes) == 1 and len(calls) == 1


def test_independent_authority_reader_rejects_status_promotion_and_missing_inventory(
    tmp_path, monkeypatch
):
    doc = manifest()
    for spec in doc["clients"].values():
        spec["probes"] = dict.fromkeys(authority.CASES)
    campaign, _, _ = campaign_for(tmp_path, monkeypatch, doc)
    report = authority.collect_authority(campaign)
    result = authority.qualify_authority(report, manifest=doc)
    assert result["status"] == "pending" and len(result["cases"]) == 16
    for mode in ("missing", "duplicate", "status", "refresh_pass"):
        modified = deepcopy(report)
        if mode == "missing":
            modified["cases"].pop()
        elif mode == "duplicate":
            modified["cases"][-1] = modified["cases"][0]
        elif mode == "status":
            modified["status"] = "pass"
        else:
            next(
                row for row in modified["cases"] if row["variant"] == "identity_refresh"
            )["status"] = "pass"
        with pytest.raises(ValueError):
            authority.qualify_authority(modified, manifest=doc)


def test_runtime_reopens_original_installation_and_excludes_ambient_credentials(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager

    path = bind(tmp_path)
    calls = []

    @contextmanager
    def installed(url, revision, *, recovery_root, recovery_id):
        calls.append(("install", recovery_id))
        home = recovery_root / recovery_id
        home.mkdir(mode=0o700)
        yield (
            home / "adp",
            {
                "HOME": str(home),
                "AWS_ACCESS_KEY_ID": "private",
                "OPENAI_API_KEY": "private",
            },
            {"adp": "a" * 64},
        )

    @contextmanager
    def resumed(url, revision, *, recovery_root, recovery_id):
        calls.append(("resume", recovery_id))
        yield (
            recovery_root / recovery_id / "adp",
            {"HOME": str(recovery_root / recovery_id)},
            {"adp": "a" * 64},
        )

    monkeypatch.setattr(authority, "installed_clients", installed)
    monkeypatch.setattr(authority, "resume_installed_clients", resumed)
    monkeypatch.setattr(authority, "import_session", lambda *_: None)
    monkeypatch.setattr(authority, "configure_model_transport", lambda *_: None)

    def campaign():
        return authority.AuthorityCampaign(
            path,
            binding=BINDING,
            gateway_url=GATEWAY,
            account=ACCOUNT,
            region=REGION,
            reader_role=ROLE,
            client_factory=None,
        )

    first = campaign()
    _, environment, _ = first._start("claude-code")
    assert (
        "AWS_ACCESS_KEY_ID" not in environment and "OPENAI_API_KEY" not in environment
    )
    first.close()
    second = campaign()
    second._start("claude-code")
    second.close()
    assert [call[0] for call in calls] == ["install", "resume"] and calls[0][
        1
    ] == calls[1][1]
