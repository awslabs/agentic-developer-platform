"""Actual image proof is explicit; a skipped local check is not image evidence."""

import os
import runpy
from pathlib import Path

import pytest


def test_adapter_image_definition_excludes_core_sources_and_dependencies():
    package = Path(__file__).resolve().parents[1]
    dockerfile = (package / "Dockerfile").read_text()
    lockfile = (package / "uv.lock").read_text().lower()
    assert "COPY modules/tools/adp_tools/ /build/tools/adp_tools/" in dockerfile
    assert "COPY modules/tools/adapters/tool_adapters/ ./tool_adapters/" in dockerfile
    assert "RUN uv sync --frozen --no-dev --no-editable --no-cache" in dockerfile
    assert "COPY . " not in dockerfile and "modules/gateway/" not in dockerfile
    runtime = dockerfile.rsplit("FROM public.ecr.aws/docker/library/python:", 1)[1]
    assert "COPY --from=build /opt/adapter-venv /opt/adapter-venv" in runtime
    assert "COPY modules/" not in runtime
    assert all(name not in lockfile for name in ('name = "sqlalchemy"', 'name = "asyncpg"', 'name = "adp-gateway"'))


@pytest.mark.skipif(
    os.environ.get("ADP_ADAPTER_IMAGE_TEST") != "1", reason="Requires the explicit Docker image qualification job"
)
def test_standalone_image(tmp_path):
    source = os.environ["ADP_SOURCE_SHA"]
    script = Path(__file__).resolve().parents[1] / "scripts" / "image_smoke.py"
    result = runpy.run_path(str(script))["verify"](source_revision=source, evidence_dir=tmp_path)
    assert result["passed"] is True
    assert result["cleanup_confirmed"] is True
    assert result["network_mode"] == "none"
    assert result["gateway_and_sql_absent"] is True
