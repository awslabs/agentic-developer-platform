"""Failure evidence must identify the code path without disclosing authority."""

import json

import httpx
import pytest

from harness_jobs.identity import OperationRefused
from superplane_executor import task_worker
from superplane_executor.authority import GatewayAuthority


def test_entrypoint_reports_locations_without_exception_text_or_locals(
    monkeypatch, capsys
):
    async def failed_run(stop):
        credential = "secret-provider-credential"
        raise RuntimeError("provider response: " + credential)

    monkeypatch.setattr(task_worker, "run", failed_run)
    with pytest.raises(SystemExit, match="durable recovery required"):
        task_worker.main()
    output = capsys.readouterr().err
    report = json.loads(output)
    assert report["exception_type"] == "RuntimeError"
    assert report["frames"][-1]["function"] == "failed_run"
    assert 1 <= len(report["frames"]) <= 12
    assert all(set(frame) == {"file", "line", "function"} for frame in report["frames"])
    assert "secret-provider-credential" not in output
    assert "provider response" not in output
    assert all("/" not in frame["file"] for frame in report["frames"])


@pytest.mark.parametrize(
    "status,body", [(403, "secret-refusal-body"), (200, "x" * 262145)]
)
async def test_authority_logs_only_path_status_and_size_flag(
    monkeypatch, capsys, tmp_path, status, body
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text=body))
    ) as client:
        authority = GatewayAuthority(
            endpoint="https://example.execute-api.us-east-1.amazonaws.com/dev",
            region="us-east-1",
            run_credential_file=tmp_path / "run",
            workload_token_file=tmp_path / "workload",
            client=client,
        )
        monkeypatch.setattr(authority, "_headers", lambda *args, **kwargs: {})
        with pytest.raises(OperationRefused):
            await authority.post(
                "/internal/v1/controller-execution/authority",
                {"secret": "request-secret"},
            )
    report = json.loads(capsys.readouterr().err)
    assert report == {
        "event": "paid_authority_response_refused",
        "path": "/internal/v1/controller-execution/authority",
        "status_code": status,
        "oversized": len(body) > 262144,
    }
