"""Check certifi and uv TLS rejection/acceptance against a loopback fixture."""

import http.client
import http.server
import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

import certifi
import pip
from pip._vendor import certifi as pip_certifi


def main():
    if not __debug__:
        raise RuntimeError("Acceptance requires assertions")
    context = ssl.create_default_context(cafile=certifi.where())
    names = {
        value
        for cert in context.get_ca_certs()
        for rdn in cert["subject"]
        for key, value in rdn
        if key == "commonName"
    }
    assert "emSign ECC Root CA - G3" not in names and len(names) > 100
    pip_context = ssl.create_default_context(cafile=pip_certifi.where())
    pip_names = {
        value
        for cert in pip_context.get_ca_certs()
        for rdn in cert["subject"]
        for key, value in rdn
        if key == "commonName"
    }
    assert "emSign ECC Root CA - G3" not in pip_names and len(pip_names) > 100
    system_context = ssl.create_default_context()
    system_names = {
        value
        for cert in system_context.get_ca_certs()
        for rdn in cert["subject"]
        for key, value in rdn
        if key == "commonName"
    }
    assert "emSign ECC Root CA - G3" not in system_names and len(system_names) > 100
    with tempfile.TemporaryDirectory(prefix="critical tls ") as tmp:
        root = Path(tmp)
        cert, key = root / "cert.pem", root / "key.pem"
        ca, ca_key = root / "ca.pem", root / "ca-key.pem"
        csr, extensions = root / "server.csr", root / "server.ext"
        extensions.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:localhost\n"
        )
        commands = [
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-noenc",
                "-days",
                "1",
                "-subj",
                "/CN=ADP TLS fixture CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-keyout",
                str(ca_key),
                "-out",
                str(ca),
            ],
            [
                "openssl",
                "req",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-noenc",
                "-subj",
                "/CN=localhost",
                "-keyout",
                str(key),
                "-out",
                str(csr),
            ],
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(ca),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-days",
                "1",
                "-extfile",
                str(extensions),
                "-out",
                str(cert),
            ],
        ]
        for command in commands:
            subprocess.run(command, check=True, capture_output=True)
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("adp_tls_fixture.py", "value = 42\n")
            archive.writestr(
                "adp_tls_fixture-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: adp-tls-fixture\nVersion: 1.0\n",
            )
            archive.writestr(
                "adp_tls_fixture-1.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            archive.writestr("adp_tls_fixture-1.0.dist-info/RECORD", "")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = (
                    wheel.getvalue()
                    if self.path.endswith(".whl")
                    else b'<a href="/adp_tls_fixture-1.0-py3-none-any.whl">fixture</a>'
                )
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "application/octet-stream"
                    if self.path.endswith(".whl")
                    else "text/html",
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for trust in (context, system_context):
                connection = http.client.HTTPSConnection(
                    "localhost", server.server_port, context=trust, timeout=5
                )
                try:
                    connection.request("GET", "/")
                except ssl.SSLCertVerificationError:
                    pass
                else:
                    raise AssertionError(
                        "TLS context accepted an untrusted certificate"
                    )
                finally:
                    connection.close()
                trust.load_verify_locations(cafile=str(ca))
                connection = http.client.HTTPSConnection(
                    "localhost", server.server_port, context=trust, timeout=5
                )
                connection.request("GET", "/")
                assert connection.getresponse().status == 200
                connection.close()
            pip_env = root / "pip-venv"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(pip_env)],
                check=True,
            )
            pip_command = [
                sys.executable,
                "-m",
                "pip",
                "--isolated",
                "--python",
                str(pip_env / "bin/python"),
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-deps",
                "--retries",
                "0",
                "--timeout",
                "5",
                "--index-url",
                f"https://localhost:{server.server_port}/simple/",
                "adp-tls-fixture",
            ]
            rejected = subprocess.run(
                pip_command, check=False, capture_output=True, text=True, timeout=30
            )
            assert (
                rejected.returncode != 0
                and "certificate" in (rejected.stdout + rejected.stderr).lower()
            ), rejected.stdout + rejected.stderr
            accepted = subprocess.run(
                pip_command + ["--cert", str(ca)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert accepted.returncode == 0, accepted.stdout + accepted.stderr
            subprocess.run(
                [
                    str(pip_env / "bin/python"),
                    "-c",
                    "import adp_tls_fixture; assert adp_tls_fixture.value == 42",
                ],
                check=True,
            )
            uv_result = "not installed"
            if shutil.which("uv"):
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if not k.startswith(("UV_", "SSL_CERT_"))
                }
                env.update(HOME=tmp, UV_HTTP_RETRIES="0", UV_PYTHON_DOWNLOADS="never")
                venv = root / "venv"
                subprocess.run(
                    [sys.executable, "-m", "venv", "--without-pip", str(venv)],
                    check=True,
                )
                command = [
                    "uv",
                    "--no-config",
                    "--no-cache",
                    "pip",
                    "install",
                    "--no-deps",
                    "--python",
                    str(venv / "bin/python"),
                    "--index-url",
                    f"https://localhost:{server.server_port}/simple/",
                    "adp-tls-fixture",
                ]
                rejected = subprocess.run(
                    command,
                    check=False,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert (
                    rejected.returncode != 0
                    and "certificate" in rejected.stderr.lower()
                ), rejected.stderr
                accepted = subprocess.run(
                    command,
                    check=False,
                    env={**env, "SSL_CERT_FILE": str(ca)},
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert accepted.returncode == 0, accepted.stderr
                subprocess.run(
                    [
                        str(venv / "bin/python"),
                        "-c",
                        "import adp_tls_fixture; assert adp_tls_fixture.value == 42",
                    ],
                    check=True,
                )
                uv_result = "untrusted rejected; trusted wheel installed"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    print(
        json.dumps(
            {
                "certifi": certifi.__version__,
                "removed_root_absent": True,
                "certifi_tls": "untrusted rejected; trusted accepted",
                "system_tls": "untrusted rejected; trusted accepted",
                "pip": pip.__version__,
                "pip_tls": "untrusted rejected; trusted wheel installed",
                "uv_tls": uv_result,
            }
        )
    )


if __name__ == "__main__":
    main()
