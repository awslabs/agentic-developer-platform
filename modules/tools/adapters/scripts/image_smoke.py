"""Build and inspect the actual isolated image; own and clean every test resource."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = Path("modules/tools/adapters")
FORBIDDEN = {
    "sqlalchemy",
    "alembic",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "psycopg2-binary",
    "adp-gateway",
    "bedrock-gateway",
}


def run(args, *, timeout=30, check=True):
    value = subprocess.run(args, capture_output=True, timeout=timeout)
    if check and value.returncode != 0:
        # Never include runtime stdout/stderr or invocation arguments in errors.
        raise RuntimeError("adapter_image_check_failed")
    return value


def docker(*args, timeout=30, check=True):
    return run(["docker", *args], timeout=timeout, check=check)


def request(container, path, *, sentinel="", method="GET"):
    script = (
        "import http.client,json,sys; "
        "c=http.client.HTTPConnection('127.0.0.1',8080,timeout=3); "
        "c.request(sys.argv[1],sys.argv[2],body=sys.argv[3],headers="
        "{'Authorization':'Bearer '+sys.argv[3],'x-caller-identity':'forged','x-adp-edge-provenance':'forged'}); "
        "r=c.getresponse(); b=r.read(16385); assert len(b)<=16384; "
        "print(json.dumps({'status':r.status,'body':b.decode()}))"
    )
    return json.loads(docker("exec", container, "python", "-c", script, method, path, sentinel, timeout=8).stdout)


def scan_export(container, sentinel):
    found = False
    with tempfile.TemporaryFile() as exported:
        subprocess.run(
            ["docker", "export", container], stdout=exported, stderr=subprocess.DEVNULL, timeout=120, check=True
        )
        exported.seek(0)
        deadline = time.monotonic() + 120
        with tarfile.open(fileobj=exported, mode="r|") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                tail = b""
                while chunk := stream.read(131072):
                    if time.monotonic() > deadline:
                        raise RuntimeError("adapter_image_scan_deadline")
                    data = tail + chunk
                    if sentinel in data:
                        found = True
                    tail = data[-len(sentinel) :]
    if found:
        raise RuntimeError("adapter_image_contains_forbidden_fixture")


def verify(*, source_revision: str, evidence_dir: Path) -> dict:
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ValueError("Immutable source revision required")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex
    tag = "adp-tool-adapters:smoke-" + suffix
    container = "adp-adapter-smoke-" + suffix
    sentinel = "adp-forbidden-image-fixture-" + suffix
    report = {
        "schema_version": "1.0",
        "scenario": "SCN-04",
        "source_revision": source_revision,
        "passed": False,
        "cleanup_confirmed": False,
    }
    cleanup_ok = True
    phase = "source_context"
    try:
        with tempfile.TemporaryDirectory(prefix="adp-adapter-image-") as temporary:
            temporary = Path(temporary)
            context = temporary / "context"
            paths = [
                Path("modules/tools/pyproject.toml"),
                PACKAGE / "Dockerfile",
                PACKAGE / "Dockerfile.dockerignore",
                PACKAGE / "pyproject.toml",
                PACKAGE / "uv.lock",
            ]
            paths += [
                path.relative_to(ROOT)
                for folder in (ROOT / "modules/tools/adp_tools", ROOT / PACKAGE / "tool_adapters")
                for path in sorted(folder.glob("*.py"))
            ]
            hashes = {}
            for path in paths:
                destination = context / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / path, destination)
                hashes[str(path)] = hashlib.sha256(destination.read_bytes()).hexdigest()
            # If a future Dockerfile broadens COPY or defeats its allowlist,
            # these recognizable app-secret/core fixtures must make the check fail.
            for relative in (
                "modules/gateway/src/private_fixture.py",
                "modules/tools/adapters/.env",
                "modules/tools/app-secret.json",
            ):
                path = context / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(sentinel)
            (evidence_dir / "source-files.json").write_text(json.dumps(hashes, sort_keys=True, indent=2) + "\n")
            report["source_files_sha256"] = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
            phase = "image_build"
            build = docker(
                "build",
                "--pull",
                "--file",
                str(context / PACKAGE / "Dockerfile"),
                "--build-arg",
                "ADP_SOURCE_REVISION=" + source_revision,
                "--tag",
                tag,
                str(context),
                timeout=900,
                check=False,
            )
            build_log = evidence_dir / "image-build.log"
            build_log.write_bytes(build.stdout + build.stderr)
            build_log.chmod(0o600)
            if build.returncode:
                raise RuntimeError("adapter_image_build_failed")
            phase = "image_inspection"
            info = json.loads(docker("image", "inspect", tag).stdout)[0]
            assert info["Config"]["User"] == "65532:65532"
            assert info["Config"]["Labels"]["org.opencontainers.image.revision"] == source_revision
            report.update(
                image_id=info["Id"],
                docker_reported_image_size_bytes=info["Size"],
                image_size_metric="docker.image.inspect.Size",
            )
            configuration = temporary / "config.json"
            configuration.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "mode": "foundation",
                        "release": {
                            "contract_version": "1.0",
                            "source_revision": source_revision,
                            "image_digest": info["Id"],
                            "group": "tool-connectors",
                            "group_generation": 1,
                            "backend_generation": 1,
                        },
                    }
                )
            )
            configuration.chmod(0o644)
            started = time.monotonic()
            phase = "container_startup"
            docker(
                "run",
                "--detach",
                "--name",
                container,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "64",
                "--cpus",
                "1",
                "--memory",
                "512m",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                "--env",
                "AWS_EC2_METADATA_DISABLED=true",
                "--env",
                "ADP_TOOL_ADAPTER_CONFIG=/etc/adp-adapters/config.json",
                "--mount",
                f"type=bind,src={configuration},dst=/etc/adp-adapters/config.json,readonly",
                tag,
            )
            while True:
                try:
                    health = request(container, "/healthz")
                    if health["status"] == 200:
                        break
                except RuntimeError:
                    pass
                if time.monotonic() - started > 30:
                    raise RuntimeError("adapter_image_startup_deadline")
                time.sleep(0.2)
            report["startup_seconds"] = round(time.monotonic() - started, 3)
            phase = "runtime_probes"
            assert request(container, "/readyz")["status"] == 200
            metadata = json.loads(request(container, "/contract")["body"])
            assert set(metadata) == {"service", "contract_version", "schema_sha256"}
            report["contract"] = metadata
            probes = []
            for path in [f"/internal/v1/tools/{name}/mcp" for name in ("atlassian", "github", "slack", "jira")] + [
                "/internal/v1/tool-adapters/observe",
                "/internal/v1/tool-adapters/authorize",
            ]:
                response = request(container, path, method="POST", sentinel=sentinel)
                assert response["status"] == 403 and sentinel not in response["body"]
                probes.append({"path": path, "status": response["status"]})
            report["unauthenticated_probes"] = probes
            phase = "dependency_inventory"
            inventory_script = (
                "import importlib.metadata as m,importlib.util,json,os; "
                "assert importlib.util.find_spec('src') is None; "
                "assert importlib.util.find_spec('sqlalchemy') is None; "
                "import tool_adapters.server,adp_tools.adapter_contracts; "
                "print(json.dumps({'uid':os.getuid(),'packages':sorted("
                "[{'name':d.metadata['Name'],'version':d.version} for d in m.distributions()],"
                "key=lambda d:d['name'])}))"
            )
            inventory = json.loads(docker("exec", container, "python", "-c", inventory_script).stdout)
            assert inventory["uid"] == 65532
            names = {item["name"].lower().replace("_", "-") for item in inventory["packages"]}
            assert not names & FORBIDDEN
            assert {"adp-tool-adapters", "adp-tools-shared"} <= names
            os_packages = (
                docker("exec", container, "dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\n")
                .stdout.decode()
                .splitlines()
            )
            os_components = []
            for row in os_packages:
                name, version, architecture = row.split("\t")
                os_components.append(
                    {
                        "type": "library",
                        "name": name,
                        "version": version,
                        "properties": [{"name": "debian:architecture", "value": architecture}],
                    }
                )
            sbom = {
                "bomFormat": "CycloneDX",
                "specVersion": "1.5",
                "version": 1,
                "serialNumber": "urn:uuid:" + str(uuid.uuid4()),
                "components": [
                    {"type": "library", **item, "purl": f"pkg:pypi/{item['name']}@{item['version']}"}
                    for item in inventory["packages"]
                ]
                + os_components,
            }
            (evidence_dir / "sbom.cdx.json").write_text(json.dumps(sbom, indent=2) + "\n")
            report["sbom_scope"] = ["python_distributions", "debian_packages"]
            phase = "secret_fixture_scan"
            scan_export(container, sentinel.encode())
            logs = docker("logs", container)
            assert sentinel.encode() not in logs.stdout + logs.stderr
            report.update(
                network_mode="none",
                read_only_root=True,
                nonroot_uid=inventory["uid"],
                gateway_and_sql_absent=True,
                forbidden_fixture_absent=True,
                passed=True,
            )
    except Exception as error:
        report["failure_type"] = type(error).__name__
        report["failure_phase"] = phase
    finally:
        for kind, owned_name, removal in (
            ("container", container, ("rm", "--force", container)),
            ("image", tag, ("image", "rm", tag)),
        ):
            try:
                exists = docker(kind, "inspect", owned_name, check=False)
                if exists.returncode == 0:
                    cleanup_ok = docker(*removal, check=False).returncode == 0 and cleanup_ok
                # A successful daemon query, followed by absence of the owned
                # resource, is required; daemon failure is not clean teardown.
                docker("info", "--format", "{{.ServerVersion}}")
                cleanup_ok = docker(kind, "inspect", owned_name, check=False).returncode != 0 and cleanup_ok
            except Exception:
                cleanup_ok = False
        report["cleanup_confirmed"] = cleanup_ok
        report["passed"] = report["passed"] and cleanup_ok
        (evidence_dir / "image-smoke.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    report = verify(source_revision=args.source_revision, evidence_dir=args.evidence_dir)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
