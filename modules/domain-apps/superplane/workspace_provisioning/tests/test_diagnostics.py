"""Provider uncertainty keeps its meaning while the original failure is visible."""

import json

import pytest

from workspace_provisioning.diagnostics import diagnostic_hook, report_failure


@pytest.mark.asyncio
async def test_hook_preserves_exception_and_its_private_chained_cause(capsys):
    private = "secret-token-and-provider-response"
    original = None

    async def provider(intent):
        nonlocal original
        assert intent == "original-intent"
        try:
            raise ValueError(private)
        except ValueError as cause:
            original = RuntimeError(private)
            raise original from cause

    with pytest.raises(RuntimeError) as failure:
        await diagnostic_hook(provider)("original-intent")
    assert failure.value is original
    output = capsys.readouterr()
    assert not output.out and private not in output.err
    record = json.loads(output.err)
    assert record["event"] == "lifecycle_provider_failed"
    assert [e["exception_type"] for e in record["exceptions"]] == [
        "RuntimeError",
        "ValueError",
    ]
    assert record["exceptions"][1]["frames"][-1]["function"] == "provider"
    for error in record["exceptions"]:
        for frame in error["frames"]:
            assert set(frame) == {"file", "line", "function"}
            assert "/" not in frame["file"]


@pytest.mark.asyncio
async def test_success_is_unchanged_and_silent(capsys):
    result = object()

    async def provider(intent):
        return result

    assert await diagnostic_hook(provider)(None) is result
    assert capsys.readouterr().err == ""


def test_cyclic_chain_is_bounded_and_suppressed_context_stays_private(capsys):
    error = RuntimeError("private")
    error.__cause__ = error
    report_failure(error)
    assert len(json.loads(capsys.readouterr().err)["exceptions"]) == 1
    error.__cause__ = None
    error.__context__ = ValueError("private")
    error.__suppress_context__ = True
    report_failure(error)
    assert len(json.loads(capsys.readouterr().err)["exceptions"]) == 1
