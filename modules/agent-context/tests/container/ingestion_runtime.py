"""Runs only inside the disposable, network-disabled ingestion image gate."""

import asyncio
import importlib
import json
import os
from pathlib import Path
import runpy
import shutil
import socket
import subprocess
import sys
import zipfile


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def refused_write(path):
    try:
        with open(path, "ab"):
            pass
    except (PermissionError, OSError):
        return
    raise AssertionError(f"protected path was writable: {path}")


def main():
    sys.path.insert(0, "/app")
    assert os.getuid() == 1001 and os.getgid() == 1001
    status = Path("/proc/self/status").read_text()
    for expected in ["NoNewPrivs:\t1", "Seccomp:\t2", "CapEff:\t0000000000000000"]:
        assert expected in status, expected
    assert not Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists()
    assert not any(
        os.environ.get(key)
        for key in [
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        ]
    )
    for name in [
        "sqs-worker.py",
        "refresh-repos.py",
        "scan-vulns.py",
        "personal_context/synthesis.py",
        "alembic/env.py",
    ]:
        path = Path("/app") / name
        assert path.stat().st_uid == 0
        refused_write(path)
    for tool in [
        "go",
        "scip-go",
        "scip-python",
        "scip-typescript",
        "zoekt-git-index",
        "trivy",
        "osv-scanner",
    ]:
        path = Path(shutil.which(tool)).resolve()
        assert path.stat().st_uid == 0
        refused_write(path)
    refused_write("/app/forbidden-new-file")
    refused_write("/platform-data/unrelated/private")
    for directory in [
        "/tmp",
        "/home/appuser",
        "/platform-data/repos",
        "/platform-data/code-indexes",
        "/platform-data/learning",
        "/platform-data/state",
    ]:
        (Path(directory) / "positive-write").write_text("fixture")

    # OS network isolation is authoritative. Fail any Python-level attempted
    # provider connection too, while allowing local browser pipes/Unix sockets.
    original_connect = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("provider/network connection attempted during image validation")
        return original_connect(sock, address)

    socket.socket.connect = connect
    # boto client construction is allowed; credentials/network calls are not.
    imports = {}
    for script in ["sqs-worker.py", "refresh-repos.py", "scan-vulns.py", "ingest-repo.py"]:
        imports[script] = runpy.run_path("/app/" + script, run_name="image_validation")
    importlib.import_module("personal_context.synthesis")
    # Load the real Alembic environment and generate SQL without connecting to DB.
    run(
        "alembic",
        "-c",
        "/app/alembic/alembic.ini",
        "upgrade",
        "head",
        "--sql",
        cwd="/app/alembic",
        env={
            **os.environ,
            "AC_DATABASE_URL": "postgresql://validation@invalid/validation",
            "AC_RDS_IAM_AUTH": "false",
        },
    )

    repo = Path("/platform-data/repos/ordinary")
    repo.mkdir()
    (repo / "main.py").write_text('def greeting(name: str) -> str:\n    return "hello " + name\n')
    run("git", "init", "-q", str(repo))
    run("git", "-C", str(repo), "add", ".")
    run(
        "git",
        "-C",
        str(repo),
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    lexical = imports["ingest-repo.py"]["_build_basic_code_index"](str(repo), "fixture/ordinary")
    assert lexical["language_stats"].get("python", 0) > 0
    assert any(symbol["name"] == "greeting" for symbol in lexical["symbols"])
    shards = Path("/platform-data/code-indexes/zoekt")
    shards.mkdir()
    run("zoekt-git-index", "-index", str(shards), str(repo))
    assert list(shards.glob("*.zoekt")), "no real lexical shards"
    import scip_indexer

    structural = scip_indexer.index_repo(str(repo), "fixture/ordinary", ["python"])
    assert structural.any_success, repr(structural)
    from scip_proto.scip_pb2 import Index

    index = Index()
    index.ParseFromString(Path(structural.results[0].scip_path).read_bytes())
    assert any(
        document.relative_path == "main.py" and document.occurrences for document in index.documents
    )

    # A local module proxy exercises the REAL Go cache write with network off.
    proxy = Path("/tmp/go-proxy/example.invalid/fixture/@v")
    proxy.mkdir(parents=True)
    (proxy / "v1.0.0.info").write_text('{"Version":"v1.0.0","Time":"2026-01-01T00:00:00Z"}')
    (proxy / "v1.0.0.mod").write_text("module example.invalid/fixture\ngo 1.25\n")
    with zipfile.ZipFile(proxy / "v1.0.0.zip", "w") as archive:
        archive.writestr(
            "example.invalid/fixture@v1.0.0/go.mod", "module example.invalid/fixture\ngo 1.25\n"
        )
        archive.writestr(
            "example.invalid/fixture@v1.0.0/fixture.go", "package fixture\nconst Value = 1\n"
        )
    go_repo = Path("/tmp/go-repo")
    go_repo.mkdir()
    (go_repo / "go.mod").write_text(
        "module example.invalid/main\ngo 1.25\nrequire example.invalid/fixture v1.0.0\n"
    )
    (go_repo / "main.go").write_text(
        'package main\nimport "example.invalid/fixture"\nfunc main() { println(fixture.Value) }\n'
    )
    os.environ.update(GOPROXY="file:///tmp/go-proxy", GOSUMDB="off")
    ok, message = scip_indexer._resolve_go_deps(str(go_repo))
    assert ok, message
    run(
        "go",
        "build",
        "-o",
        "/tmp/go-fixture",
        ".",
        cwd=go_repo,
        env=scip_indexer._safe_env(str(go_repo)),
    )
    assert Path(os.environ["GOMODCACHE"]).is_dir()
    assert Path(os.environ["GOCACHE"]).is_dir()

    hostile = Path("/tmp/hostile")
    hostile.mkdir()
    (hostile / "main.py").write_text('print("inert")\n')
    tool = hostile / ".scip-venv/bin"
    tool.mkdir(parents=True)
    planted = tool / "scip-python"
    planted.write_text("#!/bin/sh\ntouch /tmp/hostile-marker\n")
    planted.chmod(0o755)
    hostile_report = scip_indexer.index_repo(str(hostile), "fixture/hostile", ["python"])
    assert not hostile_report.any_success
    assert not Path("/tmp/hostile-marker").exists()

    async def browser():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            assert executable.is_file() and executable.stat().st_uid == 0
            refused_write(executable)
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content('<title>Non-root fixture</title><p id="value">rendered</p>')
            assert await page.title() == "Non-root fixture"
            assert await page.locator("#value").inner_text() == "rendered"
            await browser.close()

    asyncio.run(browser())
    print(
        json.dumps(
            {
                "uid": os.getuid(),
                "entrypoints": 5,
                "browser": "real Chromium rendered",
                "lexical": "real Zoekt shard + basic index",
                "structural": "real Python SCIP",
                "go": "local proxy/cache/build",
                "hostile_marker": "refused",
                "runtime": "readonly, cap-drop, no-new-privileges, seccomp filter, network none",
            }
        )
    )


if __name__ == "__main__":
    main()
