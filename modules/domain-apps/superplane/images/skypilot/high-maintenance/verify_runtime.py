"""Exercise maintained provider tools and Python packages with local fixtures."""

import hashlib
import http.server
import importlib.metadata as metadata
import json
import os
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

import git
import urllib3


def run(*args, env=None):
    return subprocess.check_output(
        args, text=True, stderr=subprocess.STDOUT, env=env, timeout=60
    ).strip()


def crc32c(data):
    value = 0xFFFFFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
    return value ^ 0xFFFFFFFF


def main():
    if not __debug__:
        raise RuntimeError("Acceptance requires assertions")
    root = Path(__file__).resolve().parent
    go = json.loads((root / "go-lock.json").read_text())
    uv = json.loads((root / "uv-lock.json").read_text())
    for w in json.loads((root / "wheel-lock.json").read_text()):
        assert metadata.version(w["name"]) == w["version"]
    for name, path in go["installed_paths"].items():
        assert (
            hashlib.sha256(Path(path).read_bytes()).hexdigest()
            == go["binary_sha256"][name]
        )
    for name, digest in uv["binary_sha256"].items():
        assert (
            hashlib.sha256((Path("/usr/local/bin") / name).read_bytes()).hexdigest()
            == digest
        )
    with tempfile.TemporaryDirectory(prefix="high-tools-") as tmp:
        home = Path(tmp)
        env = {
            **os.environ,
            "HOME": tmp,
            "UV_CACHE_DIR": str(home / "uv-cache"),
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_PYTHON": sys.executable,
            "CLOUDSDK_CONFIG": str(home / "gcloud-config"),
        }
        crc = go["installed_paths"]["gcloud-crc32c"]
        for data in [b"", b"123456789", bytes(range(256)) * 4]:
            f = home / "data"
            f.write_bytes(data)
            assert int(run(crc, str(f))) == crc32c(data)
        assert int(run(crc, "-o", "3", "-l", "17", str(f))) == crc32c(data[3:20])
        assert (
            run(go["installed_paths"]["session-manager-plugin"], "--version")
            == "1.2.814.0"
        )

        fake = home / "bin"
        fake.mkdir()
        gcloud = fake / "gcloud"
        payload = {
            "credential": {
                "access_token": "synthetic-plugin-token",
                "token_expiry": "2099-01-01T00:00:00Z",
            }
        }
        gcloud.write_text(
            "#!/usr/bin/env python3\nprint(" + repr(json.dumps(payload)) + ")\n"
        )
        gcloud.chmod(0o755)
        credential = json.loads(
            run(
                go["installed_paths"]["gke-gcloud-auth-plugin"],
                env={**env, "PATH": str(fake) + os.pathsep + env["PATH"]},
            )
        )
        assert credential["kind"] == "ExecCredential"
        assert credential["status"]["token"] == "synthetic-plugin-token"

        # Real environment creation and installation, with no registry/network.
        venv = home / "virtualenv"
        run(
            sys.executable,
            "-m",
            "virtualenv",
            "--no-seed",
            "--python",
            sys.executable,
            str(venv),
            env=env,
        )
        assert run(str(venv / "bin/python"), "-c", "print(6*7)") == "42"
        uv_env = home / "uv-env"
        run("uv", "venv", "--offline", "--python", sys.executable, str(uv_env), env=env)
        wheel = home / "adp_local_fixture-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as z:
            z.writestr("adp_local_fixture.py", "def main():\n    print(42)\n")
            z.writestr(
                "adp_local_fixture-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: adp-local-fixture\nVersion: 1.0\n",
            )
            z.writestr(
                "adp_local_fixture-1.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            z.writestr(
                "adp_local_fixture-1.0.dist-info/entry_points.txt",
                "[console_scripts]\nadp-local-fixture = adp_local_fixture:main\n",
            )
            z.writestr("adp_local_fixture-1.0.dist-info/RECORD", "")
        run(
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-index",
            "--python",
            str(uv_env / "bin/python"),
            str(wheel),
            env=env,
        )
        assert run(str(uv_env / "bin/adp-local-fixture"), env=env) == "42"
        assert (
            run(
                "uvx", "--offline", "--from", str(wheel), "adp-local-fixture", env=env
            ).splitlines()[-1]
            == "42"
        )

        repo = git.Repo.init(home / "repo with spaces")
        (Path(repo.working_tree_dir) / "README").write_text("fixture\n")
        repo.index.add(["README"])
        repo.index.commit(
            "fixture",
            author=git.Actor("Fixture", "fixture@example.invalid"),
            committer=git.Actor("Fixture", "fixture@example.invalid"),
        )
        clone = git.Repo.clone_from(repo.working_tree_dir, home / "clone")
        assert clone.head.commit.hexsha == repo.head.commit.hexsha

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"fixture")

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib3.PoolManager() as pool:
                r = pool.request(
                    "GET", f"http://127.0.0.1:{server.server_port}/", timeout=2
                )
                assert r.status == 200 and r.data == b"fixture"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    print(
        json.dumps(
            {
                "uid": os.getuid(),
                "crc32c": "passed",
                "gke_credential_contract": "passed with synthetic local gcloud",
                "ssm_version": "passed",
                "virtualenv": "passed",
                "uv_uvx_offline_install_execute": "passed",
                "gitpython_clone": "passed",
                "urllib3_http": "passed",
                "cloud_provisioning": "not exercised",
            }
        )
    )


if __name__ == "__main__":
    main()
