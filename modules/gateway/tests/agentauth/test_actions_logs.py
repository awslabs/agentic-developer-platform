"""Known token patterns are masked before a bounded log tail is returned."""

import io
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from src.agentauth import actions_logs
from src.agentauth.actions_logs import MAX_LOG_TAIL_BYTES, fetch_log_tail, redact_log_tail, redact_text
from src.agentauth.github_operations import OperationRefusedError
from src.orchestration.deployment_workflow_provider import WorkflowProvider


def archive(content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("job/step.txt", content)
    return buffer.getvalue()


def test_mask_before_tailing_and_bound_utf8_bytes():
    secret = b"ghp_" + b"x" * 40
    tail, truncated = redact_log_tail(archive(b"a" * 70000 + secret + b"\npassword=my-secret\nBearer bearer-secret\n"))
    assert truncated is True
    assert len(tail.encode()) <= MAX_LOG_TAIL_BYTES
    assert secret.decode() not in tail
    assert "my-secret" not in tail and "bearer-secret" not in tail
    assert tail.count("[REDACTED]") == 3
    assert "a" in tail


def test_invalid_utf8_cannot_expand_tail_beyond_limit():
    tail, truncated = redact_log_tail(archive(b"\xff" * 70000))
    assert truncated is True
    assert len(tail.encode()) <= MAX_LOG_TAIL_BYTES


def test_default_failure_excerpt_preserves_early_guard_failure_before_successful_jobs():
    buffer = io.BytesIO()
    secret = b"ghp_" + b"x" * 40
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        # GitHub archives can list later failures first, with duplicate step logs.
        for i in range(8):
            bundle.writestr(f"downstream-{i}.txt", b"unrelated context\n" * 300 + b"2026-10-21T11:22:30.1234567Z ##[error]Dependent jobs failed.\n")
        bundle.writestr(
            "detect/guard.txt",
            b"ACTUAL_DEFINITION=cccc\nADP_DEFINITION=bbbb\n"
            + secret
            + b"\ntest actual = expected\n2026-10-21T11:21:00.1234567Z ##[error]Process completed with exit code 1.\n",
        )
        bundle.writestr("smoke/success.txt", b"passing smoke checks\n" * 10000)
    payload = buffer.getvalue()
    tail, _ = redact_log_tail(payload)
    assert "ACTUAL_DEFINITION" not in tail  # The old tail hid the failing job.
    excerpt, truncated = redact_log_tail(payload, failure_focus=True)
    assert "ACTUAL_DEFINITION=cccc" in excerpt and "ADP_DEFINITION=bbbb" in excerpt
    assert "detect/guard.txt" in excerpt and "exit code 1" in excerpt
    assert secret.decode() not in excerpt and "[REDACTED]" in excerpt
    assert truncated and len(excerpt.encode()) <= actions_logs.DEFAULT_LOG_EXCERPT_BYTES


@pytest.mark.parametrize("content", [b"\xff" * 70000, b"setup\n" * 20000 + b"last line\n", (b"x" * 3000 + b"##[error]failure\n") * 10])
def test_default_excerpt_is_bounded_even_with_invalid_utf8_or_many_failures(content):
    excerpt, truncated = redact_log_tail(archive(content), failure_focus=True)
    assert truncated and len(excerpt.encode()) <= actions_logs.DEFAULT_LOG_EXCERPT_BYTES
    if b"last line" in content:
        assert excerpt.endswith("last line\n")


def test_oversized_uncompressed_archive_and_invalid_zip_refused():
    with pytest.raises(OperationRefusedError):
        redact_log_tail(archive(b"a" * (16 * 1024 * 1024 + 1)))
    with pytest.raises(OperationRefusedError):
        redact_log_tail(b"not a zip")


def test_redact_before_truncating_run_title():
    title = "prefix " + "ghp_" + "x" * 40
    assert "ghp_" not in redact_text(title, max_bytes=50)
    assert "[REDACTED]" in redact_text(title, max_bytes=50)


@pytest.mark.asyncio
async def test_log_redirect_must_be_approved_https_destination():
    class Provider:
        async def request(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=302, headers={"location": "https://example.invalid/archive?token=secret"})

    async def reauthorize():
        raise AssertionError("unapproved redirect must not be fetched")

    with pytest.raises(OperationRefusedError):
        await fetch_log_tail(Provider(), SimpleNamespace(repo="owner/repo"), run_id=3, token="gateway-only", reauthorize=reauthorize)


@pytest.mark.asyncio
async def test_redirected_log_archive_is_reauthorized_and_masked(monkeypatch):
    secret = b"ghp_" + b"x" * 40
    payload = archive(b"initial\n" + secret + b"\n")
    checked = []

    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def aiter_bytes(self):
            yield payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def stream(self, method, url):
            assert method == "GET" and url.startswith("https://pipelines.actions.githubusercontent.com/")
            checked.append("fetched")
            return Response()

    class Provider:
        async def request(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=302, headers={"location": "https://pipelines.actions.githubusercontent.com/logs"})

    async def reauthorize():
        checked.append("reauthorized")

    monkeypatch.setattr(actions_logs.httpx, "AsyncClient", lambda **_kwargs: Client())
    tail, truncated = await fetch_log_tail(
        Provider(),
        SimpleNamespace(repo="owner/repo"),
        run_id=3,
        token="gateway-only",
        reauthorize=reauthorize,
    )
    assert checked == ["reauthorized", "fetched"]
    assert truncated is False and "[REDACTED]" in tail and secret.decode() not in tail


@pytest.fixture
async def log_provider(monkeypatch):
    state = SimpleNamespace(
        location="https://pipelines.actions.githubusercontent.com/logs",
        payload=archive(b"a" * 70000 + b"ghp_" + b"x" * 40 + b"\n"),
        api_status=302,
        archive_status=200,
        revoked=False,
        events=[],
    )

    def api_response(request):
        assert str(request.url) == "https://api.github.com/repos/owner/repo/actions/runs/3/logs"
        assert request.method == "GET" and request.headers["Authorization"] == "Bearer gateway-only"
        state.events.append("api")
        return httpx.Response(state.api_status, headers={"location": state.location})

    def archive_response(request):
        assert str(request.url) == state.location and request.method == "GET"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        state.events.append("archive")
        return httpx.Response(state.archive_status, headers={"location": "https://example.invalid/next"}, stream=httpx.ByteStream(state.payload))

    async def reauthorize():
        state.events.append("reauthorized")
        if state.revoked:
            raise OperationRefusedError("operations authority changed")

    client_type = httpx.AsyncClient

    def archive_client(**kwargs):
        assert kwargs == {"timeout": 20, "follow_redirects": False, "trust_env": False}
        return client_type(transport=httpx.MockTransport(archive_response), **kwargs)

    async with client_type(transport=httpx.MockTransport(api_response)) as client:
        state.provider = WorkflowProvider(client=client)
        state.reauthorize = reauthorize
        monkeypatch.setattr(actions_logs.httpx, "AsyncClient", archive_client)
        yield state


async def fetch_provider_logs(state):
    return await fetch_log_tail(state.provider, SimpleNamespace(repo="owner/repo"), run_id=3, token="gateway-only", reauthorize=state.reauthorize)


@pytest.mark.parametrize("follow_redirects", [False, True])
@pytest.mark.parametrize(
    "location",
    [
        "https://pipelines.actions.githubusercontent.com/logs",
        "https://results-receiver.actions.githubusercontent.com/logs",
        "https://example.blob.core.windows.net/logs",
    ],
)
async def test_real_provider_redirected_logs_are_reauthorized_and_masked(log_provider, location, follow_redirects):
    log_provider.location = location
    log_provider.provider.client.follow_redirects = follow_redirects
    tail, truncated = await fetch_provider_logs(log_provider)
    assert log_provider.events == ["api", "reauthorized", "archive"]
    assert truncated is True and len(tail.encode()) <= MAX_LOG_TAIL_BYTES
    assert "[REDACTED]" in tail and "ghp_" not in tail


@pytest.mark.parametrize(
    "location",
    [
        "https://example.invalid/archive",
        "http://pipelines.actions.githubusercontent.com/logs",
        "https://user@pipelines.actions.githubusercontent.com/logs",
        "https://pipelines.actions.githubusercontent.com:444/logs",
        "https://pipelines.actions.githubusercontent.com.example.invalid/logs",
        "https://example.blob.core.windows.net/logs#fragment",
        "",
    ],
)
async def test_real_provider_refuses_unapproved_log_destination(log_provider, location):
    log_provider.location = location
    with pytest.raises(OperationRefusedError, match="log archive location is unavailable"):
        await fetch_provider_logs(log_provider)
    assert log_provider.events == ["api"]


async def test_real_provider_does_not_download_after_authority_revocation(log_provider):
    log_provider.revoked = True
    with pytest.raises(OperationRefusedError, match="operations authority changed"):
        await fetch_provider_logs(log_provider)
    assert log_provider.events == ["api", "reauthorized"]


@pytest.mark.parametrize("status", [302, 403])
async def test_real_provider_refuses_archive_redirects_and_failures(log_provider, status):
    log_provider.archive_status = status
    with pytest.raises(OperationRefusedError, match="log archive is unavailable"):
        await fetch_provider_logs(log_provider)
    assert log_provider.events == ["api", "reauthorized", "archive"]


async def test_real_provider_bounds_archive_download(log_provider, monkeypatch):
    monkeypatch.setattr(actions_logs, "MAX_LOG_ARCHIVE_BYTES", 64)
    log_provider.payload = b"x" * 65
    with pytest.raises(OperationRefusedError, match="log archive exceeds bound"):
        await fetch_provider_logs(log_provider)
    assert log_provider.events == ["api", "reauthorized", "archive"]


@pytest.mark.parametrize("status", [301, 303, 307, 308, 403, 500])
async def test_real_provider_refuses_unexpected_api_status(log_provider, status):
    log_provider.api_status = status
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_provider_logs(log_provider)
    assert log_provider.events == ["api"]
