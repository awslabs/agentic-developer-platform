"""Loopback-only HTTPS transport for original Security source subprocesses."""

import asyncio
import ipaddress
import json
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import jwt
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, Request


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@asynccontextmanager
async def loopback_gateway(app, directory, claims, signing_key):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Security offline test")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("offline.actions.githubusercontent.com")]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "localhost.crt", directory / "localhost.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.chmod(0o600)
    key_path.chmod(0o600)
    wrapper = FastAPI()

    @wrapper.get("/_offline_oidc")
    async def oidc(request: Request):
        assert request.headers["authorization"] == "Bearer offline-request-token"
        assert request.query_params["audience"] == "adp-operations-release-job-v1"
        return {
            "value": jwt.encode(
                {
                    **claims,
                    "iss": "https://token.actions.githubusercontent.com",
                    "aud": "adp-operations-release-job-v1",
                    "sub": "repo:" + claims["repository"] + ":ref:refs/heads/main",
                    "nbf": datetime.now(UTC),
                    "iat": datetime.now(UTC),
                    "exp": datetime.now(UTC) + timedelta(minutes=5),
                },
                signing_key,
                algorithm="RS256",
                headers={"kid": "offline-key"},
            )
        }

    wrapper.mount("/api", app)
    wrapper.mount("/", app)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            wrapper, host="127.0.0.1", port=port, lifespan="off", log_level="critical", ssl_keyfile=str(key_path), ssl_certfile=str(cert_path)
        )
    )
    running = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if running.done():
                await running
                raise AssertionError("offline server failed to start")
            await asyncio.sleep(0.01)
        yield f"https://127.0.0.1:{port}", str(cert_path), f"https://offline.actions.githubusercontent.com:{port}/_offline_oidc"
    finally:
        server.should_exit = True
        await asyncio.wait_for(running, 10)
        sock.close()


def isolated_child_transport(directory):
    """Fail any browser import or external network access in child processes."""
    path = directory / "sitecustomize.py"
    path.write_text("""import importlib.abc, ipaddress, socket, sys
class NoBrowser(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"playwright", "selenium", "pyppeteer"}:
            raise AssertionError("Browser entrypoints are poisoned in original Security integration")
sys.meta_path.insert(0, NoBrowser())
original_getaddrinfo = socket.getaddrinfo
def offline_dns(host, *args, **kwargs):
    if host == "offline.actions.githubusercontent.com":
        host = "127.0.0.1"
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise AssertionError("Offline integration refused external network host")
    return original_getaddrinfo(host, *args, **kwargs)
socket.getaddrinfo = offline_dns
original_connect = socket.socket.connect
def offline_connect(self, address):
    if self.family in {socket.AF_INET, socket.AF_INET6} and not ipaddress.ip_address(address[0]).is_loopback:
        raise AssertionError("Offline integration refused external network address")
    return original_connect(self, address)
socket.socket.connect = offline_connect
""")
    path.chmod(0o600)
    plugin = directory / "offline_security_pytest.py"
    plugin.write_text("""def pytest_sessionstart(session):
    from clients_setup import LiveSetupDriver
    def forbidden(*args, **kwargs):
        raise AssertionError("Legacy browser operation is poisoned")
    LiveSetupDriver.ui_edit = forbidden
    LiveSetupDriver.ui_observations = forbidden
""")
    plugin.chmod(0o600)
    return str(directory)


def cli_profile(case, directory, base):
    base = base + "/api"
    root = directory / ("cli-" + str(case.number))
    root.mkdir(mode=0o700)
    deployment_id = "d" + str(case.number).zfill(16)
    deployments = root / "deployments"
    deployments.mkdir(mode=0o700)
    profile = deployments / deployment_id
    profile.mkdir(mode=0o700)
    private_json(
        root / "deployments.json", {"schema_version": 1, "default": "offline", "deployments": {"offline": {"id": deployment_id, "gateway_url": base}}}
    )
    private_json(
        profile / "config.json",
        {
            "gateway_url": base,
            "user_pool_id": "us-east-1_offline",
            "client_id": "offline-client",
            "identity_pool_id": "offline",
            "region": "us-east-1",
        },
    )
    private_json(
        profile / "tokens.json",
        {
            "access_token": case.tokens["cli"],
            "id_token": case.tokens["cli"],
            "refresh_token": "offline-unused",
            "expires_at": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
    )
    return str(root), deployment_id
