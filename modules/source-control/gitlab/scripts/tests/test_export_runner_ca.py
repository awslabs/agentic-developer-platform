"""Synthetic certificate and fake-provider checks for strict CA export."""

import hashlib
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


spec = importlib.util.spec_from_file_location("export_runner_ca", Path(__file__).parents[1] / "export-runner-ca.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)
ARN = "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-4000-8000-000000000001"
URL = "https://gitlab.example.test/gitlab"


def certificates(*, hostname="gitlab.example.test", expired=False):
    root_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic approved root")])
    root = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(root_key.public_key())
            .serial_number(100).not_valid_before(datetime(2020, 1, 1, tzinfo=UTC))
            .not_valid_after(datetime(2090, 1, 1, tzinfo=UTC))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(root_key, hashes.SHA256()))
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(name).public_key(leaf_key.public_key()).serial_number(101)
            .not_valid_before(datetime(2010, 1, 1, tzinfo=UTC))
            .not_valid_after(datetime(2020 if expired else 2090, 1, 1, tzinfo=UTC))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(root_key, hashes.SHA256()))
    return (leaf.public_bytes(serialization.Encoding.PEM).decode(),
            root.public_bytes(serialization.Encoding.PEM).decode())


def fixture(tmp_path, *, hostname="gitlab.example.test", expired=False, chain=True, response=None):
    leaf, root = certificates(hostname=hostname, expired=expired)
    root_path = tmp_path / "approved-root.pem"
    root_path.write_text(root)
    acm = SimpleNamespace(get_certificate=lambda **kwargs: {
        "Certificate": leaf, "CertificateChain": root if chain else None,
    })
    secrets = FakeCaSecrets()
    seen = []

    def request(query):
        seen.append(query.url)
        assert "PRIVATE-TOKEN" not in query.headers and "Authorization" not in query.headers
        if query.url.path.endswith("/api/v4/version"):
            return httpx.Response(401, json={"message": "401 Unauthorized"})
        assert query.url.path == "/gitlab/-/liveness"
        return response or httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(request)
    return acm, secrets, root_path, hashlib.sha256(root.encode()).hexdigest(), transport, seen


class FakeCaSecrets:
    def __init__(self):
        self.value = None
        self.puts = 0

    def put_secret_value(self, **kwargs):
        assert kwargs["SecretId"] == "adp/dev/gitlab/ca-chain"
        self.value = kwargs["SecretString"]
        self.puts += 1
        return {"VersionId": "synthetic-version-id"}

    def get_secret_value(self, **kwargs):
        assert kwargs["SecretId"] == "adp/dev/gitlab/ca-chain"
        assert kwargs["VersionId"] == "synthetic-version-id"
        return {"SecretString": self.value}


def export(acm, secrets, root_path, fingerprint, transport):
    return exporter.export_ca(acm, secrets, "dev", ARN, URL, str(root_path), fingerprint, transport=transport)


def test_valid_certificate_exports_only_approved_ca_and_probes_private_route(tmp_path):
    acm, secrets, root_path, fingerprint, transport, seen = fixture(tmp_path)
    assert export(acm, secrets, root_path, fingerprint, transport) == {
        "status": "exported", "version_id": "synthetic-version-id",
    }
    assert secrets.puts == 1
    assert secrets.value == root_path.read_text()
    assert seen == [URL + "/-/liveness"]


@pytest.mark.parametrize("failure", ["wrong_host", "expired", "missing_chain", "unapproved_root", "bad_fingerprint"])
def test_wrong_or_incomplete_trust_refuses_before_storage(tmp_path, failure):
    acm, secrets, root_path, fingerprint, transport, seen = fixture(
        tmp_path, hostname="other.example.test" if failure == "wrong_host" else "gitlab.example.test",
        expired=failure == "expired", chain=failure != "missing_chain",
    )
    if failure == "unapproved_root":
        _, unrelated_root = certificates()
        root_path.write_text(unrelated_root)
        fingerprint = hashlib.sha256(unrelated_root.encode()).hexdigest()
    if failure == "bad_fingerprint":
        fingerprint = "0" * 64
    with pytest.raises(exporter.CaExportError):
        export(acm, secrets, root_path, fingerprint, transport)
    assert secrets.puts == 0
    assert seen == []


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": "https://public.example.test/gitlab"}),
    httpx.Response(401, json={"message": "401 Unauthorized"}),
    httpx.Response(403, text="Forbidden"),
    httpx.Response(503, json={"status": "failed"}),
    httpx.Response(500, text="synthetic-private-provider-payload"),
    httpx.Response(200, text="synthetic-private-provider-payload"),
    httpx.Response(200, json={"version": "synthetic-version"}),
    httpx.Response(200, json={"status": "failed"}),
    httpx.Response(200, json=[{"status": "ok"}]),
])
def test_redirect_or_invalid_private_route_never_exports(tmp_path, response):
    acm, secrets, root_path, fingerprint, transport, seen = fixture(tmp_path, response=response)
    with pytest.raises(exporter.CaExportError) as error:
        export(acm, secrets, root_path, fingerprint, transport)
    assert "synthetic-private-provider-payload" not in str(error.value)
    assert secrets.puts == 0
    assert seen == [URL + "/-/liveness"]


def test_cli_outputs_version_id_only_and_never_certificate_or_error_body(tmp_path, capsys):
    acm, secrets, root_path, fingerprint, transport, _ = fixture(tmp_path)
    sdk = SimpleNamespace(client=lambda service: {"acm": acm, "secretsmanager": secrets}[service])
    args = ["--environment", "dev", "--region", "us-east-1", "--certificate-arn", ARN,
            "--runner-url", URL, "--approved-root-file", str(root_path), "--approved-root-sha256", fingerprint]
    assert exporter.main(args, sdk=sdk, transport=transport) == 0
    output = capsys.readouterr()
    assert "synthetic-version-id" in output.out
    assert "BEGIN CERTIFICATE" not in output.out + output.err
    assert "gitlab.example.test" not in output.out + output.err


@pytest.mark.parametrize("failure", [False, True])
def test_cli_suppresses_sdk_parser_output_and_restores_logging(tmp_path, capsys, caplog, monkeypatch, failure):
    import json
    import logging

    from botocore.parsers import create_parser
    from botocore.session import get_session

    caplog.set_level(logging.DEBUG)
    acm, secrets, root_path, fingerprint, transport, _ = fixture(tmp_path)
    shape = get_session().get_service_model("secretsmanager").operation_model("GetSecretValue").output_shape
    previous = logging.root.manager.disable

    def read(**kwargs):
        response = create_parser("json").parse({
            "status_code": 200, "headers": {}, "body": json.dumps({"SecretString": secrets.value}).encode(),
        }, shape)
        if failure:
            raise RuntimeError(secrets.value)
        return response

    monkeypatch.setattr(secrets, "get_secret_value", read)
    sdk = SimpleNamespace(client=lambda service: {"acm": acm, "secretsmanager": secrets}[service])
    args = ["--environment", "dev", "--region", "us-east-1", "--certificate-arn", ARN,
            "--runner-url", URL, "--approved-root-file", str(root_path), "--approved-root-sha256", fingerprint]
    assert exporter.main(args, sdk=sdk, transport=transport) == (3 if failure else 0)
    output = capsys.readouterr()
    assert "BEGIN CERTIFICATE" not in output.out + output.err + caplog.text
    assert logging.root.manager.disable == previous
    logging.getLogger("botocore.parsers").debug("ca-sdk-logging-restored")
    assert "ca-sdk-logging-restored" in caplog.text


def test_unrelated_chain_certificate_is_never_added_to_trust(tmp_path):
    acm, secrets, root_path, fingerprint, transport, seen = fixture(tmp_path)
    _, unrelated_root = certificates()
    leaf = acm.get_certificate(CertificateArn=ARN)["Certificate"]
    acm.get_certificate = lambda **kwargs: {"Certificate": leaf, "CertificateChain": unrelated_root}
    with pytest.raises(exporter.CaExportError, match="certificate_not_verified"):
        export(acm, secrets, root_path, fingerprint, transport)
    assert secrets.puts == 0
    assert seen == []


def test_cli_provider_failure_does_not_print_private_certificate(tmp_path, capsys):
    acm, secrets, root_path, fingerprint, transport, _ = fixture(tmp_path)

    def failure(**kwargs):
        raise RuntimeError("synthetic-private-provider-payload")

    acm.get_certificate = failure
    sdk = SimpleNamespace(client=lambda service: {"acm": acm, "secretsmanager": secrets}[service])
    args = ["--environment", "dev", "--region", "us-east-1", "--certificate-arn", ARN,
            "--runner-url", URL, "--approved-root-file", str(root_path), "--approved-root-sha256", fingerprint]
    assert exporter.main(args, sdk=sdk, transport=transport) == 3
    output = capsys.readouterr()
    assert output.out == "" and output.err.strip() == "ca_provider_unavailable"
    assert "synthetic-private-provider-payload" not in output.err
    assert secrets.puts == 0
