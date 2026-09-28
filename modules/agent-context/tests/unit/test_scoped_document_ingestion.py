"""Valid ingestion writes the producer's namespace; absent ownership writes nothing."""

import importlib.util
import ipaddress
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))

from scope import IngestionScope
import url_denylist
from url_fetch import FetchResponse


def load_script(name):
    path = Path(__file__).resolve().parents[2] / "images" / "ingestion" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_scope_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("scope", "root"),
    [
        (IngestionScope(visibility="tenant", tenant_id="team-a"), "tenants/team-a/content"),
        (IngestionScope(visibility="personal", owner_sub="alice"), "users/alice/content"),
        (IngestionScope(), "content"),
        (None, None),
    ],
)
@pytest.mark.parametrize("kind", ["url", "doc"])
async def test_ingestion_writes_only_the_validated_scope(monkeypatch, scope, root, kind):
    module = load_script(f"ingest-{kind}")
    for key in IngestionScope().to_env():
        monkeypatch.delenv(key, raising=False)
    if scope:
        for key, value in scope.to_env().items():
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        module,
        "settings",
        SimpleNamespace(
            s3_bucket_name="offline-test", s3_content_prefix="content", aws_region="us-east-1"
        ),
    )
    monkeypatch.setattr(module, "STAGE_TRACKING_AVAILABLE", False)
    monkeypatch.setattr(
        url_denylist, "_resolve_hostname", lambda _: [ipaddress.ip_address("93.184.216.34")]
    )
    s3 = Mock()
    factory = Mock(return_value=s3)
    monkeypatch.setattr("boto3.client", factory)
    url = "https://docs.example/handbook.html"
    if kind == "url":
        monkeypatch.setattr(module, "discover_pages", lambda *_: [url])
        monkeypatch.setattr(
            module,
            "crawl_url",
            AsyncMock(return_value="# Public handbook\n" + "Documentation. " * 10),
        )
        result = await module.ingest_url(url)
        suffix = "web/docs.example/handbook.md"
    else:
        monkeypatch.setattr(module, "GRAPHRAG_ENABLED", False)
        monkeypatch.setattr(
            module,
            "guarded_fetch",
            Mock(
                return_value=FetchResponse(
                    url=url,
                    status_code=200,
                    headers={"content-type": "text/html"},
                    content=b"<p>Documentation</p>",
                )
            ),
        )
        monkeypatch.setattr(
            module, "convert_to_markdown", lambda *_: "# Public handbook\nDocumentation"
        )
        result = module.ingest_document(url)
        suffix = "docs/docs.example-handbook.html.md"
    if root is None:
        assert result["status"] == "refused"
        factory.assert_not_called()
    else:
        s3.put_object.assert_called_once()
        assert s3.put_object.call_args.kwargs["Key"] == f"{root}/{suffix}"
        assert s3.put_object.call_args.kwargs["Bucket"] == "offline-test"
