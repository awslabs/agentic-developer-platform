"""Exercise immutable runtime images on an owned, disconnected fixture network."""

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from foundation_contract import DEPENDENCIES, TESTS, qualified, require

ROOT = Path(__file__).resolve().parents[3]
MARKER = "ADP_FOUNDATION_RESULT_V1 "
GATEWAY_FILES = [
    "src/shared/identity/oauth_setup.py",
    "src/shared/identity/atlassian_client_registration.py",
    "src/shared/identity/oauth_setup_worker.py",
    "src/shared/identity/oauth_setup_store.py",
    "src/admin/connections/oauth_apps.py",
    "src/auth/atlassian_mcp_routes.py",
    "src/orchestration/environment_leases.py",
    "src/shared/models/oauth_setup.py",
]
ADAPTER_FILES = ["tool_adapters/server.py", "tool_adapters/config.py", "tool_adapters/caller_identity.py"]
ADAPTER_TEST_DEPENDENCIES = {"pytest", "pluggy", "iniconfig", "packaging", "pygments"}


def run(args, *, timeout=60, check=True, input=None):
    result = subprocess.run(args, capture_output=True, timeout=timeout, input=input)
    if check:
        require(result.returncode == 0)
    require(len(result.stdout) <= 4 * 1024 * 1024)
    return result


def docker(*args, **kwargs):
    return run(["docker", *args], **kwargs)


def image_observation(container, files, import_name):
    script = """
import hashlib,importlib,importlib.metadata,json,pathlib,sys
module=importlib.import_module(sys.argv[1])
root=pathlib.Path(module.__file__).parent
root=pathlib.Path('/app') if sys.argv[1].startswith('src.') else root.parent
files=json.loads(sys.argv[2])
hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}
distributions={}
for dist in importlib.metadata.distributions():
 distributions[dist.metadata['Name'].lower().replace('_','-')]=dist.version
digest=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
print(json.dumps({'import':module.__file__,'sha256':digest,'distributions':distributions}))
"""
    return json.loads(docker("exec", container, "python", "-c", script, import_name, json.dumps(files)).stdout)


def execute(env, directory):
    stage = env.get("ADP_QUALIFICATION_STAGE", "foundation")
    require(stage in {"foundation", "identity"})
    if stage == "identity":
        from identity_contract import (
            ADAPTER_FILES as adapter_files,
        )
        from identity_contract import (
            ADAPTER_TESTS as adapter_tests,
        )
        from identity_contract import (
            GATEWAY_FILES as gateway_files,
        )
        from identity_contract import (
            TESTS as tests,
        )

        gateway_import = "src.auth.atlassian_mcp_routes"
    else:
        adapter_files, gateway_files, tests = ADAPTER_FILES, GATEWAY_FILES, TESTS
        gateway_import = "src.shared.identity.oauth_setup"
    source, gateway_source = env["ADP_SOURCE_SHA"], env["ADP_FOUNDATION_GATEWAY_REVISION"]
    gateway_image, adapter_image = env["ADP_FOUNDATION_GATEWAY_IMAGE"], env["ADP_FOUNDATION_ADAPTER_IMAGE"]
    account, region, environment = env["ACCOUNT_ID"], env["AWS_REGION"], env["ADP_TOOLS_ENVIRONMENT"]
    project = f"adp-tools-{environment}-adapters-foundation-check"
    build_id = env["CODEBUILD_BUILD_ID"]
    require(re.fullmatch(r"[0-9a-f]{40}", source) and re.fullmatch(r"[0-9a-f]{40}", gateway_source))
    require(environment == "dev" and re.fullmatch(r"[0-9]{12}", account))
    require(re.fullmatch(re.escape(project) + r":[0-9a-f-]{36}", build_id))
    registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    for image, repository in ((gateway_image, "adp-gateway"), (adapter_image, f"adp-tools-{environment}-adapters")):
        require(re.fullmatch(re.escape(f"{registry}/{repository}@sha256:") + "[0-9a-f]{64}", image))
    postgres_image = env["ADP_FOUNDATION_POSTGRES_IMAGE"]
    require(
        re.fullmatch(
            r"(?:public\.ecr\.aws/docker/library/|docker\.io/library/)?postgres(?::[a-zA-Z0-9_.-]+)?@sha256:[0-9a-f]{64}",
            postgres_image,
        )
    )
    identity = json.loads(run(["aws", "sts", "get-caller-identity", "--output", "json"]).stdout)
    require(
        identity["Account"] == account and identity["Arn"].startswith(f"arn:aws:sts::{account}:assumed-role/{project}/")
    )
    password = run(["aws", "ecr", "get-login-password", "--region", region]).stdout
    docker("login", "--username", "AWS", "--password-stdin", registry, input=password)
    for image in (gateway_image, adapter_image, postgres_image):
        docker("pull", image, timeout=300)
    owner = "adp-foundation-" + uuid.uuid4().hex
    # The gateway image constructs AuthService/TokenManager while src.auth is imported,
    # and TokenManager refuses to load without BG_TOKEN_SECRET_KEY. image_observation
    # imports that package directly, before any gateway conftest could provide one, so
    # the isolated fixture generates its own single-use value here. This is never a
    # deployment secret: it exists only for this disconnected container, is not read
    # from the build environment, and is discarded with the fixture.
    fixture_token_secret_key = secrets.token_urlsafe(48)
    containers = {name: owner + "-" + name for name in ("gateway", "adapter", "postgres")}
    report = {
        "schema_version": 1,
        "stage_id": stage,
        "source_revision": source,
        "gateway_revision": gateway_source,
        "gateway_image": gateway_image,
        "adapter_image": adapter_image,
        "build_id": build_id,
        "expected_gateway_source_sha256": env["ADP_FOUNDATION_GATEWAY_HASH"],
        "expected_adapter_source_sha256": env["ADP_FOUNDATION_ADAPTER_HASH"],
    }
    configuration = directory / "adapter.json"
    configuration.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "mode": "foundation",
                "release": {
                    "contract_version": "1.0",
                    "source_revision": source,
                    "image_digest": adapter_image.split("@", 1)[1],
                    "group": "tool-connectors",
                    "group_generation": 1,
                    "backend_generation": 1,
                },
            }
        )
    )
    configuration.chmod(0o644)
    try:
        docker("network", "create", "--internal", "--label", "adp.foundation=" + owner, owner)
        docker(
            "run",
            "-d",
            "--name",
            containers["postgres"],
            "--network",
            owner,
            "--label",
            "adp.foundation=" + owner,
            "--tmpfs",
            "/var/lib/postgresql/data",
            "-e",
            "POSTGRES_PASSWORD=" + owner,
            postgres_image,
        )
        for attempt in range(60):
            if docker("exec", containers["postgres"], "pg_isready", "-U", "postgres", check=False).returncode == 0:
                break
            require(attempt < 59)
            time.sleep(1)

        def databases():
            return (
                docker(
                    "exec",
                    containers["postgres"],
                    "psql",
                    "-U",
                    "postgres",
                    "-At",
                    "-c",
                    "SELECT datname FROM pg_database ORDER BY datname",
                )
                .stdout.decode()
                .splitlines()
            )

        report["database_names_before"] = databases()
        wheels = directory / "wheels"
        wheels.mkdir(mode=0o755)
        common = [
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network",
            owner,
            "--label",
            "adp.foundation=" + owner,
            "--tmpfs",
            "/tmp:rw,noexec,mode=1777",
        ]
        docker(
            "run",
            "-d",
            "--name",
            containers["gateway"],
            *common,
            "--tmpfs",
            "/testdeps:rw,exec,nosuid,nodev,uid=65532,gid=65532,mode=0755",
            "-v",
            f"{ROOT / 'modules/gateway/tests'}:/app/tests:ro",
            "-v",
            f"{wheels}:/wheels:ro",
            "-e",
            "BG_TEST_POSTGRES_URI=" + f"postgresql://postgres:{owner}@{containers['postgres']}:5432/postgres",
            "-e",
            "BG_TOKEN_SECRET_KEY=" + fixture_token_secret_key,
            "-e",
            "AWS_EC2_METADATA_DISABLED=true",
            *(
                [
                    "-e",
                    "AWS_CONFIG_FILE=/dev/null",
                    "-e",
                    "AWS_SHARED_CREDENTIALS_FILE=/dev/null",
                    "-e",
                    "AWS_ACCESS_KEY_ID=fixture",
                    "-e",
                    "AWS_SECRET_ACCESS_KEY=fixture",
                ]
                if stage == "identity"
                else []
            ),
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "PYTHONPATH=/testdeps:/app",
            "--entrypoint",
            "python",
            gateway_image,
            "-c",
            "import time; time.sleep(1800)",
        )
        gateway = image_observation(containers["gateway"], gateway_files, gateway_import)
        report.update(gateway_import=gateway["import"], gateway_source_sha256=gateway["sha256"])
        locked = tomllib.loads((ROOT / "modules/gateway/uv.lock").read_text())
        requirements = [
            f"{package['name']}=={package['version']}"
            for package in locked["package"]
            if package["name"] in DEPENDENCIES and package["name"] not in gateway["distributions"]
        ]
        require(len({requirement.split("==")[0] for requirement in requirements}) == len(requirements))
        # The adapter has its own environment. A package already shipped in the
        # gateway can still be missing from the adapter's offline test mount.
        download_requirements = sorted(
            set(requirements)
            | {
                f"{package['name']}=={package['version']}"
                for package in locked["package"]
                if stage == "identity" and package["name"] in ADAPTER_TEST_DEPENDENCIES
            }
        )
        if download_requirements:
            run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "download",
                    "--only-binary=:all:",
                    "--no-deps",
                    "--python-version",
                    "313",
                    "--platform",
                    "manylinux2014_x86_64",
                    "--implementation",
                    "cp",
                    "--abi",
                    "cp313",
                    "--dest",
                    str(wheels),
                    *download_requirements,
                ],
                timeout=300,
            )
        if requirements:
            docker(
                "exec",
                containers["gateway"],
                "python",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--find-links",
                "/wheels",
                "--target",
                "/testdeps",
                *requirements,
                timeout=120,
            )
        test_script = (
            "import pytest; raise SystemExit(pytest.main("
            + repr(
                [
                    "-q",
                    "-o",
                    "asyncio_mode=auto",
                    "-o",
                    "junit_family=xunit1",
                    "--timeout=60",
                    "--junitxml=/tmp/foundation.xml",
                    "-p",
                    "no:cacheprovider",
                    *tests,
                ]
            )
            + "))"
        )
        tested = docker(
            "exec", "-w", "/app", containers["gateway"], "python", "-c", test_script, timeout=600, check=False
        )
        report["test_exit"] = tested.returncode
        report["junit"] = docker(
            "exec",
            containers["gateway"],
            "python",
            "-c",
            "from pathlib import Path; print(Path('/tmp/foundation.xml').read_text())",
        ).stdout.decode()
        report["database_names_after"] = databases()
        docker(
            "run",
            "-d",
            "--name",
            containers["adapter"],
            *common,
            "-v",
            f"{configuration}:/configuration.json:ro",
            *(
                [
                    "--tmpfs",
                    "/testdeps:rw,exec,nosuid,nodev,mode=1777",
                    "-v",
                    f"{ROOT / 'modules/tools/adapters/tests'}:/image_tests:ro",
                    "-v",
                    f"{wheels}:/wheels:ro",
                    "-e",
                    "PYTHONPATH=/testdeps",
                ]
                if stage == "identity"
                else []
            ),
            "-e",
            "ADP_TOOL_ADAPTER_CONFIG=/configuration.json",
            adapter_image,
        )
        adapter = image_observation(containers["adapter"], adapter_files, "tool_adapters.server")
        report.update(adapter_import=adapter["import"], adapter_source_sha256=adapter["sha256"])
        if stage == "identity":
            requirements = sorted(ADAPTER_TEST_DEPENDENCIES)
            docker(
                "exec",
                containers["adapter"],
                "/usr/local/bin/python",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--find-links",
                "/wheels",
                "--target",
                "/testdeps",
                *requirements,
                timeout=120,
            )
            adapter_script = (
                "import pytest; raise SystemExit(pytest.main("
                + repr(
                    [
                        "-q",
                        "-o",
                        "junit_family=xunit1",
                        "--junitxml=/tmp/adapter-identity.xml",
                        "-p",
                        "no:cacheprovider",
                        *adapter_tests,
                    ]
                )
                + "))"
            )
            adapter_result = docker(
                "exec",
                "-w",
                "/opt/adapter-venv",
                containers["adapter"],
                "python",
                "-c",
                adapter_script,
                timeout=150,
                check=False,
            )
            report["adapter_test_exit"] = adapter_result.returncode
            report["adapter_junit"] = docker(
                "exec",
                containers["adapter"],
                "python",
                "-c",
                "from pathlib import Path; print(Path('/tmp/adapter-identity.xml').read_text())",
            ).stdout.decode()
        script = (
            "import importlib.util,json; "
            "names=['src','sqlalchemy','alembic','asyncpg','psycopg2']; "
            "print(json.dumps([name for name in names if importlib.util.find_spec(name)]))"
        )
        report["forbidden_imports"] = json.loads(docker("exec", containers["adapter"], "python", "-c", script).stdout)
        probe = """
import http.client,json,time
for attempt in range(30):
 try:
  connection=http.client.HTTPConnection('127.0.0.1',8080,timeout=2)
  connection.request('GET','/healthz'); connection.getresponse().read(); connection.close(); break
 except OSError:
  if attempt==29: raise
  time.sleep(1)
statuses={}
for path in ['/healthz','/contract','/readyz','/internal/observe']:
 connection=http.client.HTTPConnection('127.0.0.1',8080,timeout=2)
 connection.request('GET',path,headers={'Authorization':'Bearer forged-foundation-token'})
 response=connection.getresponse(); body=response.read(16385)
 assert len(body)<=16384 and b'forged-foundation-token' not in body
 statuses[path]=response.status; connection.close()
print(json.dumps(statuses))
"""
        report["adapter_statuses"] = json.loads(
            docker("exec", containers["adapter"], "python", "-c", probe, timeout=45).stdout
        )
    finally:
        for container in containers.values():
            try:
                docker("rm", "--force", container, check=False)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        try:
            docker("network", "rm", owner, check=False)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        for field, args in (
            ("remaining_containers", ["ps", "-aq"]),
            ("remaining_networks", ["network", "ls", "-q"]),
        ):
            report[field] = ["unverified"]
            try:
                report[field] = docker(*args, "--filter", "label=adp.foundation=" + owner).stdout.decode().splitlines()
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        (directory / "result.json").write_text(json.dumps(report, sort_keys=True))
    if stage == "identity":
        from identity_contract import adapter_observations, observations

        require(report["test_exit"] == 0 and report["remaining_containers"] == report["remaining_networks"] == [])
        observations(report["junit"])
        require(report["adapter_test_exit"] == 0)
        adapter_observations(report["adapter_junit"])
    else:
        qualified(
            report,
            source=source,
            gateway_source=gateway_source,
            gateway_image=gateway_image,
            adapter_image=adapter_image,
            build_id=build_id,
        )
    return report


def main():
    with tempfile.TemporaryDirectory(prefix="adp-foundation-") as name:
        directory = Path(name)
        directory.chmod(0o755)
        os.environ["DOCKER_CONFIG"] = str(directory / "docker")
        path = directory / "result.json"
        failure = None
        try:
            report = execute(os.environ, directory)
        except Exception as error:
            failure = error
            if not path.is_file():
                raise
            report = json.loads(path.read_text())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        project, suffix = report["build_id"].split(":")
        key = f"codebuild/artifacts/{project}/{suffix}/result.json"
        checksum = base64.b64encode(bytes.fromhex(digest)).decode()
        stored = json.loads(
            run(
                [
                    "aws",
                    "s3api",
                    "put-object",
                    "--bucket",
                    os.environ["ADP_FOUNDATION_ARTIFACT_BUCKET"],
                    "--key",
                    key,
                    "--body",
                    str(path),
                    "--if-none-match",
                    "*",
                    "--server-side-encryption",
                    "AES256",
                    "--checksum-algorithm",
                    "SHA256",
                    "--checksum-sha256",
                    checksum,
                    "--output",
                    "json",
                ]
            ).stdout
        )
        require(stored.get("VersionId") not in (None, "", "null") and stored.get("ChecksumSHA256") == checksum)
        print(MARKER + json.dumps({"key": key, "version": stored["VersionId"], "sha256": digest}, sort_keys=True))
        if failure is not None:
            raise ValueError("foundation_qualification_failed; retained result records cleanup state") from None


if __name__ == "__main__":
    main()
