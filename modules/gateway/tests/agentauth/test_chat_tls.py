import pytest

from src.agentauth.chat_tls import HOST, POST_PATHS, SandboxTransport, main


async def request(path, method="POST", **updates):
    reached, sent = [], []

    async def delegate(scope, receive, send):
        reached.append(scope)
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b"authentication required"})

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "scheme": "https",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "headers": [(b"host", f"{HOST}:8443".encode())],
    }
    scope.update(updates)
    await SandboxTransport(delegate)(scope, None, send)
    return reached, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(POST_PATHS))
async def test_scoped_routes_keep_original_authentication(path):
    reached, sent = await request(path)
    assert len(reached) == 1
    assert reached[0]["headers"] == [(b"host", f"{HOST}:8443".encode())]
    assert sent[0]["status"] == 401


@pytest.mark.parametrize("operation", ["state", "next", "admit"])
async def test_persistent_session_transport_reaches_original_authorization(operation):
    reached, sent = await request(f"/v1/chat/data/session/{operation}")
    assert len(reached) == 1
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,method,changes",
    [
        ("/admin/users", "GET", {}),
        ("/v1/chat/turns/cancel", "POST", {}),
        ("/internal/v1/agent/chat/data/admit", "POST", {}),
        ("/internal/v1/agent/chat/data/exit", "POST", {}),
        ("/internal/v1/agent/chat/data/teardown", "POST", {}),
        ("/internal/v1/agent/chat/data/finalize", "POST", {}),
        ("/internal/v1/agent/chat/data/complete", "POST", {}),
        ("/internal/v1/agent/chat/data/resume", "POST", {}),
        ("/internal/v1/agent/chat/data/reserve", "POST", {}),
        ("/internal/v1/agent/chat/data/session/commit", "POST", {}),
        ("/v1/messages", "POST", {}),
        ("/openai/v1/responses", "POST", {}),
        ("/v1/chat/data/bootstrap/", "POST", {}),
        ("/v1/chat/data/bootstrap", "GET", {}),
        ("/v1/chat/data/bootstrap", "POST", {"scheme": "http"}),
        ("/v1/chat/data/bootstrap", "POST", {"raw_path": b"/v1/chat/data/%62ootstrap"}),
        ("/v1/chat/data/bootstrap", "POST", {"root_path": "/admin"}),
        ("/v1/chat/data/bootstrap", "POST", {"headers": [(b"host", b"attacker.example")]}),
        ("/v1/chat/data/bootstrap", "POST", {"headers": [(b"host", f"{HOST}:8443".encode())] * 2}),
        ("/v1/chat/data/artifact/a/../../admin", "GET", {}),
        ("/v1/chat/data/artifact/a/b", "DELETE", {}),
    ],
)
async def test_other_routes_and_ambiguous_transports_never_reach_gateway(path, method, changes):
    reached, sent = await request(path, method, **changes)
    assert reached == []
    assert sent[0]["status"] == 404


@pytest.mark.asyncio
async def test_artifact_still_requires_gateway_authorization():
    reached, sent = await request("/v1/chat/data/artifact/session-a/artifact-b", "GET")
    assert len(reached) == 1
    assert sent[0]["status"] == 401


def test_listener_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ADP_CHAT_TLS_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="disabled"):
        main()


def test_listener_cannot_fall_back_to_plaintext(monkeypatch):
    monkeypatch.setenv("ADP_CHAT_TLS_ENABLED", "true")
    monkeypatch.setattr("src.agentauth.chat_tls.Path.is_file", lambda _: False)
    with pytest.raises(RuntimeError, match="certificate and key"):
        main()
