"""Offline release checks; live selectors require an approved deployed target."""

import hashlib
import json
import os
import ssl
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/L5-services"
CONTRACTS = ROOT / "docs/l5-services/contracts/v1"


def build_wheel(tmp_path):
    result = subprocess.run(
        [str(MODULE / "scripts/build-package.sh"), str(tmp_path)],
        check=True, capture_output=True, text=True,
    )
    return Path(result.stdout.strip().splitlines()[-1])


def test_packaged_contracts_match_reviewed_sources_and_import_in_isolation(tmp_path):
    wheel = build_wheel(tmp_path)
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == wheel.with_suffix(".whl.sha256").read_text().split()[0]
    with zipfile.ZipFile(wheel) as archive:
        bundle = archive.read("l5_services/certs/rds-global-bundle.pem").decode("ascii")
        context = ssl.create_default_context(cadata=bundle)
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert any("Amazon RDS us-east-1 Root CA RSA2048 G1" in str(cert["subject"])
                   for cert in context.get_ca_certs())
        for source in CONTRACTS.iterdir():
            if source.suffix in {".json", ".tsv", ".md"}:
                assert archive.read(f"l5_services/contracts/v1/{source.name}") == source.read_bytes()
        assert "l5_services/workers/execution.py" in archive.namelist()
        assert "l5_services/runtime.py" in archive.namelist()
        assert "l5_services/runtime_support.py" in archive.namelist()
        assert "l5_services/installed_release_probe.py" in archive.namelist()
        assert "l5_services/recovery_probe.py" in archive.namelist()
        assert "l5_services/scope_probe.py" in archive.namelist()
        assert "l5_services/migrations/versions/l5_worker_repair.py" in archive.namelist()

    subprocess.run(
        ["uv", "run", "--no-project", "--python", "3.12", "--with", str(wheel),
         "python", "-I", "-c",
         "from importlib.resources import files; "
         "from l5_services.workers.execution import lambda_handler; "
         "from l5_services.runtime_support import ScopedSources, queue_publisher; "
         "from l5_services.installed_release_probe import InstalledFixture; "
         "from l5_services.recovery_probe import RecoveryFixture, verify_recovery; "
         "from l5_services.scope_probe import ScopeFixture, verify_effective_scope; "
         "assert files('l5_services').joinpath('migrations/versions/l5_worker_repair.py').is_file(); "
         "assert files('l5_services').joinpath('contracts/v1/schemas.json').is_file()"],
        check=True, cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": ""},
    )


def test_packaged_security_mcp_excludes_devops_only_operations(tmp_path):
    wheel = build_wheel(tmp_path)
    program = """
import asyncio
import sys
from importlib.resources import files
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
sys.path.insert(1, sys.argv[2])

import httpx
from fastapi import FastAPI
with patch("boto3.client", return_value=object()):
    from src import l5_integration
from src.internal import tools_l5

assert str(files("l5_services")).startswith(sys.argv[1])
devops_tools = {"l5___get_devops_prevention", "l5___configure_devops_prevention"}
packaged_tools = {tool["name"].replace(".", "___", 1) for tool in tools_l5.l5_tool_catalogue()["tools"]}
assert devops_tools <= packaged_tools

class NoProductData:
    def __getattr__(self, name):
        raise AssertionError("Unavailable operations must not access Product data: " + name)

app = FastAPI()
app.include_router(l5_integration.router)
app.dependency_overrides[l5_integration.get_current_user] = lambda: SimpleNamespace(
    account_type="service", org_id="tenant-example", agent_registry_id="registry-example")
app.dependency_overrides[l5_integration.get_db] = NoProductData

async def verify():
    with patch.object(l5_integration, "backend", side_effect=AssertionError("Unavailable backend")) as backend:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            listing = await client.post("/v1/l5/mcp/tools/list")
            assert listing.status_code == 200, listing.text
            advertised = {tool["name"] for tool in listing.json()["tools"]}
            assert not devops_tools & advertised, advertised
            assert {"l5___get_configuration", "l5___submit_run"} <= advertised
            for tool in sorted(devops_tools):
                for service_id in ("security", "devops"):
                    arguments = {"product_id": "product-example", "project_id": "project-example",
                                 "service_id": service_id, "binding_id": "binding-example"}
                    if tool == "l5___configure_devops_prevention":
                        arguments.update(input={"configuration_revision": 1, "action": "enable"},
                                         idempotency_key="prevention-example-key", if_match="opaque-etag")
                    response = await client.post("/v1/l5/mcp/tools/call", json={"tool": tool, "arguments": arguments})
                    assert response.status_code == 503, response.text
                    assert response.json()["detail"] == "L5 tool operation unavailable"
        backend.assert_not_called()

asyncio.run(verify())
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", program, str(wheel), str(ROOT / "modules/gateway")],
        cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "BG_TOKEN_SECRET_KEY": "offline-test-secret-not-for-production",
             "BG_DATABASE_URL": "sqlite+aiosqlite:///:memory:", "AWS_REGION": "us-east-1",
             "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
             "AWS_EC2_METADATA_DISABLED": "true"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_gateway_release_readback_rejects_mismatched_artifacts(tmp_path):
    wheel = build_wheel(tmp_path)
    other = build_wheel(tmp_path / "second")
    assert hashlib.sha256(wheel.read_bytes()).digest() == hashlib.sha256(other.read_bytes()).digest()
    expected = hashlib.sha256(wheel.read_bytes()).hexdigest()
    command = [
        "uv", "run", "--no-project", "--python", "3.12", "--with", str(wheel),
        "python", "-I", "-c",
        "import json; from pathlib import Path; "
        "from l5_services.release_identity import release_identity; "
        f"print(json.dumps(release_identity(Path({str(tmp_path)!r}))))",
    ]
    observed = subprocess.run(command, check=True, cwd=tmp_path, capture_output=True, text=True)
    assert json.loads(observed.stdout)["l5_wheel_sha256"] == expected
    assert json.loads(observed.stdout)["l5_schema_sha256"] == hashlib.sha256(
        (CONTRACTS / "schemas.json").read_bytes()
    ).hexdigest()
    wheel.with_suffix(".whl.sha256").write_text("0" * 64 + "  " + wheel.name + "\n")
    rejected = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert rejected.returncode != 0


def test_worker_archive_rejects_changed_wheel_and_contains_pinned_package(tmp_path):
    wheel = build_wheel(tmp_path)
    output = tmp_path / "workers"
    subprocess.run(
        [str(MODULE / "scripts/build-workers.sh"), str(wheel), str(output)],
        check=True, capture_output=True, text=True,
    )
    with zipfile.ZipFile(output / "l5-security-workers.zip") as archive:
        with zipfile.ZipFile(wheel) as package:
            assert archive.read("l5_services/certs/rds-global-bundle.pem") == package.read("l5_services/certs/rds-global-bundle.pem")
            assert archive.read("l5_services/contracts/v1/schemas.json") == package.read(
                "l5_services/contracts/v1/schemas.json"
            )
        assert "l5_services/workers/recovery.py" in archive.namelist()
        for name in ("runtime.py", "adapters/security/code_review.py",
                     "adapters/security/threat_model.py", "adapters/security/model_binding.py",
                     "security_registration.py"):
            entry = "l5_services/" + name
            with zipfile.ZipFile(wheel) as package:
                assert archive.read(entry) == package.read(entry)
        assert "boto3/__init__.py" in archive.namelist()
        assert "botocore/__init__.py" in archive.namelist()
        assert "asyncpg/__init__.py" in archive.namelist()
        assert archive.read(f"release/{wheel.name}") == wheel.read_bytes()
        assert json.loads(archive.read("release/wheel.json"))["wheel_sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert hashlib.sha256((output / "l5-security-workers.zip").read_bytes()).hexdigest() == (
        output / "l5-security-workers.zip.sha256"
    ).read_text().split()[0]
    packaged = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); "
         "from l5_services.runtime import factory; "
         "from l5_services.adapters.security.code_review import CodeReviewAdapter; "
         "from l5_services.adapters.security.threat_model import ThreatModelAdapter; "
         "assert callable(factory) and CodeReviewAdapter and ThreatModelAdapter",
         str(wheel)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert packaged.returncode == 0, packaged.stderr

    with wheel.open("ab") as artifact:
        artifact.write(b"tampered")
    for rejected_output in (tmp_path / "rejected", tmp_path / "rejected.zip"):
        rejected = subprocess.run(
            [str(MODULE / "scripts/build-workers.sh"), str(wheel), str(rejected_output)],
            capture_output=True, text=True,
        )
        assert rejected.returncode != 0
        assert not rejected_output.exists()


def test_worker_archive_executes_without_development_site_packages(tmp_path):
    wheel = build_wheel(tmp_path / "wheel")
    output = tmp_path / "workers.zip"
    subprocess.run(
        [str(MODULE / "scripts/build-workers.sh"), str(wheel), str(output)],
        check=True, capture_output=True, text=True,
    )
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(output) as archive:
        archive.extractall(extracted)
    script = """import asyncio
import sys
from pathlib import Path

archive_root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(archive_root))
from l5_services.workers.execution import ExecutionService, load_service
import os
os.environ["L5_WORKER_FACTORY"] = "l5_services.runtime:factory"
try:
    load_service()
except RuntimeError as error:
    assert "Lambda workload" in str(error)
else:
    raise AssertionError("missing installed transport was accepted")

from types import SimpleNamespace
from unittest.mock import patch
from l5_services import runtime
from l5_services.adapters.security.code_review import CodeReviewAdapter
from l5_services.adapters.security.threat_model import ThreatModelAdapter

transport = SimpleNamespace(post=lambda *args: None, request=lambda *args: None,
                            stream=lambda *args: None)
archive = SimpleNamespace(put_archive=lambda *args: None, get_archive=lambda *args: None)
with (patch.object(runtime, "_gateway_transport", return_value=transport),
      patch.object(runtime, "_sessions", return_value=lambda: None),
      patch.object(runtime, "_archive_store", return_value=archive),
      patch.object(runtime, "_queue_publisher", return_value=lambda *args: None)):
    installed = load_service()
    assert isinstance(installed.adapter, CodeReviewAdapter)
    assert isinstance(installed.security_threat_adapter, ThreatModelAdapter)
    try:
        runtime.factory(allow_override=True)
    except TypeError:
        pass
    else:
        raise AssertionError("unsupported factory override was accepted")
    transport.stream = None
    try:
        runtime.factory()
    except RuntimeError as error:
        assert "dependencies unavailable" in str(error)
    else:
        raise AssertionError("missing Product source callback was accepted")
    transport.stream = lambda *args: None
    archive.get_archive = None
    try:
        runtime.factory()
    except RuntimeError as error:
        assert "dependencies unavailable" in str(error)
    else:
        raise AssertionError("missing archive read callback was accepted")
    archive.get_archive = lambda *args: None
    with patch.object(runtime, "_provider_session", None):
        try:
            runtime.factory()
        except RuntimeError as error:
            assert "dependencies unavailable" in str(error)
        else:
            raise AssertionError("missing provider session callback was accepted")

service = ExecutionService(None, None, None, None, None)
assert asyncio.run(service.execute({})) is False
import rfc8785
assert Path(rfc8785.__file__).is_relative_to(archive_root)
"""
    subprocess.run(
        ["uv", "run", "--no-project", "--python", "3.12", "python", "-I", "-S",
         "-c", script, str(extracted)],
        check=True, cwd=tmp_path, capture_output=True, text=True,
    )


def test_main_and_security_dependencies_survive_in_the_isolated_wheel(tmp_path):
    import tomllib

    configuration = tomllib.loads((MODULE / "pyproject.toml").read_text())
    assert configuration["build-system"]["requires"] == ["setuptools==84.2.1"]
    assert set(configuration["project"]["dependencies"]) >= {
        "asyncpg==0.30.0", "boto3==1.43.111", "rfc8785==0.1.4",
    }
    assert "contracts/v1/*.json" in configuration["tool"]["setuptools"]["package-data"]["l5_services"]

    wheel = build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        metadata_file = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_file).decode()
    assert "Requires-Dist: asyncpg==0.30.0" in metadata
    assert "Requires-Dist: boto3==1.43.111" in metadata
    assert "Requires-Dist: rfc8785==0.1.4" in metadata

    script = (
        "import boto3, asyncpg, rfc8785; "
        "from l5_services.scope_probe import ScopeFixture; "
        "from importlib.resources import files; "
        "assert files('l5_services').joinpath('contracts/v1/schemas.json').is_file()"
    )
    subprocess.run(
        ["uv", "run", "--no-project", "--python", "3.12", "--with", str(wheel),
         "python", "-I", "-c", script],
        check=True, cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": ""},
    )
