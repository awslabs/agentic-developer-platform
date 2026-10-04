"""Offline assistant harness contracts; Unix socketpairs only, no network or cloud."""

import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError

import pytest

from tests.e2e.cli_uplift import (
    assistant_oracles,
    bundle,
    cases,
    cleanup,
    config,
    fixtures,
    live,
    preflight,
    runner,
    stages,
    statestore,
)
from tests.e2e.cli_uplift.remote import assistant_client, assistant_process, common


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Offline assistant tests must not use the network")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def users():
    return {
        label: {
            "login_user_id": label,
            "canonical_user_id": label + "-canonical",
            "tenant_id": "tenant-a" if label != "b1" else "tenant-b",
            "fixture_name": "synthetic/assistant/" + label,
        }
        for label in ("a1", "a2", "b1")
    }


def test_assistant_selection_and_missing_fixture_never_accept(users):
    assert [case.id for case in cases.suite_cases("assistant")] == [
        f"E{index}" for index in range(43, 51)
    ]
    assert set(cases.suite_cases("assistant")).isdisjoint(cases.suite_cases("nightly"))
    assert stages.JOURNEY_DRIVERS["E43"] == "assistant_stream"
    matrix = cases.new_matrix(("assistant",))
    assert (
        len(
            cases.block_missing_fixtures(
                matrix, {cases.EC2, cases.COGNITO, cases.PLATFORM}
            )
        )
        == 8
    )
    assert all(row["status"] == cases.BLOCKED for row in matrix.values())
    assert cases.is_full(("assistant",)) is False
    assert cases.accept(matrix, ("assistant",), cleanup_ok=True)[0] != cases.PASSED
    fixtures.validate_fixture("assistant_users", users)
    assert cases.ASSISTANT_USERS in config.fixture_classes(
        {"assistant_users": users, "websocket_url": "wss://example.invalid/ws"}
    )
    assert cases.ASSISTANT_USERS not in preflight.evaluate_fixtures(
        {"assistant_users": users}
    )
    assert cases.ASSISTANT_USERS in preflight.evaluate_fixtures(
        {"assistant_users": users, "websocket_url": "wss://example.invalid/ws"}
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda users: users.pop("a2"),
        lambda users: users["b1"].update(tenant_id="tenant-a"),
        lambda users: users["a2"].update(canonical_user_id="a1-canonical"),
        lambda users: users["b1"].update(fixture_name="synthetic/assistant/a1"),
        lambda users: users["a1"].update(password="not-allowed"),
        lambda users: users["a1"].update(fixture_name="https://bad.example"),
    ],
)
def test_assistant_fixture_refuses_unsafe_identity(users, change):
    change(users)
    with pytest.raises(config.ConfigError):
        fixtures.validate_fixture("assistant_users", users)


@pytest.fixture(
    params=[
        "wss://example.invalid:not-a-port/ws",
        "wss://example.invalid:99999/ws",
        "wss://example.invalid:-1/ws",
        "wss://example.invalid:0/ws",
        "wss://example.invalid:/ws",
        "wss://[broken/ws",
        "wss://[2001:db8::1]suffix/ws",
        "wss://[2001:db8::1]:99999/ws",
        "https://example.invalid/ws",
        "wss:///ws",
        "wss://private-marker:private-marker@example.invalid/ws",
        "wss://@example.invalid/ws",
        "wss://example.invalid/ws?token=private-marker",
        "wss://example.invalid/ws#private-marker",
        "wss://example.invalid/ws?",
        "wss://example.invalid/ws#",
        "wss://exa mple.invalid/ws",
        "wss://example.invalid\n/ws",
        "\x00wss://example.invalid/ws",
        "wss://example.invalid\uff1aprivate-marker/ws",
        443,
        [],
    ]
)
def invalid_assistant_websocket_url(request):
    return request.param


def test_assistant_config_rejects_malformed_websocket_target(
    users, invalid_assistant_websocket_url
):
    from tests.unit.test_cli_uplift import config_fixture

    supplied = config_fixture(
        assistant_users=users, websocket_url=invalid_assistant_websocket_url
    )
    with pytest.raises(config.ConfigError, match="websocket_url") as failure:
        config.validate(supplied)
    assert "private-marker" not in str(failure.value)
    assert "wss://" in str(failure.value)
    assert "port" in str(failure.value)


def test_malformed_websocket_target_cannot_enable_assistant_fixtures(
    users, invalid_assistant_websocket_url
):
    supplied = {
        "assistant_users": users,
        "websocket_url": invalid_assistant_websocket_url,
    }
    assert cases.ASSISTANT_USERS not in config.fixture_classes(supplied)
    assert cases.ASSISTANT_USERS not in preflight.evaluate_fixtures(supplied)


@pytest.mark.parametrize(
    "target",
    [
        "wss://example.invalid/ws",
        "wss://example.invalid:443/ws",
        "wss://example.invalid:1/ws",
        "wss://example.invalid:65535/ws",
        "wss://example.invalid:00443/ws",
        "wss://example.invalid/@assistant",
        "wss://[2001:db8::1]/ws",
        "wss://[2001:db8::1]:8443/ws",
        "WSS://EXAMPLE.invalid/ws",
    ],
)
def test_valid_websocket_targets_enable_assistant_fixtures(users, target):
    from tests.unit.test_cli_uplift import config_fixture

    validated = config.validate(
        config_fixture(assistant_users=users, websocket_url=target)
    )
    assert validated["websocket_url"] == target
    assert cases.ASSISTANT_USERS in config.fixture_classes(validated)
    assert cases.ASSISTANT_USERS in preflight.evaluate_fixtures(validated)


@pytest.mark.parametrize("target", [None, ""])
def test_absent_websocket_target_keeps_assistant_fixtures_blocked(users, target):
    from tests.unit.test_cli_uplift import config_fixture

    validated = config.validate(
        config_fixture(assistant_users=users, websocket_url=target)
    )
    assert cases.ASSISTANT_USERS not in config.fixture_classes(validated)


def events():
    result = []
    for cursor, kind in enumerate(
        ("acknowledged", "queued", "starting", "running", "tool", "answer", "completed")
    ):
        event = {
            "cursor": cursor,
            "request_id": "request-1",
            "session_id": "session-1",
            "type": kind,
        }
        if kind == "acknowledged":
            event["durable"] = True
        if kind == "tool":
            event["tool_id"] = "tool-1"
        if kind == "answer":
            event["text"] = "synthetic answer"
        result.append(event)
    return result


def test_stream_checks_order_replay_and_secret():
    assert (
        assistant_oracles.stream(
            events(), request_id="request-1", canary="synthetic-secret"
        )["last_cursor"]
        == 6
    )
    for altered in (events()[:-1], events() + [events()[0]], list(reversed(events()))):
        with pytest.raises(assistant_oracles.EvidenceError):
            assistant_oracles.stream(
                altered, request_id="request-1", canary="synthetic-secret"
            )
    repeated = events()
    extra_tool = {**repeated[4], "cursor": 5, "tool_id": "tool-2"}
    repeated.insert(5, extra_tool)
    for index, event in enumerate(repeated):
        event["cursor"] = index
    assert (
        assistant_oracles.stream(
            repeated, request_id="request-1", canary="synthetic-secret"
        )["event_count"]
        == 8
    )
    repeated[5]["tool_id"] = "tool-1"
    with pytest.raises(assistant_oracles.EvidenceError, match="duplicated"):
        assistant_oracles.stream(
            repeated, request_id="request-1", canary="synthetic-secret"
        )
    leaked = events()
    leaked[5]["text"] = "synthetic-secret"
    with pytest.raises(assistant_oracles.EvidenceError, match="leaked"):
        assistant_oracles.stream(
            leaked, request_id="request-1", canary="synthetic-secret"
        )
    foreign = events()
    foreign[4]["session_id"] = "foreign"
    with pytest.raises(assistant_oracles.EvidenceError):
        assistant_oracles.stream(
            foreign, request_id="request-1", canary="synthetic-secret"
        )


def pages():
    return [
        {
            "status": "complete",
            "cursor": None,
            "next_cursor": "page-2",
            "records": [{"id": "activity-1", "timestamp": "2026-01-01T10:00:00+00:00"}],
            "citations": ["activity-1"],
        },
        {
            "status": "complete",
            "cursor": "page-2",
            "next_cursor": None,
            "records": [{"id": "activity-2", "timestamp": "2026-01-01T11:00:00+00:00"}],
            "citations": ["activity-2"],
        },
    ]


def source_fixture():
    return {
        "expected_timestamps": {
            "activity-1": "2026-01-01T10:00:00+00:00",
            "activity-2": "2026-01-01T11:00:00+00:00",
        },
        "window": {
            "start": "2026-01-01T10:00:00+00:00",
            "end": "2026-01-01T12:00:00+00:00",
            "timezone": "UTC",
        },
    }


def test_sources_accept_authorized_pagination_and_empty():
    assert (
        assistant_oracles.sources(
            pages(),
            expected_ids={"activity-1", "activity-2"},
            allowed_ids={"activity-1", "activity-2"},
            canary="synthetic-secret",
            **source_fixture(),
        )["source_count"]
        == 2
    )
    assert (
        assistant_oracles.sources(
            [{"status": "empty", "cursor": None, "next_cursor": None, "records": []}],
            expected_ids=set(),
            allowed_ids=set(),
            canary="synthetic-secret",
            expected_timestamps={},
            window=source_fixture()["window"],
        )["source_count"]
        == 0
    )


@pytest.mark.parametrize(
    "alter",
    [
        lambda value: value[0]["citations"].append("incorrect"),
        lambda value: value[1]["records"][0].update(id="foreign"),
        lambda value: value[1].update(status="partial"),
        lambda value: value[1].update(next_cursor="page-3"),
        lambda value: value[0]["records"][0].update(timestamp="2026-01-01T10:00:00"),
        lambda value: value[0]["records"][0].update(id="activity-2"),
        lambda value: value[0]["records"][0].update(detail="synthetic-secret"),
    ],
)
def test_sources_refuse_wrong_or_incomplete_evidence(alter):
    value = pages()
    alter(value)
    with pytest.raises(assistant_oracles.EvidenceError):
        assistant_oracles.sources(
            value,
            expected_ids={"activity-1", "activity-2"},
            allowed_ids={"activity-1", "activity-2"},
            canary="synthetic-secret",
            **source_fixture(),
        )


@pytest.mark.parametrize(
    "alter",
    [
        pytest.param(
            lambda value: (
                value[0].update(next_cursor=None),
                value[1].update(cursor=None),
            ),
            id="pages-after-end",
        ),
        pytest.param(
            lambda value: value.insert(
                1,
                {
                    "status": "complete",
                    "cursor": "page-2",
                    "next_cursor": "page-2",
                    "records": [],
                },
            ),
            id="reused-cursor",
        ),
        pytest.param(
            lambda value: (
                value[0].update(next_cursor=""),
                value[1].update(cursor=""),
            ),
            id="blank-cursor",
        ),
        pytest.param(
            lambda value: (
                value[0].update(next_cursor=True),
                value[1].update(cursor=True),
            ),
            id="non-string-cursor",
        ),
        pytest.param(
            lambda value: value[0].update(status="empty"),
            id="empty-page-with-records",
        ),
        pytest.param(
            lambda value: value[0].update(citations={"activity-1": "not-a-list"}),
            id="non-list-citations",
        ),
        pytest.param(
            lambda value: value[0].update(records=None),
            id="non-list-records",
        ),
    ],
)
def test_source_pagination_defects_fail_oracle_and_case(alter):
    source_pages = pages()
    alter(source_pages)
    evidence = {
        "success": True,
        "pages": source_pages,
        "expected_ids": ["activity-1", "activity-2"],
        "allowed_ids": ["activity-1", "activity-2"],
        "canary": "synthetic-secret",
        **source_fixture(),
    }
    with pytest.raises(assistant_oracles.EvidenceError):
        assistant_oracles.sources(
            source_pages,
            **{
                key: value
                for key, value in evidence.items()
                if key not in {"success", "pages"}
            },
        )
    matrix = cases.new_matrix(("assistant",))
    context = {
        "document": {"instance_id": "i-synthetic"},
        "matrix": matrix,
        "transcript": [],
        "correlation": {},
        "manifest": None,
        "fault": "none",
        "record": lambda case_id, status, detail: cases.record(
            matrix, case_id, status, detail
        ),
    }
    stages.journeys_stage(
        {},
        {
            "journey": lambda purpose: (
                (lambda instance, ctx: evidence)
                if purpose == "assistant_sources"
                else None
            )
        },
    )(context)
    assert matrix["E44"]["status"] == cases.FAILED
    assert matrix["E44"]["detail"]["oracle_error"]
    assert cases.accept(matrix, ("assistant",), cleanup_ok=True)[0] != cases.PASSED


def test_sources_accept_empty_intermediate_page_with_fresh_cursor():
    source_pages = pages()
    source_pages.insert(
        1,
        {
            "status": "complete",
            "cursor": "page-2",
            "next_cursor": "page-3",
            "records": [],
            "citations": [],
        },
    )
    source_pages[2]["cursor"] = "page-3"
    observed = assistant_oracles.sources(
        source_pages,
        expected_ids={"activity-1", "activity-2"},
        allowed_ids={"activity-1", "activity-2"},
        canary="synthetic-secret",
        **source_fixture(),
    )
    assert observed["source_count"] == observed["citation_count"] == 2
    assert observed["page_count"] == 3


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_http_adapter_authenticates_without_identity_headers():
    session = assistant_client.UserSession(
        {"access_token": "ordinary", "id_token": "identity", "expires_at": 200},
        clock=lambda: 100,
    )

    def read(request, timeout):
        assert timeout == 15
        assert dict(request.header_items()) == {
            "Authorization": "Bearer ordinary",
            "Accept": "application/json",
        }
        return Response(b'{"results": []}')

    assert assistant_client.http_json(
        "https://example.invalid/assistant/history", session, opener=read
    ) == {"results": []}
    assert (
        assistant_client.websocket_url("wss://example.invalid/assistant", session)
        == "wss://example.invalid/assistant?token=identity"
    )
    with pytest.raises(assistant_client.ClientError):
        assistant_client.http_json("http://example.invalid", session, opener=read)


def test_expired_user_and_denied_http_request():
    session = assistant_client.UserSession(
        {"access_token": "old", "id_token": "old", "expires_at": 100}, clock=lambda: 101
    )
    with pytest.raises(assistant_client.ClientError, match="expired"):
        assistant_client.http_json(
            "https://example.invalid/assistant",
            session,
            opener=lambda *_args, **_kwargs: None,
        )
    session.refresh = lambda previous: {
        "access_token": "new",
        "id_token": "new",
        "expires_at": 200,
    }
    assert session.token() == "new"
    session.refresh = lambda previous: {"access_token": "stale", "expires_at": 120}
    session.tokens["expires_at"] = 100
    with pytest.raises(assistant_client.ClientError, match="refresh"):
        session.token()
    session.tokens["expires_at"] = 200

    def denied(request, timeout):
        raise HTTPError(request.full_url, 403, "denied", {}, None)

    with pytest.raises(assistant_client.ClientError, match="403"):
        assistant_client.http_json(
            "https://example.invalid/assistant", session, opener=denied
        )


class Socket:
    def settimeout(self, timeout):
        assert 0 < timeout <= 15

    def __init__(self, data=b""):
        self.data = data
        self.sent = b""
        self.closed = False

    def recv(self, length):
        result, self.data = self.data[:length], self.data[length:]
        return result

    def sendall(self, payload):
        self.sent += payload

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "status_line",
    ["HTTP/1.1 101 Switching Protocols", "HTTP/1.1 101 Custom reason"],
)
@pytest.mark.parametrize(
    "upgrade_headers",
    [
        "Upgrade: websocket\r\nConnection: Upgrade\r\n",
        "uPgRaDe: WebSocket\r\ncOnNeCtIoN: keep-alive, Upgrade\r\n",
        "Upgrade: websocket\r\nConnection: keep-alive\r\nConnection: Upgrade\r\n",
    ],
)
def test_websocket_frames_and_upgrade(monkeypatch, status_line, upgrade_headers):
    import base64
    import hashlib

    monkeypatch.setattr(assistant_client.os, "urandom", lambda size: b"a" * size)
    key = base64.b64encode(b"a" * 16).decode()
    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    event = json.dumps({"type": "acknowledged"}).encode()
    sock = Socket(
        (
            status_line
            + "\r\n"
            + upgrade_headers
            + "Sec-WebSocket-Accept: "
            + accept
            + "\r\n\r\n"
        ).encode()
        + bytes([0x81, len(event)])
        + event
    )

    class TLS:
        def wrap_socket(self, connection, server_hostname):
            assert connection is sock and server_hostname == "example.invalid"
            return connection

    session = assistant_client.UserSession(
        {"id_token": "identity", "expires_at": 200}, clock=lambda: 100
    )
    ws = assistant_client.connect(
        "wss://example.invalid/assistant",
        session,
        dial=lambda *_args, **_kwargs: sock,
        tls=TLS,
    )
    assert b"?token=identity" in sock.sent
    ws.send({"action": "message", "text": "hello"})
    assert b"Authorization:" not in sock.sent and b"X-User:" not in sock.sent
    assert ws.receive() == {"type": "acknowledged"}
    ws.close()
    assert sock.closed


@pytest.mark.parametrize(
    "upgrade_headers",
    [
        "",
        "Upgrade: websocket\r\n",
        "Connection: Upgrade\r\n",
        "Upgrade: h2c\r\nConnection: Upgrade\r\n",
        "Upgrade: websocket\r\nConnection: keep-alive\r\n",
        "Upgrade: websocket\r\nConnection: not-upgrade\r\n",
        (
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Extensions: permessage-deflate\r\n"
        ),
        (
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Protocol: unexpected\r\n"
        ),
        (
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Accept: wrong\r\n"
        ),
        "Upgrade: websocket\r\nConnection: Upgrade\r\nmalformed header\r\n",
        "Upgrade: websocket\r\nConnection: Upgrade\r\nInvalid: \xff\r\n",
    ],
)
def test_websocket_rejects_invalid_upgrade(monkeypatch, upgrade_headers):
    import base64
    import hashlib

    monkeypatch.setattr(assistant_client.os, "urandom", lambda size: b"a" * size)
    key = base64.b64encode(b"a" * 16).decode()
    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    sock = Socket(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            + upgrade_headers
            + "Sec-WebSocket-Accept: "
            + accept
            + "\r\n\r\n"
        ).encode()
    )

    class TLS:
        def wrap_socket(self, connection, server_hostname):
            return connection

    session = assistant_client.UserSession(
        {"id_token": "ordinary", "expires_at": 200}, clock=lambda: 100
    )
    with pytest.raises(assistant_client.ClientError, match="upgrade"):
        assistant_client.connect(
            "wss://example.invalid/ws",
            session,
            dial=lambda *_args, **_kwargs: sock,
            tls=TLS,
        )
    assert sock.closed


def test_ordinary_client_environment_has_no_controller_identity():
    inherited = {
        "PATH": "/usr/bin",
        "AWS_ACCESS_KEY_ID": "controller",
        "AWS_SESSION_TOKEN": "controller",
        "AWS_PROFILE": "admin",
        "ANTHROPIC_API_KEY": "provider",
        "ADP_INTERNAL_TOKEN": "internal",
    }
    assert assistant_client.ordinary_env("/tmp/ordinary", inherited) == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp/ordinary",
        "LANG": "C.UTF-8",
        "AWS_EC2_METADATA_DISABLED": "true",
    }


def test_ordinary_client_cannot_call_fault_route_or_follow_redirect():
    session = assistant_client.UserSession(
        {"access_token": "ordinary", "expires_at": 200}, clock=lambda: 100
    )
    with pytest.raises(assistant_client.ClientError, match="privileged"):
        assistant_client.http_json("https://example.invalid/internal/fault", session)

    def redirect(request, timeout):
        raise HTTPError(
            request.full_url,
            302,
            "moved",
            {"Location": "https://foreign.invalid"},
            None,
        )

    with pytest.raises(assistant_client.ClientError, match="302"):
        assistant_client.http_json(
            "https://example.invalid/history", session, opener=redirect
        )


@pytest.mark.parametrize("invalid", (False, True))
def test_assistant_stage_grades_injected_evidence_without_faking_qualification(invalid):
    matrix = cases.new_matrix(("assistant",))
    context = {
        "document": {"instance_id": "i-synthetic"},
        "matrix": matrix,
        "transcript": [],
        "correlation": {},
        "manifest": None,
        "fault": "none",
        "record": lambda case_id, status, detail: cases.record(
            matrix, case_id, status, detail
        ),
    }
    stream_events = events()
    source_pages = pages()
    if invalid:
        stream_events[3]["cursor"] = 0
        source_pages[0]["citations"] = ["wrong"]

    def resolve(purpose):
        if purpose == "assistant_stream":
            return lambda instance, ctx: {
                "success": True,
                "events": stream_events,
                "request_id": "request-1",
                "canary": "synthetic-secret",
            }
        if purpose == "assistant_sources":
            return lambda instance, ctx: {
                "success": True,
                "pages": source_pages,
                **source_fixture(),
                "expected_ids": {"activity-1", "activity-2"},
                "allowed_ids": {"activity-1", "activity-2"},
                "canary": "synthetic-secret",
            }
        return None

    stages.journeys_stage({}, {"journey": resolve})(context)
    for identifier in ("E43", "E44"):
        assert matrix[identifier]["status"] == (
            cases.FAILED if invalid else cases.PASSED
        )
        if invalid:
            assert matrix[identifier]["detail"]["oracle_error"]
    for identifier in (f"E{index}" for index in range(45, 50)):
        assert matrix[identifier]["status"] == cases.FAILED
        assert matrix[identifier]["detail"]["unimplemented"] is True
    assert cases.accept(matrix, ("assistant",), cleanup_ok=True)[0] != cases.PASSED


@pytest.mark.parametrize("case_id", ["E43", "E44"])
@pytest.mark.parametrize("initial_success", [False, True])
@pytest.mark.parametrize(
    "leak_location",
    [
        None,
        "credential",
        "nested",
        "text",
        "key",
        "jwt",
        "metadata",
        "detail",
        "transcript",
        "correlation",
    ],
)
def test_assistant_emit_rejects_raw_canary_leaks_before_grading(
    tmp_path, capsys, case_id, initial_success, leak_location
):
    canary = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature"
        if leak_location == "jwt"
        else "synthetic-secret"
    )
    observation_leaked = leak_location in {"credential", "nested", "text", "key", "jwt"}
    expected_success = initial_success and not observation_leaked
    evidence = {"success": initial_success, "canary": canary}
    if case_id == "E43":
        evidence.update(events=events(), request_id="request-1")
        observed = evidence["events"][5]
        sensitive_key = "secret"
    else:
        evidence.update(
            pages=pages(),
            expected_ids=["activity-1", "activity-2"],
            allowed_ids=["activity-1", "activity-2"],
            **source_fixture(),
        )
        observed = evidence["pages"][0]["records"][0]
        sensitive_key = "password"
    observed[sensitive_key] = "ordinary-credential"
    if leak_location == "credential":
        observed[sensitive_key] = canary
    elif leak_location == "nested":
        observed[sensitive_key] = {"records": [{"text": canary}]}
    elif leak_location in {"text", "jwt"}:
        observed["text"] = canary
    elif leak_location == "key":
        observed[canary] = "leaked key"
    if observation_leaked:
        evidence.update(
            detail={"note": canary, "other_check": True},
            transcript=[canary],
            correlation={canary: [canary]},
        )
    elif leak_location is not None:
        evidence[leak_location] = (
            [canary, {"nested": canary}]
            if leak_location == "transcript"
            else {"note": canary, canary: [canary]}
        )
    evidence["resources"] = [["adp_connection", "owned-resource"]]
    original = json.dumps(evidence, sort_keys=True)

    exit_code = common.emit(evidence)
    emitted = capsys.readouterr().out
    delivered = json.loads(emitted)
    assert exit_code == int(not expected_success)
    assert canary not in emitted
    assert "canary" not in delivered
    assert delivered["canary_check"] == ("failed" if observation_leaked else "passed")
    assert delivered["success"] is expected_success
    assert "ordinary-credential" not in emitted
    assert delivered["resources"] == evidence["resources"]
    assert json.dumps(evidence, sort_keys=True) == original
    if observation_leaked:
        assert delivered["detail"]["other_check"] is True
        assert "Synthetic secret leaked" in delivered["detail"]["oracle_error"]

    matrix = cases.new_matrix(("assistant",))
    for identifier in matrix:
        cases.record(matrix, identifier, cases.PASSED, {})
    cases.record(matrix, case_id, cases.NOT_RUN, {})
    context = {
        "document": {"instance_id": "i-synthetic"},
        "matrix": matrix,
        "transcript": [],
        "correlation": {},
        "manifest": cleanup.Manifest(tmp_path / "inventory.json", "synthetic"),
        "fault": "none",
        "record": lambda identifier, status, detail: cases.record(
            matrix, identifier, status, detail
        ),
    }
    stages.journeys_stage({}, {"journey": lambda _purpose: lambda *_args: delivered})(
        context
    )
    assert matrix[case_id]["status"] == (
        cases.PASSED if expected_success else cases.FAILED
    )
    assert cases.accept(matrix, ("assistant",), cleanup_ok=True)[0] == (
        cases.PASSED if expected_success else cases.FAILED
    )
    if observation_leaked:
        assert "Synthetic secret leaked" in matrix[case_id]["detail"]["oracle_error"]


@pytest.mark.parametrize("canary", [None, "", 123, "<redacted>"])
def test_assistant_emit_cannot_attest_without_a_valid_canary(capsys, canary):
    evidence = {
        "success": True,
        "canary": canary,
        "canary_check": "passed",
        "events": events(),
    }
    assert common.emit(evidence) == 1
    delivered = json.loads(capsys.readouterr().out)
    assert "canary" not in delivered
    assert delivered["success"] is False
    assert delivered["canary_check"] == "missing"
    assert "canary" in delivered["detail"]["oracle_error"]


@pytest.mark.parametrize("check", [None, "failed", "missing", True, "unknown"])
def test_assistant_oracles_require_pre_redaction_check_without_canary(check):
    with pytest.raises(assistant_oracles.EvidenceError, match="canary"):
        assistant_oracles.stream(
            events(), request_id="request-1", canary=None, canary_check=check
        )
    with pytest.raises(assistant_oracles.EvidenceError, match="canary"):
        assistant_oracles.sources(
            pages(),
            expected_ids=["activity-1", "activity-2"],
            allowed_ids=["activity-1", "activity-2"],
            canary=None,
            canary_check=check,
            **source_fixture(),
        )


@pytest.mark.parametrize("case_id", ["E43", "E44"])
def test_assistant_redacted_observations_still_require_structural_checks(
    capsys, case_id
):
    evidence = {"success": True, "canary": "synthetic-secret"}
    if case_id == "E43":
        evidence.update(events=events(), request_id="request-1")
        evidence["events"][3]["cursor"] = 0
    else:
        evidence.update(
            pages=pages(),
            expected_ids=["activity-1", "activity-2"],
            allowed_ids=["activity-1", "activity-2"],
            **source_fixture(),
        )
        evidence["pages"][0]["citations"] = ["wrong"]
    assert common.emit(evidence) == 0
    delivered = json.loads(capsys.readouterr().out)
    assert delivered.pop("canary_check") == "passed"
    delivered.pop("success")
    with pytest.raises(assistant_oracles.EvidenceError):
        if case_id == "E43":
            assistant_oracles.stream(**delivered, canary=None, canary_check="passed")
        else:
            assistant_oracles.sources(**delivered, canary=None, canary_check="passed")


def test_interrupted_assistant_matrix_remains_incomplete_after_resume():
    matrix = cases.new_matrix(("assistant",))
    cases.record(matrix, "E43", cases.PASSED, {"last_cursor": 6})
    resumed = json.loads(json.dumps(matrix))
    assert resumed["E43"]["detail"]["last_cursor"] == 6
    assert resumed["E44"]["status"] == cases.NOT_RUN
    assert cases.accept(resumed, ("assistant",), cleanup_ok=True)[0] == cases.FAILED
    for identifier in (f"E{number}" for number in range(44, 50)):
        cases.record(resumed, identifier, cases.PASSED)
    assert cases.accept(resumed, ("assistant",), cleanup_ok=False)[0] == cases.FAILED
    assert cases.is_full(("assistant",)) is False


def test_websocket_rejects_denied_upgrade_without_credential_fallback():
    sock = Socket(b"HTTP/1.1 403 Forbidden\r\n\r\n")

    class TLS:
        def wrap_socket(self, connection, server_hostname):
            return connection

    session = assistant_client.UserSession(
        {"id_token": "ordinary", "expires_at": 200}, clock=lambda: 100
    )
    with pytest.raises(assistant_client.ClientError, match="authentication"):
        assistant_client.connect(
            "wss://example.invalid/ws",
            session,
            dial=lambda *_args, **_kwargs: sock,
            tls=TLS,
        )
    assert sock.closed


def stage_context(matrix):
    return {
        "document": {"instance_id": "i-synthetic"},
        "matrix": matrix,
        "transcript": [],
        "correlation": {},
        "manifest": None,
        "fault": "none",
        "record": lambda identifier, status, detail: cases.record(
            matrix, identifier, status, detail
        ),
    }


@pytest.mark.parametrize("identifier", [None, "", "  ", 7, False, "wrong"])
def test_stream_requires_correlation_at_oracle_and_stage(identifier):
    observed = events()
    if identifier != "wrong":
        for event in observed:
            event.pop("request_id")
    with pytest.raises(assistant_oracles.EvidenceError):
        assistant_oracles.stream(
            observed, request_id=identifier, canary="synthetic-secret"
        )
    matrix = cases.new_matrix(("assistant",))
    evidence = {"success": True, "events": observed, "canary": "synthetic-secret"}
    if identifier is not None:
        evidence["request_id"] = identifier
    stages.journeys_stage({}, {"journey": lambda purpose: lambda *_args: evidence})(
        stage_context(matrix)
    )
    assert matrix["E43"]["status"] == cases.FAILED
    assert cases.accept(matrix, ("assistant",), cleanup_ok=True)[0] == cases.FAILED


@pytest.mark.parametrize(
    "stamp", ["1999-01-01T10:00:00Z", "2026-01-01T09:59:59Z", "2026-01-01T12:00:00Z"]
)
def test_sources_reject_wrong_timestamp_or_window(stamp):
    observed = pages()
    observed[0]["records"][0]["timestamp"] = stamp
    for fixture in (
        source_fixture(),
        {
            **source_fixture(),
            "expected_timestamps": {
                **source_fixture()["expected_timestamps"],
                "activity-1": stamp,
            },
        },
    ):
        with pytest.raises(assistant_oracles.EvidenceError):
            assistant_oracles.sources(
                observed,
                expected_ids={"activity-1", "activity-2"},
                allowed_ids={"activity-1", "activity-2"},
                canary="synthetic-secret",
                **fixture,
            )


def test_sources_accept_equivalent_instants_and_half_open_window():
    observed = pages()
    observed[0]["records"][0]["timestamp"] = "2026-01-01T05:00:00-05:00"
    fixture = source_fixture()
    fixture["window"].update(
        timezone="America/New_York",
        start="2026-01-01T05:00:00-05:00",
        end="2026-01-01T06:00:01-05:00",
    )
    assert (
        assistant_oracles.sources(
            observed,
            expected_ids={"activity-1", "activity-2"},
            allowed_ids={"activity-1", "activity-2"},
            canary="synthetic-secret",
            **fixture,
        )["source_count"]
        == 2
    )


def frame(opcode, payload=b"", final=True):
    header = bytes([(0x80 if final else 0) | opcode])
    return (
        header
        + (
            bytes([len(payload)])
            if len(payload) < 126
            else b"\x7e" + struct.pack("!H", len(payload))
        )
        + payload
    )


def test_websocket_interleaved_controls_and_continuations():
    sock = Socket(
        frame(9, b"before")
        + frame(1, b'{"text":"', False)
        + frame(10, b"pong")
        + frame(9, b"middle")
        + frame(0, "hello ☃".encode()[:7], False)
        + frame(0, "hello ☃".encode()[7:] + b'"}')
    )
    assert assistant_client.WebSocket(sock).receive() == {"text": "hello ☃"}
    assert sock.sent[0] == 0x8A
    assert sock.sent[1] & 0x80


@pytest.mark.parametrize(
    "wire",
    [
        frame(0, b"{}"),
        frame(1, b"{", False) + frame(1, b"}"),
        frame(9, b"ping", False),
        frame(9, b"x" * 126),
        frame(2, b"{}"),
        b"\xc1\x02{}",
        b"\x81\x82{}",
        frame(8, b"x"),
        frame(8, b"\x03\xed"),
        frame(1, b"x" * 40000, False) + frame(0, b"x" * 30000),
        frame(1, b"\xff"),
        frame(1, b"invalid"),
        frame(1, b"{", False),
    ],
)
def test_websocket_rejects_malformed_or_unbounded_frames(wire):
    with pytest.raises(assistant_client.ClientError):
        assistant_client.WebSocket(Socket(wire)).receive()


def test_websocket_close_is_not_an_application_event():
    sock = Socket(frame(8, struct.pack("!H", 1000)))
    with pytest.raises(assistant_client.ClientError, match="closed before"):
        assistant_client.WebSocket(sock).receive()
    assert sock.closed and sock.sent[0] == 0x88


def test_websocket_controls_do_not_extend_deadline(monkeypatch):
    clock = iter([0, 1, 2, 16])
    monkeypatch.setattr(assistant_client.time, "monotonic", lambda: next(clock))
    with pytest.raises(assistant_client.ClientError, match="timed out"):
        assistant_client.WebSocket(Socket(frame(9, b"a") + frame(1, b"{}"))).receive()


def test_assistant_bindings_reach_shipped_dispatcher(
    tmp_path, users, monkeypatch, capsys
):
    from tests.e2e.cli_uplift.remote import dispatcher
    from tests.unit.test_cli_uplift import write_config

    cfg = config.load(
        write_config(
            tmp_path, assistant_users=users, websocket_url="wss://example.invalid/ws"
        )
    )
    payload = live._journey_payload(
        cfg,
        {
            "document": {
                "instance_id": "i-synthetic",
                "session": {"access_token": "controller-secret"},
            },
            "evaluation_id": "synthetic-run",
            "preflight": {},
        },
    )
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload))
    received = []

    class Driver:
        @staticmethod
        def execute(config, evidence):
            received.append(config)
            evidence["success"] = True

    import common

    monkeypatch.setattr(common, "assert_owned_instance", lambda cfg: None)
    monkeypatch.setattr(dispatcher, "load", lambda purpose: (Driver, {}))
    assert dispatcher.main(["assistant_stream", str(path)]) == 0
    assert received[0]["assistant_users"] == users
    assert received[0]["websocket_url"] == "wss://example.invalid/ws"
    assert "controller-secret" not in path.read_text()
    assert "remote/assistant_process.py" in dict(bundle.sources())


def test_actual_subprocess_cannot_read_controller_or_metadata(tmp_path, monkeypatch):
    private = tmp_path / "controller-session.json"
    private.write_text("synthetic-controller-secret")
    private.chmod(0o644)
    inherited = os.open(private, os.O_RDONLY)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "controller")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "controller")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider")
    script = """
import io, json, os, socket, sys
from tests.e2e.cli_uplift.remote import assistant_client, assistant_process
direct = socket.socket()
assistant_process.restrict()
for name in ("AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "ANTHROPIC_API_KEY"):
    assert name not in os.environ
for path in (sys.argv[1], "/proc/self/environ", "/proc/" + sys.argv[2] + "/environ"):
    try:
        open(path).read()
    except PermissionError:
        pass
    else:
        raise AssertionError("controller files accessible")
try:
    direct.connect(("169.254.169.254", 80))
except PermissionError:
    pass
else:
    raise AssertionError("metadata accessible")
try:
    socket.socket()
except PermissionError:
    pass
else:
    raise AssertionError("new socket permitted")
try:
    os.read(int(sys.argv[3]), 1)
except OSError:
    pass
else:
    raise AssertionError("controller descriptor inherited")
class Response(io.BytesIO):
    status = 200
def opener(request, timeout):
    assert request.get_header("Authorization") == "Bearer ordinary"
    assert request.get_header("X-User") is None
    return Response(b'{"authorized":true}')
session = assistant_client.UserSession({"access_token":"ordinary", "expires_at":4000000000})
assert assistant_client.http_json("https://example.invalid/history", session, opener=opener) == {"authorized":True}
try:
    assistant_client.http_json("https://example.invalid/internal/fault", session, opener=opener)
except assistant_client.ClientError:
    pass
else:
    raise AssertionError("fault permitted")
print("isolated and authenticated")
"""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(private),
                str(os.getpid()),
                str(inherited),
            ],
            env=assistant_process.python_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=15,
            close_fds=True,
            check=False,
        )
    finally:
        os.close(inherited)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated and authenticated"


def test_production_process_requires_confinement_before_tokens():
    parent, child = socket.socketpair()
    try:
        with assistant_process.ProcessClient(child) as client:
            assert client.process.pid != os.getpid()
            with pytest.raises(
                assistant_client.ClientError,
                match="Invalid assistant isolated operation",
            ):
                client.call("fault")
    finally:
        parent.close()
        child.close()


def test_isolation_failure_has_no_unrestricted_fallback(monkeypatch):
    original_popen = subprocess.Popen
    bootstrap = (
        f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r}); "
        "from tests.e2e.cli_uplift.remote import assistant_process; "
        "assistant_process.restrict = lambda: sys.exit(17); assistant_process.worker()"
    )
    processes = []

    def launch(argv, **kwargs):
        process = original_popen([argv[0], "-I", "-c", bootstrap, argv[-1]], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(assistant_process.subprocess, "Popen", launch)
    parent, child = socket.socketpair()
    try:
        with pytest.raises(
            assistant_client.ClientError, match="libseccomp is required"
        ):
            assistant_process.ProcessClient(child)
    finally:
        parent.close()
        child.close()
    assert len(processes) == 1
    assert processes[0].poll() is not None


def test_default_adapters_use_isolated_transport(monkeypatch):
    calls = []

    class Isolated:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def call(self, action):
            calls.append(action)
            return {"authorized": True}

    def start(url, session, mode):
        calls.append((url, mode))
        return Isolated()

    monkeypatch.setattr(assistant_process, "start", start)
    session = assistant_client.UserSession(
        {"access_token": "ordinary", "expires_at": 4000000000}
    )
    assert assistant_client.http_json("https://example.invalid/history", session) == {
        "authorized": True
    }
    assert isinstance(
        assistant_client.connect("wss://example.invalid/ws", session), Isolated
    )
    assert calls == [
        ("https://example.invalid/history", "http"),
        "http",
        ("wss://example.invalid/ws", "websocket"),
    ]


@pytest.mark.parametrize(
    "address",
    ["169.254.169.254", "fd00:ec2::254", "127.0.0.1", "::ffff:169.254.169.254"],
)
def test_parent_cannot_delegate_metadata_or_local_socket(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443))
        ],
    )
    session = assistant_client.UserSession(
        {"access_token": "ordinary", "expires_at": 4000000000}
    )
    with pytest.raises(
        assistant_client.ClientError, match="metadata or a local service"
    ):
        assistant_client.http_json("https://example.invalid/history", session)


def test_canary_redaction_preserves_json_types():
    assert assistant_oracles.redact_canary({"text": ["n", None, True, 10]}, "n") == {
        "text": ["<redacted>", None, True, 10]
    }


@pytest.mark.parametrize(
    "mode,denied,invalid_upgrade",
    [
        ("http", False, False),
        ("http", True, False),
        ("websocket", False, False),
        ("websocket", True, False),
        ("websocket", False, True),
    ],
)
def test_isolated_production_worker_authenticates_over_tls_socketpair(
    tmp_path, monkeypatch, mode, denied, invalid_upgrade
):
    import base64
    import hashlib
    import ssl

    certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=example.invalid",
            "-addext",
            "subjectAltName=DNS:example.invalid",
        ],
        check=True,
        capture_output=True,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, key)
    parent, child = socket.socketpair()
    parent.settimeout(5)
    child.settimeout(5)
    requests = []
    errors = []

    def serve():
        try:
            with server_context.wrap_socket(parent, server_side=True) as connection:
                request = bytearray()
                while not request.endswith(b"\r\n\r\n"):
                    chunk = connection.recv(1)
                    if not chunk:
                        raise AssertionError("client disconnected")
                    request.extend(chunk)
                requests.append(bytes(request))
                if denied:
                    connection.sendall(
                        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
                    )
                elif mode == "http":
                    connection.sendall(
                        b'HTTP/1.1 200 OK\r\nContent-Length: 19\r\n\r\n{"authorized":true}'
                    )
                else:
                    header = next(
                        line
                        for line in request.decode().split("\r\n")
                        if line.startswith("Sec-WebSocket-Key:")
                    )
                    accept = base64.b64encode(
                        hashlib.sha1(
                            (
                                header.split(":", 1)[1].strip()
                                + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                            ).encode()
                        ).digest()
                    )
                    connection.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\n"
                        + (
                            b""
                            if invalid_upgrade
                            else b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        )
                        + b"Sec-WebSocket-Accept: "
                        + accept
                        + b"\r\n\r\n"
                        + frame(1, b'{"authorized":', False)
                        + frame(9, b"ping")
                        + frame(0, b"true}")
                    )
                    if not invalid_upgrade:
                        assert connection.recv(128)[0] == 0x8A
        except (OSError, AssertionError) as exc:
            errors.append(exc)

    original_popen = subprocess.Popen
    bootstrap = f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r}); from tests.e2e.cli_uplift.remote import assistant_process; original = assistant_process.ssl.create_default_context; assistant_process.ssl.create_default_context = lambda: original(cafile={str(certificate)!r}); assistant_process.worker()"

    def launch(argv, **kwargs):
        return original_popen([argv[0], "-I", "-c", bootstrap, argv[-1]], **kwargs)

    monkeypatch.setattr(assistant_process.subprocess, "Popen", launch)
    server = threading.Thread(target=serve, daemon=True)
    server.start()
    try:
        with assistant_process.ProcessClient(child) as client:
            initialize = {
                "url": ("https" if mode == "http" else "wss")
                + "://example.invalid/history",
                "mode": mode,
                "tokens": {
                    "access_token": "ordinary-access",
                    "id_token": "ordinary-id",
                    "expires_at": 4000000000,
                },
            }
            if (denied or invalid_upgrade) and mode == "websocket":
                with pytest.raises(
                    assistant_client.ClientError, match="authentication"
                ):
                    client.call("initialize", **initialize)
            else:
                client.call("initialize", **initialize)
                if denied:
                    with pytest.raises(assistant_client.ClientError, match="403"):
                        client.call("http")
                else:
                    assert client.call("http" if mode == "http" else "receive") == {
                        "authorized": True
                    }
    finally:
        child.close()
        server.join(6)
        parent.close()
    assert not server.is_alive() and not errors, errors
    assert len(requests) == 1
    assert b"X-User" not in requests[0]
    assert (
        b"Authorization: Bearer ordinary-access"
        if mode == "http"
        else b"?token=ordinary-id"
    ) in requests[0]


@pytest.mark.parametrize("case_id", ["E43", "E44"])
@pytest.mark.parametrize("outcome", ["passed", "driver_failed", "oracle_failed"])
def test_assistant_redacts_evidence_before_state_and_report(
    tmp_path, users, capsys, case_id, outcome
):
    from tests.unit.test_cli_uplift import NOW, FakeS3, write_config

    cfg_path = write_config(
        tmp_path, assistant_users=users, websocket_url="wss://example.invalid/ws"
    )
    store = statestore.S3StateStore(FakeS3(), "synthetic-state", clock=lambda: NOW)
    canary = "synthetic-canary"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature"
    evidence = {
        "success": outcome != "driver_failed",
        "events": events(),
        "request_id": "request-1",
        "pages": pages(),
        "expected_ids": ["activity-1", "activity-2"],
        "allowed_ids": ["activity-1", "activity-2"],
        **source_fixture(),
        "canary": canary,
        "detail": {"note": canary, "access_token": "ordinary-secret"},
        "transcript": [f"observation {canary} {jwt}"],
        "correlation": {
            "request_id": "request-1",
            "nested": {canary: [canary, jwt], "access_token": "ordinary-secret"},
        },
    }
    if outcome == "oracle_failed":
        evidence["events"][5]["text"] = canary
        evidence["pages"][0]["records"][0]["text"] = canary

    def resolve(purpose):
        if purpose == stages.JOURNEY_DRIVERS[case_id]:
            return lambda _instance, _ctx: evidence
        return None

    stage_map = {name: lambda _ctx: None for name in runner.STAGES}
    stage_map.update(
        ec2=lambda ctx: ctx["document"].update(instance_id="i-synthetic"),
        journeys=stages.journeys_stage({}, {"journey": resolve}),
        cleanup=lambda _ctx: True,
    )
    state_dir = tmp_path / "run"
    assert (
        runner.main(
            [
                "--mode",
                "start",
                "--config",
                cfg_path,
                "--suite",
                "assistant",
                "--state-dir",
                str(state_dir),
            ],
            stages=stage_map,
            clock=lambda: NOW,
            store=store,
        )
        == 1
    )
    local = runner.State(state_dir).read()
    saved, _inventory = store.load(local["evaluation_id"])
    artifact = json.loads((state_dir / "out" / "report.json").read_text())
    for document in (local, saved, artifact):
        serialized = json.dumps(document)
        assert all(
            value not in serialized for value in (canary, jwt, "ordinary-secret")
        )
        assert document["transcript"] == [
            "observation <redacted> [REDACTED]",
        ]
        assert document["correlation"] == {
            "request_id": "request-1",
            "nested": {"<redacted>": ["<redacted>", "[REDACTED]"]},
        }
    for document in (local, saved):
        entry = document["matrix"][case_id]
        assert entry["status"] == (
            cases.PASSED if outcome == "passed" else cases.FAILED
        )
        if outcome == "oracle_failed":
            assert "Synthetic secret leaked" in entry["detail"]["oracle_error"]
        elif outcome == "passed":
            assert entry["detail"]["assistant_observations"]
    assert artifact["status"] == cases.FAILED
    assert artifact["full_acceptance"] is False
    assert canary in evidence["transcript"][0]
    assert evidence["correlation"]["nested"][canary] == [canary, jwt]
    capsys.readouterr()


def test_assistant_interrupt_restore_resume_and_repeated_cleanup(
    tmp_path, users, capsys
):
    from tests.unit.test_cli_uplift import NOW, FakeS3, write_config

    cfg_path = write_config(
        tmp_path, assistant_users=users, websocket_url="wss://example.invalid/ws"
    )
    store = statestore.S3StateStore(FakeS3(), "synthetic-state", clock=lambda: NOW)
    interrupted = True
    calls = []
    deletions = []

    def prepare(ctx):
        instance = ctx["evaluation_id"] + "-instance"
        ctx["document"]["instance_id"] = instance
        ctx["manifest"].record("ec2_instance", instance)
        ctx["manifest"].record("secret", users["a1"]["fixture_name"], reused=True)

    def resolve(purpose):
        if purpose not in {"assistant_stream", "assistant_sources"}:
            return None

        def run(_instance, ctx):
            calls.append(purpose)
            if purpose == "assistant_stream":
                return {
                    "success": True,
                    "events": events(),
                    "request_id": "request-1",
                    "canary": "synthetic-secret",
                    "detail": {
                        "note": "synthetic-secret",
                        "access_token": "ordinary-secret",
                    },
                    "transcript": ["request-1 synthetic-secret"],
                    "correlation": {
                        "note": "synthetic-secret",
                        "access_token": "ordinary-secret",
                    },
                }
            if interrupted:
                raise KeyboardInterrupt("synthetic interruption")
            return {
                "success": True,
                "pages": pages(),
                "expected_ids": ["activity-1", "activity-2"],
                "allowed_ids": ["activity-1", "activity-2"],
                "canary": "synthetic-secret",
                **source_fixture(),
            }

        return run

    def remove(identifier):
        deletions.append(identifier)
        if len(deletions) == 1:
            raise RuntimeError("synthetic-secret")

    stage_map = {name: lambda _ctx: None for name in runner.STAGES}
    stage_map.update(
        ec2=prepare,
        journeys=stages.journeys_stage({}, {"journey": resolve}),
        cleanup=lambda ctx: cleanup.sweep(ctx["manifest"], {"ec2_instance": remove})[0],
    )

    def invoke(mode, directory, extra=()):
        return runner.main(
            [
                "--mode",
                mode,
                "--config",
                cfg_path,
                "--suite",
                "assistant",
                "--state-dir",
                str(directory),
                *extra,
            ],
            stages=stage_map,
            clock=lambda: NOW,
            store=store,
        )

    first_dir = tmp_path / "first"
    with pytest.raises(KeyboardInterrupt):
        invoke("start", first_dir)
    local = runner.State(first_dir).read()
    evaluation_id = local["evaluation_id"]
    saved, inventory = store.load(evaluation_id)
    assert saved["matrix"]["E43"]["status"] == cases.PASSED
    assert saved["matrix"]["E44"]["status"] == cases.NOT_RUN
    observations = saved["matrix"]["E43"]["detail"]["assistant_observations"]
    assert observations["request_id"] == "request-1"
    assert observations["events"][4] == {
        "cursor": 4,
        "type": "tool",
        "tool_id": "tool-1",
    }
    assert not deletions
    assert {entry["id"] for entry in inventory["resources"]} == {
        evaluation_id + "-instance",
        users["a1"]["fixture_name"],
    }
    assert "synthetic-secret" not in json.dumps(saved)
    assert "ordinary-secret" not in json.dumps(saved)

    interrupted = False
    resumed_dir = tmp_path / "restored"
    assert (
        invoke("resume", resumed_dir, ("--restore", "--evaluation-id", evaluation_id))
        == 1
    )
    resumed = runner.State(resumed_dir).read()
    assert resumed["attempt"] == 2
    assert calls.count("assistant_stream") == 1
    assert resumed["transcript"] == ["request-1 <redacted>"]
    assert resumed["correlation"] == {"note": "<redacted>"}
    assert resumed["matrix"]["E43"]["detail"]["assistant_observations"] == observations
    assert resumed["matrix"]["E44"]["status"] == cases.PASSED
    assert resumed["cleanup_ok"] is False
    assert resumed["cleanup_outstanding"] == [
        "ec2_instance:" + evaluation_id + "-instance"
    ]
    artifact = (resumed_dir / "out" / "report.json").read_text()
    assert "request-1" in artifact and "activity-1" in artifact
    assert "2026-01-01T10:00:00+00:00" in artifact
    assert "synthetic-secret" not in artifact and "ordinary-secret" not in artifact
    assert json.loads(artifact)["full_acceptance"] is False
    assert invoke("cleanup", resumed_dir) == 0
    assert runner.State(resumed_dir).read()["cleanup_outstanding"] == []
    assert invoke("cleanup", resumed_dir) == 0
    assert deletions == [evaluation_id + "-instance"] * 2
    _saved, inventory = store.load(evaluation_id)
    assert [entry["status"] for entry in inventory["resources"]] == [
        cleanup.DELETED,
        cleanup.REUSED,
    ]


def test_isolated_python_uses_its_own_runtime_not_inherited_loader_paths(
    tmp_path, monkeypatch
):
    import sysconfig

    monkeypatch.setenv("LD_LIBRARY_PATH", "/untrusted/runtime")
    monkeypatch.setenv("LD_PRELOAD", "/untrusted/injection.so")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/modules")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "controller-secret")
    env = assistant_process.python_env(tmp_path)
    assert not {"LD_PRELOAD", "PYTHONPATH", "AWS_SESSION_TOKEN"} & env.keys()
    if sysconfig.get_config_var("Py_ENABLE_SHARED"):
        assert (
            Path(env["LD_LIBRARY_PATH"]) / sysconfig.get_config_var("LDLIBRARY")
        ).is_file()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import ctypes, ssl; print('native imports work')",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "native imports work"


@pytest.fixture
def baseline_dispatch(tmp_path, monkeypatch, users, capsys):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_baseline")
    tokens = {
        "access_token": "ordinary-access",
        "id_token": "ordinary-id",
        "expires_at": 4000000000,
    }
    identity = {"user_id": users["a1"]["canonical_user_id"], "tenant_id": "tenant-a"}
    state = {
        "sends": [],
        "closed": 0,
        "identity": identity,
        "admin": False,
        "answer": "ready",
        "fail": None,
        "created": None,
        "chunks": None,
    }
    cfg = {
        "evaluation_id": "baseline-test",
        "region": "us-east-1",
        "sts_endpoint": "https://sts.example.invalid",
        "work_dir": str(tmp_path),
        "gateway_url": "https://example.invalid",
        "websocket_url": "wss://example.invalid/ws",
        "assistant_users": users,
    }
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(cfg))
    monkeypatch.setattr(module.common, "assert_owned_instance", lambda config: None)

    def secret(config, env, key):
        assert config["credential_secret"] == users["a1"]["fixture_name"]
        assert key == "ordinary_session"
        return tokens

    monkeypatch.setattr(module.common, "fixture_secret", secret)

    def http(url, session):
        assert session.token() == "ordinary-access"
        if state["fail"] == "auth":
            raise module.assistant_client.ClientError(
                "Assistant HTTP request returned 401"
            )
        if url.endswith("/auth/me"):
            return {
                "user_id": "a1",
                "org_id": "tenant-a",
                "account_type": "human",
                "is_admin": state["admin"],
            }
        if url.endswith("/chat/capabilities"):
            return {**state["identity"], "enabled": True, "history_configured": True}
        assert url.endswith("/chat/sessions/server-session")
        if state["fail"] == "ownership":
            raise module.assistant_client.ClientError(
                "Assistant HTTP request returned 403"
            )
        messages = []
        if len(state["sends"]) == 2:
            messages = [
                {"role": "user", "content": module.PROMPT},
                {"role": "assistant", "content": state["answer"]},
            ]
        return {**identity, "session_id": "server-session", "messages": messages}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            state["closed"] += 1

        def send(self, value):
            state["sends"].append(value)
            if state["fail"] == "interrupted" and value["action"] == "message":
                raise KeyboardInterrupt("lost connection after sending")

        def receive(self):
            if len(state["sends"]) == 1:
                if state["created"] is not None:
                    return state["created"]
                return {
                    "request_id": state["sends"][0]["request_id"],
                    "session_id": "server-session",
                }
            if state["chunks"] is not None:
                return state["chunks"].pop(0)
            return {
                "type": "response",
                "task_id": "task-1",
                "status": "completed",
                "content": state["answer"],
            }

    monkeypatch.setattr(module.assistant_client, "http_json", http)
    monkeypatch.setattr(
        module.assistant_client, "connect", lambda url, session: Client()
    )

    def run():
        code = dispatcher.main(["assistant_baseline", str(payload)])
        return code, json.loads(capsys.readouterr().out.splitlines()[-1])

    return run, state, module, tokens


def test_registered_baseline_dispatch_and_resume(baseline_dispatch, tmp_path):
    run, state, module, _ = baseline_dispatch
    code, evidence = run()
    assert code == 0 and evidence["success"] is True, evidence
    detail = evidence["detail"]
    assert detail["history_verified"] is True and detail["phase"] == "completed"
    assert state["closed"] == 1
    assert [value["action"] for value in state["sends"]] == [
        "create-session",
        "message",
    ]
    assert "ordinary-access" not in json.dumps(evidence)
    assert "ordinary-id" not in json.dumps(evidence)
    assert (
        state["answer"]
        not in next(tmp_path.glob("assistant-baseline-*.json")).read_text()
    )
    code, resumed = run()
    assert code == 0 and resumed["detail"]["resumed"] is True
    assert len(state["sends"]) == 2
    assert "assistant_baseline" in bundle.purposes()
    matrix = cases.new_matrix(("assistant",))
    stages.journeys_stage(
        {},
        {
            "journey": lambda purpose: (lambda *_args: evidence)
            if purpose == "assistant_baseline"
            else None
        },
    )(stage_context(matrix))
    assert matrix["E50"]["status"] == cases.PASSED
    assert matrix["E43"]["status"] == cases.FAILED
    assert cases.accept(matrix, ("assistant",), cleanup_ok=True)[0] != cases.PASSED


@pytest.mark.parametrize(
    "failure", ["auth", "ownership", "admin", "identity", "expired", "correlation"]
)
def test_registered_baseline_refuses_invalid_identity_and_auth(
    baseline_dispatch, failure
):
    run, state, _module, tokens = baseline_dispatch
    if failure == "admin":
        state["admin"] = True
    elif failure == "identity":
        state["identity"]["user_id"] = "other-user"
    elif failure == "expired":
        tokens["expires_at"] = 1
    elif failure == "correlation":
        state["created"] = {"request_id": "wrong", "session_id": "server-session"}
    else:
        state["fail"] = failure
    code, evidence = run()
    assert code == 1 and evidence["success"] is False
    assert not any(value["action"] == "message" for value in state["sends"])


def test_registered_baseline_never_replays_interrupted_turn(
    baseline_dispatch, tmp_path
):
    run, state, _module, _tokens = baseline_dispatch
    state["fail"] = "interrupted"
    with pytest.raises(KeyboardInterrupt):
        run()
    assert state["closed"] == 1
    journal = json.loads(next(tmp_path.glob("assistant-baseline-*.json")).read_text())
    assert journal["phase"] == "submit_attempted"
    state["fail"] = None
    code, evidence = run()
    assert code == 1 and "not automatic resubmission" in evidence["error"]
    assert len(state["sends"]) == 2


def test_baseline_consumes_actual_response_router_chunks(
    baseline_dispatch, monkeypatch
):
    import importlib.util

    run, state, _module, _tokens = baseline_dispatch
    path = (
        Path(__file__).resolve().parents[2]
        / "modules/agent-factory/gateway/lambdas/response/routers/websocket.py"
    )
    spec = importlib.util.spec_from_file_location("baseline_response_router", path)
    router_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(router_module)
    router = router_module.WebSocketRouter("wss://example.invalid/ws")
    frames = []
    monkeypatch.setattr(
        router,
        "_send_frame",
        lambda payload, *_args: frames.append(json.loads(payload)) or True,
    )
    state["answer"] = "ready " * 5000
    assert router.route(
        state["answer"], {"connection_id": "owned", "status": "completed"}, "task-1"
    )
    assert len(frames) > 1 and frames[0]["chunk_index"] == 1
    state["chunks"] = frames
    code, evidence = run()
    assert code == 0 and evidence["detail"]["response_chunks"] > 1


@pytest.mark.parametrize("mutation", ["task", "order", "failed", "missing"])
def test_baseline_rejects_invalid_wire_evidence(baseline_dispatch, mutation):
    run, state, _module, _tokens = baseline_dispatch
    frames = [
        {
            "type": "response",
            "status": "completed",
            "task_id": "task-1",
            "content": "rea",
            "chunk_index": 1,
            "chunk_total": 2,
        },
        {
            "type": "response",
            "status": "completed",
            "task_id": "task-1",
            "content": "dy",
            "chunk_index": 2,
            "chunk_total": 2,
        },
    ]
    if mutation == "task":
        frames[1]["task_id"] = "other-task"
    elif mutation == "order":
        frames.reverse()
    elif mutation == "failed":
        frames[0]["status"] = "failed"
    else:
        frames[0].pop("task_id")
    state["chunks"] = frames
    code, evidence = run()
    assert code == 1 and evidence["success"] is False


def test_baseline_lost_journal_refuses_prior_dispatch(baseline_dispatch, tmp_path):
    run, state, _module, _tokens = baseline_dispatch
    assert run()[0] == 0
    for journal in tmp_path.glob("assistant-baseline-*.json"):
        journal.unlink()
    payload = tmp_path / "payload.json"
    cfg = json.loads(payload.read_text())
    cfg["prior_dispatch"] = True
    payload.write_text(json.dumps(cfg))
    code, evidence = run()
    assert code != 0
    assert "prior dispatch has no local journal" in json.dumps(evidence)
    assert len(state["sends"]) == 2


@pytest.mark.parametrize("sink_failure", [False, True])
def test_baseline_durable_intent_before_dispatch(tmp_path, sink_failure):
    from tests.e2e.cli_uplift import cleanup, live

    calls = []
    durable = []

    def push(document, *, critical):
        assert critical
        if sink_failure:
            raise RuntimeError("storage unavailable")
        durable.append(json.loads(json.dumps(document)))

    manifest = cleanup.Manifest(tmp_path / "manifest.json", "baseline", on_change=push)

    class Ssm:
        def json_result(self, instance, commands, **kwargs):
            assert durable[-1]["diagnostic_intents"]["assistant_baseline:baseline"]
            calls.append(commands)
            return None, {}

    worker = live._run_worker(Ssm(), {}, lambda *a: None)
    payload = {"evaluation_id": "baseline", "gateway_url": "https://example.invalid"}
    if sink_failure:
        with pytest.raises(RuntimeError, match="storage unavailable"):
            worker("i-owned", "assistant_baseline", payload, manifest=manifest)
        assert not calls
    else:
        worker("i-owned", "assistant_baseline", payload, manifest=manifest)
        worker("i-replacement", "assistant_baseline", payload, manifest=manifest)
        assert '"prior_dispatch": false' in "\n".join(calls[0])
        assert '"prior_dispatch": true' in "\n".join(calls[1])


def test_isolated_python_handles_relocated_installation(tmp_path, monkeypatch):
    library_dir = tmp_path / "relocated" / "lib"
    library_dir.mkdir(parents=True)
    (library_dir / "libpython-test.so").touch()
    monkeypatch.setattr(assistant_process.sys, "base_prefix", str(library_dir.parent))
    settings = {
        "Py_ENABLE_SHARED": 1,
        "LIBDIR": "/missing/build-machine/lib",
        "LDLIBRARY": "libpython-test.so",
    }
    monkeypatch.setattr(assistant_process.sysconfig, "get_config_var", settings.get)
    assert assistant_process.python_env(tmp_path)["LD_LIBRARY_PATH"] == str(library_dir)
