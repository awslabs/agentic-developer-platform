"""Provider-shaped authenticated pull; no live API is called."""

import hashlib
import io
import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.orchestration.evaluation_evidence import EvaluationEvidenceError
from src.orchestration.evaluation_provider import EvaluationProvider
from tests.orchestration.test_evaluation_evidence import evaluation  # noqa: F401


@pytest.fixture
async def provider(evaluation):  # noqa: F811
    ctx = evaluation
    ctx.data["started_at"] = (datetime.now(UTC) - timedelta(seconds=20)).isoformat()
    ctx.expected = replace(
        ctx.expected,
        deployment=ctx.expected.deployment.model_copy(
            update={"observed_at": datetime.now(UTC) - timedelta(seconds=30), "valid_until": datetime.now(UTC) + timedelta(minutes=5)}
        ),
    )
    ctx.data["completed_at"] = datetime.now(UTC).isoformat()
    ctx.data["expires_at"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    ctx.run = dict(
        id=42,
        run_attempt=1,
        repository={"id": 123},
        head_repository={"id": 123},
        path=ctx.expected.specification["runner"]["workflow_path"],
        head_sha="c" * 40,
        event="workflow_dispatch",
        status="completed",
        conclusion="success",
    )
    ctx.corrupt = False
    ctx.expired = False
    ctx.zip_override = None
    ctx.metadata_size = None

    def archive():
        if ctx.zip_override is not None:
            return ctx.zip_override
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as zipped:
            zipped.writestr("evaluation-receipt.json", json.dumps(ctx.data))
            for path, payload in ctx.evidence.artifacts.items():
                zipped.writestr(path, payload)
        return data.getvalue()

    def response(request):
        assert request.headers["Authorization"] == "Bearer scoped-test"
        if request.url.path == "/repos/example/repository":
            return httpx.Response(200, json={"id": 123})
        if request.url.path.endswith("/runs/42"):
            return httpx.Response(200, json=ctx.run)
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        dict(
                            id=88,
                            name="orchestration-evaluation-42-1",
                            expired=ctx.expired,
                            size_in_bytes=ctx.metadata_size if ctx.metadata_size is not None else len(archive()),
                            digest="sha256:" + ("0" * 64 if ctx.corrupt else hashlib.sha256(archive()).hexdigest()),
                        )
                    ]
                },
            )
        if request.url.path.endswith("/88/zip"):
            return httpx.Response(200, content=archive())
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        ctx.provider = EvaluationProvider(client=client)
        ctx.provider.token = AsyncMock(return_value="scoped-test")
        ctx.binding = SimpleNamespace(org_id="org-a", repo="example/repository", provider_repository_id=123)
        yield ctx


async def test_authenticated_pull_adapter_binds_provider_run_and_hash(provider):
    result = await provider.provider.observe(provider.binding, provider.expected, run_id=42)
    assert result.mandatory_passed and result.artifact_ref.endswith("/88")


@pytest.mark.parametrize("failure", ["fork", "harness", "workflow", "event", "tenant", "hash", "attempt", "cancelled"])
async def test_provider_cannot_authenticate_foreign_or_wrong_run(provider, failure):
    if failure == "fork":
        provider.run["head_repository"]["id"] = 999
    elif failure == "harness":
        provider.run["head_sha"] = "0" * 40
    elif failure == "workflow":
        provider.run["path"] = ".github/workflows/arbitrary.yml"
    elif failure == "event":
        provider.run["event"] = "pull_request"
    elif failure == "tenant":
        provider.binding.org_id = "foreign"
    elif failure == "hash":
        provider.corrupt = True
    elif failure == "attempt":
        provider.run["run_attempt"] = 2
    else:
        provider.run["conclusion"] = "cancelled"
    with pytest.raises(EvaluationEvidenceError):
        await provider.provider.observe(provider.binding, provider.expected, run_id=42)


async def test_authentic_failed_criterion_is_returned_for_e2(provider):
    provider.run["conclusion"] = "failure"
    provider.data["criteria"][0]["outcome"] = "fail"
    result = await provider.provider.observe(provider.binding, provider.expected, run_id=42)
    assert not result.mandatory_passed and result.required_failures == ("API-1",)


async def test_in_progress_run_has_no_completed_evidence(provider):
    provider.run["status"] = "in_progress"
    assert await provider.provider.observe(provider.binding, provider.expected, run_id=42) is None


@pytest.mark.parametrize(
    "failure", ["expired", "metadata_size", "corrupt_zip", "traversal", "duplicates", "receipt_size", "entry_count", "missing_receipt"]
)
async def test_bounded_archive_refuses_invalid_or_unavailable_evidence(provider, failure):
    from src.orchestration.evaluation_provider import MAX_ARCHIVE, MAX_RECEIPT

    if failure == "expired":
        provider.expired = True
    elif failure == "metadata_size":
        provider.metadata_size = MAX_ARCHIVE + 1
    elif failure == "corrupt_zip":
        provider.zip_override = b"invalid zip"
    else:
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as zipped:
            if failure == "traversal":
                zipped.writestr("../escape", b"forged")
            elif failure == "duplicates":
                zipped.writestr("evaluation-receipt.json", b"one")
                with pytest.warns(UserWarning):
                    zipped.writestr("evaluation-receipt.json", b"two")
            elif failure == "receipt_size":
                zipped.writestr("evaluation-receipt.json", b"x" * (MAX_RECEIPT + 1))
            elif failure == "entry_count":
                for n in range(258):
                    zipped.writestr(str(n), b"x")
            else:
                zipped.writestr("evidence.json", b"{}")
        provider.zip_override = data.getvalue()
    with pytest.raises(EvaluationEvidenceError):
        await provider.provider.observe(provider.binding, provider.expected, run_id=42)


async def test_missing_scoped_credential_returns_typed_unavailable(provider):
    from src.orchestration.evaluation_evidence import EvidenceRefusal
    from src.orchestration.review_cycle import CycleBlockedError

    provider.provider.token.side_effect = CycleBlockedError("unavailable")
    with pytest.raises(EvaluationEvidenceError) as error:
        await provider.provider.observe(provider.binding, provider.expected, run_id=42)
    assert error.value.reason is EvidenceRefusal.PROVIDER_UNAVAILABLE


async def test_find_selects_only_correlated_pinned_completed_runs(provider):
    ctx = provider
    request = ctx.provider.request
    calls = []

    async def listing(binding, method, path, **kwargs):
        if "/workflows/" in path:
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {**ctx.run, "id": 99, "display_title": "unrelated"},
                        {**ctx.run, "id": 98, "display_title": "ADP evaluation execution", "status": "in_progress"},
                        {**ctx.run, "id": 97, "display_title": "ADP evaluation execution", "head_sha": "0" * 40},
                        {**ctx.run, "display_title": "ADP evaluation execution"},
                    ]
                },
            )
        calls.append(path)
        return await request(binding, method, path, **kwargs)

    ctx.provider.request = listing
    result = await ctx.provider.find(ctx.binding, ctx.expected)
    assert result.mandatory_passed and result.receipt.producer.run_id == 42
    assert not any("/runs/99" in path or "/runs/98" in path or "/runs/97" in path for path in calls)


@pytest.mark.parametrize("failure", ["oversized", "malformed", "unavailable"])
async def test_find_bounds_provider_listing_and_maps_errors(provider, failure):
    async def listing(*args, **kwargs):
        if failure == "unavailable":
            raise httpx.ReadTimeout("offline")
        return httpx.Response(200, json={"workflow_runs": [{}] * 21 if failure == "oversized" else {}})

    provider.provider.request = listing
    with pytest.raises(EvaluationEvidenceError):
        await provider.provider.find(provider.binding, provider.expected)
