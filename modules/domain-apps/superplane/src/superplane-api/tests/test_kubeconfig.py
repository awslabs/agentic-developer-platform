"""Tests for kubeconfig export and EKS cluster authentication (U16b, issue #5057).

Covers the five behaviors the story names: valid token construction, exec configuration,
local known-CA TLS success, wrong-CA refusal, and brokered-default behavior.

Everything here is offline. AWS clients are stubbed, and the TLS cases use certificates
generated in-process and served by a local TLS socket on loopback — no network egress and
no AWS call. That matters because these assertions are the ones that fail if someone
reintroduces an unverified fallback, so they must be runnable on the credential-free CI
lane rather than deferred to a live environment.
"""

import base64
import datetime
import glob
import os
import socket
import ssl
import tempfile
import threading
import urllib.parse
import uuid
from unittest.mock import MagicMock, patch

import pytest
import yaml

from app.services.eks_auth import (
    CLUSTER_TOKEN_PREFIX,
    EksAuthError,
    build_cluster_token,
    decode_cluster_token,
    describe_cluster_ca,
    validate_ca_data,
    write_ca_bundle,
)
from app.services.kubeconfig import KubeconfigError, generate_kubeconfig

# Fixture credentials. Structurally valid but inert: SigV4 signing is arithmetic over
# these bytes and needs no live account, which is what keeps the token tests offline.
FAKE_CREDENTIALS = {
    "AccessKeyId": "ASIAIOSFODNN7EXAMPLE",
    "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "SessionToken": "FwoGZXIvYXdzEExampleSessionTokenValueOnly",
}


# ---- Certificate helpers (local, in-process) ----


def _generate_self_signed(common_name: str) -> tuple[bytes, bytes]:
    """Generate a self-signed cert + key PEM pair for ``common_name``.

    Used to build two *different* certificate authorities so the wrong-CA case is a
    genuine trust failure rather than a mocked assertion.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


@pytest.fixture(scope="module")
def cert_pair(tmp_path_factory):
    """The cluster's own CA/cert, and a second unrelated one to test refusal."""
    tmp = tmp_path_factory.mktemp("certs")
    good_cert, good_key = _generate_self_signed("localhost")
    other_cert, _ = _generate_self_signed("localhost")

    good_cert_path = tmp / "good.crt"
    good_key_path = tmp / "good.key"
    other_cert_path = tmp / "other.crt"
    good_cert_path.write_bytes(good_cert)
    good_key_path.write_bytes(good_key)
    other_cert_path.write_bytes(other_cert)

    return {
        "good_cert_pem": good_cert,
        "good_cert_path": str(good_cert_path),
        "good_key_path": str(good_key_path),
        "other_cert_pem": other_cert,
        "other_cert_path": str(other_cert_path),
        "good_b64": base64.b64encode(good_cert).decode(),
        "other_b64": base64.b64encode(other_cert).decode(),
    }


class _LocalTlsServer:
    """A one-shot TLS server on loopback, presenting the given cert."""

    def __init__(self, certfile: str, keyfile: str):
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
            try:
                with self._ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1024)
                    tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            except (ssl.SSLError, OSError):
                # Expected for the wrong-CA case: the client aborts the handshake.
                pass
            finally:
                conn.close()
        except OSError:
            pass

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._sock.close()


# ---- Token construction (R12: never an STS SessionToken) ----


class TestClusterTokenConstruction:
    """A cluster bearer token must be a signed, cluster-bound presigned STS request."""

    def test_token_has_k8s_aws_v1_prefix(self):
        token = build_cluster_token("my-cluster", FAKE_CREDENTIALS, "us-east-1")
        assert token.startswith(CLUSTER_TOKEN_PREFIX)

    def test_token_is_not_the_sts_session_token(self):
        """The defect this story fixes: SessionToken must never be the bearer token."""
        token = build_cluster_token("my-cluster", FAKE_CREDENTIALS, "us-east-1")
        assert token != FAKE_CREDENTIALS["SessionToken"]
        assert FAKE_CREDENTIALS["SessionToken"] not in token

    def test_token_decodes_to_presigned_get_caller_identity(self):
        token = build_cluster_token("my-cluster", FAKE_CREDENTIALS, "us-east-1")
        url = decode_cluster_token(token)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert query["Action"] == ["GetCallerIdentity"]
        assert "X-Amz-Signature" in query
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]

    def test_token_is_bound_to_the_named_cluster(self):
        """The cluster name is inside SignedHeaders, so a token cannot be replayed."""
        token = build_cluster_token("cluster-a", FAKE_CREDENTIALS, "us-east-1")
        url = decode_cluster_token(token)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert "x-k8s-aws-id" in query["X-Amz-SignedHeaders"][0]

    def test_tokens_for_different_clusters_differ(self):
        token_a = build_cluster_token("cluster-a", FAKE_CREDENTIALS, "us-east-1")
        token_b = build_cluster_token("cluster-b", FAKE_CREDENTIALS, "us-east-1")
        # Different signed cluster names produce different signatures.
        assert decode_cluster_token(token_a) != decode_cluster_token(token_b)

    def test_token_signed_against_requested_region(self):
        token = build_cluster_token("my-cluster", FAKE_CREDENTIALS, "eu-west-1")
        url = decode_cluster_token(token)
        assert "eu-west-1" in url

    def test_token_is_short_lived(self):
        token = build_cluster_token("my-cluster", FAKE_CREDENTIALS, "us-east-1")
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(decode_cluster_token(token)).query
        )
        assert int(query["X-Amz-Expires"][0]) <= 900

    def test_missing_cluster_name_refused(self):
        with pytest.raises(EksAuthError, match="Cluster name is required"):
            build_cluster_token("", FAKE_CREDENTIALS, "us-east-1")

    @pytest.mark.parametrize(
        "missing", ["AccessKeyId", "SecretAccessKey", "SessionToken"]
    )
    def test_incomplete_credentials_refused(self, missing):
        """An incomplete triple cannot sign; signing anyway yields a silently rejected token."""
        creds = dict(FAKE_CREDENTIALS)
        creds[missing] = ""
        with pytest.raises(EksAuthError, match="Incomplete STS credentials"):
            build_cluster_token("my-cluster", creds, "us-east-1")

    def test_malformed_token_rejected_on_decode(self):
        with pytest.raises(EksAuthError, match="not in k8s-aws-v1"):
            decode_cluster_token("Bearer abc123")


# ---- CA validation: verification is mandatory ----


class TestCaValidation:
    """A missing or malformed CA is a refusal, never an unverified connection."""

    def test_valid_ca_accepted(self, cert_pair):
        assert b"BEGIN CERTIFICATE" in validate_ca_data(cert_pair["good_b64"])

    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_absent_ca_refused(self, empty):
        with pytest.raises(EksAuthError, match="refusing to connect without"):
            validate_ca_data(empty)

    def test_non_base64_ca_refused(self):
        with pytest.raises(EksAuthError, match="not valid base64"):
            validate_ca_data("this-is-not-base64!!!")

    def test_base64_but_not_pem_refused(self):
        """Catches truncated or double-encoded values that would yield an empty trust store."""
        payload = base64.b64encode(b"not a certificate").decode()
        with pytest.raises(EksAuthError, match="does not contain a PEM certificate"):
            validate_ca_data(payload)

    def test_write_ca_bundle_writes_pem(self, cert_pair):
        path = write_ca_bundle(cert_pair["good_b64"])
        with open(path, "rb") as handle:
            assert handle.read() == cert_pair["good_cert_pem"]

    def test_write_ca_bundle_does_not_leak_a_file_per_call(self, cert_pair):
        """Identical CA data must reuse one file, not accumulate one per request.

        `create_k8s_client` writes a bundle on every brokered request, so a fresh temp
        file per call would grow /tmp without bound for the lifetime of the process. The
        file cannot simply be deleted on return: the Kubernetes client reads
        `ssl_ca_cert` from disk on each request, so the path must stay valid.
        """
        paths = {write_ca_bundle(cert_pair["good_b64"]) for _ in range(25)}
        assert len(paths) == 1

        leftovers = glob.glob(os.path.join(tempfile.gettempdir(), "eks-ca-*-*.crt"))
        assert leftovers == [], f"staging files not cleaned up: {leftovers}"

    def test_write_ca_bundle_separates_distinct_cas(self, cert_pair):
        """A rotated or different CA must get its own file, never reuse a stale one."""
        good = write_ca_bundle(cert_pair["good_b64"])
        other = write_ca_bundle(cert_pair["other_b64"])
        assert good != other
        with open(other, "rb") as handle:
            assert handle.read() == cert_pair["other_cert_pem"]

    def test_write_ca_bundle_rewrites_a_corrupted_leftover(self, cert_pair):
        """A tampered or truncated leftover must not be used as a trust anchor."""
        path = write_ca_bundle(cert_pair["good_b64"])
        with open(path, "wb") as handle:
            handle.write(b"corrupted-not-a-cert")

        assert write_ca_bundle(cert_pair["good_b64"]) == path
        with open(path, "rb") as handle:
            assert handle.read() == cert_pair["good_cert_pem"]

    def test_describe_cluster_ca_returns_stored_data(self, cert_pair):
        fake_eks = MagicMock()
        fake_eks.describe_cluster.return_value = {
            "cluster": {"certificateAuthority": {"data": cert_pair["good_b64"]}}
        }
        with patch("app.services.eks_auth.boto3.Session") as session:
            session.return_value.client.return_value = fake_eks
            result = describe_cluster_ca("my-cluster", FAKE_CREDENTIALS, "us-east-1")
        assert result == cert_pair["good_b64"]

    def test_describe_cluster_ca_refuses_when_cluster_reports_none(self):
        fake_eks = MagicMock()
        fake_eks.describe_cluster.return_value = {"cluster": {}}
        with patch("app.services.eks_auth.boto3.Session") as session:
            session.return_value.client.return_value = fake_eks
            with pytest.raises(EksAuthError, match="reported no CA certificate"):
                describe_cluster_ca("my-cluster", FAKE_CREDENTIALS, "us-east-1")


# ---- TLS: known-CA success and wrong-CA refusal, against a real local socket ----


class TestTlsVerification:
    """Exercises the actual TLS trust decision, not a mock of it."""

    def test_known_ca_verifies_successfully(self, cert_pair):
        """A client trusting the cluster's own CA completes the handshake."""
        ca_path = write_ca_bundle(cert_pair["good_b64"])
        with _LocalTlsServer(
            cert_pair["good_cert_path"], cert_pair["good_key_path"]
        ) as server:
            ctx = ssl.create_default_context(cafile=ca_path)
            with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
                with ctx.wrap_socket(sock, server_hostname="localhost") as tls:
                    assert tls.getpeercert() is not None

    def test_wrong_ca_is_refused(self, cert_pair):
        """A CA from a different cluster must fail the handshake, not be accepted."""
        wrong_ca_path = write_ca_bundle(cert_pair["other_b64"])
        with _LocalTlsServer(
            cert_pair["good_cert_path"], cert_pair["good_key_path"]
        ) as server:
            ctx = ssl.create_default_context(cafile=wrong_ca_path)
            with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
                with pytest.raises(ssl.SSLCertVerificationError):
                    ctx.wrap_socket(sock, server_hostname="localhost")


# ---- Kubeconfig export shape ----


class TestKubeconfigGeneration:
    """The exported file must describe how to get a token, not contain a credential."""

    def _generate(self, cert_pair, **overrides):
        kwargs = {
            "cluster_endpoint": "https://ABC123.gr7.us-east-1.eks.amazonaws.com",
            "cluster_ca_cert": cert_pair["good_b64"],
            "cluster_name": "superplane-ws-1",
            "workspace_aws_account_id": "123456789012",
            "workspace_name": "research-a",
        }
        kwargs.update(overrides)
        raw, expires_at = generate_kubeconfig(**kwargs)
        return yaml.safe_load(raw), raw, expires_at

    def test_uses_exec_block_not_a_token(self, cert_pair):
        config, raw, _ = self._generate(cert_pair)
        user = config["users"][0]["user"]
        assert "exec" in user
        assert "token" not in user
        # The STS SessionToken must not appear anywhere in the exported document.
        assert FAKE_CREDENTIALS["SessionToken"] not in raw

    def test_exec_block_invokes_aws_eks_get_token_for_this_cluster(self, cert_pair):
        config, _, _ = self._generate(cert_pair)
        exec_cfg = config["users"][0]["user"]["exec"]
        assert exec_cfg["command"] == "aws"
        assert exec_cfg["apiVersion"] == "client.authentication.k8s.io/v1beta1"
        args = exec_cfg["args"]
        assert "eks" in args and "get-token" in args
        assert args[args.index("--cluster-name") + 1] == "superplane-ws-1"

    def test_exec_block_assumes_the_workspace_role(self, cert_pair):
        config, _, _ = self._generate(cert_pair)
        args = config["users"][0]["user"]["exec"]["args"]
        role_arn = args[args.index("--role-arn") + 1]
        assert (
            role_arn
            == "arn:aws:iam::123456789012:role/superplane-workspace-research-a"
        )

    def test_exec_block_uses_the_cluster_region(self, cert_pair):
        config, _, _ = self._generate(cert_pair, region="eu-west-1")
        args = config["users"][0]["user"]["exec"]["args"]
        assert args[args.index("--region") + 1] == "eu-west-1"

    def test_external_id_delegates_to_a_local_profile(self, cert_pair):
        """An ExternalId can only reach STS via a profile, so the config must use one.

        `aws eks get-token` has no `--external-id` flag and the CLI honours no
        `AWS_EXTERNAL_ID` environment variable, so emitting one would produce a config
        that fails with AccessDenied against a trust policy that enforces the condition.
        """
        config, raw, _ = self._generate(
            cert_pair, external_id="tenant-external-id-xyz"
        )
        exec_cfg = config["users"][0]["user"]["exec"]
        args = exec_cfg["args"]

        assert args[args.index("--profile") + 1] == "superplane-research-a"
        # No env var: the CLI has none for this, so emitting one would only mislead.
        assert "env" not in exec_cfg
        assert "AWS_EXTERNAL_ID" not in raw

    def test_external_id_path_omits_role_arn(self, cert_pair):
        """`--role-arn` alongside a profile silently drops the ExternalId.

        Verified against aws-cli 2.36.48: with both set, the AssumeRole call that mints the
        token carries only RoleArn and RoleSessionName, so the tenant's condition is never
        satisfied. Delegating wholly to the profile is what makes the assumption carry it.
        """
        config, _, _ = self._generate(cert_pair, external_id="tenant-external-id-xyz")
        args = config["users"][0]["user"]["exec"]["args"]
        assert "--role-arn" not in args

    def test_no_profile_or_env_when_external_id_absent(self, cert_pair):
        """Without a condition to satisfy, the plugin assumes the role directly."""
        config, _, _ = self._generate(cert_pair, external_id=None)
        exec_cfg = config["users"][0]["user"]["exec"]
        args = exec_cfg["args"]
        assert "env" not in exec_cfg
        assert "--profile" not in args
        assert (
            args[args.index("--role-arn") + 1]
            == "arn:aws:iam::123456789012:role/superplane-workspace-research-a"
        )

    def test_ca_data_is_pinned_in_the_config(self, cert_pair):
        config, _, _ = self._generate(cert_pair)
        cluster = config["clusters"][0]["cluster"]
        assert cluster["certificate-authority-data"] == cert_pair["good_b64"]
        assert cluster["server"].startswith("https://")

    def test_never_emits_insecure_skip_tls_verify(self, cert_pair):
        _, raw, _ = self._generate(cert_pair)
        assert "insecure-skip-tls-verify" not in raw

    def test_missing_ca_refuses_generation(self, cert_pair):
        """Without a CA the config would trust the system store instead of the cluster."""
        with pytest.raises(KubeconfigError, match="refusing to connect without"):
            self._generate(cert_pair, cluster_ca_cert="")

    def test_malformed_ca_refuses_generation(self, cert_pair):
        with pytest.raises(KubeconfigError, match="not valid base64"):
            self._generate(cert_pair, cluster_ca_cert="!!!not-base64!!!")

    def test_missing_cluster_name_refuses_generation(self, cert_pair):
        with pytest.raises(KubeconfigError, match="Cluster name is required"):
            self._generate(cert_pair, cluster_name="")

    def test_missing_endpoint_refuses_generation(self, cert_pair):
        with pytest.raises(KubeconfigError, match="Cluster endpoint is required"):
            self._generate(cert_pair, cluster_endpoint="")

    def test_context_is_wired_to_the_cluster_and_user(self, cert_pair):
        config, _, _ = self._generate(cert_pair)
        context = config["contexts"][0]
        assert context["context"]["cluster"] == "superplane-ws-1"
        assert context["context"]["user"] == config["users"][0]["name"]
        assert config["current-context"] == context["name"]

    def test_exec_block_is_non_interactive(self, cert_pair):
        """kubectl must not prompt; the plugin returns its token on stdout."""
        config, _, _ = self._generate(cert_pair)
        assert config["users"][0]["user"]["exec"]["interactiveMode"] == "Never"

    def test_expiry_is_in_the_future(self, cert_pair):
        _, _, expires_at = self._generate(cert_pair)
        assert expires_at > datetime.datetime.now(datetime.timezone.utc)


# ---- Brokered access is the default; export is exceptional ----


class TestBrokeredDefault:
    """Workload operations go through the brokered proxy; export grants nothing itself."""

    def test_exported_config_alone_grants_no_access(self, cert_pair):
        """Every value in the file is non-secret: an endpoint, a public CA, a role ARN.

        This is what makes export tolerable as an exceptional path — the holder still has
        to be authorized to assume the role before the exec plugin can obtain a token.
        """
        config, raw = self._exported(cert_pair)
        user = config["users"][0]["user"]
        assert set(user.keys()) == {"exec"}
        for secret_ish in ("SecretAccessKey", "SessionToken", "AccessKeyId"):
            assert secret_ish not in raw
        # The CA is a public certificate, not a credential.
        assert b"PRIVATE KEY" not in base64.b64decode(
            config["clusters"][0]["cluster"]["certificate-authority-data"]
        )

    def _exported(self, cert_pair):
        raw, _ = generate_kubeconfig(
            cluster_endpoint="https://ABC.gr7.us-east-1.eks.amazonaws.com",
            cluster_ca_cert=cert_pair["good_b64"],
            cluster_name="superplane-ws-1",
            workspace_aws_account_id="123456789012",
            workspace_name="research-a",
        )
        return yaml.safe_load(raw), raw

    @pytest.mark.asyncio
    async def test_brokered_client_setup_uses_signed_token_and_verified_tls(
        self, cert_pair
    ):
        """The default path: get_k8s_clients yields a client with a signed token and CA.

        Asserted at the Configuration the Kubernetes client is actually built with, so a
        regression that reintroduced verify_ssl = False or the SessionToken would fail
        here rather than only in a live environment.
        """
        from app.models.cluster import Cluster
        from app.models.workspace import Workspace
        from app.services import proxy as proxy_module

        workspace = Workspace(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="research-a",
            isolation_mode="dedicated",
            status="Active",
        )
        workspace.cluster_id = uuid.uuid4()
        cluster = Cluster(
            id=workspace.cluster_id,
            org_id=workspace.org_id,
            name="display-name",
            eks_cluster_arn="arn:aws:eks:eu-west-1:123456789012:cluster/real-eks-name",
            endpoint="https://ABC.gr7.eu-west-1.eks.amazonaws.com",
            status="Active",
        )

        captured = {}
        real_create = proxy_module.create_k8s_client

        def _capture(**kwargs):
            captured.update(kwargs)
            return real_create(**kwargs)

        with (
            patch.object(
                proxy_module,
                "get_workspace_cluster",
                return_value=(workspace, cluster),
            ),
            patch.object(
                proxy_module,
                "_get_workspace_external_id",
                return_value="tenant-xyz",
            ),
            patch.object(
                proxy_module,
                "assume_role_for_cluster",
                return_value=FAKE_CREDENTIALS,
            ) as assume,
            patch.object(
                proxy_module,
                "describe_cluster_ca",
                return_value=cert_pair["good_b64"],
            ),
            patch.object(proxy_module, "create_k8s_client", _capture),
        ):
            core_api, apps_api, _, _ = await proxy_module.get_k8s_clients(
                workspace.id, workspace.org_id, MagicMock()
            )

        # Brokered with the tenant's stored ExternalId, in the cluster's own region,
        # under the cluster's real EKS name — not the row's display name.
        assert assume.call_args.kwargs["external_id"] == "tenant-xyz"
        assert captured["cluster_name"] == "real-eks-name"
        assert captured["region"] == "eu-west-1"

        config = core_api.api_client.configuration
        assert config.verify_ssl is True
        assert config.ssl_ca_cert is not None
        token = config.api_key["BearerToken"]
        assert token.startswith(CLUSTER_TOKEN_PREFIX)
        assert token != FAKE_CREDENTIALS["SessionToken"]
        assert apps_api is not None

    def test_client_refuses_to_build_without_a_ca(self):
        """No CA means no client — there is no unverified fallback to reach."""
        from app.services.proxy import ProxyError, create_k8s_client

        with pytest.raises(ProxyError, match="refusing to connect without"):
            create_k8s_client(
                cluster_endpoint="https://example.eks.amazonaws.com",
                cluster_ca_data="",
                credentials=FAKE_CREDENTIALS,
                cluster_name="my-cluster",
                region="us-east-1",
            )

    def test_verify_ssl_cannot_be_disabled_by_any_input(self, cert_pair):
        """Guard against the removed insecure branch returning."""
        from app.services.proxy import create_k8s_client

        client = create_k8s_client(
            cluster_endpoint="https://example.eks.amazonaws.com",
            cluster_ca_data=cert_pair["good_b64"],
            credentials=FAKE_CREDENTIALS,
            cluster_name="my-cluster",
            region="us-east-1",
        )
        assert client.configuration.verify_ssl is True

    def test_proxy_source_contains_no_verify_ssl_false(self):
        """A source-level assertion, because this is the exact line that caused the defect.

        A future edit that reintroduces `verify_ssl = False` in the proxy path fails here
        even if it is on a branch no other test happens to exercise.
        """
        import inspect

        from app.services import proxy as proxy_module

        source = inspect.getsource(proxy_module)
        normalized = source.replace(" ", "")
        assert "verify_ssl=False" not in normalized
        assert 'api_key={"BearerToken":credentials["SessionToken"]}' not in normalized
