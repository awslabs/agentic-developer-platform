#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
python3 - "$REPO_ROOT" <<'PY'
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root = pathlib.Path(sys.argv[1])
cli = root / "modules/gateway/cli"
skill = b"---\nname: adp-flow\n---\n# Flow\n"
manifest_source = {"schema_version": 1, "skills": [
    {"name": "adp-flow", "clients": ["claude-code", "codex"], "invocation": "adp flow"}
]}
manifest = {**manifest_source, "version": "1.0.0"}
archives = {}
with tempfile.TemporaryDirectory() as build_directory:
    build_path = pathlib.Path(build_directory)
    source = build_path / "source"
    (source / "adp-flow").mkdir(parents=True)
    (source / "MANIFEST.json").write_text(json.dumps(manifest_source))
    (source / "adp-flow/SKILL.md").write_bytes(skill)
    output = build_path / "output"
    for version in ("1.0.0", "2.0.0"):
        versioned_adp = build_path / "adp"
        versioned_adp.write_text(f'readonly ADP_VERSION="{version}"\n')
        subprocess.run([
            "bash", str(root / "modules/gateway/scripts/build-skills-archive.sh"),
            "--build", str(source), str(output), str(versioned_adp)
        ], check=True, capture_output=True, text=True, timeout=20)
        archive_bytes = (output / "adp-skills.tar.gz").read_bytes()
        digest = hashlib.sha256(archive_bytes).hexdigest()
        assert (output / "adp-skills.tar.gz.sha256").read_text() == f"{digest}  adp-skills.tar.gz\n"
        archives[version] = (archive_bytes, digest)
archive_bytes, digest = archives["1.0.0"]

class Handler(BaseHTTPRequestHandler):
    requested = []
    served_archive = archive_bytes
    served_digest = digest
    served_version = "1.0.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.requested.append(self.path)
        name = self.path.rsplit("/", 1)[-1]
        if name.startswith("adp-skills.tar.gz") and self.served_archive is None:
            self.send_error(404)
            return
        if name == "adp-skills.tar.gz":
            body = self.served_archive
        elif name == "adp-skills.tar.gz.sha256":
            body = (self.served_digest + "  adp-skills.tar.gz\n").encode()
        elif (cli / name).is_file():
            body = (cli / name).read_bytes()
            if name == "adp":
                body = re.sub(rb'^readonly ADP_VERSION="[^"]+"',
                              f'readonly ADP_VERSION="{self.served_version}"'.encode(),
                              body, flags=re.MULTILINE)
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

with tempfile.TemporaryDirectory() as directory:
    temporary = pathlib.Path(directory)
    isolated = temporary / "install.sh"
    shutil.copyfile(cli / "install.sh", isolated)
    home = temporary / "home"
    home.mkdir()
    prefix = temporary / "prefix"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        command = ["sh", str(isolated), "--gateway-url", url, "--no-path-edit"]
        env = {**os.environ, "HOME": str(home), "SHELL": "/bin/sh"}
        result = subprocess.run(
            [*command, "--prefix", str(prefix)], env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert (prefix / "adp").is_file()
        assert (prefix / "skills/adp-flow/SKILL.md").read_bytes() == skill
        assert json.loads((prefix / "skills/MANIFEST.json").read_text()) == manifest
        installed = json.loads((prefix / ".adp-manifest.json").read_text())
        assert installed["skills"] == {"version": "1.0.0", "sha256": digest}, installed
        assert "/api/cli/adp-skills.tar.gz" in Handler.requested, Handler.requested
        assert "/api/cli/adp-skills.tar.gz.sha256" in Handler.requested, Handler.requested
        print("PASS: full local-server installer places verified skills and records version/digest")

        update = subprocess.run(["bash", str(prefix / "adp"), "update"], env=env,
                                capture_output=True, text=True, timeout=60)
        assert update.returncode == 0, update.stderr
        assert (prefix / "skills.prev/adp-flow/SKILL.md").read_bytes() == skill
        assert json.loads((prefix / ".adp-manifest.json").read_text())["skills"]["sha256"] == digest
        rollback = subprocess.run(["bash", str(prefix / "adp"), "update", "--rollback"],
                                  env=env, capture_output=True, text=True, timeout=60)
        assert rollback.returncode == 0, rollback.stderr
        assert (prefix / "skills/adp-flow/SKILL.md").read_bytes() == skill
        assert json.loads((prefix / ".adp-manifest.json").read_text())["skills"]["sha256"] == digest
        print("PASS: update and rollback retain the corresponding skills archive")

        Handler.served_archive = archive_bytes + b"corrupted"
        corrupt_prefix = temporary / "corrupt-prefix"
        fresh_result = subprocess.run(
            [*command, "--prefix", str(corrupt_prefix)], env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert fresh_result.returncode != 0, fresh_result.stderr
        assert "checksum mismatch" in fresh_result.stderr, fresh_result.stderr
        assert not list(corrupt_prefix.iterdir()), list(corrupt_prefix.iterdir())
        assert not (corrupt_prefix / "skills").exists()

        before = {entry.name: entry.read_bytes() for entry in prefix.iterdir() if entry.is_file()}
        existing_result = subprocess.run(
            [*command, "--prefix", str(prefix)], env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert existing_result.returncode != 0, existing_result.stderr
        assert "checksum mismatch" in existing_result.stderr, existing_result.stderr
        after = {entry.name: entry.read_bytes() for entry in prefix.iterdir() if entry.is_file()}
        assert after == before, "checksum failure modified the existing CLI or its manifest"
        assert (prefix / "skills/adp-flow/SKILL.md").read_bytes() == skill
        assert not list(temporary.glob("prefix/skills.tmp.*"))
        assert not list(prefix.glob("*.tmp.*")), (list(prefix.glob("*.tmp.*")), existing_result.stderr)
        print("PASS: corrupted archive refuses fresh install and preserves an existing installation")

        def serve(version, with_skills=True):
            Handler.served_version = version
            Handler.served_archive, Handler.served_digest = archives[version] if with_skills else (None, None)

        def run(arguments):
            result = subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr
            return result

        def assert_version(target, version):
            result = run(["bash", str(target / "adp"), "version"])
            assert version in result.stdout, result.stdout
            assert json.loads((target / ".adp-manifest.json").read_text())["version"] == version

        for layout in ("custom-prefix", "default-layout/bin"):
            target = temporary / layout
            skills_root = target.parent / "skills" if target.name == "bin" else target / "skills"
            serve("0.9.0", with_skills=False)
            run([*command, "--prefix", str(target)])
            assert not skills_root.exists()
            serve("1.0.0")
            run(["bash", str(target / "adp"), "update", "--to", "1.0.0"])
            assert (skills_root / "adp-flow/SKILL.md").read_bytes() == skill
            run(["bash", str(target / "adp"), "update", "--rollback", "--to", "0.9.0"])
            assert_version(target, "0.9.0")
            assert not skills_root.exists(), "rollback retained skills absent from the outgoing generation"
            assert "skills" not in json.loads((target / ".adp-manifest.json").read_text())
            print(f"PASS: {layout} rollback restores the absence of skills")

            serve("1.0.0")
            run(["bash", str(target / "adp"), "update"])
            serve("2.0.0")
            run(["bash", str(target / "adp"), "update"])
            serve("3.0.0", with_skills=False)
            run(["bash", str(target / "adp"), "update"])
            run(["bash", str(target / "adp"), "update", "--rollback", "--to", "2.0.0"])
            assert_version(target, "2.0.0")
            assert json.loads((skills_root / "MANIFEST.json").read_text())["version"] == "2.0.0"
            assert json.loads((target / ".adp-manifest.json").read_text())["skills"]["sha256"] == archives["2.0.0"][1]
            assert not skills_root.with_name("skills.prev").exists()
            assert not skills_root.with_name("skills.prev.absent").exists()
            print(f"PASS: {layout} archive-less update rotates the immediately previous skills generation")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
PY
