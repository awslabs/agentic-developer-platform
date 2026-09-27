"""Offline TLS and installed Git/libcurl compatibility, with fresh synthetic keys."""

import http.server
import json
import os
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path


def run(*args, expected=0):
    result = subprocess.run(
        args, text=True, capture_output=True, check=False, timeout=30
    )
    assert result.returncode == expected, (args[0], result.returncode, result.stderr)
    return result.stdout


assert os.getuid() != 0
with tempfile.TemporaryDirectory(prefix="curl-git-", dir="/tmp") as temp:
    root = Path(temp)
    run(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(root / "key.pem"),
        "-out",
        str(root / "cert.pem"),
        "-days",
        "1",
        "-subj",
        "/CN=localhost",
        "-addext",
        "subjectAltName=DNS:localhost,IP:127.0.0.1",
    )
    run("git", "init", "-q", "-b", "main", str(root / "seed"))
    (root / "seed/README").write_text("verified transport fixture\n")
    run("git", "-C", str(root / "seed"), "add", "README")
    run(
        "git",
        "-C",
        str(root / "seed"),
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    run("git", "clone", "-q", "--bare", str(root / "seed"), str(root / "repo.git"))
    run("git", "-C", str(root / "repo.git"), "update-server-info")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "cert.pem", root / "key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"https://localhost:{server.server_port}"
    try:
        run(
            "curl",
            "--noproxy",
            "*",
            "--fail",
            "--silent",
            "--show-error",
            "--cacert",
            str(root / "cert.pem"),
            base + "/repo.git/HEAD",
        )
        run(
            "curl",
            "--noproxy",
            "*",
            "--fail",
            "--silent",
            "--show-error",
            base + "/repo.git/HEAD",
            expected=60,
        )
        run(
            "curl",
            "--noproxy",
            "*",
            "--cacert",
            str(root / "cert.pem"),
            "--connect-to",
            f"wrong.invalid:{server.server_port}:127.0.0.1:{server.server_port}",
            f"https://wrong.invalid:{server.server_port}/",
            expected=60,
        )
        run(
            "git",
            "-c",
            "http.sslCAInfo=" + str(root / "cert.pem"),
            "clone",
            "-q",
            base + "/repo.git",
            str(root / "clone"),
        )
        assert (root / "clone/README").read_text() == "verified transport fixture\n"
        run(
            "git",
            "-C",
            str(root / "clone"),
            "-c",
            "http.sslCAInfo=" + str(root / "cert.pem"),
            "fetch",
            "-q",
        )
        bad = subprocess.run(
            ["git", "clone", "-q", base + "/repo.git", str(root / "untrusted")],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert bad.returncode != 0 and "certificate" in bad.stderr.lower(), bad.stderr
        print(
            json.dumps(
                {
                    "uid": os.getuid(),
                    "curl_verified_tls": "passed",
                    "untrusted_cert_refused": "passed",
                    "wrong_host_refused": "passed",
                    "git_https_clone_fetch": "passed",
                    "git_untrusted_cert_refused": "passed",
                    "network": "isolated loopback",
                    "credentials": "fresh synthetic TLS key only",
                }
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
