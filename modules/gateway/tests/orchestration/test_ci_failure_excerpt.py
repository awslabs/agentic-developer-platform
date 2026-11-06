"""Failure diagnostics survive verbose cleanup without changing CI outcomes."""

import httpx
import pytest

from src.orchestration import ci_failure_evidence


@pytest.mark.parametrize(
    "diagnostic",
    [
        "FAILED tests/test_routes.py::test_inventory - AssertionError: unexpected route",
        "E   AssertionError: expected one successful admission",
        "Traceback (most recent call last):\nRuntimeError: unable to load configuration",
        "error[E0425]: cannot find value `widget`",
        "src/main.go:12: error: undefined: widget",
        "FAIL src/widget.test.ts\nExpected 1, received 2",
    ],
)
def test_diagnostic_before_verbose_cleanup_is_preserved(diagnostic):
    log = "Setup completed\n" * 500 + diagnostic + "\nPost job cleanup\n" * 1000

    excerpt = ci_failure_evidence.failure_excerpt(log, limit=3100)

    assert diagnostic in excerpt
    assert len(excerpt) <= 3100


def test_multiple_failures_keep_their_context():
    diagnostics = [f"FAILED tests/test_worker.py::test_case_{index}\nAssertionError: case {index}" for index in range(3)]
    log = "\n".join(diagnostic + "\ncleanup line\n" * 500 for diagnostic in diagnostics)

    excerpt = ci_failure_evidence.failure_excerpt(log, limit=3100)

    assert all(diagnostic in excerpt for diagnostic in diagnostics)
    assert len(excerpt) <= 3100


def test_long_line_cannot_hide_diagnostic():
    diagnostic = "error: compiler rejected source"
    log = "x" * 100_000 + " " + diagnostic + "\nPost job cleanup\n" * 1000

    assert diagnostic in ci_failure_evidence.failure_excerpt(log, limit=3100)


def test_cleanup_tracebacks_do_not_displace_explicit_failure():
    cleanup = "Traceback (most recent call last):\nValueError: closed log stream\n" * 100
    diagnostic = "FAILED tests/test_worker.py::test_concurrency\nAssertionError: unexpected result"
    log = cleanup + diagnostic + "\nPost job cleanup\n" * 1000

    assert diagnostic in ci_failure_evidence.failure_excerpt(log, limit=3100)


def test_unrecognized_output_falls_back_to_tail():
    log = "normal progress\n" * 1000 + "unexpected compiler exit"

    assert ci_failure_evidence.failure_excerpt(log, limit=3100) == log[-3100:]
    assert ci_failure_evidence.failure_excerpt("short output", limit=3100) == "short output"


async def test_download_retains_bounded_context_before_last_sixty_lines(monkeypatch):
    signed_url = "https://pipelines.actions.githubusercontent.com/logs?sig=fictional-signature"
    diagnostic = b"FAILED tests/test_worker.py::test_inventory\nAssertionError: unexpected route\n"
    payload = b"old setup\n" * 30_000 + diagnostic + b"Post job cleanup\n" * 1000

    def api_response(request):
        return httpx.Response(302, headers={"Location": signed_url})

    def signed_response(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, stream=httpx.ByteStream(payload))

    client_type = httpx.AsyncClient

    def signed_client(**kwargs):
        return client_type(transport=httpx.MockTransport(signed_response), **kwargs)

    async with client_type(transport=httpx.MockTransport(api_response), trust_env=False) as client:
        monkeypatch.setattr(ci_failure_evidence.httpx, "AsyncClient", signed_client)
        log, reason = await ci_failure_evidence.download_job_log(client, {"Authorization": "Bearer fictional-token"}, "fictional/sample", 90)

    assert reason is None
    assert log == payload[-ci_failure_evidence.TAIL_BYTES :].decode()
    assert diagnostic.decode() in log
    assert diagnostic.decode() in ci_failure_evidence.failure_excerpt(log, limit=3100)
