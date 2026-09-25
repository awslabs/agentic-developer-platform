"""Offline S15 acceptance against reviewed ingestion source and real image tools.

Run in the ingestion image with latest module mounted read-only at /reviewed,
network disabled, UID1001, read-only root and private /tmp. Only cloud/source
transport, ACL registration, backend availability and optional telemetry are
replaced. Git, Zoekt, code analysis, SCIP selection, scope routing, output
serialization and scratch cleanup are real.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


def run() -> dict:
    module_root = Path(os.environ.get("REVIEWED_MODULE", "/reviewed"))
    ingestion = module_root / "images" / "ingestion"
    with tempfile.TemporaryDirectory(prefix="s15-lexical-") as temporary:
        root = Path(temporary)
        os.environ.update(
            AWS_EC2_METADATA_DISABLED="true",
            OTEL_SDK_DISABLED="true",
            OTEL_TRACES_EXPORTER="none",
            OTEL_METRICS_EXPORTER="none",
            OTEL_LOGS_EXPORTER="none",
            SCRATCH_BASE=str(root),
            STATE_DIR=str(root / "state"),
            CODE_INDEX_DIR=str(root / "state" / "code-indexes"),
            EMBED_VECTORS_ENABLED="false",
            SBOM_ENABLED="false",
            GRAPHRAG_ENABLED="false",
            INGESTION_SCOPE_VISIBILITY="tenant",
            INGESTION_SCOPE_TENANT_ID="fixture-tenant",
            INGESTION_SCOPE_OWNER_SUB="",
            SCIP_ENABLED="true",
        )
        assert os.getuid() == 1001, "acceptance requires the reviewed non-root identity"
        assert not any(
            os.environ.get(k)
            for k in (
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "GITHUB_TOKEN",
                "GH_TOKEN",
                "AWS_WEB_IDENTITY_TOKEN_FILE",
            )
        )
        network_attempts = {"dns": 0, "tcp": 0}

        def no_dns(*args, **kwargs):
            network_attempts["dns"] += 1
            raise AssertionError("unexpected DNS in offline lexical fixture")

        def no_connect(*args, **kwargs):
            network_attempts["tcp"] += 1
            raise AssertionError("unexpected TCP in offline lexical fixture")

        socket.getaddrinfo = no_dns
        socket.socket.connect = no_connect
        socket.socket.connect_ex = no_connect
        sys.path[:0] = [str(ingestion), str(module_root)]
        spec = importlib.util.spec_from_file_location(
            "s15_lexical_ingest", ingestion / "ingest-repo.py"
        )
        ingest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ingest)
        import db
        import isolated_runner

        fixture = root / "fixture"
        fixture.mkdir()
        marker = root / "REPOSITORY_CODE_EXECUTED"
        (fixture / "example.py").write_text(
            "def lexical_acceptance_marker(value):\n    return value + 1\n"
        )
        (fixture / "setup.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        )
        (fixture / "gradlew").write_text(f"#!/bin/sh\ntouch {marker}\n")
        (fixture / "package.json").write_text(
            json.dumps(
                {
                    "name": "inert-lexical-fixture",
                    "version": "1.0.0",
                    "scripts": {"postinstall": f"touch {marker}"},
                }
            )
        )
        subprocess.run(["git", "init", "-q", str(fixture)], check=True)
        subprocess.run(["git", "-C", str(fixture), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(fixture),
                "-c",
                "user.name=Offline Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "inert fixture",
            ],
            check=True,
        )

        records = []
        clones = []
        stores = []
        registrations = []

        class Connection:
            closed = False

            def close(self):
                self.closed = True

        class LocalObjects:
            def __init__(self):
                self.objects = {}

            def upload_file(self, *, Filename, Bucket, Key, ExtraArgs):
                self.objects[Key] = Path(Filename).read_bytes()

            def put_object(self, *, Bucket, Key, Body, **kwargs):
                self.objects[Key] = Body

            def head_object(self, *, Bucket, Key):
                return {"ContentLength": len(self.objects[Key])}

        class LocalStore:
            def __init__(self, *, bucket_name, prefix, region_name):
                self.bucket_name = "offline-fixture"
                self.prefix = prefix
                self._s3 = LocalObjects()
                stores.append(self)

        def register(conn, repo, url, **scope):
            assert scope == {
                "allowed_principals": ["fixture-reader"],
                "public_verified": False,
                "tenant_id": "fixture-tenant",
                "owner_sub": None,
            }
            registrations.append((repo, scope))
            return "fixture-repository-id"

        def local_clone(url, destination):
            assert registrations, "ACL ownership must be registered before retrieval"
            assert url == "https://github.com/fixture/lexical"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--no-hardlinks",
                    str(fixture),
                    destination,
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    destination,
                    "config",
                    "remote.origin.url",
                    url,
                ],
                check=True,
            )
            clones.append(destination)
            return True

        def no_legacy(*args, **kwargs):
            raise AssertionError("credential-bearing structural fallback was reached")

        def no_parser(*args, **kwargs):
            raise AssertionError("ungranted parser execution was reached")

        def no_tracker(*args, **kwargs):
            raise RuntimeError("optional stage telemetry excluded from offline fixture")

        # Explicit external adapters; the no-grant authorizer is not replaced.
        ingest.S3ContentStore = LocalStore
        ingest.git_clone = local_clone
        ingest.resolve_allowed_principals = lambda repo: ["fixture-reader"]
        ingest.StageTracker = no_tracker
        ingest.scip_structural_ingest = no_legacy
        db.get_connection = Connection
        db.ensure_repo_exists = register
        isolated_runner.DockerBackend.run = no_parser
        isolated_runner.DockerBackend.is_available = lambda self: True
        authority_calls = []
        real_issue_fetch = isolated_runner.ProductionAuthorizer.issue_fetch

        def production_fetch(self, asset_id, attempt_id, **kwargs):
            authority_calls.append((asset_id, attempt_id))
            return real_issue_fetch(self, asset_id, attempt_id, **kwargs)

        isolated_runner.ProductionAuthorizer.issue_fetch = production_fetch

        for backend in ("", "docker"):
            ingest.SCIP_ISOLATED_BACKEND = backend
            result = ingest.ingest_repo("fixture/lexical", skip_deepwiki=True)
            assert result["clone"] == "ok", result
            assert result["zoekt_index"] == "complete", result
            assert result["code_index"] == "written", result
            assert result["s3_upload"] == "ok", result
            assert result["scip_structural"] == "structural_stage_unavailable", result
            assert not marker.exists(), "repository-authored script executed"
            assert not Path(clones[-1]).exists(), "fresh source survived ingestion return"
            index_path = root / "state/tenants/fixture-tenant/code-indexes/fixture-lexical.json"
            index = json.loads(index_path.read_text())
            assert any(
                symbol["name"] == "lexical_acceptance_marker" for symbol in index["symbols"]
            ), index
            objects = stores[-1]._s3.objects
            shards = {k: v for k, v in objects.items() if k.endswith(".zoekt")}
            assert shards and all(len(v) > 0 for v in shards.values())
            assert any(
                b"lexical_acceptance_marker" in value
                for key, value in objects.items()
                if not key.endswith(".zoekt")
            )
            records.append(
                {
                    "backend": backend or "not-configured",
                    "structural": result["scip_structural"],
                    "lexical_shards": len(shards),
                    "lexical_bytes": sum(map(len, shards.values())),
                    "code_index_symbols": len(index["symbols"]),
                    "positive_symbol": "lexical_acceptance_marker",
                    "repository_marker_absent": True,
                    "scratch_removed": True,
                }
            )
        assert len(authority_calls) == 1 and authority_calls[0][0] == "fixture/lexical"
        assert len(clones) == len(set(clones)) == 2
        assert network_attempts == {"dns": 0, "tcp": 0}, network_attempts
        return {
            "result": "pass",
            "scope": "latest-source-local-image-lexical-acceptance",
            "source_sha256": hashlib.sha256(
                (ingestion / "ingest-repo.py").read_bytes()
            ).hexdigest(),
            "uid": os.getuid(),
            "production_authority_denials": len(authority_calls),
            "cases": records,
            "network_attempts": network_attempts,
            "external_adapters": [
                "local Git source transport",
                "local object store",
                "ACL registration fixture",
                "optional telemetry",
                "backend availability probe",
            ],
            "limitations": [
                "not live AWS",
                "not deployed acceptance",
                "ACL SQL tested separately",
                "no canonical parser grants",
            ],
        }


if __name__ == "__main__":
    print("S15_LEXICAL_ACCEPTANCE=" + json.dumps(run(), sort_keys=True))
